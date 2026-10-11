package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/citriage"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
	"context"
	"os"
	"testing"
)

func TestResponsibilityV2EvaluatorDispatchAndProvenance(t *testing.T) {
	raw, e := os.ReadFile("../citriage/testdata/world-test-only.json")
	if e != nil {
		t.Fatal(e)
	}
	world, _ := asset.ParseJSON(raw)
	input, e := citriage.MaterializeWorldInput(world, []string{})
	if e != nil {
		t.Fatal(e)
	}
	change := "fixture-change"
	gt, e := citriage.DeriveGroundTruth(world, citriage.ControlledFault{ID: "test-only-eval", Target: citriage.EntityRef{Kind: "ARTIFACT", ID: "fixture-helper"}, Mechanism: "TEST_LOGIC", Change: &change, Category: "TEST_CASE", Actions: []string{"FIX_TEST"}, Ticket: "IGNORE", Criticality: "NORMAL"})
	if e != nil {
		t.Fatal(e)
	}
	freeze := func(v any) asset.JSON {
		j, e := asset.Freeze(v)
		if e != nil {
			t.Fatal(e)
		}
		return j
	}
	in2, _ := citriage.ReadInputV2(input)
	ref := in2.Business.Relations[0].Source
	answer := citriage.Output{Category: gt.Category, Candidates: []citriage.Candidate{{ID: "candidate-1", Rank: 1, Summary: "TEST-ONLY", Descriptor: gt.Roots[0].Descriptors[0], Refs: []string{ref.ID}, Confidence: 0.8}}, Refs: []citriage.EvidenceRef{ref}, Confidence: 0.8, Ticket: gt.Ticket}
	answer.Action.Code = gt.Actions[0]
	p, run, attempt, request := asset.NewID(), asset.NewID(), asset.NewID(), asset.NewID()
	mr := asset.Ref{EntityID: asset.NewID(), Version: "test-only-v2"}
	artifact := ev.Binding{ProjectID: p, RunID: run, AttemptID: attempt, RequestID: request, Ref: "test-only-answer", Schema: "artifact.v1", Availability: "AVAILABLE", Body: freeze(freeze(answer).String())}
	artifact.Digest = artifact.Body.Digest()
	d := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: citriage.InputV2, OutputMetrics: []asset.Ref{mr}, Applicability: asset.Applicability{RuleRef: "test-only.v2"}, Availability: asset.ContractOnly, ImplementationRef: citriage.ImplementationV2, SchemaVersion: "stage13.triage-output.v1", Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 65536}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}, Normalization: "stage13.cause-descriptor-exact.v2"}
	in := worker.EvaluationInput{Scope: ev.Scope{Scope: asset.Scope{ProjectID: p}}, Attempt: ev.Attempt{ID: attempt, RunID: run, RequestID: request, Outcome: ev.Success, Request: ev.Request{Case: ev.CaseInput{Case: catalog.CaseContent{Input: input, GroundTruth: freeze(gt), Criticality: catalog.Normal}}}, Metadata: ev.AttemptMetadata{Observation: ev.Outcome{Artifact: &artifact}}}, Work: ev.Work{Metadata: ev.WorkMetadata{Spec: ev.EvaluatorSpec{Definition: d}}}}
	for _, name := range citriage.MetricsForVersion(citriage.ContractV2) {
		d.Config = freeze(Config{Metric: name, K: 3})
		if !CITriageSupported(d) {
			t.Fatal("definition", name)
		}
		in.Work.Metadata.Spec.Definition = d
		out, e := (DeterministicEvaluator{}).Evaluate(context.Background(), in)
		if e != nil || out.Value.Score == nil || *out.Value.Score != 1 {
			t.Fatalf("%s: %+v %v", name, out.Value, e)
		}
		var proof map[string]any
		_ = out.Value.Provenance.Decode(&proof)
		if proof["implementation_ref"] != citriage.ImplementationV2 {
			t.Fatal("metric unbound", proof)
		}
	}
	d.InputContract = "stage13.triage-input.v1"
	if CITriageSupported(d) {
		t.Fatal("v2 implementation accepted v1 input contract")
	}
	d.InputContract = citriage.InputV2
	d.ImplementationRef = citriage.Implementation
	in.Work.Metadata.Spec.Definition = d
	out, e := evaluateCITriage(context.Background(), in)
	if e != nil || out.Value.SourceError != "INVALID_GROUND_TRUTH" {
		t.Fatal("mixed evaluator/GT", out, e)
	}
}
