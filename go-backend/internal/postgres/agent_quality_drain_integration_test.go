//go:build integration

package postgres_test

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
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

func TestAgentQualityWorkerDrainUnknownGate(t *testing.T) {
	f := fixture(t)
	ctx := context.Background()
	live := qualityProducer(t, f.s.ProjectID, "--drain")
	t.Setenv("PUBLIC_QUALITY_TEST_TOKEN", "public-quality-test-token")
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	var role string
	must(f.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&role))
	_, err := f.db.pool.Exec(ctx, "GRANT INSERT,UPDATE ON evaluation_policies,evaluation_policy_versions TO "+pgx.Identifier{role}.Sanitize()+"; GRANT INSERT ON evaluation_comparisons,evaluation_gate_receipts,evaluation_case_comparisons TO "+pgx.Identifier{role}.Sanitize())
	must(err)
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: f.k.Pool}, Datasets: postgres.Datasets{Pool: f.k.Pool}, Suites: postgres.Suites{Pool: f.k.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: f.k.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: f.k.Pool}}
	source := asset.Source{Kind: "PUBLIC_SYNTHETIC", Ref: "agent-quality-public.v1", Principal: f.s.Principal}
	ref := func() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "v1"} }
	cs := catalog.CaseService{Store: readers.Cases}
	ds := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	ms := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
	es := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	ss := catalog.SuiteService{Store: readers.Suites, References: readers}
	cases, err := agentquality.PublicQualityCases(live.Memory["allowed"], live.Memory["forbidden"])
	must(err)
	// 仅采用原公开 Handoff Case，GT 不修改、不发送给 Producer。
	cr := ref()
	_, err = cs.CreateCase(ctx, f.s.Scope, asset.Create{ID: cr.EntityID, Name: "public handoff drain"})
	must(err)
	_, err = cs.PublishCaseVersion(ctx, f.s.Scope, asset.Publish[catalog.CaseContent]{Ref: cr, Body: cases[2], Source: source})
	must(err)
	dr := ref()
	_, err = ds.CreateDataset(ctx, f.s.Scope, asset.Create{ID: dr.EntityID, Name: "single public handoff"})
	must(err)
	_, err = ds.PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: dr, Body: catalog.DatasetContent{Cases: []asset.Ref{cr}}, Source: source})
	must(err)
	metrics := []asset.Ref{}
	bindings := []catalog.EvaluatorBinding{}
	for _, name := range []string{"agent_execution_integrity.v1", "task_success.v1"} {
		var definition metric.Definition
		for _, b := range metric.Builtins() {
			if b.Key+"."+b.Version == name {
				definition = b.Definition
			}
		}
		mr, er := ref(), ref()
		_, err = ms.CreateMetricDefinition(ctx, f.s.Scope, asset.Create{ID: mr.EntityID, Name: name})
		must(err)
		_, err = ms.PublishMetricDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.Definition]{Ref: mr, Body: definition, Source: source})
		must(err)
		_, err = es.CreateEvaluatorDefinition(ctx, f.s.Scope, asset.Create{ID: er.EntityID, Name: name})
		must(err)
		body := agentquality.BehaviorEvaluatorDefinition(mr, name)
		_, err = es.PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: er, Body: body, Source: source})
		must(err)
		metrics = append(metrics, mr)
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: er, Metrics: []asset.Ref{mr}, Required: true, Applicability: body.Applicability})
	}
	suite := ref()
	_, err = ss.CreateSuite(ctx, f.s.Scope, asset.Create{ID: suite.EntityID, Name: "handoff drain required"})
	must(err)
	_, err = ss.PublishSuiteVersion(ctx, f.s.Scope, asset.Publish[catalog.SuiteContent]{Ref: suite, Body: catalog.SuiteContent{Dataset: &dr, Evaluators: bindings, Metrics: metrics, Applicability: asset.Applicability{RuleRef: "agent-quality-suite.v1"}}, Source: source})
	must(err)
	snapshot, err := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Suite: &suite})
	must(err)
	caps, err := f.k.LoadEvaluatorCapabilities(ctx, agentquality.DeterministicSupported)
	must(err)
	f.k.Capabilities = caps
	configs := map[string]asset.JSON{}
	create := func(variant string) string {
		cfg := live.Configs[variant]
		cfg.HTTP = provider.DefaultHTTPConfig()
		j, e := asset.Freeze(cfg)
		must(e)
		configs[variant] = j
		subject, e := asset.Freeze(map[string]string{"subject": "core_router", "subject_version": cfg.Manifest.Digest(), "environment": "public-isolated-pg", "run_mode": "agent-quality-public"})
		must(e)
		reply, e := f.k.CreateRun(ctx, f.s, ev.CreateRun{CommandID: asset.NewID(), Snapshot: snapshot, Target: ev.Target{ID: provider.AgentQualityTargetID, Kind: "LOCALAGENT_HTTP", Version: provider.AgentQualityTargetVersion, TimeoutMilliseconds: 60000, Config: j}, Subject: subject, Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}})
		must(e)
		if reply.Code != ev.Applied {
			t.Fatal(reply.Code)
		}
		return reply.ID
	}
	start := func(variant string) (context.CancelFunc, chan error) {
		cfg := live.Configs[variant]
		cfg.HTTP = provider.DefaultHTTPConfig()
		target, e := provider.NewAgentQualityTarget(cfg, nil)
		must(e)
		t.Cleanup(func() { target.Close() })
		wc := worker.DefaultConfig()
		wc.ExecutionConcurrency = 1
		wc.EvaluatorConcurrency = 1
		wc.PollInterval = 20 * time.Millisecond
		wc.MaxBackoff = 100 * time.Millisecond
		wc.ScanBatch = 1
		wc.DrainTimeout = 100 * time.Millisecond
		runtime := &worker.Runtime{Config: wc, Backend: f.k, Target: target, Evaluator: agentquality.DeterministicEvaluator{}, Epoch: f.s.Epoch, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
		running, cancel := context.WithCancel(ctx)
		done := make(chan error, 1)
		go func() { done <- runtime.Run(running) }()
		return cancel, done
	}
	waitRun := func(id string) {
		until := time.Now().Add(40 * time.Second)
		for time.Now().Before(until) {
			state, e := f.k.ReadRunState(ctx, f.s.Scope, id)
			must(e)
			if state.Run.Status.Terminal() {
				return
			}
			time.Sleep(30 * time.Millisecond)
		}
		t.Fatal("single-case worker terminal timeout")
	}
	baseline := create("baseline")
	cancel, done := start("baseline")
	waitRun(baseline)
	cancel()
	must(<-done)
	normal, err := f.k.ReadRunState(ctx, f.s.Scope, baseline)
	must(err)
	if len(normal.Attempts) != 1 || len(normal.Results) != 2 {
		t.Fatal("baseline facts missing")
	}
	for _, r := range normal.Results {
		if r.Value.Verdict != "PASS" {
			t.Fatal("baseline", r.Value)
		}
	}
	candidate := create("drain")
	cancel, done = start("drain")
	held := filepath.Join(live.Directory, "response-held.json")
	deadline := time.Now().Add(35 * time.Second)
	for time.Now().Before(deadline) {
		if _, e := os.Stat(held); e == nil {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	raw, err := os.ReadFile(held)
	must(err)
	var remote struct {
		Run              string `json:"run_id"`
		Digest           string `json:"sealed_digest"`
		BusinessReceipts int    `json:"business_receipts"`
	}
	must(json.Unmarshal(raw, &remote))
	if remote.BusinessReceipts != 2 {
		t.Fatal("actual business receipts missing", remote.BusinessReceipts)
	}
	// Python 已真实执行并封存；响应仍被 server barrier 阻断。取消触发原 Worker drain。
	cancel()
	select {
	case err = <-done:
		must(err)
	case <-time.After(10 * time.Second):
		t.Fatal("worker drain did not join")
	}
	state, err := f.k.ReadRunState(ctx, f.s.Scope, candidate)
	must(err)
	if len(state.Attempts) != 1 || state.Attempts[0].ID != remote.Run || state.Attempts[0].Outcome != ev.Unknown || len(state.Works) != 0 || len(state.Results) != 0 {
		t.Fatalf("drain facts: %+v", state)
	}
	// 原 Worker 重启扫描原 PG；不能再次 POST 或为 UNKNOWN 创建 Work。
	stop, restarted := start("drain")
	waitRun(candidate)
	stop()
	must(<-restarted)
	req, err := http.NewRequest(http.MethodGet, live.Configs["drain"].BaseURL+"/api/runtime/runs/"+remote.Run+"/agent-quality-evidence/v1", nil)
	must(err)
	req.Header.Set("Authorization", "Bearer public-quality-test-token")
	response, err := http.DefaultClient.Do(req)
	must(err)
	body, err := io.ReadAll(response.Body)
	response.Body.Close()
	must(err)
	if response.StatusCode != 200 {
		t.Fatal("readonly investigation", response.StatusCode)
	}
	var sealed struct {
		Digest string `json:"bundle_digest"`
		Status string `json:"status"`
	}
	must(json.Unmarshal(body, &sealed))
	canonical, e := asset.ParseJSON(body)
	must(e)
	if sealed.Status != "SUCCEEDED" || canonical.Digest() != remote.Digest {
		t.Fatal("sealed evidence identity changed")
	}
	after, err := f.k.ReadRunState(ctx, f.s.Scope, candidate)
	must(err)
	if len(after.Attempts) != 1 || after.Attempts[0].Outcome != ev.Unknown || len(after.Works) != 0 || len(after.Results) != 0 {
		t.Fatal("UNKNOWN rewritten")
	}
	for _, variant := range []string{"baseline", "drain"} {
		lines, e := os.ReadFile(filepath.Join(live.Directory, variant+"-posts.jsonl"))
		must(e)
		if strings.Count(string(lines), "\n") != 1 {
			t.Fatal("duplicate POST", variant)
		}
		lines, e = os.ReadFile(filepath.Join(live.Directory, variant+"-models.jsonl"))
		must(e)
		if strings.Count(string(lines), "\n") != 4 {
			t.Fatal("duplicate provider", variant)
		}
	}
	lines, err := os.ReadFile(filepath.Join(live.Directory, "dispatch.jsonl"))
	must(err)
	if strings.Count(string(lines), "\n") != 2 {
		t.Fatal("business effect dispatched again")
	}
	one := floatRef(1)
	policy := decision.Policy{Offline: true, Criticalities: []string{"CRITICAL"}, CriticalMissing: true, BlockPerCase: true, Coverage: decision.CoveragePolicy{MinimumDecision: 1, MinimumEvaluation: 1}, TaskSuccess: &decision.MetricRule{Metric: metrics[1], Aggregation: "rate", Minimum: one, MinimumCoverage: 1, Required: true, PerCase: true}, Metrics: []decision.MetricRule{{Metric: metrics[0], Aggregation: "rate", Minimum: one, MinimumCoverage: 1, Required: true, PerCase: true}}, AcceptedDifferences: []decision.AcceptedDifference{{Dimension: "target_config", Baseline: configs["baseline"].Digest(), Candidate: configs["drain"].Digest(), Actor: f.s.Principal, Reason: "公开单Case停机验收", Proof: "public-drain-deployment.v1"}}}
	store := postgres.Decisions{Pool: f.k.Pool}
	pr := ref()
	_, err = store.CreatePolicy(ctx, f.s.Scope, asset.Create{ID: pr.EntityID, Name: "drain required gate"})
	must(err)
	_, err = store.PublishPolicyVersion(ctx, f.s.Scope, asset.Publish[decision.Policy]{Ref: pr, Body: policy, Source: source})
	must(err)
	command := decision.Command{ID: asset.NewID(), Baseline: decision.SourceRef{Kind: "RUN_SET", Runs: []string{baseline}}, Candidate: decision.SourceRef{Kind: "RUN_SET", Runs: []string{candidate}}, Policy: pr}
	receipt, err := store.CreateGate(ctx, f.s, command)
	must(err)
	if receipt.Decision != decision.Blocked {
		t.Fatal("drain gate must BLOCK", receipt.Decision, receipt.Reasons)
	}
	saved, err := store.GetGateReceipt(ctx, f.s.Scope, command.ID)
	must(err)
	if saved.Decision != decision.Blocked {
		t.Fatal("gate not durable")
	}
	t.Logf("project=%s baseline=%s candidate=%s attempt=%s seal=%s gate=%s decision=%s exit=%d posts=2 model_calls=8 business_dispatches=2", f.s.ProjectID, baseline, candidate, remote.Run, remote.Digest, command.ID, receipt.Decision, decision.ExitCode(receipt.Decision))
	must(os.WriteFile(filepath.Join(live.Directory, "release-response"), []byte("release"), 0600))
}
