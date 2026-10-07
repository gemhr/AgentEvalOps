package decision

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
	ev "agentevalops/go-backend/internal/evaluation"
	"testing"
	"time"
)

func TestStage13GateSourceMaterializedInput(t *testing.T) {
	f := func(v any) asset.JSON {
		j, e := asset.Freeze(v)
		if e != nil {
			t.Fatal(e)
		}
		return j
	}
	input := f(map[string]any{"schema_version": "stage13.triage-input.v1", "evidence_policy": map[string]any{"deadline_at": "2020-01-01T00:00:00Z", "max_tool_reads": 8}})
	manifest := f(map[string]any{"agent_id": "baseline", "model_profile_digest": "same", "output_schema_digest": "same"})
	config := f(map[string]any{"transport": map[string]any{"extra": "frozen"}, "expected_subject_manifest": manifest, "execution_policy_version": citriage.ExecutionPolicyVersion})
	a := ev.Attempt{ID: asset.NewID(), RunID: asset.NewID()}
	body, e := citriage.MaterializeExecution(input, manifest, a.RunID, a.ID, 180000, time.Now().UTC())
	if e != nil {
		t.Fatal(e)
	}
	var fields map[string]asset.JSON
	_ = body.Decode(&fields)
	var p citriage.ExecutionPolicy
	_ = fields["execution_policy"].Decode(&p)
	var query string
	_ = fields["query"].Decode(&query)
	actual, _ := asset.ParseJSON([]byte(query))
	r := map[string]any{"run_id": a.ID, "evaluation_attempt_id": a.ID, "actual_subject_manifest": manifest, "actual_input_digest": actual.Digest(), "semantic_input_digest": p.SemanticDigest, "execution_request_digest": body.Digest(), "execution_policy": p, "receipt_digest": "additional-real-receipt-field"}
	a.Metadata.Observation.Cleanup = f(map[string]any{"comparability": "COMPARABLE", "target_version": "stage13-evaluation-v1", "model_comparability_decision": map[string]any{"policy_version": "stage13.model-comparability.v2", "comparable": true, "actual_provider": "deepseek", "actual_model": "same", "warnings": []string{}}})
	setReceipt := func() {
		a.Metadata.Observation.Evidence = []ev.Binding{{Schema: "stage13-subject-receipt.v1", Body: f(map[string]any{"selected_final_run_id": a.ID, "actual_subject_receipt": r, "child_subject_receipts": []any{}})}}
	}
	setReceipt()
	if _, ok := stage13Identity(a, config, input); !ok {
		t.Fatal("actual materialized receipt blocked")
	}
	r["actual_input_digest"] = input.Digest()
	setReceipt()
	if _, ok := stage13Identity(a, config, input); ok {
		t.Fatal("Dataset digest accepted as actual request digest")
	}
	r["actual_input_digest"] = actual.Digest()
	r["execution_request_digest"] = "wrong"
	setReceipt()
	if _, ok := stage13Identity(a, config, input); ok {
		t.Fatal("unbound execution accepted")
	}
}
