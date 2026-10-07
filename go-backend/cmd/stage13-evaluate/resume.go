package main

import (
	"context"
	"fmt"
	"log/slog"
	"path/filepath"
	"time"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"github.com/jackc/pgx/v5/pgxpool"
)

// resume 消费原 durable 状态；不发布新资产、不刷新输入、不重试已 terminal 的 Attempt。
func resume(ctx context.Context, out string, pool *pgxpool.Pool, k postgres.Evaluation, epoch int64, plan provider.Stage13PolicyDocument) {
	var ids []string
	load(filepath.Join(out, "main-run-ids.json"), &ids)
	var policy struct {
		Ref asset.Ref `json:"policy_ref"`
	}
	load(filepath.Join(out, "frozen-policy.json"), &policy)
	var first ev.RunState
	scope := asset.Scope{ProjectID: "71300005-0000-4000-8000-000000000001", OrganizationID: "71300005-0000-4000-8000-000000000002", Principal: "system:stage13-wp05", CanPublish: true}
	s := ev.Scope{Scope: scope, Epoch: epoch, Create: true, Execute: true, Evaluate: true, Coordinate: true, Repair: true}
	var err error
	first, err = k.ReadRunState(ctx, scope, ids[0])
	must(err)
	router := targets{}
	for _, id := range ids {
		state, e := k.ReadRunState(ctx, scope, id)
		must(e)
		var cfg provider.Stage13Config
		must(state.Run.Snapshot.Target.Config.Decode(&cfg))
		target, e := provider.NewStage13Target(cfg)
		must(e)
		router[state.Run.Snapshot.Target.Config.Digest()] = target
		defer target.Close()
	}
	save(out, "recovery-before", map[string]any{"run_ids": ids, "case_count": len(first.Attempts), "input_refreshed": false, "retry_created": false})
	k.Capabilities, err = k.LoadEvaluatorCapabilities(ctx, agentquality.DeterministicSupported)
	must(err)
	wc := worker.DefaultConfig()
	wc.WorkerID = "system:wp05-recovery"
	wc.ExecutionConcurrency = 2
	wc.EvaluatorConcurrency = 2
	wc.ScanBatch = 1
	wc.PollInterval = 250 * time.Millisecond
	rt := worker.Runtime{Config: wc, Backend: k, Target: router, Evaluator: agentquality.DeterministicEvaluator{}, Epoch: epoch, Log: slog.Default()}
	runctx, cancel := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- rt.Run(runctx) }()
	defer func() { cancel(); must(<-done) }()
	limit := time.Now().Add(15 * time.Minute)
	last := -1
	for {
		var count int
		must(pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_attempts WHERE run_id=ANY($1::uuid[]) AND status='TERMINAL'", ids).Scan(&count))
		if count != last {
			fmt.Printf("recovery attempts terminal=%d/48\n", count)
			last = count
		}
		var pending int
		must(pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_runs WHERE id=ANY($1::uuid[]) AND status IN ('PENDING','RUNNING')", ids).Scan(&pending))
		if pending == 0 {
			break
		}
		if time.Now().After(limit) {
			panic("RECOVERY_TERMINAL_TIMEOUT")
		}
		time.Sleep(2 * time.Second)
	}
	states := []ev.RunState{}
	for i, id := range ids {
		state, e := k.ReadRunState(ctx, scope, id)
		must(e)
		states = append(states, state)
		name := []string{"baseline", "candidate"}[i]
		save(out, name+"-run-state", state)
		save(out, name+"-results", state.Results)
	}
	finish(ctx, out, postgres.Decisions{Pool: pool}, s, plan, states, ids, policy.Ref)
}
