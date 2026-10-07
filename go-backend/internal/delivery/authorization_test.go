package delivery

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	"strings"
	"testing"
	"time"
)

func TestGatePermissionProjection(t *testing.T) {
	d := strings.Repeat("a", 64)
	project := asset.NewID()
	g := GateBinding{GateID: asset.NewID(), Decision: decision.Pass, ReceiptDigest: d, DatasetVersion: "CONTROLLED_PASS_FIXTURE", BaselineSubject: d, CandidateSubject: d, ComparisonDigest: d, SnapshotDigest: d, PolicyVersion: "TEST_SCOPE", PolicyDigest: d}
	o := OutcomeBinding{CaseID: asset.NewID(), CaseVersion: "v1", EvaluationRunID: asset.NewID(), AnchorRunID: asset.NewID(), SelectedRunID: asset.NewID(), SubjectDigest: d, AnswerDigest: d, ReceiptDigest: d}
	dest := Destination{Kind: SinkKind, Scope: "TEST_SCOPE", ID: "fixture"}
	for _, decision := range []decision.Decision{decision.Pass, decision.Fail, decision.Blocked} {
		gate := g
		gate.Decision = decision
		q := Request{Gate: gate, Outcome: o, Destination: dest}
		a := Project(project, time.Now(), q, gate, o, []string{"result"})
		if (a.Status == "AUTHORIZED") != (decision == "PASS") || a.Digest != a.ContentDigest() {
			t.Fatal(a)
		}
	}
	q := Request{Gate: g, Outcome: o, Destination: dest}
	for _, field := range []string{"gate", "subject", "output", "receipt", "comparison", "snapshot", "destination"} {
		t.Run(field, func(t *testing.T) {
			bad := q
			switch field {
			case "gate":
				bad.Gate = GateBinding{Decision: "PASS"}
			case "subject":
				bad.Outcome.SubjectDigest = strings.Repeat("b", 64)
			case "output":
				bad.Outcome.AnswerDigest = strings.Repeat("b", 64)
			case "receipt":
				bad.Outcome.ReceiptDigest = strings.Repeat("b", 64)
			case "comparison":
				bad.Gate.ComparisonDigest = strings.Repeat("b", 64)
			case "snapshot":
				bad.Gate.SnapshotDigest = strings.Repeat("b", 64)
			case "destination":
				bad.Destination.Scope = "PRODUCTION"
			}
			if Project(project, time.Now(), bad, g, o, []string{"result"}).Status != "DENIED" {
				t.Fatal("invalid binding authorized")
			}
		})
	}
	if Project(project, time.Now(), Request{Gate: GateBinding{Decision: "PASS"}, Destination: dest}, GateBinding{Decision: "PASS"}, OutcomeBinding{}, []string{"result"}).Status != "DENIED" {
		t.Fatal("PASS string became authority")
	}
	first := Project(project, time.Now(), q, g, o, []string{"result"})
	q.Gate.GateID = asset.NewID()
	next := Project(project, first.IssuedAt, q, q.Gate, o, []string{"result"})
	if first.Digest == next.Digest || Identity(q) == Identity(Request{Gate: g, Outcome: o, Destination: dest}) {
		t.Fatal("gate change reused identity")
	}
}
