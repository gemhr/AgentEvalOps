package featureimpact

import (
	"agentevalops/go-backend/internal/asset"
	"testing"
)

func TestActionabilitySeparatesRiskFromPublicSequence(t *testing.T) {
	freeze := func(v any) asset.JSON {
		t.Helper()
		j, e := asset.Freeze(v)
		if e != nil {
			t.Fatal(e)
		}
		return j
	}
	input := Input{CandidateKind: "HUMAN", Snapshot: freeze(map[string]any{"planning_cutoff": "2026-10-02T00:00:00Z", "documents": []any{map[string]any{"source_ref": "public-history", "version": "1", "chunk": "whole", "content_digest": "public-digest", "published_at": "2026-10-01T00:00:00Z"}}})}
	reference := Reference{Source: "public-history", Version: "1", Chunk: "whole", Digest: "public-digest", Basis: "TASK_INPUT"}
	truth := Truth{Version: "feature-impact-truth.v1", InputDigest: "input", CorpusDigest: "corpus", Units: []Unit{{Feature: "periodic", Mechanism: "SHARED_DELAYED_REUSE", Conditions: []string{"DELAYED_CONSUME", "PRODUCER_COMPLETE", "SUBSEQUENT_WRITE"}, Class: "MUST_TEST", Support: []string{"public-history"}, Existing: "PARTIAL"}}}
	operation := func(action, id string, extra map[string]any) asset.JSON {
		v := map[string]any{"action": action, "request_id": id}
		for k, x := range extra {
			v[k] = x
		}
		return freeze(v)
	}
	submit := operation("SUBMIT", "A", map[string]any{"payload": 17})
	deliver := operation("DELIVER", "A", map[string]any{"mode": "SHARED"})
	complete := operation("COMPLETE", "A", nil)
	write := operation("SUBMIT", "B", map[string]any{"payload": 91})
	consume := operation("CONSUME", "A", nil)
	for _, tc := range []struct {
		name       string
		ops        []asset.JSON
		actionable int
		reason     string
	}{
		{"consume_missing", []asset.JSON{operation("CONSUME", "missing", nil)}, 0, "TEST_SEQUENCE_INVALID"},
		{"legal_public_sequence", []asset.JSON{submit, deliver, complete, write, consume}, 1, "PUBLIC_TEST_SEQUENCE_VALID"},
		{"missing_delivery", []asset.JSON{submit, consume}, 0, "TEST_SEQUENCE_INVALID"},
		{"completion_before_submit", []asset.JSON{complete, submit, deliver, consume}, 0, "TEST_SEQUENCE_INVALID"},
		{"allocation_dependency_pending", []asset.JSON{submit, deliver, write, complete, consume}, 0, "TEST_RESOURCE_DEPENDENCY_PENDING"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			finding := Finding{ID: "risk", Feature: "periodic", Mechanism: truth.Units[0].Mechanism, Conditions: truth.Units[0].Conditions, Class: "MUST_TEST", Support: []Reference{reference}, Tests: []Test{{ID: "test", Preconditions: "提交原数据并延后上报", Operations: tc.ops, Observation: "读取原请求上报", Expected: "上报等于原输入", Policy: "NEW"}}}
			output := Output{Envelope: Envelope{InputDigest: "input", CorpusDigest: "corpus"}, Report: Report{Findings: []Finding{finding}}}
			actual := Score(input, truth, output)
			if actual.MustFound != 1 || actual.MustActionable != tc.actionable || actual.Details[0].Reason != tc.reason || actual.SemanticReview != "PENDING" || actual.Verdict == "PASS" {
				t.Fatalf("found/actionable/pending: %+v", actual)
			}
			if tc.reason == "TEST_RESOURCE_DEPENDENCY_PENDING" && (actual.Verdict != "INCONCLUSIVE" || actual.Reason != "TEST_ACTIONABILITY_REVIEW_PENDING") {
				t.Fatalf("unknown dependency must remain pending: %+v", actual)
			}
		})
	}
}
