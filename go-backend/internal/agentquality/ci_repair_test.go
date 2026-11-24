package agentquality

import (
	"context"
	"encoding/json"
	"testing"
)

func TestCIRepairIndependentTLV(t *testing.T) {
	for _, tc := range []struct {
		name, caseID, encoded, verdict, reason string
		expected, actual                       int
	}{
		{"stale", RepairPrimary, "100107", "FAIL", staleTLV, 7, 7},
		{"candidate", RepairPrimary, "11020007", "PASS", "", 7, 7},
		{"bad_length", RepairPrimary, "11010007", "FAIL", "TLV_INPUT_INVALID", 7, -1},
		{"wrong_value", RepairPrimary, "11020700", "FAIL", "TLV_BUSINESS_VALUE_MISMATCH", 7, 1792},
		{"control", RepairControl, "11020009", "PASS", "", 9, 9},
		{"control_regression", RepairControl, "11020008", "FAIL", "TLV_BUSINESS_VALUE_MISMATCH", 9, 8},
	} {
		t.Run(tc.name, func(t *testing.T) {
			raw, err := json.Marshal(repairScript{CaseID: tc.caseID, Request: tc.encoded})
			if err != nil {
				t.Fatal(err)
			}
			r := parseRepairResult(string(raw), tc.caseID, RepairTruth{Expected: tc.expected, Revision: 2, Type: 17, Length: 2})
			if r.Oracle != tc.verdict || (r.Reason == nil && tc.reason != "") || (r.Reason != nil && *r.Reason != tc.reason) ||
				(tc.actual == -1 && r.Actual != nil) || (tc.actual != -1 && (r.Actual == nil || *r.Actual != tc.actual)) {
				t.Fatalf("result=%+v", r)
			}
		})
	}
}

func TestCIRepairPatchChecksActualBytesAndScope(t *testing.T) {
	a := repairArtifact{Before: "100107\n", After: "11020007\n", Diff: "diff --git a/cases/tlv-report.json b/cases/tlv-report.json\n--- a/cases/tlv-report.json\n+++ b/cases/tlv-report.json\n@@ -1 +1 @@\n-100107\n+11020007\n"}
	ctx := context.Background()
	if !applyRepairPatch(ctx, a) {
		t.Fatal("exact patch failed")
	}
	wrong := a
	wrong.After += "\n"
	if applyRepairPatch(ctx, wrong) {
		t.Fatal("unrelated after bytes accepted")
	}
	wrong = a
	wrong.Diff += "diff --git a/extra b/extra\nnew file mode 100644\n--- /dev/null\n+++ b/extra\n@@ -0,0 +1 @@\n+unexpected\n"
	if applyRepairPatch(ctx, wrong) {
		t.Fatal("extra path accepted")
	}
}
