package agentquality

import (
	"strings"
	"testing"
)

func TestCIToolAttemptFormat(t *testing.T) {
	// 原147的真实ToolExecutionService输出；不是Kernel EvaluationAttempt。
	const original = "3fa9aa2d03114730ac920b2cf7bf1acd"
	for _, tc := range []struct {
		name, id string
		valid    bool
	}{
		{"original_owner", original, true}, {"all_zero_hex32", strings.Repeat("0", 32), true},
		{"kernel_uuid", "3fa9aa2d-0311-4730-ac92-0b2cf7bf1acd", false},
		{"short", original[:31], false}, {"long", original + "0", false}, {"uppercase", strings.ToUpper(original), false},
		{"non_hex", original[:31] + "g", false}, {"leading_space", " " + original, false}, {"trailing_space", original + " ", false}, {"newline", original + "\n", false}, {"empty", "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if ciToolAttemptID.MatchString(tc.id) != tc.valid {
				t.Fatal("Tool Attempt format mismatch")
			}
		})
	}
}
