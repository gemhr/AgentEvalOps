package postgres

import (
	"context"
	"fmt"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
)

// VerifyWorker 只验证，不迁移、不改变 writer control，也不接管旧 Python 进程。
func (k Evaluation) VerifyWorker(ctx context.Context, schema string) (int64, error) {
	var actual, mode, writer, contract, required string
	var epoch int64
	var super bool
	err := k.Pool.QueryRow(ctx, `SELECT v.version_num,c.mode,c.active_writer,c.writer_epoch,c.contract_version,c.required_schema_version,r.rolsuper FROM alembic_version v CROSS JOIN evaluation_writer_control c JOIN pg_roles r ON r.rolname=current_user WHERE c.domain='evaluation'`).Scan(&actual, &mode, &writer, &epoch, &contract, &required, &super)
	if err != nil {
		return 0, err
	}
	if actual != schema || mode != "GO_ACTIVE" || writer != "GO" || contract != ev.DurableContract || required != ev.SchemaVersion || super {
		return 0, fmt.Errorf("WORKER_BOOTSTRAP_REJECTED: schema/writer/runtime role mismatch")
	}
	return epoch, nil
}

// system Worker 根据 candidate 的真实 Project 建立 scope，worker_id 只作审计。
func (k Evaluation) WorkerScope(ctx context.Context, project, principal string, epoch int64) (ev.Scope, error) {
	if !asset.ValidID(project) || !asset.Text(principal) || epoch < 1 {
		return ev.Scope{}, asset.ErrInvalid
	}
	var org string
	if err := k.Pool.QueryRow(ctx, "SELECT org_id::text FROM projects WHERE id=$1", project).Scan(&org); err != nil {
		return ev.Scope{}, dbError(err)
	}
	return ev.Scope{Scope: asset.Scope{ProjectID: project, OrganizationID: org, Principal: principal}, Epoch: epoch, Execute: true, Evaluate: true, Coordinate: true, Repair: true}, nil
}

// 装配边界只授予 binary 确认支持的 implementation/version/digest 绑定。
func (k Evaluation) LoadEvaluatorCapabilities(ctx context.Context, supports func(metric.EvaluatorDefinition) bool) (ev.EvaluatorExecutionCapability, error) {
	rows, err := k.Pool.Query(ctx, "SELECT entity_id::text,version,content_digest,canonical_bytes FROM evaluation_evaluator_definition_versions WHERE sealed ORDER BY project_id,entity_id,version")
	if err != nil {
		return ev.EvaluatorExecutionCapability{}, err
	}
	defer rows.Close()
	caps := ev.EvaluatorExecutionCapability{}
	for rows.Next() {
		var id, version, digest string
		var raw []byte
		if err = rows.Scan(&id, &version, &digest, &raw); err != nil {
			return caps, err
		}
		var content asset.Content[metric.EvaluatorDefinition]
		j, e := asset.ParseJSON(raw)
		if e != nil {
			return caps, e
		}
		if e = j.Decode(&content); e != nil {
			return caps, e
		}
		if supports(content.Body) {
			caps.Bindings = append(caps.Bindings, ev.Capability{Evaluator: asset.Ref{EntityID: id, Version: version}, ImplementationRef: content.Body.ImplementationRef, DefinitionDigest: digest})
		}
	}
	return caps, rows.Err()
}
