package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"os"
	"testing"
)

func TestCISourceValueContract(t *testing.T) {
	raw, err := os.ReadFile("testdata/ci_source_values.json")
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		Schema string `json:"schema_version"`
		Rows   []struct {
			Kind      string     `json:"kind"`
			Execution string     `json:"execution_id"`
			Content   asset.JSON `json:"content"`
			Accepted  bool       `json:"accepted"`
			Digest    *string    `json:"semantic_digest"`
		} `json:"rows"`
	}
	if StrictQualityShape(raw, &v) != nil || v.Schema != "public-ci-source-values.v1" {
		t.Fatal("public vector")
	}
	for i, row := range v.Rows {
		got, err := CISourceSemanticDigest(row.Kind, row.Content, row.Execution)
		if (err == nil) != row.Accepted || row.Accepted && (row.Digest == nil || got != *row.Digest) {
			t.Fatalf("row=%d kind=%s accepted=%v err=%v", i, row.Kind, row.Accepted, err)
		}
	}
	t.Logf("public Python/Go value vectors: %d; valid null diagnosis branches retained", len(v.Rows))
}
