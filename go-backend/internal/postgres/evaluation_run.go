package postgres

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"encoding/json"
	"errors"
	"github.com/jackc/pgx/v5"
	"time"
)

func attemptsAndWorks(ctx context.Context, tx pgx.Tx, s ev.Scope, run string) ([]ev.Attempt, []ev.Work, error) {
	var attempts []ev.Attempt
	var works []ev.Work
	rows, e := tx.Query(ctx, "SELECT to_jsonb(a) FROM evaluation_attempts a WHERE project_id=$1 AND run_id=$2 ORDER BY case_id,attempt_no", s.ProjectID, run)
	if e != nil {
		return nil, nil, e
	}
	for rows.Next() {
		var b []byte
		var a ev.Attempt
		if e = rows.Scan(&b); e == nil {
			e = json.Unmarshal(b, &a)
		}
		if e != nil {
			rows.Close()
			return nil, nil, e
		}
		attempts = append(attempts, a)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, nil, e
	}
	rows, e = tx.Query(ctx, "SELECT to_jsonb(w) FROM evaluation_evaluator_works w WHERE project_id=$1 AND run_id=$2", s.ProjectID, run)
	if e != nil {
		return nil, nil, e
	}
	defer rows.Close()
	for rows.Next() {
		var b []byte
		var w ev.Work
		if e = rows.Scan(&b); e == nil {
			e = json.Unmarshal(b, &w)
		}
		if e != nil {
			return nil, nil, e
		}
		works = append(works, w)
	}
	return attempts, works, rows.Err()
}
func retryInTransaction(ctx context.Context, tx pgx.Tx, s ev.Scope, r ev.Run, c ev.Retry, automatic bool) (ev.Reply, error) {
	digest, e := ev.Intent(struct {
		Retry ev.Retry
		Actor string
	}{c, s.Principal})
	if e != nil {
		return ev.Reply{}, e
	}
	var existing ev.Attempt
	e = loadJSON(ctx, tx, "SELECT to_jsonb(a) FROM evaluation_attempts a WHERE project_id=$1 AND run_id=$2 AND retry_of_attempt_id=$3", &existing, s.ProjectID, r.ID, c.AttemptID)
	if e == nil {
		if existing.RetryIntent == nil || *existing.RetryIntent != digest {
			return ev.Reply{}, stop(ev.Conflict, "RETRY_INTENT_CONFLICT")
		}
		return ev.Reply{Code: ev.AlreadyApplied, ID: existing.ID}, nil
	}
	var cs commandStop
	if !errors.As(e, &cs) || cs.reply.Code != ev.NotFound {
		return ev.Reply{}, e
	}
	if e = activeRun(r); e != nil {
		return ev.Reply{}, e
	}
	a, e := getAttempt(ctx, tx, s, ev.Owned{RunID: r.ID, AttemptID: c.AttemptID})
	if e != nil {
		return ev.Reply{}, e
	}
	var max int
	e = tx.QueryRow(ctx, "SELECT max(attempt_no) FROM evaluation_attempts WHERE project_id=$1 AND run_id=$2 AND case_id=$3 AND case_version=$4", s.ProjectID, r.ID, a.CaseID, a.CaseVersion).Scan(&max)
	if e != nil {
		return ev.Reply{}, e
	}
	if max != a.Number {
		return ev.Reply{}, stop(ev.Conflict, "NOT_LATEST_ATTEMPT")
	}
	if !r.Snapshot.Retry.Allows(a, automatic, c.Authorized && s.Operator, c.UnknownAck) {
		return ev.Reply{}, stop(ev.Rejected, "RETRY_POLICY_OR_BUDGET")
	}
	id, e := insertAttempt(ctx, tx, s, r, a.Request.Case, a.Number+1, &a.ID, &digest, &c.CommandID)
	return ev.Reply{Code: ev.Applied, ID: id}, e
}
func (k Evaluation) CreateExecutionRetry(ctx context.Context, s ev.Scope, c ev.Retry) (ev.Reply, error) {
	if !s.Operator || !asset.ValidID(c.CommandID) || !asset.Text(c.Reason) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.transact(ctx, s, c.RunID, true, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		return retryInTransaction(ctx, tx, s, r, c, false)
	})
}
func (k Evaluation) CreateRunRerun(ctx context.Context, s ev.Scope, c ev.Rerun) (ev.Reply, error) {
	if !s.Create || !s.Operator || !asset.ValidID(c.CommandID) || !asset.Text(c.Reason) || !asset.Text(c.Trigger) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.transact(ctx, s, c.SourceRunID, true, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		if !r.Status.Terminal() {
			return ev.Reply{}, stop(ev.Rejected, "SOURCE_NOT_TERMINAL")
		}
		if r.Status == ev.RunUnknown && !c.UnknownAck {
			return ev.Reply{}, stop(ev.Rejected, "UNKNOWN_RERUN_ACK_REQUIRED")
		}
		for _, spec := range r.Snapshot.Input.Evaluators {
			if !k.Capabilities.Supports(spec) {
				return ev.Reply{}, stop(ev.Rejected, "UNSUPPORTED_EVALUATOR_IMPLEMENTATION")
			}
		}
		digest, e := ev.Intent(struct {
			Rerun ev.Rerun
			Actor string
		}{c, s.Principal})
		if e != nil {
			return ev.Reply{}, e
		}
		return insertRun(ctx, tx, s, r.Snapshot, c.CommandID, digest, &r.ID, c.Reason, struct {
			Actor, Trigger string
			UnknownAck     bool
		}{s.Principal, c.Trigger, c.UnknownAck})
	})
}
func (k Evaluation) CoordinateRun(ctx context.Context, s ev.Scope, id string) (ev.Reply, error) {
	if !s.Coordinate {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.transact(ctx, s, id, false, func(tx pgx.Tx, r ev.Run, mode string) (ev.Reply, error) {
		if r.Status.Terminal() {
			return ev.Reply{Code: ev.AlreadyApplied, ID: r.ID, Status: string(r.Status)}, nil
		}
		if e := activeRun(r); e != nil {
			return ev.Reply{}, e
		}
		attempts, works, e := attemptsAndWorks(ctx, tx, s, r.ID)
		if e != nil {
			return ev.Reply{}, e
		}
		latest, e := ev.Latest(attempts)
		if e != nil {
			return ev.Reply{}, stop(ev.IntegrityBlocked, "LATEST_ATTEMPTS")
		}
		for _, c := range r.Snapshot.Input.Manifest {
			a, ok := latest[c.Identity.Ref.EntityID]
			if !ok {
				return ev.Reply{}, stop(ev.IntegrityBlocked, "MISSING_ATTEMPT")
			}
			if r.Snapshot.Retry.Allows(a, true, false, false) {
				if mode != "GO_ACTIVE" {
					return ev.Reply{Code: ev.NotReady, Reason: "DRAIN_REQUIRES_CHILD"}, nil
				}
				reply, e := retryInTransaction(ctx, tx, s, r, ev.Retry{RunID: r.ID, AttemptID: a.ID, CommandID: asset.NewID(), Reason: "frozen automatic retry"}, true)
				reply.Reason = "PROGRESSED"
				return reply, e
			}
		}
		reply := ev.DecideRun(r.Snapshot, attempts, works)
		if reply.Code != ev.Applied {
			return reply, nil
		}
		for _, w := range works {
			if w.Status == "COMPLETED" {
				var valid bool
				e = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM evaluation_results WHERE project_id=$1 AND run_id=$2 AND attempt_id=$3 AND id=$4 AND (work_id=$5 OR ($6='LEGACY' AND work_id IS NULL)))", s.ProjectID, r.ID, w.AttemptID, w.ResultID, w.ID, w.ContractKind).Scan(&valid)
				if e != nil {
					return ev.Reply{}, e
				}
				if !valid {
					return ev.Reply{}, stop(ev.IntegrityBlocked, "RESULT_WORK_MISMATCH")
				}
			}
		}
		_, e = tx.Exec(ctx, "UPDATE evaluation_runs SET status=$3,finished_at=clock_timestamp() WHERE project_id=$1 AND id=$2", s.ProjectID, r.ID, reply.Status)
		reply.ID = r.ID
		return reply, e
	})
}

// Candidates 使用稳定 cursor；扫描不加 child 锁，命令重新验证 DB 时间与 scope。
type EvaluationCursor struct {
	CreatedAt time.Time
	ID        string
}
type EvaluationCandidate struct {
	ProjectID, RunID, AttemptID, WorkID string
	Cursor                              EvaluationCursor
}
type CandidateKind string

const (
	PendingExecution      CandidateKind = "PENDING_EXECUTION"
	ExpiredExecution      CandidateKind = "EXPIRED_EXECUTION"
	PendingEvaluator      CandidateKind = "PENDING_EVALUATOR"
	ExpiredEvaluator      CandidateKind = "EXPIRED_EVALUATOR"
	RunningRun            CandidateKind = "RUNNING_RUN"
	MissingEvaluatorSlots CandidateKind = "MISSING_EVALUATOR_SLOTS"
)

func (k Evaluation) Candidates(ctx context.Context, kind CandidateKind, after EvaluationCursor, limit int) ([]EvaluationCandidate, error) {
	if limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	if after.ID == "" {
		after.ID = "00000000-0000-0000-0000-000000000000"
		after.CreatedAt = time.Unix(0, 0)
	}
	sql := ""
	switch kind {
	case PendingExecution, ExpiredExecution:
		pred := "a.status='PENDING'"
		if kind == ExpiredExecution {
			pred = "a.status IN ('CLAIMED','RUNNING') AND a.lease_expires_at<=clock_timestamp()"
		}
		sql = "SELECT a.project_id::text,a.run_id::text,a.id::text,''::text,a.created_at,a.id::text FROM evaluation_attempts a JOIN evaluation_runs r ON r.project_id=a.project_id AND r.id=a.run_id WHERE r.contract_version IS NOT NULL AND r.status IN ('PENDING','RUNNING') AND " + pred + " AND (a.created_at,a.id)>($1,$2::uuid) ORDER BY a.created_at,a.id LIMIT $3"
	case PendingEvaluator, ExpiredEvaluator:
		pred := "w.status='PENDING' AND w.next_available_at<=clock_timestamp()"
		if kind == ExpiredEvaluator {
			pred = "w.status='CLAIMED' AND w.lease_expires_at<=clock_timestamp()"
		}
		sql = "SELECT w.project_id::text,w.run_id::text,w.attempt_id::text,w.id::text,w.created_at,w.id::text FROM evaluation_evaluator_works w JOIN evaluation_runs r ON r.project_id=w.project_id AND r.id=w.run_id WHERE r.status='RUNNING' AND " + pred + " AND (w.created_at,w.id)>($1,$2::uuid) ORDER BY w.created_at,w.id LIMIT $3"
	case RunningRun:
		sql = "SELECT project_id::text,id::text,''::text,''::text,created_at,id::text FROM evaluation_runs WHERE contract_version IS NOT NULL AND status='RUNNING' AND (created_at,id)>($1,$2::uuid) ORDER BY created_at,id LIMIT $3"
	case MissingEvaluatorSlots:
		sql = `SELECT a.project_id::text,a.run_id::text,a.id::text,''::text,a.created_at,a.id::text FROM evaluation_attempts a JOIN evaluation_runs r ON r.project_id=a.project_id AND r.id=a.run_id WHERE a.execution_outcome_kind='SUCCESS' AND EXISTS(SELECT 1 FROM jsonb_array_elements(r.kernel_snapshot->'Input'->'evaluator_specs') spec WHERE NOT EXISTS(SELECT 1 FROM evaluation_evaluator_works w WHERE w.project_id=a.project_id AND w.run_id=a.run_id AND w.attempt_id=a.id AND w.evaluator_id=spec->'identity'->'ref'->>'entity_id' AND w.evaluator_version=spec->'identity'->'ref'->>'version')) AND (a.created_at,a.id)>($1,$2::uuid) ORDER BY a.created_at,a.id LIMIT $3`
	default:
		return nil, asset.ErrInvalid
	}
	rows, e := k.Pool.Query(ctx, sql, after.CreatedAt, after.ID, limit)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []EvaluationCandidate{}
	for rows.Next() {
		var v EvaluationCandidate
		e = rows.Scan(&v.ProjectID, &v.RunID, &v.AttemptID, &v.WorkID, &v.Cursor.CreatedAt, &v.Cursor.ID)
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
func (k Evaluation) ReconcileEvaluatorSlots(ctx context.Context, s ev.Scope, o ev.Owned) (ev.Reply, error) {
	if !s.Repair {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.transact(ctx, s, o.RunID, false, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		if e := activeRun(r); e != nil {
			return ev.Reply{}, e
		}
		a, e := getAttempt(ctx, tx, s, o)
		if e != nil {
			return ev.Reply{}, e
		}
		if a.Outcome != ev.Success || a.Status != "TERMINAL" {
			return ev.Reply{}, stop(ev.Rejected, "NOT_SUCCESS")
		}
		changed := false
		for _, spec := range r.Snapshot.Input.Evaluators {
			var exists bool
			e = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM evaluation_evaluator_works WHERE project_id=$1 AND run_id=$2 AND attempt_id=$3 AND evaluator_id=$4 AND evaluator_version=$5)", s.ProjectID, r.ID, a.ID, spec.Identity.Ref.EntityID, spec.Identity.Ref.Version).Scan(&exists)
			if e != nil {
				return ev.Reply{}, e
			}
			if exists {
				continue
			}
			var result bool
			e = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM evaluation_results WHERE project_id=$1 AND run_id=$2 AND attempt_id=$3 AND evaluator_id=$4 AND evaluator_version=$5)", s.ProjectID, r.ID, a.ID, spec.Identity.Ref.EntityID, spec.Identity.Ref.Version).Scan(&result)
			if e != nil {
				return ev.Reply{}, e
			}
			if result {
				return ev.Reply{}, stop(ev.IntegrityBlocked, "HISTORICAL_RESULT_REQUIRES_BARRIER_IMPORT")
			}
			partial := r
			partial.Snapshot.Input.Evaluators = []ev.EvaluatorSpec{spec}
			if e = insertSlots(ctx, tx, s, partial, a); e != nil {
				return ev.Reply{}, e
			}
			changed = true
		}
		code := ev.AlreadyApplied
		if changed {
			code = ev.Applied
		}
		return ev.Reply{Code: code}, nil
	})
}
