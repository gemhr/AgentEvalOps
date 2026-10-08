package postgres

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/cigovernance"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"time"
)

type Evaluation struct {
	Pool         *pgxpool.Pool
	Capabilities ev.EvaluatorExecutionCapability
	MaxLease     time.Duration
}

var _ ev.Persistence = Evaluation{}

type commandStop struct{ reply ev.Reply }

func (e commandStop) Error() string { return string(e.reply.Code) + ":" + e.reply.Reason }
func stop(code ev.Code, reason string) error {
	return commandStop{ev.Reply{Code: code, Reason: reason}}
}
func encode(v any) []byte {
	b, e := json.Marshal(v)
	if e != nil {
		panic(e)
	}
	return b
}
func loadJSON(ctx context.Context, q queryer, sql string, dst any, args ...any) error {
	var b []byte
	if e := q.QueryRow(ctx, sql, args...).Scan(&b); e != nil {
		if errors.Is(e, pgx.ErrNoRows) {
			return stop(ev.NotFound, "")
		}
		return e
	}
	return json.Unmarshal(b, dst)
}
func getRun(ctx context.Context, q queryer, s ev.Scope, id string, lock bool) (ev.Run, error) {
	var r ev.Run
	var raw, canonical []byte
	sql := "SELECT to_jsonb(r),kernel_bytes FROM evaluation_runs r WHERE project_id=$1 AND id=$2"
	if lock {
		sql += " FOR UPDATE"
	}
	err := q.QueryRow(ctx, sql, s.ProjectID, id).Scan(&raw, &canonical)
	if errors.Is(err, pgx.ErrNoRows) {
		return r, stop(ev.NotFound, "")
	}
	if err != nil {
		return r, err
	}
	if err = json.Unmarshal(raw, &r); err != nil {
		return r, err
	}
	// JSONB 只作查询投影；执行始终使用冻结 bytea，保留数值类型和 identity。
	if canonical == nil {
		var saved []byte
		err = q.QueryRow(ctx, "SELECT decode(receipt->>'SnapshotBytes','base64') FROM evaluation_legacy_import_receipts WHERE project_id=$1 AND run_id=$2 ORDER BY created_at DESC LIMIT 1", s.ProjectID, id).Scan(&saved)
		if err == nil {
			canonical = saved
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return r, err
		}
	}
	if canonical != nil {
		if err = json.Unmarshal(canonical, &r.Snapshot); err != nil {
			return r, err
		}
	}
	return r, nil
}
func getAttempt(ctx context.Context, q queryer, s ev.Scope, o ev.Owned) (ev.Attempt, error) {
	var a ev.Attempt
	e := loadJSON(ctx, q, "SELECT to_jsonb(a) FROM evaluation_attempts a WHERE project_id=$1 AND run_id=$2 AND id=$3 FOR UPDATE", &a, s.ProjectID, o.RunID, o.AttemptID)
	if e == nil && a.Metadata.ObservationBytes != nil {
		e = json.Unmarshal(a.Metadata.ObservationBytes, &a.Metadata.Observation)
	}
	if e == nil {
		r, err := getRun(ctx, q, s, o.RunID, false)
		if err != nil {
			return a, err
		}
		for _, c := range r.Snapshot.Input.Manifest {
			if c.Identity.Ref.EntityID == a.CaseID && c.Identity.Ref.Version == a.CaseVersion {
				a.Request = ev.Request{ID: a.RequestID, IdempotencyKey: a.Key, Case: c, Target: r.Snapshot.Target, RunID: r.ID, AttemptID: a.ID}
			}
		}
		if a.Outcome == ev.Success && a.Metadata.Observation.Artifact == nil {
			var raw []byte
			err = q.QueryRow(ctx, "SELECT output_artifact_ref FROM evaluation_attempts WHERE project_id=$1 AND run_id=$2 AND id=$3", s.ProjectID, r.ID, a.ID).Scan(&raw)
			if err != nil {
				return a, err
			}
			var old map[string]any
			if err = json.Unmarshal(raw, &old); err != nil {
				return a, err
			}
			ref, _ := old["artifact_id"].(string)
			if ref == "" {
				return a, stop(ev.IntegrityBlocked, "LEGACY_ARTIFACT_UNVERIFIABLE")
			}
			a.Metadata.Observation.Artifact = &ev.Binding{ProjectID: s.ProjectID, RunID: r.ID, AttemptID: a.ID, RequestID: a.RequestID, Ref: ref, Schema: "legacy-artifact.v1", Availability: "UNAVAILABLE"}
		}
	}
	return a, e
}
func getWork(ctx context.Context, q queryer, s ev.Scope, o ev.Owned) (ev.Work, error) {
	var w ev.Work
	e := loadJSON(ctx, q, "SELECT to_jsonb(w) FROM evaluation_evaluator_works w WHERE project_id=$1 AND run_id=$2 AND attempt_id=$3 AND id=$4 FOR UPDATE", &w, s.ProjectID, o.RunID, o.AttemptID, o.WorkID)
	if e == nil {
		e = restoreCalls(&w)
	}
	return w, e
}
func restoreCalls(w *ev.Work) error {
	for i := range w.Calls {
		c := &w.Calls[i]
		if c.ResponseBytes != nil {
			j, e := asset.ParseJSON(c.ResponseBytes)
			if e != nil {
				return e
			}
			c.Response = j
		}
		if c.DraftBytes != nil {
			var v ev.ResultValue
			if e := json.Unmarshal(c.DraftBytes, &v); e != nil {
				return e
			}
			c.Draft = &v
		}
	}
	return nil
}
func (k Evaluation) transact(ctx context.Context, s ev.Scope, run string, admission bool, fn func(pgx.Tx, ev.Run, string) (ev.Reply, error)) (ev.Reply, error) {
	if s.Scope.Validate(false) != nil || s.Epoch < 1 {
		return ev.Reply{Code: ev.Rejected, Reason: "INVALID_SCOPE"}, nil
	}
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if e != nil {
		return ev.Reply{}, e
	}
	defer rollback(ctx, tx)
	var mode, writer, contract, schema string
	var epoch int64
	e = tx.QueryRow(ctx, "SELECT mode,active_writer,writer_epoch,contract_version,required_schema_version FROM evaluation_writer_control WHERE domain='evaluation' FOR SHARE").Scan(&mode, &writer, &epoch, &contract, &schema)
	if e != nil {
		return ev.Reply{}, e
	}
	if epoch != s.Epoch {
		return ev.Reply{Code: ev.OwnershipLost, Reason: "WRITER_EPOCH_MISMATCH"}, nil
	}
	if contract != ev.DurableContract || schema != ev.SchemaVersion || writer != "GO" || (mode != "GO_ACTIVE" && mode != "GO_DRAINING") || (admission && mode != "GO_ACTIVE") {
		return ev.Reply{Code: ev.Rejected, Reason: "WRITER_NOT_ACTIVE"}, nil
	}
	if e = lockProject(ctx, tx, s.Scope); e != nil {
		if errors.Is(e, asset.ErrNotFound) {
			return ev.Reply{Code: ev.NotFound}, nil
		}
		return ev.Reply{}, e
	}
	var r ev.Run
	if run != "" {
		r, e = getRun(ctx, tx, s, run, true)
		if e != nil {
			return finishStop(e)
		}
		if r.Snapshot.Contract != ev.DurableContract {
			return ev.Reply{Code: ev.Rejected, Reason: "LEGACY_REQUIRES_IMPORT"}, nil
		}
	}
	reply, e := fn(tx, r, mode)
	if e != nil {
		return finishStop(e)
	}
	if e = tx.Commit(ctx); e != nil {
		return ev.Reply{}, fmt.Errorf("COMMIT_UNKNOWN or rejected commit; reread exact receipt: %w", e)
	}
	return reply, nil
}
func finishStop(e error) (ev.Reply, error) {
	var s commandStop
	if errors.As(e, &s) {
		return s.reply, nil
	}
	return ev.Reply{}, e
}
func activeRun(r ev.Run) error {
	if r.Status.Terminal() {
		return stop(ev.Rejected, "RUN_SEALED")
	}
	if r.Status != ev.RunRunning {
		return stop(ev.NotReady, "RUN_NOT_RUNNING")
	}
	return nil
}
func dbNow(ctx context.Context, tx pgx.Tx) (time.Time, error) {
	var now time.Time
	e := tx.QueryRow(ctx, "SELECT clock_timestamp()").Scan(&now)
	return now, e
}
func owned(ctx context.Context, tx pgx.Tx, token *string, epoch *int64, lease *time.Time, s ev.Scope, o ev.Owned, active bool) error {
	now, e := dbNow(ctx, tx)
	if e != nil {
		return e
	}
	if !active || token == nil || *token != o.Token || epoch == nil || *epoch != s.Epoch || lease == nil || !lease.After(now) {
		return stop(ev.OwnershipLost, "")
	}
	return nil
}

// 即使校验与写入之间经过其他本地查询，最终 mutation 仍重新用 DB clock fencing。
func fencedExec(ctx context.Context, tx pgx.Tx, sql string, s ev.Scope, o ev.Owned, args ...any) error {
	n := len(args)
	sql += fmt.Sprintf(" AND claim_token=$%d AND writer_epoch=$%d AND lease_expires_at>clock_timestamp() AND status IN ('CLAIMED','RUNNING')", n+1, n+2)
	args = append(args, o.Token, s.Epoch)
	tag, err := tx.Exec(ctx, sql, args...)
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 {
		return stop(ev.OwnershipLost, "")
	}
	return nil
}
func (k Evaluation) lease(d time.Duration) error {
	max := k.MaxLease
	if max == 0 {
		max = 5 * time.Minute
	}
	if d < time.Millisecond || d > max {
		return stop(ev.Rejected, "INVALID_LEASE")
	}
	return nil
}
func receipt(id, digest string, s ev.Scope, o ev.Owned) ev.Receipt {
	return ev.Receipt{CommandID: id, Digest: digest, Token: o.Token, Epoch: s.Epoch}
}
func compareReceipt(old, want ev.Receipt) error {
	if old != want {
		return stop(ev.Conflict, "INTENT_CONFLICT")
	}
	return nil
}
func identity(r ev.Run) (string, string, string, string) {
	d, dv, u, uv := "explicit-cases", "v1", "explicit-evaluators", "v1"
	if r.Snapshot.Input.Dataset != nil {
		d = r.Snapshot.Input.Dataset.Ref.EntityID
		dv = r.Snapshot.Input.Dataset.Ref.Version
	}
	if r.Snapshot.Input.Suite != nil {
		u = r.Snapshot.Input.Suite.Ref.EntityID
		uv = r.Snapshot.Input.Suite.Ref.Version
	}
	return d, dv, u, uv
}
func insertAttempt(ctx context.Context, tx pgx.Tx, s ev.Scope, r ev.Run, c ev.CaseInput, no int, parent *string, intent *string, command *string) (string, error) {
	id, q, key := asset.NewID(), asset.NewID(), asset.NewID()
	request := ev.Request{ID: q, IdempotencyKey: key, Case: c, Target: r.Snapshot.Target, RunID: r.ID, AttemptID: id}
	var catalogID any
	if r.Snapshot.Input.Origin == ev.PublishedCatalog {
		catalogID = c.Identity.Ref.EntityID
	}
	_, e := tx.Exec(ctx, `INSERT INTO evaluation_attempts(id,project_id,run_id,case_id,case_version,catalog_case_id,attempt_no,retry_of_attempt_id,execution_target_id,execution_target_kind,target_version_kind,target_version_value,execution_request_id,idempotency_key,request_snapshot,status,contract_version,retry_intent_digest,retry_command_id,input_origin) VALUES($1,$2,$3,$4::text,$5,$17::uuid,$6,$7,$8,$9,'version',$10,$11,$12,$13,'PENDING',$14,$15,$16,$18)`, id, s.ProjectID, r.ID, c.Identity.Ref.EntityID, c.Identity.Ref.Version, no, parent, r.Snapshot.Target.ID, r.Snapshot.Target.Kind, r.Snapshot.Target.Version, q, key, encode(request), ev.DurableContract, intent, command, catalogID, r.Snapshot.Input.Origin)
	return id, e
}
func insertRun(ctx context.Context, tx pgx.Tx, s ev.Scope, snapshot ev.RunSnapshot, command, digest string, source *string, reason string, meta any) (ev.Reply, error) {
	var old ev.Run
	e := loadJSON(ctx, tx, "SELECT to_jsonb(r) FROM evaluation_runs r WHERE project_id=$1 AND creation_command_id=$2", &old, s.ProjectID, command)
	if e == nil {
		if old.Intent != digest {
			return ev.Reply{}, stop(ev.Conflict, "COMMAND_CONFLICT")
		}
		return ev.Reply{Code: ev.AlreadyApplied, ID: old.ID}, nil
	}
	var cs commandStop
	if !errors.As(e, &cs) || cs.reply.Code != ev.NotFound {
		return ev.Reply{}, e
	}
	r := ev.Run{ID: asset.NewID(), ProjectID: s.ProjectID, Snapshot: snapshot}
	d, dv, u, uv := identity(r)
	frozen, e := asset.Freeze(snapshot)
	if e != nil {
		return ev.Reply{}, e
	}
	_, e = tx.Exec(ctx, `INSERT INTO evaluation_runs(id,project_id,dataset_id,dataset_version,suite_id,suite_version,execution_target_id,execution_target_kind,target_version_kind,target_version_value,dataset_snapshot,suite_snapshot,execution_target_snapshot,subject_ref,status,contract_version,creation_command_id,creation_intent_digest,source_run_id,rerun_reason,kernel_snapshot,kernel_bytes,writer_epoch,metadata) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'version',$9,$10,$11,$12,$13,'PENDING',$14,$15,$16,$17,$18,$19,$20,$21,$22)`, r.ID, s.ProjectID, d, dv, u, uv, snapshot.Target.ID, snapshot.Target.Kind, snapshot.Target.Version, snapshot.InputBytes, encode(snapshot.Input), encode(snapshot.Target), snapshot.Subject.Bytes(), ev.DurableContract, command, digest, source, reason, frozen.Bytes(), frozen.Bytes(), s.Epoch, encode(meta))
	if e != nil {
		return ev.Reply{}, e
	}
	for _, c := range snapshot.Input.Manifest {
		if _, e = insertAttempt(ctx, tx, s, r, c, 1, nil, nil, nil); e != nil {
			return ev.Reply{}, e
		}
	}
	return ev.Reply{Code: ev.Applied, ID: r.ID, Status: "PENDING"}, nil
}
func (k Evaluation) CreateRun(ctx context.Context, s ev.Scope, c ev.CreateRun) (ev.Reply, error) {
	if !s.Create {
		return ev.Reply{Code: ev.Rejected, Reason: "FORBIDDEN"}, nil
	}
	snap, e := ev.FreezeRun(c, s.ProjectID, k.Capabilities)
	if e != nil {
		return ev.Reply{Code: ev.Rejected, Reason: e.Error()}, nil
	}
	metadata := map[string]any{"Actor": s.Principal}
	if snap.Intent == "RELEASE_EVALUATION" {
		manifest, err := cigovernance.ReleaseSubject(snap.Target.Config)
		if err != nil || snap.Target.ID != "localagent-ci-triage-http" {
			return ev.Reply{Code: ev.Rejected, Reason: "RELEASE_SUBJECT_INVALID"}, nil
		}
		// 复用原 Run 的原子幂等命令；换 command ID 不能重抽同一主体/完整 Holdout。
		c.CommandID = cigovernance.ID("stage13.holdout-consumption.v1", s.ProjectID, snap.Input.Dataset.Ref.EntityID, snap.Input.Dataset.Ref.Version, manifest.ID, manifest.Version)
		metadata["holdout_consumption"] = map[string]any{"version": "stage13.holdout-consumption.v1", "state": "CONSUMED", "candidate_subject_manifest_digest": manifest.Digest, "consumption_reason": "FIRST_FORMAL_RELEASE_EVALUATION_CREATED", "consumed_at_source": "evaluation_runs.created_at", "gate_source": "evaluation_gate_receipts joined through evaluation_comparisons"}
	}
	digest, e := ev.Intent(struct {
		Snapshot ev.RunSnapshot
		Actor    string
	}{snap, s.Principal})
	if e != nil {
		return ev.Reply{}, e
	}
	return k.transact(ctx, s, "", true, func(tx pgx.Tx, _ ev.Run, _ string) (ev.Reply, error) {
		refs := []struct {
			table string
			id    ev.AssetIdentity
		}{}
		for _, v := range snap.Input.Manifest {
			refs = append(refs, struct {
				table string
				id    ev.AssetIdentity
			}{"evaluation_case_versions", v.Identity})
		}
		for _, v := range snap.Input.Evaluators {
			refs = append(refs, struct {
				table string
				id    ev.AssetIdentity
			}{"evaluation_evaluator_definition_versions", v.Identity})
		}
		for _, v := range snap.Input.Metrics {
			refs = append(refs, struct {
				table string
				id    ev.AssetIdentity
			}{"evaluation_metric_definition_versions", v.Identity})
		}
		if snap.Input.Dataset != nil {
			refs = append(refs, struct {
				table string
				id    ev.AssetIdentity
			}{"evaluation_dataset_versions", *snap.Input.Dataset})
		}
		if snap.Input.Suite != nil {
			refs = append(refs, struct {
				table string
				id    ev.AssetIdentity
			}{"evaluation_suite_versions", *snap.Input.Suite})
		}
		for _, v := range refs {
			var raw []byte
			var digest, algorithm string
			err := tx.QueryRow(ctx, "SELECT canonical_bytes,content_digest,algorithm_ref FROM "+v.table+" WHERE project_id=$1 AND entity_id=$2 AND version=$3 AND sealed", s.ProjectID, v.id.Ref.EntityID, v.id.Ref.Version).Scan(&raw, &digest, &algorithm)
			if errors.Is(err, pgx.ErrNoRows) {
				return ev.Reply{}, stop(ev.NotFound, "PINNED_VERSION")
			}
			if err != nil {
				return ev.Reply{}, err
			}
			if digest != v.id.ContentDigest || algorithm != v.id.Algorithm || string(raw) != v.id.CanonicalContent.String() {
				return ev.Reply{}, stop(ev.IntegrityBlocked, "PINNED_IDENTITY_MISMATCH")
			}
		}
		return insertRun(ctx, tx, s, snap, c.CommandID, digest, nil, "", metadata)
	})
}
func (k Evaluation) ClaimExecutionAttempt(ctx context.Context, s ev.Scope, o ev.Owned, owner string, d time.Duration) (ev.Reply, error) {
	if !s.Execute || !asset.Text(owner) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	if e := k.lease(d); e != nil {
		return finishStop(e)
	}
	return k.transact(ctx, s, o.RunID, true, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		if r.Status.Terminal() {
			return ev.Reply{}, stop(ev.Rejected, "RUN_SEALED")
		}
		a, e := getAttempt(ctx, tx, s, o)
		if e != nil {
			return ev.Reply{}, e
		}
		if a.Status != "PENDING" || a.Token != nil {
			return ev.Reply{Code: ev.NotClaimed}, nil
		}
		token := asset.NewID()
		var expiry time.Time
		e = tx.QueryRow(ctx, `UPDATE evaluation_attempts SET status='CLAIMED',claim_token=$4,writer_epoch=$5,worker_ref=$6,claimed_at=clock_timestamp(),lease_expires_at=clock_timestamp()+$7*interval '1 millisecond' WHERE project_id=$1 AND run_id=$2 AND id=$3 AND status='PENDING' AND claim_token IS NULL RETURNING lease_expires_at`, s.ProjectID, r.ID, a.ID, token, s.Epoch, owner, d.Milliseconds()).Scan(&expiry)
		if e != nil {
			return ev.Reply{}, e
		}
		if r.Status == ev.RunPending {
			_, e = tx.Exec(ctx, "UPDATE evaluation_runs SET status='RUNNING',started_at=clock_timestamp() WHERE project_id=$1 AND id=$2", s.ProjectID, r.ID)
		}
		return ev.Reply{Code: ev.Applied, ID: a.ID, Token: token, Lease: &expiry}, e
	})
}
func (k Evaluation) StartExecutionAttempt(ctx context.Context, s ev.Scope, o ev.Owned) (ev.Reply, error) {
	if !s.Execute {
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
		if e = owned(ctx, tx, a.Token, a.Epoch, a.Lease, s, o, a.Status == "CLAIMED" || a.Status == "RUNNING"); e != nil {
			return ev.Reply{}, e
		}
		if a.Status == "RUNNING" {
			return ev.Reply{Code: ev.AlreadyApplied, ID: a.ID}, nil
		}
		e = fencedExec(ctx, tx, "UPDATE evaluation_attempts SET status='RUNNING',started_at=clock_timestamp() WHERE project_id=$1 AND run_id=$2 AND id=$3", s, o, s.ProjectID, r.ID, a.ID)
		return ev.Reply{Code: ev.Applied, ID: a.ID}, e
	})
}
func (k Evaluation) RenewExecutionLease(ctx context.Context, s ev.Scope, o ev.Owned, d time.Duration) (ev.Reply, error) {
	if !s.Execute {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	if e := k.lease(d); e != nil {
		return finishStop(e)
	}
	return k.transact(ctx, s, o.RunID, false, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		if e := activeRun(r); e != nil {
			return ev.Reply{}, e
		}
		a, e := getAttempt(ctx, tx, s, o)
		if e != nil {
			return ev.Reply{}, e
		}
		if e = owned(ctx, tx, a.Token, a.Epoch, a.Lease, s, o, a.Status == "CLAIMED" || a.Status == "RUNNING"); e != nil {
			return ev.Reply{}, e
		}
		var lease time.Time
		e = tx.QueryRow(ctx, "UPDATE evaluation_attempts SET lease_expires_at=clock_timestamp()+$4*interval '1 millisecond' WHERE project_id=$1 AND run_id=$2 AND id=$3 AND claim_token=$5 AND writer_epoch=$6 AND lease_expires_at>clock_timestamp() AND status IN ('CLAIMED','RUNNING') RETURNING lease_expires_at", s.ProjectID, r.ID, a.ID, d.Milliseconds(), o.Token, s.Epoch).Scan(&lease)
		if errors.Is(e, pgx.ErrNoRows) {
			return ev.Reply{}, stop(ev.OwnershipLost, "")
		}
		return ev.Reply{Code: ev.Applied, Lease: &lease}, e
	})
}
func insertSlots(ctx context.Context, tx pgx.Tx, s ev.Scope, r ev.Run, a ev.Attempt) error {
	for _, spec := range r.Snapshot.Input.Evaluators {
		meta := ev.WorkMetadata{Spec: spec, CompletionMode: "NORMAL"}
		_, e := tx.Exec(ctx, `INSERT INTO evaluation_evaluator_works(id,project_id,run_id,attempt_id,case_id,case_version,evaluator_id,evaluator_version,spec_digest,spec_version,required,status,metadata,contract_kind) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'PENDING',$12,'STAGE12_CANONICAL')`, asset.NewID(), s.ProjectID, r.ID, a.ID, a.CaseID, a.CaseVersion, spec.Identity.Ref.EntityID, spec.Identity.Ref.Version, spec.Identity.ContentDigest, ev.SnapshotContract, spec.Required, encode(meta))
		if e != nil {
			return e
		}
	}
	return nil
}
func (k Evaluation) FinalizeExecutionOutcome(ctx context.Context, s ev.Scope, c ev.FinalizeOutcome) (ev.Reply, error) {
	if !s.Execute || !asset.ValidID(c.CommandID) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	digest, e := ev.Intent(c.Outcome)
	if e != nil {
		return ev.Reply{}, e
	}
	want := receipt(c.CommandID, digest, s, c.Owned)
	return k.transact(ctx, s, c.RunID, false, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		a, e := getAttempt(ctx, tx, s, c.Owned)
		if e != nil {
			return ev.Reply{}, e
		}
		if a.Status == "TERMINAL" {
			if a.Metadata.Receipt.CommandID == c.CommandID {
				if e = compareReceipt(a.Metadata.Receipt, want); e != nil {
					return ev.Reply{}, e
				}
				return ev.Reply{Code: ev.AlreadyApplied, ID: a.ID}, nil
			}
			return ev.Reply{}, stop(ev.OwnershipLost, "TERMINAL")
		}
		if e = activeRun(r); e != nil {
			return ev.Reply{}, e
		}
		if e = owned(ctx, tx, a.Token, a.Epoch, a.Lease, s, c.Owned, a.Status == "RUNNING"); e != nil {
			return ev.Reply{}, e
		}
		if e = c.Outcome.Validate(s.ProjectID, r.ID, a.ID, a.RequestID); e != nil {
			return ev.Reply{}, stop(ev.Rejected, "INVALID_OUTCOME")
		}
		var artifact any
		var category, reason any
		if c.Outcome.Artifact != nil {
			artifact = encode(c.Outcome.Artifact)
		}
		if c.Outcome.Kind != ev.Success {
			category = c.Outcome.ErrorCategory
			reason = c.Outcome.Reason
		}
		evidence := c.Outcome.Evidence
		if evidence == nil {
			evidence = []ev.Binding{}
		}
		observation, err := asset.Freeze(c.Outcome)
		if err != nil {
			return ev.Reply{}, err
		}
		e = fencedExec(ctx, tx, `UPDATE evaluation_attempts SET status='TERMINAL',finished_at=clock_timestamp(),execution_outcome_kind=$4,output_artifact_ref=$5,outcome_evidence_refs=$6,error_category=$7,reason=$8,outcome_metadata=$9 WHERE project_id=$1 AND run_id=$2 AND id=$3`, s, c.Owned, s.ProjectID, r.ID, a.ID, c.Outcome.Kind, artifact, encode(evidence), category, reason, encode(ev.AttemptMetadata{Receipt: want, Observation: c.Outcome, ObservationBytes: observation.Bytes()}))
		if e != nil {
			return ev.Reply{}, e
		}
		if c.Outcome.Kind == ev.Success {
			e = insertSlots(ctx, tx, s, r, a)
		}
		return ev.Reply{Code: ev.Applied, ID: a.ID}, e
	})
}
func (k Evaluation) ExpireExecutionAttempt(ctx context.Context, s ev.Scope, o ev.Owned) (ev.Reply, error) {
	if !s.Coordinate {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.transact(ctx, s, o.RunID, false, func(tx pgx.Tx, r ev.Run, _ string) (ev.Reply, error) {
		a, e := getAttempt(ctx, tx, s, o)
		if e != nil {
			return ev.Reply{}, e
		}
		if a.Status == "TERMINAL" {
			return ev.Reply{Code: ev.AlreadyApplied}, nil
		}
		if e = activeRun(r); e != nil {
			return ev.Reply{}, e
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return ev.Reply{}, e
		}
		if a.Lease == nil || a.Lease.After(now) {
			return ev.Reply{Code: ev.NotReady}, nil
		}
		out := ev.Outcome{Kind: ev.Unknown, RequestID: a.RequestID, Source: "lease-scanner", DispatchCertainty: "MAY_HAVE_DISPATCHED", TerminalCertainty: "UNCONFIRMED", ErrorCategory: "STALE_EXECUTION", Reason: "DB lease expired"}
		_, e = tx.Exec(ctx, `UPDATE evaluation_attempts SET status='TERMINAL',finished_at=clock_timestamp(),execution_outcome_kind='OUTCOME_UNKNOWN',error_category='STALE_EXECUTION',reason='DB lease expired',outcome_metadata=$4 WHERE project_id=$1 AND run_id=$2 AND id=$3`, s.ProjectID, r.ID, a.ID, encode(ev.AttemptMetadata{Observation: out}))
		return ev.Reply{Code: ev.Applied}, e
	})
}
