package provider

import (
	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"strings"
	"time"
)

const BasebandTargetID = "baseband-sealed-http"
const BasebandTargetKind = "BASEBAND_SEALED_HTTP"
const BasebandTargetVersion = "baseband-sealed-http.v1"

type BasebandTarget struct {
	config LocalAgentConfig
	client *http.Client
	Reader ev.Reader
}

func BasebandConfigFromEnv() (LocalAgentConfig, error) {
	var c LocalAgentConfig
	j, e := asset.ParseJSON([]byte(os.Getenv("BASEBAND_TARGET_CONFIG")))
	if e != nil || j.Decode(&c) != nil || c.Validate() != nil {
		return c, asset.ErrInvalid
	}
	return c, nil
}
func NewBasebandTarget(c LocalAgentConfig, reader ev.Reader) (*BasebandTarget, error) {
	if c.Validate() != nil || reader == nil {
		return nil, asset.ErrInvalid
	}
	client, e := newClient(c.HTTP)
	if e != nil {
		return nil, e
	}
	return &BasebandTarget{c, client, reader}, nil
}
func (t *BasebandTarget) Close() { t.client.CloseIdleConnections() }
func (t *BasebandTarget) Execute(parent context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	out := ev.Outcome{Kind: ev.Failure, RequestID: q.ID, Source: BasebandTargetKind, RemoteID: q.AttemptID, Protocol: aq.BasebandOutput, DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: string(ProtocolError), Reason: "BASEBAND_BINDING_INVALID"}
	expected, _ := asset.Freeze(t.config)
	if q.Target.ID != BasebandTargetID || q.Target.Kind != BasebandTargetKind || q.Target.Version != BasebandTargetVersion || q.Target.Config.Digest() != expected.Digest() || q.Case.Identity.ProjectID != s.ProjectID {
		return out, nil
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(t.config.CallMilliseconds)*time.Millisecond)
	defer cancel()
	state, e := t.Reader.ReadRunState(ctx, s.Scope, q.RunID)
	if e != nil {
		return out, e
	}
	var subject aq.BasebandSubject
	if state.Run.Snapshot.Subject.Decode(&subject) != nil || subject.Subject != "baseband" || (subject.Version != "baseband-rule.v1" && subject.Version != "baseband_test_investigator/1.0.0") || subject.Environment != "development-visible" || subject.Mode != "SEALED_RUN_REPLAY" {
		return out, nil
	}
	selector, ok := subject.Selectors[q.Case.Identity.Ref.EntityID]
	if !ok {
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
	// 明确 DTO：绝不把 Case.GroundTruth/ExpectedOutput 发给 AgentCore。
	body := map[string]any{"protocol": "baseband-quality-request.v1", "project_id": s.ProjectID, "run_id": q.RunID, "attempt_id": q.AttemptID, "request_id": q.ID, "case_id": q.Case.Identity.Ref.EntityID, "frozen_input_digest": q.Case.Case.Input.Digest(), "input": q.Case.Case.Input, "selector": selector}
	raw, _ := json.Marshal(body)
	req, e := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(t.config.BaseURL, "/")+"/api/ci/quality/baseband/evaluate", nil)
	if e != nil {
		return out, e
	}
	post(req, raw)
	req.Header.Set("Authorization", "Bearer "+token)
	out.Kind = ev.Unknown
	out.DispatchCertainty = "MAY_HAVE_DISPATCHED"
	out.TerminalCertainty = "UNCONFIRMED"
	out.Reason = "BASEBAND_RECEIPT_UNCONFIRMED"
	response, e := t.client.Do(req)
	if e != nil {
		return out, nil
	}
	defer response.Body.Close()
	result, e := boundedBody(response.Body, t.config.MaxResponseBytes)
	if e != nil || response.StatusCode != 200 {
		return out, nil
	}
	artifact, e := asset.ParseJSON(result)
	var receipt aq.BasebandReceipt
	if e != nil || artifact.Decode(&receipt) != nil || receipt.Validate(q.Case.Case.Input, selector, s.ProjectID, q.RunID, q.AttemptID, q.ID, q.Case.Identity.Ref.EntityID) != nil {
		return out, nil
	}
	b := binding(s, q, "baseband://"+q.AttemptID, aq.BasebandOutput, artifact)
	out.Kind = ev.Success
	out.DispatchCertainty = "REMOTE_ACCEPTED"
	out.TerminalCertainty = "REMOTE_CONFIRMED"
	out.ErrorCategory = ""
	out.Reason = "BASEBAND_SEALED_SOURCE_VERIFIED"
	out.Artifact = &b
	out.Evidence = []ev.Binding{b}
	return out, nil
}
