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

func TestCoverageExecutionFiveChecksAndNull(t *testing.T) {
	raw, e := os.ReadFile("../../testdata/coverage_execution_vector.json")
	if e != nil {
		t.Fatal(e)
	}
	var f struct {
		Kind     string           `json:"fixture_kind"`
		Input    asset.JSON       `json:"input"`
		GT       CoverageTruth    `json:"ground_truth"`
		Selector CoverageSelector `json:"selector"`
		Receipt  CoverageReceipt  `json:"receipt"`
	}
	if json.Unmarshal(raw, &f) != nil || f.Kind != "PURE_CONTRACT_VECTOR_NOT_PERSISTED" {
		t.Fatal("public fixture invalid")
	}
	var in worker.EvaluationInput
	in.Scope.ProjectID = "p"
	in.Attempt.ID = "a"
	in.Attempt.RunID = "r"
	in.Attempt.RequestID = "q"
	in.Attempt.Outcome = ev.Success
	in.Attempt.Request.Case.Identity.Ref = asset.Ref{EntityID: "case", Version: "v1"}
	in.Attempt.Request.Case.Case.Input = f.Input
	in.Attempt.Request.Case.Case.GroundTruth = coverageJSON(f.GT)
	in.Run.Snapshot.Subject = coverageJSON(CoverageSubject{Mode: "SEALED_BUSINESS_REPLAY", Selectors: map[string]CoverageSelector{"case": f.Selector}})
	bind := func(receipt CoverageReceipt) {
		body := coverageJSON(receipt)
		in.Attempt.Metadata.Observation.Artifact = &ev.Binding{ProjectID: "p", RunID: "r", AttemptID: "a", RequestID: "q", Ref: "coverage-execution://fixture", Schema: CoverageOutput, Availability: "AVAILABLE", Body: body, Digest: body.Digest()}
	}
	check := func(score *float64, verdict string) {
		t.Helper()
		out, e := evaluateCoverage(context.Background(), in)
		if e != nil || out.Value.Verdict != verdict || (score == nil) != (out.Value.Score == nil) || (score != nil && *score != *out.Value.Score) {
			t.Fatalf("score=%v verdict=%s reason=%s err=%v", out.Value.Score, out.Value.Verdict, out.Value.Reason, e)
		}
	}
	one, zero := 1.0, 0.0
	bind(f.Receipt)
	check(&one, "PASS")
	checks, counts := CoverageScore(f.Receipt, f.GT)
	if len(checks) != 5 || counts["matched"] != 1 {
		t.Fatal(checks, counts)
	}
	b := coverageObjects(f.Receipt.Business)
	plan := coverageObject(b["plan"])
	original := plan["tests"]
	plan["tests"] = []any{}
	changed := f.Receipt
	changed.Business = coverageJSON(b)
	checks, counts = CoverageScore(changed, f.GT)
	if checks["coverage"] || counts["missing"] != 1 {
		t.Fatal("missing gain coverage accepted")
	}
	plan["tests"] = original
	a := coverageObject(b["case_asset"])
	originalTag := a["tag_ref"]
	a["tag_ref"] = map[string]any{"id": "sampling-gain", "version": "1"}
	changed.Business = coverageJSON(b)
	checks, _ = CoverageScore(changed, f.GT)
	if checks["asset"] {
		t.Fatal("wrong Tag accepted")
	}
	a["tag_ref"] = originalTag
	joint := coverageObject(b["joint"])
	report := coverageObject(joint["report"])
	finding := coverageObject(coverageList(report["candidate_findings"])[0])
	finding["mechanism"] = "APPLICATION_UNCONFIRMED"
	finding["finding_status"] = "CANDIDATE"
	finding["failure_relation"] = "UNRESOLVED"
	joint["report_digest"] = coverageJSON(report).Digest()
	changed.Business = coverageJSON(b)
	changed.Digest = changed.Business.Digest()
	bind(changed)
	check(&zero, "FAIL")
	bind(f.Receipt)
	bad := f.Selector
	bad.ExecutionID = "other"
	in.Run.Snapshot.Subject = coverageJSON(CoverageSubject{Mode: "SEALED_BUSINESS_REPLAY", Selectors: map[string]CoverageSelector{"case": bad}})
	check(nil, "INCONCLUSIVE")
	in.Run.Snapshot.Subject = coverageJSON(CoverageSubject{Mode: "SEALED_BUSINESS_REPLAY", Selectors: map[string]CoverageSelector{"case": f.Selector}})
	in.Attempt.Metadata.Observation.Artifact = nil
	check(nil, "INCONCLUSIVE")
	bind(f.Receipt)
	in.Attempt.Request.Case.Case.GroundTruth = asset.JSON{}
	check(nil, "INCONCLUSIVE")
}
