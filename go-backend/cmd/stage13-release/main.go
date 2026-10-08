// stage13-release 只装配冻结 Holdout 的正式 owners，不接入开发或交付执行。
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	gov "agentevalops/go-backend/internal/cigovernance"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/delivery"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"github.com/jackc/pgx/v5/pgxpool"
)

const holdoutProject = "ca57dddb-cc22-5272-8f81-8d56b73ba640"
const candidateDigest = "499f4286cad06f1da4c4cd1c55cd5c9f8d946df38e4710462e8062c6150b1d59"

func must(e error) {
	if e != nil {
		panic(e)
	}
}
func freeze(v any) asset.JSON { j, e := asset.Freeze(v); must(e); return j }
func load(p string, v any)    { b, e := os.ReadFile(p); must(e); must(json.Unmarshal(b, v)) }
func save(dir, name string, v any) {
	must(os.WriteFile(filepath.Join(dir, name+".json"), freeze(v).Bytes(), 0600))
}

type targets map[string]*provider.Stage13Target

func (t targets) Execute(ctx context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	if s.ProjectID != holdoutProject {
		return ev.Outcome{}, asset.ErrForbidden
	}
	if p := t[q.Target.Config.Digest()]; p != nil {
		return p.Execute(ctx, s, q)
	}
	return ev.Outcome{}, asset.ErrForbidden
}

func main() {
	if (len(os.Args) != 2 && !(len(os.Args) == 3 && os.Args[2] == "--gate-replay")) || os.Getenv("STAGE13_WP09_CONTROLLED") != "1" {
		panic("OPERATOR_ONLY")
	}
	out, e := filepath.Abs(os.Args[1])
	must(e)
	cfg, e := pgxpool.ParseConfig(os.Getenv("STAGE13_WP09_DATABASE_URL"))
	must(e)
	if cfg.ConnConfig.Host != "127.0.0.1" || cfg.ConnConfig.Port != 55432 || cfg.ConnConfig.Database != "stage13_wp09_evalops_test" {
		panic("ISOLATED_WP09_REQUIRED")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 7200*time.Second)
	defer cancel()
	pool, e := pgxpool.NewWithConfig(ctx, cfg)
	must(e)
	defer pool.Close()
	k := postgres.Evaluation{Pool: pool}
	epoch, e := k.VerifyWorker(ctx, gov.Schema)
	must(e)
	var org string
	must(pool.QueryRow(ctx, "SELECT org_id::text FROM projects WHERE id=$1", holdoutProject).Scan(&org))
	s := ev.Scope{Scope: asset.Scope{ProjectID: holdoutProject, OrganizationID: org, Principal: "system:stage13-wp09-release", CanPublish: true}, Epoch: epoch, Create: true, Execute: true, Evaluate: true, Coordinate: true, Repair: true}
	if len(os.Args) == 3 {
		replayGate(ctx, out, pool, k, s)
		return
	}
	if _, err := os.Stat(filepath.Join(out, "main-run-ids.json")); err == nil {
		var ids []string
		load(filepath.Join(out, "main-run-ids.json"), &ids)
		if len(ids) != 2 {
			panic("CONSUMPTION_RUN_SET_MISMATCH")
		}
		state, err := k.ReadRunState(ctx, s.Scope, ids[1])
		must(err)
		identity, err := gov.ReleaseSubject(state.Run.Snapshot.Target.Config)
		must(err)
		if state.Run.Snapshot.Intent != "RELEASE_EVALUATION" || identity.Digest != candidateDigest || state.Run.Snapshot.Input.Dataset.Ref.EntityID != "6fb67037-6512-562a-8227-e842851e6f0f" {
			panic("CONSUMPTION_BINDING_MISMATCH")
		}
		var cmd decision.Command
		load(filepath.Join(out, "gate-command.json"), &cmd)
		gate, err := (postgres.Decisions{Pool: pool}).GetGateReceipt(ctx, s.Scope, cmd.ID)
		must(err)
		saveConsumption(ctx, out, pool, ids[1], gate.GateID, string(gate.Decision))
		fmt.Println("HOLDOUT_ALREADY_CONSUMED; original Run/Gate returned; no Target/Worker started")
		return
	}
	var plan provider.Stage13PolicyDocument
	load(filepath.Join(out, "release-plan.json"), &plan)
	var cand map[string]asset.JSON
	must(plan.Candidate.Manifest.Decode(&cand))
	var digest string
	must(cand["subject_manifest_digest"].Decode(&digest))
	if digest != candidateDigest {
		panic("FROZEN_CANDIDATE_MISMATCH")
	}
	pre := provider.CompareStage13Models(plan)
	save(out, "comparability-preflight", pre)
	if !pre.Comparable {
		panic("PREFLIGHT_COMPARABILITY_BLOCKED")
	}
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: pool}, Datasets: postgres.Datasets{Pool: pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: pool}}
	var frozen struct {
		Dataset struct {
			Ref     asset.Ref  `json:"ref"`
			Digest  string     `json:"content_digest"`
			Content asset.JSON `json:"content"`
		} `json:"dataset"`
		Cases []struct {
			Ref     asset.Ref  `json:"ref"`
			Digest  string     `json:"content_digest"`
			Content asset.JSON `json:"content"`
		} `json:"cases"`
	}
	load(filepath.Join(out, "frozen-holdout.json"), &frozen)
	ds, e := readers.Datasets.GetDatasetVersion(ctx, s.Scope, frozen.Dataset.Ref)
	must(e)
	if ds.ContentDigest() != frozen.Dataset.Digest || freeze(ds.Content()).Digest() != frozen.Dataset.Content.Digest() || len(ds.Content().Body.Cases) != 6 || len(frozen.Cases) != 6 {
		panic("FROZEN_HOLDOUT_MISMATCH")
	}
	integrity := []map[string]any{}
	for i, ref := range ds.Content().Body.Cases {
		c, e := readers.Cases.GetCaseVersion(ctx, s.Scope, ref)
		must(e)
		if ref != frozen.Cases[i].Ref || c.ContentDigest() != frozen.Cases[i].Digest || freeze(c.Content()).Digest() != frozen.Cases[i].Content.Digest() {
			panic("FROZEN_CASE_MISMATCH")
		}
		integrity = append(integrity, map[string]any{"case_version": ref, "content_digest": c.ContentDigest(), "input_digest": c.Content().Body.Input.Digest(), "gt_digest": c.Content().Body.GroundTruth.Digest(), "review_provenance_digest": freeze(c.Content().Source).Digest()})
	}
	save(out, "holdout-manifest-verification", map[string]any{"PASS": true, "dataset_version": ds.Ref(), "dataset_digest": ds.ContentDigest(), "full_cases": integrity, "cases": 6, "role": "HOLDOUT", "intent": "RELEASE_EVALUATION"})
	// 只重绑定项目内资产引用；逐项复制冻结的公式、预算、重试和 Gate 规则。
	var historical ev.RunState
	load(filepath.Join(out, "historical-baseline-state.json"), &historical)
	var oldPolicy struct {
		Policy decision.Policy `json:"policy"`
	}
	load(filepath.Join(out, "frozen-policy-source.json"), &oldPolicy)
	ms := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
	es := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	bindings := []catalog.EvaluatorBinding{}
	mapping := map[asset.Ref]asset.Ref{}
	source := asset.Source{Kind: "CONTROLLED_RELEASE_BINDING", Ref: "stage13.wp09-release.v1", Principal: s.Principal}
	hs := asset.Scope{ProjectID: historical.Run.ProjectID, OrganizationID: "71300005-0000-4000-8000-000000000002", Principal: s.Principal}
	for _, spec := range historical.Run.Snapshot.Input.Evaluators {
		if !agentquality.DeterministicSupported(spec.Definition) {
			panic("JUDGE_FORBIDDEN")
		}
		def := spec.Definition
		refs := []asset.Ref{}
		for _, old := range def.OutputMetrics {
			previous, e := readers.MetricDefinitions.GetMetricDefinitionVersion(ctx, hs, old)
			must(e)
			r := asset.Ref{EntityID: gov.ID("wp09-metric", holdoutProject, old.EntityID), Version: old.Version}
			_, e = ms.CreateMetricDefinition(ctx, s.Scope, asset.Create{ID: r.EntityID, Name: previous.Content().Body.Name})
			must(e)
			_, e = ms.PublishMetricDefinitionVersion(ctx, s.Scope, asset.Publish[metric.Definition]{Ref: r, Body: previous.Content().Body, Source: source})
			must(e)
			refs = append(refs, r)
			mapping[old] = r
		}
		def.OutputMetrics = refs
		r := asset.Ref{EntityID: gov.ID("wp09-evaluator", holdoutProject, spec.Identity.Ref.EntityID), Version: spec.Identity.Ref.Version}
		_, e = es.CreateEvaluatorDefinition(ctx, s.Scope, asset.Create{ID: r.EntityID, Name: "WP09 " + def.Config.String()})
		must(e)
		_, e = es.PublishEvaluatorDefinitionVersion(ctx, s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: r, Body: def, Source: source})
		must(e)
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: r, Metrics: refs, Required: spec.Required, Applicability: spec.Applicability})
	}
	k.Capabilities, e = k.LoadEvaluatorCapabilities(ctx, agentquality.DeterministicSupported)
	must(e)
	transport := provider.DefaultLocalAgentConfig(os.Getenv("STAGE13_WP09_LOCALAGENT_BASE_URL"), "STAGE13_WP09_LOCALAGENT_SERVICE_TOKEN")
	transport.CallMilliseconds = 180000
	configs := []provider.Stage13Config{{Transport: transport, ExpectedSubjectManifest: plan.Baseline.Manifest, ExecutionPolicyVersion: provider.Stage13ExecutionPolicyVersion}, {Transport: transport, ExpectedSubjectManifest: plan.Candidate.Manifest, ExecutionPolicyVersion: provider.Stage13ExecutionPolicyVersion}}
	var policy decision.Policy
	must(freeze(oldPolicy.Policy).Decode(&policy))
	for i := range policy.Metrics {
		policy.Metrics[i].Metric = mapping[policy.Metrics[i].Metric]
	}
	if len(policy.AcceptedDifferences) != 1 || policy.AcceptedDifferences[0].Dimension != "target_config" {
		panic("FROZEN_POLICY_UNEXPECTED")
	}
	policy.AcceptedDifferences[0].Baseline = targetProofDigest(configs[0])
	policy.AcceptedDifferences[0].Candidate = targetProofDigest(configs[1])
	policy.AcceptedDifferences[0].Proof = "stage13.model-comparability.v2:" + freeze(pre).Digest()
	policy.AcceptedDifferences[0].Actor = s.Principal
	decisions := postgres.Decisions{Pool: pool}
	pr := asset.Ref{EntityID: gov.ID("wp09-policy", holdoutProject, candidateDigest), Version: "v1"}
	_, e = decisions.CreatePolicy(ctx, s.Scope, asset.Create{ID: pr.EntityID, Name: "WP09 frozen policy binding"})
	must(e)
	pv, e := decisions.PublishPolicyVersion(ctx, s.Scope, asset.Publish[decision.Policy]{Ref: pr, Body: policy, Source: source})
	must(e)
	save(out, "policy-binding", map[string]any{"policy_ref": pr, "policy_digest": pv.ContentDigest(), "policy": policy, "source_policy": oldPolicy.Policy, "rules_changed": false, "binding_changes_only": []string{"project-local metric references", "actual frozen target_config proof"}})
	snapshot, e := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, s.Scope, ev.BuildRunSnapshot{Dataset: &frozen.Dataset.Ref, Evaluators: bindings})
	must(e)
	ids := []string{}
	states := []ev.RunState{}
	commands := []ev.CreateRun{}
	router := targets{}
	for _, cfg := range configs {
		target, e := provider.NewStage13Target(cfg)
		must(e)
		target.ReserveExecution = k.ReserveStage13Execution
		router[freeze(cfg).Digest()] = target
		defer target.Close()
		var m map[string]asset.JSON
		must(cfg.ExpectedSubjectManifest.Decode(&m))
		cmd := ev.CreateRun{Intent: "RELEASE_EVALUATION", CommandID: asset.NewID(), Snapshot: snapshot, Target: ev.Target{ID: provider.Stage13TargetID, Kind: "LOCALAGENT_HTTP", Version: provider.Stage13TargetVersion, Config: freeze(cfg), TimeoutMilliseconds: 180000}, Subject: freeze(map[string]any{"subject": "ci-failure-triage", "subject_version": m["subject_version"], "environment": "controlled-local", "run_mode": "AGENT_REGRESSION", "dataset_usage": "HOLDOUT"}), Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}
		r, e := k.CreateRun(ctx, s, cmd)
		must(e)
		if r.Code != ev.Applied && r.Code != ev.AlreadyApplied {
			panic(r)
		}
		ids = append(ids, r.ID)
		commands = append(commands, cmd)
		st, e := k.ReadRunState(ctx, s.Scope, r.ID)
		must(e)
		states = append(states, st)
	}
	save(out, "main-run-ids", ids)
	saveConsumption(ctx, out, pool, ids[1], "", "")
	order := []string{}
	for _, c := range snapshot.Input().Manifest {
		for _, st := range states {
			for _, a := range st.Attempts {
				if a.CaseID == c.Identity.Ref.EntityID {
					order = append(order, a.ID)
				}
			}
		}
	}
	save(out, "deterministic-schedule", order)
	backend := &pairedBackend{Evaluation: k, order: order}
	wc := worker.DefaultConfig()
	wc.WorkerID = "system:wp09-release"
	wc.ExecutionConcurrency = 1
	wc.EvaluatorConcurrency = 1
	wc.OnlineConcurrency = 0
	wc.ScanBatch = 1
	wc.PollInterval = 100 * time.Millisecond
	runtime := worker.Runtime{Config: wc, Backend: backend, Target: router, Evaluator: agentquality.DeterministicEvaluator{}, Epoch: epoch, Log: slog.Default()}
	runctx, stop := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- runtime.Run(runctx) }()
	last := -1
	for {
		terminal := true
		n := 0
		states = nil
		for _, id := range ids {
			st, e := k.ReadRunState(ctx, s.Scope, id)
			must(e)
			states = append(states, st)
			terminal = terminal && st.Run.Status.Terminal()
			for _, a := range st.Attempts {
				if a.Status == "TERMINAL" {
					n++
				}
			}
		}
		if n != last {
			fmt.Printf("RELEASE attempts=%d/12\n", n)
			last = n
		}
		if terminal {
			break
		}
		select {
		case <-ctx.Done():
			stop()
			must(<-done)
			panic("RUN_OVERALL_DEADLINE_NOT_MODEL_FAILURE")
		case <-time.After(2 * time.Second):
		}
	}
	stop()
	must(<-done)
	save(out, "scheduler", map[string]any{"order": order, "claims": backend.claims, "execution_concurrency": 1, "evaluator_concurrency": 1})
	for i, st := range states {
		name := []string{"baseline", "candidate"}[i]
		save(out, name+"-run-state", st)
		save(out, name+"-results", st.Results)
	}
	finish(ctx, out, decisions, s, plan, states, ids, pr)
	var gate decision.Receipt
	load(filepath.Join(out, "release-gate.json"), &gate)
	saveConsumption(ctx, out, pool, ids[1], gate.GateID, string(gate.Decision))
	replays := []ev.Reply{}
	for _, cmd := range commands {
		cmd.CommandID = asset.NewID()
		r, e := k.CreateRun(ctx, s, cmd)
		must(e)
		if r.Code != ev.AlreadyApplied {
			panic("RELEASE_DUPLICATE_CREATED")
		}
		replays = append(replays, r)
	}
	save(out, "consumption-idempotency", map[string]any{"PASS": true, "replies": replays, "new_attempts": 0, "new_model_calls": 0})
	projectAuthorization(ctx, out, decisions, s, gate, states[1])
}

func projectAuthorization(ctx context.Context, out string, decisions postgres.Decisions, s ev.Scope, gate decision.Receipt, candidate ev.RunState) {
	authorizations := []delivery.Authorization{}
	for _, a := range candidate.Attempts {
		g, o, results, e := decisions.Stage13DeliveryBinding(ctx, s.Scope, gate.GateID, a.ID)
		must(e)
		q := delivery.Request{Gate: g, Outcome: o, Destination: delivery.Destination{Kind: delivery.SinkKind, Scope: "TEST_SCOPE", ID: "wp09-eligibility-only"}}
		auth, e := decisions.AuthorizeStage13Delivery(ctx, s.Scope, q)
		must(e)
		if len(results) != 7 {
			panic("AUTHORIZATION_RESULT_BINDING")
		}
		authorizations = append(authorizations, auth)
	}
	save(out, "delivery-authorization-projection", map[string]any{"read_only": true, "new_delivery_payloads": 0, "enterprise_side_effects": 0, "authorizations": authorizations})
}

func saveConsumption(ctx context.Context, out string, pool *pgxpool.Pool, run, gate, decision string) {
	var created time.Time
	var metadata []byte
	must(pool.QueryRow(ctx, "SELECT created_at,metadata FROM evaluation_runs WHERE id=$1", run).Scan(&created, &metadata))
	save(out, "holdout-consumption", map[string]any{"version": "stage13.holdout-consumption.v1", "state": "CONSUMED", "holdout_dataset_version": asset.Ref{EntityID: "6fb67037-6512-562a-8227-e842851e6f0f", Version: "golden-v1"}, "candidate_subject_manifest_digest": candidateDigest, "release_evaluation_run": run, "consumed_at": created, "consumption_reason": "FIRST_FORMAL_RELEASE_EVALUATION_CREATED", "gate_id": gate, "gate_decision": decision, "durable_sources": []string{"evaluation_runs immutable snapshot/metadata + created_at", "evaluation_comparisons candidate run binding", "evaluation_gate_receipts immutable decision"}, "run_metadata": json.RawMessage(metadata)})
}
