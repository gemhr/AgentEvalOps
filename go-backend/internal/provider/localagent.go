package provider

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptrace"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
)

const LocalAgentTargetID = "localagent-coordinated-http"
const LocalAgentVersion = "evaluation-v2"
const LocalAgentProtocol = "localagent-evaluation-execute.v2"

type LocalAgentConfig struct {
	BaseURL          string     `json:"base_url"`
	TokenEnv         string     `json:"token_env"`
	MaxResponseBytes int        `json:"max_response_bytes"`
	CallMilliseconds int64      `json:"call_milliseconds"`
	HTTP             HTTPConfig `json:"http"`
}

func (c LocalAgentConfig) Validate() error {
	if endpoint(c.BaseURL) != nil || strings.TrimSpace(c.TokenEnv) == "" || c.MaxResponseBytes < 1 || c.MaxResponseBytes > 16*1024*1024 || c.CallMilliseconds < 1 || c.CallMilliseconds > 3600000 {
		return asset.ErrInvalid
	}
	return c.HTTP.Validate()
}
func DefaultLocalAgentConfig(baseURL, tokenEnv string) LocalAgentConfig {
	return LocalAgentConfig{BaseURL: baseURL, TokenEnv: tokenEnv, MaxResponseBytes: 4 * 1024 * 1024, CallMilliseconds: 65000, HTTP: DefaultHTTPConfig()}
}

type LocalAgentHttpExecutionTarget struct {
	config LocalAgentConfig
	client *http.Client
	log    *slog.Logger
}

func NewLocalAgentHttpExecutionTarget(c LocalAgentConfig, log *slog.Logger) (*LocalAgentHttpExecutionTarget, error) {
	if e := c.Validate(); e != nil {
		return nil, e
	}
	client, e := newClient(c.HTTP)
	if e != nil {
		return nil, e
	}
	if log == nil {
		log = slog.Default()
	}
	return &LocalAgentHttpExecutionTarget{c, client, log}, nil
}
func (t *LocalAgentHttpExecutionTarget) Close() { t.client.CloseIdleConnections() }

type finalAnswer struct {
	Schema    string  `json:"schema_version"`
	ID        string  `json:"evidence_id"`
	RunID     string  `json:"run_id"`
	AttemptID string  `json:"attempt_id"`
	Media     string  `json:"media_type"`
	Digest    string  `json:"content_sha256"`
	Content   *string `json:"content"`
}
type localResponse struct {
	Protocol      string       `json:"protocol_version"`
	RunID         string       `json:"run_id"`
	Status        string       `json:"status"`
	Stop          string       `json:"stop_reason"`
	Error         *string      `json:"error_code"`
	Message       *string      `json:"safe_message"`
	Capture       string       `json:"capture_status"`
	CaptureError  *string      `json:"capture_error_code"`
	RAG           []asset.JSON `json:"rag_evaluation_artifacts"`
	AnswerCapture string       `json:"final_answer_capture_status"`
	AnswerError   *string      `json:"final_answer_capture_error_code"`
	Answer        asset.JSON   `json:"final_answer_evidence"`
}

func (t *LocalAgentHttpExecutionTarget) Execute(parent context.Context, s ev.Scope, q ev.Request) (out ev.Outcome, err error) {
	started := time.Now().UTC()
	status := 0
	actualRemote, remoteStatus, stopReason, captureStatus, answerCapture, responseDigest := "", "", "", "", "", ""
	out = ev.Outcome{Kind: ev.Failure, RequestID: q.ID, RemoteID: q.AttemptID, Protocol: LocalAgentProtocol, Source: "LOCALAGENT_HTTP", DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: string(ProtocolError), Reason: "本地请求校验失败"}
	defer func() {
		out.Cleanup, _ = asset.Freeze(map[string]any{"cancellation": "UNSUPPORTED_TERMINAL_RECEIPT", "run_id": q.RunID, "attempt_id": q.AttemptID, "request_id": q.ID, "idempotency_key": q.IdempotencyKey, "expected_remote_run_id": q.AttemptID, "observed_remote_run_id": nullable(actualRemote), "http_status": status, "response_sha256": nullable(responseDigest), "remote_status": nullable(remoteStatus), "stop_reason": nullable(stopReason), "rag_capture_status": nullable(captureStatus), "final_answer_capture_status": nullable(answerCapture), "target_version": q.Target.Version, "local_started_at": started, "local_finished_at": time.Now().UTC()})
		t.log.Info("target_response_classified", "event", "target_response_classified", "run_id", q.RunID, "attempt_id", q.AttemptID, "outcome", out.Kind, "category", out.ErrorCategory)
		if out.Kind == ev.Unknown {
			t.log.Info("target_unknown", "event", "target_unknown", "run_id", q.RunID, "attempt_id", q.AttemptID)
		}
	}()
	var input struct {
		Agent string  `json:"agent_id"`
		Query *string `json:"query"`
	}
	var config LocalAgentConfig
	if q.Target.ID != LocalAgentTargetID || q.Target.Kind != "LOCALAGENT_HTTP" || q.Target.Version != LocalAgentVersion || q.Target.TimeoutMilliseconds < 1 || q.Target.TimeoutMilliseconds > 3600000 || !asset.ValidID(q.AttemptID) || !asset.ValidID(q.RunID) || !asset.ValidID(q.ID) || s.ProjectID != q.Case.Identity.ProjectID || q.Case.Case.Input.Decode(&input) != nil || input.Agent == "" || input.Query == nil || q.Target.Config.Decode(&config) != nil || config != t.config {
		return out, nil
	}
	token := os.Getenv(config.TokenEnv)
	if strings.TrimSpace(token) == "" || strings.ContainsAny(token, "\r\n") {
		out.ErrorCategory = string(TransportError)
		out.Reason = "缺少有效 service credential"
		return out, nil
	}
	if parent.Err() != nil {
		out.ErrorCategory = string(TransportError)
		out.Reason = "调用前 context 已结束"
		return out, nil
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(config.CallMilliseconds)*time.Millisecond)
	defer cancel()
	body, e := asset.Freeze(map[string]any{"agent_id": input.Agent, "query": *input.Query, "run_id": q.AttemptID, "timeout_seconds": float64(q.Target.TimeoutMilliseconds) / 1000})
	if e != nil {
		return out, nil
	}
	var gotConn, connectFailed atomic.Bool
	trace := &httptrace.ClientTrace{GotConn: func(httptrace.GotConnInfo) { gotConn.Store(true) }, ConnectDone: func(_, _ string, e error) {
		if e != nil {
			connectFailed.Store(true)
		}
	}}
	req, e := http.NewRequestWithContext(httptrace.WithClientTrace(ctx, trace), http.MethodPost, strings.TrimRight(config.BaseURL, "/")+"/api/runtime/evaluation-execute/v2", nil)
	if e != nil {
		return out, nil
	}
	post(req, body.Bytes())
	req.Header.Set("Authorization", "Bearer "+token)
	t.log.Info("target_request_started", "event", "target_request_started", "run_id", q.RunID, "attempt_id", q.AttemptID, "request_id", q.ID, "target_version", q.Target.Version)
	resp, e := t.client.Do(req)
	out.Kind = ev.Unknown
	out.DispatchCertainty = "MAY_HAVE_DISPATCHED"
	out.TerminalCertainty = "UNCONFIRMED"
	out.ErrorCategory = string(TransportError)
	out.Reason = "本地传输未确认远端终态"
	if e != nil {
		if !gotConn.Load() && connectFailed.Load() {
			out.Kind = ev.Failure
			out.DispatchCertainty = "NOT_DISPATCHED"
			out.TerminalCertainty = "NOT_DISPATCHED"
			out.Reason = "连接阶段失败，未取得可 dispatch 连接"
		}
		return out, nil
	}
	defer resp.Body.Close()
	status = resp.StatusCode
	raw, e := boundedBody(resp.Body, config.MaxResponseBytes)
	if e != nil {
		if p, ok := e.(*Error); ok {
			out.ErrorCategory = string(p.Kind)
			out.Reason = "远端响应超过协议大小上限"
		}
		return out, nil
	}
	responseDigest = fmt.Sprintf("%x", sha256.Sum256(raw))
	if resp.StatusCode != http.StatusOK {
		out.ErrorCategory = string(ProtocolError)
		out.Reason = "HTTP 状态不提供可信 terminal/admission receipt"
		return out, nil
	}
	out.ErrorCategory = string(ProtocolError)
	out.Reason = "远端响应协议校验失败"
	if required(raw, "protocol_version", "run_id", "status", "stop_reason", "error_code", "safe_message", "capture_status", "capture_error_code", "rag_evaluation_artifacts", "final_answer_capture_status", "final_answer_capture_error_code", "final_answer_evidence") != nil {
		return out, nil
	}
	var wire localResponse
	if strict(raw, &wire) != nil {
		return out, nil
	}
	actualRemote = safeIdentity(wire.RunID)
	if wire.Protocol != LocalAgentProtocol {
		out.ErrorCategory = string(SchemaMismatch)
		return out, nil
	}
	if wire.RunID != q.AttemptID {
		out.ErrorCategory = string(IdentityMismatch)
		return out, nil
	}
	if !contains(wire.Stop, "COMPLETED", "UNHANDLED_ERROR", "DEADLINE_EXCEEDED", "USER_CANCELLED", "CLIENT_DISCONNECTED", "SYSTEM_SHUTDOWN", "MAX_STEPS_REACHED", "NO_ACTION", "REPEATED_ACTION", "BUDGET_EXHAUSTED", "PLANNING_FAILED") || !contains(wire.Capture, "COMPLETE", "PARTIAL", "FAILED") || wire.RAG == nil {
		return out, nil
	}
	evidence := []ev.Binding{}
	seen := map[string]bool{}
	for _, j := range wire.RAG {
		r, parseErr := agentquality.DecodeRAG(j, q.AttemptID)
		if parseErr != nil || seen[r.ID] {
			return out, nil
		}
		seen[r.ID] = true
		evidence = append(evidence, binding(s, q, r.ID, r.Schema, j))
	}
	var answer *finalAnswer
	if wire.Status == "SUCCEEDED" && wire.AnswerCapture == "COMPLETE" {
		var a finalAnswer
		if wire.AnswerError != nil || wire.Answer.Decode(&a) != nil || a.Content == nil || a.Schema != "final-answer-evidence.v1" || a.Media != "text/plain; charset=utf-8" || len(*a.Content) > 65536 {
			return out, nil
		}
		if a.RunID != q.AttemptID || a.AttemptID != q.AttemptID || a.ID != "final-answer://"+q.AttemptID {
			out.ErrorCategory = string(IdentityMismatch)
			return out, nil
		}
		if a.Digest != fmt.Sprintf("%x", sha256.Sum256([]byte(*a.Content))) {
			return out, nil
		}
		answer = &a
		evidence = append(evidence, binding(s, q, a.ID, a.Schema, wire.Answer))
	} else if wire.AnswerCapture != "FAILED" || wire.AnswerError == nil || wire.Answer.String() != "null" {
		return out, nil
	}
	kind := ev.Unknown
	switch wire.Status {
	case "SUCCEEDED":
		if wire.Stop != "COMPLETED" || wire.Error != nil {
			return out, nil
		}
		kind = ev.Success
	case "FAILED":
		if contains(wire.Stop, "COMPLETED", "USER_CANCELLED", "CLIENT_DISCONNECTED", "SYSTEM_SHUTDOWN") {
			return out, nil
		}
		kind = ev.Failure
		if wire.Stop == "DEADLINE_EXCEEDED" {
			kind = ev.Timeout
		}
	case "CANCELLED":
		if !contains(wire.Stop, "USER_CANCELLED", "CLIENT_DISCONNECTED", "SYSTEM_SHUTDOWN") {
			return out, nil
		}
		kind = ev.Cancelled
	default:
		return out, nil
	}
	out.Kind = kind
	remoteStatus = wire.Status
	stopReason = wire.Stop
	captureStatus = wire.Capture
	answerCapture = wire.AnswerCapture
	out.DispatchCertainty = "REMOTE_ACCEPTED"
	out.TerminalCertainty = "REMOTE_CONFIRMED"
	out.Evidence = evidence
	if kind == ev.Success {
		out.ErrorCategory = ""
		out.Reason = "远端确认执行成功"
		b := binding(s, q, "localagent-run://"+q.AttemptID, "artifact.v1", asset.JSON{})
		b.Availability = "UNAVAILABLE"
		if answer != nil {
			j, _ := asset.Freeze(*answer.Content)
			b = binding(s, q, answer.ID, "artifact.v1", j)
		}
		out.Artifact = &b
	} else {
		out.ErrorCategory = string(RemoteTerminal)
		out.Reason = "远端 terminal: " + wire.Stop
	}
	return out, nil
}
func contains(v string, all ...string) bool {
	for _, x := range all {
		if v == x {
			return true
		}
	}
	return false
}
func binding(s ev.Scope, q ev.Request, ref, schema string, j asset.JSON) ev.Binding {
	return ev.Binding{ProjectID: s.ProjectID, RunID: q.RunID, AttemptID: q.AttemptID, RequestID: q.ID, Ref: ref, Schema: schema, Digest: j.Digest(), Availability: "AVAILABLE", Body: j}
}
