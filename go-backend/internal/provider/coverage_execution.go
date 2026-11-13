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

// 与基带分支共享同一 Target、HTTP client、凭据配置及 Worker。
func (t *BasebandTarget) executeCoverage(parent context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	out := ev.Outcome{Kind: ev.Failure, RequestID: q.ID, Source: BasebandTargetKind, RemoteID: q.AttemptID, Protocol: aq.CoverageOutput, DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: string(ProtocolError), Reason: "COVERAGE_BINDING_INVALID"}
	frozen, _ := asset.Freeze(t.config)
	if q.Target.ID != BasebandTargetID || q.Target.Kind != BasebandTargetKind || q.Target.Version != aq.CoverageTargetVersion || q.Target.Config.Digest() != frozen.Digest() || q.Case.Identity.ProjectID != s.ProjectID {
		return out, nil
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(t.config.CallMilliseconds)*time.Millisecond)
	defer cancel()
	state, e := t.Reader.ReadRunState(ctx, s.Scope, q.RunID)
	if e != nil {
		return out, e
	}
	var subject aq.CoverageSubject
	if state.Run.Snapshot.Subject.Decode(&subject) != nil || subject.Mode != "SEALED_BUSINESS_REPLAY" {
		return out, nil
	}
	selector, ok := subject.Selectors[q.Case.Identity.Ref.EntityID]
	if !ok || selector.Diagnosis != "RULE" {
		return out, nil
	}
	found := false
	for _, a := range state.Attempts {
		found = found || (a.ID == q.AttemptID && a.Request.ID == q.ID && a.Request.Case.Identity.ContentDigest == q.Case.Identity.ContentDigest)
	}
	if !found {
		return out, nil
	}
	token := os.Getenv(t.config.TokenEnv)
	if strings.TrimSpace(token) == "" || strings.ContainsAny(token, "\r\n") {
		return out, nil
	}
	// 显式 DTO，不传 GT、预期结果或客户端伪造的 Plan/Trigger。
	body := map[string]any{"protocol": "coverage-execution-request.v1", "project_id": s.ProjectID, "run_id": q.RunID, "attempt_id": q.AttemptID, "request_id": q.ID, "case_id": q.Case.Identity.Ref.EntityID, "frozen_input_digest": q.Case.Case.Input.Digest(), "input": q.Case.Case.Input, "selector": selector}
	raw, _ := json.Marshal(body)
	req, e := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(t.config.BaseURL, "/")+"/api/ci/quality/coverage-execution/evaluate", nil)
	if e != nil {
		return out, e
	}
	post(req, raw)
	req.Header.Set("Authorization", "Bearer "+token)
	out.Kind = ev.Unknown
	out.DispatchCertainty = "MAY_HAVE_DISPATCHED"
	out.TerminalCertainty = "UNCONFIRMED"
	out.Reason = "COVERAGE_SOURCE_UNCONFIRMED"
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
	var receipt aq.CoverageReceipt
	if e != nil || artifact.Decode(&receipt) != nil || receipt.Validate(q.Case.Case.Input, selector, s.ProjectID, q.RunID, q.AttemptID, q.ID, q.Case.Identity.Ref.EntityID) != nil {
		return out, nil
	}
	b := binding(s, q, "coverage-execution://"+q.AttemptID, aq.CoverageOutput, artifact)
	out.Kind = ev.Success
	out.DispatchCertainty = "REMOTE_ACCEPTED"
	out.TerminalCertainty = "REMOTE_CONFIRMED"
	out.ErrorCategory = ""
	out.Reason = "COVERAGE_SOURCE_VERIFIED"
	out.Artifact = &b
	out.Evidence = []ev.Binding{b}
	return out, nil
}
