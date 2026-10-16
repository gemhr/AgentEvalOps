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

const AgentLoopTargetID = "localagent-agent-loop-http"
const AgentLoopTargetVersion = "agent-loop-evaluation.v1"
const AgentLoopProtocol = "localagent-agent-loop-evaluation-execute.v1"

type AgentLoopConfig struct {
	BaseURL          string     `json:"base_url"`
	TokenEnv         string     `json:"token_env"`
	TrustedProjectID string     `json:"trusted_project_id"`
	MaxResponseBytes int        `json:"max_response_bytes"`
	CallMilliseconds int64      `json:"call_milliseconds"`
	HTTP             HTTPConfig `json:"http"`
}

func (c AgentLoopConfig) Validate() error {
	if !asset.ValidID(c.TrustedProjectID) {
		return asset.ErrInvalid
	}
	return (LocalAgentConfig{BaseURL: c.BaseURL, TokenEnv: c.TokenEnv, MaxResponseBytes: c.MaxResponseBytes, CallMilliseconds: c.CallMilliseconds, HTTP: c.HTTP}).Validate()
}

type AgentLoopTarget struct {
	config AgentLoopConfig
	client *http.Client
	log    *slog.Logger
}

func NewAgentLoopTarget(c AgentLoopConfig, log *slog.Logger) (*AgentLoopTarget, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	client, err := newClient(c.HTTP)
	if err != nil {
		return nil, err
	}
	if log == nil {
		log = slog.Default()
	}
	return &AgentLoopTarget{c, client, log}, nil
}
func (t *AgentLoopTarget) Close() { t.client.CloseIdleConnections() }

type agentLoopResponse struct {
	Protocol      string     `json:"protocol_version"`
	RunID         string     `json:"run_id"`
	Status        string     `json:"status"`
	Stop          string     `json:"stop_reason"`
	Error         *string    `json:"error_code"`
	Message       *string    `json:"safe_message"`
	Capture       string     `json:"capture_status"`
	CaptureError  *string    `json:"capture_error_code"`
	Loop          asset.JSON `json:"loop_evidence"`
	LoopDigest    string     `json:"loop_evidence_digest"`
	AnswerCapture string     `json:"final_answer_capture_status"`
	AnswerError   *string    `json:"final_answer_capture_error_code"`
	Answer        asset.JSON `json:"final_answer_evidence"`
}

func (t *AgentLoopTarget) Execute(parent context.Context, s ev.Scope, q ev.Request) (out ev.Outcome, err error) {
	out = ev.Outcome{Kind: ev.Failure, RequestID: q.ID, RemoteID: q.AttemptID, Protocol: AgentLoopProtocol, Source: "LOCALAGENT_HTTP", DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: string(ProtocolError), Reason: "Loop 请求校验失败"}
	var config AgentLoopConfig
	var input struct {
		Agent string  `json:"agent_id"`
		Query *string `json:"query"`
	}
	if q.Target.ID != AgentLoopTargetID || q.Target.Version != AgentLoopTargetVersion || q.Target.Kind != "LOCALAGENT_HTTP" || q.Target.TimeoutMilliseconds < 1 || q.Target.TimeoutMilliseconds > 300000 ||
		!asset.ValidID(q.RunID) || !asset.ValidID(q.AttemptID) || !asset.ValidID(q.ID) || s.ProjectID != t.config.TrustedProjectID || s.ProjectID != q.Case.Identity.ProjectID ||
		q.Case.Case.Input.Decode(&input) != nil || !asset.Text(input.Agent) || input.Query == nil || q.Target.Config.Decode(&config) != nil || config != t.config {
		return out, nil
	}
	token := os.Getenv(config.TokenEnv)
	if strings.TrimSpace(token) == "" || strings.ContainsAny(token, "\r\n") || parent.Err() != nil {
		out.ErrorCategory = string(TransportError)
		out.Reason = "Loop service credential 或 context 不可用"
		return out, nil
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(config.CallMilliseconds)*time.Millisecond)
	defer cancel()
	body, _ := asset.Freeze(map[string]any{"agent_id": input.Agent, "query": *input.Query, "run_id": q.AttemptID, "evaluation_project_id": s.ProjectID, "evaluation_run_id": q.RunID, "attempt_id": q.AttemptID, "request_id": q.ID, "timeout_seconds": float64(q.Target.TimeoutMilliseconds) / 1000})
	var connected, connectFailed atomic.Bool
	trace := &httptrace.ClientTrace{GotConn: func(httptrace.GotConnInfo) { connected.Store(true) }, ConnectDone: func(_, _ string, err error) {
		if err != nil {
			connectFailed.Store(true)
		}
	}}
	request, e := http.NewRequestWithContext(httptrace.WithClientTrace(ctx, trace), http.MethodPost, strings.TrimRight(config.BaseURL, "/")+"/api/runtime/evaluation-execute/agent-loop/v1", nil)
	if e != nil {
		return out, nil
	}
	post(request, body.Bytes())
	request.Header.Set("Authorization", "Bearer "+token)
	response, e := t.client.Do(request)
	out.Kind = ev.Unknown
	out.DispatchCertainty = "MAY_HAVE_DISPATCHED"
	out.TerminalCertainty = "UNCONFIRMED"
	out.ErrorCategory = string(TransportError)
	out.Reason = "Loop dispatch 未确认终态；只允许查询同一 Run"
	if e != nil {
		if !connected.Load() && connectFailed.Load() {
			out.Kind = ev.Failure
			out.DispatchCertainty = "NOT_DISPATCHED"
			out.TerminalCertainty = "NOT_DISPATCHED"
		}
		return out, nil
	}
	defer response.Body.Close()
	raw, e := boundedBody(response.Body, config.MaxResponseBytes)
	if e != nil {
		out.ErrorCategory = string(ProtocolError)
		return out, nil
	}
	if response.StatusCode == http.StatusAccepted {
		var admitted struct {
			Protocol string `json:"protocol_version"`
			RunID    string `json:"run_id"`
			Status   string `json:"status"`
			Capture  string `json:"capture_status"`
		}
		if agentquality.StrictLoopShape(raw, &admitted) == nil && admitted.Protocol == AgentLoopProtocol && admitted.RunID == q.AttemptID && admitted.Status == "ADMITTED" && admitted.Capture == "NOT_READY" {
			out.DispatchCertainty = "REMOTE_ACCEPTED"
		}
		return out, nil
	}
	if response.StatusCode != http.StatusOK {
		out.ErrorCategory = string(ProtocolError)
		return out, nil
	}
	out.ErrorCategory = string(ProtocolError)
	if required(raw, "protocol_version", "run_id", "status", "stop_reason", "error_code", "safe_message", "capture_status", "capture_error_code", "loop_evidence", "loop_evidence_digest", "final_answer_capture_status", "final_answer_capture_error_code", "final_answer_evidence") != nil {
		return out, nil
	}
	var wire agentLoopResponse
	if strict(raw, &wire) != nil || wire.Protocol != AgentLoopProtocol || wire.RunID != q.AttemptID || wire.LoopDigest != wire.Loop.Digest() || !contains(wire.Capture, "COMPLETE", "PARTIAL") || wire.CaptureError != nil {
		return out, nil
	}
	loop, e := agentquality.DecodeLoopEvidence(wire.Loop, q.AttemptID, agentquality.LoopCorrelation{EvaluationRunID: q.RunID, AttemptID: q.AttemptID, RequestID: q.ID})
	if e != nil || loop.Coverage != wire.Capture || loop.Root.Status != wire.Status || loop.Root.StopReason != wire.Stop {
		return out, nil
	}
	evidence := []ev.Binding{binding(s, q, loop.ID, agentquality.LoopEvidenceSchema, wire.Loop)}
	var answer *finalAnswer
	if wire.Status == "SUCCEEDED" && wire.AnswerCapture == "COMPLETE" {
		var a finalAnswer
		if agentquality.StrictLoopShape(wire.Answer.Bytes(), &a) != nil || a.Schema != "final-answer-evidence.v1" || a.ID != "final-answer://"+q.AttemptID || a.RunID != q.AttemptID || a.AttemptID != q.AttemptID || a.Content == nil || a.Media != "text/plain; charset=utf-8" || len(*a.Content) > 65536 || a.Digest != fmt.Sprintf("%x", sha256.Sum256([]byte(*a.Content))) || wire.AnswerError != nil || loop.Final == nil || loop.Final.ContentDigest != a.Digest {
			return out, nil
		}
		answer = &a
		evidence = append(evidence, binding(s, q, a.ID, a.Schema, wire.Answer))
	} else if wire.AnswerCapture != "FAILED" || wire.AnswerError == nil || wire.Answer.String() != "null" {
		return out, nil
	}
	switch wire.Status {
	case "SUCCEEDED":
		if wire.Stop != "COMPLETED" || wire.Error != nil {
			return out, nil
		}
		out.Kind = ev.Success
	case "FAILED":
		if contains(wire.Stop, "COMPLETED", "USER_CANCELLED", "CLIENT_DISCONNECTED", "SYSTEM_SHUTDOWN") {
			return out, nil
		}
		out.Kind = ev.Failure
		if wire.Stop == "DEADLINE_EXCEEDED" {
			out.Kind = ev.Timeout
		}
	case "CANCELLED":
		if !contains(wire.Stop, "USER_CANCELLED", "CLIENT_DISCONNECTED", "SYSTEM_SHUTDOWN") {
			return out, nil
		}
		out.Kind = ev.Cancelled
	default:
		return out, nil
	}
	out.DispatchCertainty = "REMOTE_ACCEPTED"
	out.TerminalCertainty = "REMOTE_CONFIRMED"
	out.Evidence = evidence
	if out.Kind == ev.Success {
		out.ErrorCategory = ""
		out.Reason = "Loop terminal 已确认"
		artifact := binding(s, q, "localagent-run://"+q.AttemptID, "artifact.v1", asset.JSON{})
		artifact.Availability = "UNAVAILABLE"
		if answer != nil {
			j, _ := asset.Freeze(*answer.Content)
			artifact = binding(s, q, answer.ID, "artifact.v1", j)
		}
		out.Artifact = &artifact
	} else {
		out.ErrorCategory = string(RemoteTerminal)
		out.Reason = "Loop terminal: " + wire.Stop
	}
	out.Cleanup, _ = asset.Freeze(map[string]any{"target_version": AgentLoopTargetVersion, "response_sha256": fmt.Sprintf("%x", sha256.Sum256(raw)), "capture_status": wire.Capture})
	return out, nil
}
