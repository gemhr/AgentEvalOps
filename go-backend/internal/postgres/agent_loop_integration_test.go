//go:build integration

package postgres_test

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
)

func TestAgentLoopPythonEvidenceDurableEvaluation(t *testing.T) {
	f := fixture(t)
	ctx := context.Background()
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: f.k.Pool}, Datasets: postgres.Datasets{Pool: f.k.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: f.k.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: f.k.Pool}}
	original := f.cmd.Snapshot.Input()
	source := asset.Source{Kind: "TEST", Ref: "public-loop-contract.v1", Principal: f.s.Principal}
	metricRef := original.Metrics[0].Identity.Ref
	metricRef.Version = "loop-v1"
	var definition metric.Definition
	for _, b := range metric.Builtins() {
		if b.Key == "loop_integrity" {
			definition = b.Definition
		}
	}
	_, e := (metric.MetricDefinitionService{Store: readers.MetricDefinitions}).PublishMetricDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.Definition]{Ref: metricRef, Body: definition, Source: source})
	mustG7(t, e)
	evaluatorRef := original.Evaluators[0].Identity.Ref
	evaluatorRef.Version = "loop-v1"
	evaluator := agentquality.LoopEvaluatorDefinition(metricRef)
	_, e = (metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}).PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: evaluatorRef, Body: evaluator, Source: source})
	mustG7(t, e)
	datasetRef := original.Dataset.Ref
	f.cmd.Snapshot, e = (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &datasetRef, Evaluators: []catalog.EvaluatorBinding{{Evaluator: evaluatorRef, Metrics: []asset.Ref{metricRef}, Required: true, Applicability: evaluator.Applicability}}})
	mustG7(t, e)
	f.k.Capabilities = ev.EvaluatorExecutionCapability{}
	for _, spec := range f.cmd.Snapshot.Input().Evaluators {
		f.k.Capabilities.Bindings = append(f.k.Capabilities.Bindings, ev.Capability{Evaluator: spec.Identity.Ref, ImplementationRef: spec.Definition.ImplementationRef, DefinitionDigest: spec.Identity.ContentDigest})
	}
	root, e := filepath.Abs(filepath.Join("..", "..", "..", "..", "Local_Agent"))
	mustG7(t, e)
	mode := "complete"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var input struct {
			RunID     string `json:"run_id"`
			EvalRunID string `json:"evaluation_run_id"`
			RequestID string `json:"request_id"`
		}
		// 测试服务只转送公开合成 Python producer 响应，不声称是真实 AgentCore HTTP。
		_ = json.NewDecoder(r.Body).Decode(&input)
		command := exec.Command("uv", "run", "python", "tests/fixtures/loop_evidence_public.py", input.RunID, input.EvalRunID, input.RequestID)
		command.Dir = root
		raw, err := command.Output()
		if err != nil {
			t.Error(err)
			w.WriteHeader(500)
			return
		}
		var wire map[string]any
		_ = json.Unmarshal(raw, &wire)
		loop := wire["loop_evidence"].(map[string]any)
		if mode == "missing-observation" {
			loop["iterations"].([]any)[0].(map[string]any)["action"].(map[string]any)["observation_digest"] = nil
		}
		if mode == "failed-budget" {
			wire["status"] = "FAILED"
			wire["stop_reason"] = "BUDGET_EXHAUSTED"
			wire["error_code"] = "LOOP_ITERATION_LIMIT"
			wire["final_answer_capture_status"] = "FAILED"
			wire["final_answer_capture_error_code"] = "FINAL_ANSWER_RUNTIME_NOT_SUCCEEDED"
			wire["final_answer_evidence"] = nil
			loop["root"].(map[string]any)["status"] = "FAILED"
			loop["root"].(map[string]any)["stop_reason"] = "BUDGET_EXHAUSTED"
		}
		body, _ := asset.Freeze(loop)
		wire["loop_evidence_digest"] = body.Digest()
		encoded, _ := json.Marshal(wire)
		_, _ = w.Write(encoded)
	}))
	defer server.Close()
	config := provider.AgentLoopConfig{BaseURL: server.URL, TokenEnv: "PUBLIC_LOOP_TEST_TOKEN", TrustedProjectID: f.s.ProjectID, MaxResponseBytes: 1024 * 1024, CallMilliseconds: 3000, HTTP: provider.DefaultHTTPConfig()}
	t.Setenv(config.TokenEnv, "public-test")
	target, e := provider.NewAgentLoopTarget(config, slog.New(slog.NewTextHandler(io.Discard, nil)))
	mustG7(t, e)
	defer target.Close()
	f.cmd.Target = ev.Target{ID: provider.AgentLoopTargetID, Version: provider.AgentLoopTargetVersion, Kind: "LOCALAGENT_HTTP", TimeoutMilliseconds: 3000}
	f.cmd.Target.Config, _ = asset.Freeze(config)
	f.cmd.Subject, _ = asset.Freeze(map[string]any{"agent": "public-loop", "revision": "v1", "environment": "TEST", "run_mode": "COORDINATED"})
	// 当前 Case 输入通过版本化目录替换，不篡改已冻结 Attempt。
	caseRef := original.Manifest[0].Identity.Ref
	caseRef.Version = "loop-v1"
	caseBody := testCase()
	caseBody.Input, _ = asset.Freeze(map[string]any{"agent_id": "public-loop", "query": "public query"})
	_, e = (catalog.CaseService{Store: readers.Cases}).PublishCaseVersion(ctx, f.s.Scope, asset.Publish[catalog.CaseContent]{Ref: caseRef, Body: caseBody, Source: source})
	mustG7(t, e)
	datasetRef.Version = "loop-v1"
	_, e = (catalog.DatasetService{Store: readers.Datasets, Cases: readers}).PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: datasetRef, Body: catalog.DatasetContent{Cases: []asset.Ref{caseRef}}, Source: source})
	mustG7(t, e)
	f.cmd.Snapshot, e = (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &datasetRef, Evaluators: []catalog.EvaluatorBinding{{Evaluator: evaluatorRef, Metrics: []asset.Ref{metricRef}, Required: true, Applicability: evaluator.Applicability}}})
	mustG7(t, e)
	one := 1.0
	policy := decision.Policy{Offline: true, Coverage: decision.CoveragePolicy{MinimumDecision: 1, MinimumEvaluation: 1}, Metrics: []decision.MetricRule{{Metric: metricRef, Aggregation: "mean", Minimum: &one, MinimumCoverage: 1, Required: true}}}
	var baseline decision.Source
	for _, scenario := range []string{"complete", "missing-observation", "failed-budget"} {
		mode = scenario
		o := start(t, f, create(t, f), time.Minute)
		state, err := f.k.ReadRunState(ctx, f.s.Scope, o.RunID)
		mustG7(t, err)
		q := state.Attempts[0].Request
		actual, err := target.Execute(ctx, f.s, q)
		mustG7(t, err)
		want := ev.Success
		if scenario == "failed-budget" {
			want = ev.Failure
		}
		if actual.Kind != want || len(actual.Evidence) == 0 {
			t.Fatalf("%s outcome %s evidence %d", scenario, actual.Kind, len(actual.Evidence))
		}
		r, err := f.k.FinalizeExecutionOutcome(ctx, f.s, ev.FinalizeOutcome{Owned: o, CommandID: asset.NewID(), Outcome: actual})
		reply(t, r, err, ev.Applied)
		state, err = f.k.ReadRunState(ctx, f.s.Scope, o.RunID)
		mustG7(t, err)
		if state.Attempts[0].Metadata.Observation.Evidence[0].Body.Digest() != actual.Evidence[0].Body.Digest() {
			t.Fatal("durable Evidence changed")
		}
		if scenario != "failed-budget" {
			if len(state.Works) != 1 {
				t.Fatal("required Loop Work missing")
			}
			work := state.Works[0]
			owner := ev.Owned{RunID: o.RunID, AttemptID: o.AttemptID, WorkID: work.ID}
			r, err = f.k.ClaimEvaluatorWork(ctx, f.s, owner, "loop-evaluator", time.Minute)
			reply(t, r, err, ev.Applied)
			owner.Token = r.Token
			evaluated, err := (agentquality.DeterministicEvaluator{}).Evaluate(ctx, worker.EvaluationInput{Run: state.Run, Attempt: state.Attempts[0], Work: work, Scope: f.s, Owned: owner, Persistence: f.k})
			mustG7(t, err)
			if (scenario == "complete" && evaluated.Value.Verdict != "PASS") || (scenario != "complete" && evaluated.Value.Verdict == "PASS") {
				t.Fatalf("%s verdict %+v", scenario, evaluated.Value)
			}
			r, err = f.k.FinalizeEvaluationResult(ctx, f.s, ev.FinalizeResult{Owned: owner, CommandID: asset.NewID(), ResultID: asset.NewID(), Value: evaluated.Value})
			reply(t, r, err, ev.Applied)
		} else if actual.Artifact != nil {
			t.Fatal("failed execution received artifact")
		}
		r, err = f.k.CoordinateRun(ctx, f.s, o.RunID)
		reply(t, r, err, ev.Applied)
		state, err = f.k.ReadRunState(ctx, f.s.Scope, o.RunID)
		mustG7(t, err)
		source, err := decision.Offline(decision.SourceRef{Kind: "RUN_SET", Runs: []string{o.RunID}}, []ev.RunState{state})
		mustG7(t, err)
		if scenario == "complete" {
			baseline = source
		}
		receipt, err := decision.Compare(decision.Snapshot{ProjectID: f.s.ProjectID, Baseline: baseline, Candidate: source, Policy: policy, CapturedAt: time.Now().UTC()})
		mustG7(t, err)
		if scenario != "complete" && receipt.Decision == decision.Pass {
			t.Fatal("required missing/failed Loop gate PASS")
		}
		if scenario == "complete" && receipt.Decision != decision.Pass {
			t.Fatalf("complete Loop gate %s: %v", receipt.Decision, receipt.Reasons)
		}
		t.Logf("scenario=%s outcome=%s results=%d gate=%s", scenario, actual.Kind, len(state.Results), receipt.Decision)
		if scenario == "complete" && len(state.Results) != 1 {
			t.Fatal("Loop result not persisted")
		}
	}
}
