//go:build integration

package postgres_test

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
)

func TestCISyntheticKernelAttemptAuthorization(t *testing.T) {
	// 原 fixture 会创建临时 DB/role；未获资源授权时绝不进入 fixture。
	if os.Getenv("CI_SYNTHETIC_KERNEL_PG_TEST_APPROVED") != "1" || os.Getenv("G1_TEST_POSTGRES_URL") == "" {
		t.Skip("requires approved isolated G1 PostgreSQL fixture resources; no resources created")
	}
	f := fixture(t)
	ctx := context.Background()
	must := func(e error) {
		t.Helper()
		if e != nil {
			t.Fatal(e)
		}
	}
	seen := make(chan string, 1)
	// 明确无远程模型：只证明正式 Worker 到达真实 HTTP Target，202 保持 UNKNOWN。
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var envelope struct {
			Quality struct {
				Run string `json:"run_id"`
			} `json:"quality"`
		}
		if json.NewDecoder(r.Body).Decode(&envelope) != nil {
			t.Error("request envelope")
		}
		if r.Header.Get("Authorization") != "Bearer kernel-test-token" {
			t.Error("credential")
		}
		select {
		case seen <- envelope.Quality.Run:
		default:
			t.Error("unexpected repeat dispatch")
		}
		w.WriteHeader(202)
	}))
	defer server.Close()
	t.Setenv("CI_KERNEL_TEST_TOKEN", "kernel-test-token")
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: f.k.Pool}, Datasets: postgres.Datasets{Pool: f.k.Pool}, Suites: postgres.Suites{Pool: f.k.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: f.k.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: f.k.Pool}}
	cs := catalog.CaseService{Store: readers.Cases}
	ds := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	cr, dr := asset.Ref{EntityID: asset.NewID(), Version: "1"}, asset.Ref{EntityID: asset.NewID(), Version: "1"}
	digest := strings.Repeat("a", 64)
	input, _ := asset.Freeze(provider.CISyntheticCaseInput{Template: "report_pipeline_failure", TemplateVersion: "2", InputDigest: digest})
	body := testCase()
	body.Input = input
	source := asset.Source{Kind: "TEST", Ref: "ci-kernel-dispatch-fixture", Principal: f.s.Principal}
	_, e := cs.CreateCase(ctx, f.s.Scope, asset.Create{ID: cr.EntityID, Name: "synthetic dispatch fixture"})
	must(e)
	_, e = cs.PublishCaseVersion(ctx, f.s.Scope, asset.Publish[catalog.CaseContent]{Ref: cr, Body: body, Source: source})
	must(e)
	_, e = ds.CreateDataset(ctx, f.s.Scope, asset.Create{ID: dr.EntityID, Name: "single synthetic dispatch fixture"})
	must(e)
	_, e = ds.PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: dr, Body: catalog.DatasetContent{Cases: []asset.Ref{cr}}, Source: source})
	must(e)
	bindings := []catalog.EvaluatorBinding{}
	for _, spec := range f.cmd.Snapshot.Input().Evaluators {
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: spec.Identity.Ref, Metrics: spec.Metrics, Required: spec.Required, Applicability: spec.Applicability})
	}
	snapshot, e := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &dr, Evaluators: bindings})
	must(e)
	identity := snapshot.Input().Manifest[0].Identity
	manifest, _ := asset.Freeze(aq.QualityManifest{Schema: "agent-quality-execution-manifest.v1", Agents: map[string]aq.QualityAgentPin{"ci_incident_investigator": {Version: "1", Policy: "durable-agent-loop.v2"}}})
	profile, _ := asset.Freeze(aq.QualityProfile{ID: "synthetic", Version: 1, Policy: "agent-quality-capture.v1", Aliases: map[string]string{}, ParameterPaths: map[string][]string{}, FactKeys: []string{}})
	c := provider.CISyntheticConfig{Quality: provider.AgentQualityConfig{BaseURL: server.URL, TokenEnv: "CI_KERNEL_TEST_TOKEN", TrustedProjectID: f.s.ProjectID, MaxResponseBytes: 8192, CallMilliseconds: 1000, HTTP: provider.DefaultHTTPConfig(), Manifest: manifest, Profile: profile}, Frozen: provider.FrozenCI{CaseID: cr.EntityID, CaseVersion: cr.Version, CaseDigest: identity.ContentDigest, Template: "report_pipeline_failure", TemplateVersion: "2", InputDigest: digest}, Mode: "CANDIDATE", Manifest: digest, Prompt: digest, Candidate: "ci-prompt-" + digest}
	config, _ := asset.Freeze(c)
	cmd := f.cmd
	cmd.Snapshot = snapshot
	cmd.Target = ev.Target{ID: provider.CISyntheticTargetID, Kind: "LOCALAGENT_HTTP", Version: provider.CISyntheticTargetVersion, Config: config, TimeoutMilliseconds: 1000}
	created, e := f.k.CreateRun(ctx, f.s, cmd)
	reply(t, created, e, ev.Applied)
	state, e := f.k.ReadRunState(ctx, f.s.Scope, created.ID)
	must(e)
	if len(state.Attempts) != 1 || state.Attempts[0].Status != "PENDING" || !asset.ValidID(state.Attempts[0].ID) {
		t.Fatal("real pending attempt missing")
	}
	attempt := state.Attempts[0]
	var before, after []byte
	must(f.db.pool.QueryRow(ctx, "SELECT kernel_bytes FROM evaluation_runs WHERE id=$1", created.ID).Scan(&before))
	// 同 command 改 Target 由原 Kernel 幂等一致性拒绝，绝不改写冻结数据。
	changed := cmd
	c.Mode = "BASELINE"
	changed.Target.Config, _ = asset.Freeze(c)
	conflict, e := f.k.CreateRun(ctx, f.s, changed)
	reply(t, conflict, e, ev.Conflict)
	c.Mode = "CANDIDATE"
	frozenTarget, _ := asset.Freeze(state.Run.Snapshot.Target)
	t.Logf("Kernel pending: run=%s attempt=%s target_digest=%s conflict=%s", created.ID, attempt.ID, frozenTarget.Digest(), conflict.Code)
	a := provider.CISyntheticAttemptAuthorization{Schema: provider.CISyntheticAuthorizationVersion, Attempt: attempt.ID, Run: created.ID, Project: f.s.ProjectID, Case: cr.EntityID, TargetDigest: frozenTarget.Digest(), Principal: asset.NewID(), Tenant: "kernel-test", CredentialDigest: fmt.Sprintf("%x", sha256.Sum256([]byte("kernel-test-token"))), Scopes: []string{"localagent:evaluation:execute", "localagent:ci-synthetic:evaluate"}}
	target, e := provider.NewCISyntheticTarget(c, a)
	must(e)
	defer target.Close()
	wc := worker.DefaultConfig()
	wc.ExecutionConcurrency = 1
	wc.EvaluatorConcurrency = 0
	wc.PollInterval = 10 * time.Millisecond
	wc.MaxBackoff = 50 * time.Millisecond
	wc.Reconcile = false
	wc.Coordinate = false
	runtime := &worker.Runtime{Config: wc, Backend: f.k, Target: target, Epoch: f.s.Epoch, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	running, cancel := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- runtime.Run(running) }()
	defer func() {
		cancel()
		select {
		case e := <-done:
			must(e)
		case <-time.After(5 * time.Second):
			t.Error("worker did not stop")
		}
	}()
	select {
	case root := <-seen:
		if root != attempt.ID {
			t.Fatal("worker did not dispatch Kernel attempt")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no worker dispatch")
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		state, e = f.k.ReadRunState(ctx, f.s.Scope, created.ID)
		must(e)
		if state.Attempts[0].Status == "TERMINAL" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("attempt not finalized")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if state.Attempts[0].Outcome != ev.Unknown || runtime.Counters.Claims.Load() != 1 {
		t.Fatal("Pending fixture not UNKNOWN or duplicate claim")
	}
	must(f.db.pool.QueryRow(ctx, "SELECT kernel_bytes FROM evaluation_runs WHERE id=$1", created.ID).Scan(&after))
	if string(before) != string(after) {
		t.Fatal("frozen snapshot changed")
	}
	t.Logf("Worker evidence: attempt=%s claims=%d outcome=%s kernel_before=%x kernel_after=%x", attempt.ID, runtime.Counters.Claims.Load(), state.Attempts[0].Outcome, sha256.Sum256(before), sha256.Sum256(after))
}
