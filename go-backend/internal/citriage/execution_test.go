package citriage

import (
	"agentevalops/go-backend/internal/asset"
	"testing"
	"time"
)

func TestAttemptDeadlineAndSemanticBoundary(t *testing.T) {
	input := jf(map[string]any{"schema_version": "stage13.triage-input.v1", "incident_ref": map[string]any{"incident_id": "incident-a", "evidence_revision": 1, "manifest_digest": "manifest"}, "failure_summary": map[string]any{"component_scope": []string{"payment"}}, "visible_evidence": []any{}, "visible_change_inventory": []string{"change-a"}, "evidence_policy": map[string]any{"deadline_at": "2020-01-01T00:00:00Z", "max_tool_reads": 8, "max_bytes": 1048576, "allowed_types": []string{"FAILURE_DETAIL"}}})
	before := input.String()
	start := time.Date(2026, 10, 7, 8, 0, 0, 0, time.UTC)
	manifest := jf(map[string]any{"agent_id": "baseline", "subject_version": "frozen-v1"})
	a, e := MaterializeExecution(input, manifest, "run-a", "attempt-a", 180000, start)
	if e != nil {
		t.Fatal(e)
	}
	b, e := MaterializeExecution(input, manifest, "run-b", "attempt-b", 180000, start.Add(120*time.Second))
	if e != nil {
		t.Fatal(e)
	}
	read := func(body asset.JSON) ExecutionPolicy {
		var fields map[string]asset.JSON
		_ = body.Decode(&fields)
		var p ExecutionPolicy
		_ = fields["execution_policy"].Decode(&p)
		return p
	}
	ap, bp := read(a), read(b)
	if ap.DeadlineAt.Sub(start) != 180*time.Second || bp.DeadlineAt == ap.DeadlineAt || ap.SemanticDigest != bp.SemanticDigest || a.Digest() == b.Digest() || input.String() != before {
		t.Fatal("queue/publish time leaked into execution policy")
	}
	replayed, _ := MaterializeExecution(input, manifest, "run-a", "attempt-a", 180000, start)
	if replayed.String() != a.String() || ap.DeadlineAt.Sub(start.Add(60*time.Second)) != 120*time.Second {
		t.Fatal("restart refreshed execution budget")
	}
	semantic, _ := SemanticInput(input)
	var changed, policy map[string]asset.JSON
	_ = input.Decode(&changed)
	_ = changed["evidence_policy"].Decode(&policy)
	policy["max_tool_reads"] = jf(7)
	changed["evidence_policy"] = jf(policy)
	other, _ := SemanticInput(jf(changed))
	if semantic.Digest() == other.Digest() {
		t.Fatal("authorization semantics excluded")
	}
	changed["failure_summary"] = jf(map[string]any{"component_scope": []string{"other"}})
	other, _ = SemanticInput(jf(changed))
	if semantic.Digest() == other.Digest() {
		t.Fatal("business input excluded")
	}
}
