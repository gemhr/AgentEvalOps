//go:build integration

package postgres_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/bootstrap"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	ob "agentevalops/go-backend/internal/observation"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type g5Fixture struct {
	*g3Fixture
	online  postgres.Online
	s5      ob.Scope
	binding ob.Binding
}

func g5(t *testing.T) *g5Fixture {
	t.Helper()
	f := g3(t, worker.FixturePlan{Mode: "SUCCESS"}, worker.FixturePlan{Mode: "PASS"})
	ctx := context.Background()
	var role string
	if e := f.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&role); e != nil {
		t.Fatal(e)
	}
	grant := `GRANT INSERT,UPDATE ON evaluation_observations,evaluation_online_rules,evaluation_online_rule_versions,evaluation_online_bindings,evaluation_online_project_limits,evaluation_online_rule_usage,evaluation_online_admissions,evaluation_online_works,evaluation_online_backfills,localagent_external_trace_identity,localagent_external_span_identity TO ` + pgx.Identifier{role}.Sanitize() + `; GRANT INSERT ON evaluation_online_results,localagent_trace_envelope_sidecars,traces,spans TO ` + pgx.Identifier{role}.Sanitize()
	if _, e := f.db.pool.Exec(ctx, grant); e != nil {
		t.Fatal(e)
	}
	s := ob.Scope{Scope: f.s.Scope, Epoch: f.s.Epoch, Ingest: true, AuthenticatedSource: true, Evaluate: true, Reconcile: true}
	k := postgres.Online{Pool: f.k.Pool}
	budget := ob.Budget{MaxSampled: 100, MaxWorks: 100, MaxProviderCalls: 100, MaxEstimatedTokens: 1000000, MaxReportedTokens: 1000000}
	r, e := k.ConfigureProjectLimits(ctx, s, postgres.ProjectOnlinePolicy{Budget: budget, Rate: ob.Rate{Mode: "BOUNDED_CONCURRENCY"}})
	reply(t, r, e, ev.Applied)
	source := asset.Source{Kind: "TEST", Ref: "G5_CONTROLLED", Principal: s.Principal}
	ref := func() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "g5-v1"} }
	m, eRef := ref(), ref()
	metrics := postgres.MetricDefinitions{Pool: f.k.Pool}
	evaluators := postgres.EvaluatorDefinitions{Pool: f.k.Pool}
	md := metric.Builtins()[2].Definition
	md.Name = "observed_span_status.v1"
	md.Grain = metric.Trace
	md.ValueType = metric.Scalar
	md.Range = &[2]float64{0, 1}
	md.Applicability = asset.Applicability{RuleRef: "strict-status.v1", RequiredEvidence: []asset.EvidenceRequirement{{Kind: "observation", Schema: "trace-v1", BodyRequired: true}}}
	_, e = metrics.CreateMetricDefinition(ctx, s.Scope, asset.Create{ID: m.EntityID, Name: md.Name})
	if e != nil {
		t.Fatal(e)
	}
	mv, e := metrics.PublishMetricDefinitionVersion(ctx, s.Scope, asset.Publish[metric.Definition]{Ref: m, Body: md, Source: source})
	if e != nil {
		t.Fatal(e)
	}
	d := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: ob.Contract, OutputMetrics: []asset.Ref{m}, Applicability: md.Applicability, Availability: asset.ContractOnly, ImplementationRef: provider.ObservationStatusImplementation, SchemaVersion: "observation-status.v1", Normalization: "null-preserving.v1", Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 16384}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 3}}
	_, e = evaluators.CreateEvaluatorDefinition(ctx, s.Scope, asset.Create{ID: eRef.EntityID, Name: "status"})
	if e != nil {
		t.Fatal(e)
	}
	dv, e := evaluators.PublishEvaluatorDefinitionVersion(ctx, s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: eRef, Body: d, Source: source})
	if e != nil {
		t.Fatal(e)
	}
	identity := func(ref asset.Ref, digest string) ev.AssetIdentity {
		return ev.AssetIdentity{Ref: ref, ProjectID: s.ProjectID, ContentDigest: digest}
	}
	b := ob.Binding{Evaluator: ev.EvaluatorSpec{Identity: identity(eRef, dv.ContentDigest()), Definition: d, Metrics: []asset.Ref{m}, Required: true, Applicability: md.Applicability}, Metric: ev.MetricInput{Identity: identity(m, mv.ContentDigest()), Definition: md}}
	return &g5Fixture{f, k, s, b}
}
func (f *g5Fixture) rule(t *testing.T, change func(*ob.Rule)) (asset.Ref, ob.Rule) {
	t.Helper()
	ref := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	r := ob.Rule{Enabled: true, Scope: ob.TraceKind, Trust: "STRICT_AUTHENTICATED", Source: "LOCALAGENT_TRACE_V1", Sampling: ob.Sampling{Policy: "ALL", Algorithm: "sha256-trace-basis-points", Version: 1, BasisPoints: 10000}, Budget: ob.Budget{MaxSampled: 100, MaxWorks: 100, MaxProviderCalls: 100, MaxEstimatedTokens: 1000000, MaxReportedTokens: 1000000}, Rate: ob.Rate{Mode: "BOUNDED_CONCURRENCY"}, AllowBackfill: true, BackfillMax: 100, Bindings: []ob.Binding{f.binding}}
	if change != nil {
		change(&r)
	}
	_, e := f.online.CreateOnlineRule(context.Background(), f.s5.Scope, asset.Create{ID: ref.EntityID, Name: "online"})
	if e != nil {
		t.Fatal(e)
	}
	v, e := f.online.PublishOnlineRuleVersion(context.Background(), f.s5, ref, r)
	reply(t, v, e, ev.Applied)
	return ref, r
}
func traceBody(trace, span, run, status string) []byte {
	var errorCode any
	if status != "OK" {
		errorCode = "RUNTIME_ERROR"
	}
	raw, _ := json.Marshal(map[string]any{"contract_identity": ob.TraceIdentity, "contract_version": 1, "contract_fingerprint": ob.TraceFingerprint, "run_id": run, "trace_id": trace, "span_id": span, "parent_span_id": nil, "step_id": nil, "operation": "runtime.run", "component": "agentcore", "started_at": "2026-10-04T00:00:00Z", "completed_at": "2026-10-04T00:00:01Z", "duration_ms": 0.1, "status": status, "error_code": errorCode, "attributes": map[string]any{"runtime_mode": "direct", "step_count": 1}})
	return raw
}
func (f *g5Fixture) ingest(t *testing.T, status string) (string, []byte) {
	t.Helper()
	raw := traceBody(asset.NewID(), asset.NewID(), asset.NewID(), status)
	r, e := f.online.IngestStrict(context.Background(), f.s5, "LOCALAGENT_TRACE_V1", bytes.NewReader(raw))
	reply(t, r, e, ev.Applied)
	return r.ID, raw
}
func mustMaterialize(t *testing.T, f *g5Fixture, ref asset.Ref, id string) {
	t.Helper()
	r, e := f.online.Materialize(context.Background(), f.s5, ref, id, false)
	reply(t, r, e, ev.Applied)
}
func workID(t *testing.T, f *g5Fixture, ref asset.Ref, id string) string {
	t.Helper()
	var work string
	if e := f.k.Pool.QueryRow(context.Background(), `SELECT id::text FROM evaluation_online_works WHERE project_id=$1 AND rule_id=$2 AND rule_version=$3 AND observation_id=$4`, f.s.ProjectID, ref.EntityID, ref.Version, id).Scan(&work); e != nil {
		t.Fatal(e)
	}
	return work
}

func TestG5ReceiptRulesOwnershipAndBackfill(t *testing.T) {
	f := g5(t)
	ctx := context.Background()
	ref, rule := f.rule(t, nil)
	id, raw := f.ingest(t, "OK")
	r, e := f.online.IngestStrict(ctx, f.s5, "LOCALAGENT_TRACE_V1", bytes.NewReader(raw))
	reply(t, r, e, ev.AlreadyApplied)
	bad := bytes.Replace(raw, []byte(`"duration_ms":0.1`), []byte(`"duration_ms":0.2`), 1)
	r, e = f.online.IngestStrict(ctx, f.s5, "LOCALAGENT_TRACE_V1", bytes.NewReader(bad))
	reply(t, r, e, ev.Conflict)
	other := f.s5
	other.Scope = seedProject(t, f.db.pool, asset.NewID())
	r, e = f.online.IngestStrict(ctx, other, "LOCALAGENT_TRACE_V1", bytes.NewReader(raw))
	reply(t, r, e, ev.Conflict)
	if _, e = f.online.GetObservation(ctx, other.Scope, id); !errors.Is(e, asset.ErrNotFound) {
		t.Fatalf("cross project %v", e)
	}
	r, e = f.online.PublishOnlineRuleVersion(ctx, f.s5, ref, rule)
	reply(t, r, e, ev.AlreadyApplied)
	rule.Sampling.BasisPoints = 0
	rule.Sampling.Policy = "HASH_PERCENTAGE"
	r, e = f.online.PublishOnlineRuleVersion(ctx, f.s5, ref, rule)
	reply(t, r, e, ev.Conflict)
	mustMaterialize(t, f, ref, id)
	r, e = f.online.Materialize(ctx, f.s5, ref, id, false)
	reply(t, r, e, ev.AlreadyApplied)
	work := workID(t, f, ref, id)
	var wg sync.WaitGroup
	var winner atomic.Int32
	var claim ev.Reply
	var mu sync.Mutex
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r, e := f.online.ClaimOnlineWork(ctx, f.s5, work, "owner", 100*time.Millisecond)
			if e != nil {
				t.Error(e)
			}
			if r.Code == ev.Applied {
				winner.Add(1)
				mu.Lock()
				claim = r
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if winner.Load() != 1 {
		t.Fatal("claim competition")
	}
	// waiter 在行锁后越过 expiry，不能使用加锁前的 now 延续 owner。
	lock, e := f.db.pool.Begin(ctx)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = lock.Exec(ctx, `SELECT id FROM evaluation_online_works WHERE id=$1 FOR UPDATE`, work); e != nil {
		t.Fatal(e)
	}
	renewDone := make(chan struct {
		reply ev.Reply
		err   error
	}, 1)
	go func() {
		r, e := f.online.RenewOnlineLease(ctx, f.s5, work, claim.Token, time.Second)
		renewDone <- struct {
			reply ev.Reply
			err   error
		}{r, e}
	}()
	time.Sleep(120 * time.Millisecond)
	if e = lock.Commit(ctx); e != nil {
		t.Fatal(e)
	}
	renewed := <-renewDone
	reply(t, renewed.reply, renewed.err, ev.OwnershipLost)
	r, e = f.online.ExpireOnlineWork(ctx, f.s5, work)
	reply(t, r, e, ev.Applied)
	fresh, e := f.online.ClaimOnlineWork(ctx, f.s5, work, "new-owner", time.Second)
	reply(t, fresh, e, ev.Applied)
	if fresh.Token == claim.Token {
		t.Fatal("fresh token")
	}
	w, _, e := f.online.GetOnlineEvaluationState(ctx, f.s5.Scope, work)
	if e != nil {
		t.Fatal(e)
	}
	value, e := (provider.OnlineEvaluator{DBTimeout: time.Second}).EvaluateOnline(ctx, f.s5, w)
	if e != nil {
		t.Fatal(e)
	}
	command, resultID := asset.NewID(), asset.NewID()
	r, e = f.online.FinalizeOnlineResult(ctx, f.s5, work, claim.Token, command, resultID, value)
	reply(t, r, e, ev.OwnershipLost)
	// 不伴随 COMPLETED Work 的独立 Result 必须在 commit Gate 被回滚。
	if _, e = f.db.pool.Exec(ctx, `INSERT INTO evaluation_online_results(id,project_id,work_id,result_bytes,command_id,author_token,author_epoch,intent_digest) VALUES($1,$2,$3,'{}',$4,$5,$6,'invalid-orphan')`, asset.NewID(), f.s.ProjectID, work, asset.NewID(), fresh.Token, f.s5.Epoch); e == nil {
		t.Fatal("orphan result committed")
	}
	r, e = f.online.FinalizeOnlineResult(ctx, f.s5, work, fresh.Token, command, resultID, value)
	reply(t, r, e, ev.Applied)
	r, e = f.online.FinalizeOnlineResult(ctx, f.s5, work, fresh.Token, command, resultID, value)
	reply(t, r, e, ev.AlreadyApplied)
	for _, sql := range []string{`UPDATE evaluation_online_results SET intent_digest='changed' WHERE id=$1`, `DELETE FROM evaluation_online_results WHERE id=$1`} {
		if _, e = f.db.pool.Exec(ctx, sql, resultID); e == nil {
			t.Fatal("result immutable")
		}
	}
	if _, e = f.db.pool.Exec(ctx, `UPDATE evaluation_online_rule_versions SET enabled=false WHERE entity_id=$1`, ref.EntityID); e == nil {
		t.Fatal("rule immutable")
	}
	cross := f.s5
	cross.Scope = other.Scope
	if _, _, e = f.online.GetOnlineEvaluationState(ctx, cross.Scope, work); !errors.Is(e, asset.ErrNotFound) {
		t.Fatal("work project", e)
	}
	b := ob.Backfill{CommandID: asset.NewID(), Rule: ref, From: time.Date(2026, 10, 3, 0, 0, 0, 0, time.UTC), Until: time.Date(2026, 10, 5, 0, 0, 0, 0, time.UTC), MaxRecords: 10}
	r, e = f.online.StartBackfill(ctx, f.s5, b)
	reply(t, r, e, ev.Applied)
	receipt, e := f.online.ContinueBackfill(ctx, f.s5, b.CommandID, 10)
	if e != nil || !receipt.Completed {
		t.Fatal(receipt, e)
	}
	r, e = f.online.StartBackfill(ctx, f.s5, b)
	reply(t, r, e, ev.Applied)
	coverage, e := f.online.GetOnlineCoverage(ctx, f.s5.Scope, ref)
	if e != nil || coverage.Completed != 1 || coverage.WorkCreated != 1 || coverage.Decidable != 1 {
		t.Fatal(coverage, e)
	}
	// v2 是独立 binding，历史 v1 不重解释。
	ref.Version = "v2"
	r, e = f.online.PublishOnlineRuleVersion(ctx, f.s5, ref, rule)
	reply(t, r, e, ev.Applied)
	r, e = f.online.Materialize(ctx, f.s5, ref, id, true)
	reply(t, r, e, ev.Applied)
	coverage, e = f.online.GetOnlineCoverage(ctx, f.s5.Scope, ref)
	if e != nil || coverage.Sampled != 0 || coverage.Eligible != 1 {
		t.Fatal(coverage, e)
	}
	// 两种已封 Execution Truth 后到的 OK trace 都不改变原行。
	for _, kind := range []ev.OutcomeKind{ev.Unknown, ev.Failure} {
		o := start(t, f.kernelFixture, create(t, f.kernelFixture), time.Second)
		out := ev.Outcome{Kind: kind, RequestID: "", Source: "TEST", DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: "CONTROLLED", Reason: "controlled"}
		var q string
		_ = f.k.Pool.QueryRow(ctx, "SELECT execution_request_id FROM evaluation_attempts WHERE id=$1", o.AttemptID).Scan(&q)
		out.RequestID = q
		if kind == ev.Unknown {
			out.TerminalCertainty = "UNCONFIRMED"
			out.DispatchCertainty = "MAY_HAVE_DISPATCHED"
		}
		r, e = f.k.FinalizeExecutionOutcome(ctx, f.s, ev.FinalizeOutcome{Owned: o, CommandID: asset.NewID(), Outcome: out})
		reply(t, r, e, ev.Applied)
		r, e = f.k.CoordinateRun(ctx, f.s, o.RunID)
		reply(t, r, e, ev.Applied)
		var before, after []byte
		_ = f.db.pool.QueryRow(ctx, `SELECT row_to_json(r)::text::bytea FROM evaluation_runs r WHERE id=$1`, o.RunID).Scan(&before)
		late := traceBody(asset.NewID(), asset.NewID(), o.AttemptID, "OK")
		r, e = f.online.IngestStrict(ctx, f.s5, "LOCALAGENT_TRACE_V1", bytes.NewReader(late))
		reply(t, r, e, ev.Applied)
		mustMaterialize(t, f, asset.Ref{EntityID: ref.EntityID, Version: "v1"}, r.ID)
		lateWork := workID(t, f, asset.Ref{EntityID: ref.EntityID, Version: "v1"}, r.ID)
		lateClaim, err := f.online.ClaimOnlineWork(ctx, f.s5, lateWork, "late-online", time.Second)
		reply(t, lateClaim, err, ev.Applied)
		lateState, _, err := f.online.GetOnlineEvaluationState(ctx, f.s5.Scope, lateWork)
		if err != nil {
			t.Fatal(err)
		}
		lateValue, err := (provider.OnlineEvaluator{DBTimeout: time.Second}).EvaluateOnline(ctx, f.s5, lateState)
		if err != nil {
			t.Fatal(err)
		}
		lateReply, err := f.online.FinalizeOnlineResult(ctx, f.s5, lateWork, lateClaim.Token, asset.NewID(), asset.NewID(), lateValue)
		reply(t, lateReply, err, ev.Applied)
		_ = f.db.pool.QueryRow(ctx, `SELECT row_to_json(r)::text::bytea FROM evaluation_runs r WHERE id=$1`, o.RunID).Scan(&after)
		if !bytes.Equal(before, after) {
			t.Fatal("late trace mutated Run")
		}
		state, e := f.k.ReadRunState(ctx, f.s.Scope, o.RunID)
		if e != nil || state.Attempts[0].Outcome != kind {
			t.Fatal("late trace changed execution", e)
		}
	}
	// child 先到；parent 后到的匹配及冲突均可查询，原 receipt 不改写。
	for _, conflict := range []bool{false, true} {
		trace, span, parent, run := asset.NewID(), asset.NewID(), asset.NewID(), asset.NewID()
		var payload map[string]any
		_ = json.Unmarshal(traceBody(trace, span, run, "OK"), &payload)
		payload["parent_span_id"] = parent
		child, _ := json.Marshal(payload)
		childReceipt, err := f.online.IngestStrict(ctx, f.s5, "LOCALAGENT_TRACE_V1", bytes.NewReader(child))
		reply(t, childReceipt, err, ev.Applied)
		before, err := f.online.GetObservation(ctx, f.s5.Scope, childReceipt.ID)
		if err != nil || before.ParentIntegrity != "PENDING" {
			t.Fatal(before, err)
		}
		parentTrace, parentRun := trace, run
		if conflict {
			parentTrace, parentRun = asset.NewID(), asset.NewID()
		}
		parentReceipt, err := f.online.IngestStrict(ctx, f.s5, "LOCALAGENT_TRACE_V1", bytes.NewReader(traceBody(parentTrace, parent, parentRun, "OK")))
		reply(t, parentReceipt, err, ev.Applied)
		after, err := f.online.GetObservation(ctx, f.s5.Scope, childReceipt.ID)
		want := "MATCHED"
		if conflict {
			want = "PARENT_IDENTITY_CONFLICT"
		}
		if err != nil || after.ParentIntegrity != want || before.Ref.Digest != after.Ref.Digest || !bytes.Equal(before.Canonical, after.Canonical) {
			t.Fatal(after, err)
		}
	}
}

func TestG5ControlledOnlineAndBudget(t *testing.T) {
	f := g5(t)
	ctx := context.Background()
	var judges atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		judges.Add(1)
		var active int
		_ = f.db.pool.QueryRow(ctx, "SELECT sum(call_count) FROM evaluation_online_works").Scan(&active)
		if active < 1 {
			t.Error("call must commit before HTTP")
		}
		w.Header().Set("X-Provider", "controlled")
		w.Header().Set("X-Model-Revision", "controlled-v1")
		var req struct {
			Messages []struct {
				Content string `json:"content"`
			} `json:"messages"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		var data struct {
			Untrusted struct{ EvidenceRefs []string } `json:"untrusted_candidate_data"`
		}
		_ = json.Unmarshal([]byte(req.Messages[1].Content), &data)
		output := map[string]any{"schema_version": agentquality.JudgeSchema, "verdict": "FAIL", "score": 0.0, "reason": "controlled fail", "evidence_refs": data.Untrusted.EvidenceRefs, "confidence": 0.9, "task_success": nil}
		encoded, _ := json.Marshal(output)
		_ = json.NewEncoder(w).Encode(map[string]any{"model": "controlled", "id": "response-1", "choices": []any{map[string]any{"finish_reason": "stop", "message": map[string]any{"content": string(encoded)}}}, "usage": map[string]int{"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})
	}))
	defer server.Close()
	ref, _ := f.rule(t, nil)
	id, raw := f.ingest(t, "OK")
	excluded, _ := f.rule(t, func(r *ob.Rule) { r.Sampling.Policy = "HASH_PERCENTAGE"; r.Sampling.BasisPoints = 0 })
	excludedID, _ := f.ingest(t, "OK")
	// G4 Judge 定义用于普通已存正文；strict receipt 未导出正文的 groundedness 仍缺证据。
	g4Assets(t, f.kernelFixture, "answer_correctness", server.URL, provider.LocalAgentConfig{})
	input := f.cmd.Snapshot.Input()
	judgeBinding := ob.Binding{Evaluator: input.Evaluators[0], Metric: input.Metrics[0]}
	judgeBinding.Evaluator.Applicability = asset.Applicability{RuleRef: "online-evidence.v1"}
	// 发布 OBSERVATION 适用版本，不修改已有 G4 definition。
	md := judgeBinding.Metric.Definition
	md.Applicability = asset.Applicability{RuleRef: "online-evidence.v1", RequiredEvidence: []asset.EvidenceRequirement{{Kind: "actual_output", Schema: "artifact.v1", BodyRequired: true}}}
	mref := judgeBinding.Metric.Identity.Ref
	mref.Version = "online"
	mv, e := (postgres.MetricDefinitions{Pool: f.k.Pool}).PublishMetricDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.Definition]{Ref: mref, Body: md, Source: asset.Source{Kind: "TEST", Ref: "G5", Principal: f.s.Principal}})
	if e != nil {
		t.Fatal(e)
	}
	d := judgeBinding.Evaluator.Definition
	d.OutputMetrics = []asset.Ref{mref}
	d.Applicability = md.Applicability
	eref := judgeBinding.Evaluator.Identity.Ref
	eref.Version = "online"
	dv, e := (postgres.EvaluatorDefinitions{Pool: f.k.Pool}).PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: eref, Body: d, Source: asset.Source{Kind: "TEST", Ref: "G5", Principal: f.s.Principal}})
	if e != nil {
		t.Fatal(e)
	}
	judgeBinding.Evaluator.Identity.Ref = eref
	judgeBinding.Evaluator.Identity.ContentDigest = dv.ContentDigest()
	judgeBinding.Evaluator.Definition = d
	judgeBinding.Evaluator.Metrics = []asset.Ref{mref}
	judgeBinding.Metric.Identity.Ref = mref
	judgeBinding.Metric.Identity.ContentDigest = mv.ContentDigest()
	judgeBinding.Metric.Definition = md
	judgeRule, _ := f.rule(t, func(r *ob.Rule) {
		r.Trust = "NORMAL_OBSERVATION"
		r.Source = "CONTROLLED_OBSERVATION"
		r.Bindings = []ob.Binding{judgeBinding}
	})
	g4Assets(t, f.kernelFixture, "groundedness", server.URL, provider.LocalAgentConfig{})
	grounded := f.cmd.Snapshot.Input()
	groundBinding := ob.Binding{Evaluator: grounded.Evaluators[0], Metric: grounded.Metrics[0]}
	missingRule, _ := f.rule(t, func(r *ob.Rule) { r.Bindings = []ob.Binding{groundBinding} })
	f.ingest(t, "OK")
	normal := ob.Observation{Ref: ob.Ref{ProjectID: f.s.ProjectID, ID: asset.NewID(), Kind: ob.TraceKind, Schema: "controlled-observation.v1"}, TraceID: asset.NewID(), RuntimeID: "production", Source: "CONTROLLED_OBSERVATION", Trust: "NORMAL_OBSERVATION", Operation: "answer", Status: "OK", Completed: time.Now(), BodyAvailability: "AVAILABLE", Retention: "EXPLICIT_EVALUATION_RETENTION"}
	normal.Envelope, _ = asset.Freeze(map[string]string{"operation": "answer"})
	body, _ := asset.Freeze("answer")
	normal.Evidence = []ob.Evidence{{Kind: "actual_output", Schema: "artifact.v1", Availability: "AVAILABLE", Digest: body.Digest(), Body: body, Source: "controlled-output", Retention: "EXPLICIT_EVALUATION_RETENTION"}}
	r, e := f.online.StoreObservation(ctx, f.s5, normal)
	reply(t, r, e, ev.Applied)
	// 数据库中的 rule 级滚动窗口与 token reservation 不依赖进程内计数器。
	policyRule, _ := f.rule(t, func(r *ob.Rule) {
		r.Source = "POLICY_PROBE"
		r.Trust = "NORMAL_OBSERVATION"
		r.Bindings = []ob.Binding{judgeBinding}
		r.Rate = ob.Rate{Mode: "DURABLE_INTERVAL", MaxCalls: 1, IntervalSeconds: 3600}
	})
	var policyWorks []ob.Work
	for i := 0; i < 3; i++ {
		x := normal
		x.Ref.ID = asset.NewID()
		x.TraceID = asset.NewID()
		x.Source = "POLICY_PROBE"
		r, e = f.online.StoreObservation(ctx, f.s5, x)
		reply(t, r, e, ev.Applied)
		mustMaterialize(t, f, policyRule, x.Ref.ID)
		wi := workID(t, f, policyRule, x.Ref.ID)
		claim, err := f.online.ClaimOnlineWork(ctx, f.s5, wi, "policy-owner", 5*time.Second)
		reply(t, claim, err, ev.Applied)
		w, _, err := f.online.GetOnlineEvaluationState(ctx, f.s5.Scope, wi)
		if err != nil {
			t.Fatal(err)
		}
		policyWorks = append(policyWorks, w)
	}
	for i, w := range policyWorks {
		call := ob.Call{Provenance: ev.ProviderCall{ID: asset.NewID(), Number: 1, EvaluationNumber: w.Claims, RequestedProvider: judgeBinding.Evaluator.Definition.Model.Provider, RequestedModel: judgeBinding.Evaluator.Definition.Model.Model, PromptRef: judgeBinding.Evaluator.Definition.PromptRef, PromptDigest: strings.Repeat("a", 64), Schema: judgeBinding.Evaluator.Definition.SchemaVersion, InputDigest: w.Observation.Ref.Digest, EvidenceDigest: w.Observation.Ref.Digest}, EstimatedTokens: 10}
		want := ev.Applied
		reason := ""
		if i == 1 {
			want = ev.Rejected
			reason = "SKIPPED_RATE_LIMIT"
		}
		if i == 2 {
			call.EstimatedTokens = 1000001
			want = ev.Rejected
			reason = "SKIPPED_BUDGET"
		}
		r, e = f.online.BeginOnlineCall(ctx, f.s5, w.ID, w.Token, call)
		reply(t, r, e, want)
		if reason != "" && r.Reason != reason {
			t.Fatal(r)
		}
		value := ob.Value{Verdict: "ERROR", SourceVerdict: "ERROR", Reason: "受控 policy admission 测试", Category: reason, Applicability: asset.Applicable}
		if i == 0 {
			r, e = f.online.FinishOnlineCall(ctx, f.s5, w.ID, w.Token, ob.FinishCall{CallID: call.Provenance.ID, Provenance: ev.FinishCall{Classification: "ERROR", UsageAvailability: "UNKNOWN", CostAvailability: "UNKNOWN"}, Draft: &value})
			reply(t, r, e, ev.Applied)
		}
		r, e = f.online.FinalizeOnlineResult(ctx, f.s5, w.ID, w.Token, asset.NewID(), asset.NewID(), value)
		reply(t, r, e, ev.Applied)
	}
	policyCoverage, err := f.online.GetOnlineCoverage(ctx, f.s5.Scope, policyRule)
	if err != nil || policyCoverage.RateLimited != 1 || policyCoverage.BudgetSkipped != 1 || policyCoverage.EvaluatorError != 1 || policyCoverage.Decidable != 0 {
		t.Fatal(policyCoverage, err)
	}
	cfg := worker.DefaultConfig()
	cfg.ExecutionConcurrency = 0
	cfg.EvaluatorConcurrency = 0
	cfg.OnlineConcurrency = 2
	cfg.PollInterval = 10 * time.Millisecond
	cfg.MaxBackoff = 40 * time.Millisecond
	cfg.LeaseDuration = time.Second
	cfg.RenewInterval = 100 * time.Millisecond
	cfg.DBTimeout = 200 * time.Millisecond
	runtime, closeDB, e := bootstrap.Worker(ctx, runtimeURL(t, f.g3Fixture), cfg, true, slog.New(slog.NewTextHandler(&processLog{}, nil)))
	if e != nil {
		t.Fatal(e)
	}
	runCtx, cancel := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- runtime.Run(runCtx) }()
	defer func() { cancel(); <-done; closeDB() }()
	await(t, func() bool {
		c, e := f.online.GetOnlineCoverage(ctx, f.s5.Scope, judgeRule)
		return e == nil && c.Completed == 1
	})
	await(t, func() bool {
		c, e := f.online.GetOnlineCoverage(ctx, f.s5.Scope, ref)
		return e == nil && c.Completed >= 1
	})
	c, e := f.online.GetOnlineCoverage(ctx, f.s5.Scope, excluded)
	if e != nil || c.Sampled != 0 || c.WorkCreated != 0 {
		t.Fatal(c, e)
	}
	_ = excludedID
	await(t, func() bool {
		c, e := f.online.GetOnlineCoverage(ctx, f.s5.Scope, missingRule)
		return e == nil && c.MissingEvidence >= 1
	})
	if judges.Load() != 1 {
		t.Fatalf("expected exactly one provider call got %d", judges.Load())
	}
	r, e = f.online.IngestStrict(ctx, f.s5, "LOCALAGENT_TRACE_V1", bytes.NewReader(raw))
	reply(t, r, e, ev.AlreadyApplied)
	r, e = f.online.Materialize(ctx, f.s5, ref, id, false)
	reply(t, r, e, ev.AlreadyApplied)
	if judges.Load() != 1 {
		t.Fatal("duplicate charge")
	}
	work := workID(t, f, judgeRule, normal.Ref.ID)
	_, result, e := f.online.GetOnlineEvaluationState(ctx, f.s5.Scope, work)
	if e != nil || result == nil || result.Value.Verdict != "FAIL" || len(result.Calls) != 1 || len(result.Value.SelectedCallIDs) != 1 || !result.Sampling.Sampled || result.Observation.Digest != result.EvidenceDigest {
		t.Fatalf("online Judge result %+v %v", result, e)
	}
	candidates, e := f.online.ListFailureCandidates(ctx, f.s5.Scope, ob.Cursor{}, 100)
	if e != nil || len(candidates) < 1 {
		t.Fatal("failure source", e)
	}
	budgetRule, _ := f.rule(t, func(r *ob.Rule) { r.Budget.MaxSampled = 0 })
	budgetID, _ := f.ingest(t, "ERROR")
	mustMaterialize(t, f, budgetRule, budgetID)
	c, e = f.online.GetOnlineCoverage(ctx, f.s5.Scope, budgetRule)
	if e != nil || c.BudgetSkipped != 1 || c.WorkCreated != 0 {
		t.Fatal(c, e)
	}
}

func TestG5OnlineWorkerProcesses(t *testing.T) {
	// 只有独立测试子进程使用此入口：commit 后等待父进程 kill，不启动 scanner。
	if os.Getenv("G5_INGEST_CHILD") == "1" {
		var scope ob.Scope
		if e := json.Unmarshal([]byte(os.Getenv("G5_INGEST_SCOPE")), &scope); e != nil {
			t.Fatal(e)
		}
		pool, e := pgxpool.New(context.Background(), os.Getenv("DATABASE_URL"))
		if e != nil {
			t.Fatal(e)
		}
		defer pool.Close()
		r, e := (postgres.Online{Pool: pool}).IngestStrict(context.Background(), scope, "LOCALAGENT_TRACE_V1", strings.NewReader(os.Getenv("G5_INGEST_BODY")))
		if e != nil || r.Code != ev.Applied {
			t.Fatal(r, e)
		}
		fmt.Println("G5_TRACE_COMMITTED")
		for {
			time.Sleep(time.Second)
		}
	}
	f := g5(t)
	binary := buildWorker(t)
	ctx := context.Background()
	ref, _ := f.rule(t, nil)
	// O03：接收事务 commit 后单独的 producer process 被 kill；数据库仍能补 work。
	raw := traceBody(asset.NewID(), asset.NewID(), asset.NewID(), "OK")
	executable, e := os.Executable()
	if e != nil {
		t.Fatal(e)
	}
	producer := exec.Command(executable, "-test.run=^TestG5OnlineWorkerProcesses$")
	scopeBytes, _ := json.Marshal(f.s5)
	producer.Env = append(os.Environ(), "G5_INGEST_CHILD=1", "G5_INGEST_SCOPE="+string(scopeBytes), "G5_INGEST_BODY="+string(raw), "DATABASE_URL="+runtimeURL(t, f.g3Fixture))
	producerLog := &processLog{}
	producer.Stdout = producerLog
	producer.Stderr = producerLog
	if e = producer.Start(); e != nil {
		t.Fatal(e)
	}
	producerDone := make(chan struct{})
	go func() { _ = producer.Wait(); close(producerDone) }()
	t.Cleanup(func() {
		select {
		case <-producerDone:
		default:
			_ = producer.Process.Kill()
			<-producerDone
		}
	})
	await(t, func() bool { return strings.Contains(producerLog.text(), "G5_TRACE_COMMITTED") })
	var id string
	if e = f.k.Pool.QueryRow(ctx, `SELECT id::text FROM evaluation_observations`).Scan(&id); e != nil {
		t.Fatal(e)
	}
	var preWork int
	_ = f.k.Pool.QueryRow(ctx, `SELECT count(*) FROM evaluation_online_works`).Scan(&preWork)
	if preWork != 0 {
		t.Fatal("materialized before process crash")
	}
	_ = producer.Process.Kill()
	<-producerDone
	p1 := spawnWorker(t, binary, f.g3Fixture, "g5-online-1", "--execution-concurrency", "0", "--evaluator-concurrency", "0", "--online-concurrency", "1")
	p2 := spawnWorker(t, binary, f.g3Fixture, "g5-online-2", "--execution-concurrency", "0", "--evaluator-concurrency", "0", "--online-concurrency", "1")
	await(t, func() bool {
		c, e := f.online.GetOnlineCoverage(ctx, f.s5.Scope, ref)
		return e == nil && c.Completed == 1
	})
	work := workID(t, f, ref, id)
	w, _, e := f.online.GetOnlineEvaluationState(ctx, f.s5.Scope, work)
	if e != nil || w.Claims != 1 {
		t.Fatal("O01 one owner", w.Claims, e)
	}
	_ = p1.cmd.Process.Kill()
	<-p1.done
	_ = p2.cmd.Process.Kill()
	<-p2.done
	// O02：真实进程已 claim 后被 kill，fresh owner 重领；用阻塞 Judge 在网络调用边界停住。
	var calls atomic.Int32
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		select {
		case entered <- struct{}{}:
		default:
		}
		select {
		case <-r.Context().Done():
		case <-release:
		}
	}))
	defer func() { close(release); server.Close() }()
	// 使用已有 G4 source producer 的定义重新发布受控 online input。
	g4Assets(t, f.kernelFixture, "answer_correctness", server.URL, provider.LocalAgentConfig{})
	snapshot := f.cmd.Snapshot.Input()
	b := ob.Binding{Evaluator: snapshot.Evaluators[0], Metric: snapshot.Metrics[0]}
	b.Evaluator.Applicability = asset.Applicability{RuleRef: "online.v1"}
	md := b.Metric.Definition
	md.Applicability = asset.Applicability{RuleRef: "online.v1"}
	mr := b.Metric.Identity.Ref
	mr.Version = "g5-kill"
	mv, e := (postgres.MetricDefinitions{Pool: f.k.Pool}).PublishMetricDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.Definition]{Ref: mr, Body: md, Source: asset.Source{Kind: "TEST", Ref: "g5-kill", Principal: f.s.Principal}})
	if e != nil {
		t.Fatal(e)
	}
	d := b.Evaluator.Definition
	d.OutputMetrics = []asset.Ref{mr}
	d.Applicability = md.Applicability
	var cfg provider.JudgeConfig
	_ = d.Config.Decode(&cfg)
	cfg.CallMilliseconds = 10000
	cfg.Retries = 0
	d.Budget.TotalMilliseconds = 20000
	d.Budget.MaxProviderCalls = 1
	d.Retry.MaxEvaluationAttempts = 2
	d.Config, _ = asset.Freeze(cfg)
	er := b.Evaluator.Identity.Ref
	er.Version = "g5-kill"
	dv, e := (postgres.EvaluatorDefinitions{Pool: f.k.Pool}).PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: er, Body: d, Source: asset.Source{Kind: "TEST", Ref: "g5-kill", Principal: f.s.Principal}})
	if e != nil {
		t.Fatal(e)
	}
	b.Metric.Identity.Ref = mr
	b.Metric.Identity.ContentDigest = mv.ContentDigest()
	b.Metric.Definition = md
	b.Evaluator.Identity.Ref = er
	b.Evaluator.Identity.ContentDigest = dv.ContentDigest()
	b.Evaluator.Definition = d
	b.Evaluator.Metrics = []asset.Ref{mr}
	killRule, _ := f.rule(t, func(r *ob.Rule) {
		r.Source = "CONTROLLED"
		r.Trust = "NORMAL_OBSERVATION"
		r.Bindings = []ob.Binding{b}
	})
	normal := ob.Observation{Ref: ob.Ref{ProjectID: f.s.ProjectID, ID: asset.NewID(), Kind: ob.TraceKind, Schema: "test.v1"}, Source: "CONTROLLED", Trust: "NORMAL_OBSERVATION", TraceID: asset.NewID(), Status: "OK", Completed: time.Now(), Retention: "EXPLICIT_EVALUATION_RETENTION"}
	normal.Envelope, _ = asset.Freeze("test")
	body, _ := asset.Freeze("answer")
	normal.Evidence = []ob.Evidence{{Kind: "actual_output", Schema: "artifact.v1", Availability: "AVAILABLE", Digest: body.Digest(), Body: body, Source: "output", Retention: "EXPLICIT_EVALUATION_RETENTION"}}
	r, e := f.online.StoreObservation(ctx, f.s5, normal)
	reply(t, r, e, ev.Applied)
	victim := spawnWorker(t, binary, f.g3Fixture, "g5-victim", "--execution-concurrency", "0", "--evaluator-concurrency", "0", "--online-concurrency", "1")
	select {
	case <-entered:
	case <-time.After(10 * time.Second):
		t.Fatal("online provider not entered", victim.log.text())
	}
	killWork := workID(t, f, killRule, normal.Ref.ID)
	old, _, e := f.online.GetOnlineEvaluationState(ctx, f.s5.Scope, killWork)
	if e != nil {
		t.Fatal(e)
	}
	_ = victim.cmd.Process.Kill()
	<-victim.done
	recovery := spawnWorker(t, binary, f.g3Fixture, "g5-recovery", "--execution-concurrency", "0", "--evaluator-concurrency", "0", "--online-concurrency", "1")
	await(t, func() bool {
		w, r, e := f.online.GetOnlineEvaluationState(ctx, f.s5.Scope, killWork)
		return e == nil && r != nil && w.Claims == 2
	})
	w, result, e := f.online.GetOnlineEvaluationState(ctx, f.s5.Scope, killWork)
	if e != nil || w.Token == old.Token || calls.Load() != 1 || result.Calls[0].Provenance.Classification != "RESPONSE_UNKNOWN" {
		t.Fatal("bounded reclaim", w, result, e)
	}
	r, e = f.online.RenewOnlineLease(ctx, f.s5, killWork, old.Token, time.Second)
	reply(t, r, e, ev.OwnershipLost)
	// O04：result commit 后 kill，coverage 仍从 PG 派生。
	_ = recovery.cmd.Process.Kill()
	<-recovery.done
	c, e := f.online.GetOnlineCoverage(ctx, f.s5.Scope, killRule)
	if e != nil || c.Completed != 1 || c.Decidable != 0 {
		t.Fatal(c, e)
	}
	// O05：稳定 backfill command，独立进程 kill/restart 后无重复 work。
	for i := 0; i < 25; i++ {
		f.ingest(t, "OK")
	}
	backfill := ob.Backfill{CommandID: asset.NewID(), Rule: ref, From: time.Date(2026, 10, 3, 0, 0, 0, 0, time.UTC), Until: time.Date(2026, 10, 5, 0, 0, 0, 0, time.UTC), MaxRecords: 40}
	r, e = f.online.StartBackfill(ctx, f.s5, backfill)
	reply(t, r, e, ev.Applied)
	bf := spawnWorker(t, binary, f.g3Fixture, "g5-backfill-1", "--execution-concurrency", "0", "--evaluator-concurrency", "0", "--online-concurrency", "1")
	await(t, func() bool {
		var n int
		_ = f.k.Pool.QueryRow(ctx, "SELECT processed FROM evaluation_online_backfills WHERE id=$1", backfill.CommandID).Scan(&n)
		return n > 0
	})
	_ = bf.cmd.Process.Kill()
	<-bf.done
	var beforeRestart bool
	if e = f.k.Pool.QueryRow(ctx, `SELECT completed FROM evaluation_online_backfills WHERE id=$1`, backfill.CommandID).Scan(&beforeRestart); e != nil || beforeRestart {
		t.Fatal("backfill must be incomplete at crash", e)
	}
	_ = spawnWorker(t, binary, f.g3Fixture, "g5-backfill-2", "--execution-concurrency", "0", "--evaluator-concurrency", "0", "--online-concurrency", "1")
	await(t, func() bool {
		var complete bool
		_ = f.k.Pool.QueryRow(ctx, "SELECT completed FROM evaluation_online_backfills WHERE id=$1", backfill.CommandID).Scan(&complete)
		return complete
	})
	var duplicates int
	_ = f.k.Pool.QueryRow(ctx, `SELECT count(*) FROM (SELECT observation_id,rule_id,rule_version,evaluator_id,evaluator_version,count(*) n FROM evaluation_online_works GROUP BY 1,2,3,4,5 HAVING count(*)>1) x`).Scan(&duplicates)
	if duplicates != 0 {
		t.Fatal("backfill duplicates")
	}
	for _, p := range []*workerProcess{p1, p2, victim, recovery, bf} {
		if strings.Contains(p.log.text(), "DATA RACE") {
			t.Fatal("race")
		}
	}
	t.Log(fmt.Sprintf("O01-O05 independent worker processes PASS; provider calls=%d", calls.Load()))
}
