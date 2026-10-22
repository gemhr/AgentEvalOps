package provider

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/cibusiness"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"strings"
	"time"
)

const CIBusinessTargetID = "ci-business-http"
const CIBusinessTargetKind = "CI_BUSINESS_HTTP"
const CIBusinessTargetVersion = "ci-business-http.v1"

type CIBusinessConfig struct {
	Transport   LocalAgentConfig  `json:"transport"`
	Definitions map[string]string `json:"rule_definitions"`
}

func (c CIBusinessConfig) Validate() error {
	if c.Transport.Validate() != nil || len(c.Definitions) != 2 || len(c.Definitions[cibusiness.V1]) != 64 || len(c.Definitions[cibusiness.V2]) != 64 {
		return asset.ErrInvalid
	}
	return nil
}
func BusinessConfigFromEnv() (CIBusinessConfig, error) {
	var c CIBusinessConfig
	if cibusiness.Decode([]byte(os.Getenv("CI_BUSINESS_TARGET_CONFIG")), &c) != nil || c.Validate() != nil {
		return c, asset.ErrInvalid
	}
	return c, nil
}

type CIBusinessTarget struct {
	config CIBusinessConfig
	client *http.Client
	Reader ev.Reader
}

func NewCIBusinessTarget(c CIBusinessConfig, reader ev.Reader) (*CIBusinessTarget, error) {
	if c.Validate() != nil || reader == nil {
		return nil, asset.ErrInvalid
	}
	client, e := newClient(c.Transport.HTTP)
	if e != nil {
		return nil, e
	}
	return &CIBusinessTarget{config: c, client: client, Reader: reader}, nil
}
func (t *CIBusinessTarget) Close() { t.client.CloseIdleConnections() }
func (t *CIBusinessTarget) Execute(parent context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	out := ev.Outcome{Kind: ev.Failure, RequestID: q.ID, Source: "CI_BUSINESS_HTTP", RemoteID: q.AttemptID, Protocol: cibusiness.RequestProtocol, DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: string(ProtocolError), Reason: "业务目标请求校验失败"}
	observations, e := cibusiness.ReadInput(q.Case.Case.Input.Bytes())
	if e != nil {
		return out, nil
	}
	expected, _ := asset.Freeze(t.config)
	if q.Target.ID != CIBusinessTargetID || q.Target.Kind != CIBusinessTargetKind || q.Target.Version != CIBusinessTargetVersion || q.Target.Config.Digest() != expected.Digest() || q.Case.Identity.ProjectID != s.ProjectID {
		return out, nil
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(t.config.Transport.CallMilliseconds)*time.Millisecond)
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
	if state.Run.Snapshot.Subject.Decode(&subject) != nil || subject.Subject != "ci-incident-business-rules" || !cibusiness.ValidRule(subject.Version) || subject.Environment != "frozen-simulation" || subject.Mode != "OFFLINE_BUSINESS_REPLAY" {
		return out, nil
	}
	matched := false
	for _, a := range state.Attempts {
		if a.ID == q.AttemptID && a.Request.ID == q.ID && a.Request.Case.Identity.Ref == q.Case.Identity.Ref && a.Request.Case.Identity.ContentDigest == q.Case.Identity.ContentDigest && a.Request.Case.Case.Input.Digest() == q.Case.Case.Input.Digest() {
			matched = true
		}
	}
	if !matched {
		return out, nil
	}
	body := cibusiness.Request{Protocol: cibusiness.RequestProtocol, ProjectID: s.ProjectID, RunID: q.RunID, AttemptID: q.AttemptID, RequestID: q.ID, CaseID: q.Case.Identity.Ref.EntityID, InputDigest: q.Case.Case.Input.Digest(), Rule: subject.Version, Observations: observations}
	raw, _ := json.Marshal(body)
	token := os.Getenv(t.config.Transport.TokenEnv)
	if strings.TrimSpace(token) == "" || strings.ContainsAny(token, "\r\n") {
		return out, nil
	}
	req, e := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(t.config.Transport.BaseURL, "/")+"/api/ci/quality/evaluate", nil)
	if e != nil {
		return out, e
	}
	post(req, raw)
	req.Header.Set("Authorization", "Bearer "+token)
	out.Kind = ev.Unknown
	out.DispatchCertainty = "MAY_HAVE_DISPATCHED"
	out.TerminalCertainty = "UNCONFIRMED"
	out.Reason = "业务 HTTP 未取得有效回执"
	resp, e := t.client.Do(req)
	if e != nil {
		return out, nil
	}
	defer resp.Body.Close()
	result, e := boundedBody(resp.Body, t.config.Transport.MaxResponseBytes)
	if e != nil || resp.StatusCode != 200 {
		return out, nil
	}
	var receipt cibusiness.Output
	if cibusiness.Decode(result, &receipt) != nil || receipt.Validate(body, t.config.Definitions[body.Rule], q.Case.Case.Input.Digest()) != nil {
		return out, nil
	}
	artifact, e := asset.ParseJSON(result)
	if e != nil {
		return out, nil
	}
	b := binding(s, q, "ci-business://"+q.AttemptID, cibusiness.OutputProtocol, artifact)
	out.Kind = ev.Success
	out.DispatchCertainty = "REMOTE_ACCEPTED"
	out.TerminalCertainty = "REMOTE_CONFIRMED"
	out.ErrorCategory = ""
	out.Reason = "冻结业务检查已真实完成"
	out.Artifact = &b
	out.Evidence = []ev.Binding{b}
	return out, nil
}
