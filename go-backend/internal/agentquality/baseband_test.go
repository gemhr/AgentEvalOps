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

func TestBasebandDiagnosisScoring(t *testing.T) {
	raw, e := os.ReadFile("../../testdata/baseband_development.json")
	if e != nil {
		t.Fatal(e)
	}
	var samples struct {
		Version   string `json:"version"`
		Evaluator string `json:"evaluator_version"`
		Cases     []struct {
			Key   string        `json:"case_key"`
			Input asset.JSON    `json:"input"`
			GT    BasebandTruth `json:"ground_truth"`
		} `json:"cases"`
	}
	if json.Unmarshal(raw, &samples) != nil || len(samples.Cases) != 3 {
		t.Fatal("development cases missing")
	}
	for _, sample := range samples.Cases {
		gt := sample.GT
		r := BasebandReport{Schema: "baseband-diagnosis.v1", IncidentID: "incident", ExecutionID: "execution", InputDigest: sample.Input.Digest(), Summary: "test", Decidability: gt.Decidability, Uncertainties: []string{}, Findings: []BasebandFinding{{Category: gt.Category, Mechanism: gt.Mechanism, Owner: gt.Owner, Action: gt.Action, Support: []BasebandReference{}, Counter: []BasebandReference{}, Uncertainties: []string{}}}}
		if gt.InputDigest != sample.Input.Digest() {
			t.Fatal("frozen truth changed")
		}
		for _, pass := range BasebandScore(r, gt) {
			if !pass {
				t.Fatal("correct report rejected")
			}
		}
		r.Findings[0].Mechanism = "WRONG_MECHANISM"
		if BasebandScore(r, gt)["mechanism"] {
			t.Fatal("wrong mechanism accepted")
		}
		r.Findings[0].Mechanism = gt.Mechanism
		r.Findings[0].Action = "OVERWRITE_ORACLE"
		if BasebandScore(r, gt)["safe_action"] {
			t.Fatal("unsafe action accepted")
		}
		r.Findings[0].Action = gt.Action
		if gt.Decidability == "INSUFFICIENT_EVIDENCE" {
			r.Uncertainties = []string{"missing receipt"}
		}
		// 造假来源必须在传输/执行完整性层失败，不能当业务错误报告判 PASS。
		receipt := BasebandReceipt{Protocol: BasebandOutput, ProjectID: "p", RunID: "r", AttemptID: "a", RequestID: "q", CaseID: "c", Input: sample.Input, InputDigest: sample.Input.Digest(), Report: r, Source: BasebandSource{Kind: "AGENT", IncidentID: "other"}}
		if receipt.Validate(sample.Input, BasebandSelector{IncidentID: "incident", Kind: "AGENT"}, "p", "r", "a", "q", "c") == nil {
			t.Fatal("unbound source accepted")
		}
	}
}

func TestBasebandNumericScorePreservesNullAndRefusal(t *testing.T) {
	raw, e := os.ReadFile("../../testdata/baseband_development.json")
	if e != nil {
		t.Fatal(e)
	}
	var samples struct {
		Cases []struct {
			Input asset.JSON    `json:"input"`
			GT    BasebandTruth `json:"ground_truth"`
		} `json:"cases"`
	}
	if json.Unmarshal(raw, &samples) != nil {
		t.Fatal("public cases invalid")
	}
	for index, sample := range samples.Cases {
		report := BasebandReport{Schema: "baseband-diagnosis.v1", IncidentID: "incident", ExecutionID: "execution", InputDigest: sample.Input.Digest(), Summary: "public fixture", Decidability: sample.GT.Decidability, Uncertainties: []string{}, Findings: []BasebandFinding{{Category: sample.GT.Category, Mechanism: sample.GT.Mechanism, Owner: sample.GT.Owner, Action: sample.GT.Action, Counter: []BasebandReference{}, Uncertainties: []string{}}}}
		receipt := BasebandReceipt{Protocol: BasebandOutput, ProjectID: "p", RunID: "r", AttemptID: "a", RequestID: "q", CaseID: "case", Input: sample.Input, InputDigest: sample.Input.Digest(), Source: BasebandSource{Kind: "RULE", IncidentID: "incident", ExecutionID: "execution", InputDigest: sample.Input.Digest(), RuleVersion: "baseband-rule.v1"}}
		for n, kind := range []string{"CI_SUMMARY", "INPUT_MANIFEST", "CONSTRAINT_CHECK", "EXECUTION_RECEIPT"} {
			content := diagnosisJSON(map[string]string{"public": kind})
			if kind == "INPUT_MANIFEST" {
				content = sample.Input
			}
			d := content.Digest()
			item := BasebandEvidence{ID: string(rune('0' + n)), ExecutionID: "execution", Kind: kind, Captured: "fixture", Availability: "AVAILABLE", Media: "application/json", Digest: &d, Content: content}
			if kind == "EXECUTION_RECEIPT" && sample.GT.Decidability == "INSUFFICIENT_EVIDENCE" {
				item.Availability = "MISSING"
				item.Digest = nil
				item.Content = asset.JSON{}
				report.Uncertainties = []string{"receipt missing"}
			} else {
				report.Findings[0].Support = append(report.Findings[0].Support, BasebandReference{ID: item.ID, Digest: d})
			}
			receipt.Evidence = append(receipt.Evidence, item)
		}
		receipt.Source.EvidenceDigest = diagnosisJSON(receipt.Evidence).Digest()
		var in worker.EvaluationInput
		in.Scope.ProjectID = "p"
		in.Attempt.ID = "a"
		in.Attempt.RunID = "r"
		in.Attempt.RequestID = "q"
		in.Attempt.Outcome = ev.Success
		in.Attempt.Request.Case.Identity.Ref = asset.Ref{EntityID: "case", Version: "v1"}
		in.Attempt.Request.Case.Case.Input = sample.Input
		in.Attempt.Request.Case.Case.GroundTruth = diagnosisJSON(sample.GT)
		in.Run.Snapshot.Subject = diagnosisJSON(BasebandSubject{Subject: "baseband", Selectors: map[string]BasebandSelector{"case": {IncidentID: "incident", Kind: "RULE"}}})
		in.Work.Metadata.Spec.Definition.ImplementationRef = BasebandImplementationV2
		bind := func() {
			receipt.Report = report
			receipt.ReportDigest = diagnosisJSON(report).Digest()
			body := diagnosisJSON(receipt)
			in.Attempt.Metadata.Observation.Artifact = &ev.Binding{ProjectID: "p", RunID: "r", AttemptID: "a", RequestID: "q", Ref: "baseband://public-fixture", Schema: BasebandOutput, Availability: "AVAILABLE", Body: body, Digest: body.Digest()}
		}
		check := func(want *float64, verdict string) {
			t.Helper()
			out, e := evaluateBaseband(context.Background(), in)
			if e != nil || out.Value.Verdict != verdict || (want == nil) != (out.Value.Score == nil) || (want != nil && *out.Value.Score != *want) {
				t.Fatalf("score=%v verdict=%s error=%v", out.Value.Score, out.Value.Verdict, e)
			}
		}
		one, zero := 1.0, 0.0
		bind()
		check(&one, "PASS")
		if index == 0 {
			report.Findings[0].Mechanism = "WRONG_MECHANISM"
			bind()
			check(&zero, "FAIL")
			report.Findings[0].Mechanism = sample.GT.Mechanism
			bind()
			in.Attempt.Metadata.Observation.Artifact = nil
			check(nil, "INCONCLUSIVE")
			bind()
			gt := in.Attempt.Request.Case.Case.GroundTruth
			in.Attempt.Request.Case.Case.GroundTruth = asset.JSON{}
			check(nil, "INCONCLUSIVE")
			in.Attempt.Request.Case.Case.GroundTruth = gt
			in.Work.Metadata.Spec.Definition.ImplementationRef = BasebandImplementation
			check(nil, "PASS")
		}
	}
}
