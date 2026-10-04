package agentquality

import (
	"context"
	"encoding/json"
	"math"
	"os"
	"testing"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
)

func TestRAGGoldenAndIdentity(t *testing.T) {
	raw, e := os.ReadFile("../../testdata/rag_golden.json")
	if e != nil {
		t.Fatal(e)
	}
	var f struct {
		Vectors []struct {
			Name     string                   `json:"name"`
			Artifact RAGArtifact              `json:"artifact"`
			GT       *GroundTruth             `json:"ground_truth"`
			K        int                      `json:"k"`
			Expected map[string]*float64      `json:"expected"`
			State    asset.ApplicabilityState `json:"state"`
		} `json:"vectors"`
	}
	if e = json.Unmarshal(raw, &f); e != nil {
		t.Fatal(e)
	}
	for _, v := range f.Vectors {
		t.Run(v.Name, func(t *testing.T) {
			if e = v.Artifact.Validate("remote"); e != nil {
				t.Fatal(e)
			}
			for key, want := range v.Expected {
				got, err := Ranking(key, v.K, v.GT, v.Artifact)
				if err != nil || got.State != v.State || (got.Score == nil) != (want == nil) || (want != nil && math.Abs(*want-*got.Score) > 1e-12) {
					t.Fatalf("%s: got %+v want %v/%s err=%v", key, got, want, v.State, err)
				}
			}
		})
	}
	doc := "d"
	r := RAGArtifact{Ranked: []RankedItem{{Document: "d", Chunk: "c", Rank: 1}, {Document: "other", Chunk: "c", Rank: 2}}}
	for _, g := range []GroundTruth{{Graded: []Graded{{Chunk: "c", Relevance: 1}}}, {Graded: []Graded{{Document: &doc, Chunk: "c", Relevance: 1}, {Document: &doc, Chunk: "c", Relevance: 2}}}} {
		if _, e = Ranking("ndcg.v1", 5, &g, r); e == nil {
			t.Fatal("ambiguous/duplicate GT accepted")
		}
	}
	r.Ranked = r.Ranked[:1]
	g := GroundTruth{Graded: []Graded{{Chunk: "c", Relevance: 1}, {Document: &doc, Chunk: "c", Relevance: 1}}}
	if _, e = Ranking("ndcg.v1", 5, &g, r); e == nil {
		t.Fatal("overlapping identities accepted")
	}
}
func TestTaskSuccessCoverageAndImportedJudgment(t *testing.T) {
	j := TaskJudgment{Version: "task_success.v1", Decision: TaskSuccess, Method: "IMPORTED", Reason: "受控已验证 evidence", ProducerRef: "human-compatible.v1"}
	if j.Validate() != nil {
		t.Fatal("typed imported judgment rejected")
	}
	failed := j
	failed.Decision = TaskFailure
	inc := j
	inc.Decision = TaskInconclusive
	in := []TaskObservation{{ev.Success, asset.Applicable, j}, {ev.Success, asset.Applicable, failed}, {ev.Success, asset.Applicable, inc}, {ev.Success, asset.MissingEvidence, j}, {ev.Unknown, asset.Applicable, j}, {ev.Timeout, asset.Applicable, j}, {ev.Success, asset.NotApplicable, j}, {ev.Success, asset.UnsupportedEvidence, j}}
	r, e := AggregateTaskSuccess(in)
	if e != nil || r.EligibleTotal != 7 || r.Success != 1 || r.Failure != 2 || r.UnknownExecution != 1 || r.MissingEvidence != 1 || r.Unsupported != 1 || r.NotApplicable != 1 || r.Inconclusive != 1 || r.EligibleDecidable != 3 || *r.Rate != 1.0/3 || *r.Coverage != 3.0/7 {
		t.Fatalf("unexpected rate %+v/%v", r, e)
	}
	r, e = AggregateTaskSuccess([]TaskObservation{{ev.Unknown, asset.Applicable, j}})
	if e != nil || r.Rate != nil || *r.Coverage != 0 {
		t.Fatal("UNKNOWN entered success denominator")
	}
	// 结构化断言与缺失证据是不同结果；不因不存在 path 且 expected=null 误判成功。
	p, run, attempt, request := asset.NewID(), asset.NewID(), asset.NewID(), asset.NewID()
	metricRef := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	c := catalog.CaseContent{Type: catalog.AgentTask, TaskGoal: "检查结构", AcceptanceCriteria: []string{"state.ok=true"}, Applicability: asset.Applicability{RuleRef: "case.v1"}}
	body, _ := asset.ParseJSON([]byte(`{"state":{"ok":true}}`))
	a := ev.Binding{ProjectID: p, RunID: run, AttemptID: attempt, RequestID: request, Ref: "artifact", Schema: "artifact.v1", Availability: "AVAILABLE", Body: body, Digest: body.Digest()}
	d := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: InputContract, OutputMetrics: []asset.Ref{metricRef}, Applicability: metric.Builtins()[0].Definition.Applicability, Availability: asset.ContractOnly, ImplementationRef: DeterministicImplementation, SchemaVersion: JudgeSchema, Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 4096}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}, Normalization: Normalization}
	d.Config, _ = asset.Freeze(Config{Metric: "task_success.v1"})
	inEval := worker.EvaluationInput{Scope: ev.Scope{Scope: asset.Scope{ProjectID: p}}, Run: ev.Run{Snapshot: ev.RunSnapshot{Input: ev.SnapshotInput{Metrics: []ev.MetricInput{{Identity: ev.AssetIdentity{Ref: metricRef}, Definition: metric.Builtins()[0].Definition}}}}}, Attempt: ev.Attempt{ID: attempt, RunID: run, RequestID: request, Outcome: ev.Success, Request: ev.Request{Case: ev.CaseInput{Identity: ev.AssetIdentity{ContentDigest: "input"}, Case: c}}, Metadata: ev.AttemptMetadata{Observation: ev.Outcome{Artifact: &a}}}, Work: ev.Work{SpecDigest: "spec", Metadata: ev.WorkMetadata{Spec: ev.EvaluatorSpec{Identity: ev.AssetIdentity{ContentDigest: "spec"}, Definition: d, Metrics: []asset.Ref{metricRef}, Applicability: asset.Applicability{RuleRef: "binding.v1"}}}}}
	for _, tc := range []struct{ Kind, Config, Want string }{{"json_equals.v1", `{"path":["state","ok"],"equals":true}`, "PASS"}, {"json_equals.v1", `{"path":["missing"],"equals":null}`, "FAIL"}, {"json_equals.v1", `{"path":["state","ok","child"],"equals":null}`, "FAIL"}, {"json_type.v1", `{"path":["state"],"type":"object"}`, "PASS"}, {"unsupported.v1", `{}`, "INCONCLUSIVE"}} {
		t.Run(tc.Kind+tc.Want, func(t *testing.T) {
			cfg, _ := asset.ParseJSON([]byte(tc.Config))
			inEval.Attempt.Request.Case.Case.Assertions = []catalog.Assertion{{ID: "check", Kind: tc.Kind, Config: cfg, Required: true}}
			out, err := (DeterministicEvaluator{}).Evaluate(context.Background(), inEval)
			if err != nil || out.Value.Verdict != tc.Want {
				t.Fatalf("got %s/%v", out.Value.Verdict, err)
			}
		})
	}
	inEval.Work.Metadata.Spec.Definition.Applicability.CaseTypes = []string{"RAG"}
	out, _ := (DeterministicEvaluator{}).Evaluate(context.Background(), inEval)
	if out.Value.SourceError != "NOT_APPLICABLE" || out.Value.Score != nil {
		t.Fatal("N/A became zero score")
	}
	inEval.Work.Metadata.Spec.Definition.Applicability.CaseTypes = nil
	inEval.Attempt.Metadata.Observation.Artifact.Availability = "UNAVAILABLE"
	inEval.Attempt.Metadata.Observation.Artifact.Body = asset.JSON{}
	out, _ = (DeterministicEvaluator{}).Evaluate(context.Background(), inEval)
	if out.Value.SourceError != "MISSING_EVIDENCE" || out.Value.Score != nil {
		t.Fatal("opaque artifact used to score")
	}
}
