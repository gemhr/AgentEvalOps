package provider

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	fi "agentevalops/go-backend/internal/featureimpact"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"strings"
	"time"
)

const FeatureImpactTargetID = "feature-impact-http"
const FeatureImpactTargetKind = "FEATURE_IMPACT_HTTP"
const FeatureImpactTargetVersion = "feature-impact-http.v1"

type FeatureImpactTarget struct {
	config LocalAgentConfig
	client *http.Client
	Reader ev.Reader
}

func FeatureImpactConfigFromEnv() (LocalAgentConfig, error) {
	var c LocalAgentConfig
	if fi.Decode([]byte(os.Getenv("FEATURE_IMPACT_TARGET_CONFIG")), &c) != nil || c.Validate() != nil {
		return c, asset.ErrInvalid
	}
	return c, nil
}
func NewFeatureImpactTarget(c LocalAgentConfig, reader ev.Reader) (*FeatureImpactTarget, error) {
	if c.Validate() != nil || reader == nil {
		return nil, asset.ErrInvalid
	}
	client, e := newClient(c.HTTP)
	if e != nil {
		return nil, e
	}
	return &FeatureImpactTarget{c, client, reader}, nil
}
func (t *FeatureImpactTarget) Close() { t.client.CloseIdleConnections() }
func (t *FeatureImpactTarget) Execute(parent context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	out := ev.Outcome{Kind: ev.Failure, RequestID: q.ID, Source: FeatureImpactTargetKind, RemoteID: q.AttemptID, Protocol: fi.OutputProtocol, DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: string(ProtocolError), Reason: "影响分析输入或身份无效"}
	in, e := fi.ReadInput(q.Case.Case.Input.Bytes())
	expected, _ := asset.Freeze(t.config)
	if e != nil || q.Target.ID != FeatureImpactTargetID || q.Target.Kind != FeatureImpactTargetKind || q.Target.Version != FeatureImpactTargetVersion || q.Target.Config.Digest() != expected.Digest() || q.Case.Identity.ProjectID != s.ProjectID {
		return out, nil
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(t.config.CallMilliseconds)*time.Millisecond)
	defer cancel()
	state, e := t.Reader.ReadRunState(ctx, s.Scope, q.RunID)
	if e != nil {
		return out, e
	}
	var subject struct {
		Subject     string `json:"subject"`
		Version     string `json:"subject_version"`
		Environment string `json:"environment"`
		Mode        string `json:"run_mode"`
	}
	if state.Run.Snapshot.Subject.Decode(&subject) != nil || subject.Subject != "feature-impact" || subject.Version != in.CandidateVersion || subject.Mode != "OFFLINE_BUSINESS_REPLAY" || subject.Environment != "frozen-public-corpus" {
		return out, nil
	}
	found := false
	for _, a := range state.Attempts {
		if a.ID == q.AttemptID && a.Request.ID == q.ID && a.Request.Case.Identity.ContentDigest == q.Case.Identity.ContentDigest {
			found = true
		}
	}
	if !found {
		return out, nil
	}
	token := os.Getenv(t.config.TokenEnv)
	if strings.TrimSpace(token) == "" || strings.ContainsAny(token, "\r\n") {
		return out, nil
	}
	body := map[string]any{"protocol": "feature-impact-request.v1", "project_id": s.ProjectID, "run_id": q.RunID, "attempt_id": q.AttemptID, "request_id": q.ID, "case_id": q.Case.Identity.Ref.EntityID, "frozen_input_digest": q.Case.Case.Input.Digest(), "input": in}
	raw, _ := json.Marshal(body)
	req, e := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(t.config.BaseURL, "/")+"/api/feature-impact/quality/evaluate", nil)
	if e != nil {
		return out, e
	}
	post(req, raw)
	req.Header.Set("Authorization", "Bearer "+token)
	out.Kind = ev.Unknown
	out.DispatchCertainty = "MAY_HAVE_DISPATCHED"
	out.TerminalCertainty = "UNCONFIRMED"
	out.Reason = "业务 HTTP 回执未确认"
	response, e := t.client.Do(req)
	if e != nil {
		return out, nil
	}
	defer response.Body.Close()
	result, e := boundedBody(response.Body, t.config.MaxResponseBytes)
	if e != nil || response.StatusCode != 200 {
		return out, nil
	}
	var receipt fi.Output
	if fi.Decode(result, &receipt) != nil || receipt.Validate(in, s.ProjectID, q.RunID, q.AttemptID, q.ID, q.Case.Identity.Ref.EntityID, q.Case.Case.Input.Digest()) != nil {
		return out, nil
	}
	artifact, e := asset.ParseJSON(result)
	if e != nil {
		return out, nil
	}
	b := binding(s, q, "feature-impact://"+q.AttemptID, fi.OutputProtocol, artifact)
	out.Kind = ev.Success
	out.DispatchCertainty = "REMOTE_ACCEPTED"
	out.TerminalCertainty = "REMOTE_CONFIRMED"
	out.ErrorCategory = ""
	out.Reason = "冻结公开输入业务分析已完成"
	out.Artifact = &b
	out.Evidence = []ev.Binding{b}
	return out, nil
}
