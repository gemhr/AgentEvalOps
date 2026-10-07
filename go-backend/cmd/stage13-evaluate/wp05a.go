package main

import (
	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"context"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	"log/slog"
	"path/filepath"
	"time"
)

// 终态只读重建 source；沿用原 Gate command，不启动 Worker 或重新派发模型。
func replayWP05A(ctx context.Context, out string, k postgres.Evaluation, epoch int64, plan provider.Stage13PolicyDocument) {
	var ids []string
	load(filepath.Join(out, "main-run-ids.json"), &ids)
	var old ev.RunState
	load(filepath.Join(out, "old-baseline-run-state.json"), &old)
	var frozen struct {
		Ref asset.Ref `json:"policy_ref"`
	}
	load(filepath.Join(out, "frozen-policy.json"), &frozen)
	s := ev.Scope{Scope: asset.Scope{ProjectID: old.Run.ProjectID, OrganizationID: "71300005-0000-4000-8000-000000000002", Principal: "system:stage13-wp05a", CanPublish: true}, Epoch: epoch, Create: true, Coordinate: true}
	states := []ev.RunState{}
	sources := []decision.Source{}
	for _, id := range ids {
		state, err := k.ReadRunState(ctx, s.Scope, id)
		must(err)
		source, err := decision.Offline(decision.SourceRef{Kind: "RUN_SET", Runs: []string{id}}, []ev.RunState{state})
		must(err)
		if len(source.Reasons) != 0 {
			panic(source.Reasons)
		}
		states = append(states, state)
		sources = append(sources, source)
	}
	save(out, "actual-source-validation", map[string]any{"PASS": true, "sources": sources, "new_model_calls": 0, "validation": "independent reconstruction of actual query, execution request, semantic digest and policy"})
	finish(ctx, out, postgres.Decisions{Pool: k.Pool}, s, plan, states, ids, frozen.Ref)
}

// 受控 harness 的固定 pair 顺序；不改变 generic Kernel scanner。
type pairedBackend struct {
	postgres.Evaluation
	order  []string
	claims []map[string]any
}

func (b *pairedBackend) Candidates(ctx context.Context, kind postgres.CandidateKind, cursor postgres.EvaluationCursor, limit int) ([]postgres.EvaluationCandidate, error) {
	if kind != postgres.PendingExecution {
		return b.Evaluation.Candidates(ctx, kind, cursor, limit)
	}
	var c postgres.EvaluationCandidate
	err := b.Pool.QueryRow(ctx, `SELECT project_id::text,run_id::text,id::text FROM evaluation_attempts WHERE id=ANY($1::uuid[]) AND status='PENDING' ORDER BY array_position($1::uuid[],id) LIMIT 1`, b.order).Scan(&c.ProjectID, &c.RunID, &c.AttemptID)
	if err != nil {
		if err.Error() == "no rows in result set" {
			return nil, nil
		}
		return nil, err
	}
	return []postgres.EvaluationCandidate{c}, nil
}
func (b *pairedBackend) ClaimExecutionAttempt(ctx context.Context, s ev.Scope, o ev.Owned, owner string, d time.Duration) (ev.Reply, error) {
	start := time.Now()
	reply, err := b.Evaluation.ClaimExecutionAttempt(ctx, s, o, owner, d)
	b.claims = append(b.claims, map[string]any{"attempt_id": o.AttemptID, "claim_latency_ms": float64(time.Since(start).Microseconds()) / 1000, "code": reply.Code})
	return reply, err
}

func executeWP05A(ctx context.Context, out string, pool *pgxpool.Pool, k postgres.Evaluation, epoch int64, plan provider.Stage13PolicyDocument) {
	var old ev.RunState
	load(filepath.Join(out, "old-baseline-run-state.json"), &old)
	var frozen struct {
		Ref asset.Ref `json:"policy_ref"`
	}
	load(filepath.Join(out, "frozen-policy.json"), &frozen)
	s := ev.Scope{Scope: asset.Scope{ProjectID: old.Run.ProjectID, OrganizationID: "71300005-0000-4000-8000-000000000002", Principal: "system:stage13-wp05a", CanPublish: true}, Epoch: epoch, Create: true, Execute: true, Evaluate: true, Coordinate: true, Repair: true}
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: pool}, Datasets: postgres.Datasets{Pool: pool}, Suites: postgres.Suites{Pool: pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: pool}}
	k.Capabilities, _ = k.LoadEvaluatorCapabilities(ctx, agentquality.DeterministicSupported)
	var configs [2]provider.Stage13Config
	var oldCandidate ev.RunState
	load(filepath.Join(out, "old-candidate-run-state.json"), &oldCandidate)
	must(old.Run.Snapshot.Target.Config.Decode(&configs[0]))
	must(oldCandidate.Run.Snapshot.Target.Config.Decode(&configs[1]))
	router := targets{}
	for i := range configs {
		configs[i].ExecutionPolicyVersion = provider.Stage13ExecutionPolicyVersion
		target, err := provider.NewStage13Target(configs[i])
		must(err)
		target.ReserveExecution = k.ReserveStage13Execution
		router[freeze(configs[i]).Digest()] = target
		defer target.Close()
	}
	create := func(selected []asset.Ref) ([]string, []string) {
		ids := []string{}
		attempts := [2]map[string]string{{}, {}}
		for i, cfg := range configs {
			suite := old.Run.Snapshot.Input.Suite.Ref
			snapshot, err := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, s.Scope, ev.BuildRunSnapshot{Suite: &suite, SelectedCases: selected})
			must(err)
			st := []ev.RunState{old, oldCandidate}[i]
			reply, err := k.CreateRun(ctx, s, ev.CreateRun{CommandID: asset.NewID(), Snapshot: snapshot, Target: ev.Target{ID: provider.Stage13TargetID, Kind: "LOCALAGENT_HTTP", Version: provider.Stage13TargetVersion, Config: freeze(cfg), TimeoutMilliseconds: 180000}, Subject: st.Run.Snapshot.Subject, Retry: st.Run.Snapshot.Retry})
			must(err)
			if reply.Code != ev.Applied {
				panic(reply)
			}
			ids = append(ids, reply.ID)
			state, err := k.ReadRunState(ctx, s.Scope, reply.ID)
			must(err)
			for _, a := range state.Attempts {
				attempts[i][a.CaseID] = a.ID
			}
		}
		order := []string{}
		for _, c := range old.Run.Snapshot.Input.Manifest {
			if id, ok := attempts[0][c.Identity.Ref.EntityID]; ok {
				order = append(order, id, attempts[1][c.Identity.Ref.EntityID])
			}
		}
		return ids, order
	}
	run := func(ids, order []string, label string) []ev.RunState {
		b := &pairedBackend{Evaluation: k, order: order}
		wc := worker.DefaultConfig()
		wc.WorkerID = "system:wp05a-" + label
		wc.ExecutionConcurrency = 1
		wc.EvaluatorConcurrency = 1
		wc.ScanBatch = 1
		wc.PollInterval = 100 * time.Millisecond
		rt := worker.Runtime{Config: wc, Backend: b, Target: router, Evaluator: agentquality.DeterministicEvaluator{}, Epoch: epoch, Log: slog.Default()}
		// 独立整批停止条件，不进入 Agent input，也不刷新任何 Attempt deadline。
		runctx, cancel := context.WithTimeout(ctx, 2*time.Hour)
		done := make(chan error, 1)
		go func() { done <- rt.Run(runctx) }()
		defer func() {
			cancel()
			must(<-done)
			save(out, label+"-scheduler-observation", map[string]any{"execution_workers": 1, "evaluator_workers": 1, "provider_call_capacity": 1, "order": order, "claim_observations": b.claims, "claims": rt.Counters.Claims.Load(), "lost_races": rt.Counters.LostRaces.Load(), "controller": "2s scalar count, no full RunState polling", "overall_budget_seconds": 7200})
		}()
		last := -1
		for runctx.Err() == nil {
			var pending, terminal int
			must(pool.QueryRow(ctx, `SELECT count(*) FILTER(WHERE status!='TERMINAL'),count(*) FILTER(WHERE status='TERMINAL') FROM evaluation_attempts WHERE run_id=ANY($1::uuid[])`, ids).Scan(&pending, &terminal))
			if terminal != last {
				fmt.Printf("%s terminal=%d/%d\n", label, terminal, len(order))
				last = terminal
			}
			var active int
			must(pool.QueryRow(ctx, `SELECT count(*) FROM evaluation_runs WHERE id=ANY($1::uuid[]) AND status IN ('PENDING','RUNNING')`, ids).Scan(&active))
			if pending == 0 && active == 0 {
				break
			}
			time.Sleep(2 * time.Second)
		}
		if runctx.Err() != nil {
			panic("RUN_OVERALL_DEADLINE_NOT_MODEL_FAILURE")
		}
		states := []ev.RunState{}
		for _, id := range ids {
			st, err := k.ReadRunState(ctx, s.Scope, id)
			must(err)
			states = append(states, st)
		}
		return states
	}
	cohort := []asset.Ref{}
	for _, c := range old.Run.Snapshot.Input.Manifest {
		if len(cohort) < 3 {
			cohort = append(cohort, c.Identity.Ref)
		}
	}
	ids, order := create(cohort)
	save(out, "small-cohort-run-ids", ids)
	small := run(ids, order, "small")
	save(out, "small-cohort", small)
	for _, st := range small {
		for _, a := range st.Attempts {
			if a.Outcome != ev.Success {
				panic("SMALL_COHORT_EXECUTION_BLOCKED")
			}
		}
	}
	// 验证已 materialize 的 semantic binding 后再开始正式 24×2。
	for _, st := range small {
		for _, a := range st.Attempts {
			var raw []byte
			must(pool.QueryRow(ctx, `SELECT request_bytes FROM evaluation_stage13_execution_requests WHERE attempt_id=$1`, a.ID).Scan(&raw))
			body, err := asset.ParseJSON(raw)
			must(err)
			var m map[string]asset.JSON
			_ = body.Decode(&m)
			var p citriage.ExecutionPolicy
			must(m["execution_policy"].Decode(&p))
			semantic, err := citriage.SemanticInput(a.Request.Case.Case.Input)
			must(err)
			if p.SemanticDigest != semantic.Digest() || p.DeadlineAt.Sub(p.StartedAt) != 180*time.Second {
				panic("SMALL_POLICY_BINDING_FAILED")
			}
		}
	}
	ids, order = create(nil)
	save(out, "main-run-ids", ids)
	save(out, "deterministic-schedule", order)
	states := run(ids, order, "main")
	for i, st := range states {
		name := []string{"baseline", "candidate"}[i]
		save(out, name+"-run-state", st)
		save(out, name+"-results", st.Results)
	}
	finish(ctx, out, postgres.Decisions{Pool: pool}, s, plan, states, ids, frozen.Ref)
}
