//go:build integration

package citriage

import (
	"agentevalops/go-backend/internal/asset"
	"encoding/json"
	"os"
	"testing"
)

func TestResponsibilityV1HistoricalReplay(t *testing.T) {
	path := os.Getenv("WP12CT_V1_REPLAY_PATH")
	if path == "" {
		t.Fatal("explicit already-exposed historical replay vectors required")
	}
	raw, e := os.ReadFile(path)
	if e != nil {
		t.Fatal(e)
	}
	var vectors []struct {
		Input  asset.JSON         `json:"input"`
		GT     asset.JSON         `json:"ground_truth"`
		Raw    string             `json:"raw"`
		Scores map[string]float64 `json:"scores"`
	}
	if json.Unmarshal(raw, &vectors) != nil || len(vectors) != 84 {
		t.Fatal("historical inputs")
	}
	for i, v := range vectors {
		g, e := ReadGroundTruth(v.GT)
		if e != nil || g.Version != Contract || InputVersion(v.Input) != "stage13.triage-input.v1" {
			t.Fatal(i, "version", e)
		}
		s, e := Score(v.Raw, v.Input, g)
		if e != nil {
			t.Fatal(i, e)
		}
		for metric, expected := range v.Scores {
			if s.Values[metric] != expected {
				t.Fatal(i, metric, s.Values[metric], expected)
			}
		}
	}
	t.Logf("replayed %d actual historical v1 scores; no persisted Result or Gate rewritten", len(vectors))
}
