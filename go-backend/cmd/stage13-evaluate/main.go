// stage13-evaluate 是显式隔离的 WP05 受控执行入口；全部事实经过现有 owners。
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
	"agentevalops/go-backend/internal/citriage"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"github.com/jackc/pgx/v5/pgxpool"
)

func must(err error) {
	if err != nil {
		panic(err)
	}
}
func freeze(v any) asset.JSON { j, e := asset.Freeze(v); must(e); return j }
func save(dir, name string, v any) {
	must(os.WriteFile(filepath.Join(dir, name+".json"), freeze(v).Bytes(), 0600))
}
func load(path string, v any) { b, e := os.ReadFile(path); must(e); must(json.Unmarshal(b, v)) }

type exportedCase struct {
	ID             string     `json:"case_id"`
	Input          asset.JSON `json:"input"`
	GT             asset.JSON `json:"ground_truth"`
	Incident       string     `json:"source_incident_id"`
	SourceInput    string     `json:"source_input_digest"`
	SourceRevision int        `json:"source_evidence_revision"`
	SourceManifest string     `json:"source_evidence_manifest_digest"`
	Detail         string     `json:"supplemental_detail_digest"`
}
type export struct {
	Dataset  string         `json:"dataset_id"`
	Version  string         `json:"dataset_version"`
	Cases    []exportedCase `json:"cases"`
	Profile  asset.JSON     `json:"source_profile"`
	Manifest string         `json:"source_manifest_digest"`
	GTExport asset.JSON     `json:"gt_export"`
}
type targets map[string]*provider.Stage13Target

func (t targets) Execute(c context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	if v := t[q.Target.Config.Digest()]; v != nil {
		return v.Execute(c, s, q)
	}
	return ev.Outcome{}, asset.ErrInvalid
}

func main() {
	if (len(os.Args) != 2 && len(os.Args) != 3) || os.Getenv("STAGE13_WP05_CONTROLLED") != "1" {
		panic("EXPLICIT_CONTROLLED_SCOPE_REQUIRED")
	}
	out, e := filepath.Abs(os.Args[1])
	must(e)
	url := os.Getenv("STAGE13_WP05_DATABASE_URL")
	cfg, e := pgxpool.ParseConfig(url)
	must(e)
	if cfg.ConnConfig.Host != "127.0.0.1" || cfg.ConnConfig.Port != 55432 || cfg.ConnConfig.Database != "stage13_wp05_evalops_test" {
		panic("ISOLATED_TEST_DATABASE_REQUIRED")
	}
	cfg.MaxConns = 32
	cfg.ConnConfig.RuntimeParams["role"] = "stage13_wp05_runtime"
	ctx := context.Background()
	pool, e := pgxpool.NewWithConfig(ctx, cfg)
	must(e)
	defer pool.Close()
	must(pool.Ping(ctx))
	k := postgres.Evaluation{Pool: pool}
	epoch, e := k.VerifyWorker(ctx, "c12a00800001")
	must(e)
	var doc export
	load(filepath.Join(out, "case-export.json"), &doc)
	var plan provider.Stage13PolicyDocument
	load(filepath.Join(out, "frozen-subject-plan.json"), &plan)
	if len(os.Args) == 3 && os.Args[2] == "--resume" {
		resume(ctx, out, pool, k, epoch, plan)
		return
	}
	preflight := provider.CompareStage13Models(plan)
	save(out, "comparability-preflight", preflight)
	if !preflight.Comparable {
		panic("COMPARABILITY_BLOCKED")
	}
	scope := asset.Scope{ProjectID: "71300005-0000-4000-8000-000000000001", OrganizationID: "71300005-0000-4000-8000-000000000002", Principal: "system:stage13-wp05", CanPublish: true}
	s := ev.Scope{Scope: scope, Epoch: epoch, Create: true, Execute: true, Evaluate: true, Coordinate: true, Repair: true}
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: pool}, Datasets: postgres.Datasets{Pool: pool}, Suites: postgres.Suites{Pool: pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: pool}}
	cases := catalog.CaseService{Store: readers.Cases}
	datasets := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	suites := catalog.SuiteService{Store: readers.Suites, References: readers}
	mdsvc := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
	edsvc := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	source := asset.Source{Kind: "SYNTHETIC_HIDDEN_GT", Ref: "stage13.wp05.offline-export.v1", Principal: scope.Principal, Metadata: freeze(map[string]any{"source_profile": doc.Profile, "source_manifest_digest": doc.Manifest, "gt_export": doc.GTExport})}
	ref := func() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "v1"} }
	app := asset.Applicability{RuleRef: "stage13.triage-applicability.v1"}
	bindings := []catalog.EvaluatorBinding{}
	metricRefs := []asset.Ref{}
	rules := []decision.MetricRule{}
	zero := 0.0
	for _, name := range citriage.Metrics {
		m, ed := ref(), ref()
		d := metric.Definition{Name: name, Grain: metric.Case, ValueType: metric.Scalar, Range: &[2]float64{0, 1}, Unit: "ratio", Direction: metric.Higher, Denominator: "all planned applicable incident episodes", Missing: "invalid output=0; undecidable separately blocks gate; missing execution keeps planned slot", Aggregation: "pessimistic planned-slot macro mean", Comparison: "same frozen dataset/case/evidence/GT/evaluator and model-comparability.v2", Applicability: app, Availability: asset.ContractOnly}
		_, e = mdsvc.CreateMetricDefinition(ctx, scope, asset.Create{ID: m.EntityID, Name: name})
		must(e)
		_, e = mdsvc.PublishMetricDefinitionVersion(ctx, scope, asset.Publish[metric.Definition]{Ref: m, Body: d, Source: source})
		must(e)
		definition := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: "stage13.triage-input.v1", OutputMetrics: []asset.Ref{m}, Applicability: app, Availability: asset.ContractOnly, ImplementationRef: citriage.Implementation, Config: freeze(agentquality.Config{Metric: name, K: 3}), SchemaVersion: "stage13.triage-output.v1", Normalization: "stage13.cause-descriptor-exact.v1", Budget: metric.Budget{TotalMilliseconds: 10000, MaxResponseBytes: 131072}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 4}}
		_, e = edsvc.CreateEvaluatorDefinition(ctx, scope, asset.Create{ID: ed.EntityID, Name: name})
		must(e)
		_, e = edsvc.PublishEvaluatorDefinitionVersion(ctx, scope, asset.Publish[metric.EvaluatorDefinition]{Ref: ed, Body: definition, Source: source})
		must(e)
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: ed, Metrics: []asset.Ref{m}, Required: true, Applicability: app})
		metricRefs = append(metricRefs, m)
		rules = append(rules, decision.MetricRule{Metric: m, Aggregation: "mean", MinimumCoverage: 1, Required: true, PerCase: true, AbsoluteTolerance: &zero, RelativeTolerance: &zero})
	}
	transport := provider.DefaultLocalAgentConfig("http://127.0.0.1:55438", "LOCAL_AGENT_STAGE13_SERVICE_TOKEN")
	transport.CallMilliseconds = 180000
	configs := []provider.Stage13Config{{Transport: transport, ExpectedSubjectManifest: plan.Baseline.Manifest}, {Transport: transport, ExpectedSubjectManifest: plan.Candidate.Manifest}}
	decisionStore := postgres.Decisions{Pool: pool}
	policyRef := ref()
	policy := decision.Policy{Offline: true, Criticalities: []string{"CRITICAL"}, CriticalMissing: true, BlockPerCase: true, Coverage: decision.CoveragePolicy{MinimumDecision: 1, MinimumEvaluation: 1}, Metrics: rules, AcceptedDifferences: []decision.AcceptedDifference{{Dimension: "target_config", Baseline: freeze(configs[0]).Digest(), Candidate: freeze(configs[1]).Digest(), Proof: "stage13.model-comparability.v2:" + freeze(preflight).Digest(), Actor: scope.Principal, Reason: "同模型/input/schema/工具能力，冻结不同 Agent/Prompt；每 Attempt 正式 Target 独立核验实际回执"}}}
	_, e = decisionStore.CreatePolicy(ctx, scope, asset.Create{ID: policyRef.EntityID, Name: "Stage13 controlled release policy"})
	must(e)
	_, e = decisionStore.PublishPolicyVersion(ctx, scope, asset.Publish[decision.Policy]{Ref: policyRef, Body: policy, Source: source})
	must(e)
	save(out, "frozen-policy", map[string]any{"policy": policy, "policy_ref": policyRef, "thresholds": map[string]float64{"accuracy": 0.95, "root_metrics": 0.80}, "invalid_output": "zero score + undecidable; no exclusion", "evaluation_mode": "AGENT_REGRESSION"})
	// deadline 只在首次 Dataset 发布前冻结；模型调用和证据不刷新它。
	deadline := time.Now().UTC().Add(170 * time.Second).Format(time.RFC3339Nano)
	refs := []asset.Ref{}
	published := []catalog.CaseVersion{}
	for _, c := range doc.Cases {
		var input map[string]asset.JSON
		must(c.Input.Decode(&input))
		var ep map[string]asset.JSON
		must(input["evidence_policy"].Decode(&ep))
		ep["deadline_at"] = freeze(deadline)
		input["evidence_policy"] = freeze(ep)
		var gt citriage.GroundTruth
		must(c.GT.Decode(&gt))
		must(gt.Validate())
		r := asset.Ref{EntityID: c.ID, Version: "v1"}
		body := catalog.CaseContent{Input: freeze(input), GroundTruth: c.GT, TaskGoal: "分析有界 CI Incident Episode", AcceptanceCriteria: []string{"只依据授权可见证据输出冻结七字段结构"}, Type: catalog.AgentTask, Capability: "CI_FAILURE_TRIAGE", Criticality: catalog.Criticality(gt.Criticality), BodyPolicy: catalog.Retained, Applicability: app, Metadata: freeze(map[string]any{"source_incident_id": c.Incident, "source_evidence_revision": c.SourceRevision, "source_evidence_manifest_digest": c.SourceManifest, "source_input_digest": c.SourceInput, "supplemental_detail_digest": c.Detail, "offline_input_digest": freeze(input).Digest(), "GT_digest": c.GT.Digest(), "evaluator_version": citriage.Implementation})}
		_, e = cases.CreateCase(ctx, scope, asset.Create{ID: c.ID, Name: "Stage13 episode " + c.Incident})
		must(e)
		v, err := cases.PublishCaseVersion(ctx, scope, asset.Publish[catalog.CaseContent]{Ref: r, Body: body, Source: source})
		must(err)
		published = append(published, v)
		refs = append(refs, r)
	}
	ds := asset.Ref{EntityID: doc.Dataset, Version: doc.Version}
	_, e = datasets.CreateDataset(ctx, scope, asset.Create{ID: ds.EntityID, Name: "Stage13 CI Failure Triage Dataset"})
	must(e)
	dv, e := datasets.PublishDatasetVersion(ctx, scope, asset.Publish[catalog.DatasetContent]{Ref: ds, Body: catalog.DatasetContent{Cases: refs, Metadata: freeze(map[string]any{"source_profile": doc.Profile, "source_manifest_digest": doc.Manifest, "GT_export": doc.GTExport, "case_count": len(refs), "frozen_deadline": deadline})}, Source: source})
	must(e)
	save(out, "dataset-manifest", map[string]any{"dataset": dv, "manifest_digest": dv.ContentDigest(), "case_count": len(refs), "cases": published, "source_profile": doc.Profile, "GT_export": doc.GTExport})
	suite := ref()
	_, e = suites.CreateSuite(ctx, scope, asset.Create{ID: suite.EntityID, Name: "Stage13 CI Failure Triage Suite"})
	must(e)
	pv, err := decisionStore.GetPolicyVersion(ctx, scope, policyRef)
	must(err)
	_, e = suites.PublishSuiteVersion(ctx, scope, asset.Publish[catalog.SuiteContent]{Ref: suite, Body: catalog.SuiteContent{Dataset: &ds, Evaluators: bindings, Metrics: metricRefs, Policy: &asset.PolicyRef{PolicyID: policyRef.EntityID, Version: policyRef.Version, Digest: pv.ContentDigest(), Algorithm: pv.Algorithm()}, Applicability: app}, Source: source})
	must(e)
	k.Capabilities, e = k.LoadEvaluatorCapabilities(ctx, agentquality.DeterministicSupported)
	must(e)
	router := targets{}
	for _, cfg := range configs {
		target, err := provider.NewStage13Target(cfg)
		must(err)
		router[freeze(cfg).Digest()] = target
		defer target.Close()
	}
	wc := worker.DefaultConfig()
	wc.WorkerID = "system:wp05-worker"
	wc.ExecutionConcurrency = 12
	wc.EvaluatorConcurrency = 8
	rt := worker.Runtime{Config: wc, Backend: k, Target: router, Evaluator: agentquality.DeterministicEvaluator{}, Epoch: epoch, Log: slog.Default()}
	runctx, cancel := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- rt.Run(runctx) }()
	defer func() { cancel(); must(<-done) }()
	create := func(selected []asset.Ref) []string {
		ids := []string{}
		for i, cfg := range configs {
			snapshot, err := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, scope, ev.BuildRunSnapshot{Suite: &suite, SelectedCases: selected})
			must(err)
			var manifest map[string]asset.JSON
			_ = cfg.ExpectedSubjectManifest.Decode(&manifest)
			subject := freeze(map[string]any{"subject": "ci-failure-triage", "subject_version": manifest["subject_version"], "environment": "controlled-local", "run_mode": "AGENT_REGRESSION"})
			reply, err := k.CreateRun(ctx, s, ev.CreateRun{CommandID: asset.NewID(), Snapshot: snapshot, Target: ev.Target{ID: provider.Stage13TargetID, Kind: "LOCALAGENT_HTTP", Version: provider.Stage13TargetVersion, Config: freeze(cfg), TimeoutMilliseconds: 180000}, Subject: subject, Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}})
			must(err)
			if reply.Code != ev.Applied {
				panic(reply)
			}
			ids = append(ids, reply.ID)
			fmt.Printf("run created side=%d run=%s cases=%d\n", i, reply.ID, len(snapshot.Input().Manifest))
		}
		return ids
	}
	wait := func(ids []string) []ev.RunState {
		limit := time.Now().Add(4 * time.Minute)
		last := -1
		for time.Now().Before(limit) {
			states := []ev.RunState{}
			terminal := true
			count := 0
			for _, id := range ids {
				st, err := k.ReadRunState(ctx, scope, id)
				must(err)
				states = append(states, st)
				terminal = terminal && st.Run.Status.Terminal()
				for _, a := range st.Attempts {
					if a.Status == "TERMINAL" {
						count++
					}
				}
			}
			if count != last {
				fmt.Printf("attempts terminal=%d\n", count)
				last = count
			}
			if terminal {
				return states
			}
			time.Sleep(2 * time.Second)
		}
		panic("RUN_TERMINAL_TIMEOUT")
	}
	cohort := []asset.Ref{}
	categories := map[string]bool{}
	for _, c := range published {
		var gt citriage.GroundTruth
		_ = c.Content().Body.GroundTruth.Decode(&gt)
		if citriage.Has(gt.Category, []string{"ENVIRONMENT", "TOOL_CHAIN", "PRODUCT"}) && !categories[gt.Category] {
			categories[gt.Category] = true
			cohort = append(cohort, c.Ref())
		}
	}
	small := create(cohort)
	save(out, "small-cohort-run-ids", small)
	smallStates := wait(small)
	save(out, "small-cohort", smallStates)
	for _, st := range smallStates {
		for _, a := range st.Attempts {
			if a.Outcome != ev.Success {
				panic("SMALL_COHORT_EXECUTION_BLOCKED")
			}
		}
		if len(st.Results) != len(cohort)*7 {
			panic("SMALL_COHORT_RESULT_MISSING")
		}
	}
	fmt.Println("small cohort pipeline PASS; starting full frozen cohort")
	ids := create(nil)
	save(out, "main-run-ids", ids)
	states := wait(ids)
	for i, st := range states {
		name := []string{"baseline", "candidate"}[i]
		save(out, name+"-run-state", st)
		save(out, name+"-results", st.Results)
	}
	finish(ctx, out, decisionStore, s, plan, states, ids, policyRef)
}

func finish(ctx context.Context, out string, decisionStore postgres.Decisions, s ev.Scope, plan provider.Stage13PolicyDocument, states []ev.RunState, ids []string, policyRef asset.Ref) {
	replayed := 0
	for _, state := range states {
		for _, result := range state.Results {
			if result.WorkID == nil {
				panic("RESULT_WORK_MISSING")
			}
			var attempt ev.Attempt
			var work ev.Work
			for _, a := range state.Attempts {
				if a.ID == result.AttemptID {
					attempt = a
				}
			}
			for _, w := range state.Works {
				if w.ID == *result.WorkID {
					work = w
				}
			}
			value, err := (agentquality.DeterministicEvaluator{}).Evaluate(ctx, worker.EvaluationInput{Scope: s, Run: state.Run, Attempt: attempt, Work: work})
			must(err)
			if freeze(value.Value).Digest() != freeze(result.Value).Digest() {
				panic("EVALUATOR_REPLAY_CHANGED")
			}
			replayed++
		}
	}
	save(out, "evaluator-replay", map[string]any{"PASS": true, "replayed_results": replayed, "new_model_calls": 0, "new_results_written": 0})
	// 实际逐 Case 跨侧模型政策验证，包含 tool capability 原始载荷。
	pairResults := []provider.Stage13PairDecision{}
	pairs := map[string]ev.Attempt{}
	for _, a := range states[0].Attempts {
		pairs[a.CaseID] = a
	}
	side := func(a ev.Attempt, tool asset.JSON) provider.Stage13PolicyInput {
		for _, e := range a.Metadata.Observation.Evidence {
			if e.Schema != "stage13-subject-receipt.v1" {
				continue
			}
			var wire map[string]asset.JSON
			_ = e.Body.Decode(&wire)
			receipt := wire["actual_subject_receipt"]
			var selected string
			_ = wire["selected_final_run_id"].Decode(&selected)
			if selected != a.ID {
				var children []asset.JSON
				_ = wire["child_subject_receipts"].Decode(&children)
				if len(children) == 1 {
					receipt = children[0]
				}
			}
			var r map[string]asset.JSON
			_ = receipt.Decode(&r)
			return provider.Stage13PolicyInput{Manifest: r["actual_subject_manifest"], Receipt: receipt, ToolIdentity: tool}
		}
		return provider.Stage13PolicyInput{}
	}
	for _, a := range states[1].Attempts {
		p := provider.CompareStage13Models(provider.Stage13PolicyDocument{Mode: "AGENT_REGRESSION", Baseline: side(pairs[a.CaseID], plan.Baseline.ToolIdentity), Candidate: side(a, plan.Candidate.ToolIdentity)})
		pairResults = append(pairResults, p)
	}
	save(out, "comparability", pairResults)
	cmd := decision.Command{ID: asset.NewID(), Baseline: decision.SourceRef{Kind: "RUN_SET", Runs: []string{ids[0]}}, Candidate: decision.SourceRef{Kind: "RUN_SET", Runs: []string{ids[1]}}, Policy: policyRef}
	if _, err := os.Stat(filepath.Join(out, "gate-command.json")); err == nil {
		load(filepath.Join(out, "gate-command.json"), &cmd)
	}
	save(out, "gate-command", cmd)
	gate, err := decisionStore.CreateGate(ctx, s, cmd)
	must(err)
	save(out, "release-gate", gate)
	save(out, "aggregate-metrics", gate.Stage13)
	save(out, "per-case-regression", gate.Cases)
	before := freeze(gate).Digest()
	replay, err := decisionStore.CreateGate(ctx, s, cmd)
	must(err)
	if freeze(replay).Digest() != before {
		panic("GATE_REPLAY_CHANGED")
	}
	save(out, "gate-idempotency", map[string]any{"PASS": true, "gate_id": gate.GateID, "gate_digest": before, "new_model_calls": 0})
	snapshot, err := decisionStore.GetComparison(ctx, s.Scope, cmd.ID)
	must(err)
	computed, err := decision.Compare(snapshot)
	must(err)
	if freeze(computed).Digest() != before {
		panic("GATE_FROZEN_REPLAY_CHANGED")
	}
	save(out, "gate-frozen-replay", map[string]any{"PASS": true, "gate_digest": before, "snapshot_digest": gate.SnapshotDigest, "new_model_calls": 0, "new_results_written": 0})

	fmt.Printf("controlled evaluation complete; main attempts=%d+%d; candidate release gate=%s\n", len(states[0].Attempts), len(states[1].Attempts), gate.Decision)
}
