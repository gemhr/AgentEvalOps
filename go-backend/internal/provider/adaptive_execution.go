package provider

import (
	"context"
	"net/http"
	"os"
	"strings"
	"time"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
)

const AdaptiveTargetID = "localagent-adaptive-http"
const AdaptiveProtocol = "adaptive-execution-evaluate.v1"

type AdaptiveConfig struct {
	Transport     AgentLoopConfig `json:"transport"`
	Group         string          `json:"group"`
	PolicyDigest  string          `json:"policy_digest"`
	DatasetDigest string          `json:"dataset_digest"`
}
type AdaptiveInput struct {
	Slice           string `json:"slice"`
	IncidentID      string `json:"incident_id"`
	ExecutionID     string `json:"execution_id"`
	ExecutionDigest string `json:"execution_digest"`
}
type AdaptiveTarget struct {
	config AdaptiveConfig
	client *http.Client
}

func NewAdaptiveTarget(c AdaptiveConfig) (*AdaptiveTarget, error) {
	digest := agentquality.AdaptivePolicyDigest(c.Group)
	if c.Transport.Validate() != nil || digest == "" || c.PolicyDigest != digest || !ciDigest(c.DatasetDigest) {
		return nil, asset.ErrInvalid
	}
	client, e := newClient(c.Transport.HTTP)
	if e != nil {
		return nil, e
	}
	return &AdaptiveTarget{c, client}, nil
}
func (t *AdaptiveTarget) Close() { t.client.CloseIdleConnections() }
func (t *AdaptiveTarget) Execute(parent context.Context, s ev.Scope, q ev.Request) (out ev.Outcome, err error) {
	started := time.Now()
	out = ev.Outcome{Kind: ev.Failure, RequestID: q.ID, RemoteID: q.AttemptID, Protocol: AdaptiveProtocol, Source: "LOCALAGENT_HTTP", DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: string(ProtocolError), Reason: "Adaptive 请求无效"}
	defer func() {
		elapsed := time.Since(started).Milliseconds()
		out.Cleanup, _ = asset.Freeze(agentquality.AdaptiveClientObservation{ClientRT: &elapsed, Censored: out.Kind == ev.Unknown, RemoteCertainty: out.TerminalCertainty})
	}()
	var config AdaptiveConfig
	var input AdaptiveInput
	if q.Target.ID != AdaptiveTargetID || q.Target.Version != AdaptiveProtocol || q.Target.Kind != "LOCALAGENT_HTTP" || q.Target.Config.Decode(&config) != nil || config != t.config || q.Target.TimeoutMilliseconds < 1 || q.Target.TimeoutMilliseconds > 300000 || s.ProjectID != config.Transport.TrustedProjectID || s.ProjectID != q.Case.Identity.ProjectID || agentquality.StrictLoopShape(q.Case.Case.Input.Bytes(), &input) != nil || !contains(input.Slice, "STATUS", "DIAGNOSIS") || !asset.ValidID(input.IncidentID) || !asset.ValidID(input.ExecutionID) {
		return out, nil
	}
	token := os.Getenv(config.Transport.TokenEnv)
	if strings.TrimSpace(token) == "" || strings.ContainsAny(token, "\r\n") {
		out.ErrorCategory = string(TransportError)
		return out, nil
	}
	identity := agentquality.AdaptiveIdentity{ProjectID: s.ProjectID, EvaluationRunID: q.RunID, AttemptID: q.AttemptID, RequestID: q.ID, CaseID: q.Case.Identity.Ref.EntityID, CaseVersion: q.Case.Identity.Ref.Version, CaseDigest: q.Case.Identity.ContentDigest, DatasetDigest: config.DatasetDigest, InputDigest: q.Case.Case.Input.Digest(), RunID: q.AttemptID}
	body, _ := asset.Freeze(map[string]any{"protocol_version": AdaptiveProtocol, "project_id": identity.ProjectID, "evaluation_run_id": identity.EvaluationRunID, "attempt_id": identity.AttemptID, "request_id": identity.RequestID, "case_id": identity.CaseID, "case_version": identity.CaseVersion, "case_digest": identity.CaseDigest, "dataset_digest": identity.DatasetDigest, "input_digest": identity.InputDigest, "group": config.Group, "policy_digest": config.PolicyDigest, "timeout_milliseconds": q.Target.TimeoutMilliseconds, "input": input})
	ctx, cancel := context.WithTimeout(parent, time.Duration(config.Transport.CallMilliseconds)*time.Millisecond)
	defer cancel()
	request, e := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(config.Transport.BaseURL, "/")+"/api/runtime/evaluation-execute/adaptive/v1", nil)
	if e != nil {
		return out, nil
	}
	post(request, body.Bytes())
	request.Header.Set("Authorization", "Bearer "+token)
	out.Kind = ev.Unknown
	out.DispatchCertainty = "MAY_HAVE_DISPATCHED"
	out.TerminalCertainty = "UNCONFIRMED"
	out.ErrorCategory = string(TransportError)
	out.Reason = "远端结果未确认；只能查询原 Attempt 对应 Run"
	response, e := t.client.Do(request)
	if e != nil {
		return out, nil
	}
	defer response.Body.Close()
	if response.StatusCode == 202 {
		out.DispatchCertainty = "REMOTE_ACCEPTED"
		return out, nil
	}
	if response.StatusCode != 200 {
		out.ErrorCategory = string(ProtocolError)
		return out, nil
	}
	raw, e := boundedBody(response.Body, config.Transport.MaxResponseBytes)
	if e != nil {
		return out, nil
	}
	j, e := asset.ParseJSON(raw)
	if e != nil {
		return out, nil
	}
	evidence, e := agentquality.DecodeAdaptive(j)
	if e != nil || evidence.Identity != identity || evidence.Strategy.Group != config.Group || evidence.Strategy.Digest != config.PolicyDigest {
		out.ErrorCategory = string(ProtocolError)
		return out, nil
	}
	out.DispatchCertainty = "REMOTE_ACCEPTED"
	out.TerminalCertainty = "REMOTE_CONFIRMED"
	out.Evidence = []ev.Binding{binding(s, q, "adaptive-execution://"+q.AttemptID, agentquality.AdaptiveSchema, j)}
	switch evidence.Runtime.Status {
	case "SUCCEEDED":
		out.Kind = ev.Success
		out.ErrorCategory = ""
	case "FAILED":
		out.Kind = ev.Failure
		out.ErrorCategory = string(RemoteTerminal)
		if evidence.Runtime.Stop == "DEADLINE_EXCEEDED" {
			out.Kind = ev.Timeout
		}
	case "CANCELLED":
		out.Kind = ev.Cancelled
		out.ErrorCategory = string(RemoteTerminal)
	}
	out.Reason = "已确认 Runtime 与业务回执，业务质量由 Evaluator 单独判定"
	if out.Kind == ev.Success {
		artifact := binding(s, q, "adaptive-output://"+q.AttemptID, "artifact.v1", asset.JSON{})
		artifact.Availability = "UNAVAILABLE"
		if evidence.Business.Output != nil {
			output, e := asset.ParseJSON([]byte(*evidence.Business.Output))
			if e != nil {
				return out, e
			}
			artifact = binding(s, q, "adaptive-output://"+q.AttemptID, "artifact.v1", output)
		}
		out.Artifact = &artifact
	}
	return out, nil
}
