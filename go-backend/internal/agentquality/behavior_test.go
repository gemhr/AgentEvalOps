package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"testing"
)

func TestBehaviorBusinessPassFailInconclusive(t *testing.T) {
	alias := "read_a"
	args := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	c := QualityCase{Schema: "agent-quality-case.v1"}
	c.Tools.Mode = "REQUIRED"
	c.Tools.Required = []ToolExpectation{{Alias: alias, Count: 1, Arguments: &args}}
	c.Handoff.Mode = "NOT_REQUESTED"
	c.Context.Mode = "NOT_REQUESTED"
	c.Memory.Mode = "NOT_REQUESTED"
	c.Answer.Mode = "NOT_REQUESTED"
	b := QualityBundle{Coverage: "COMPLETE"}
	o := QualityObservations{Coverage: "COMPLETE", Tools: []QualityTool{{Alias: &alias, State: "COMMITTED", Arguments: &args}}}
	if verdict, _ := taskQuality(c, b, o, nil); verdict != "PASS" {
		t.Fatal("exact frozen business expectation must pass", verdict)
	}
	wrong := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	o.Tools[0].Arguments = &wrong
	if verdict, _ := taskQuality(c, b, o, nil); verdict != "FAIL" {
		t.Fatal("confirmed wrong arguments must fail", verdict)
	}
	o.Tools[0].Arguments = nil
	if verdict, _ := taskQuality(c, b, o, nil); verdict != "INCONCLUSIVE" {
		t.Fatal("missing argument proof cannot pass", verdict)
	}
	c.Tools.Forbidden = []string{alias}
	b.Coverage = "PARTIAL"
	if verdict, _ := taskQuality(c, b, o, nil); verdict != "FAIL" {
		t.Fatal("confirmed forbidden action remains a counterexample", verdict)
	}
	bad, _ := asset.Freeze(map[string]any{"schema_version": "agent-quality-case.v1"})
	if _, err := ReadQualityCase(bad); err == nil {
		t.Fatal("missing dimensions cannot default to success")
	}
	c.Tools.Mode = "NOT_REQUESTED"
	c.Memory.Mode = "REQUIRED"
	c.Memory.Formation = "REQUIRED"
	o.Memory = &QualityMemory{ActualUse: true, Effects: &QualityMemoryEffects{Policy: "SERVICE_PROJECT_READ_ONLY.v1", Formation: QualityMemoryEffect{Status: "NOT_ATTEMPTED", Reason: "DISABLED_BY_POLICY"}}}
	b.Coverage = "COMPLETE"
	if verdict, _ := taskQuality(c, b, o, nil); verdict != "INCONCLUSIVE" {
		t.Fatal("policy-disabled formation cannot satisfy required formation", verdict)
	}
	c.Memory.Formation = "NOT_REQUESTED"
	c.Memory.Epistemic = []string{"USER_ASSERTION"}
	o.Memory.Selected = []QualityMemoryRecord{{Ref: "public-record"}}
	if verdict, _ := taskQuality(c, b, o, nil); verdict != "INCONCLUSIVE" {
		t.Fatal("missing epistemic proof is unknown, not a confirmed counterexample", verdict)
	}
}
