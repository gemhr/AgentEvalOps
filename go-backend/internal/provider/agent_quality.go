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

const AgentQualityTargetID = "localagent-agent-quality-http"
const AgentQualityTargetVersion = "agent-quality-evaluation.v1"
const AgentQualityProtocol = "localagent-agent-quality-evaluation-execute.v1"

type AgentQualityConfig struct {
	BaseURL          string     `json:"base_url"`
	TokenEnv         string     `json:"token_env"`
	TrustedProjectID string     `json:"trusted_project_id"`
	MaxResponseBytes int        `json:"max_response_bytes"`
	CallMilliseconds int64      `json:"call_milliseconds"`
	HTTP             HTTPConfig `json:"http"`
	Manifest         asset.JSON `json:"execution_manifest"`
	Profile          asset.JSON `json:"capture_profile"`
}

func (c AgentQualityConfig) Validate() error {
	var m agentquality.QualityManifest
	var p agentquality.QualityProfile
	if c.MaxResponseBytes > 2*1024*1024 || c.MaxResponseBytes < 1 ||
		agentquality.StrictQualityShape(c.Manifest.Bytes(), &m) != nil || agentquality.StrictQualityShape(c.Profile.Bytes(), &p) != nil ||
		m.Schema != "agent-quality-execution-manifest.v1" || len(m.Agents) == 0 || len(m.Agents) > 16 ||
		p.Policy != "agent-quality-capture.v1" || !asset.Text(p.ID) || p.Version < 1 ||
		agentquality.ValidateQualityJSON(c.Manifest, 65536) != nil || agentquality.ValidateQualityJSON(c.Profile, 16384) != nil {
		return asset.ErrInvalid
	}
	return (AgentLoopConfig{BaseURL: c.BaseURL, TokenEnv: c.TokenEnv, TrustedProjectID: c.TrustedProjectID,
		MaxResponseBytes: c.MaxResponseBytes, CallMilliseconds: c.CallMilliseconds, HTTP: c.HTTP}).Validate()
}

type AgentQualityTarget struct {
	config AgentQualityConfig
	client *http.Client
}

func NewAgentQualityTarget(c AgentQualityConfig, _ *slog.Logger) (*AgentQualityTarget, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	client, err := newClient(c.HTTP)
	if err != nil {
		return nil, err
	}
	return &AgentQualityTarget{c, client}, nil
}
func (t *AgentQualityTarget) Close() { t.client.CloseIdleConnections() }

type agentQualityResponse struct {
	Protocol string     `json:"protocol_version"`
	Run      string     `json:"run_id"`
	Status   string     `json:"status"`
	Stop     string     `json:"stop_reason"`
	Capture  string     `json:"capture_status"`
	Bundle   asset.JSON `json:"bundle"`
	Digest   string     `json:"bundle_digest"`
	Answer   asset.JSON `json:"final_answer_evidence"`
}

func (t *AgentQualityTarget) Execute(parent context.Context, s ev.Scope, q ev.Request) (out ev.Outcome, err error) {
	out = ev.Outcome{Kind: ev.Failure, RequestID: q.ID, RemoteID: q.AttemptID, Protocol: AgentQualityProtocol, Source: "LOCALAGENT_HTTP",
		DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: string(ProtocolError), Reason: "QUALITY_REQUEST_INVALID"}
	var config AgentQualityConfig
	var input struct {
		Agent string  `json:"agent_id"`
		Query *string `json:"query"`
	}
	if q.Target.ID != AgentQualityTargetID || q.Target.Version != AgentQualityTargetVersion || q.Target.Kind != "LOCALAGENT_HTTP" ||
		q.Target.TimeoutMilliseconds < 1 || q.Target.TimeoutMilliseconds > 300000 || !asset.ValidID(q.RunID) || !asset.ValidID(q.AttemptID) || !asset.ValidID(q.ID) ||
		s.ProjectID != t.config.TrustedProjectID || s.ProjectID != q.Case.Identity.ProjectID || q.Case.Case.Input.Decode(&input) != nil ||
		!asset.Text(input.Agent) || input.Query == nil || q.Target.Config.Decode(&config) != nil {
		return out, nil
	}
	want, _ := asset.Freeze(t.config)
	got, _ := asset.Freeze(config)
	if want.String() != got.String() {
		return out, nil
	}
	var profile agentquality.QualityProfile
	_ = config.Profile.Decode(&profile)
	correlation := agentquality.QualityCorrelation{Agent: input.Agent, Run: q.AttemptID, Project: s.ProjectID, EvaluationRun: q.RunID,
		Attempt: q.AttemptID, Request: q.ID, Case: q.Case.Identity.Ref.EntityID, CaseVersion: q.Case.Identity.Ref.Version,
		CaseDigest: q.Case.Identity.ContentDigest, ManifestDigest: config.Manifest.Digest(), ProfileID: profile.ID, ProfileVersion: profile.Version,
		ProfileDigest: config.Profile.Digest(), Timeout: q.Target.TimeoutMilliseconds, QueryDigest: fmt.Sprintf("%x", sha256.Sum256([]byte(*input.Query)))}
	token := os.Getenv(config.TokenEnv)
	if strings.TrimSpace(token) == "" || strings.ContainsAny(token, "\r\n") || parent.Err() != nil {
		out.ErrorCategory = string(TransportError)
		out.Reason = "QUALITY_CREDENTIAL_UNAVAILABLE"
		return out, nil
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(config.CallMilliseconds)*time.Millisecond)
	defer cancel()
	body, _ := asset.Freeze(map[string]any{"agent_id": input.Agent, "query": *input.Query, "run_id": q.AttemptID, "evaluation_project_id": s.ProjectID,
		"evaluation_run_id": q.RunID, "attempt_id": q.AttemptID, "request_id": q.ID, "case_id": correlation.Case, "case_version": correlation.CaseVersion,
		"case_digest": correlation.CaseDigest, "execution_manifest_digest": correlation.ManifestDigest, "capture_profile_id": profile.ID,
		"capture_profile_version": profile.Version, "capture_profile_digest": correlation.ProfileDigest, "timeout_milliseconds": q.Target.TimeoutMilliseconds})
	var connected, connectFailed atomic.Bool
	trace := &httptrace.ClientTrace{GotConn: func(httptrace.GotConnInfo) { connected.Store(true) }, ConnectDone: func(_, _ string, err error) {
		if err != nil {
			connectFailed.Store(true)
		}
	}}
	request, e := http.NewRequestWithContext(httptrace.WithClientTrace(ctx, trace), http.MethodPost, strings.TrimRight(config.BaseURL, "/")+"/api/runtime/evaluation-execute/agent-quality/v1", nil)
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
	out.Reason = "QUALITY_OUTCOME_UNKNOWN"
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
			Run      string `json:"run_id"`
			Status   string `json:"status"`
			Capture  string `json:"capture_status"`
		}
		if agentquality.StrictQualityShape(raw, &admitted) == nil && admitted.Protocol == AgentQualityProtocol && admitted.Run == q.AttemptID && admitted.Status == "ADMITTED" && admitted.Capture == "NOT_READY" {
			out.DispatchCertainty = "REMOTE_ACCEPTED"
		}
		return out, nil
	}
	return verifyAgentQualityResponse(s, q, correlation, out, raw, response.StatusCode)
}

func verifyAgentQualityResponse(s ev.Scope, q ev.Request, correlation agentquality.QualityCorrelation, out ev.Outcome, raw []byte, status int) (ev.Outcome, error) {
	out.ErrorCategory = string(ProtocolError)
	var wire agentQualityResponse
	canonical, e := asset.ParseJSON(raw)
	if status != http.StatusOK || e != nil || string(raw) != canonical.String() || agentquality.StrictQualityShape(raw, &wire) != nil ||
		wire.Protocol != AgentQualityProtocol || wire.Run != q.AttemptID || wire.Digest != wire.Bundle.Digest() {
		return out, nil
	}
	bundle, _, e := agentquality.DecodeQuality(wire.Bundle, correlation)
	if e != nil || bundle.Coverage != wire.Capture || bundle.Terminal.Status != wire.Status || bundle.Terminal.Stop != wire.Stop {
		return out, nil
	}
	var answer *finalAnswer
	if wire.Answer.String() != "null" {
		var a finalAnswer
		if wire.Status != "SUCCEEDED" || agentquality.StrictQualityShape(wire.Answer.Bytes(), &a) != nil || a.Schema != "final-answer-evidence.v1" ||
			a.ID != "final-answer://"+q.AttemptID || a.RunID != q.AttemptID || a.AttemptID != q.AttemptID || a.Content == nil || a.Media != "text/plain; charset=utf-8" ||
			len([]byte(*a.Content)) > 65536 || a.Digest != fmt.Sprintf("%x", sha256.Sum256([]byte(*a.Content))) || bundle.Final == nil || bundle.Final.Digest != a.Digest {
			return out, nil
		}
		answer = &a
	}
	switch wire.Status {
	case "SUCCEEDED":
		if wire.Stop != "COMPLETED" {
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
	out.Evidence = []ev.Binding{binding(s, q, bundle.ID, agentquality.QualitySchema, wire.Bundle)}
	if out.Kind == ev.Success {
		out.ErrorCategory = ""
		out.Reason = "QUALITY_TERMINAL_CONFIRMED"
		artifact := binding(s, q, "localagent-run://"+q.AttemptID, "artifact.v1", asset.JSON{})
		artifact.Availability = "UNAVAILABLE"
		if answer != nil {
			a, _ := asset.Freeze(*answer.Content)
			artifact = binding(s, q, answer.ID, "artifact.v1", a)
		}
		out.Artifact = &artifact
	} else {
		out.ErrorCategory = string(RemoteTerminal)
		out.Reason = "QUALITY_REMOTE_TERMINAL"
	}
	out.Cleanup, _ = asset.Freeze(map[string]any{"target_version": AgentQualityTargetVersion, "response_sha256": fmt.Sprintf("%x", sha256.Sum256(raw)), "capture_status": wire.Capture})
	return out, nil
}
