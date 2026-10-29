package review

import (
	"agentevalops/go-backend/internal/asset"
	"testing"
)

func feedbackFixture() FeedbackProjection {
	o := FeedbackObservation{"UNAVAILABLE", "UNAVAILABLE", "UNAVAILABLE"}
	j, _ := asset.Freeze(o)
	return FeedbackProjection{Contract: FeedbackContract, FeedbackID: asset.NewID(), IncidentID: asset.NewID(), InvestigationID: asset.NewID(), RunID: asset.NewID(), Owner: asset.NewID(), Tenant: "test", ReportVersion: 1, ReportDigest: j.Digest(), RuntimeDigest: j.Digest(), EvidenceDigest: j.Digest(), Decision: "REJECT", Status: "ACTIVE", Source: "SYNTHETIC", Trust: "HUMAN", ReviewedAt: "2026-10-29T00:00:00Z", Observations: o, Attribution: FeedbackAttribution{Observation: []string{"OBSERVED_OUTCOME_DISAGREEMENT"}, Supporting: []string{"feedback-decision"}, Contradicting: []string{}, Causes: []string{}, Component: "UNKNOWN", Uncertainty: []string{"ACTUAL_CONTEXT_CAPTURE_UNAVAILABLE"}, Verification: "INCONCLUSIVE", Action: "INDEPENDENT_HUMAN_REVIEW"}}
}
func feedbackRaw(p FeedbackProjection) []byte {
	p.RevisionID = p.FeedbackID
	j, _ := asset.Freeze(p)
	w, _ := asset.Freeze(FeedbackWire{j, j.Digest()})
	return w.Bytes()
}
func TestBusinessFeedbackStrictProjection(t *testing.T) {
	p := feedbackFixture()
	raw := feedbackRaw(p)
	if _, _, e := DecodeFeedback(raw); e != nil {
		t.Fatal(e)
	}
	var w FeedbackWire
	j, _ := asset.ParseJSON(raw)
	_ = j.Decode(&w)
	w.Digest = "wrong"
	bad, _ := asset.Freeze(w)
	if _, _, e := DecodeFeedback(bad.Bytes()); e == nil {
		t.Fatal("digest mismatch accepted")
	}
	p.Attribution.Verification = "VERIFIED_ROOT_CAUSE"
	if _, _, e := DecodeFeedback(feedbackRaw(p)); e == nil {
		t.Fatal("user opinion became verified GT")
	}
}
func TestBusinessFeedbackSourceCompatibility(t *testing.T) {
	old := SourceRef{Type: "GATE_DECISION", GateID: asset.NewID()}
	j, _ := asset.Freeze(old)
	var fields map[string]any
	_ = j.Decode(&fields)
	if _, ok := fields["FeedbackID"]; ok {
		t.Fatal("legacy canonical source changed")
	}
	if old.Validate() != nil {
		t.Fatal("old source rejected")
	}
	ref := SourceRef{Type: "BUSINESS_FEEDBACK", FeedbackID: asset.NewID()}
	if ref.Validate() != nil {
		t.Fatal("new source rejected")
	}
	ref.RunID = asset.NewID()
	if ref.Validate() == nil {
		t.Fatal("mixed authority accepted")
	}
}
