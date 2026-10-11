package provider

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"slices"
	"testing"
	"time"
)

func v2TestManifest(t *testing.T) asset.JSON {
	var m map[string]any
	_ = stage13UnitManifest(t).Decode(&m)
	m["subject_id"], m["agent_id"], m["subject_version"], m["agent_definition_version"] = "test_only_contract_subject", "test_only_contract_subject", "test-only-v2", "test-only-v2"
	for k, v := range citriage.ContractIdentities() {
		m[k] = v
	}
	var domain map[string]any
	_ = citriage.ComparisonDomain().Decode(&domain)
	m["output_schema_digest"] = domain["output_schema_digest"]
	delete(m, "subject_manifest_digest")
	j, _ := asset.Freeze(m)
	m["subject_manifest_digest"] = j.Digest()
	j, _ = asset.Freeze(m)
	return j
}
func v2TestWire(t *testing.T, request asset.JSON) map[string]any {
	t.Helper()
	var body map[string]asset.JSON
	_ = request.Decode(&body)
	var query, attempt string
	_ = body["query"].Decode(&query)
	_ = body["run_id"].Decode(&attempt)
	wire := stage13UnitWire(body["expected_subject_manifest"], attempt, query)
	r := wire["actual_subject_receipt"].(map[string]any)
	r["receipt_version"] = citriage.ReceiptV2
	r["execution_policy"] = body["execution_policy"]
	r["execution_request_digest"] = request.Digest()
	for k, v := range citriage.ContractIdentities() {
		r[k] = v
	}
	var p map[string]asset.JSON
	_ = body["execution_policy"].Decode(&p)
	for _, k := range []string{"source_input_digest", "source_semantic_input_digest", "projected_input_digest", "semantic_input_digest", "comparison_domain_digest"} {
		r[k] = p[k]
	}
	sealReceipt(r)
	return wire
}
func TestResponsibilityV2ActualHTTPAndComparableDomain(t *testing.T) {
	raw, err := os.ReadFile("../citriage/testdata/input-test-only.json")
	if err != nil {
		t.Fatal(err)
	}
	input, _ := asset.ParseJSON(raw)
	manifest := v2TestManifest(t)
	makeSide := func() Stage13PolicyInput {
		request, e := citriage.MaterializeExecution(input, manifest, asset.NewID(), asset.NewID(), 180000, time.Now().UTC())
		if e != nil {
			t.Fatal(e)
		}
		wire := v2TestWire(t, request)
		receipt, _ := asset.Freeze(wire["actual_subject_receipt"])
		return Stage13PolicyInput{Manifest: manifest, Receipt: receipt}
	}
	pair := Stage13PolicyDocument{Mode: "AGENT_REGRESSION", Baseline: makeSide(), Candidate: makeSide()}
	result := CompareStage13Models(pair)
	if !result.Comparable || result.Policy != Stage13ModelPolicyV3 {
		t.Fatalf("same v2 domain blocked: %+v", result)
	}
	old := stage13UnitManifest(t)
	wire := stage13UnitWire(old, asset.NewID(), input.String())
	oldReceipt, _ := asset.Freeze(wire["actual_subject_receipt"])
	pair.Candidate = Stage13PolicyInput{Manifest: old, Receipt: oldReceipt}
	result = CompareStage13Models(pair)
	if result.Comparable || !slices.Contains(result.Reasons, "DOMAIN_MISMATCH") {
		t.Fatalf("mixed domain: %+v", result)
	}
	for _, mode := range []string{"valid", "v1-receipt", "source-tamper", "domain-tamper"} {
		t.Run(mode, func(t *testing.T) {
			count := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				count++
				var body asset.JSON
				if json.NewDecoder(r.Body).Decode(&body) != nil {
					t.Fatal("request")
				}
				wire := v2TestWire(t, body)
				receipt := wire["actual_subject_receipt"].(map[string]any)
				if mode == "v1-receipt" {
					receipt["receipt_version"] = "stage13.actual-subject-receipt.v1"
				}
				if mode == "source-tamper" {
					receipt["source_input_digest"] = "wrong"
				}
				if mode == "domain-tamper" {
					receipt["comparison_domain_digest"] = "wrong"
				}
				sealReceipt(receipt)
				_ = json.NewEncoder(w).Encode(wire)
			}))
			defer server.Close()
			t.Setenv("WP12CT_UNIT_TOKEN", "controlled-test")
			cfg := Stage13Config{Transport: DefaultLocalAgentConfig(server.URL, "WP12CT_UNIT_TOKEN"), ExpectedSubjectManifest: manifest, ExecutionPolicyVersion: citriage.ExecutionPolicyV2}
			target, e := NewStage13Target(cfg)
			if e != nil {
				t.Fatal(e)
			}
			defer target.Close()
			config, _ := asset.Freeze(cfg)
			q := ev.Request{ID: asset.NewID(), RunID: asset.NewID(), AttemptID: asset.NewID(), Target: ev.Target{ID: Stage13TargetID, Kind: "LOCALAGENT_HTTP", Version: Stage13TargetVersion, Config: config, TimeoutMilliseconds: 180000}, Case: ev.CaseInput{Identity: ev.AssetIdentity{ProjectID: asset.NewID()}}}
			q.Case.Case.Input = input
			target.ReserveExecution = func(_ context.Context, _ ev.Scope, q ev.Request) (asset.JSON, error) {
				return citriage.MaterializeExecution(input, manifest, q.RunID, q.AttemptID, 180000, time.Now().UTC())
			}
			out, e := target.Execute(context.Background(), ev.Scope{Scope: asset.Scope{ProjectID: q.Case.Identity.ProjectID}}, q)
			if e != nil || count != 1 || (mode == "valid") != (out.Kind == ev.Success) {
				t.Fatal(mode, out, e, count)
			}
		})
	}
}
