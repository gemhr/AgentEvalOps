package provider_test

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/cibusiness"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/provider"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type businessReader struct{ state ev.RunState }

func (r businessReader) ReadRunState(context.Context, asset.Scope, string) (ev.RunState, error) {
	return r.state, nil
}
func TestFrozenBusinessBoundaryAndJudgments(t *testing.T) {
	ctx := context.Background()
	canary := "REFERENCE_CANARY_NEVER_SENT_7539"
	badIdentity := false
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		if strings.Contains(string(raw), canary) || strings.Contains(string(raw), "ground_truth") || strings.Contains(string(raw), "expected_output") || strings.Contains(string(raw), "assertions") {
			t.Error("reference crossed target boundary")
		}
		var q cibusiness.Request
		if cibusiness.Decode(raw, &q) != nil {
			t.Error("non-whitelisted request")
		}
		calls++
		id := q.AttemptID
		if badIdentity {
			id = "wrong"
		}
		yes := true
		_ = json.NewEncoder(w).Encode(cibusiness.Output{Protocol: cibusiness.OutputProtocol, ProjectID: q.ProjectID, RunID: q.RunID, AttemptID: id, RequestID: q.RequestID, CaseID: q.CaseID, InputDigest: q.InputDigest, Rule: q.Rule, DefinitionDigest: strings.Repeat("b", 64), ObservationsDigest: q.InputDigest, Records: cibusiness.Check{Status: "CHECKED", Violations: []string{}}, Pair: cibusiness.Pair{Candidate: &yes, Reason: "SAME_OBSERVABLE_MECHANISM", EvidenceRefs: []json.RawMessage{}}, Repair: cibusiness.Check{Status: "NOT_APPLICABLE", Violations: []string{}}})
	}))
	defer server.Close()
	fields := []string{"execution_id", "instance_id", "owner_user_id", "tenant_id", "environment_id", "case_id", "feature_id", "state_revision", "outcome", "started_at", "completed_at", "summary", "evidence_manifest", "evidence_digest", "nightly_version_execution_id", "business_completed_at", "incident_id"}
	row := map[string]any{}
	for _, k := range fields {
		row[k] = nil
	}
	row["execution_id"] = "probe-record"
	row["summary"] = map[string]any{}
	raw, _ := json.Marshal(row)
	fact := cibusiness.Fact{Algorithm: "localagent-canonical-json.v1", Bytes: base64.StdEncoding.EncodeToString(raw), SHA256: cibusiness.Digest(raw)}
	observations := cibusiness.Observations{Protocol: cibusiness.InputProtocol, Kind: "PAIR", Records: []cibusiness.Fact{fact, fact}}
	input, _ := asset.Freeze(observations)
	secret, _ := asset.Freeze(map[string]any{"canary": canary})
	content := catalog.CaseContent{Input: input, ExpectedOutput: secret, GroundTruth: secret, TaskGoal: canary, AcceptanceCriteria: []string{canary}, Assertions: []catalog.Assertion{{ID: canary, Kind: canary, Config: secret}}}
	canonical, _ := asset.Freeze(content)
	cfg := provider.CIBusinessConfig{Transport: provider.DefaultLocalAgentConfig(server.URL, "CI_BUSINESS_UNIT_TOKEN"), Definitions: map[string]string{cibusiness.V1: strings.Repeat("a", 64), cibusiness.V2: strings.Repeat("b", 64)}}
	config, _ := asset.Freeze(cfg)
	project, run, attempt, request := asset.NewID(), asset.NewID(), asset.NewID(), asset.NewID()
	q := ev.Request{ID: request, RunID: run, AttemptID: attempt, Case: ev.CaseInput{Identity: ev.AssetIdentity{ProjectID: project, Ref: asset.Ref{EntityID: asset.NewID(), Version: "v1"}, ContentDigest: canonical.Digest(), CanonicalContent: canonical}, Case: content}, Target: ev.Target{ID: provider.CIBusinessTargetID, Kind: provider.CIBusinessTargetKind, Version: provider.CIBusinessTargetVersion, Config: config, TimeoutMilliseconds: 10000}}
	subject, _ := asset.Freeze(map[string]any{"subject": "ci-incident-business-rules", "subject_version": cibusiness.V2, "environment": "frozen-simulation", "run_mode": "OFFLINE_BUSINESS_REPLAY"})
	state := ev.RunState{Run: ev.Run{Snapshot: ev.RunSnapshot{Subject: subject}}, Attempts: []ev.Attempt{{ID: attempt, Request: q}}}
	target, e := provider.NewCIBusinessTarget(cfg, businessReader{state})
	if e != nil {
		t.Fatal(e)
	}
	defer target.Close()
	t.Setenv("CI_BUSINESS_UNIT_TOKEN", "unit-service-credential")
	scope := ev.Scope{Scope: asset.Scope{ProjectID: project, OrganizationID: asset.NewID(), Principal: "probe"}}
	outcome, e := target.Execute(ctx, scope, q)
	if e != nil || outcome.Kind != ev.Success || outcome.Artifact == nil || outcome.Artifact.Digest != outcome.Artifact.Body.Digest() || !outcome.Artifact.Matches(project, run, attempt, request) {
		t.Fatal("artifact binding", outcome, e)
	}
	var actual cibusiness.Output
	_ = outcome.Artifact.Body.Decode(&actual)
	if actual.Rule != cibusiness.V2 || actual.InputDigest != input.Digest() {
		t.Fatal("wrong frozen subject/input")
	}
	yes, no := true, false
	ref := cibusiness.Reference{LabelStatus: "VERIFIED_SIMULATION", Reviewer: "independent-review", ReviewedAt: "2026-10-22T00:00:00Z", Sources: []string{"independent-contract"}, Applicability: map[string]string{}, RecordViolations: []string{}, Association: &yes}
	for _, m := range cibusiness.Metrics {
		ref.Applicability[m] = "APPLICABLE"
	}
	if j := cibusiness.Score(cibusiness.Metrics[0], ref, actual); j.Score == nil || *j.Score != 1 {
		t.Fatal(j)
	}
	ref.Association = &no
	if j := cibusiness.Score(cibusiness.Metrics[1], ref, actual); j.Score == nil || *j.Score != 0 {
		t.Fatal(j)
	}
	if j := cibusiness.Score(cibusiness.Metrics[2], ref, actual); j.Score == nil || *j.Score != 1 || j.Verdict != "FAIL" {
		t.Fatal(j)
	}
	if j := cibusiness.Score(cibusiness.Metrics[3], ref, actual); j.Score != nil || j.Applicability != "MISSING_EVIDENCE" {
		t.Fatal(j)
	}
	ref.Applicability[cibusiness.Metrics[1]] = "NOT_APPLICABLE"
	if j := cibusiness.Score(cibusiness.Metrics[1], ref, actual); j.Score != nil || j.Applicability != "NOT_APPLICABLE" {
		t.Fatal(j)
	}
	ref.LabelStatus = "UNREVIEWED"
	if j := cibusiness.Score(cibusiness.Metrics[0], ref, actual); j.Score != nil || j.Reason != "UNREVIEWED_REFERENCE" {
		t.Fatal(j)
	}
	badIdentity = true
	outcome, e = target.Execute(ctx, scope, q)
	if e != nil || outcome.Kind == ev.Success {
		t.Fatal("bad receipt accepted", e)
	}
	observations.Records[0].SHA256 = strings.Repeat("0", 64)
	q.Case.Case.Input, _ = asset.Freeze(observations)
	outcome, _ = target.Execute(ctx, scope, q)
	if outcome.Kind == ev.Success || calls != 2 {
		t.Fatal("bad source dispatched")
	}
}
