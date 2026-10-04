package postgres

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"errors"
	"github.com/jackc/pgx/v5"
	"time"
)

func workReady(ctx context.Context, tx pgx.Tx, s ev.Scope, r ev.Run, o ev.Owned) (ev.Work, ev.Attempt, error) {
	if e := activeRun(r); e != nil {
		return ev.Work{}, ev.Attempt{}, e
	}
	a, e := getAttempt(ctx, tx, s, o)
	if e != nil {
		return ev.Work{}, a, e
	}
	if a.Status != "TERMINAL" || a.Outcome != ev.Success {
		return ev.Work{}, a, stop(ev.Rejected, "ATTEMPT_NOT_SUCCESS")
	}
	w, e := getWork(ctx, tx, s, o)
	return w, a, e
}
func workOwned(ctx context.Context, tx pgx.Tx, s ev.Scope, o ev.Owned, w ev.Work) error {
	return owned(ctx, tx, w.Token, w.Epoch, w.Lease, s, o, w.Status == "CLAIMED")
}
func (k Evaluation) ClaimEvaluatorWork(ctx context.Context, s ev.Scope, o ev.Owned, owner string, d time.Duration) (ev.Reply, error) {
	if !s.Evaluate || !asset.Text(owner) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	if e := k.lease(d); e != nil {
		return finishStop(e)
	}
	return k.transact(ctx, s, o.RunID, true, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		w, _, e := workReady(ctx, tx, s, r, o)
		if e != nil {
			return ev.Reply{}, e
		}
		if !k.Capabilities.Supports(w.Metadata.Spec) {
			return ev.Reply{}, stop(ev.Rejected, "UNSUPPORTED_EVALUATOR_IMPLEMENTATION")
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return ev.Reply{}, e
		}
		if w.Status != "PENDING" || w.ResultID != nil || w.Next.After(now) {
			return ev.Reply{Code: ev.NotClaimed}, nil
		}
		if (w.Metadata.DeadlineAt != nil && !w.Metadata.DeadlineAt.After(now)) || w.Evaluations >= w.Metadata.Spec.Definition.Retry.MaxEvaluationAttempts {
			w.Metadata.CompletionMode = "FINALIZE_ERROR"
		}
		_, e = tx.Exec(ctx, "UPDATE evaluation_evaluator_works SET metadata=$4 WHERE project_id=$1 AND run_id=$2 AND id=$3", s.ProjectID, r.ID, w.ID, encode(w.Metadata))
		if e != nil {
			return ev.Reply{}, e
		}
		token := asset.NewID()
		var expiry time.Time
		e = tx.QueryRow(ctx, `UPDATE evaluation_evaluator_works SET status='CLAIMED',claim_token=$4,writer_epoch=$5,owner_ref=$6,claimed_at=clock_timestamp(),lease_expires_at=clock_timestamp()+$7*interval '1 millisecond',claim_count=claim_count+1 WHERE project_id=$1 AND run_id=$2 AND id=$3 AND status='PENDING' RETURNING lease_expires_at`, s.ProjectID, r.ID, w.ID, token, s.Epoch, owner, d.Milliseconds()).Scan(&expiry)
		return ev.Reply{Code: ev.Applied, ID: w.ID, Token: token, Lease: &expiry}, e
	})
}
func (k Evaluation) RenewEvaluatorLease(ctx context.Context, s ev.Scope, o ev.Owned, d time.Duration) (ev.Reply, error) {
	if !s.Evaluate {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	if e := k.lease(d); e != nil {
		return finishStop(e)
	}
	return k.transact(ctx, s, o.RunID, false, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		w, _, e := workReady(ctx, tx, s, r, o)
		if e != nil {
			return ev.Reply{}, e
		}
		if e = workOwned(ctx, tx, s, o, w); e != nil {
			return ev.Reply{}, e
		}
		var expiry time.Time
		e = tx.QueryRow(ctx, "UPDATE evaluation_evaluator_works SET lease_expires_at=clock_timestamp()+$4*interval '1 millisecond' WHERE project_id=$1 AND run_id=$2 AND id=$3 AND claim_token=$5 AND writer_epoch=$6 AND lease_expires_at>clock_timestamp() AND status='CLAIMED' RETURNING lease_expires_at", s.ProjectID, r.ID, w.ID, d.Milliseconds(), o.Token, s.Epoch).Scan(&expiry)
		if errors.Is(e, pgx.ErrNoRows) {
			return ev.Reply{}, stop(ev.OwnershipLost, "")
		}
		return ev.Reply{Code: ev.Applied, Lease: &expiry}, e
	})
}
func (k Evaluation) BeginEvaluatorAttempt(ctx context.Context, s ev.Scope, o ev.Owned, command string, no int) (ev.Reply, error) {
	if !s.Evaluate || !asset.ValidID(command) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	digest, _ := ev.Intent(struct {
		Number int
		Author string
	}{no, o.Token})
	want := receipt(command, digest, s, o)
	return k.transact(ctx, s, o.RunID, false, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		w, _, e := workReady(ctx, tx, s, r, o)
		if e != nil {
			return ev.Reply{}, e
		}
		if e = workOwned(ctx, tx, s, o, w); e != nil {
			return ev.Reply{}, e
		}
		for _, rc := range w.Metadata.EvaluationReceipts {
			if rc.CommandID == command {
				if e = compareReceipt(rc, want); e != nil {
					return ev.Reply{}, e
				}
				return ev.Reply{Code: ev.AlreadyApplied}, nil
			}
		}
		if w.Metadata.CompletionMode != "NORMAL" || w.Evaluations >= w.Metadata.Spec.Definition.Retry.MaxEvaluationAttempts {
			return ev.Reply{}, stop(ev.Rejected, "EVALUATION_BUDGET_EXHAUSTED")
		}
		if no != w.Evaluations+1 {
			return ev.Reply{}, stop(ev.Conflict, "ATTEMPT_NUMBER")
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return ev.Reply{}, e
		}
		if w.Metadata.DeadlineAt == nil {
			deadline := now.Add(time.Duration(w.Metadata.Spec.Definition.Budget.TotalMilliseconds) * time.Millisecond)
			w.Metadata.DeadlineAt = &deadline
		} else if !w.Metadata.DeadlineAt.After(now) {
			return ev.Reply{}, stop(ev.Rejected, "EVALUATOR_DEADLINE")
		}
		w.Metadata.EvaluationReceipts = append(w.Metadata.EvaluationReceipts, want)
		e = fencedExec(ctx, tx, "UPDATE evaluation_evaluator_works SET evaluation_attempt_count=evaluation_attempt_count+1,metadata=$4 WHERE project_id=$1 AND run_id=$2 AND id=$3", s, o, s.ProjectID, r.ID, w.ID, encode(w.Metadata))
		return ev.Reply{Code: ev.Applied}, e
	})
}
func backoff(w ev.Work) time.Duration {
	p := w.Metadata.Spec.Definition.Retry
	ms := p.InitialBackoffMilliseconds
	for i := 1; i < w.Evaluations && ms < p.MaxBackoffMilliseconds; i++ {
		if ms > p.MaxBackoffMilliseconds/2 {
			ms = p.MaxBackoffMilliseconds
			break
		}
		ms *= 2
	}
	if ms > p.MaxBackoffMilliseconds {
		ms = p.MaxBackoffMilliseconds
	}
	return time.Duration(ms) * time.Millisecond
}
func releaseWork(ctx context.Context, tx pgx.Tx, s ev.Scope, r ev.Run, w ev.Work, category, reason string, rc *ev.Receipt) error {
	for i := range w.Calls {
		if w.Calls[i].Classification == "STARTED" {
			w.Calls[i].Classification = "RESPONSE_UNKNOWN"
			w.Calls[i].UsageAvailability = "UNKNOWN"
			w.Calls[i].CostAvailability = "UNKNOWN"
		}
	}
	if w.Evaluations >= w.Metadata.Spec.Definition.Retry.MaxEvaluationAttempts || (w.Metadata.Spec.Definition.Budget.MaxProviderCalls > 0 && w.CallCount >= w.Metadata.Spec.Definition.Budget.MaxProviderCalls) {
		w.Metadata.CompletionMode = "FINALIZE_ERROR"
	}
	if rc != nil {
		w.Metadata.ReleaseReceipts = append(w.Metadata.ReleaseReceipts, *rc)
	}
	sql := `UPDATE evaluation_evaluator_works SET status='PENDING',claim_token=NULL,owner_ref=NULL,lease_expires_at=NULL,last_error_category=$4,last_error_reason=$5,next_available_at=clock_timestamp()+$6*interval '1 millisecond',bounded_call_provenance=$7,metadata=$8 WHERE project_id=$1 AND run_id=$2 AND id=$3`
	args := []any{s.ProjectID, r.ID, w.ID, category, reason, backoff(w).Milliseconds(), encode(w.Calls), encode(w.Metadata)}
	if rc != nil {
		return fencedExec(ctx, tx, sql, s, ev.Owned{Token: rc.Token}, args...)
	}
	tag, e := tx.Exec(ctx, sql+" AND status='CLAIMED' AND lease_expires_at<=clock_timestamp()", args...)
	if e == nil && tag.RowsAffected() != 1 {
		return stop(ev.NotReady, "")
	}

	return e
}
func (k Evaluation) ReleaseEvaluatorWorkRetry(ctx context.Context, s ev.Scope, o ev.Owned, command, reason string) (ev.Reply, error) {
	if !s.Evaluate || !asset.ValidID(command) || !asset.Text(reason) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	digest, _ := ev.Intent(reason)
	want := receipt(command, digest, s, o)
	return k.transact(ctx, s, o.RunID, false, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		w, _, e := workReady(ctx, tx, s, r, o)
		if e != nil {
			return ev.Reply{}, e
		}
		for _, prior := range w.Metadata.ReleaseReceipts {
			if prior.CommandID == command {
				if e = compareReceipt(prior, want); e != nil {
					return ev.Reply{}, e
				}
				return ev.Reply{Code: ev.AlreadyApplied}, nil
			}
		}
		if e = workOwned(ctx, tx, s, o, w); e != nil {
			return ev.Reply{}, e
		}
		if len(w.Metadata.EvaluationReceipts) == 0 || w.Metadata.EvaluationReceipts[len(w.Metadata.EvaluationReceipts)-1].Token != o.Token {
			return ev.Reply{}, stop(ev.Rejected, "NO_EVALUATOR_ATTEMPT")
		}
		e = releaseWork(ctx, tx, s, r, w, "EVALUATOR_RETRY", reason, &want)
		return ev.Reply{Code: ev.Applied}, e
	})
}
func (k Evaluation) ExpireEvaluatorWork(ctx context.Context, s ev.Scope, o ev.Owned) (ev.Reply, error) {
	if !s.Coordinate {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.transact(ctx, s, o.RunID, false, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		w, _, e := workReady(ctx, tx, s, r, o)
		if e != nil {
			return ev.Reply{}, e
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return ev.Reply{}, e
		}
		if w.Status != "CLAIMED" {
			return ev.Reply{Code: ev.AlreadyApplied}, nil
		}
		if o.Token != "" && (w.Token == nil || *w.Token != o.Token) {
			return ev.Reply{}, stop(ev.OwnershipLost, "")
		}
		if w.Lease == nil || w.Lease.After(now) {
			return ev.Reply{Code: ev.NotReady}, nil
		}
		e = releaseWork(ctx, tx, s, r, w, "STALE_EVALUATOR", "DB lease expired", nil)
		return ev.Reply{Code: ev.Applied}, e
	})
}
func (k Evaluation) BeginProviderCall(ctx context.Context, s ev.Scope, c ev.BeginCall) (ev.Reply, error) {
	if !s.Evaluate || !asset.ValidID(c.Call.ID) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	digest, e := ev.Intent(c.Call)
	if e != nil {
		return ev.Reply{}, e
	}
	want := receipt(c.Call.ID, digest, s, c.Owned)
	return k.transact(ctx, s, c.RunID, false, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		w, a, e := workReady(ctx, tx, s, r, c.Owned)
		if e != nil {
			return ev.Reply{}, e
		}
		if e = workOwned(ctx, tx, s, c.Owned, w); e != nil {
			return ev.Reply{}, e
		}
		for _, old := range w.Calls {
			if old.ID == c.Call.ID {
				if e = compareReceipt(old.Author, want); e != nil {
					return ev.Reply{}, e
				}
				return ev.Reply{Code: ev.AlreadyApplied, ID: old.ID, Status: old.Classification}, nil
			}
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return ev.Reply{}, e
		}
		if w.Metadata.DeadlineAt == nil || !w.Metadata.DeadlineAt.After(now) {
			return ev.Reply{}, stop(ev.Rejected, "EVALUATOR_DEADLINE")
		}
		spec := w.Metadata.Spec.Definition
		call := c.Call
		if w.Metadata.CompletionMode != "NORMAL" || w.Evaluations == 0 || len(w.Metadata.EvaluationReceipts) == 0 || w.Metadata.EvaluationReceipts[len(w.Metadata.EvaluationReceipts)-1].Token != c.Token || w.CallCount >= spec.Budget.MaxProviderCalls {
			return ev.Reply{}, stop(ev.Rejected, "PROVIDER_BUDGET_OR_ATTEMPT")
		}
		if call.Number != w.CallCount+1 || call.EvaluationNumber != w.Evaluations || !asset.Text(call.Suboperation) || call.Schema != spec.SchemaVersion || call.InputDigest != a.Request.Case.Identity.ContentDigest || spec.Model == nil || call.RequestedProvider != spec.Model.Provider || call.RequestedModel != spec.Model.Model || call.PromptRef == nil || spec.PromptRef == nil || *call.PromptRef != *spec.PromptRef || !asset.Text(call.PromptDigest) || call.EvidenceDigest != bindingDigest(a.Metadata.Observation.Evidence) {
			return ev.Reply{}, stop(ev.Rejected, "CALL_BINDING")
		}
		if call.Classification != "" || call.FinishedAt != nil || call.Response.String() != "null" || call.ActualProvider != "" || call.Author.CommandID != "" || call.Draft != nil || call.ResponseBytes != nil || call.DraftBytes != nil {
			return ev.Reply{}, stop(ev.Rejected, "CALL_INTENT_ONLY")
		}
		call.Author = want
		call.Classification = "STARTED"
		call.UsageAvailability = "UNKNOWN"
		call.CostAvailability = "UNKNOWN"
		call.StartedAt, e = dbNow(ctx, tx)
		if e != nil {
			return ev.Reply{}, e
		}
		w.Calls = append(w.Calls, call)
		e = fencedExec(ctx, tx, "UPDATE evaluation_evaluator_works SET provider_call_count=provider_call_count+1,bounded_call_provenance=$4 WHERE project_id=$1 AND run_id=$2 AND id=$3", s, c.Owned, s.ProjectID, r.ID, w.ID, encode(w.Calls))
		return ev.Reply{Code: ev.Applied, ID: call.ID}, e
	})
}
func bindingDigest(v any) string { d, _ := ev.Intent(v); return d }
func (k Evaluation) FinishProviderCall(ctx context.Context, s ev.Scope, c ev.FinishCall) (ev.Reply, error) {
	if !s.Evaluate {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	digest, e := ev.Intent(c)
	if e != nil {
		return ev.Reply{}, e
	}
	return k.transact(ctx, s, c.RunID, false, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		w, a, e := workReady(ctx, tx, s, r, c.Owned)
		if e != nil {
			return ev.Reply{}, e
		}
		if e = workOwned(ctx, tx, s, c.Owned, w); e != nil {
			return ev.Reply{}, e
		}
		index := -1
		for i, call := range w.Calls {
			if call.ID == c.CallID {
				index = i
			}
		}
		if index < 0 {
			return ev.Reply{}, stop(ev.NotFound, "CALL")
		}
		call := &w.Calls[index]
		if call.Author.Token != c.Token || call.Author.Epoch != s.Epoch {
			return ev.Reply{}, stop(ev.OwnershipLost, "CALL_AUTHOR")
		}
		if call.Classification != "STARTED" {
			if call.FinishDigest != digest {
				return ev.Reply{}, stop(ev.Conflict, "CALL_CONFLICT")
			}
			return ev.Reply{Code: ev.AlreadyApplied}, nil
		}
		if c.Classification != "RESPONSE_RECEIVED" && c.Classification != "ERROR" && c.Classification != "RESPONSE_UNKNOWN" {
			return ev.Reply{}, stop(ev.Rejected, "CALL_CLASSIFICATION")
		}
		if len(encode(c)) > w.Metadata.Spec.Definition.Budget.MaxResponseBytes {
			return ev.Reply{}, stop(ev.Rejected, "RESPONSE_TOO_LARGE")
		}
		if (c.UsageAvailability != "AVAILABLE" && c.UsageAvailability != "UNKNOWN" && c.UsageAvailability != "NOT_APPLICABLE") || (c.CostAvailability != "AVAILABLE" && c.CostAvailability != "UNKNOWN" && c.CostAvailability != "NOT_APPLICABLE") {
			return ev.Reply{}, stop(ev.Rejected, "AVAILABILITY")
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return ev.Reply{}, e
		}
		call.FinishedAt = &now
		call.Classification = c.Classification
		call.ActualProvider = c.ActualProvider
		call.ActualModel = c.ActualModel
		call.ActualRevision = c.ActualRevision
		call.ResponseDigest = c.ResponseDigest
		call.ResponseRef = c.ResponseRef
		call.RemoteRequestID = c.RemoteRequestID
		call.Usage = c.Usage
		call.Cost = c.Cost
		call.Response = c.Response
		call.UsageAvailability = c.UsageAvailability
		call.CostAvailability = c.CostAvailability
		if c.Draft != nil {
			if a.Metadata.Observation.Artifact == nil || c.Draft.Validate(w.Metadata.Spec, a.Request.Case.Identity.ContentDigest, *a.Metadata.Observation.Artifact) != nil {
				return ev.Reply{}, stop(ev.Rejected, "DRAFT_BINDING")
			}
		}
		call.Draft = c.Draft
		call.ResponseBytes = c.Response.Bytes()
		if c.Draft != nil {
			frozen, err := asset.Freeze(c.Draft)
			if err != nil {
				return ev.Reply{}, err
			}
			call.DraftBytes = frozen.Bytes()
		}
		call.FinishDigest = digest
		e = fencedExec(ctx, tx, "UPDATE evaluation_evaluator_works SET bounded_call_provenance=$4 WHERE project_id=$1 AND run_id=$2 AND id=$3", s, c.Owned, s.ProjectID, r.ID, w.ID, encode(w.Calls))
		return ev.Reply{Code: ev.Applied}, e
	})
}
func (k Evaluation) FinalizeEvaluationResult(ctx context.Context, s ev.Scope, c ev.FinalizeResult) (ev.Reply, error) {
	if !s.Evaluate || !asset.ValidID(c.CommandID) || !asset.ValidID(c.ResultID) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	digest, e := ev.Intent(c.Value)
	if e != nil {
		return ev.Reply{}, e
	}
	want := receipt(c.CommandID, digest, s, c.Owned)
	return k.transact(ctx, s, c.RunID, false, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		w, e := getWork(ctx, tx, s, c.Owned)
		if e != nil {
			return ev.Reply{}, e
		}
		if w.ResultID != nil {
			var old ev.EvaluationResult
			e = loadJSON(ctx, tx, "SELECT to_jsonb(x) FROM evaluation_results x WHERE project_id=$1 AND run_id=$2 AND id=$3", &old, s.ProjectID, r.ID, *w.ResultID)
			if e != nil {
				return ev.Reply{}, e
			}
			if old.ID != c.ResultID {
				return ev.Reply{}, stop(ev.Conflict, "RESULT_SLOT_CONFLICT")
			}
			if e = compareReceipt(old.Receipt, want); e != nil {
				return ev.Reply{}, e
			}
			return ev.Reply{Code: ev.AlreadyApplied, ID: old.ID}, nil
		}
		w, a, e := workReady(ctx, tx, s, r, c.Owned)
		if e != nil {
			return ev.Reply{}, e
		}
		if e = workOwned(ctx, tx, s, c.Owned, w); e != nil {
			return ev.Reply{}, e
		}
		artifact := a.Metadata.Observation.Artifact
		if artifact == nil {
			return ev.Reply{}, stop(ev.IntegrityBlocked, "MISSING_ARTIFACT")
		}
		if e = c.Value.Validate(w.Metadata.Spec, a.Request.Case.Identity.ContentDigest, *artifact); e != nil {
			return ev.Reply{}, stop(ev.Rejected, "RESULT_BINDING")
		}
		if bindingDigest(c.Value.Evidence) != bindingDigest(a.Metadata.Observation.Evidence) {
			return ev.Reply{}, stop(ev.Rejected, "EVIDENCE_BINDING")
		}
		if c.Value.Score != nil {
			for _, m := range r.Snapshot.Input.Metrics {
				for _, ref := range w.Metadata.Spec.Metrics {
					if m.Identity.Ref == ref && m.Definition.Range != nil && (*c.Value.Score < m.Definition.Range[0] || *c.Value.Score > m.Definition.Range[1]) {
						return ev.Reply{}, stop(ev.Rejected, "SCORE_RANGE")
					}
				}
			}
		}
		selected := map[string]bool{}
		for _, id := range c.Value.SelectedCallIDs {
			if selected[id] {
				return ev.Reply{}, stop(ev.Rejected, "DUPLICATE_CALL")
			}
			found := false
			for _, call := range w.Calls {
				if call.ID == id && call.Classification == "RESPONSE_RECEIVED" {
					found = true
					if c.Value.Score != nil && (call.ActualProvider != call.RequestedProvider || call.ActualModel != call.RequestedModel || !asset.Text(call.ActualRevision)) {
						return ev.Reply{}, stop(ev.Rejected, "PROVIDER_MISMATCH")
					}
				}
			}
			if !found {
				return ev.Reply{}, stop(ev.Rejected, "UNOBSERVED_CALL")
			}
			selected[id] = true
		}
		if w.Metadata.Spec.Definition.Budget.MaxProviderCalls > 0 && c.Value.Score != nil && len(selected) == 0 {
			return ev.Reply{}, stop(ev.Rejected, "MISSING_PROVIDER_PROVENANCE")
		}
		durableDraft := false
		for _, call := range w.Calls {
			if selected[call.ID] && call.Draft != nil && bindingDigest(*call.Draft) == bindingDigest(c.Value) {
				durableDraft = true
			}
		}
		if w.Metadata.CompletionMode == "FINALIZE_ERROR" && !durableDraft && (c.Value.Score != nil || (c.Value.Verdict != "ERROR" && c.Value.Verdict != "INCONCLUSIVE")) {
			return ev.Reply{}, stop(ev.Rejected, "FINALIZE_ERROR_ONLY")
		}
		d, dv, u, uv := identity(r)
		spec := w.Metadata.Spec
		configDigest := bindingDigest(spec.Definition.Config)
		meta := struct {
			Value       ev.ResultValue
			ValueBytes  []byte
			Calls       []ev.ProviderCall
			Evaluations int
			Requested   any
			Actor       string
		}{c.Value, encode(c.Value), w.Calls, w.Evaluations, spec.Definition.Model, s.Principal}
		var pk, pv any
		if spec.Definition.PromptRef != nil {
			pk = "catalog-version"
			pv = spec.Definition.PromptRef.EntityID + ":" + spec.Definition.PromptRef.Version
		}
		evidence := c.Value.Evidence
		if evidence == nil {
			evidence = []ev.Binding{}
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_results(id,project_id,run_id,attempt_id,dataset_id,dataset_version,case_id,case_version,suite_id,suite_version,evaluator_id,evaluator_version,config_ref_kind,config_ref_value,prompt_ref_kind,prompt_ref_value,execution_target_id,target_version_kind,target_version_value,execution_request_id,verdict,reason,provenance_completeness,output_artifact_ref,score,evidence_refs,metadata,work_id,contract_version,kernel_value,kernel_receipt) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'catalog-json-v1',$13,$14,$15,$16,'version',$17,$18,$19,$20,'COMPLETE',$21,$22,$23,$24,$25,$26,$27,$28)`, c.ResultID, s.ProjectID, r.ID, a.ID, d, dv, a.CaseID, a.CaseVersion, u, uv, w.EvaluatorID, w.EvaluatorVersion, configDigest, pk, pv, r.Snapshot.Target.ID, r.Snapshot.Target.Version, a.RequestID, c.Value.Verdict, c.Value.Reason, encode(artifact), c.Value.Score, encode(evidence), encode(meta), w.ID, ev.DurableContract, encode(c.Value), encode(want))
		if e != nil {
			return ev.Reply{}, e
		}
		e = fencedExec(ctx, tx, "UPDATE evaluation_evaluator_works SET status='COMPLETED',result_id=$4,completed_at=clock_timestamp() WHERE project_id=$1 AND run_id=$2 AND id=$3", s, c.Owned, s.ProjectID, r.ID, w.ID, c.ResultID)
		return ev.Reply{Code: ev.Applied, ID: c.ResultID}, e
	})
}
