//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/analytics"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	rv "agentevalops/go-backend/internal/review"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestG9AnalyticsCanonicalFacts(t *testing.T) {
	x := g8(t)
	run := x.run(t, "base", g6Plan{Success: 8, Missing: true, Unknown: true})
	ctx := context.Background()
	base := x.run(t, "complete", g6Plan{Success: 10})
	policy := x.policy(t, nil)
	for _, candidate := range []decision.SourceRef{base, run, x.run(t, "regression", g6Plan{Success: 9, CriticalFail: true})} {
		_, e := x.gate.CreateGate(ctx, x.s, decision.Command{ID: asset.NewID(), Baseline: base, Candidate: candidate, Policy: policy})
		mustG7(t, e)
	}
	state, e := x.k.ReadRunState(ctx, x.s.Scope, base.Runs[0])
	mustG7(t, e)
	item := x.enqueue(t, g7Source(t, state, x.bindings[0].Evaluator, 0), x.refs[0], rv.TaskSuccess, 2, true)
	golden := x.double(t, item, "SUCCESS", "SUCCESS")
	c := rv.CalibrationCommand{ID: asset.NewID(), Dataset: x.dataset, Evaluator: x.bindings[0].Evaluator, Schema: x.refs[0], Protocol: rv.Protocol{Blind: true}, Sampling: rv.Sampling{Version: "review-hash.v1", Seed: "G9", BasisPoints: 10000}, PositiveClass: "SUCCESS"}
	for i, v := range state.Run.Snapshot.Input.Manifest {
		sample := rv.CalibrationSample{Case: v.Identity.Ref, Result: g7Source(t, state, x.bindings[0].Evaluator, i)}
		if i == 0 {
			sample.GoldenID = golden.ID
		}
		c.Samples = append(c.Samples, sample)
	}
	_, e = x.reviews.PrepareCalibration(ctx, x.reviewScope, c)
	mustG7(t, e)
	report, e := x.reviews.CompleteCalibration(ctx, x.reviewScope, c.ID)
	mustG7(t, e)
	ref, _ := x.rule(t, nil)
	id, _ := x.ingest(t, "OK")
	mustMaterialize(t, x.g5Fixture, ref, id)
	work := workID(t, x.g5Fixture, ref, id)
	claim, e := x.online.ClaimOnlineWork(ctx, x.s5, work, "g9", time.Minute)
	reply(t, claim, e, ev.Applied)
	w, _, e := x.online.GetOnlineEvaluationState(ctx, x.s.Scope, work)
	mustG7(t, e)
	value, e := (provider.OnlineEvaluator{DBTimeout: time.Second}).EvaluateOnline(ctx, x.s5, w)
	mustG7(t, e)
	r, e := x.online.FinalizeOnlineResult(ctx, x.s5, work, claim.Token, asset.NewID(), asset.NewID(), value)
	reply(t, r, e, ev.Applied)
	q := analytics.Query{From: time.Now().UTC().Add(-7 * 24 * time.Hour), Until: time.Now().UTC().Add(time.Hour), Bucket: "hour"}
	reader := postgres.ProductReader{Pool: x.pool}
	for _, kind := range []string{"overview", "quality", "trends", "gates", "online", "human"} {
		v, e := reader.Aggregate(context.Background(), x.s.Scope, q, kind)
		if e != nil {
			t.Fatalf("%s: %v", kind, e)
		}
		if kind == "gates" {
			var n, pass, fail, blocked float64
			for _, row := range v.Rows {
				n += row["receipts"].(float64)
				pass += row["pass"].(float64)
				fail += row["fail"].(float64)
				blocked += row["blocked"].(float64)
			}
			if n != 3 || pass != 1 || fail != 1 || blocked != 1 {
				t.Fatal("gate counts", v.Rows)
			}
		}
		if kind == "online" && (len(v.Rows) != 1 || v.Rows[0]["eligible"] != float64(1) || v.Rows[0]["sampled"] != float64(1)) {
			t.Fatal("online population", v.Rows)
		}
		if kind == "human" {
			goldenCount := float64(0)
			for _, row := range v.Rows {
				if count, ok := row["golden"].(float64); ok {
					goldenCount += count
				}
			}
			if len(v.Rows) != 2 || goldenCount != 1 {
				t.Fatal("human facts", v.Rows)
			}
		}
		if kind == "quality" {
			for _, row := range v.Rows {
				if row["subject_version"] == "base" && row["metric"] == "task_success.v1" && (row["unknown"] != float64(1) || row["expected"] != float64(10) || row["completed"] != float64(9)) {
					t.Fatal("denominator drift", row)
				}
			}
		}
		path := "/analytics/" + kind
		if kind == "overview" {
			path = "/overview"
		}
		x.call(t, "reader", "GET", x.url(path), "", "", 200)
	}
	if report.Agreement.PairedDecidable != 1 || report.Agreement.MissingHuman != 9 {
		t.Fatal("calibration population drift")
	}
	immutable := []string{"evaluation_case_versions", "evaluation_dataset_versions", "evaluation_metric_definition_versions", "evaluation_evaluator_definition_versions", "evaluation_runs", "evaluation_attempts", "evaluation_results", "evaluation_gate_receipts", "evaluation_case_comparisons", "evaluation_observations", "evaluation_online_rule_versions", "evaluation_online_results", "evaluation_human_annotations", "evaluation_golden_labels", "evaluation_calibration_reports"}
	fingerprints := func() map[string]string {
		out := map[string]string{}
		for _, table := range immutable {
			var raw []byte
			mustG7(t, x.db.pool.QueryRow(ctx, "SELECT convert_to(coalesce(jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r)::text),'[]'::jsonb)::text,'UTF8') FROM "+table+" r").Scan(&raw))
			out[table] = fmt.Sprintf("%x", sha256.Sum256(raw))
		}
		return out
	}
	prior := fingerprints()
	x.db.migrate(t, "head")
	current := fingerprints()
	for table, hash := range prior {
		if current[table] != hash {
			t.Fatal("read-only G8→G9 upgrade rewrote immutable facts", table)
		}
	}
	g9Evidence(t, "upgrade-immutable-fingerprints.json", map[string]any{"before": prior, "after": current, "schema_head": "c12a00800001", "change": "G9 read projections; no schema/data rewrite"})
	for _, path := range []string{"/exports/results?limit=2", "/exports/online?limit=1", "/exports/analytics/quality?limit=1"} {
		body := x.call(t, "reader", "GET", x.url(path), "", "", 200)
		if len(body["items"].([]any)) < 1 {
			t.Fatal("empty export", path)
		}
	}
	x.call(t, "reader", "GET", x.url("/exports/results?limit=101"), "", "", 400)
	request, e := http.NewRequest("GET", x.url("/exports/results?limit=2&format=ndjson"), nil)
	mustG7(t, e)
	request.Header.Set("Authorization", "Bearer "+x.tokens["reader"])
	response, e := http.DefaultClient.Do(request)
	mustG7(t, e)
	body, e := io.ReadAll(response.Body)
	response.Body.Close()
	mustG7(t, e)
	if response.StatusCode != 200 || response.Header.Get("Content-Type") != "application/x-ndjson" || len(bytes.Split(bytes.TrimSpace(body), []byte("\n"))) != 3 || strings.Contains(string(body), "ProviderCall") {
		t.Fatal("NDJSON export", response.StatusCode, string(body))
	}
	request, e = http.NewRequest("GET", x.url("/calibrations/"+c.ID), nil)
	mustG7(t, e)
	request.Header.Set("Authorization", "Bearer "+x.tokens["judge"])
	response, e = http.DefaultClient.Do(request)
	mustG7(t, e)
	tag := response.Header.Get("ETag")
	response.Body.Close()
	if tag == "" {
		t.Fatal("immutable projection lacks ETag")
	}
	request.Header.Set("If-None-Match", tag)
	response, e = http.DefaultClient.Do(request)
	mustG7(t, e)
	response.Body.Close()
	if response.StatusCode != 304 {
		t.Fatal("conditional", response.StatusCode)
	}
	_, e = x.db.pool.Exec(ctx, `DELETE FROM product_project_memberships WHERE project_id=$1 AND principal_id=$2`, x.s.ProjectID, x.principals["judge"].ID)
	mustG7(t, e)
	response, e = http.DefaultClient.Do(request)
	mustG7(t, e)
	response.Body.Close()
	if response.StatusCode != 404 {
		t.Fatal("revoke bypassed by ETag", response.StatusCode)
	}
	var before, after []byte
	mustG7(t, x.db.pool.QueryRow(ctx, `SELECT convert_to(kernel_snapshot::text,'UTF8') FROM evaluation_runs WHERE id=$1`, run.Runs[0]).Scan(&before))
	_, e = reader.Aggregate(ctx, x.s.Scope, q, "quality")
	mustG7(t, e)
	mustG7(t, x.db.pool.QueryRow(ctx, `SELECT convert_to(kernel_snapshot::text,'UTF8') FROM evaluation_runs WHERE id=$1`, run.Runs[0]).Scan(&after))
	if !bytes.Equal(before, after) {
		t.Fatal("read mutated source")
	}
	_ = json.Valid(before)
	items, e := reader.ResultPage(context.Background(), x.s.Scope, run.Runs[0], "", 3)
	if e != nil || len(items) != 3 || items[0].ID == "" || items[0].Value.Verdict == "" {
		t.Fatalf("result page: %+v %v", items, e)
	}
}
