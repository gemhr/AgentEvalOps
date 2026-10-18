//go:build integration

package postgres_test

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	"io"
	"log/slog"
	"math"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"github.com/jackc/pgx/v5"
)

func TestAgentQualityBoundedCapacity(t *testing.T) {
	f := fixture(t)
	capacityPoolConfig := f.k.Pool.Config()
	capacityPoolConfig.MaxConns = 8
	capacityPool, capacityPoolErr := pgxpool.NewWithConfig(context.Background(), capacityPoolConfig)
	if capacityPoolErr != nil {
		t.Fatal(capacityPoolErr)
	}
	t.Cleanup(capacityPool.Close)
	f.k.Pool = capacityPool
	ctx, stopAll := context.WithTimeout(context.Background(), 35*time.Minute)
	defer stopAll()
	output := os.Getenv("QUALITY_CAPACITY_EVIDENCE_DIR")
	if output == "" {
		t.Fatal("explicit capacity artifact directory required")
	}
	capacityStarted := time.Now()
	live := qualityProducer(t, f.s.ProjectID, "--capacity")
	workerLog, e := os.Create(filepath.Join(os.Getenv("QUALITY_CAPACITY_EVIDENCE_DIR"), "worker.jsonl"))
	if e != nil {
		t.Fatal(e)
	}
	defer workerLog.Close()
	t.Setenv("PUBLIC_QUALITY_TEST_TOKEN", "public-quality-test-token")
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	var role string
	must(f.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&role))
	_, err := f.db.pool.Exec(ctx, "GRANT INSERT,UPDATE ON evaluation_policies,evaluation_policy_versions TO "+pgx.Identifier{role}.Sanitize()+"; GRANT INSERT ON evaluation_comparisons,evaluation_gate_receipts,evaluation_case_comparisons TO "+pgx.Identifier{role}.Sanitize())
	must(err)
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: f.k.Pool}, Datasets: postgres.Datasets{Pool: f.k.Pool}, Suites: postgres.Suites{Pool: f.k.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: f.k.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: f.k.Pool}}
	source := asset.Source{Kind: "PUBLIC_SYNTHETIC", Ref: "agent-quality-public.v1", Principal: f.s.Principal}
	ref := func() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "v1"} }
	cs := catalog.CaseService{Store: readers.Cases}
	ds := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	ms := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
	es := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	ss := catalog.SuiteService{Store: readers.Suites, References: readers}
	cases, err := agentquality.PublicQualityCases(live.Memory["allowed"], live.Memory["forbidden"])
	must(err)
	// 仅采用原公开 Handoff Case，GT 不修改、不发送给 Producer。
	cr := ref()
	_, err = cs.CreateCase(ctx, f.s.Scope, asset.Create{ID: cr.EntityID, Name: "public handoff drain"})
	must(err)
	_, err = cs.PublishCaseVersion(ctx, f.s.Scope, asset.Publish[catalog.CaseContent]{Ref: cr, Body: cases[2], Source: source})
	must(err)
	dr := ref()
	_, err = ds.CreateDataset(ctx, f.s.Scope, asset.Create{ID: dr.EntityID, Name: "single public handoff"})
	must(err)
	_, err = ds.PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: dr, Body: catalog.DatasetContent{Cases: []asset.Ref{cr}}, Source: source})
	must(err)
	metrics := []asset.Ref{}
	bindings := []catalog.EvaluatorBinding{}
	for _, name := range []string{"agent_execution_integrity.v1", "task_success.v1"} {
		var definition metric.Definition
		for _, b := range metric.Builtins() {
			if b.Key+"."+b.Version == name {
				definition = b.Definition
			}
		}
		mr, er := ref(), ref()
		_, err = ms.CreateMetricDefinition(ctx, f.s.Scope, asset.Create{ID: mr.EntityID, Name: name})
		must(err)
		_, err = ms.PublishMetricDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.Definition]{Ref: mr, Body: definition, Source: source})
		must(err)
		_, err = es.CreateEvaluatorDefinition(ctx, f.s.Scope, asset.Create{ID: er.EntityID, Name: name})
		must(err)
		body := agentquality.BehaviorEvaluatorDefinition(mr, name)
		_, err = es.PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: er, Body: body, Source: source})
		must(err)
		metrics = append(metrics, mr)
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: er, Metrics: []asset.Ref{mr}, Required: true, Applicability: body.Applicability})
	}
	suite := ref()
	_, err = ss.CreateSuite(ctx, f.s.Scope, asset.Create{ID: suite.EntityID, Name: "handoff drain required"})
	must(err)
	_, err = ss.PublishSuiteVersion(ctx, f.s.Scope, asset.Publish[catalog.SuiteContent]{Ref: suite, Body: catalog.SuiteContent{Dataset: &dr, Evaluators: bindings, Metrics: metrics, Applicability: asset.Applicability{RuleRef: "agent-quality-suite.v1"}}, Source: source})
	must(err)
	snapshot, err := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Suite: &suite})
	must(err)
	caps, err := f.k.LoadEvaluatorCapabilities(ctx, agentquality.DeterministicSupported)
	must(err)
	f.k.Capabilities = caps
	configs := map[string]asset.JSON{}
	create := func(variant string) string {
		cfg := live.Configs[variant]
		cfg.HTTP = provider.DefaultHTTPConfig()
		j, e := asset.Freeze(cfg)
		must(e)
		configs[variant] = j
		subject, e := asset.Freeze(map[string]string{"subject": "core_router", "subject_version": cfg.Manifest.Digest(), "environment": "public-isolated-pg", "run_mode": "agent-quality-public"})
		must(e)
		reply, e := f.k.CreateRun(ctx, f.s, ev.CreateRun{CommandID: asset.NewID(), Snapshot: snapshot, Target: ev.Target{ID: provider.AgentQualityTargetID, Kind: "LOCALAGENT_HTTP", Version: provider.AgentQualityTargetVersion, TimeoutMilliseconds: 15000, Config: j}, Subject: subject, Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}})
		must(e)
		if reply.Code != ev.Applied {
			t.Fatal(reply.Code)
		}
		return reply.ID
	}
	slots := 1
	start := func(variant string) (context.CancelFunc, chan error) {
		cfg := live.Configs[variant]
		cfg.HTTP = provider.DefaultHTTPConfig()
		target, e := provider.NewAgentQualityTarget(cfg, nil)
		must(e)
		t.Cleanup(func() { target.Close() })
		wc := worker.DefaultConfig()
		wc.ExecutionConcurrency = slots
		wc.EvaluatorConcurrency = slots
		wc.PollInterval = 100 * time.Millisecond
		wc.MaxBackoff = 2 * time.Second
		wc.ScanBatch = 16
		wc.DrainTimeout = 20 * time.Second
		runtime := &worker.Runtime{Config: wc, Backend: f.k, Target: target, Evaluator: agentquality.DeterministicEvaluator{}, Epoch: f.s.Epoch, Log: slog.New(slog.NewJSONHandler(workerLog, nil))}
		running, cancel := context.WithCancel(ctx)
		done := make(chan error, 1)
		finished := make(chan struct{})
		go func() { done <- runtime.Run(running); close(finished) }()
		t.Cleanup(func() {
			cancel()
			select {
			case <-finished:
			case <-time.After(30 * time.Second):
				t.Error("worker cleanup deadline")
			}
		})
		return cancel, done
	}
	waitRun := func(id string) {
		until := time.Now().Add(40 * time.Second)
		for time.Now().Before(until) {
			state, e := f.k.ReadRunState(ctx, f.s.Scope, id)
			must(e)
			if state.Run.Status.Terminal() {
				return
			}
			time.Sleep(30 * time.Millisecond)
		}
		t.Fatal("single-case worker terminal timeout")
	}
	_ = waitRun
	_ = configs
	_ = strings.Count
	// 一个批次总预算：G1<=150，G2<=100，G3<=300，G4<=10。
	var all []map[string]any
	save := func() {
		b, _ := json.MarshalIndent(all, "", "  ")
		must(os.WriteFile(filepath.Join(output, "capacity-results.json"), b, 0600))
	}
	quantiles := func(values []float64) map[string]any {
		if len(values) == 0 {
			return map[string]any{"n": 0}
		}
		sort.Float64s(values)
		q := func(p float64) float64 { return values[int(math.Ceil(p*float64(len(values))))-1] }
		return map[string]any{"n": len(values), "p50_ms": q(.5), "p95_ms": q(.95), "p99_ms": q(.99), "min_ms": values[0], "max_ms": values[len(values)-1]}
	}
	client := &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{Proxy: nil, MaxConnsPerHost: 8}}
	defer client.CloseIdleConnections()
	config := live.Configs["baseline"]
	var profile agentquality.QualityProfile
	must(config.Profile.Decode(&profile))
	var directMu float64
	coreLevels := []int{1, 2, 4}
	g2Only := os.Getenv("QUALITY_CAPACITY_GROUP") == "g2-only"
	if g2Only {
		coreLevels = nil
	}
	for _, concurrency := range coreLevels {
		started := time.Now()
		latencies := []float64{}
		failures := 0
		kinds := []string{}
		for i := 0; i < 50; i++ {
			kind := "loop"
			if i >= 30 && i < 40 {
				kind = "batch"
			}
			if i >= 40 && i < 48 {
				kind = "handoff"
			}
			if i >= 48 {
				kind = "compaction"
			}
			kinds = append(kinds, kind)
		}
		var mu sync.Mutex
		jobs := make(chan string)
		var wg sync.WaitGroup
		for j := 0; j < concurrency; j++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for kind := range jobs {
					id := asset.NewID()
					start := time.Now()
					payload, _ := json.Marshal(map[string]any{"agent_id": "core_router", "query": "public loop_a loop_b case=" + kind, "run_id": id, "attempt_id": id, "request_id": asset.NewID(), "evaluation_project_id": f.s.ProjectID, "evaluation_run_id": asset.NewID(), "case_id": cr.EntityID, "case_version": "v1", "case_digest": snapshot.Input().Manifest[0].Identity.ContentDigest, "execution_manifest_digest": config.Manifest.Digest(), "capture_profile_id": profile.ID, "capture_profile_version": profile.Version, "capture_profile_digest": config.Profile.Digest(), "timeout_milliseconds": 15000})
					req, _ := http.NewRequest("POST", config.BaseURL+"/api/runtime/evaluation-execute/agent-quality/v1", strings.NewReader(string(payload)))
					req.Header.Set("Authorization", "Bearer public-quality-test-token")
					req.Header.Set("Content-Type", "application/json")
					response, err := client.Do(req)
					status := "TRANSPORT"
					var wire map[string]any
					if err == nil {
						body, _ := io.ReadAll(io.LimitReader(response.Body, 2*1024*1024+1))
						response.Body.Close()
						_ = json.Unmarshal(body, &wire)
						status, _ = wire["status"].(string)
						if response.StatusCode != 200 {
							status = fmt.Sprint(response.StatusCode)
						}
					}
					mu.Lock()
					latencies = append(latencies, float64(time.Since(start).Microseconds())/1000)
					if status != "SUCCEEDED" {
						failures++
						t.Logf("G1 failure kind=%s status=%s detail=%v", kind, status, wire)
					}
					mu.Unlock()
				}
			}()
		}
		for _, kind := range kinds {
			mu.Lock()
			stop := failures >= 3
			mu.Unlock()
			if stop || time.Since(started) > 180*time.Second {
				break
			}
			jobs <- kind
		}
		close(jobs)
		wg.Wait()
		elapsed := time.Since(started).Seconds()
		entry := map[string]any{"group": "G1", "concurrency": concurrency, "elapsed_s": elapsed, "completed": len(latencies), "failed": failures, "latency": quantiles(latencies), "latency_samples_ms": latencies, "throughput": float64(len(latencies)-failures) / elapsed}
		all = append(all, entry)
		save()
		t.Logf("G1 %+v", entry)
		if concurrency == 2 {
			directMu = float64(len(latencies)-failures) / elapsed
		}
		if failures > 0 {
			t.Fatal("STOP: G1 failure; preserve evidence, no higher load")
		}
	}
	// G2：原 Target / Worker / 两项 Required Evaluator，当前池显式 max8。
	var evalMu float64
	selectedRuns := []string{}
	for _, nslots := range []int{1, 2} {
		slots = nslots
		started := time.Now()
		cancel, done := start("baseline")
		ids := []string{}
		durations := []float64{}
		completed := 0
		failures := 0
		for wave := 0; wave < 25; wave++ {
			waveIDs := []string{}
			times := map[string]time.Time{}
			for i := 0; i < 2; i++ {
				id := create("baseline")
				ids = append(ids, id)
				waveIDs = append(waveIDs, id)
				times[id] = time.Now()
			}
			for _, id := range waveIDs {
				deadline := time.Now().Add(30 * time.Second)
				for time.Now().Before(deadline) {
					state, e := f.k.ReadRunState(ctx, f.s.Scope, id)
					must(e)
					if state.Run.Status.Terminal() {
						completed++
						durations = append(durations, float64(time.Since(times[id]).Microseconds())/1000)
						if len(state.Attempts) != 1 || state.Attempts[0].Outcome != ev.Success || len(state.Results) != 2 {
							failures++
						}
						for _, r := range state.Results {
							if r.Value.Verdict != "PASS" {
								failures++
							}
						}
						break
					}
					time.Sleep(100 * time.Millisecond)
				}
			}
			if failures > 0 || completed != len(ids) || time.Since(started) > 180*time.Second {
				break
			}
		}
		stopStarted := time.Now()
		cancel()
		must(<-done)
		elapsed := time.Since(started).Seconds()
		stats := f.k.Pool.Stat()
		var mem runtime.MemStats
		runtime.ReadMemStats(&mem)
		entry := map[string]any{"group": "G2", "slots": nslots, "elapsed_s": elapsed, "created": len(ids), "completed": completed, "failed": failures, "throughput": float64(completed) / elapsed, "work_per_second": float64(completed*2) / elapsed, "latency": quantiles(durations), "latency_samples_ms": durations, "run_ids": ids, "pool_max": stats.MaxConns(), "pool_wait_ns": stats.AcquireDuration().Nanoseconds(), "goroutines": runtime.NumGoroutine(), "heap_bytes": mem.HeapAlloc, "drain_ms": float64(time.Since(stopStarted).Microseconds()) / 1000}
		all = append(all, entry)
		save()
		t.Logf("G2 slots=%d completed=%d elapsed=%f", nslots, completed, elapsed)
		if len(ids) > 0 {
			selectedRuns = append(selectedRuns, ids[0])
		}
		if nslots == 2 {
			evalMu = float64(completed) / elapsed
		}
		if failures > 0 || completed != len(ids) {
			t.Fatal("STOP: G2 unsafe/incomplete outcomes")
		}
	}
	// 同一 manifest/Case/GT；实际发布 Policy、生成并重读原 Gate。
	one := floatRef(1)
	policy := decision.Policy{Offline: true, Criticalities: []string{"CRITICAL"}, CriticalMissing: true, BlockPerCase: true, Coverage: decision.CoveragePolicy{MinimumDecision: 1, MinimumEvaluation: 1}, TaskSuccess: &decision.MetricRule{Metric: metrics[1], Aggregation: "rate", Minimum: one, MinimumCoverage: 1, Required: true, PerCase: true}, Metrics: []decision.MetricRule{{Metric: metrics[0], Aggregation: "rate", Minimum: one, MinimumCoverage: 1, Required: true, PerCase: true}}}
	store := postgres.Decisions{Pool: f.k.Pool}
	pr := ref()
	_, err = store.CreatePolicy(ctx, f.s.Scope, asset.Create{ID: pr.EntityID, Name: "bounded capacity"})
	must(err)
	_, err = store.PublishPolicyVersion(ctx, f.s.Scope, asset.Publish[decision.Policy]{Ref: pr, Body: policy, Source: source})
	must(err)
	gateStart := time.Now()
	command := decision.Command{ID: asset.NewID(), Baseline: decision.SourceRef{Kind: "RUN_SET", Runs: []string{selectedRuns[0]}}, Candidate: decision.SourceRef{Kind: "RUN_SET", Runs: []string{selectedRuns[1]}}, Policy: pr}
	gate, err := store.CreateGate(ctx, f.s, command)
	must(err)
	saved, err := store.GetGateReceipt(ctx, f.s.Scope, command.ID)
	must(err)
	all = append(all, map[string]any{"group": "gate", "decision": gate.Decision, "comparability": gate.Compatibility, "saved_decision": saved.Decision, "ms": float64(time.Since(gateStart).Microseconds()) / 1000, "id": command.ID})
	save()
	if gate.Decision != decision.Pass {
		t.Fatal("real comparable normal gate not PASS", gate.Reasons)
	}
	if g2Only {
		return
	}
	// G3：基于实测能力冻结到达率。最多600s、300 Attempt，不重置总预算。
	rate := math.Min(.5, .5*math.Min(directMu, evalMu))
	if rate <= 0 {
		t.Fatal("missing measured rate")
	}
	slots = 2
	soakStart := time.Now()
	cancel, done := start("baseline")
	var soakIDs []string
	var samples []map[string]any
	next := soakStart
	completed := 0
	failed := 0
	for time.Since(soakStart) < 600*time.Second && len(soakIDs) < 300 {
		if time.Now().Before(next) {
			time.Sleep(time.Until(next))
		}
		if time.Since(soakStart) >= 600*time.Second {
			break
		}
		id := create("baseline")
		soakIDs = append(soakIDs, id)
		next = next.Add(time.Duration(float64(time.Second) / rate))
		var memory runtime.MemStats
		runtime.ReadMemStats(&memory)
		stats := f.k.Pool.Stat()
		var pending int
		var oldest float64
		must(f.db.pool.QueryRow(ctx, "SELECT count(*),coalesce(max(extract(epoch FROM(clock_timestamp()-created_at))),0) FROM evaluation_evaluator_works WHERE status='PENDING'").Scan(&pending, &oldest))
		samples = append(samples, map[string]any{"elapsed_s": time.Since(soakStart).Seconds(), "goroutines": runtime.NumGoroutine(), "heap_bytes": memory.HeapAlloc, "pool_total": stats.TotalConns(), "pool_acquired": stats.AcquiredConns(), "pending_work": pending, "oldest_work_s": oldest, "planned_lateness_ms": math.Max(0, float64(time.Since(next.Add(-time.Duration(float64(time.Second)/rate))).Microseconds())/1000)})
		// 发送上限与原slots共同限制Root；到达堆积则记录并停止，不无限排队。
		var outstanding int
		must(f.db.pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_runs WHERE status='RUNNING'").Scan(&outstanding))
		if outstanding > 8 {
			t.Fatal("STOP: bounded arrival queue exceeded")
		}
		if memory.HeapAlloc > 1024*1024*1024 || oldest > 30 {
			t.Fatal("STOP: soak resource/backlog")
		}
	}
	for _, id := range soakIDs {
		state, e := f.k.ReadRunState(ctx, f.s.Scope, id)
		must(e)
		if state.Run.Status.Terminal() {
			completed++
		}
		if len(state.Attempts) != 1 || state.Attempts[0].Outcome != ev.Success || len(state.Results) != 2 {
			failed++
		}
		for _, r := range state.Results {
			if r.Value.Verdict != "PASS" {
				failed++
			}
		}
	}
	cancel()
	must(<-done)
	all = append(all, map[string]any{"group": "G3", "rate": rate, "elapsed_s": time.Since(soakStart).Seconds(), "created": len(soakIDs), "completed": completed, "failed": failed, "warmup_s": 120, "samples": samples, "run_ids": soakIDs})
	save()
	if failed > 0 {
		t.Fatal("STOP: soak outcome")
	}
	// G4：正常关门和一次独占实验容器PG暂停；独立恢复守卫。
	drainStart := time.Now()
	response, err := client.Post(strings.TrimSuffix(config.BaseURL, "/baseline")+"/capacity-drain", "application/json", nil)
	must(err)
	body, _ := io.ReadAll(response.Body)
	response.Body.Close()
	drainDuration := float64(time.Since(drainStart).Microseconds()) / 1000
	container := os.Getenv("QUALITY_CAPACITY_PG_CONTAINER")
	if container != "agent-capacity-20261018-0100" {
		t.Fatal("isolated PG identity missing")
	}
	pauseStart := time.Now()
	must(exec.Command("docker", "pause", container).Run())
	defer exec.Command("docker", "unpause", container).Run()
	go func() { time.Sleep(6 * time.Second); _ = exec.Command("docker", "unpause", container).Run() }()
	probeCtx, probeCancel := context.WithTimeout(ctx, 2*time.Second)
	probeErr := f.k.Pool.Ping(probeCtx)
	probeCancel()
	time.Sleep(time.Until(pauseStart.Add(5 * time.Second)))
	must(exec.Command("docker", "unpause", container).Run())
	restoreStart := time.Now()
	must(f.k.Pool.Ping(ctx))
	all = append(all, map[string]any{"group": "G4", "drain_ms": drainDuration, "drain_response": string(body), "pg_pause_s": time.Since(pauseStart).Seconds(), "pg_unavailable_observed": probeErr != nil, "pg_requery_ms": float64(time.Since(restoreStart).Microseconds()) / 1000, "recovery_takeover": "NOT_EXERCISED", "total_elapsed_s": time.Since(capacityStarted).Seconds()})
	save()
}
