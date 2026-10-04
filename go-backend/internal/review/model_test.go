package review

import (
	"testing"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
)

func TestTypedJudgmentAndIndependentConsensus(t *testing.T) {
	evidence := []ev.Binding{{Ref: "answer"}}
	for _, kind := range []DecisionKind{TaskSuccess, Quality, Preference, FailureCategory} {
		schema := metric.Definition{ValueType: metric.Enum, Labels: Labels(kind)}
		for _, v := range Labels(kind) {
			j := Judgment{Kind: kind, Value: v, Reason: "人工理由", EvidenceRefs: []string{"answer"}}
			if j.Validate(schema, evidence) != nil {
				t.Fatal(kind, v)
			}
		}
		bad := Judgment{Kind: kind, Value: "good", Reason: "理由", EvidenceRefs: []string{"answer"}}
		if bad.Validate(schema, evidence) == nil {
			t.Fatal("untyped label accepted")
		}
		bad.Value = Labels(kind)[0]
		bad.EvidenceRefs = []string{"not-presented"}
		if bad.Validate(schema, evidence) == nil {
			t.Fatal("unseen evidence accepted")
		}
	}
	a := Annotation{Reviewer: Reviewer{ID: "A"}, Decision: Judgment{Kind: Quality, Value: "PASS"}}
	b := a
	if Consensus(a, b) {
		t.Fatal("same reviewer cannot form consensus")
	}
	b.Reviewer.ID = "B"
	if !Consensus(a, b) {
		t.Fatal("independent agreement missing")
	}
	b.Decision.Value = "FAIL"
	if Consensus(a, b) {
		t.Fatal("disagreement suppressed")
	}
	if (Protocol{Blind: true, JudgeShown: true}).Validate() == nil {
		t.Fatal("blindness inconsistent")
	}
	s := Scope{Scope: ev.Scope{Scope: asset.Scope{ProjectID: asset.NewID(), OrganizationID: asset.NewID(), Principal: "reviewer"}}, ReviewerType: "HUMAN", IdentitySource: "CONTROLLED_TEST_REVIEWER"}
	if s.ValidateReviewer() != nil || s.Reviewer().ID != "reviewer" {
		t.Fatal("principal binding")
	}
	s.ReviewerType = "body-trusted"
	if s.ValidateReviewer() == nil {
		t.Fatal("uncontrolled type accepted")
	}
	ref := SourceRef{Type: "OFFLINE_RESULT", RunID: asset.NewID(), ResultID: asset.NewID(), GateID: asset.NewID()}
	if ref.Validate() == nil {
		t.Fatal("irrelevant source field bypasses deduplication")
	}
}
func TestCalibrationAgreementCoverageAndEvaluatorRegression(t *testing.T) {
	ref := func() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "v1"} }
	evaluator, schema, dataset := ref(), ref(), ref()
	protocol := Protocol{Blind: true}
	sampling := Sampling{Version: "review-hash.v1", Seed: "frozen", BasisPoints: 10000}
	command := CalibrationCommand{ID: asset.NewID(), Dataset: dataset, Evaluator: evaluator, Schema: schema, Protocol: protocol, Sampling: sampling, PositiveClass: "SUCCESS"}
	snapshot := CalibrationSnapshot{ID: command.ID, ProjectID: asset.NewID(), Command: command, EvaluatorDigest: "e", GoldenSetDigest: "gold", SchemaDigest: "schema", DatasetDigest: "ds"}
	for _, labels := range [][2]string{{"SUCCESS", "SUCCESS"}, {"SUCCESS", "FAILURE"}, {"FAILURE", "SUCCESS"}, {"FAILURE", "FAILURE"}, {"INCONCLUSIVE", "SUCCESS"}, {"NOT_APPLICABLE", "INCONCLUSIVE"}, {"", ""}} {
		c := ref()
		command.Samples = append(command.Samples, CalibrationSample{Case: c})
		p := CalibrationPair{Case: c}
		if labels[0] != "" {
			p.Golden = &GoldenLabel{Schema: schema, Protocol: protocol, EvidenceDigest: "evidence", Decision: Judgment{Value: labels[0]}}
		}
		if labels[1] != "" {
			p.Judge = &Automatic{Evaluator: evaluator, EvaluatorDigest: "e", EvidenceDigest: "evidence", Decision: labels[1]}
		}
		snapshot.Pairs = append(snapshot.Pairs, p)
	}
	snapshot.Command = command
	badCommand := command
	badCommand.PositiveClass = "INCONCLUSIVE"
	if badCommand.Validate() == nil {
		t.Fatal("undecidable positive class accepted")
	}
	r, e := Compute(snapshot)
	if e != nil {
		t.Fatal(e)
	}
	a := r.Agreement
	if a.N != 7 || a.PairedDecidable != 4 || a.Matches != 2 || a.Disagreements != 2 || a.FalsePositive != 1 || a.FalseNegative != 1 || len(a.Confusion) != 4 || a.MissingHuman != 1 || a.HumanUndecidable != 2 || a.AgreementRate == nil || *a.AgreementRate != .5 {
		t.Fatalf("incorrect denominator/matrix: %+v", a)
	}
	if a.Undecidable["human:NOT_APPLICABLE"] != 1 {
		t.Fatal("N/A entered binary matrix")
	}
	candidate := r
	candidate.ID = asset.NewID()
	candidate.Evaluator.Version = "v2"
	candidate.Agreement.Matches = 3
	v := .75
	candidate.Agreement.AgreementRate = &v
	regression := CompareEvaluators(r, candidate)
	if regression.Status != "COMPARABLE" || regression.AgreementDelta == nil || *regression.AgreementDelta != .25 {
		t.Fatal(regression)
	}
	candidate.Protocol = Protocol{JudgeShown: true}
	if CompareEvaluators(r, candidate).Status != "INCOMPARABLE" {
		t.Fatal("blind/non-blind mixed")
	}
	candidate.Protocol = r.Protocol
	candidate.Pairs = append([]CalibrationPair(nil), r.Pairs...)
	judge := *candidate.Pairs[0].Judge
	judge.EvidenceDigest = "different"
	candidate.Pairs[0].Judge = &judge
	if CompareEvaluators(r, candidate).Status != "INCOMPARABLE" {
		t.Fatal("different evidence compared")
	}
	snapshot.Pairs[0].Judge.ProviderError = true
	snapshot.Pairs[0].Judge.InvalidOutput = true
	snapshot.Pairs[0].Judge.IdentityDrift = true
	snapshot.Pairs[0].Judge.Decision = "INCONCLUSIVE"
	errorsReport, e := Compute(snapshot)
	if e != nil || errorsReport.Agreement.ProviderErrors != 1 || errorsReport.Agreement.InvalidOutput != 1 || errorsReport.Agreement.IdentityDrift != 1 || errorsReport.Agreement.PairedDecidable != 3 {
		t.Fatal("provider/invalid/identity error coverage incorrect", e)
	}
	snapshot.Pairs[0].Judge.EvidenceDigest = "different"
	if _, e = Compute(snapshot); e == nil {
		t.Fatal("evidence drift accepted")
	}
}
func TestSamplingAndSanitizedDraftReadiness(t *testing.T) {
	p := Sampling{Version: "review-hash.v1", Seed: "immutable", BasisPoints: 5000}
	ref := SourceRef{Type: "OFFLINE_RESULT", RunID: asset.NewID(), ResultID: asset.NewID()}
	if p.Selected("project", ref) != p.Selected("project", ref) {
		t.Fatal("sampling changed")
	}
	p.BasisPoints = 0
	if p.Selected("project", ref) {
		t.Fatal("0% selected")
	}
	p.BasisPoints = 10000
	if !p.Selected("project", ref) {
		t.Fatal("100% missing")
	}
	input, _ := asset.ParseJSON([]byte(`{"query":"人工脱敏输入"}`))
	body := catalog.CaseContent{Input: input, TaskGoal: "目标", AcceptanceCriteria: []string{"验收"}, Type: catalog.Regression, Capability: "task", Criticality: catalog.Normal, BodyPolicy: catalog.Redacted, Applicability: asset.Applicability{RuleRef: "human-reviewed.v1"}}
	d := Draft{Case: asset.Ref{EntityID: asset.NewID(), Version: "v1"}, Body: body, Sanitization: "REDACTED", Policy: "explicit-human-sanitization.v1", Reason: "已人工移除敏感信息"}
	item := Item{Status: "COMPLETED", Source: Source{BodyAvailability: "UNAVAILABLE"}}
	if d.Ready(item) == nil {
		t.Fatal("unavailable body silently copied")
	}
	d.HumanSupplement = true
	if d.Ready(item) != nil {
		t.Fatal("explicit human supplement refused")
	}
	d.Sanitization = "NOT_REVIEWED"
	if d.Ready(item) == nil {
		t.Fatal("unreviewed draft published")
	}
	item.Status = "IN_REVIEW"
	d.Sanitization = "REDACTED"
	if d.Ready(item) == nil {
		t.Fatal("review incomplete")
	}
}
