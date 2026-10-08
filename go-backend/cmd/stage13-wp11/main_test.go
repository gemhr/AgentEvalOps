package main

import (
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	"encoding/json"
	"testing"
)

func TestWP11RequiresExplicitV2(t *testing.T) {
	for _, version := range []string{"", "stage13.ci-triage-release.v1", "unknown"} {
		if requireV2(decision.Policy{Stage13ReleaseVersion: version}) == nil {
			t.Fatal("release policy silently substituted", version)
		}
	}
}

func TestWP11MetadataWhitelistAndFrozenManifest(t *testing.T) {
	m := catalog.HoldoutVerification{Dataset: datasetRef, CaseCount: 6, ManifestDigest: datasetDigest, Role: "HOLDOUT", CriticalCount: 3, NormalCount: 3, IntegrityStatus: "PASS"}
	if verifyMetadata(m) != nil {
		t.Fatal("frozen metadata rejected")
	}
	raw, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if json.Unmarshal(raw, &fields) != nil || len(fields) != 7 {
		t.Fatal("metadata whitelist changed")
	}
	for _, key := range []string{"dataset_version", "case_count", "manifest_digest", "role", "critical_count", "normal_count", "integrity_status"} {
		if _, ok := fields[key]; !ok {
			t.Fatal("metadata field missing", key)
		}
	}
	m.ManifestDigest = "tampered"
	if verifyMetadata(m) == nil {
		t.Fatal("holdout manifest mismatch accepted")
	}
	m.ManifestDigest = datasetDigest
	m.CaseCount = 5
	if verifyMetadata(m) == nil {
		t.Fatal("holdout subset accepted")
	}
}
