//go:build integration

package postgres_test

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
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
	"github.com/jackc/pgx/v5"
)

func TestAdaptiveDiagnosisWorkerHTTPPostgresGate(t *testing.T) {
	f := fixture(t)
	ctx := context.Background()
	root, e := filepath.Abs("../../../../Local_Agent")
	mustG7(t, e)
	python := filepath.Join(root, ".venv", "bin", "python")
	if runtime.GOOS == "windows" {
		python = filepath.Join(root, ".venv", "Scripts", "python.exe")
	}
	process := exec.Command(python, "tests/fixtures/adaptive_execution_server.py", "--diagnosis")
	process.Dir = root
	process.Stderr = io.Discard
	input, e := process.StdinPipe()
	mustG7(t, e)
	output, e := process.StdoutPipe()
	mustG7(t, e)
	mustG7(t, process.Start())
	done := make(chan error, 1)
	go func() { done <- process.Wait() }()
	t.Cleanup(func() {
		_ = input.Close()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			_ = process.Process.Kill()
			<-done
		}
	})
	scanner := bufio.NewScanner(output)
	scanner.Buffer(make([]byte, 4096), 1024*1024)
	read := func(v any) {
		t.Helper()
		if !scanner.Scan() {
			t.Fatal("Core diagnosis producer exited before handshake")
		}
		if json.Unmarshal(scanner.Bytes(), v) != nil {
			t.Fatal("invalid private handshake; raw content withheld")
		}
	}
	type publicCase struct {
		Input    provider.AdaptiveInput `json:"input"`
		Expected asset.JSON             `json:"expected_output"`
		GT       asset.JSON             `json:"ground_truth"`
		Business string                 `json:"expected_business"`
		Facts    asset.JSON             `json:"facts"`
	}
	var ready struct {
		Principal string                `json:"principal_id"`
		Tenant    string                `json:"tenant_id"`
		Token     string                `json:"token"`
		Cases     map[string]publicCase `json:"cases"`
	}
	read(&ready)
	if len(ready.Cases) != 4 || ready.Cases["S1"].Input.ExecutionID == ready.Cases["S2"].Input.ExecutionID {
		t.Fatal("original/verification/diagnosis case set incomplete")
	}
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: f.k.Pool}, Datasets: postgres.Datasets{Pool: f.k.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: f.k.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: f.k.Pool}}
	source := asset.Source{Kind: "TEST", Ref: "public-adaptive-diagnosis-preflight.v1", Principal: f.s.Principal}
	refs := map[string]asset.Ref{}
	allRefs := []asset.Ref{}
	for _, label := range []string{"S1", "S2", "D1", "D2"} {
		ref := asset.Ref{EntityID: asset.NewID(), Version: "public-preflight-v1"}
		refs[label] = ref
		allRefs = append(allRefs, ref)
		cs := catalog.CaseService{Store: readers.Cases}
		_, e = cs.CreateCase(ctx, f.s.Scope, asset.Create{ID: ref.EntityID, Name: label})
		mustG7(t, e)
		c := testCase()
		c.Input, _ = asset.Freeze(ready.Cases[label].Input)
		c.ExpectedOutput = ready.Cases[label].Expected
		c.GroundTruth = ready.Cases[label].GT
		c.Metadata, _ = asset.Freeze(map[string]any{"development_visible": true, "expected_business": ready.Cases[label].Business, "facts_digest": ready.Cases[label].Facts.Digest(), "semantic_ground_truth": "NOT_AVAILABLE"})
		_, e = cs.PublishCaseVersion(ctx, f.s.Scope, asset.Publish[catalog.CaseContent]{Ref: ref, Body: c, Source: source})
		mustG7(t, e)
	}
	datasetAll := f.cmd.Snapshot.Input().Dataset.Ref
	datasetAll.Version = "public-preflight-v1"
	ds := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	_, e = ds.PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: datasetAll, Body: catalog.DatasetContent{Cases: allRefs}, Source: source})
	mustG7(t, e)
	dataset := datasetAll
	dataset.Version = "diagnosis-preflight-v1"
	_, e = ds.PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: dataset, Body: catalog.DatasetContent{Cases: []asset.Ref{refs["D1"], refs["D2"]}}, Source: source})
	mustG7(t, e)
	bindings := []catalog.EvaluatorBinding{}
	metrics := map[string]asset.Ref{}
	for _, name := range []string{"quality", "client_rt_ms", "model_attempts", "tool_invocations", "provider_tokens", "actual_cost_micros"} {
		mr := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
		er := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
		metrics[name] = mr
		ms := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
		_, e = ms.CreateMetricDefinition(ctx, f.s.Scope, asset.Create{ID: mr.EntityID, Name: name})
		mustG7(t, e)
		_, e = ms.PublishMetricDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.Definition]{Ref: mr, Body: agentquality.AdaptiveMetricDefinition(name), Source: source})
		mustG7(t, e)
		es := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
		_, e = es.CreateEvaluatorDefinition(ctx, f.s.Scope, asset.Create{ID: er.EntityID, Name: name})
		mustG7(t, e)
		d := agentquality.AdaptiveEvaluatorDefinition(mr, name)
		_, e = es.PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: er, Body: d, Source: source})
		mustG7(t, e)
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: er, Metrics: []asset.Ref{mr}, Required: name != "provider_tokens" && name != "actual_cost_micros", Applicability: d.Applicability})
	}
	snapshot, e := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &dataset, Evaluators: bindings})
	mustG7(t, e)
	allSnapshot, e := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &datasetAll, Evaluators: bindings})
	mustG7(t, e)
	f.k.Capabilities, e = f.k.LoadEvaluatorCapabilities(ctx, agentquality.DeterministicSupported)
	mustG7(t, e)
	frozen := snapshot.Input()
	frozenCases := map[string]any{}
	for _, c := range frozen.Manifest {
		var business provider.AdaptiveInput
		mustG7(t, c.Case.Input.Decode(&business))
		frozenCases[c.Identity.Ref.EntityID] = map[string]any{"case_version": c.Identity.Ref.Version, "case_digest": c.Identity.ContentDigest, "dataset_digest": frozen.Dataset.ContentDigest, "input": business}
	}
	mustG7(t, json.NewEncoder(input).Encode(map[string]any{"version": "adaptive-evaluation-config.v1", "project_id": f.s.ProjectID, "principal_id": ready.Principal, "tenant_id": ready.Tenant, "cases": frozenCases}))
	var endpoint struct {
		URL string `json:"url"`
	}
	read(&endpoint)
	t.Setenv("PUBLIC_DIAGNOSIS_TOKEN", ready.Token)
	sources := []decision.Source{}
	configs := []asset.JSON{}
	runIDs := []string{}
	evidenceByRun := map[string]agentquality.AdaptiveEvidence{}
	summaries := []map[string]any{}
	for _, group := range []string{"CANDIDATE_A", "CANDIDATE_B"} {
		cfg := provider.AdaptiveConfig{Transport: provider.AgentLoopConfig{BaseURL: endpoint.URL, TokenEnv: "PUBLIC_DIAGNOSIS_TOKEN", TrustedProjectID: f.s.ProjectID, MaxResponseBytes: 1024 * 1024, CallMilliseconds: 210000, HTTP: provider.DefaultHTTPConfig()}, Group: group, PolicyDigest: agentquality.AdaptivePolicyDigest(group), DatasetDigest: frozen.Dataset.ContentDigest}
		target, e := provider.NewAdaptiveTarget(cfg)
		mustG7(t, e)
		cfgJSON, _ := asset.Freeze(cfg)
		configs = append(configs, cfgJSON)
		command := f.cmd
		command.CommandID = asset.NewID()
		command.Snapshot = snapshot
		command.Target = ev.Target{ID: provider.AdaptiveTargetID, Version: provider.AdaptiveProtocol, Kind: "LOCALAGENT_HTTP", TimeoutMilliseconds: 180000, Config: cfgJSON}
		command.Subject, _ = asset.Freeze(map[string]string{"agent": "ci_incident_investigator", "revision": "public-observation-provider.v1", "environment": "isolated-pg", "run_mode": "COORDINATED"})
		created, e := f.k.CreateRun(ctx, f.s, command)
		reply(t, created, e, ev.Applied)
		wc := worker.DefaultConfig()
		wc.ExecutionConcurrency = 1
		wc.EvaluatorConcurrency = 1
		wc.PollInterval = 10 * time.Millisecond
		wc.MaxBackoff = 50 * time.Millisecond
		wc.DrainTimeout = 5 * time.Second
		running, cancel := context.WithCancel(ctx)
		wr := worker.Runtime{Config: wc, Backend: f.k, Target: target, Evaluator: agentquality.DeterministicEvaluator{}, Epoch: f.s.Epoch, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
		workerDone := make(chan error, 1)
		go func() { workerDone <- wr.Run(running) }()
		var state ev.RunState
		until := time.Now().Add(120 * time.Second)
		for time.Now().Before(until) {
			state, e = f.k.ReadRunState(ctx, f.s.Scope, created.ID)
			mustG7(t, e)
			if state.Run.Status.Terminal() {
				break
			}
			time.Sleep(30 * time.Millisecond)
		}
		cancel()
		mustG7(t, <-workerDone)
		target.Close()
		if !state.Run.Status.Terminal() || len(state.Results) != 12 || len(state.Attempts) != 2 {
			t.Fatalf("%s incomplete: status=%s attempts=%d results=%d", group, state.Run.Status, len(state.Attempts), len(state.Results))
		}
		for _, a := range state.Attempts {
			if a.Outcome != ev.Success {
				t.Fatalf("diagnosis not confirmed: attempt=%s outcome=%s category=%s", a.ID, a.Outcome, a.Metadata.Observation.ErrorCategory)
			}
			v, e := agentquality.DecodeAdaptive(a.Metadata.Observation.Evidence[0].Body)
			mustG7(t, e)
			label := "D1"
			wantModels, wantTools := 5, 5
			if group == "CANDIDATE_B" {
				wantModels, wantTools = 4, 4
			}
			if a.CaseID == refs["D2"].EntityID {
				label = "D2"
				wantModels, wantTools = 3, 3
			}
			if v.Business.Validation != "ACCEPTED" || v.Business.Outcome != ready.Cases[label].Business || v.Metrics.Models != wantModels || v.Metrics.Tools != wantTools || v.Metrics.Usage != nil || v.Metrics.Cost != nil {
				t.Fatalf("wrong %s business/counts/usage: %+v", label, v.Metrics)
			}
			frame := v.Mode.Loop.Frames[0]
			if group == "CANDIDATE_B" && label == "D1" {
				if frame.Stop == nil || frame.Stop.Reason != "COVERAGE_SUFFICIENT" || frame.Stop.AllowedActions != "FINAL_ONLY" {
					t.Fatal("missing original stop")
				}
			} else if frame.Stop != nil {
				t.Fatal("unexpected stop decision")
			}
			for _, r := range state.Results {
				if r.AttemptID == a.ID && r.EvaluatorID == bindings[0].Evaluator.EntityID {
					if label == "D1" && (r.Value.Score != nil || r.Value.Verdict != "INCONCLUSIVE") {
						t.Fatal("SUPPORTED invented semantic GT")
					}
					if label == "D2" && (r.Value.Score == nil || *r.Value.Score != 0 || r.Value.Verdict != "FAIL") {
						t.Fatal("insufficient counted as complete")
					}
				}
			}
			runIDs = append(runIDs, a.ID)
			evidenceByRun[a.ID] = v
			summaries = append(summaries, map[string]any{"group": group, "case": label, "evaluation_run_id": created.ID, "attempt_core_run_id": a.ID, "evidence": v, "result_count": 6})
			t.Logf("case=%s group=%s eval_run=%s attempt/core_run=%s outcome=%s validator=%s models=%d tools=%d observations=%d stop=%v", label, group, created.ID, a.ID, v.Business.Outcome, v.Business.Validation, v.Metrics.Models, v.Metrics.Tools, frame.Observations, frame.Stop)
		}
		src, e := decision.Offline(decision.SourceRef{Kind: "RUN_SET", Runs: []string{created.ID}}, []ev.RunState{state})
		mustG7(t, e)
		if src.AdaptiveLatency == nil || src.AdaptiveLatency.Planned != 2 || src.AdaptiveLatency.Known != 2 || src.AdaptiveTaskOutcomes["INCONCLUSIVE"] != 1 || src.AdaptiveTaskOutcomes["INSUFFICIENT_EVIDENCE"] != 1 || src.TaskCompletionOverPlanned == nil || *src.TaskCompletionOverPlanned != 0 {
			t.Fatal("lost quality denominator or incomplete outcome categories")
		}
		sources = append(sources, src)
	}
	// 第三次私有管道交换读取原 PG/ContextSnapshot 与模型实际请求选项，再关闭子进程。
	mustG7(t, json.NewEncoder(input).Encode(map[string]any{"audit": runIDs}))
	var audits map[string]struct {
		Stop         *agentquality.AdaptiveStop `json:"stop"`
		Seed         string                     `json:"seed_digest"`
		Committed    int                        `json:"committed_tools"`
		Cursors      []int                      `json:"observation_cursors"`
		FinalOptions struct {
			Choice string            `json:"tool_choice"`
			Tools  []json.RawMessage `json:"tools"`
		} `json:"final_options"`
		Requests []struct {
			Iteration int      `json:"iteration"`
			Choice    string   `json:"tool_choice"`
			Tools     []string `json:"tool_names"`
		} `json:"requests"`
	}
	read(&audits)
	for id, v := range evidenceByRun {
		a := audits[id]
		frame := v.Mode.Loop.Frames[0]
		stop, _ := asset.Freeze(frame.Stop)
		actualStop, _ := asset.Freeze(a.Stop)
		if stop.String() != actualStop.String() || a.Seed != frame.SeedDigest || a.Committed != v.Metrics.Tools || len(a.Cursors) != frame.Observations {
			t.Fatal("wire differs from actual PG facts")
		}
		if frame.Stop != nil {
			last := a.Requests[len(a.Requests)-1]
			if a.FinalOptions.Choice != "none" || len(a.FinalOptions.Tools) != 0 || last.Choice != "none" || len(last.Tools) != 0 || last.Iteration != frame.Stop.EffectiveIteration {
				t.Fatal("FINAL_ONLY request exposed Tool/Handoff")
			}
		}
	}
	var role string
	mustG7(t, f.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&role))
	_, e = f.db.pool.Exec(ctx, `GRANT INSERT,UPDATE ON evaluation_policies,evaluation_policy_versions TO `+pgx.Identifier{role}.Sanitize()+`; GRANT INSERT ON evaluation_comparisons,evaluation_gate_receipts,evaluation_case_comparisons TO `+pgx.Identifier{role}.Sanitize())
	mustG7(t, e)
	gate := postgres.Decisions{Pool: f.k.Pool}
	policyRef := asset.Ref{EntityID: asset.NewID(), Version: "quality-required-v1"}
	_, e = gate.CreatePolicy(ctx, f.s.Scope, asset.Create{ID: policyRef.EntityID, Name: "公开调查前置合同验收"})
	mustG7(t, e)
	one := 1.0
	policy := decision.Policy{Offline: true, Coverage: decision.CoveragePolicy{MinimumDecision: .5, MinimumEvaluation: 1, MaximumMissing: .5}, Metrics: []decision.MetricRule{{Metric: metrics["quality"], Aggregation: "mean", Minimum: &one, MinimumCoverage: 1, Required: true}}, AcceptedDifferences: []decision.AcceptedDifference{{Dimension: "target_config", Baseline: configs[0].Digest(), Candidate: configs[1].Digest(), Actor: f.s.Principal, Reason: "相同原业务/模型/工具/授权/Deadline/Seed，仅开启原动态停止", Proof: "public-diagnosis-stop-preflight.v1"}}}
	_, e = gate.PublishPolicyVersion(ctx, f.s.Scope, asset.Publish[decision.Policy]{Ref: policyRef, Body: policy, Source: source})
	mustG7(t, e)
	qualityGate, e := gate.CreateGate(ctx, f.s, decision.Command{ID: asset.NewID(), Baseline: sources[0].Ref, Candidate: sources[1].Ref, Policy: policyRef})
	mustG7(t, e)
	if qualityGate.Decision == decision.Pass {
		t.Fatal("missing semantic GT/insufficient result passed full quality gate")
	}
	// 技术接线政策不声称语义完成，仅验证已知调用/耗时和传输覆盖；不作为 Phase B 正式门禁。
	policyRef.Version = "transport-only-v1"
	maxCalls := 8.0
	policy.Metrics = []decision.MetricRule{{Metric: metrics["model_attempts"], Aggregation: "mean", Maximum: &maxCalls, MinimumCoverage: 1, Required: true}}
	_, e = gate.PublishPolicyVersion(ctx, f.s.Scope, asset.Publish[decision.Policy]{Ref: policyRef, Body: policy, Source: source})
	mustG7(t, e)
	command := decision.Command{ID: asset.NewID(), Baseline: sources[0].Ref, Candidate: sources[1].Ref, Policy: policyRef}
	receipt, e := gate.CreateGate(ctx, f.s, command)
	mustG7(t, e)
	saved, e := gate.GetGateReceipt(ctx, f.s.Scope, command.ID)
	mustG7(t, e)
	if receipt.Decision != decision.Pass || saved.Decision != decision.Pass || saved.Compatibility != decision.Accepted {
		t.Fatalf("technical gate=%s %v", saved.Decision, saved.Reasons)
	}
	// 实际 Seed 维度必须相同；精确策略差异证明不能掩盖前置条件变化。
	frozenComparison, e := gate.GetComparison(ctx, f.s.Scope, command.ID)
	mustG7(t, e)
	frozenComparison.Candidate.Dimensions["adaptive_context:"+refs["D1"].EntityID] = "changed-seed"
	blocked, e := decision.Compare(frozenComparison)
	mustG7(t, e)
	if blocked.Decision != decision.Blocked {
		t.Fatal("strategy proof masked initial seed drift")
	}
	t.Logf("four-case dataset=%s digest=%s diagnosis_dataset=%s cases=%v technical_gate=%s/%s semantic_quality_gate=%s/%s", datasetAll.EntityID, allSnapshot.Input().Dataset.ContentDigest, dataset.Version, refs, saved.GateID, saved.Decision, qualityGate.GateID, qualityGate.Decision)
	if path := os.Getenv("ADAPTIVE_PREFLIGHT_ARTIFACT_PATH"); path != "" {
		// 不写 ready.Token 或私有握手；仅保存公开合成冻结数据与证据。
		artifact := map[string]any{"version": "adaptive-experiment-preflight.v1", "phase_b_authorized": false, "project_id": f.s.ProjectID, "principal_id": ready.Principal, "tenant_id": ready.Tenant, "case_refs": refs, "core_cases": ready.Cases, "four_case_snapshot": allSnapshot.Input(), "diagnosis_snapshot": frozen, "runs": summaries, "pg_audit": audits, "technical_gate": saved, "semantic_quality_gate": qualityGate}
		raw, e := json.MarshalIndent(artifact, "", "  ")
		mustG7(t, e)
		mustG7(t, os.MkdirAll(filepath.Dir(path), 0700))
		mustG7(t, os.WriteFile(path, raw, 0600))
	}
}
