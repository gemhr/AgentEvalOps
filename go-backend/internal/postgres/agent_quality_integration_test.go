//go:build integration

package postgres_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
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

type qualityLive struct {
	Configs   map[string]provider.AgentQualityConfig `json:"configs"`
	Memory    map[string]string                      `json:"memory_refs"`
	Directory string                                 `json:"-"`
}

func qualityProducer(t *testing.T, project string, extra ...string) qualityLive {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", "..", "..", "..", "Local_Agent"))
	if err != nil {
		t.Fatal(err)
	}
	directory := t.TempDir()
	if len(extra) > 0 && extra[0] == "--capacity" {
		directory = os.Getenv("QUALITY_CAPACITY_EVIDENCE_DIR")
		if directory == "" {
			t.Fatal("capacity evidence directory required")
		}
		if err := os.MkdirAll(directory, 0700); err != nil {
			t.Fatal(err)
		}
	}
	python := filepath.Join(root, ".venv", "bin", "python")
	if runtime.GOOS == "windows" {
		python = filepath.Join(root, ".venv", "Scripts", "python.exe")
	}
	args := append([]string{"tests/fixtures/quality_http_service.py", "--project-id", project, "--directory", directory}, extra...)
	cmd := exec.Command(python, args...)
	if len(extra) > 0 && extra[0] == "--capacity" {
		// 容量子进程只继承启动所需路径与显式隔离DB，不转交真实Provider/CI凭据。
		cmd.Env = []string{}
		for _, key := range []string{"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PYTHONIOENCODING", "LOCAL_AGENT_TEST_DATABASE_URL"} {
			if value, ok := os.LookupEnv(key); ok {
				cmd.Env = append(cmd.Env, key+"="+value)
			}
		}
	}
	cmd.Dir = root
	log := &processLog{}
	cmd.Stdout = log
	cmd.Stderr = log
	if err = cmd.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	var processErr error
	go func() { processErr = cmd.Wait(); close(done) }()
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("Python producer did not stop")
		}
	})
	ready := filepath.Join(directory, "ready.json")
	until := time.Now().Add(45 * time.Second)
	for time.Now().Before(until) {
		if raw, err := os.ReadFile(ready); err == nil {
			var live qualityLive
			if json.Unmarshal(raw, &live) != nil {
				t.Fatal("producer config")
			}
			live.Directory = directory
			return live
		}
		select {
		case <-done:
			t.Fatalf("producer initialization: %v %s", processErr, log.text())
		default:
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("producer readiness timeout", log.text())
	return qualityLive{}
}

type qualityErrorEvaluator struct{}

func (qualityErrorEvaluator) Evaluate(context.Context, worker.EvaluationInput) (worker.EvaluationOutput, error) {
	return worker.EvaluationOutput{}, errors.New("SYNTHETIC_EVALUATOR_UNAVAILABLE")
}

func TestAgentQualityPublishedSuiteGate(t *testing.T) {
	f := fixture(t)
	ctx := context.Background()
	live := qualityProducer(t, f.s.ProjectID)
	t.Setenv("PUBLIC_QUALITY_TEST_TOKEN", "public-quality-test-token")
	var role string
	if err := f.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&role); err != nil {
		t.Fatal(err)
	}
	_, err := f.db.pool.Exec(ctx, "GRANT INSERT,UPDATE ON evaluation_policies,evaluation_policy_versions TO "+pgx.Identifier{role}.Sanitize()+"; GRANT INSERT ON evaluation_comparisons,evaluation_gate_receipts,evaluation_case_comparisons TO "+pgx.Identifier{role}.Sanitize())
	if err != nil {
		t.Fatal(err)
	}
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: f.k.Pool}, Datasets: postgres.Datasets{Pool: f.k.Pool}, Suites: postgres.Suites{Pool: f.k.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: f.k.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: f.k.Pool}}
	source := asset.Source{Kind: "PUBLIC_SYNTHETIC", Ref: "agent-quality-public.v1", Principal: f.s.Principal}
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	ref := func() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "v1"} }
	caseService := catalog.CaseService{Store: readers.Cases}
	datasetService := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	metricService := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
	evalService := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	suiteService := catalog.SuiteService{Store: readers.Suites, References: readers}
	cases, err := agentquality.PublicQualityCases(live.Memory["allowed"], live.Memory["forbidden"])
	must(err)
	refs := []asset.Ref{}
	for i, c := range cases {
		r := ref()
		_, err = caseService.CreateCase(ctx, f.s.Scope, asset.Create{ID: r.EntityID, Name: fmt.Sprintf("public quality %d", i)})
		must(err)
		_, err = caseService.PublishCaseVersion(ctx, f.s.Scope, asset.Publish[catalog.CaseContent]{Ref: r, Body: c, Source: source})
		must(err)
		refs = append(refs, r)
	}
	dataset := ref()
	_, err = datasetService.CreateDataset(ctx, f.s.Scope, asset.Create{ID: dataset.EntityID, Name: "public quality dataset"})
	must(err)
	_, err = datasetService.PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: dataset, Body: catalog.DatasetContent{Cases: refs}, Source: source})
	must(err)
	metrics := []asset.Ref{}
	bindings := []catalog.EvaluatorBinding{}
	for _, name := range []string{"agent_execution_integrity.v1", "task_success.v1"} {
		var definition metric.Definition
		for _, builtin := range metric.Builtins() {
			if builtin.Key+"."+builtin.Version == name {
				definition = builtin.Definition
			}
		}
		mr := ref()
		_, err = metricService.CreateMetricDefinition(ctx, f.s.Scope, asset.Create{ID: mr.EntityID, Name: name})
		must(err)
		_, err = metricService.PublishMetricDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.Definition]{Ref: mr, Body: definition, Source: source})
		must(err)
		metrics = append(metrics, mr)
		er := ref()
		_, err = evalService.CreateEvaluatorDefinition(ctx, f.s.Scope, asset.Create{ID: er.EntityID, Name: name})
		must(err)
		body := agentquality.BehaviorEvaluatorDefinition(mr, name)
		_, err = evalService.PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: er, Body: body, Source: source})
		must(err)
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: er, Metrics: []asset.Ref{mr}, Required: true, Applicability: body.Applicability})
	}
	suite := ref()
	_, err = suiteService.CreateSuite(ctx, f.s.Scope, asset.Create{ID: suite.EntityID, Name: "public agent quality"})
	must(err)
	_, err = suiteService.PublishSuiteVersion(ctx, f.s.Scope, asset.Publish[catalog.SuiteContent]{Ref: suite, Body: catalog.SuiteContent{Dataset: &dataset, Evaluators: bindings, Metrics: metrics, Applicability: asset.Applicability{RuleRef: "agent-quality-suite.v1"}}, Source: source})
	must(err)
	snapshot, err := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Suite: &suite})
	must(err)
	caps, err := f.k.LoadEvaluatorCapabilities(ctx, agentquality.DeterministicSupported)
	must(err)
	f.k.Capabilities = caps
	configs := map[string]asset.JSON{}
	run := func(variant string, localError bool) string {
		config := live.Configs[variant]
		config.HTTP = provider.DefaultHTTPConfig()
		j, err := asset.Freeze(config)
		must(err)
		configs[variant] = j
		subject, _ := asset.Freeze(map[string]string{"subject": "core_router", "subject_version": config.Manifest.Digest(), "environment": "public-isolated-pg", "run_mode": "agent-quality-public"})
		command := ev.CreateRun{CommandID: asset.NewID(), Snapshot: snapshot, Target: ev.Target{ID: provider.AgentQualityTargetID, Kind: "LOCALAGENT_HTTP", Version: provider.AgentQualityTargetVersion, TimeoutMilliseconds: 60000, Config: j}, Subject: subject, Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}
		replyValue, err := f.k.CreateRun(ctx, f.s, command)
		if err != nil || replyValue.Code != ev.Applied {
			t.Fatalf("create quality run: %s %v", replyValue.Code, err)
		}
		target, err := provider.NewAgentQualityTarget(config, nil)
		must(err)
		defer target.Close()
		wc := worker.DefaultConfig()
		wc.ExecutionConcurrency = 2
		wc.EvaluatorConcurrency = 2
		wc.PollInterval = 20 * time.Millisecond
		wc.MaxBackoff = 100 * time.Millisecond
		wc.ScanBatch = 5
		wc.DrainTimeout = 5 * time.Second
		evaluator := worker.Evaluator(agentquality.DeterministicEvaluator{})
		if localError {
			evaluator = qualityErrorEvaluator{}
		}
		r := worker.Runtime{Config: wc, Backend: f.k, Target: target, Evaluator: evaluator, Epoch: f.s.Epoch, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
		running, cancel := context.WithCancel(ctx)
		done := make(chan error, 1)
		go func() { done <- r.Run(running) }()
		terminal := false
		until := time.Now().Add(150 * time.Second)
		for time.Now().Before(until) {
			state, err := f.k.ReadRunState(ctx, f.s.Scope, replyValue.ID)
			must(err)
			if state.Run.Status.Terminal() {
				terminal = true
				break
			}
			time.Sleep(50 * time.Millisecond)
		}
		cancel()
		must(<-done)
		if !terminal {
			state, _ := f.k.ReadRunState(ctx, f.s.Scope, replyValue.ID)
			t.Fatalf("quality run timeout: variant=%s status=%s attempts=%d works=%d results=%d", variant, state.Run.Status, len(state.Attempts), len(state.Works), len(state.Results))
		}
		state, err := f.k.ReadRunState(ctx, f.s.Scope, replyValue.ID)
		must(err)
		if variant == "baseline" || variant == "candidate" || variant == "regression" {
			if len(state.Attempts) != 5 || len(state.Works) != 10 || len(state.Results) != 10 {
				t.Fatalf("real run missing facts: variant=%s attempts=%d works=%d results=%d", variant, len(state.Attempts), len(state.Works), len(state.Results))
			}
			for _, a := range state.Attempts {
				if a.Outcome != ev.Success {
					t.Fatalf("remote run not succeeded: variant=%s outcome=%s category=%s", variant, a.Outcome, a.Metadata.Observation.ErrorCategory)
				}
			}
			if localError {
				for _, result := range state.Results {
					if result.Value.Verdict != "ERROR" || result.Value.Score != nil {
						t.Fatal("injected evaluator error must remain ERROR/null")
					}
				}
			} else if variant != "regression" {
				for _, result := range state.Results {
					if result.Value.Verdict != "PASS" {
						t.Fatalf("baseline/candidate must meet independent GT: variant=%s verdict=%s reason=%s", variant, result.Value.Verdict, result.Value.Reason)
					}
				}
			} else {
				for _, result := range state.Results {
					var work ev.Work
					for _, w := range state.Works {
						if result.WorkID != nil && w.ID == *result.WorkID {
							work = w
						}
					}
					var config agentquality.Config
					must(work.Metadata.Spec.Definition.Config.Decode(&config))
					label := ""
					for _, a := range state.Attempts {
						if a.ID == work.AttemptID {
							label = a.Request.Case.Case.Tags[2]
						}
					}
					want := "FAIL"
					if config.Metric == "agent_execution_integrity.v1" || label == "memory" {
						want = "PASS"
					}
					if result.Value.Verdict != want {
						t.Fatalf("regression business boundary: case=%s metric=%s want=%s got=%s reason=%s", label, config.Metric, want, result.Value.Verdict, result.Value.Reason)
					}
				}
			}
		}
		return replyValue.ID
	}
	baseline := run("baseline", false)
	candidate := run("candidate", false)
	regression := run("regression", false)
	unknown := run("unknown", false)
	errorRun := run("candidate", true)
	gateStore := postgres.Decisions{Pool: f.k.Pool}
	compare := func(candidateRun, variant string, want decision.Decision) {
		one := floatRef(1)
		p := decision.Policy{Offline: true, Criticalities: []string{"CRITICAL"}, CriticalMissing: true, BlockPerCase: true,
			Coverage:    decision.CoveragePolicy{MinimumDecision: 1, MinimumEvaluation: 1},
			TaskSuccess: &decision.MetricRule{Metric: metrics[1], Aggregation: "rate", Minimum: one, MinimumCoverage: 1, Required: true, PerCase: true},
			Metrics:     []decision.MetricRule{{Metric: metrics[0], Aggregation: "rate", Minimum: one, MinimumCoverage: 1, Required: true, PerCase: true}},
			AcceptedDifferences: []decision.AcceptedDifference{{Dimension: "target_config", Baseline: configs["baseline"].Digest(), Candidate: configs[variant].Digest(),
				Actor: f.s.Principal, Reason: "受控公开部署版本比较；Case与Ground Truth及评分定义保持冻结", Proof: "public-agent-quality-deployment-diff.v1"}}}
		pr := ref()
		_, err = gateStore.CreatePolicy(ctx, f.s.Scope, asset.Create{ID: pr.EntityID, Name: "required quality gate"})
		must(err)
		_, err = gateStore.PublishPolicyVersion(ctx, f.s.Scope, asset.Publish[decision.Policy]{Ref: pr, Body: p, Source: source})
		must(err)
		command := decision.Command{ID: asset.NewID(), Baseline: decision.SourceRef{Kind: "RUN_SET", Runs: []string{baseline}}, Candidate: decision.SourceRef{Kind: "RUN_SET", Runs: []string{candidateRun}}, Policy: pr}
		receipt, err := gateStore.CreateGate(ctx, f.s, command)
		must(err)
		if receipt.Decision != want {
			t.Fatalf("quality gate: want=%s got=%s reasons=%v", want, receipt.Decision, receipt.Reasons)
		}
		saved, err := gateStore.GetGateReceipt(ctx, f.s.Scope, command.ID)
		must(err)
		if saved.Decision != want {
			t.Fatal("gate receipt not durable")
		}
		t.Logf("agent quality gate variant=%s decision=%s exit=%d", variant, receipt.Decision, decision.ExitCode(receipt.Decision))
	}
	compare(candidate, "candidate", decision.Pass)
	compare(regression, "regression", decision.Fail)
	compare(unknown, "unknown", decision.Blocked)
	compare(errorRun, "candidate", decision.Blocked)
	// UNKNOWN Attempt 只读 GET 到后续成功证据也保持不可变；每个 Case 仍只有一个 Attempt。
	state, err := f.k.ReadRunState(ctx, f.s.Scope, unknown)
	must(err)
	for _, a := range state.Attempts {
		if a.Outcome != ev.Unknown {
			t.Fatal("lost response must remain UNKNOWN")
		}
		request, err := http.NewRequest(http.MethodGet, live.Configs["unknown"].BaseURL+"/api/runtime/runs/"+a.ID+"/agent-quality-evidence/v1", nil)
		must(err)
		request.Header.Set("Authorization", "Bearer public-quality-test-token")
		response, err := http.DefaultClient.Do(request)
		must(err)
		response.Body.Close()
		if response.StatusCode != 200 {
			t.Fatal("readonly unknown investigation unavailable")
		}
	}
	after, err := f.k.ReadRunState(ctx, f.s.Scope, unknown)
	must(err)
	if len(after.Attempts) != 5 {
		t.Fatal("UNKNOWN produced retries")
	}
}
