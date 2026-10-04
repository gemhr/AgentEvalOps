package postgres

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"encoding/json"
	"errors"
	"github.com/jackc/pgx/v5"
)

// LegacySlot 由 barrier 的受控清单提供，不能从当前目录 latest 补齐。
type LegacySlot struct {
	CaseID, CaseVersion string
	Spec                ev.EvaluatorSpec
}
type LegacyImport struct {
	RunID, CutoverID, InventoryRef, CommandID string
	OldWriterRevoked                          bool
	Snapshot                                  ev.RunSnapshot
	Slots                                     []LegacySlot
}

func (k Evaluation) ImportLegacyEvaluationRun(ctx context.Context, s ev.Scope, c LegacyImport) (ev.Reply, error) {
	if !s.Operator || s.Scope.Validate(false) != nil || !asset.ValidID(c.RunID) || !asset.ValidID(c.CutoverID) || !asset.ValidID(c.CommandID) || !asset.Text(c.InventoryRef) || !c.OldWriterRevoked {
		return ev.Reply{Code: ev.Rejected, Reason: "BARRIER_INVENTORY_REQUIRED"}, nil
	}
	digest, e := ev.Intent(c)
	if e != nil {
		return ev.Reply{}, e
	}
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if e != nil {
		return ev.Reply{}, e
	}
	defer rollback(ctx, tx)
	var mode, writer, cutover string
	var epoch int64
	var operator bool
	e = tx.QueryRow(ctx, `SELECT mode,active_writer,writer_epoch,COALESCE(cutover_id::text,''),current_user=pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid='evaluation_writer_control'::regclass)) FROM evaluation_writer_control WHERE domain='evaluation' FOR UPDATE`).Scan(&mode, &writer, &epoch, &cutover, &operator)
	if e != nil {
		return ev.Reply{}, e
	}
	if mode != "BARRIER" || writer != "NONE" || epoch != s.Epoch || cutover != c.CutoverID || !operator {
		return ev.Reply{Code: ev.Rejected, Reason: "OPERATOR_BARRIER_ONLY"}, nil
	}
	if e = lockProject(ctx, tx, s.Scope); e != nil {
		return ev.Reply{}, e
	}
	r, e := getRun(ctx, tx, s, c.RunID, true)
	if e != nil {
		return finishStop(e)
	}
	var existing string
	e = tx.QueryRow(ctx, "SELECT intent_digest FROM evaluation_legacy_import_receipts WHERE project_id=$1 AND run_id=$2 AND cutover_id=$3", s.ProjectID, r.ID, c.CutoverID).Scan(&existing)
	if e == nil {
		if existing != digest {
			return ev.Reply{Code: ev.Conflict, Reason: "IMPORT_INTENT"}, nil
		}
		return ev.Reply{Code: ev.AlreadyApplied, ID: r.ID}, nil
	}
	if !errors.Is(e, pgx.ErrNoRows) {
		return ev.Reply{}, e
	}
	var contract *string
	e = tx.QueryRow(ctx, "SELECT contract_version FROM evaluation_runs WHERE project_id=$1 AND id=$2", s.ProjectID, r.ID).Scan(&contract)
	if e != nil {
		return ev.Reply{}, e
	}
	if contract != nil {
		return ev.Reply{Code: ev.Rejected, Reason: "NOT_LEGACY"}, nil
	}
	if c.Snapshot.Contract != ev.DurableContract || c.Snapshot.Input.ProjectID != s.ProjectID || c.Snapshot.Input.Origin != ev.LegacySnapshot || c.Snapshot.Input.Legacy == nil || c.Snapshot.Input.Legacy.SourceRunID != r.ID || len(c.Snapshot.Input.Manifest) == 0 || len(c.Snapshot.Input.Evaluators) == 0 || c.Snapshot.Retry.Validate() != nil {
		return ev.Reply{Code: ev.IntegrityBlocked, Reason: "RECOVERY_MANIFEST_REQUIRED"}, nil
	}
	// 将清单与真正旧快照核对，既不改旧 Result，也不替旧 Run 创建 Catalog row。
	var storedDataset, storedSuite, storedTarget, storedSubject []byte
	e = tx.QueryRow(ctx, "SELECT dataset_snapshot,suite_snapshot,execution_target_snapshot,COALESCE(subject_ref,'null'::jsonb) FROM evaluation_runs WHERE project_id=$1 AND id=$2", s.ProjectID, r.ID).Scan(&storedDataset, &storedSuite, &storedTarget, &storedSubject)
	if e != nil {
		return ev.Reply{}, e
	}
	l := c.Snapshot.Input.Legacy
	equal := func(b []byte, j asset.JSON) bool {
		x, err := asset.ParseJSON(b)
		return err == nil && x.String() == j.String()
	}
	if !equal(storedDataset, l.DatasetSnapshot) || !equal(storedSuite, l.SuiteSnapshot) || !equal(storedTarget, l.TargetSnapshot) || !equal(storedSubject, l.SubjectSnapshot) {
		return ev.Reply{Code: ev.IntegrityBlocked, Reason: "LEGACY_SNAPSHOT_MISMATCH"}, nil
	}
	// 原始 manifest 的 evaluator identity/digest 必须出现在 frozen suite，不信任新配置。
	var source map[string]json.RawMessage
	if e = json.Unmarshal(storedSuite, &source); e != nil {
		return ev.Reply{}, e
	}
	var original []map[string]any
	if e = json.Unmarshal(source["evaluator_specs"], &original); e != nil {
		return ev.Reply{Code: ev.IntegrityBlocked, Reason: "LEGACY_SPEC_MANIFEST_UNVERIFIABLE"}, nil
	}
	for _, slot := range c.Slots {
		found := false
		for _, old := range original {
			if old["evaluator_id"] == slot.Spec.Identity.Ref.EntityID && old["evaluator_version"] == slot.Spec.Identity.Ref.Version && old["definition_digest"] == slot.Spec.Identity.ContentDigest {
				found = true
			}
		}
		if !found || slot.Spec.Identity.ProjectID != s.ProjectID || slot.Spec.Definition.Validate() != nil {
			return ev.Reply{Code: ev.IntegrityBlocked, Reason: "RECOVERY_SPEC_OR_BUDGET"}, nil
		}
	}
	attempts, _, e := attemptsAndWorks(ctx, tx, s, r.ID)
	if e != nil {
		return ev.Reply{}, e
	}
	cases := map[string]bool{}
	for _, a := range attempts {
		cases[a.CaseID+":"+a.CaseVersion] = true
	}
	if len(c.Snapshot.Input.Manifest) != len(cases) || len(c.Snapshot.Input.Evaluators) != len(original) || len(c.Slots) != len(cases)*len(original) {
		return ev.Reply{Code: ev.IntegrityBlocked, Reason: "RECOVERY_MANIFEST_INCOMPLETE"}, nil
	}
	seenSlots := map[string]bool{}
	for _, slot := range c.Slots {
		key := slot.CaseID + ":" + slot.CaseVersion + ":" + slot.Spec.Identity.Ref.EntityID + ":" + slot.Spec.Identity.Ref.Version
		if seenSlots[key] || !cases[slot.CaseID+":"+slot.CaseVersion] {
			return ev.Reply{Code: ev.IntegrityBlocked, Reason: "RECOVERY_SLOT_CONFLICT"}, nil
		}
		seenSlots[key] = true
		found := false
		for _, spec := range c.Snapshot.Input.Evaluators {
			if bindingDigest(spec) == bindingDigest(slot.Spec) {
				found = true
			}
		}
		if !found {
			return ev.Reply{Code: ev.IntegrityBlocked, Reason: "RECOVERY_SPEC_CONFLICT"}, nil
		}
	}
	for _, m := range c.Snapshot.Input.Manifest {
		if !cases[m.Identity.Ref.EntityID+":"+m.Identity.Ref.Version] {
			return ev.Reply{Code: ev.IntegrityBlocked, Reason: "LEGACY_CASE_MISMATCH"}, nil
		}
		var raw []byte
		e = tx.QueryRow(ctx, "SELECT request_snapshot FROM evaluation_attempts WHERE project_id=$1 AND run_id=$2 AND case_id=$3 AND case_version=$4 ORDER BY attempt_no LIMIT 1", s.ProjectID, r.ID, m.Identity.Ref.EntityID, m.Identity.Ref.Version).Scan(&raw)
		if e != nil {
			return ev.Reply{}, e
		}
		var request struct {
			Input asset.JSON `json:"input_payload"`
			Case  struct {
				Digest string `json:"semantic_digest"`
			} `json:"case_snapshot"`
		}
		if e = json.Unmarshal(raw, &request); e != nil {
			return ev.Reply{}, e
		}
		if request.Case.Digest == "" || request.Case.Digest != m.Identity.ContentDigest || request.Input.String() != m.Case.Input.String() {
			return ev.Reply{Code: ev.IntegrityBlocked, Reason: "LEGACY_INPUT_OR_DIGEST_UNVERIFIABLE"}, nil
		}
	}
	terminalMissing := []string{}
	for _, a := range attempts {
		if a.Status == "CLAIMED" || a.Status == "RUNNING" {
			if r.Status.Terminal() {
				return ev.Reply{Code: ev.IntegrityBlocked, Reason: "TERMINAL_RUN_ACTIVE_ATTEMPT"}, nil
			}
			_, e = tx.Exec(ctx, `UPDATE evaluation_attempts SET status='TERMINAL',execution_outcome_kind='OUTCOME_UNKNOWN',finished_at=clock_timestamp(),output_artifact_ref=NULL,error_category='CUTOVER_BARRIER',reason='old writer revoked; remote state unknown',outcome_metadata=outcome_metadata||$4::jsonb WHERE project_id=$1 AND run_id=$2 AND id=$3`, s.ProjectID, r.ID, a.ID, encode(map[string]any{"cutover_id": c.CutoverID, "source": "barrier-import", "dispatch_certainty": "MAY_HAVE_DISPATCHED", "terminal_certainty": "UNCONFIRMED"}))
			if e != nil {
				return ev.Reply{}, e
			}
			continue
		}
		if a.Outcome != ev.Success {
			continue
		}
		for _, slot := range c.Slots {
			if slot.CaseID != a.CaseID || slot.CaseVersion != a.CaseVersion {
				continue
			}
			var present bool
			e = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM evaluation_evaluator_works WHERE project_id=$1 AND run_id=$2 AND attempt_id=$3 AND evaluator_id=$4 AND evaluator_version=$5)", s.ProjectID, r.ID, a.ID, slot.Spec.Identity.Ref.EntityID, slot.Spec.Identity.Ref.Version).Scan(&present)
			if e != nil {
				return ev.Reply{}, e
			}
			if present {
				continue
			}
			var resultID string
			var metadata []byte
			e = tx.QueryRow(ctx, `SELECT id::text,metadata FROM evaluation_results WHERE project_id=$1 AND run_id=$2 AND attempt_id=$3 AND case_id=$4 AND case_version=$5 AND evaluator_id=$6 AND evaluator_version=$7 AND execution_request_id=$8`, s.ProjectID, r.ID, a.ID, a.CaseID, a.CaseVersion, slot.Spec.Identity.Ref.EntityID, slot.Spec.Identity.Ref.Version, a.RequestID).Scan(&resultID, &metadata)
			status := "PENDING"
			var rid any
			var at any
			if e == nil {
				var meta map[string]any
				if e = json.Unmarshal(metadata, &meta); e != nil {
					return ev.Reply{}, e
				}
				if meta["definition_digest"] != slot.Spec.Identity.ContentDigest {
					return ev.Reply{Code: ev.IntegrityBlocked, Reason: "HISTORICAL_RESULT_PROVENANCE"}, nil
				}
				status = "COMPLETED"
				rid = resultID
				at, _ = dbNow(ctx, tx)
			} else if !errors.Is(e, pgx.ErrNoRows) {
				return ev.Reply{}, e
			} else if r.Status.Terminal() {
				terminalMissing = append(terminalMissing, a.ID+":"+slot.Spec.Identity.Ref.EntityID)
				continue
			}
			meta := ev.WorkMetadata{Spec: slot.Spec, CompletionMode: "NORMAL", RecoveryPolicyRef: c.InventoryRef}
			_, e = tx.Exec(ctx, `INSERT INTO evaluation_evaluator_works(id,project_id,run_id,attempt_id,case_id,case_version,evaluator_id,evaluator_version,spec_digest,spec_version,required,status,result_id,completed_at,metadata,contract_kind) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,'LEGACY')`, asset.NewID(), s.ProjectID, r.ID, a.ID, a.CaseID, a.CaseVersion, slot.Spec.Identity.Ref.EntityID, slot.Spec.Identity.Ref.Version, slot.Spec.Identity.ContentDigest, "legacy-recovery.v1", slot.Spec.Required, status, rid, at, encode(meta))
			if e != nil {
				return ev.Reply{}, e
			}
		}
	}
	frozen, e := asset.Freeze(c.Snapshot)
	if e != nil {
		return ev.Reply{}, e
	}
	data := map[string]any{"SnapshotBytes": frozen.Bytes(), "TerminalMissingSlots": terminalMissing, "CommandID": c.CommandID, "OldWriterRevoked": true}
	_, e = tx.Exec(ctx, "INSERT INTO evaluation_legacy_import_receipts(project_id,run_id,cutover_id,intent_digest,inventory_ref,receipt) VALUES($1,$2,$3,$4,$5,$6)", s.ProjectID, r.ID, c.CutoverID, digest, c.InventoryRef, encode(data))
	if e != nil {
		return ev.Reply{}, e
	}
	if e = tx.Commit(ctx); e != nil {
		return ev.Reply{}, e
	}
	return ev.Reply{Code: ev.Applied, ID: r.ID}, nil
}
