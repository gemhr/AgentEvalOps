//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/citriage"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/postgres"
	"context"
	"github.com/jackc/pgx/v5"
	"testing"
	"time"
)

func TestStage13ClaimBeforeExecutionCrash(t *testing.T) {
	f := fixture(t)
	ctx := context.Background()
	f.cmd.Target.TimeoutMilliseconds = 180000
	f.cmd.Target.Config, _ = asset.Freeze(map[string]any{"execution_policy_version": citriage.ExecutionPolicyVersion, "expected_subject_manifest": map[string]any{"agent_id": "unit"}})
	o := create(t, f)
	// claim 前持久态保持 PENDING，没有任何 execution policy；重建 owner 不产生 start。
	state, err := f.k.ReadRunState(ctx, f.s.Scope, o.RunID)
	if err != nil || state.Attempts[0].Status != "PENDING" {
		t.Fatal("pending truth changed", err)
	}
	r, err := f.k.ClaimExecutionAttempt(ctx, f.s, o, "crashed-before-start", time.Minute)
	o.Token = reply(t, r, err, ev.Applied).Token
	state, err = f.k.ReadRunState(ctx, f.s.Scope, o.RunID)
	if err != nil {
		t.Fatal(err)
	}
	restarted := postgres.Evaluation{Pool: f.k.Pool, Capabilities: f.k.Capabilities}
	if _, err = restarted.ReserveStage13Execution(ctx, f.s, state.Attempts[0].Request); err == nil {
		t.Fatal("CLAIMED but not started acquired execution budget")
	}
	var n int
	if err = f.k.Pool.QueryRow(ctx, `SELECT count(*) FROM evaluation_stage13_execution_requests WHERE attempt_id=$1`, o.AttemptID).Scan(&n); err != nil || n != 0 {
		t.Fatal("pre-execution crash produced envelope", err)
	}
	forceExpiry(t, f, "evaluation_attempts", o.AttemptID)
	r, err = restarted.ExpireExecutionAttempt(ctx, f.s, o)
	reply(t, r, err, ev.Applied)
	state, err = restarted.ReadRunState(ctx, f.s.Scope, o.RunID)
	if err != nil || state.Attempts[0].Outcome != ev.Unknown {
		t.Fatal("pre-execution crash disguised as model failure", err)
	}
}

func TestStage13DurableExecutionBudget(t *testing.T) {
	f := fixture(t)
	ctx := context.Background()
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: f.k.Pool}, Datasets: postgres.Datasets{Pool: f.k.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: f.k.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: f.k.Pool}}
	in := f.cmd.Snapshot.Input()
	caseRef := in.Manifest[0].Identity.Ref
	caseRef.Version = "execution-policy-test"
	body := testCase()
	body.Input, _ = asset.Freeze(map[string]any{"schema_version": "stage13.triage-input.v1", "incident_ref": map[string]any{"incident_id": "test", "evidence_revision": 1, "manifest_digest": "test"}, "visible_evidence": []any{}, "evidence_policy": map[string]any{"deadline_at": "2020-01-01T00:00:00Z", "max_tool_reads": 8}})
	source := asset.Source{Kind: "TEST", Ref: "wp05a-deadline", Principal: f.s.Principal}
	_, err := (catalog.CaseService{Store: readers.Cases}).PublishCaseVersion(ctx, f.s.Scope, asset.Publish[catalog.CaseContent]{Ref: caseRef, Body: body, Source: source})
	if err != nil {
		t.Fatal(err)
	}
	dataset := in.Dataset.Ref
	dataset.Version = "execution-policy-test"
	_, err = (catalog.DatasetService{Store: readers.Datasets, Cases: readers}).PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: dataset, Body: catalog.DatasetContent{Cases: []asset.Ref{caseRef}}, Source: source})
	if err != nil {
		t.Fatal(err)
	}
	bindings := []catalog.EvaluatorBinding{}
	for _, e := range in.Evaluators {
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: e.Identity.Ref, Metrics: e.Metrics, Required: e.Required, Applicability: e.Applicability})
	}
	f.cmd.Snapshot, err = (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &dataset, Evaluators: bindings})
	if err != nil {
		t.Fatal(err)
	}
	f.cmd.Target.TimeoutMilliseconds = 180000
	f.cmd.Target.Config, _ = asset.Freeze(map[string]any{"execution_policy_version": citriage.ExecutionPolicyVersion, "expected_subject_manifest": map[string]any{"agent_id": "unit"}})
	var role string
	if err = f.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&role); err != nil {
		t.Fatal(err)
	}
	_, err = f.db.pool.Exec(ctx, "GRANT INSERT ON evaluation_stage13_execution_requests TO "+pgx.Identifier{role}.Sanitize())
	if err != nil {
		t.Fatal(err)
	}
	o := create(t, f)
	var n int
	if err = f.k.Pool.QueryRow(ctx, `SELECT count(*) FROM evaluation_stage13_execution_requests WHERE attempt_id=$1`, o.AttemptID).Scan(&n); err != nil || n != 0 {
		t.Fatal("queued Attempt acquired deadline", err)
	}
	t.Log("queue waiting 120 real seconds; no execution envelope exists")
	time.Sleep(120 * time.Second)
	o = start(t, f, o, 3*time.Minute)
	state, err := f.k.ReadRunState(ctx, f.s.Scope, o.RunID)
	if err != nil {
		t.Fatal(err)
	}
	request := state.Attempts[0].Request
	first, err := f.k.ReserveStage13Execution(ctx, f.s, request)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]asset.JSON
	_ = first.Decode(&fields)
	var p citriage.ExecutionPolicy
	_ = fields["execution_policy"].Decode(&p)
	if left := time.Until(p.DeadlineAt); left < 179*time.Second || left > 180*time.Second {
		t.Fatal("queue consumed execution budget", left)
	}
	t.Log("execution reserved; wait 60 real seconds then restart storage reader")
	time.Sleep(60 * time.Second)
	restarted := postgres.Evaluation{Pool: f.k.Pool, Capabilities: f.k.Capabilities}
	replayed, err := restarted.ReserveStage13Execution(ctx, f.s, request)
	if err != nil || replayed.String() != first.String() || time.Until(p.DeadlineAt) > 120*time.Second || time.Until(p.DeadlineAt) < 118*time.Second {
		t.Fatal("restart refreshed budget", err)
	}
	_, err = f.db.pool.Exec(ctx, `UPDATE evaluation_stage13_execution_requests SET request_digest=repeat('0',64) WHERE attempt_id=$1`, o.AttemptID)
	if err == nil {
		t.Fatal("immutable envelope mutated")
	}
	// generic Kernel 的 crash recovery 保守关闭；同 Attempt 不重新 claim/dispatch。
	forceExpiry(t, f, "evaluation_attempts", o.AttemptID)
	r, err := f.k.ExpireExecutionAttempt(ctx, f.s, o)
	reply(t, r, err, ev.Applied)
	r, err = f.k.ClaimExecutionAttempt(ctx, f.s, o, "restart", time.Minute)
	reply(t, r, err, ev.NotClaimed)
	state, err = f.k.ReadRunState(ctx, f.s.Scope, o.RunID)
	if err != nil || state.Attempts[0].Outcome != ev.Unknown {
		t.Fatal("crash truth changed", err)
	}
}
