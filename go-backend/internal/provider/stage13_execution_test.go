package provider

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestStage13MaterializedReceiptBinding(t *testing.T) {
	for _, mode := range []string{"valid", "semantic-tamper", "request-tamper"} {
		t.Run(mode, func(t *testing.T) {
			manifest := stage13UnitManifest(t)
			input, _ := asset.ParseJSON([]byte(`{"schema_version":"stage13.triage-input.v1","evidence_policy":{"deadline_at":"2020-01-01T00:00:00Z","max_tool_reads":8}}`))
			var actual asset.JSON
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				var b map[string]json.RawMessage
				_ = json.NewDecoder(r.Body).Decode(&b)
				raw, _ := json.Marshal(b)
				actual, _ = asset.ParseJSON(raw)
				var query, attempt string
				_ = json.Unmarshal(b["query"], &query)
				_ = json.Unmarshal(b["run_id"], &attempt)
				wire := stage13UnitWire(manifest, attempt, query)
				receipt := wire["actual_subject_receipt"].(map[string]any)
				semantic, _ := citriage.SemanticInput(input)
				receipt["semantic_input_digest"], receipt["execution_request_digest"] = semantic.Digest(), actual.Digest()
				p, _ := asset.ParseJSON(b["execution_policy"])
				receipt["execution_policy"] = p
				if mode == "semantic-tamper" {
					receipt["semantic_input_digest"] = "wrong"
				}
				if mode == "request-tamper" {
					receipt["execution_request_digest"] = "wrong"
				}
				sealReceipt(receipt)
				_ = json.NewEncoder(w).Encode(wire)
			}))
			defer server.Close()
			cfg := Stage13Config{Transport: DefaultLocalAgentConfig(server.URL, "WP05A_UNIT_TOKEN"), ExpectedSubjectManifest: manifest, ExecutionPolicyVersion: Stage13ExecutionPolicyVersion}
			t.Setenv("WP05A_UNIT_TOKEN", "controlled")
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
			if e != nil || calls != 1 {
				t.Fatal(e, calls)
			}
			if (mode == "valid") != (out.Kind == ev.Success) {
				t.Fatal(mode, out)
			}
			if mode == "valid" {
				var fields map[string]asset.JSON
				_ = actual.Decode(&fields)
				var p citriage.ExecutionPolicy
				_ = fields["execution_policy"].Decode(&p)
				if p.DeadlineAt.Sub(p.StartedAt) != 180*time.Second {
					t.Fatal(p)
				}
			}
		})
	}
}
