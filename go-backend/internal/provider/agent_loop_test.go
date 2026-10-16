package provider

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
)

func TestAgentLoopPythonProducerStrictTarget(t *testing.T) {
	root, e := filepath.Abs(filepath.Join("..", "..", "..", "..", "Local_Agent"))
	if e != nil {
		t.Fatal(e)
	}
	command := exec.Command("uv", "run", "python", "tests/fixtures/loop_evidence_public.py")
	command.Dir = root
	raw, e := command.Output()
	if e != nil {
		t.Fatal("真实 Python producer 执行失败", e)
	}
	golden, e := os.ReadFile("testdata/loop_public_golden.json")
	if e != nil {
		t.Fatal(e)
	}
	j, e := asset.ParseJSON(raw)
	if e != nil {
		t.Fatal(e)
	}
	g, e := asset.ParseJSON(golden)
	if e != nil || j.String() != g.String() {
		t.Fatal("Python producer 与公开 golden 漂移")
	}
	var wire agentLoopResponse
	if strict(raw, &wire) != nil {
		t.Fatal("wire")
	}
	correlation := agentquality.LoopCorrelation{"22222222-2222-4222-8222-222222222222", "11111111-1111-4111-8111-111111111111", "33333333-3333-4333-8333-333333333333"}
	evidence, e := agentquality.DecodeLoopEvidence(wire.Loop, correlation.AttemptID, correlation)
	if e != nil {
		t.Fatal(e)
	}
	if verdict, reason := agentquality.LoopIntegrity(evidence); verdict != "PASS" {
		t.Fatal(verdict, reason)
	}
	project := asset.NewID()
	config := AgentLoopConfig{TokenEnv: "TEST_LOOP_TOKEN", TrustedProjectID: project, MaxResponseBytes: 1024 * 1024, CallMilliseconds: 1000, HTTP: DefaultHTTPConfig()}
	t.Setenv(config.TokenEnv, "public-test-token")
	respond := raw
	status := http.StatusOK
	dispatches := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		dispatches++
		if r.URL.Path != "/api/runtime/evaluation-execute/agent-loop/v1" {
			t.Error("protocol endpoint")
		}
		body, _ := io.ReadAll(r.Body)
		var request map[string]asset.JSON
		if strict(body, &request) != nil {
			t.Error("request")
		}
		if request["evaluation_project_id"].String() != `"`+project+`"` {
			t.Error("trusted project")
		}
		w.WriteHeader(status)
		_, _ = w.Write(respond)
	}))
	defer server.Close()
	config.BaseURL = server.URL
	target, e := NewAgentLoopTarget(config, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if e != nil {
		t.Fatal(e)
	}
	defer target.Close()
	configJSON, _ := asset.Freeze(config)
	input, _ := asset.Freeze(map[string]any{"agent_id": "public-loop", "query": "public query"})
	q := ev.Request{ID: correlation.RequestID, RunID: correlation.EvaluationRunID, AttemptID: correlation.AttemptID, Target: ev.Target{ID: AgentLoopTargetID, Version: AgentLoopTargetVersion, Kind: "LOCALAGENT_HTTP", Config: configJSON, TimeoutMilliseconds: 1000}, Case: ev.CaseInput{Identity: ev.AssetIdentity{ProjectID: project}}}
	q.Case.Case.Input = input
	scope := ev.Scope{Scope: asset.Scope{ProjectID: project}}
	out, e := target.Execute(context.Background(), scope, q)
	if e != nil || out.Kind != ev.Success || len(out.Evidence) != 2 || out.Validate(project, q.RunID, q.AttemptID, q.ID) != nil {
		t.Fatalf("success outcome %+v %v", out, e)
	}
	if !LoopMetricDefinitionValid(t) {
		t.Fatal("loop definition unsupported")
	}
	// 缺失必需 Observation 必须不可判定；改变既有唯一绑定必须 FAIL。
	missing := evidence
	missing.Iterations = append([]agentquality.LoopIteration(nil), evidence.Iterations...)
	a := *missing.Iterations[0].Action
	a.ObservationDigest = nil
	missing.Iterations[0].Action = &a
	if v, _ := agentquality.LoopIntegrity(missing); v == "PASS" {
		t.Fatal("missing Observation PASS")
	}
	altered := evidence
	altered.Iterations = append([]agentquality.LoopIteration(nil), evidence.Iterations...)
	b := *altered.Iterations[1].Action
	b.InvocationID = altered.Iterations[0].Action.InvocationID
	altered.Iterations[1].Action = &b
	if v, _ := agentquality.LoopIntegrity(altered); v != "FAIL" {
		t.Fatal("duplicate binding not FAIL")
	}
	for _, bad := range []string{strings.Replace(wire.Loop.String(), `"schema_version":"loop-evidence.v1"`, `"schema_version":"loop-evidence.v99"`, 1), strings.Replace(wire.Loop.String(), `"sealed":true`, `"sealed":true,"extra":1`, 1), strings.Replace(wire.Loop.String(), `"observation_cursor":1,`, "", 1)} {
		invalid, _ := asset.ParseJSON([]byte(bad))
		if _, e := agentquality.DecodeLoopEvidence(invalid, correlation.AttemptID, correlation); e == nil {
			t.Fatal("strict decoder accepted malformed evidence")
		}
	}
	status = http.StatusAccepted
	respond = []byte(`{"protocol_version":"` + AgentLoopProtocol + `","run_id":"` + q.AttemptID + `","status":"ADMITTED","capture_status":"NOT_READY"}`)
	before := dispatches
	out, e = target.Execute(context.Background(), scope, q)
	if e != nil || out.Kind != ev.Unknown || dispatches != before+1 || out.Artifact != nil {
		t.Fatal("202 must UNKNOWN without redispatch")
	}
}

func LoopMetricDefinitionValid(t *testing.T) bool {
	t.Helper()
	return agentquality.LoopSupported(agentquality.LoopEvaluatorDefinition(asset.Ref{EntityID: asset.NewID(), Version: "v1"}))
}
