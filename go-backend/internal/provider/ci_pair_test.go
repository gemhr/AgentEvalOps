package provider

import (
	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
)

type pairReaderFixture struct{ state ev.RunState }

func (r *pairReaderFixture) ReadRunState(context.Context, asset.Scope, string) (ev.RunState, error) {
	return r.state, nil
}

// 原 Target/HTTP/严格 Reader 的公开内存接线，不创建 Kernel/PG/模型执行。
func TestCIPairTransportContract(t *testing.T) {
	var vector struct {
		Request ev.Request `json:"request"`
		Scope   ev.Scope   `json:"scope"`
		Plan    string     `json:"plan_digest"`
		Wire    asset.JSON `json:"wire"`
	}
	raw, e := os.ReadFile("../agentquality/testdata/ci_pair_wire.json")
	if e != nil {
		t.Fatal(e)
	}
	j, e := asset.ParseJSON(raw)
	if e != nil || j.Decode(&vector) != nil {
		t.Fatal("public vector")
	}
	for _, tc := range []struct {
		name   string
		status int
		mutate string
	}{{"sealed", 200, ""}, {"accepted-stays-unknown", 202, ""}, {"attempt-rejected", 200, "attempt"}, {"gt-version-rejected", 200, "gt"}, {"target-version-rejected", 200, "target"}, {"reader-dataset-rejected", 200, "dataset"}, {"gt-in-input-rejected", 200, "input"}} {
		t.Run(tc.name, func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Header.Get("Authorization") != "Bearer public-unit-token" {
					t.Error("credential snapshot")
				}
				if r.Method == http.MethodPost {
					if r.URL.Path != "/api/runtime/evaluation-execute/ci-synthetic/v2" {
						t.Error("POST route")
					}
					body, _ := io.ReadAll(r.Body)
					payload, err := asset.ParseJSON(body)
					var fields map[string]asset.JSON
					if err != nil || payload.Decode(&fields) != nil || len(fields) != 6 {
						t.Error("strict visible envelope")
					}
					for _, forbidden := range []string{"ground_truth", "required_sources", "acceptance_criteria", "expected_output", "evaluator"} {
						if strings.Contains(string(body), "\""+forbidden+"\"") {
							t.Error("GT leaked into HTTP DTO")
						}
					}
					var f catalog.CIPairFrozen
					if fields["frozen_ci"].Decode(&f) != nil || f.Validate() != nil {
						t.Error("visible input DTO")
					}
				} else if r.Method != http.MethodGet || r.URL.Path != "/api/runtime/runs/"+vector.Request.AttemptID+"/ci-synthetic-evidence/v2" {
					t.Error("sealed GET route")
				}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(tc.status)
				if tc.status == 200 {
					_, _ = w.Write(vector.Wire.Bytes())
				}
			}))
			defer server.Close()
			q := vector.Request
			c := CIPairConfig{}
			if q.Target.Config.Decode(&c) != nil {
				t.Fatal("config")
			}
			c.Quality.BaseURL = server.URL
			c.Quality.TokenEnv = "CI_PAIR_PUBLIC_TEST_TOKEN"
			c.Quality.CallMilliseconds = 1000
			c.Quality.MaxResponseBytes = 2 * 1024 * 1024
			c.Quality.HTTP = DefaultHTTPConfig()
			t.Setenv(c.Quality.TokenEnv, "public-unit-token")
			q.Target.Config, _ = asset.Freeze(c)
			targetJSON, _ := asset.Freeze(q.Target)
			var m aq.QualityManifest
			_ = c.Quality.Manifest.Decode(&m)
			a := CIPairAuthorization{Schema: "ci-synthetic-attempt-authorization.v2", Attempt: q.AttemptID, Run: q.RunID, Request: q.ID, Project: vector.Scope.ProjectID, Principal: asset.NewID(), Tenant: "public-test-tenant", Credential: fmt.Sprintf("%x", sha256.Sum256([]byte("public-unit-token"))), Scopes: []string{"localagent:evaluation:execute", "localagent:ci-synthetic:evaluate"}, Target: targetJSON.Digest(), Plan: c.Frozen.PlanID, PlanVersion: c.Frozen.PlanVersion, PlanDigest: vector.Plan, Dataset: asset.Ref{EntityID: c.Frozen.Dataset, Version: c.Frozen.DatasetVersion}, Case: q.Case.Identity.Ref, InputDigest: c.Frozen.InputDigest, Operator: "public-operator", Source: "OPERATOR_FROZEN_CI", Mode: c.Mode, Prompt: c.Prompt, Manifest: c.Quality.Manifest.Digest(), Runtime: m.Agents["ci_incident_investigator"].ConfigDigest}
			reader := &pairReaderFixture{state: ev.RunState{Run: ev.Run{ID: q.RunID, ProjectID: vector.Scope.ProjectID, Snapshot: ev.RunSnapshot{Input: ev.SnapshotInput{Origin: ev.PublishedCatalog, ProjectID: vector.Scope.ProjectID, Dataset: &ev.AssetIdentity{Ref: a.Dataset, ProjectID: vector.Scope.ProjectID}, Manifest: []ev.CaseInput{q.Case}}, Target: q.Target, Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}}, Attempts: []ev.Attempt{{ID: q.AttemptID, RequestID: q.ID}}}}
			target, err := NewCIPairTarget(c, a, reader)
			if err != nil {
				t.Fatal("new pair target", err)
			}
			defer target.Close()
			bad := a
			bad.Scopes = bad.Scopes[:1]
			if rejected, err := NewCIPairTarget(c, bad, reader); err == nil {
				rejected.Close()
				t.Fatal("missing scope admitted")
			}
			switch tc.mutate {
			case "attempt":
				q.AttemptID = asset.NewID()
			case "target":
				q.Target.Version = CISyntheticTargetVersion
			case "gt":
				q.Case.Case.GroundTruth, _ = asset.Freeze(map[string]string{"schema_version": catalog.CIDiagnosisGTVersion})
			case "dataset":
				reader.state.Run.Snapshot.Input.Dataset.Ref.EntityID = asset.NewID()
			case "input":
				var fields map[string]asset.JSON
				_ = q.Case.Case.Input.Decode(&fields)
				fields["ground_truth"], _ = asset.Freeze("PARSER")
				q.Case.Case.Input, _ = asset.Freeze(fields)
			}
			// 构造后改变进程环境不替换已批准的凭据快照。
			t.Setenv(c.Quality.TokenEnv, "changed-public-token")
			out, err := target.Execute(context.Background(), vector.Scope, q)
			if err != nil {
				t.Fatal(err)
			}
			if tc.mutate != "" {
				if calls.Load() != 0 || out.DispatchCertainty != "NOT_DISPATCHED" {
					t.Fatal("unauthorized HTTP dispatch")
				}
				return
			}
			if tc.status == 202 {
				if calls.Load() != 1 || out.Kind != ev.Unknown || out.TerminalCertainty != "UNCONFIRMED" {
					t.Fatal("202 retried or declared success")
				}
				return
			}
			if out.Kind != ev.Success || out.TerminalCertainty != "REMOTE_CONFIRMED" || calls.Load() != 2 {
				t.Fatalf("strict new wire rejected: kind=%s terminal=%s calls=%d reason=%s", out.Kind, out.TerminalCertainty, calls.Load(), out.Reason)
			}
			if len(out.Evidence) != 2 || out.Evidence[1].Schema != aq.CIPairSidecarVersion {
				t.Fatal("source not persisted in original evidence outcome")
			}
		})
	}
}
