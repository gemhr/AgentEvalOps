//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/analytics"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/httpapi"
	ob "agentevalops/go-backend/internal/observation"
	"agentevalops/go-backend/internal/postgres"
	"context"
	"encoding/json"
	"fmt"
	"github.com/jackc/pgx/v5"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

func g9Evidence(t *testing.T, name string, value any) {
	t.Helper()
	dir := os.Getenv("G9_EVIDENCE_DIR")
	if dir == "" {
		return
	}
	if e := os.MkdirAll(dir, 0700); e != nil {
		t.Fatal(e)
	}
	raw, e := json.MarshalIndent(value, "", "  ")
	mustG7(t, e)
	mustG7(t, os.WriteFile(filepath.Join(dir, name), raw, 0600))
}

// fixture-only COPY 保留 StoreObservation 的同一 canonicalization；不成为 runtime path。
func seedG9Observations(t *testing.T, x *g8Fixture, n int) {
	t.Helper()
	ctx := context.Background()
	rows := make([][]any, 0, n)
	for i := 0; i < n; i++ {
		o := ob.Observation{Ref: ob.Ref{ProjectID: x.s.ProjectID, ID: asset.NewID(), Kind: ob.TraceKind, Schema: "g9-controlled.v1"}, Source: "G9_CONTROLLED", Principal: x.s.Principal, Trust: "NORMAL_OBSERVATION", TraceID: asset.NewID(), RuntimeID: fmt.Sprint("g9-", i%20), Status: "OK", Completed: time.Now().UTC().Add(-time.Duration(i%168) * time.Hour), Retention: "EXPLICIT_EVALUATION_RETENTION", BodyAvailability: "SAFE_METADATA_ONLY"}
		o.Envelope, _ = asset.Freeze(map[string]any{"status": "OK", "fixture_index": i})
		j, e := asset.Freeze(o)
		mustG7(t, e)
		o.Canonical = j.Bytes()
		o.Ref.Digest = j.Digest()
		raw, e := json.Marshal(o)
		mustG7(t, e)
		rows = append(rows, []any{o.Ref.ID, x.s.ProjectID, string(o.Ref.Kind), o.Source, o.Principal, o.Trust, o.Ref.ID, o.TraceID, o.RuntimeID, o.Ref.Schema, o.Ref.Digest, o.Canonical, o.Canonical, raw, o.Completed})
	}
	count, e := x.db.pool.CopyFrom(ctx, pgx.Identifier{"evaluation_observations"}, []string{"id", "project_id", "kind", "source", "source_principal", "trust", "source_identity", "trace_id", "runtime_id", "schema_version", "digest", "canonical_bytes", "envelope_bytes", "observation_bytes", "completed_at"}, pgx.CopyFromRows(rows))
	mustG7(t, e)
	if count != int64(n) {
		t.Fatalf("COPY %d want %d", count, n)
	}
	var sample string
	mustG7(t, x.db.pool.QueryRow(ctx, `SELECT id::text FROM evaluation_observations WHERE project_id=$1 LIMIT 1`, x.s.ProjectID).Scan(&sample))
	o, e := x.online.GetObservation(ctx, x.s.Scope, sample)
	mustG7(t, e)
	canonical := o
	canonical.Ref.Digest = ""
	canonical.Canonical = nil
	canonical.Created = time.Time{}
	canonical.ParentIntegrity = ""
	j, e := asset.Freeze(canonical)
	mustG7(t, e)
	if j.Digest() != o.Ref.Digest {
		t.Fatal("fixture canonical identity drift")
	}
}

func TestG9ControlledCapacity(t *testing.T) {
	if os.Getenv("G9_SCALE") != "1" {
		t.Skip("explicit SMALL/MEDIUM controlled capacity harness")
	}
	config := httpapi.DefaultConfig()
	config.RequestsPerMinute = 100000
	x := g8WithConfig(t, config)
	x.api.Log = slog.New(slog.NewJSONHandler(io.Discard, nil))
	run := x.run(t, "capacity", g6Plan{Success: 10})
	gateID := asset.NewID()
	_, gateErr := x.gate.CreateGate(context.Background(), x.s, decision.Command{ID: gateID, Baseline: run, Candidate: run, Policy: x.policy(t, nil)})
	mustG7(t, gateErr)
	reader := postgres.ProductReader{Pool: x.pool}
	q := analytics.Query{From: time.Now().UTC().Add(-30 * 24 * time.Hour), Until: time.Now().UTC().Add(time.Hour), Bucket: "day"}
	for _, profile := range []struct {
		Name  string
		Added int
	}{{"SMALL", 10000}, {"MEDIUM", 40000}} {
		seedG9Observations(t, x, profile.Added)
		_, e := x.db.pool.Exec(context.Background(), "ANALYZE")
		mustG7(t, e)
		plans := map[string]json.RawMessage{}
		dbLatency := map[string]float64{}
		for _, kind := range []string{"overview", "quality", "trends", "gates", "online", "human"} {
			before := time.Now()
			raw, e := reader.ExplainAnalytics(context.Background(), x.s.Scope, q, kind)
			mustG7(t, e)
			dbLatency[kind] = float64(time.Since(before).Microseconds()) / 1000
			plans[kind] = raw
		}
		// 与对应 owner/read page 的 SQL 一致；仅保存 EXPLAIN，不改变运行时查询。
		pagePlans := []struct {
			name, sql string
			args      []any
		}{
			{"runs", `SELECT r.id::text,'',r.created_at FROM evaluation_runs r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND ($3::uuid IS NULL OR r.id>$3) ORDER BY r.id LIMIT $4`, []any{x.s.ProjectID, x.s.OrganizationID, nil, 26}},
			{"results", `SELECT to_jsonb(x),decode(x.metadata->>'ValueBytes','base64') FROM evaluation_results x JOIN projects p ON p.id=x.project_id WHERE x.project_id=$1 AND p.org_id=$2 AND ($3::uuid IS NULL OR x.run_id=$3) AND ($4::uuid IS NULL OR x.id>$4) ORDER BY x.id LIMIT $5`, []any{x.s.ProjectID, x.s.OrganizationID, run.Runs[0], nil, 26}},
			{"gates", `SELECT r.gate_id::text,'',r.created_at FROM evaluation_gate_receipts r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND ($3::uuid IS NULL OR r.gate_id>$3) ORDER BY r.gate_id LIMIT $4`, []any{x.s.ProjectID, x.s.OrganizationID, nil, 26}},
			{"gate_cases", `SELECT comparison_bytes FROM evaluation_case_comparisons WHERE project_id=$1 AND gate_id=$2 AND unit_key COLLATE "C">$3 COLLATE "C" ORDER BY unit_key COLLATE "C" LIMIT $4`, []any{x.s.ProjectID, gateID, "", 26}},
			{"review_queue", `SELECT r.item_bytes,r.status,r.created_at FROM evaluation_review_items r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND r.status IN ('PENDING','IN_REVIEW','ADJUDICATION_REQUIRED') AND ($3::uuid IS NULL OR r.id>$3) ORDER BY r.id LIMIT $4`, []any{x.s.ProjectID, x.s.OrganizationID, nil, 26}},
			{"online_observations", `SELECT o.id::text FROM evaluation_observations o JOIN projects p ON p.id=o.project_id WHERE o.project_id=$1 AND p.org_id=$2 AND ($3::timestamptz IS NULL OR (o.created_at,o.id)>($3,$4::uuid)) ORDER BY o.created_at,o.id LIMIT $5`, []any{x.s.ProjectID, x.s.OrganizationID, nil, nil, 25}},
			{"online_work", `SELECT project_id::text,id::text,created_at FROM evaluation_online_works WHERE status='PENDING' AND ($1::timestamptz IS NULL OR (created_at,id)>($1,$2::uuid)) ORDER BY created_at,id LIMIT $3`, []any{nil, nil, 25}},
			{"failure_candidates", `SELECT r.created_at,r.kind,r.id::text,r.body FROM (SELECT created_at,'O'::text kind,id,observation_bytes body,project_id FROM evaluation_observations UNION ALL SELECT created_at,'R'::text,id,result_bytes,project_id FROM evaluation_online_results) r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND ($3::timestamptz IS NULL OR (r.created_at,r.kind,r.id)>=($3,$4,$5::uuid)) ORDER BY r.created_at,r.kind,r.id LIMIT 101`, []any{x.s.ProjectID, x.s.OrganizationID, nil, "", nil}},
		}
		for _, page := range pagePlans {
			var raw []byte
			mustG7(t, x.db.pool.QueryRow(context.Background(), "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "+page.sql, page.args...).Scan(&raw))
			plans[page.name] = raw
		}
		g9Evidence(t, "plans-"+profile.Name+".json", plans)
		const n = 1000
		const concurrency = 8
		latency := make([]float64, n)
		statuses := make([]int, n)
		jobs := make(chan int)
		var wg sync.WaitGroup
		paths := []string{"/overview", "/analytics/quality", "/analytics/trends", "/analytics/gates", "/analytics/online", "/analytics/human", "/runs", "/runs/" + run.Runs[0] + "/results?limit=25"}
		begin := time.Now()
		client := &http.Client{Timeout: 10 * time.Second}
		for c := 0; c < concurrency; c++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for i := range jobs {
					method, path, who := "GET", paths[i%len(paths)], "reader"
					var body io.Reader
					if i%100 == 0 {
						method, path, who = "POST", "/cases", "judge"
						body = strings.NewReader(fmt.Sprintf(`{"name":"g9-%s-%d"}`, profile.Name, i))
					}
					req, e := http.NewRequest(method, x.url(path), body)
					if e != nil {
						statuses[i] = 0
						continue
					}
					req.Header.Set("Authorization", "Bearer "+x.tokens[who])
					if method == "POST" {
						req.Header.Set("Content-Type", "application/json")
						req.Header.Set("Idempotency-Key", fmt.Sprintf("g9-%s-%d", profile.Name, i))
					}
					start := time.Now()
					response, e := client.Do(req)
					if e == nil {
						statuses[i] = response.StatusCode
						_, _ = io.Copy(io.Discard, response.Body)
						response.Body.Close()
					}
					latency[i] = float64(time.Since(start).Microseconds()) / 1000
				}
			}()
		}
		maxAcquired := int32(0)
		for i := 0; i < n; i++ {
			jobs <- i
			if v := x.pool.Stat().AcquiredConns(); v > maxAcquired {
				maxAcquired = v
			}
		}
		close(jobs)
		wg.Wait()
		elapsed := time.Since(begin).Seconds()
		client.CloseIdleConnections()
		sort.Float64s(latency)
		errors := 0
		for _, s := range statuses {
			if s != 200 {
				errors++
			}
		}
		var memory runtime.MemStats
		runtime.ReadMemStats(&memory)
		var count, size int64
		mustG7(t, x.db.pool.QueryRow(context.Background(), "SELECT count(*),pg_database_size(current_database()) FROM evaluation_observations WHERE project_id=$1", x.s.ProjectID).Scan(&count, &size))
		stats := x.pool.Stat()
		evidence := map[string]any{"classification": "MEASURED_CONTROLLED_CAPACITY", "profile": profile.Name, "observations": count, "results": 30, "requests": n, "concurrency": concurrency, "p50_ms": latency[n/2], "p95_ms": latency[n*95/100], "p99_ms": latency[n*99/100], "throughput_rps": float64(n) / elapsed, "error_count": errors, "error_rate": float64(errors) / n, "pool_max": stats.MaxConns(), "pool_peak_acquired": maxAcquired, "pool_total": stats.TotalConns(), "pool_acquire_wait_ns": stats.AcquireDuration().Nanoseconds(), "go_heap_alloc_bytes": memory.HeapAlloc, "database_bytes": size, "db_explain_roundtrip_ms": dbLatency, "query_classification": "DIRECT_QUERY_OK at measured profiles; no unmeasured warehouse claim"}
		evidence["request_mix"] = map[string]int{"read": 990, "create_case": 10}
		g9Evidence(t, "capacity-"+profile.Name+".json", evidence)
		t.Logf("%s %+v", profile.Name, evidence)
		if errors != 0 {
			t.Fatalf("load errors %d", errors)
		}
	}
}
