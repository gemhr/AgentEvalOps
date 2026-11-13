package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/worker"
	"context"
	"encoding/json"
	"os"
	"testing"
)

func TestJointSemanticSetsAndSourceBoundary(t *testing.T) {
	raw, e := os.ReadFile("../../testdata/oracle_chain_projection.json")
	if e != nil {
		t.Fatal(e)
	}
	var vectors []struct {
		Input    asset.JSON         `json:"input"`
		GT       JointTruth         `json:"ground_truth"`
		Report   JointReport        `json:"report"`
		Evidence []BasebandEvidence `json:"evidence"`
		Digest   string             `json:"evidence_digest"`
	}
	if json.Unmarshal(raw, &vectors) != nil {
		t.Fatal("invalid public Python projection")
	}
	for i, v := range vectors {
		receipt := JointReceipt{Protocol: JointOutput, ProjectID: "p", RunID: "r", AttemptID: "a", RequestID: "q", CaseID: "case", Input: v.Input, InputDigest: v.Input.Digest(), Report: v.Report, Evidence: v.Evidence, Source: JointSource{BasebandSource: BasebandSource{Kind: "RULE", IncidentID: "incident", ExecutionID: "execution", InputDigest: v.Input.Digest(), EvidenceDigest: v.Digest, RuleVersion: "baseband-rule.v2"}, Reads: []ObservedRead{}}}
		var in worker.EvaluationInput
		in.Scope.ProjectID = "p"
		in.Attempt.ID = "a"
		in.Attempt.RunID = "r"
		in.Attempt.RequestID = "q"
		in.Attempt.Outcome = ev.Success
		in.Attempt.Request.Case.Identity.Ref = asset.Ref{EntityID: "case", Version: "v1"}
		in.Attempt.Request.Case.Case.Input = v.Input
		in.Attempt.Request.Case.Case.GroundTruth = diagnosisJSON(v.GT)
		in.Run.Snapshot.Subject = diagnosisJSON(BasebandSubject{Subject: "baseband", Version: "baseband-rule.v2", Selectors: map[string]BasebandSelector{"case": {IncidentID: "incident", Kind: "RULE"}}})
		bind := func() {
			receipt.ReportDigest = diagnosisJSON(receipt.Report).Digest()
			body := diagnosisJSON(receipt)
			in.Attempt.Metadata.Observation.Artifact = &ev.Binding{ProjectID: "p", RunID: "r", AttemptID: "a", RequestID: "q", Ref: "baseband://fixture", Schema: JointOutput, Availability: "AVAILABLE", Body: body, Digest: body.Digest()}
		}
		check := func(score *float64, verdict string) {
			t.Helper()
			out, e := evaluateJoint(context.Background(), in)
			if e != nil || out.Value.Verdict != verdict || (score == nil) != (out.Value.Score == nil) || (score != nil && *score != *out.Value.Score) {
				t.Fatalf("case %d score=%v verdict=%s error=%v", i, out.Value.Score, out.Value.Verdict, e)
			}
		}
		one, zero := 1.0, 0.0
		bind()
		check(&one, "PASS")
		if len(receipt.Report.Findings) > 1 {
			receipt.Report.Findings[0], receipt.Report.Findings[1] = receipt.Report.Findings[1], receipt.Report.Findings[0]
			bind()
			check(&one, "PASS")
			original := append([]JointFinding{}, receipt.Report.Findings...)
			receipt.Report.Findings = receipt.Report.Findings[:1]
			bind()
			check(&zero, "FAIL")
			receipt.Report.Findings = original
			receipt.Report.Findings[0].Status = "CONFIRMED"
			bind()
			check(&zero, "FAIL")
			receipt.Report = v.Report
		}
		receipt.Report.Decidability = "INSUFFICIENT_EVIDENCE"
		receipt.Report.Uncertainties = []string{"unjustified refusal"}
		if v.GT.Decidability == "INSUFFICIENT_EVIDENCE" {
			receipt.Report.Decidability = "SUPPORTED"
		}
		bind()
		check(&zero, "FAIL")
		receipt.Report = v.Report
		bind()
		in.Attempt.Metadata.Observation.Artifact = nil
		check(nil, "INCONCLUSIVE")
		bind()
		in.Attempt.Request.Case.Case.GroundTruth = asset.JSON{}
		check(nil, "INCONCLUSIVE")
		var old BasebandReceipt
		if diagnosisJSON(receipt).Decode(&old) == nil && old.Validate(v.Input, BasebandSelector{IncidentID: "incident", Kind: "RULE"}, "p", "r", "a", "q", "case") == nil {
			t.Fatal("v2 leaked into v1 decoder")
		}
	}
}
