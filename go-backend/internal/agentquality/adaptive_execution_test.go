package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/worker"
	"context"
	"os"
	"testing"
)

func TestAdaptiveProviderAuthoritativeTotalTokens(t *testing.T) {
	raw, err := os.ReadFile("testdata/adaptive_diagnosis_public.json")
	if err != nil {
		t.Fatal(err)
	}
	body, err := asset.ParseJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	evidence, err := DecodeAdaptive(body)
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name  string
		usage AdaptiveUsage
		valid bool
	}{
		{"total_equals_split", AdaptiveUsage{Input: 7, Output: 3, Total: 10}, true},
		{"provider_extra_tokens", AdaptiveUsage{Input: 7, Output: 3, Total: 12}, true},
		{"total_below_split", AdaptiveUsage{Input: 7, Output: 3, Total: 9}, false},
		{"negative_input", AdaptiveUsage{Input: -1, Output: 3, Total: 12}, false},
		{"negative_output", AdaptiveUsage{Input: 7, Output: -1, Total: 12}, false},
		{"negative_total", AdaptiveUsage{Input: 7, Output: 3, Total: -1}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// 在原公开完整证据上构造 Usage 合同输入，不改写 producer 向量。
			sample := evidence
			sample.Metrics.Usage = &tc.usage
			sample.Metrics.UsageAvailable = sample.Metrics.Models
			wire, err := asset.Freeze(sample)
			if err != nil {
				t.Fatal(err)
			}
			got, err := DecodeAdaptive(wire)
			if !tc.valid {
				if err == nil {
					t.Fatal("非法 Provider Usage 被接受")
				}
				return
			}
			if err != nil {
				t.Fatalf("合法 Provider Usage 被拒绝: %v", err)
			}
			if got.Metrics.Usage == nil || *got.Metrics.Usage != tc.usage {
				t.Fatalf("Provider 原始 Usage 被改写: %+v", got.Metrics.Usage)
			}
			if got.Metrics.UsageAvailable != sample.Metrics.Models || got.Runtime.Status != "SUCCEEDED" || got.Metrics.Cost != nil {
				t.Fatal("Usage 覆盖率、原终态或费用缺失语义被改变")
			}
		})
	}
	if evidence.Metrics.Usage != nil || evidence.Metrics.UsageAvailable != 0 || evidence.Metrics.Cost != nil {
		t.Fatal("原公开证据的 Usage/费用缺失语义被改变")
	}
}

func TestAdaptiveDiagnosisStopAndGroundTruthSemantics(t *testing.T) {
	failedRaw, e := os.ReadFile("testdata/adaptive_failed_public.json")
	if e != nil {
		t.Fatal(e)
	}
	failedJSON, e := asset.ParseJSON(failedRaw)
	if e != nil {
		t.Fatal(e)
	}
	failed, e := DecodeAdaptive(failedJSON)
	if e != nil || failed.Runtime.Status != "FAILED" || failed.Business.Output != nil || failed.Metrics.Models < 1 {
		t.Fatal("known persisted failure lost", e)
	}
	// 此公开向量由 Python 定向测试的真实 PG 调查导出；它不替代 HTTP/Worker 集成。
	raw, e := os.ReadFile("testdata/adaptive_diagnosis_public.json")
	if e != nil {
		t.Fatal(e)
	}
	j, e := asset.ParseJSON(raw)
	if e != nil {
		t.Fatal(e)
	}
	evidence, e := DecodeAdaptive(j)
	if e != nil {
		t.Fatal("real persisted stop rejected", e)
	}
	stop := evidence.Mode.Loop.Frames[0].Stop
	if stop == nil || stop.Reason != "COVERAGE_SUFFICIENT" || evidence.Metrics.Models != 4 {
		t.Fatal("lost original stop/model facts")
	}
	bad := *stop
	bad.DecisionID = bad.EvidenceDigest
	evidence.Mode.Loop.Frames[0].Stop = &bad
	altered, _ := asset.Freeze(evidence)
	if _, e = DecodeAdaptive(altered); e == nil {
		t.Fatal("fabricated decision accepted")
	}
	evidence.Mode.Loop.Frames[0].Stop = stop
	firstAction := evidence.Mode.Loop.Actions[0]
	evidence.Mode.Loop.Actions[0].Iteration = stop.EffectiveIteration
	altered, _ = asset.Freeze(evidence)
	if _, e = DecodeAdaptive(altered); e == nil {
		t.Fatal("tool start after FINAL_ONLY accepted")
	}
	evidence.Mode.Loop.Actions[0] = firstAction
	// 单元测试使用明确的新输入并同步身份；不声称这些修改是远端执行事实。
	input, _ := asset.Freeze(map[string]string{"public": "unit-quality"})
	evidence.Identity.InputDigest = input.Digest()
	evidence.Comparability.Input = input.Digest()
	id := evidence.Identity
	metric := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	in := worker.EvaluationInput{}
	in.Scope.ProjectID = id.ProjectID
	in.Run.Snapshot.Input.Dataset = &ev.AssetIdentity{ContentDigest: id.DatasetDigest}
	in.Attempt = ev.Attempt{ProjectID: id.ProjectID, ID: id.AttemptID, RunID: id.EvaluationRunID, RequestID: id.RequestID, CaseID: id.CaseID, CaseVersion: id.CaseVersion, Outcome: ev.Success}
	in.Attempt.Request.Case.Identity = ev.AssetIdentity{ProjectID: id.ProjectID, Ref: asset.Ref{EntityID: id.CaseID, Version: id.CaseVersion}, ContentDigest: id.CaseDigest}
	in.Attempt.Request.Case.Case.Input = input
	in.Work.Metadata.Spec.Definition = AdaptiveEvaluatorDefinition(metric, "quality")
	evaluate := func(v AdaptiveEvidence) ev.ResultValue {
		t.Helper()
		body, _ := asset.Freeze(v)
		if _, e := DecodeAdaptive(body); e != nil {
			t.Fatal("unit wire invalid", e)
		}
		in.Attempt.Metadata.Observation.Evidence = []ev.Binding{{ProjectID: id.ProjectID, RunID: id.EvaluationRunID, AttemptID: id.AttemptID, RequestID: id.RequestID, Ref: "public-adaptive://unit", Schema: AdaptiveSchema, Availability: "AVAILABLE", Body: body, Digest: body.Digest()}}
		out, e := (DeterministicEvaluator{}).Evaluate(context.Background(), in)
		if e != nil {
			t.Fatal(e)
		}
		return out.Value
	}
	unknown := evaluate(evidence)
	if unknown.Score != nil || unknown.Verdict != "INCONCLUSIVE" {
		t.Fatalf("SUPPORTED without GT: verdict=%s score=%v reason=%s", unknown.Verdict, unknown.Score, unknown.Reason)
	}
	var report diagnosisReport
	_ = StrictLoopShape([]byte(*evidence.Business.Output), &report)
	in.Attempt.Request.Case.Case.GroundTruth, _ = asset.Freeze(map[string]string{"root_cause_hypothesis": report.Hypothesis})
	known := evaluate(evidence)
	if known.Score == nil || *known.Score != 1 {
		t.Fatalf("frozen GT mismatch %+v", known)
	}
	if decision.TaskDecision(decision.MetricFact{ResultID: asset.NewID(), Verdict: known.Verdict, Provenance: known.Provenance, Applicability: asset.Applicable}) != "SUCCESS" {
		t.Fatal("original TaskJudgment consumer mismatch")
	}
	evidence.Business.Outcome = "INSUFFICIENT_EVIDENCE"
	report.Status = evidence.Business.Outcome
	report.Uncertainties = []string{"公开测试证据不足"}
	body, _ := asset.Freeze(report)
	text := body.String()
	evidence.Business.Output = &text
	insufficient := evaluate(evidence)
	if insufficient.Score == nil || *insufficient.Score != 0 || insufficient.Verdict != "FAIL" {
		t.Fatal("insufficient counted as complete")
	}
	evidence.Business.Validation = "REJECTED"
	evidence.Business.Output = nil
	rejected := evaluate(evidence)
	if rejected.Score == nil || *rejected.Score != 0 {
		t.Fatal("validator rejection counted as complete")
	}
	in.Work.Metadata.Spec.Definition = AdaptiveEvaluatorDefinition(metric, "actual_cost_micros")
	missing := evaluate(evidence)
	if missing.Score != nil {
		t.Fatal("missing actual cost imputed")
	}
}
