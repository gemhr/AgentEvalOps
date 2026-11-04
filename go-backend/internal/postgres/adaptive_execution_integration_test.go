//go:build integration

package postgres_test

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
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

func TestAdaptiveRealWorkerCoreHTTPPostgresGate(t *testing.T) {
	f := fixture(t)
	ctx := context.Background()
	root, e := filepath.Abs("../../../../Local_Agent")
	mustG7(t, e)
	python := filepath.Join(root, ".venv", "bin", "python")
	if runtime.GOOS == "windows" {
		python = filepath.Join(root, ".venv", "Scripts", "python.exe")
	}
	process := exec.Command(python, "tests/fixtures/adaptive_execution_server.py")
	process.Dir = root
	input, e := process.StdinPipe()
	mustG7(t, e)
	output, e := process.StdoutPipe()
	mustG7(t, e)
	// stderr 中没有密钥；stdout 私有管道含一次性 token，禁止输出原始行。
	process.Stderr = io.Discard
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
			t.Fatal("Core producer exited before handshake")
		}
		if json.Unmarshal(scanner.Bytes(), v) != nil {
			t.Fatal("invalid producer handshake")
		}
	}
	var ready struct {
		Principal string                 `json:"principal_id"`
		Tenant    string                 `json:"tenant_id"`
		Input     provider.AdaptiveInput `json:"input"`
		Expected  json.RawMessage        `json:"expected_output"`
		Token     string                 `json:"token"`
	}
	read(&ready)
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: f.k.Pool}, Datasets: postgres.Datasets{Pool: f.k.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: f.k.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: f.k.Pool}}
	original := f.cmd.Snapshot.Input()
	source := asset.Source{Kind: "TEST", Ref: "public-adaptive-http.v1", Principal: f.s.Principal}
	caseRef := original.Manifest[0].Identity.Ref
	caseRef.Version = "adaptive-v1"
	body := testCase()
	body.Input, e = asset.Freeze(ready.Input)
	mustG7(t, e)
	body.ExpectedOutput, e = asset.ParseJSON(ready.Expected)
	mustG7(t, e)
	_, e = (catalog.CaseService{Store: readers.Cases}).PublishCaseVersion(ctx, f.s.Scope, asset.Publish[catalog.CaseContent]{Ref: caseRef, Body: body, Source: source})
	mustG7(t, e)
	datasetRef := original.Dataset.Ref
	datasetRef.Version = "adaptive-v1"
	_, e = (catalog.DatasetService{Store: readers.Datasets, Cases: readers}).PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: datasetRef, Body: catalog.DatasetContent{Cases: []asset.Ref{caseRef}}, Source: source})
	mustG7(t, e)
	bindings := []catalog.EvaluatorBinding{}
	metrics := []asset.Ref{}
	for _, name := range []string{"quality", "client_rt_ms", "model_attempts", "tool_invocations", "provider_tokens", "actual_cost_micros"} {
		mr := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
		er := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
		ms := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
		_, e = ms.CreateMetricDefinition(ctx, f.s.Scope, asset.Create{ID: mr.EntityID, Name: name})
		mustG7(t, e)
		_, e = ms.PublishMetricDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.Definition]{Ref: mr, Body: agentquality.AdaptiveMetricDefinition(name), Source: source})
		mustG7(t, e)
		es := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
		_, e = es.CreateEvaluatorDefinition(ctx, f.s.Scope, asset.Create{ID: er.EntityID, Name: name})
		mustG7(t, e)
		definition := agentquality.AdaptiveEvaluatorDefinition(mr, name)
		_, e = es.PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: er, Body: definition, Source: source})
		mustG7(t, e)
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: er, Metrics: []asset.Ref{mr}, Required: name != "provider_tokens" && name != "actual_cost_micros", Applicability: definition.Applicability})
		metrics = append(metrics, mr)
	}
	f.cmd.Snapshot, e = (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &datasetRef, Evaluators: bindings})
	mustG7(t, e)
	f.k.Capabilities, e = f.k.LoadEvaluatorCapabilities(ctx, agentquality.DeterministicSupported)
	mustG7(t, e)
	frozen := f.cmd.Snapshot.Input()
	config := map[string]any{"version": "adaptive-evaluation-config.v1", "project_id": f.s.ProjectID, "principal_id": ready.Principal, "tenant_id": ready.Tenant, "cases": map[string]any{caseRef.EntityID: map[string]any{"case_version": caseRef.Version, "case_digest": frozen.Manifest[0].Identity.ContentDigest, "dataset_digest": frozen.Dataset.ContentDigest, "input": ready.Input}}}
	mustG7(t, json.NewEncoder(input).Encode(config))
	var endpoint struct {
		URL string `json:"url"`
	}
	read(&endpoint)
	for _, path := range []string{"/api/runtime/evaluation-execute/adaptive/v1", "/api/runtime/evaluation-execute/agent-loop/v1"} {
		request, e := http.NewRequest(http.MethodPost, endpoint.URL+path, nil)
		mustG7(t, e)
		want := 401
		if path != "/api/runtime/evaluation-execute/adaptive/v1" {
			request.Header.Set("Authorization", "Bearer "+ready.Token)
			want = 403
		}
		response, e := http.DefaultClient.Do(request)
		mustG7(t, e)
		_ = response.Body.Close()
		if response.StatusCode != want {
			t.Fatalf("production auth boundary %s=%d want %d", path, response.StatusCode, want)
		}
	}
	t.Setenv("PUBLIC_ADAPTIVE_TOKEN", ready.Token)
	sources := []decision.Source{}
	configs := []asset.JSON{}
	for _, group := range []string{"BASELINE", "CANDIDATE_A"} {
		cfg := provider.AdaptiveConfig{Transport: provider.AgentLoopConfig{BaseURL: endpoint.URL, TokenEnv: "PUBLIC_ADAPTIVE_TOKEN", TrustedProjectID: f.s.ProjectID, MaxResponseBytes: 1024 * 1024, CallMilliseconds: 90000, HTTP: provider.DefaultHTTPConfig()}, Group: group, PolicyDigest: agentquality.AdaptivePolicyDigest(group), DatasetDigest: frozen.Dataset.ContentDigest}
		target, e := provider.NewAdaptiveTarget(cfg)
		mustG7(t, e)
		cfgJSON, e := asset.Freeze(cfg)
		mustG7(t, e)
		configs = append(configs, cfgJSON)
		command := f.cmd
		command.CommandID = asset.NewID()
		command.Target = ev.Target{ID: provider.AdaptiveTargetID, Version: provider.AdaptiveProtocol, Kind: "LOCALAGENT_HTTP", TimeoutMilliseconds: 60000, Config: cfgJSON}
		command.Subject, _ = asset.Freeze(map[string]string{"agent": "ci_execution_status", "revision": "public-adaptive-v1", "environment": "isolated-pg", "run_mode": "COORDINATED"})
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
		until := time.Now().Add(100 * time.Second)
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
		if !state.Run.Status.Terminal() || len(state.Results) != 6 || len(state.Attempts) != 1 || state.Attempts[0].Outcome != ev.Success {
			t.Fatalf("%s incomplete: status=%s results=%d attempts=%d", group, state.Run.Status, len(state.Results), len(state.Attempts))
		}
		attempt := state.Attempts[0]
		evidence, e := agentquality.DecodeAdaptive(attempt.Metadata.Observation.Evidence[0].Body)
		mustG7(t, e)
		wantModels := 0
		if group == "BASELINE" {
			wantModels = 2
		}
		if evidence.Metrics.Models != wantModels || evidence.Metrics.Tools != 1 || evidence.Metrics.Usage != nil || evidence.Metrics.Cost != nil {
			t.Fatal("noncanonical invocation/usage counts")
		}
		for _, result := range state.Results {
			if result.EvaluatorID == bindings[0].Evaluator.EntityID && (result.Value.Score == nil || *result.Value.Score != 1) {
				t.Fatalf("quality mismatch %+v", result.Value)
			}
		}
		src, e := decision.Offline(decision.SourceRef{Kind: "RUN_SET", Runs: []string{created.ID}}, []ev.RunState{state})
		mustG7(t, e)
		if src.AdaptiveLatency == nil || src.AdaptiveLatency.Planned != 1 || src.AdaptiveLatency.Known != 1 || src.TaskCompletionOverPlanned == nil || *src.TaskCompletionOverPlanned != 1 {
			t.Fatalf("missing denominator/latency: %+v", src)
		}
		sources = append(sources, src)
		t.Logf("group=%s dataset=%s case=%s eval_run=%s attempt/core_run=%s results=%d models=%d tools=%d rt_ms=%d", group, datasetRef.EntityID, caseRef.EntityID, created.ID, attempt.ID, len(state.Results), evidence.Metrics.Models, evidence.Metrics.Tools, *src.AdaptiveLatency.P50)
		// 对真实 Producer 响应验证严格合同，避免仅测试手写黄金样本。
		var mutated map[string]any
		mustG7(t, json.Unmarshal(attempt.Metadata.Observation.Evidence[0].Body.Bytes(), &mutated))
		mutated["schema_version"] = "adaptive-execution-evidence.v999"
		bad, _ := asset.Freeze(mutated)
		if _, e = agentquality.DecodeAdaptive(bad); e == nil {
			t.Fatal("unknown protocol accepted")
		}
		mutated["schema_version"] = agentquality.AdaptiveSchema
		mutated["unknown_field"] = true
		bad, _ = asset.Freeze(mutated)
		if _, e = agentquality.DecodeAdaptive(bad); e == nil {
			t.Fatal("unknown field accepted")
		}
		delete(mutated, "unknown_field")
		mutated["mode_evidence"].(map[string]any)["fast"] = map[string]any{"step_count": 1, "tool_refs": []any{}}
		bad, _ = asset.Freeze(mutated)
		if group == "BASELINE" {
			if _, e = agentquality.DecodeAdaptive(bad); e == nil {
				t.Fatal("mixed mode accepted")
			}
		}
	}
	one := 1.0
	policy := decision.Policy{Offline: true, Coverage: decision.CoveragePolicy{MinimumDecision: 1, MinimumEvaluation: 1}, Metrics: []decision.MetricRule{{Metric: metrics[0], Aggregation: "mean", Minimum: &one, MinimumCoverage: 1, Required: true}}}
	snap := decision.Snapshot{ProjectID: f.s.ProjectID, Baseline: sources[0], Candidate: sources[1], Policy: policy, CapturedAt: time.Now().UTC()}
	receipt, e := decision.Compare(snap)
	mustG7(t, e)
	if receipt.Decision != decision.Blocked {
		t.Fatalf("unproved strategy gate=%s", receipt.Decision)
	}
	snap.Policy.AcceptedDifferences = []decision.AcceptedDifference{{Dimension: "target_config", Baseline: configs[0].Digest(), Candidate: configs[1].Digest(), Actor: f.s.Principal, Reason: "冻结 Case/输入/授权/工具数据/模型/Deadline 相同，仅切换已声明的路由策略", Proof: "public-adaptive-strategy-difference.v1"}}
	receipt, e = decision.Compare(snap)
	mustG7(t, e)
	if receipt.Decision != decision.Blocked {
		t.Fatal("required full coverage hid missing Usage/cost")
	}
	// 本验收只要求四项已知指标和质量；两项无 Provider 证据保持缺失。
	// 这是公开合成合同测试政策，不是 WP4 的正式实验阈值。
	snap.Policy.Coverage.MinimumDecision = 2.0 / 3
	snap.Policy.Coverage.MaximumMissing = 1.0 / 3
	receipt, e = decision.Compare(snap)
	mustG7(t, e)
	if receipt.Decision != decision.Pass {
		t.Fatalf("proved strategy gate=%s %v", receipt.Decision, receipt.Reasons)
	}
	// 精确映射只允许这对 digest；不能掩盖另一个配置变化。
	originalTarget := snap.Candidate.Dimensions["target_config"]
	var unapproved provider.AdaptiveConfig
	mustG7(t, configs[1].Decode(&unapproved))
	unapproved.Transport.CallMilliseconds++
	changed, _ := asset.Freeze(unapproved)
	snap.Candidate.Dimensions["target_config"] = changed.Digest()
	blocked, e := decision.Compare(snap)
	mustG7(t, e)
	if blocked.Decision != decision.Blocked {
		t.Fatal("unrelated configuration difference admitted")
	}
	snap.Candidate.Dimensions["target_config"] = originalTarget
	key := "adaptive_context:" + caseRef.EntityID
	snap.Candidate.Dimensions[key] = "changed-model-or-memory-seed"
	blocked, e = decision.Compare(snap)
	mustG7(t, e)
	if blocked.Decision != decision.Blocked {
		t.Fatal("strategy proof masked environment/seed drift")
	}
	// 原正式 Gate Service 从 PG 重新读取并冻结事实，持久化 Comparison/Gate/CaseComparison。
	var role string
	mustG7(t, f.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&role))
	_, e = f.db.pool.Exec(ctx, `GRANT INSERT,UPDATE ON evaluation_policies,evaluation_policy_versions TO `+pgx.Identifier{role}.Sanitize()+`; GRANT INSERT ON evaluation_comparisons,evaluation_gate_receipts,evaluation_case_comparisons TO `+pgx.Identifier{role}.Sanitize())
	mustG7(t, e)
	gate := postgres.Decisions{Pool: f.k.Pool}
	policyRef := asset.Ref{EntityID: asset.NewID(), Version: "public-adaptive-v1"}
	_, e = gate.CreatePolicy(ctx, f.s.Scope, asset.Create{ID: policyRef.EntityID, Name: "公开自适应合同验收政策"})
	mustG7(t, e)
	_, e = gate.PublishPolicyVersion(ctx, f.s.Scope, asset.Publish[decision.Policy]{Ref: policyRef, Body: snap.Policy, Source: source})
	mustG7(t, e)
	command := decision.Command{ID: asset.NewID(), Baseline: sources[0].Ref, Candidate: sources[1].Ref, Policy: policyRef}
	durable, e := gate.CreateGate(ctx, f.s, command)
	mustG7(t, e)
	saved, e := gate.GetGateReceipt(ctx, f.s.Scope, command.ID)
	mustG7(t, e)
	if durable.Decision != decision.Pass || saved.Decision != decision.Pass || saved.GateID != command.ID {
		t.Fatalf("durable gate=%s %v", saved.Decision, saved.Reasons)
	}
	summary, e := gate.GetRegressionSummary(ctx, f.s.Scope, command.ID)
	mustG7(t, e)
	t.Logf("gate=%s policy=%s comparison=%s compatibility=%s regression_summary=%+v; required quality=1; missing provider_usage/cost remain null", saved.GateID, policyRef.EntityID, saved.Decision, saved.Compatibility, summary)
}
