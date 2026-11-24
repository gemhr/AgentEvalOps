package provider

import (
	"context"
	"net/http"
	"os"
	"strings"
	"time"

	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
)

func (t *BasebandTarget) executeRepair(parent context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	out := ev.Outcome{Kind: ev.Failure, RequestID: q.ID, Source: BasebandTargetKind, RemoteID: q.AttemptID,
		Protocol: aq.RepairWire, DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED",
		ErrorCategory: string(ProtocolError), Reason: "CI_REPAIR_BINDING_INVALID"}
	expected, _ := asset.Freeze(t.config)
	c, _, valid := aq.RepairCatalogCase(q.Case.Case.Input, q.Case.Case.GroundTruth)
	if !valid || c.CaseID == "" || q.Target.ID != BasebandTargetID || q.Target.Kind != BasebandTargetKind ||
		q.Target.Config.Digest() != expected.Digest() || q.Case.Identity.ProjectID != s.ProjectID {
		return out, nil
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(t.config.CallMilliseconds)*time.Millisecond)
	defer cancel()
	state, err := t.Reader.ReadRunState(ctx, s.Scope, q.RunID)
	if err != nil {
		return out, err
	}
	var sub aq.RepairSubject
	if state.Run.Snapshot.Target.Version != aq.RepairTargetVersion || state.Run.Snapshot.Subject.Decode(&sub) != nil || !sub.Valid() {
		return out, nil
	}
	found := false
	for _, attempt := range state.Attempts {
		if attempt.ID == q.AttemptID && attempt.Request.ID == q.ID &&
			attempt.Request.Case.Identity.ContentDigest == q.Case.Identity.ContentDigest {
			found = true
			break
		}
	}
	token := os.Getenv(t.config.TokenEnv)
	if !found || strings.TrimSpace(token) == "" || strings.ContainsAny(token, "\r\n") {
		return out, nil
	}
	url := strings.TrimRight(t.config.BaseURL, "/") + "/api/ci/quality/repair-evidence/" + sub.WorkflowID +
		"/" + sub.EvidenceDigest + "?project_id=" + s.ProjectID
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return out, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/json")
	out.Kind, out.DispatchCertainty, out.TerminalCertainty, out.Reason = ev.Unknown, "MAY_HAVE_DISPATCHED", "UNCONFIRMED", "CI_REPAIR_EVIDENCE_UNCONFIRMED"
	res, err := t.client.Do(req)
	if err != nil {
		return out, nil
	}
	defer res.Body.Close()
	raw, err := boundedBody(res.Body, t.config.MaxResponseBytes)
	if err != nil || res.StatusCode != http.StatusOK || res.Header.Get("X-Content-SHA256") != sub.EvidenceDigest ||
		!aq.RepairTransportValid(string(raw), sub.RepairLocator) {
		return out, nil
	}
	body, err := asset.Freeze(aq.RepairTransport{ProjectID: s.ProjectID, Raw: string(raw), Digest: sub.EvidenceDigest})
	if err != nil {
		return out, err
	}
	proof := binding(s, q, "ci-repair://"+sub.WorkflowID+"/"+sub.EvidenceDigest, aq.RepairWire, body)
	out.Kind, out.DispatchCertainty, out.TerminalCertainty, out.ErrorCategory = ev.Success, "REMOTE_ACCEPTED", "REMOTE_CONFIRMED", ""
	out.Reason, out.Artifact, out.Evidence = "CI_REPAIR_EVIDENCE_FETCHED", &proof, []ev.Binding{proof}
	return out, nil
}
