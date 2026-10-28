package featureimpact

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"agentevalops/go-backend/internal/asset"
)

// 仅消费真实 Python HTTP 原始报告及独立公开开发参考；不生成候选、不运行模拟器。
func TestPublicExperimentHTTPReports(t *testing.T) {
	evidence := os.Getenv("FEATURE_IMPACT_PUBLIC_EVIDENCE")
	if evidence == "" {
		t.Skip("NOT_EXECUTED: set explicit real HTTP evidence directory")
	}
	results := map[string]Judgment{}
	expected := map[string][5]int{"T2": {1, 1, 1, 1, 0}, "T3": {1, 1, 1, 1, 0}, "T5": {0, 0, 0, 0, 2}, "T6": {2, 2, 1, 0, 0}}
	for _, code := range []string{"T2", "T3", "T5", "T6"} {
		t.Run(code, func(t *testing.T) {
			raw, err := os.ReadFile(filepath.Join(evidence, code+".json"))
			if err != nil {
				t.Fatal(err)
			}
			var actual struct {
				Transport string `json:"transport"`
				Status    int    `json:"http_status"`
				Request   struct {
					Input   asset.JSON `json:"input"`
					Project string     `json:"project_id"`
					Run     string     `json:"run_id"`
					Attempt string     `json:"attempt_id"`
					Request string     `json:"request_id"`
					Case    string     `json:"case_id"`
					Digest  string     `json:"frozen_input_digest"`
				} `json:"request"`
				Output Output `json:"output"`
			}
			if err = json.Unmarshal(raw, &actual); err != nil {
				t.Fatal(err)
			}
			if actual.Transport != "REAL_PYTHON_HTTP" || actual.Status != 200 {
				t.Fatal("real business HTTP evidence required")
			}
			input, err := ReadInput(actual.Request.Input.Bytes())
			if err != nil {
				t.Fatal(err)
			}
			req := actual.Request
			if req.Input.Digest() != req.Digest || actual.Output.Validate(input, req.Project, req.Run, req.Attempt, req.Request, req.Case, req.Digest) != nil {
				t.Fatal("HTTP output binding invalid")
			}
			referenceBytes, err := os.ReadFile(filepath.Join("..", "..", "testdata", "feature-impact-public", code+".json"))
			if err != nil {
				t.Fatal(err)
			}
			var reference struct {
				Purpose string `json:"purpose"`
				Truth   Truth  `json:"coverage_truth"`
			}
			if json.Unmarshal(referenceBytes, &reference) != nil || reference.Purpose != "DEVELOPMENT_EXPOSED" {
				t.Fatal("independent development reference required")
			}
			score := Score(input, reference.Truth, actual.Output)
			want := expected[code]
			if [5]int{score.MustTotal, score.MustFound, score.MustActionable, score.Reasonable, score.Uncertainty} != want || !score.EvidenceAuthentic || score.Invalid != 0 || score.Duplicates != 0 || score.SemanticReview != "PENDING" || score.Verdict != "INCONCLUSIVE" {
				t.Fatalf("unexpected real rule score: %+v", score)
			}
			if code == "T6" && score.Reason != "TEST_ACTIONABILITY_REVIEW_PENDING" {
				t.Fatal("FULL reuse must remain pending")
			}
			results[code] = score
			t.Logf("%s: must=%d/%d actionable=%d reasonable=%d uncertainty=%d invalid=%d duplicates=%d verdict=%s reason=%s", code, score.MustFound, score.MustTotal, score.MustActionable, score.Reasonable, score.Uncertainty, score.Invalid, score.Duplicates, score.Verdict, score.Reason)
		})
	}
	if !t.Failed() {
		raw, err := json.MarshalIndent(results, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(filepath.Join(evidence, "go-scores.json"), append(raw, '\n'), 0600); err != nil {
			t.Fatal(err)
		}
	}
}
