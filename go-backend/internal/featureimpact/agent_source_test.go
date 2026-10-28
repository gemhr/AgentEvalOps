package featureimpact_test

import (
	"context"
	"encoding/json"
	"os"
	"testing"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	fi "agentevalops/go-backend/internal/featureimpact"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
)

// TEST_FIXTURE：Python 转换合同输出，不是 HTTP 见证、真实 Agent Run 或模型效果。
func TestAgentSourceContractFixtureAndEvaluator(t *testing.T) {
	path := os.Getenv("FEATURE_AGENT_CONTRACT_FIXTURE")
	if path == "" {
		t.Skip("NOT_EXECUTED: explicit TEST_FIXTURE artifact required")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Kind   string     `json:"evidence_kind"`
		Input  asset.JSON `json:"input"`
		Output fi.Output  `json:"output"`
	}
	if json.Unmarshal(raw, &fixture) != nil || fixture.Kind != "TEST_FIXTURE" {
		t.Fatal("fixture boundary missing")
	}
	input, err := fi.ReadInput(fixture.Input.Bytes())
	if err != nil {
		t.Fatal(err)
	}
	out := fixture.Output
	validate := func(o fi.Output) error {
		return o.Validate(input, out.ProjectID, out.RunID, out.AttemptID, out.RequestID, out.CaseID, fixture.Input.Digest())
	}
	if validate(out) != nil {
		t.Fatal("trusted adapter fixture rejected")
	}
	refBytes, err := os.ReadFile("../../testdata/feature-impact-public/T2.json")
	if err != nil {
		t.Fatal(err)
	}
	var reference struct {
		Truth fi.Truth `json:"coverage_truth"`
	}
	if json.Unmarshal(refBytes, &reference) != nil {
		t.Fatal("reference")
	}
	scored := fi.Score(input, reference.Truth, out)
	if scored.MustFound != 1 || scored.MustActionable != 1 || scored.Reasonable != 1 || !scored.EvidenceAuthentic || scored.SemanticReview != "PENDING" || scored.Verdict != "INCONCLUSIVE" {
		t.Fatalf("score %+v", scored)
	}
	freeze := func(v any) asset.JSON {
		j, e := asset.Freeze(v)
		if e != nil {
			t.Fatal(e)
		}
		return j
	}
	// HUMAN 合同 fixture 保持独立来源，不携带 AGENT provenance，也不要求 Runtime FINAL。
	humanInput := input
	humanInput.CandidateKind, humanInput.CandidateVersion = "HUMAN", "human.v1"
	humanInput.Report = freeze(map[string]any{"candidate_kind": "HUMAN"})
	var humanOutput fi.Output
	_ = fi.Decode(freeze(out).Bytes(), &humanOutput)
	humanOutput.Envelope.Kind, humanOutput.Envelope.Version, humanOutput.Envelope.RunID = "HUMAN", "human.v1", nil
	humanOutput.InputDigest = freeze(humanInput).Digest()
	humanOutput.Sources = freeze(map[string]any{"submitted_by": "TEST_FIXTURE-human"})
	if humanOutput.Validate(humanInput, out.ProjectID, out.RunID, out.AttemptID, out.RequestID, out.CaseID, humanOutput.InputDigest) != nil ||
		!fi.Score(humanInput, reference.Truth, humanOutput).EvidenceAuthentic {
		t.Fatal("HUMAN compatibility failed")
	}
	for _, key := range []string{"run_id", "session_id", "final_receipt_digest", "raw_output_digest", "protocol"} {
		var proof map[string]any
		_ = out.Sources.Decode(&proof)
		delete(proof, key)
		changed := out
		changed.Sources = freeze(proof)
		if validate(changed) == nil || fi.Score(input, reference.Truth, changed).EvidenceAuthentic {
			t.Fatal("missing provenance accepted", key)
		}
	}
	for _, kind := range []string{"RULE", "HUMAN"} {
		changed := out
		changed.Envelope.Kind = kind
		if validate(changed) == nil {
			t.Fatal("relabeled candidate accepted")
		}
	}
	untrusted := input
	untrusted.Report = freeze(map[string]any{"report_id": "x", "run_id": "y", "raw_output": "pretend"})
	if _, err = fi.ReadInput(freeze(untrusted).Bytes()); err == nil {
		t.Fatal("client report body accepted as trusted selector")
	}
	// 单独伪造 EVIDENCE_READ 清单也无效：引用本身必须匹配所声明的来源类型。
	var changed fi.Output
	_ = fi.Decode(freeze(out).Bytes(), &changed)
	changed.Report.Findings[0].Support[0].Basis = "EVIDENCE_READ"
	if fi.Score(input, reference.Truth, changed).EvidenceAuthentic {
		t.Fatal("changed raw conclusion accepted")
	}
	// 同一真实领域 evaluator dispatch；只构造 EvaluationInput，不创建虚假的持久 Run/Result。
	mr := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	definition := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: fi.InputProtocol, OutputMetrics: []asset.Ref{mr},
		Applicability: asset.Applicability{RuleRef: "feature-impact-public.v1"}, Availability: asset.ContractOnly, ImplementationRef: fi.Implementation,
		Config: freeze(agentquality.Config{Metric: fi.Metric}), SchemaVersion: fi.OutputProtocol, Normalization: fi.Normalization,
		Budget: metric.Budget{TotalMilliseconds: 10000, MaxResponseBytes: 1048576}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}}
	if !agentquality.FeatureImpactSupported(definition) {
		t.Fatal("feature evaluator definition rejected")
	}
	body := freeze(out)
	artifact := ev.Binding{ProjectID: out.ProjectID, RunID: out.RunID, AttemptID: out.AttemptID, RequestID: out.RequestID,
		Ref: "test-fixture-agent-contract", Schema: fi.OutputProtocol, Availability: "AVAILABLE", Body: body, Digest: body.Digest()}
	evaluation := worker.EvaluationInput{Scope: ev.Scope{Scope: asset.Scope{ProjectID: out.ProjectID}},
		Attempt: ev.Attempt{ID: out.AttemptID, RunID: out.RunID, RequestID: out.RequestID, Outcome: ev.Success,
			Request: ev.Request{Case: ev.CaseInput{Identity: ev.AssetIdentity{Ref: asset.Ref{EntityID: out.CaseID, Version: "v1"}, ContentDigest: fixture.Input.Digest()},
				Case: catalog.CaseContent{Input: fixture.Input, GroundTruth: freeze(reference.Truth), Criticality: catalog.Normal}}},
			Metadata: ev.AttemptMetadata{Observation: ev.Outcome{Artifact: &artifact}}},
		Work: ev.Work{Metadata: ev.WorkMetadata{Spec: ev.EvaluatorSpec{Definition: definition}}}}
	result, err := (agentquality.DeterministicEvaluator{}).Evaluate(context.Background(), evaluation)
	if err != nil || result.Value.Score != nil || result.Value.Verdict != "INCONCLUSIVE" || result.Value.Reason != "SEMANTIC_SUPPORT_REVIEW_PENDING" {
		t.Fatalf("dispatch error=%v verdict=%s reason=%s", err, result.Value.Verdict, result.Value.Reason)
	}
	var provenance struct {
		Counts fi.Judgment `json:"counts"`
	}
	if json.Unmarshal(result.Value.Provenance.Bytes(), &provenance) != nil || provenance.Counts.MustFound != 1 {
		t.Fatal("domain counts not reused")
	}
	if path := os.Getenv("FEATURE_AGENT_SCORE_EVIDENCE"); path != "" {
		raw, err := json.MarshalIndent(map[string]any{"evidence_kind": "TEST_FIXTURE", "real_agent_execution": "NOT_EXECUTED", "judgment": scored, "evaluator_result": result.Value}, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(path, raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	t.Log("TEST_FIXTURE accepted by existing evaluator; must_found=1 must_actionable=1 semantic=PENDING; NOT real Agent execution")
}
