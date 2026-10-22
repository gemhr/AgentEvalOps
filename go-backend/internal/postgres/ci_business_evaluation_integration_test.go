//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/cibusiness"
	"bytes"
	"context"
	"encoding/json"
	"github.com/jackc/pgx/v5/pgxpool"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// 此节点只连接本轮明确配置的真实隔离 PG/API/Worker 和 LocalAgent HTTP，不装配 fixture Target。
func TestBusinessRunResultsAndComparison(t *testing.T) {
	base, project, token := os.Getenv("CI_BUSINESS_API_URL"), os.Getenv("CI_BUSINESS_PROJECT"), os.Getenv("CI_BUSINESS_PRODUCT_TOKEN")
	if base == "" || project == "" || token == "" || os.Getenv("CI_BUSINESS_PG_URL") == "" {
		t.Fatal("BLOCKED: real CI Business API/PG configuration required")
	}
	var published struct {
		Dataset, Suite, Policy asset.Ref
		Assets                 map[string]json.RawMessage
	}
	raw, e := os.ReadFile(os.Getenv("CI_BUSINESS_PUBLICATION"))
	if e != nil || json.Unmarshal(raw, &published) != nil {
		t.Fatal("real reviewed publication required", e)
	}
	pool, e := pgxpool.New(context.Background(), os.Getenv("CI_BUSINESS_PG_URL"))
	if e != nil {
		t.Fatal(e)
	}
	defer pool.Close()
	var resume struct {
		Runs []string `json:"runs"`
	}
	if previous, err := os.ReadFile(os.Getenv("CI_BUSINESS_EVIDENCE_PATH")); err == nil {
		_ = json.Unmarshal(previous, &resume)
	}
	evidence := map[string]any{"project_id": project, "dataset": published.Dataset, "suite": published.Suite, "policy": published.Policy, "status": "FAILED"}
	t.Cleanup(func() {
		if path := os.Getenv("CI_BUSINESS_EVIDENCE_PATH"); path != "" {
			body, _ := json.MarshalIndent(evidence, "", "  ")
			_ = os.MkdirAll(filepath.Dir(path), 0700)
			_ = os.WriteFile(path, body, 0600)
		}
	})
	call := func(method, path string, body any) map[string]json.RawMessage {
		t.Helper()
		var data []byte
		if body != nil {
			data, _ = json.Marshal(body)
		}
		req, _ := http.NewRequest(method, base+"/api/v1/projects/"+project+path, bytes.NewReader(data))
		req.Header.Set("Authorization", "Bearer "+token)
		if body != nil {
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("Idempotency-Key", asset.NewID())
		}
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		b, _ := io.ReadAll(res.Body)
		if res.StatusCode < 200 || res.StatusCode >= 300 {
			t.Fatalf("%s %s: %d %s", method, path, res.StatusCode, b)
		}
		var value map[string]json.RawMessage
		if json.Unmarshal(b, &value) != nil {
			t.Fatal("invalid real API response")
		}
		return value
	}
	dataset := call("GET", "/datasets/"+published.Dataset.EntityID+"/versions/"+published.Dataset.Version, nil)
	var content struct {
		Body struct {
			Cases []asset.Ref `json:"ordered_cases"`
		}
	}
	_ = json.Unmarshal(dataset["content"], &content)
	if len(content.Body.Cases) != 8 {
		t.Fatal("not reviewed eight-case dataset")
	}
	runs := []string{}
	allResults := []any{}
	for index, rule := range []string{cibusiness.V1, cibusiness.V2} {
		var id string
		if index < len(resume.Runs) {
			id = resume.Runs[index]
		} else {
			created := call("POST", "/ci-business/runs", map[string]any{"suite": published.Suite, "rule_version": rule})
			_ = json.Unmarshal(created["id"], &id)
		}
		runs = append(runs, id)
		evidence["runs"] = runs
		deadline := time.Now().Add(50 * time.Second)
		var state map[string]json.RawMessage
		for {
			state = call("GET", "/runs/"+id, nil)
			var status string
			_ = json.Unmarshal(state["pipeline_state"], &status)
			if status == "COMPLETED" {
				break
			}
			if status == "FAILED" || status == "OUTCOME_UNKNOWN" || time.Now().After(deadline) {
				evidence["failed_run"] = state
				t.Fatalf("real run did not complete: %s %s", id, status)
			}
			time.Sleep(time.Second)
		}
		var results []map[string]json.RawMessage
		_ = json.Unmarshal(state["results"], &results)
		if len(results) != 32 {
			t.Fatalf("expected 32 metric slots, got %d", len(results))
		}
		failed, passed, unknown := 0, 0, 0
		details := []any{}
		for _, result := range results {
			var verdict string
			_ = json.Unmarshal(result["evaluation_verdict"], &verdict)
			if verdict == "" {
				_ = json.Unmarshal(result["verdict"], &verdict)
			}
			switch verdict {
			case "PASS":
				passed++
			case "FAIL":
				failed++
			case "INCONCLUSIVE":
				unknown++
			default:
				t.Fatalf("unexpected verdict %s", verdict)
			}
		}
		if unknown != 13 || (rule == cibusiness.V1 && (failed != 1 || passed != 18)) || (rule == cibusiness.V2 && (failed != 0 || passed != 19)) {
			t.Fatalf("business scores: %s pass=%d fail=%d null=%d", rule, passed, failed, unknown)
		}
		for _, result := range results[:2] {
			var resultID string
			_ = json.Unmarshal(result["id"], &resultID)
			detail := call("GET", "/results/"+resultID+"/business-quality", nil)
			var actual cibusiness.Output
			_ = json.Unmarshal(detail["actual"], &actual)
			if actual.Rule != rule || actual.DefinitionDigest == "" {
				t.Fatal("actual rule missing")
			}
			details = append(details, detail)
			again := call("GET", "/results/"+resultID+"/business-quality", nil)
			a, _ := json.Marshal(detail)
			b, _ := json.Marshal(again)
			if !bytes.Equal(a, b) {
				t.Fatal("saved result changed on read")
			}
		}
		allResults = append(allResults, map[string]any{"run_id": id, "rule": rule, "pass": passed, "fail": failed, "inconclusive": unknown, "state": state, "details": details})
	}
	evidence["run_results"] = allResults
	gate := call("POST", "/gates", map[string]any{"baseline": map[string]any{"kind": "RUN_SET", "runs": []string{runs[0]}}, "candidate": map[string]any{"kind": "RUN_SET", "runs": []string{runs[1]}}, "policy": published.Policy})
	evidence["gate"] = gate
	var gateDecision, comparability string
	_ = json.Unmarshal(gate["decision"], &gateDecision)
	_ = json.Unmarshal(gate["comparability"], &comparability)
	if gateDecision != "PASS" || comparability != "COMPARABLE" {
		t.Fatalf("real gate must preserve applicable coverage and exclude paired NA: %s / %s", gateDecision, comparability)
	}
	var gateID string
	_ = json.Unmarshal(gate["gate_id"], &gateID)
	if gateID == "" {
		_ = json.Unmarshal(gate["id"], &gateID)
	}
	if gateID == "" {
		t.Fatal("missing real gate identity", gate)
	}
	cases := call("GET", "/gates/"+gateID+"/cases?limit=100", nil)
	evidence["case_comparison"] = cases
	var providerCalls int
	if e = pool.QueryRow(context.Background(), "SELECT COALESCE(sum(provider_call_count),0) FROM evaluation_evaluator_works WHERE project_id=$1", project).Scan(&providerCalls); e != nil || providerCalls != 0 {
		t.Fatal("unexpected model provider calls", providerCalls, e)
	}
	var attempts int
	if e = pool.QueryRow(context.Background(), "SELECT count(*) FROM evaluation_attempts WHERE project_id=$1 AND run_id=ANY($2::uuid[])", project, runs).Scan(&attempts); e != nil || attempts != 16 {
		t.Fatal("unexpected attempt count", attempts, e)
	}
	evidence["model_provider_calls"] = providerCalls
	evidence["attempts"] = attempts
	evidence["status"] = "PASS"
	t.Logf("real business runs %v; gate %s; 32 results per run, zero provider calls", runs, gateID)
}
