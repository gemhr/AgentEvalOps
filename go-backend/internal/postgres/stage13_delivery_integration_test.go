//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/delivery"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/httpapi"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Python Provider seam 仅属于测试，返回真实 Core 持久执行/回执；没有外网模型请求。
func wp06Python(t *testing.T, body any) map[string]asset.JSON {
	t.Helper()
	raw, _ := json.Marshal(body)
	command := exec.Command(`D:\PythonProject\Local_Agent\.venv\Scripts\python.exe`, `.ai/handoff/stage13_wp06/fixture_runtime.py`)
	command.Dir = `D:\PythonProject\Local_Agent`
	command.Env = append(os.Environ(), "PYTHONIOENCODING=utf-8", `PYTHONPATH=D:\PythonProject\Local_Agent`, "LOCAL_AGENT_ENVIRONMENT_PROFILE=TEST")
	command.Stdin = bytes.NewReader(raw)
	var errlog bytes.Buffer
	command.Stderr = &errlog
	out, e := command.Output()
	if e != nil {
		t.Fatalf("controlled Core: %v %s", e, errlog.String())
	}
	var v map[string]asset.JSON
	if json.Unmarshal(out, &v) != nil {
		t.Fatalf("invalid fixture response")
	}
	return v
}

func TestWP06ControlledPassGateAndPersistentDelivery(t *testing.T) {
	f := g6(t)
	ctx := context.Background()
	initial := wp06Python(t, map[string]any{})
	manifest := initial["manifest"]
	input := initial["input"]
	source := asset.Source{Kind: "TEST", Ref: "TEST_SCOPE/CONTROLLED_PASS_FIXTURE", Principal: f.s.Principal}
	readers := g6Readers(f)
	c := asset.Ref{EntityID: asset.NewID(), Version: "CONTROLLED_PASS_FIXTURE"}
	cases := catalog.CaseService{Store: readers.Cases}
	_, e := cases.CreateCase(ctx, f.s.Scope, asset.Create{ID: c.EntityID, Name: "TEST_SCOPE CONTROLLED_PASS_FIXTURE"})
	if e != nil {
		t.Fatal(e)
	}
	body := testCase()
	body.Input = input
	body.Capability = "CONTROLLED_DELIVERY_FIXTURE"
	body.TaskGoal = "验证受控 schema 输出的授权交付"
	body.AcceptanceCriteria = []string{"脚本输出结构有效；不代表当前 Candidate 质量"}
	_, e = cases.PublishCaseVersion(ctx, f.s.Scope, asset.Publish[catalog.CaseContent]{Ref: c, Body: body, Source: source})
	if e != nil {
		t.Fatal(e)
	}
	f.dataset = asset.Ref{EntityID: asset.NewID(), Version: "CONTROLLED_PASS_FIXTURE"}
	datasets := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	_, e = datasets.CreateDataset(ctx, f.s.Scope, asset.Create{ID: f.dataset.EntityID, Name: "TEST_SCOPE CONTROLLED_PASS_FIXTURE"})
	if e != nil {
		t.Fatal(e)
	}
	_, e = datasets.PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: f.dataset, Body: catalog.DatasetContent{Cases: []asset.Ref{c}}, Source: source})
	if e != nil {
		t.Fatal(e)
	}
	f.build(t, f.bindings[:1])
	// 独立脚本 Provider / Core HTTP，仅证明真实 receipt 与正式 Target 绑定。
	calls := 0
	core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var q struct {
			RunID string `json:"run_id"`
			Query string `json:"query"`
		}
		if json.NewDecoder(r.Body).Decode(&q) != nil {
			w.WriteHeader(400)
			return
		}
		p, _ := asset.ParseJSON([]byte(q.Query))
		v := wp06Python(t, map[string]any{"run_id": q.RunID, "input": p})
		calls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(v["response"].Bytes())
	}))
	defer core.Close()
	tokenEnv := "WP06_TEST_CORE_TOKEN"
	t.Setenv(tokenEnv, "test-fixture-credential")
	cfg := provider.Stage13Config{Transport: provider.DefaultLocalAgentConfig(core.URL, tokenEnv), ExpectedSubjectManifest: manifest}
	cfg.Transport.CallMilliseconds = 120000
	target, e := provider.NewStage13Target(cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer target.Close()
	cfgJSON, _ := asset.Freeze(cfg)
	f.cmd.Target = ev.Target{ID: provider.Stage13TargetID, Kind: "LOCALAGENT_HTTP", Version: provider.Stage13TargetVersion, Config: cfgJSON, TimeoutMilliseconds: 120000}
	f.cmd.Subject, _ = asset.Freeze(map[string]string{"subject": "CONTROLLED_PASS_FIXTURE", "subject_version": "TEST_SCOPE", "environment": "TEST_SCOPE", "run_mode": "CONTROLLED_PASS_FIXTURE"})
	run := func() (decision.SourceRef, string) {
		cmd := f.cmd
		cmd.CommandID = asset.NewID()
		r, e := f.k.CreateRun(ctx, f.s, cmd)
		reply(t, r, e, ev.Applied)
		st, e := f.k.ReadRunState(ctx, f.s.Scope, r.ID)
		if e != nil {
			t.Fatal(e)
		}
		a := st.Attempts[0]
		owner := start(t, f.kernelFixture, ev.Owned{RunID: r.ID, AttemptID: a.ID}, time.Minute)
		out, e := target.Execute(ctx, f.s, a.Request)
		if e != nil || out.Kind != ev.Success {
			t.Fatalf("Target %v %+v", e, out)
		}
		result, e := f.k.FinalizeExecutionOutcome(ctx, f.s, ev.FinalizeOutcome{Owned: owner, CommandID: asset.NewID(), Outcome: out})
		reply(t, result, e, ev.Applied)
		st, e = f.k.ReadRunState(ctx, f.s.Scope, r.ID)
		if e != nil {
			t.Fatal(e)
		}
		work := st.Works[0]
		o := ev.Owned{RunID: r.ID, AttemptID: a.ID, WorkID: work.ID}
		claim, e := f.k.ClaimEvaluatorWork(ctx, f.s, o, "TEST_SCOPE", time.Minute)
		reply(t, claim, e, ev.Applied)
		o.Token = claim.Token
		result, e = f.k.BeginEvaluatorAttempt(ctx, f.s, o, asset.NewID(), 1)
		reply(t, result, e, ev.Applied)
		// 现有 G6 imported test authority，冻结规则只验证本 fixture 有效输出。
		var cleanup map[string]asset.JSON
		_ = out.Cleanup.Decode(&cleanup)
		var validation map[string]asset.JSON
		_ = cleanup["validation"].Decode(&validation)
		var valid string
		_ = validation["status"].Decode(&valid)
		if valid != "VALID" {
			t.Fatal("fixture output invalid")
		}
		value := ev.ResultValue{Verdict: "PASS", SourceVerdict: "PASS", Score: floatRef(1), Reason: "TEST_SCOPE CONTROLLED_PASS_FIXTURE schema-valid output", SpecDigest: work.SpecDigest, InputDigest: cmd.Snapshot.Input().Manifest[0].Identity.ContentDigest, Artifact: *out.Artifact, Evidence: out.Evidence}
		value.Provenance, _ = asset.Freeze(map[string]any{"metric": "task_success.v1", "applicability": asset.Applicable, "task_success": map[string]any{"version": "task_success.v1", "decision": "SUCCESS", "method": "IMPORTED", "producer_ref": "G6_CONTROLLED_IMPORT/CONTROLLED_PASS_FIXTURE", "reason": "TEST_SCOPE schema-valid fixture", "evidence_refs": []string{out.Artifact.Ref}}})
		result, e = f.k.FinalizeEvaluationResult(ctx, f.s, ev.FinalizeResult{Owned: o, CommandID: asset.NewID(), ResultID: asset.NewID(), Value: value})
		reply(t, result, e, ev.Applied)
		result, e = f.k.CoordinateRun(ctx, f.s, r.ID)
		reply(t, result, e, ev.Applied)
		return decision.SourceRef{Kind: "RUN_SET", Runs: []string{r.ID}}, a.ID
	}
	base, _ := run()
	candidate, anchor := run()
	policy := f.policy(t, nil)
	command := f.gateCommand(base, candidate, policy)
	gate, e := f.gate.CreateGate(ctx, f.s, command)
	if e != nil || gate.Decision != decision.Pass {
		t.Fatalf("formal fixture gate %v %s %v", e, gate.Decision, gate.Reasons)
	}
	g, o, refs, e := f.gate.Stage13DeliveryBinding(ctx, f.s.Scope, gate.GateID, anchor)
	if e != nil || len(refs) != 1 {
		t.Fatalf("binding %v", e)
	}
	q := delivery.Request{Gate: g, Outcome: o, Destination: delivery.Destination{Kind: delivery.SinkKind, Scope: "TEST_SCOPE", ID: "CONTROLLED_PASS_FIXTURE"}}
	auth, e := f.gate.AuthorizeStage13Delivery(ctx, f.s.Scope, q)
	if e != nil || auth.Status != "AUTHORIZED" {
		t.Fatal(auth, e)
	}
	for _, field := range []string{"gate_receipt_digest", "candidate_subject_digest", "comparison_digest", "snapshot_digest"} {
		t.Run(field, func(t *testing.T) {
			bad := q
			switch field {
			case "gate_receipt_digest":
				bad.Gate.ReceiptDigest = strings.Repeat("f", 64)
			case "candidate_subject_digest":
				bad.Gate.CandidateSubject = strings.Repeat("f", 64)
			case "comparison_digest":
				bad.Gate.ComparisonDigest = strings.Repeat("f", 64)
			case "snapshot_digest":
				bad.Gate.SnapshotDigest = strings.Repeat("f", 64)
			}
			a, e := f.gate.AuthorizeStage13Delivery(ctx, f.s.Scope, bad)
			if e != nil || a.Status != "DENIED" {
				t.Fatal(a, e)
			}
		})
	}
	bad := q
	bad.Gate.GateID = asset.NewID()
	if _, e = f.gate.AuthorizeStage13Delivery(ctx, f.s.Scope, bad); e == nil {
		t.Fatal("unknown gate authorized")
	}
	newGate, e := f.gate.CreateGate(ctx, f.s, f.gateCommand(base, candidate, policy))
	if e != nil {
		t.Fatal(e)
	}
	ng, no, nrefs, e := f.gate.Stage13DeliveryBinding(ctx, f.s.Scope, newGate.GateID, anchor)
	if e != nil {
		t.Fatal(e)
	}
	newAuth := delivery.Project(f.s.ProjectID, newGate.CreatedAt, delivery.Request{Gate: ng, Outcome: no, Destination: q.Destination}, ng, no, nrefs)
	if newAuth.Digest == auth.Digest {
		t.Fatal("new gate reused authorization")
	}
	// 正式项目授权 HTTP handler + 真实独立 LocalAgent endpoint/process。
	pepper := strings.Repeat("wp06-test-pepper-", 3)
	ident := postgres.ProductIdentity{Pool: f.db.pool, Pepper: []byte(pepper)}
	principal := identity.Principal{ID: f.s.Principal, Type: "HUMAN", Capabilities: []identity.Capability{identity.Read, identity.ManageAPIKey}}
	_, key, e := ident.CreateCredential(ctx, identity.Access{Principal: principal, Scope: f.s.Scope}, asset.NewID(), "WP06 fixture consumer", []identity.Capability{identity.Read}, nil)
	if e != nil {
		t.Fatal(e)
	}
	api, e := httpapi.New(httpapi.Server{Pool: f.k.Pool, Identity: ident, Config: httpapi.DefaultConfig(), Log: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if e != nil {
		t.Fatal(e)
	}
	httpServer := httptest.NewServer(api.Handler())
	defer httpServer.Close()
	evidence := os.Getenv("WP06_EVIDENCE_DIR")
	if evidence == "" {
		t.Fatal("WP06_EVIDENCE_DIR required")
	}
	save := func(name string, v any) {
		j, e := asset.Freeze(v)
		if e != nil {
			t.Fatal(e)
		}
		if e = os.WriteFile(filepath.Join(evidence, name+".json"), j.Bytes(), 0600); e != nil {
			t.Fatal(e)
		}
	}
	save("controlled-pass-gate", gate)
	save("controlled-pass-authorization", auth)
	save("fixture-request", q)
	script := exec.Command(`D:\PythonProject\Local_Agent\.venv\Scripts\python.exe`, `.ai/handoff/stage13_wp06/delivery_evidence.py`)
	script.Dir = `D:\PythonProject\Local_Agent`
	script.Env = append(os.Environ(), "PYTHONIOENCODING=utf-8", `PYTHONPATH=D:\PythonProject\Local_Agent`, "LOCAL_AGENT_STAGE13_EVALOPS_URL="+httpServer.URL, "LOCAL_AGENT_STAGE13_EVALOPS_KEY="+key, "LOCAL_AGENT_STAGE13_DELIVERY_PROJECT="+f.s.ProjectID, "LOCAL_AGENT_STAGE13_SCOPE=wp04-test", "WP06_FIXTURE_DATABASE=stage13_wp06_fixture_test")
	raw, _ := asset.Freeze(q)
	script.Stdin = bytes.NewReader(raw.Bytes())
	result, e := script.CombinedOutput()
	if e != nil {
		t.Fatalf("persistent E2E: %v %s", e, result)
	}
	t.Log(string(result))
	save("fixture-authority-provenance", map[string]any{"scope": "TEST_SCOPE", "fixture": "CONTROLLED_PASS_FIXTURE", "construction": "published catalog -> kernel Run/Attempt/Result -> Decisions.CreateGate -> project-authenticated HTTP authorization -> LocalAgent endpoint", "scripted_provider_calls": calls, "real_model_calls": 0, "current_candidate_gate_used": false, "gate_replay_unchanged": gate.GateID})
}
