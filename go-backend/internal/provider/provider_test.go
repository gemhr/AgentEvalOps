package provider

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
)

func response(run string) map[string]any {
	return map[string]any{"protocol_version": LocalAgentProtocol, "run_id": run, "status": "SUCCEEDED", "stop_reason": "COMPLETED", "error_code": nil, "safe_message": nil, "capture_status": "COMPLETE", "capture_error_code": nil, "rag_evaluation_artifacts": []any{}, "final_answer_capture_status": "COMPLETE", "final_answer_capture_error_code": nil, "final_answer_evidence": map[string]any{"schema_version": "final-answer-evidence.v1", "evidence_id": "final-answer://" + run, "run_id": run, "attempt_id": run, "media_type": "text/plain; charset=utf-8", "content_sha256": fmt.Sprintf("%x", sha256.Sum256([]byte("answer"))), "content": "answer"}}
}
func TestLocalAgentClassificationAndSinglePOST(t *testing.T) {
	t.Setenv("G4_TEST_AGENT_TOKEN", "controlled-test-token")
	for _, mode := range []string{"success", "failure", "deadline", "cancelled", "read_timeout", "local_cancel", "partial_write", "reset", "500", "400", "redirect", "malformed", "schema", "identity", "attempt_identity", "artifact_digest", "missing_artifact", "oversized", "cancel_accepted", "missing_required", "answer_unavailable"} {
		t.Run(mode, func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Header.Get("Idempotency-Key") != "" || r.Header.Get("X-Idempotency-Key") != "" {
					t.Error("unsupported idempotency transmission")
				}
				if mode == "partial_write" {
					buf := make([]byte, 32)
					_, _ = r.Body.Read(buf)
					c, _, _ := w.(http.Hijacker).Hijack()
					_ = c.Close()
					return
				}
				var q map[string]any
				_ = json.NewDecoder(r.Body).Decode(&q)
				wire := response(q["run_id"].(string))
				switch mode {
				case "failure", "deadline", "cancelled":
					wire["status"] = "FAILED"
					wire["stop_reason"] = "UNHANDLED_ERROR"
					wire["error_code"] = "test"
					wire["final_answer_capture_status"] = "FAILED"
					wire["final_answer_capture_error_code"] = "unavailable"
					wire["final_answer_evidence"] = nil
					if mode == "deadline" {
						wire["stop_reason"] = "DEADLINE_EXCEEDED"
					}
					if mode == "cancelled" {
						wire["status"] = "CANCELLED"
						wire["stop_reason"] = "USER_CANCELLED"
					}
				case "read_timeout", "local_cancel":
					select {
					case <-r.Context().Done():
					case <-time.After(250 * time.Millisecond):
					}
					return
				case "reset":
					c, _, _ := w.(http.Hijacker).Hijack()
					_ = c.Close()
					return
				case "500":
					w.WriteHeader(500)
					return
				case "400":
					w.WriteHeader(400)
					return
				case "redirect":
					w.Header().Set("Location", "/execute-again")
					w.WriteHeader(307)
					return
				case "malformed":
					_, _ = io.WriteString(w, "{bad}")
					return
				case "schema":
					wire["protocol_version"] = "unknown"
				case "identity":
					wire["run_id"] = asset.NewID()
				case "attempt_identity":
					wire["final_answer_evidence"].(map[string]any)["attempt_id"] = asset.NewID()
				case "artifact_digest":
					wire["final_answer_evidence"].(map[string]any)["content_sha256"] = strings.Repeat("0", 64)
				case "missing_artifact":
					wire["final_answer_evidence"] = nil
				case "missing_required":
					delete(wire, "error_code")
				case "answer_unavailable":
					wire["final_answer_capture_status"] = "FAILED"
					wire["final_answer_capture_error_code"] = "unavailable"
					wire["final_answer_evidence"] = nil
				case "oversized":
					_, _ = io.WriteString(w, strings.Repeat("a", 5000))
					return
				case "cancel_accepted":
					w.WriteHeader(202)
					_, _ = io.WriteString(w, `{"status":"cancelled"}`)
					return
				}
				_ = json.NewEncoder(w).Encode(wire)
			}))
			defer server.Close()
			cfg := LocalAgentConfig{BaseURL: server.URL, TokenEnv: "G4_TEST_AGENT_TOKEN", MaxResponseBytes: 4096, CallMilliseconds: 1000, HTTP: DefaultHTTPConfig()}
			if mode == "read_timeout" {
				cfg.CallMilliseconds = 40
			}
			log := slog.New(slog.NewTextHandler(io.Discard, nil))
			target, e := NewLocalAgentHttpExecutionTarget(cfg, log)
			if e != nil {
				t.Fatal(e)
			}
			defer target.Close()
			p := asset.NewID()
			config, _ := asset.Freeze(cfg)
			query := "question"
			if mode == "partial_write" {
				query = strings.Repeat("x", 4*1024*1024)
			}
			input, _ := asset.Freeze(map[string]string{"agent_id": "test", "query": query})
			q := ev.Request{ID: asset.NewID(), RunID: asset.NewID(), AttemptID: asset.NewID(), IdempotencyKey: asset.NewID(), Target: ev.Target{ID: LocalAgentTargetID, Kind: "LOCALAGENT_HTTP", Version: LocalAgentVersion, Config: config, TimeoutMilliseconds: 1000}, Case: ev.CaseInput{Identity: ev.AssetIdentity{ProjectID: p}}}
			q.Case.Case.Input = input
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if mode == "local_cancel" {
				var stop context.CancelFunc
				ctx, stop = context.WithTimeout(ctx, 40*time.Millisecond)
				defer stop()
			}
			got, e := target.Execute(ctx, ev.Scope{Scope: asset.Scope{ProjectID: p}}, q)
			if e != nil {
				t.Fatal(e)
			}
			want := ev.Unknown
			switch mode {
			case "success", "answer_unavailable":
				want = ev.Success
			case "failure":
				want = ev.Failure
			case "deadline":
				want = ev.Timeout
			case "cancelled":
				want = ev.Cancelled
			}
			if got.Kind != want || calls.Load() != 1 {
				t.Fatalf("got %s/%s %s calls=%d want=%s", got.Kind, got.DispatchCertainty, got.ErrorCategory, calls.Load(), want)
			}
			if got.Validate(p, q.RunID, q.AttemptID, q.ID) != nil {
				t.Fatal("invalid typed observation")
			}
			if got.Kind == ev.Unknown && (got.Artifact != nil || got.TerminalCertainty != "UNCONFIRMED") {
				t.Fatal("unconfirmed artifact accepted")
			}
			if want != ev.Unknown && got.TerminalCertainty != "REMOTE_CONFIRMED" {
				t.Fatal("terminal not confirmed")
			}
		})
	}
	listener, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	address := listener.Addr().String()
	_ = listener.Close()
	cfg := LocalAgentConfig{BaseURL: "http://" + address, TokenEnv: "G4_TEST_AGENT_TOKEN", MaxResponseBytes: 4096, CallMilliseconds: 300, HTTP: DefaultHTTPConfig()}
	target, _ := NewLocalAgentHttpExecutionTarget(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	defer target.Close()
	config, _ := asset.Freeze(cfg)
	input, _ := asset.ParseJSON([]byte(`{"agent_id":"test","query":"q"}`))
	q := ev.Request{ID: asset.NewID(), AttemptID: asset.NewID(), RunID: asset.NewID(), Target: ev.Target{ID: LocalAgentTargetID, Kind: "LOCALAGENT_HTTP", Version: LocalAgentVersion, Config: config, TimeoutMilliseconds: 300}}
	q.Case.Identity.ProjectID = "p"
	q.Case.Case.Input = input
	got, _ := target.Execute(context.Background(), ev.Scope{Scope: asset.Scope{ProjectID: "p"}}, q)
	if got.Kind != ev.Failure || got.DispatchCertainty != "NOT_DISPATCHED" {
		t.Fatalf("refusal %+v", got)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	got, _ = target.Execute(ctx, ev.Scope{Scope: asset.Scope{ProjectID: "p"}}, q)
	if got.Kind != ev.Failure || got.TerminalCertainty != "NOT_DISPATCHED" {
		t.Fatal("preflight context")
	}
}
func TestJudgeStrictOutputAndRetryAfter(t *testing.T) {
	valid := `{"schema_version":"agent-quality-judge.v1","verdict":"PASS","score":1,"reason":"受控判断","evidence_refs":["artifact"],"confidence":0.9,"task_success":"SUCCESS"}`
	if _, e := ParseJudgeOutput([]byte(valid), "task_success.v1", []string{"artifact"}); e != nil {
		t.Fatal(e)
	}
	for _, bad := range []string{"PASS", strings.Replace(valid, `"verdict":"PASS"`, `"verdict":"PASS","extra":true`, 1), strings.Replace(valid, `"score":1,`, "", 1), strings.Replace(valid, `"score":1`, `"score":2`, 1), strings.Replace(valid, `"SUCCESS"`, `"FAILURE"`, 1), strings.Replace(valid, `["artifact"]`, `["wrong"]`, 1), strings.Replace(valid, `"score":1`, `"score":1,"score":0`, 1)} {
		if _, e := ParseJudgeOutput([]byte(bad), "task_success.v1", []string{"artifact"}); e == nil {
			t.Fatal("invalid structured output accepted")
		}
	}
	if retryAfter("2") != 2*time.Second || retryAfter("invalid") != 0 || retryAfter(time.Now().Add(5*time.Second).UTC().Format(http.TimeFormat)) <= 0 {
		t.Fatal("Retry-After parsing")
	}
}
