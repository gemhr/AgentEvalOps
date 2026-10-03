package asset_test

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
)

func TestLegacyCatalogGoldenParity(t *testing.T) {
	raw, err := os.ReadFile("../../testdata/legacy_catalog_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var corpus []struct {
		ID        string `json:"vector_id"`
		Kind      string `json:"kind"`
		Algorithm string `json:"algorithm_ref"`
		Snapshot  string `json:"snapshot_json"`
		Bytes     string `json:"expected_canonical_bytes_base64"`
		Digest    string `json:"expected_sha256"`
	}
	if err = json.Unmarshal(raw, &corpus); err != nil {
		t.Fatal(err)
	}
	for _, v := range corpus {
		t.Run(v.ID, func(t *testing.T) {
			if v.Algorithm != asset.LegacyAlgorithm {
				t.Fatal(v.Algorithm)
			}
			input, err := asset.ParseJSON([]byte(v.Snapshot))
			if err != nil {
				t.Fatal(err)
			}
			var projection asset.JSON
			if v.Kind == "CASE" {
				projection, err = asset.LegacyCaseDigest(input)
			} else {
				projection, err = asset.LegacyEvaluatorDefinitionDigest(input)
			}
			if err != nil {
				t.Fatal(err)
			}
			expected, err := base64.StdEncoding.DecodeString(v.Bytes)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(projection.Bytes(), expected) {
				t.Fatalf("byte parity mismatch\ngot: %s\nwant: %s", projection.Bytes(), expected)
			}
			if projection.Digest() != v.Digest {
				t.Fatalf("digest parity mismatch: %s != %s", projection.Digest(), v.Digest)
			}
			var snapshot map[string]asset.JSON
			if err = input.Decode(&snapshot); err != nil {
				t.Fatal(err)
			}
			if v.Kind == "CASE" {
				for _, key := range []string{"case_id", "version", "name", "created_at", "tags"} {
					snapshot[key], _ = asset.ParseJSON([]byte(`"changed non-semantic label"`))
				}
				changed, _ := asset.Freeze(snapshot)
				unchanged, err := asset.LegacyCaseDigest(changed)
				if err != nil || unchanged.Digest() != v.Digest {
					t.Fatal("legacy identity included display/version fields", err)
				}
			} else {
				snapshot["execution_budget"], _ = asset.ParseJSON([]byte(`{"max_provider_calls":9}`))
				changed, _ := asset.Freeze(snapshot)
				unchanged, err := asset.LegacyEvaluatorDefinitionDigest(changed)
				if err != nil || unchanged.Digest() != v.Digest {
					t.Fatal("new budget silently changed legacy Spec digest", err)
				}
			}
		})
	}
}

func TestCanonicalIdentityAndImmutableVersion(t *testing.T) {
	parse := func(raw string) asset.JSON {
		v, err := asset.ParseJSON([]byte(raw))
		if err != nil {
			t.Fatal(err)
		}
		return v
	}
	a := parse(`{"b":[1,2],"a":1.0}`)
	b := parse(`{ "a":1.0,"b":[1,2] }`)
	if a.Digest() != b.Digest() {
		t.Fatal("key order changed semantic equality")
	}
	if a.Digest() == parse(`{"b":[2,1],"a":1.0}`).Digest() || parse(`1`).Digest() == parse(`1.0`).Digest() || parse(`0.0`).Digest() == parse(`-0.0`).Digest() || parse(`9007199254740992`).Digest() == parse(`9007199254740993`).Digest() {
		t.Fatal("semantic identity collapsed")
	}
	for _, raw := range []string{`{"a":1,"a":2}`, `NaN`, `1e400`, `"\ud800"`, `"\udc00"`} {
		if _, err := asset.ParseJSON([]byte(raw)); err == nil {
			t.Fatalf("accepted invalid %s", raw)
		}
	}
	project, caseID := asset.NewID(), asset.NewID()
	body := catalog.CaseContent{Input: a, TaskGoal: "完成任务", AcceptanceCriteria: []string{"准确"}, Type: catalog.AgentTask, Capability: "answer", Criticality: catalog.Critical, BodyPolicy: catalog.Retained, Applicability: asset.Applicability{RuleRef: "case.v1"}}
	source := asset.Source{Kind: "TEST", Ref: "fixture.v1", Principal: "test"}
	v, err := asset.NewVersion(project, asset.Ref{EntityID: caseID, Version: "v1"}, body, source, "test", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	digest := v.SemanticDigest()
	body.AcceptanceCriteria[0] = "调用方修改"
	decoded := v.Content()
	decoded.Body.AcceptanceCriteria[0] = "副本修改"
	raw := v.Bytes()
	raw[0] = 'x'
	if v.Content().Body.AcceptanceCriteria[0] != "准确" || v.SemanticDigest() != digest {
		t.Fatal("published content was mutable")
	}
	v2, err := asset.NewVersion(project, asset.Ref{EntityID: caseID, Version: "v2"}, v.Content().Body, source, "test", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if v2.Ref() == v.Ref() || v2.SemanticDigest() != v.SemanticDigest() {
		t.Fatal("version label conflated with content identity")
	}
	changed := v.Content().Body
	changed.TaskGoal = "新目标"
	v3, _ := asset.NewVersion(project, v.Ref(), changed, source, "test", time.Now())
	if v3.SemanticDigest() == digest {
		t.Fatal("task goal absent from identity")
	}
}
