package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	ob "agentevalops/go-backend/internal/observation"
	"github.com/jackc/pgx/v5"
)

func getOnlineWork(ctx context.Context, q queryer, s asset.Scope, id string, lock bool) (ob.Work, error) {
	var w ob.Work
	var binding, calls []byte
	var token *string
	var epoch *int64
	sql := `SELECT id::text,project_id::text,observation_id::text,rule_id::text,rule_version,binding_bytes,status,claim_token::text,writer_epoch,lease_expires_at,claim_count,call_count,calls_bytes,created_at FROM evaluation_online_works WHERE project_id=$1 AND id=$2`
	if lock {
		sql += " FOR UPDATE"
	}
	e := q.QueryRow(ctx, sql, s.ProjectID, id).Scan(&w.ID, &w.ProjectID, &w.ObservationID, &w.Rule.EntityID, &w.Rule.Version, &binding, &w.Status, &token, &epoch, &w.Lease, &w.Claims, &w.Calls, &calls, &w.Created)
	if e != nil {
		return w, dbError(e)
	}
	if token != nil {
		w.Token = *token
	}
	if epoch != nil {
		w.Epoch = *epoch
	}
	if e = json.Unmarshal(binding, &w.Binding); e != nil {
		return w, e
	}
	if e = json.Unmarshal(calls, &w.CallRecords); e != nil {
		return w, e
	}
	w.Observation, e = readObservation(ctx, q, s, w.ObservationID)
	return w, e
}
func (k Online) GetOnlineEvaluationState(ctx context.Context, s asset.Scope, id string) (ob.Work, *ob.Result, error) {
	if s.Validate(false) != nil || !asset.ValidID(id) {
		return ob.Work{}, nil, asset.ErrInvalid
	}
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if e != nil {
		return ob.Work{}, nil, e
	}
	defer rollback(ctx, tx)
	w, e := getOnlineWork(ctx, tx, s, id, false)
	if e != nil {
		return w, nil, e
	}
	var raw []byte
	var created time.Time
	e = tx.QueryRow(ctx, `SELECT result_bytes,created_at FROM evaluation_online_results WHERE project_id=$1 AND work_id=$2`, s.ProjectID, id).Scan(&raw, &created)
	if errors.Is(e, pgx.ErrNoRows) {
		return w, nil, tx.Commit(ctx)
	}
	if e != nil {
		return w, nil, e
	}
	var r ob.Result
	if e = json.Unmarshal(raw, &r); e != nil {
		return w, nil, e
	}
	r.Created = created
	return w, &r, tx.Commit(ctx)
}
func (k Online) workTransaction(ctx context.Context, s ob.Scope, id string, admission bool, fn func(pgx.Tx, ob.Work, ob.RuleVersion, ProjectOnlinePolicy, usage, usage) (ev.Reply, error)) (ev.Reply, error) {
	if !s.Evaluate || !asset.ValidID(id) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.transaction(ctx, s, admission, func(tx pgx.Tx) (ev.Reply, error) {
		var ref asset.Ref
		e := tx.QueryRow(ctx, `SELECT rule_id::text,rule_version FROM evaluation_online_works WHERE project_id=$1 AND id=$2`, s.ProjectID, id).Scan(&ref.EntityID, &ref.Version)
		if e != nil {
			return ev.Reply{}, dbError(e)
		}
		v, e := readRule(ctx, tx, s.Scope, ref)
		if e != nil {
			return ev.Reply{}, e
		}
		policy, pu, ru, e := limits(ctx, tx, s, ref)
		if e != nil {
			return ev.Reply{}, e
		}
		w, e := getOnlineWork(ctx, tx, s.Scope, id, true)
		if e != nil {
			return ev.Reply{}, e
		}
		return fn(tx, w, v, policy, pu, ru)
	})
}
func onlineOwned(ctx context.Context, tx pgx.Tx, s ob.Scope, w ob.Work, token string) error {
	now, e := dbNow(ctx, tx)
	if e != nil {
		return e
	}
	if w.Status != "CLAIMED" || w.Token != token || w.Epoch != s.Epoch || w.Lease == nil || !w.Lease.After(now) {
		return stop(ev.OwnershipLost, "ONLINE_OWNER_LOST")
	}
	return nil
}
func onlineFenced(ctx context.Context, tx pgx.Tx, s ob.Scope, w ob.Work, sql string, args ...any) error {
	n := len(args)
	sql += fmt.Sprintf(` AND status='CLAIMED' AND claim_token=$%d AND writer_epoch=$%d AND lease_expires_at>clock_timestamp()`, n+1, n+2)
	args = append(args, w.Token, s.Epoch)
	tag, e := tx.Exec(ctx, sql, args...)
	if e != nil {
		return e
	}
	if tag.RowsAffected() != 1 {
		return stop(ev.OwnershipLost, "ONLINE_OWNER_LOST")
	}
	return nil
}
func (k Online) ClaimOnlineWork(ctx context.Context, s ob.Scope, id, owner string, lease time.Duration) (ev.Reply, error) {
	if !asset.Text(owner) || lease < time.Millisecond || lease > 5*time.Minute {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.workTransaction(ctx, s, id, true, func(tx pgx.Tx, w ob.Work, _ ob.RuleVersion, _ ProjectOnlinePolicy, _, _ usage) (ev.Reply, error) {
		if w.Status != "PENDING" {
			return ev.Reply{Code: ev.NotClaimed}, nil
		}
		token := asset.NewID()
		var expires time.Time
		e := tx.QueryRow(ctx, `UPDATE evaluation_online_works SET status='CLAIMED',claim_token=$3,writer_epoch=$4,owner_ref=$5,claim_count=claim_count+1,lease_expires_at=clock_timestamp()+$6*interval '1 millisecond' WHERE project_id=$1 AND id=$2 AND status='PENDING' RETURNING lease_expires_at`, s.ProjectID, id, token, s.Epoch, owner, lease.Milliseconds()).Scan(&expires)
		if e != nil {
			return ev.Reply{}, e
		}
		slog.Info("online_work_claimed", "event", "online_work_claimed", "project_id", s.ProjectID, "work_id", id)
		return ev.Reply{Code: ev.Applied, ID: id, Token: token, Lease: &expires}, nil
	})
}
func (k Online) RenewOnlineLease(ctx context.Context, s ob.Scope, id, token string, lease time.Duration) (ev.Reply, error) {
	if lease < time.Millisecond || lease > 5*time.Minute {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.workTransaction(ctx, s, id, false, func(tx pgx.Tx, w ob.Work, _ ob.RuleVersion, _ ProjectOnlinePolicy, _, _ usage) (ev.Reply, error) {
		if e := onlineOwned(ctx, tx, s, w, token); e != nil {
			return ev.Reply{}, e
		}
		e := onlineFenced(ctx, tx, s, w, `UPDATE evaluation_online_works SET lease_expires_at=clock_timestamp()+$3*interval '1 millisecond' WHERE project_id=$1 AND id=$2`, s.ProjectID, id, lease.Milliseconds())
		return ev.Reply{Code: ev.Applied}, e
	})
}
func (k Online) ExpireOnlineWork(ctx context.Context, s ob.Scope, id string) (ev.Reply, error) {
	if !s.Reconcile {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.workTransaction(ctx, s, id, false, func(tx pgx.Tx, w ob.Work, _ ob.RuleVersion, _ ProjectOnlinePolicy, _, _ usage) (ev.Reply, error) {
		now, e := dbNow(ctx, tx)
		if e != nil {
			return ev.Reply{}, e
		}
		if w.Status != "CLAIMED" || w.Lease == nil || w.Lease.After(now) {
			return ev.Reply{Code: ev.NotReady}, nil
		}
		for i := range w.CallRecords {
			if w.CallRecords[i].Provenance.Classification == "STARTED" {
				w.CallRecords[i].Provenance.Classification = "RESPONSE_UNKNOWN"
			}
		}
		_, e = tx.Exec(ctx, `UPDATE evaluation_online_works SET status='PENDING',claim_token=NULL,writer_epoch=NULL,lease_expires_at=NULL,owner_ref=NULL,calls_bytes=$3 WHERE project_id=$1 AND id=$2 AND status='CLAIMED' AND lease_expires_at<=clock_timestamp()`, s.ProjectID, id, encode(w.CallRecords))
		return ev.Reply{Code: ev.Applied}, e
	})
}
func checkRate(ctx context.Context, tx pgx.Tx, s ob.Scope, ref asset.Ref, rate ob.Rate, project bool) error {
	if rate.Mode != "DURABLE_INTERVAL" {
		return nil
	}
	var count int
	// STARTED 包括可能未真正 dispatch 的调用，保守计入跨进程窗口。
	sql := `SELECT count(*) FROM evaluation_online_works w CROSS JOIN LATERAL jsonb_array_elements(convert_from(w.calls_bytes,'UTF8')::jsonb) c WHERE w.project_id=$1 AND (c->'Provenance'->>'StartedAt')::timestamptz>clock_timestamp()-$2*interval '1 second'`
	args := []any{s.ProjectID, rate.IntervalSeconds}
	if !project {
		sql += ` AND rule_id=$3 AND rule_version=$4`
		args = append(args, ref.EntityID, ref.Version)
	}
	if e := tx.QueryRow(ctx, sql, args...).Scan(&count); e != nil {
		return e
	}
	if count >= rate.MaxCalls {
		return stop(ev.Rejected, "SKIPPED_RATE_LIMIT")
	}
	return nil
}
func (k Online) BeginOnlineCall(ctx context.Context, s ob.Scope, id, token string, call ob.Call) (ev.Reply, error) {
	return k.workTransaction(ctx, s, id, false, func(tx pgx.Tx, w ob.Work, v ob.RuleVersion, p ProjectOnlinePolicy, pu, ru usage) (ev.Reply, error) {
		if e := onlineOwned(ctx, tx, s, w, token); e != nil {
			return ev.Reply{}, e
		}
		c := call.Provenance
		for _, old := range w.CallRecords {
			if old.Provenance.ID == c.ID {
				if old.Provenance.Author.Token == token && old.Provenance.PromptDigest == c.PromptDigest && old.EstimatedTokens == call.EstimatedTokens {
					return ev.Reply{Code: ev.AlreadyApplied, ID: c.ID}, nil
				}
				return ev.Reply{}, stop(ev.Conflict, "CALL_INTENT_CONFLICT")
			}
		}
		d := w.Binding.Evaluator.Definition
		if d.Model == nil || d.Kind != "LLM_JUDGE" || !asset.ValidID(c.ID) || c.Number != w.Calls+1 || c.EvaluationNumber != w.Claims || c.PromptRef == nil || d.PromptRef == nil || *c.PromptRef != *d.PromptRef || len(c.PromptDigest) != 64 || c.InputDigest != w.Observation.Ref.Digest || c.EvidenceDigest != w.Observation.Ref.Digest || c.Schema != d.SchemaVersion || call.EstimatedTokens < 1 || c.RequestedProvider != d.Model.Provider || c.RequestedModel != d.Model.Model {
			return ev.Reply{}, stop(ev.Rejected, "INVALID_CALL_BINDING")
		}
		if w.Calls >= w.Binding.Evaluator.Definition.Budget.MaxProviderCalls || w.Claims > w.Binding.Evaluator.Definition.Retry.MaxEvaluationAttempts {
			return ev.Reply{}, stop(ev.Rejected, "SKIPPED_BUDGET")
		}
		for _, item := range []struct {
			b ob.Budget
			u usage
		}{{p.Budget, pu}, {v.Body.Budget, ru}} {
			if item.u.Calls >= item.b.MaxProviderCalls || call.EstimatedTokens > item.b.MaxEstimatedTokens-item.u.Estimated || call.EstimatedTokens > item.b.MaxReportedTokens-item.u.Estimated || item.u.Reported >= item.b.MaxReportedTokens {
				return ev.Reply{}, stop(ev.Rejected, "SKIPPED_BUDGET")
			}
		}
		if e := checkRate(ctx, tx, s, w.Rule, p.Rate, true); e != nil {
			return ev.Reply{}, e
		}
		if e := checkRate(ctx, tx, s, w.Rule, v.Body.Rate, false); e != nil {
			return ev.Reply{}, e
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return ev.Reply{}, e
		}
		call.Provenance.StartedAt = now
		call.Provenance.Classification = "STARTED"
		call.Provenance.Author = ev.Receipt{Token: token, Epoch: s.Epoch}
		call.Provenance.UsageAvailability = "UNKNOWN"
		call.Provenance.CostAvailability = "UNKNOWN"
		call.Draft = nil
		w.CallRecords = append(w.CallRecords, call)
		if e = onlineFenced(ctx, tx, s, w, `UPDATE evaluation_online_works SET call_count=call_count+1,calls_bytes=$3 WHERE project_id=$1 AND id=$2`, s.ProjectID, id, encode(w.CallRecords)); e != nil {
			return ev.Reply{}, e
		}
		_, e = tx.Exec(ctx, `UPDATE evaluation_online_project_limits SET calls=calls+1,estimated_tokens=estimated_tokens+$2 WHERE project_id=$1`, s.ProjectID, call.EstimatedTokens)
		if e != nil {
			return ev.Reply{}, e
		}
		_, e = tx.Exec(ctx, `UPDATE evaluation_online_rule_usage SET calls=calls+1,estimated_tokens=estimated_tokens+$4 WHERE project_id=$1 AND rule_id=$2 AND rule_version=$3`, s.ProjectID, w.Rule.EntityID, w.Rule.Version, call.EstimatedTokens)
		return ev.Reply{Code: ev.Applied, ID: c.ID}, e
	})
}
func (k Online) FinishOnlineCall(ctx context.Context, s ob.Scope, id, token string, f ob.FinishCall) (ev.Reply, error) {
	return k.workTransaction(ctx, s, id, false, func(tx pgx.Tx, w ob.Work, _ ob.RuleVersion, _ ProjectOnlinePolicy, _, _ usage) (ev.Reply, error) {
		if e := onlineOwned(ctx, tx, s, w, token); e != nil {
			return ev.Reply{}, e
		}
		digest, _ := ev.Intent(f)
		var call *ob.Call
		for i := range w.CallRecords {
			if w.CallRecords[i].Provenance.ID == f.CallID {
				call = &w.CallRecords[i]
			}
		}
		if call == nil || call.Provenance.Author.Token != token {
			return ev.Reply{}, stop(ev.Rejected, "CALL_OWNER_MISMATCH")
		}
		c := &call.Provenance
		if c.FinishDigest == digest {
			return ev.Reply{Code: ev.AlreadyApplied}, nil
		}
		if c.Classification != "STARTED" {
			return ev.Reply{}, stop(ev.Conflict, "CALL_FINISHED")
		}
		if !containsOnline(f.Provenance.Classification, "RESPONSE_RECEIVED", "ERROR", "RESPONSE_UNKNOWN") || len(encode(f)) > w.Binding.Evaluator.Definition.Budget.MaxResponseBytes {
			return ev.Reply{}, stop(ev.Rejected, "INVALID_CALL_RECEIPT")
		}
		if f.Draft != nil && f.Draft.Validate(w.Binding) != nil {
			return ev.Reply{}, stop(ev.Rejected, "INVALID_DRAFT")
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return ev.Reply{}, e
		}
		c.FinishedAt = &now
		c.FinishDigest = digest
		c.Classification = f.Provenance.Classification
		c.ActualProvider = f.Provenance.ActualProvider
		c.ActualModel = f.Provenance.ActualModel
		c.ActualRevision = f.Provenance.ActualRevision
		c.Usage = f.Provenance.Usage
		c.UsageAvailability = f.Provenance.UsageAvailability
		c.CostAvailability = f.Provenance.CostAvailability
		c.ResponseDigest = f.Provenance.ResponseDigest
		c.RemoteRequestID = f.Provenance.RemoteRequestID
		call.Draft = f.Draft
		reported := int64(0)
		if c.UsageAvailability == "AVAILABLE" {
			var usage map[string]asset.JSON
			var total int64
			if c.Usage.Decode(&usage) != nil || usage["total_tokens"].Decode(&total) != nil || total < 0 {
				return ev.Reply{}, stop(ev.Rejected, "INVALID_USAGE")
			}
			reported = total
		}
		if e = onlineFenced(ctx, tx, s, w, `UPDATE evaluation_online_works SET calls_bytes=$3 WHERE project_id=$1 AND id=$2`, s.ProjectID, id, encode(w.CallRecords)); e != nil {
			return ev.Reply{}, e
		}
		_, e = tx.Exec(ctx, `UPDATE evaluation_online_project_limits SET reported_tokens=reported_tokens+$2 WHERE project_id=$1`, s.ProjectID, reported)
		if e != nil {
			return ev.Reply{}, e
		}
		_, e = tx.Exec(ctx, `UPDATE evaluation_online_rule_usage SET reported_tokens=reported_tokens+$4 WHERE project_id=$1 AND rule_id=$2 AND rule_version=$3`, s.ProjectID, w.Rule.EntityID, w.Rule.Version, reported)
		return ev.Reply{Code: ev.Applied}, e
	})
}
func containsOnline(s string, values ...string) bool {
	for _, v := range values {
		if s == v {
			return true
		}
	}
	return false
}
func (k Online) FinalizeOnlineResult(ctx context.Context, s ob.Scope, id, token, command, resultID string, v ob.Value) (ev.Reply, error) {
	if !asset.ValidID(command) || !asset.ValidID(resultID) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.workTransaction(ctx, s, id, false, func(tx pgx.Tx, w ob.Work, _ ob.RuleVersion, _ ProjectOnlinePolicy, _, _ usage) (ev.Reply, error) {
		intent, _ := ev.Intent(v)
		var oldIntent, oldToken, oldID, oldCommand string
		var oldEpoch int64
		e := tx.QueryRow(ctx, `SELECT id::text,intent_digest,author_token::text,author_epoch,command_id::text FROM evaluation_online_results WHERE project_id=$1 AND work_id=$2`, s.ProjectID, id).Scan(&oldID, &oldIntent, &oldToken, &oldEpoch, &oldCommand)
		if e == nil {
			if oldID == resultID && oldIntent == intent && oldToken == token && oldEpoch == s.Epoch && oldCommand == command {
				return ev.Reply{Code: ev.AlreadyApplied, ID: oldID}, nil
			}
			return ev.Reply{}, stop(ev.Conflict, "ONLINE_RESULT_SLOT_CONFLICT")
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return ev.Reply{}, e
		}
		if e = onlineOwned(ctx, tx, s, w, token); e != nil {
			return ev.Reply{}, e
		}
		if v.Validate(w.Binding) != nil {
			return ev.Reply{}, stop(ev.Rejected, "INVALID_ONLINE_RESULT")
		}
		selected := map[string]bool{}
		for _, selectedID := range v.SelectedCallIDs {
			if selected[selectedID] {
				return ev.Reply{}, stop(ev.Rejected, "DUPLICATE_SELECTED_CALL")
			}
			selected[selectedID] = true
			found := false
			for _, c := range w.CallRecords {
				if c.Provenance.ID == selectedID && c.Provenance.Classification == "RESPONSE_RECEIVED" && c.Draft != nil {
					a, _ := ev.Intent(*c.Draft)
					if a == intent {
						found = true
					}
				}
			}
			if !found {
				return ev.Reply{}, stop(ev.Rejected, "INVALID_SELECTED_CALL")
			}
		}
		if w.Binding.Evaluator.Definition.Kind == "LLM_JUDGE" && containsOnline(v.Verdict, "PASS", "FAIL") && len(selected) == 0 {
			return ev.Reply{}, stop(ev.Rejected, "JUDGE_PROVENANCE_REQUIRED")
		}
		var selection ob.Selection
		var selectedBytes []byte
		if e = tx.QueryRow(ctx, `SELECT selection_bytes FROM evaluation_online_admissions WHERE project_id=$1 AND rule_id=$2 AND rule_version=$3 AND observation_id=$4`, s.ProjectID, w.Rule.EntityID, w.Rule.Version, w.ObservationID).Scan(&selectedBytes); e != nil {
			return ev.Reply{}, e
		}
		if e = json.Unmarshal(selectedBytes, &selection); e != nil {
			return ev.Reply{}, e
		}
		result := ob.Result{ID: resultID, WorkID: id, ProjectID: s.ProjectID, ObservationID: w.ObservationID, Observation: w.Observation.Ref, Sampling: selection, Rule: w.Rule, Binding: w.Binding, EvidenceDigest: w.Observation.Ref.Digest, Value: v, Calls: w.CallRecords}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_online_results(id,project_id,work_id,result_bytes,command_id,author_token,author_epoch,intent_digest) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, resultID, s.ProjectID, id, encode(result), command, token, s.Epoch, intent)
		if e != nil {
			return ev.Reply{}, e
		}
		e = onlineFenced(ctx, tx, s, w, `UPDATE evaluation_online_works SET status='COMPLETED',result_id=$3,completed_at=clock_timestamp() WHERE project_id=$1 AND id=$2`, s.ProjectID, id, resultID)
		if e != nil {
			return ev.Reply{}, e
		}
		event := "online_result_completed"
		if v.Applicability == asset.MissingEvidence {
			event = "online_missing_evidence"
		}
		slog.Info(event, "event", event, "work_id", id, "project_id", s.ProjectID)
		return ev.Reply{Code: ev.Applied, ID: resultID}, nil
	})
}
func (k Online) OnlineCandidates(ctx context.Context, c ob.Cursor, expired bool, limit int) ([]ob.Candidate, error) {
	if limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	predicate := `status='PENDING'`
	if expired {
		predicate = `status='CLAIMED' AND lease_expires_at<=clock_timestamp()`
	}
	rows, e := k.Pool.Query(ctx, `SELECT project_id::text,id::text,created_at FROM evaluation_online_works WHERE `+predicate+` AND ($1::timestamptz IS NULL OR (created_at,id)>($1,$2::uuid)) ORDER BY created_at,id LIMIT $3`, nullableTime(c.Created), nullableID(c.ID), limit)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []ob.Candidate{}
	for rows.Next() {
		var v ob.Candidate
		if e = rows.Scan(&v.ProjectID, &v.ID, &v.Created); e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
