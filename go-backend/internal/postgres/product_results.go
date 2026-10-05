package postgres

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	ob "agentevalops/go-backend/internal/observation"
	"context"
	"encoding/json"
)

// ResultPage 只读取最多一页事实；不装载整个 RunState。
func (k ProductReader) ResultPage(ctx context.Context, s asset.Scope, run, after string, n int) ([]ev.EvaluationResult, error) {
	if s.Validate(false) != nil || n < 1 || n > 101 || (run != "" && !asset.ValidID(run)) || (after != "" && !asset.ValidID(after)) {
		return nil, asset.ErrInvalid
	}
	if run != "" {
		var found bool
		e := k.Pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM evaluation_runs r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND r.id=$3)`, s.ProjectID, s.OrganizationID, run).Scan(&found)
		if e != nil {
			return nil, e
		}
		if !found {
			return nil, asset.ErrNotFound
		}
	}
	rows, e := k.Pool.Query(ctx, `SELECT to_jsonb(x),decode(x.metadata->>'ValueBytes','base64') FROM evaluation_results x JOIN projects p ON p.id=x.project_id WHERE x.project_id=$1 AND p.org_id=$2 AND ($3::uuid IS NULL OR x.run_id=$3) AND ($4::uuid IS NULL OR x.id>$4) ORDER BY x.id LIMIT $5`, s.ProjectID, s.OrganizationID, nullableID(run), nullableID(after), n)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []ev.EvaluationResult{}
	for rows.Next() {
		var raw, value []byte
		var v ev.EvaluationResult
		if e = rows.Scan(&raw, &value); e == nil {
			e = json.Unmarshal(raw, &v)
		}
		if e == nil && value != nil {
			e = json.Unmarshal(value, &v.Value)
		}
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

func (k ProductReader) OnlineResults(ctx context.Context, s asset.Scope, after string, n int) ([]ob.Result, error) {
	if s.Validate(false) != nil || n < 1 || n > 101 || after != "" && !asset.ValidID(after) {
		return nil, asset.ErrInvalid
	}
	rows, e := k.Pool.Query(ctx, `SELECT r.result_bytes FROM evaluation_online_results r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND ($3::uuid IS NULL OR r.id>$3) ORDER BY r.id LIMIT $4`, s.ProjectID, s.OrganizationID, nullableID(after), n)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []ob.Result{}
	for rows.Next() {
		var raw []byte
		var v ob.Result
		if e = rows.Scan(&raw); e == nil {
			e = json.Unmarshal(raw, &v)
		}
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

func (k ProductReader) Result(ctx context.Context, s asset.Scope, id string) (ev.EvaluationResult, error) {
	var v ev.EvaluationResult
	if s.Validate(false) != nil || !asset.ValidID(id) {
		return v, asset.ErrInvalid
	}
	var raw, value []byte
	e := k.Pool.QueryRow(ctx, `SELECT to_jsonb(x),decode(x.metadata->>'ValueBytes','base64') FROM evaluation_results x JOIN projects p ON p.id=x.project_id WHERE x.project_id=$1 AND p.org_id=$2 AND x.id=$3`, s.ProjectID, s.OrganizationID, id).Scan(&raw, &value)
	if e != nil {
		return v, dbError(e)
	}
	if e = json.Unmarshal(raw, &v); e == nil && value != nil {
		e = json.Unmarshal(value, &v.Value)
	}
	return v, e
}

func (k ProductReader) GateCases(ctx context.Context, s asset.Scope, id, after string, n int) ([]decision.CaseComparison, error) {
	if s.Validate(false) != nil || !asset.ValidID(id) || len(after) > 512 || n < 1 || n > 101 {
		return nil, asset.ErrInvalid
	}
	var found bool
	e := k.Pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM evaluation_gate_receipts g JOIN projects p ON p.id=g.project_id WHERE g.project_id=$1 AND p.org_id=$2 AND g.gate_id=$3)`, s.ProjectID, s.OrganizationID, id).Scan(&found)
	if e != nil {
		return nil, e
	}
	if !found {
		return nil, asset.ErrNotFound
	}
	rows, e := k.Pool.Query(ctx, `SELECT comparison_bytes FROM evaluation_case_comparisons WHERE project_id=$1 AND gate_id=$2 AND unit_key COLLATE "C">$3 COLLATE "C" ORDER BY unit_key COLLATE "C" LIMIT $4`, s.ProjectID, id, after, n)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []decision.CaseComparison{}
	for rows.Next() {
		var raw []byte
		var v decision.CaseComparison
		if e = rows.Scan(&raw); e == nil {
			e = json.Unmarshal(raw, &v)
		}
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
