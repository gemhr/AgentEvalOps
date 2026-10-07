package postgres

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"encoding/json"
	"time"
)

// ReserveStage13Execution 在短事务内绑定已启动的 Attempt；事务外才发 HTTP。
func (k Evaluation) ReserveStage13Execution(ctx context.Context, s ev.Scope, q ev.Request) (asset.JSON, error) {
	if !s.Execute {
		return asset.JSON{}, asset.ErrInvalid
	}
	tx, err := k.Pool.Begin(ctx)
	if err != nil {
		return asset.JSON{}, err
	}
	defer rollback(ctx, tx)
	if err = lockProject(ctx, tx, s.Scope); err != nil {
		return asset.JSON{}, err
	}
	var started time.Time
	err = tx.QueryRow(ctx, `SELECT started_at FROM evaluation_attempts WHERE project_id=$1 AND run_id=$2 AND id=$3 AND execution_request_id=$4 AND status='RUNNING' AND writer_epoch=$5 AND lease_expires_at>clock_timestamp() FOR UPDATE`, s.ProjectID, q.RunID, q.AttemptID, q.ID, s.Epoch).Scan(&started)
	if err != nil {
		return asset.JSON{}, err
	}
	var cfg struct {
		ExecutionPolicyVersion  string     `json:"execution_policy_version"`
		ExpectedSubjectManifest asset.JSON `json:"expected_subject_manifest"`
	}
	if json.Unmarshal(q.Target.Config.Bytes(), &cfg) != nil || cfg.ExecutionPolicyVersion != citriage.ExecutionPolicyVersion {
		return asset.JSON{}, asset.ErrInvalid
	}
	body, err := citriage.MaterializeExecution(q.Case.Case.Input, cfg.ExpectedSubjectManifest, q.RunID, q.AttemptID, q.Target.TimeoutMilliseconds, started)
	if err != nil {
		return asset.JSON{}, err
	}
	_, err = tx.Exec(ctx, `INSERT INTO evaluation_stage13_execution_requests(project_id,run_id,attempt_id,policy_version,request_bytes,request_digest,case_id,case_version) VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING`, s.ProjectID, q.RunID, q.AttemptID, cfg.ExecutionPolicyVersion, body.Bytes(), body.Digest(), q.Case.Identity.Ref.EntityID, q.Case.Identity.Ref.Version)
	if err != nil {
		return asset.JSON{}, err
	}
	var raw []byte
	var digest string
	err = tx.QueryRow(ctx, `SELECT request_bytes,request_digest FROM evaluation_stage13_execution_requests WHERE project_id=$1 AND run_id=$2 AND attempt_id=$3`, s.ProjectID, q.RunID, q.AttemptID).Scan(&raw, &digest)
	if err != nil {
		return asset.JSON{}, err
	}
	saved, err := asset.ParseJSON(raw)
	if err != nil || saved.Digest() != digest || saved.String() != body.String() {
		return asset.JSON{}, asset.ErrConflict
	}
	return saved, tx.Commit(ctx)
}
