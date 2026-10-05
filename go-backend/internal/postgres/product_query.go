package postgres

import (
	"agentevalops/go-backend/internal/asset"
	"context"
	"github.com/jackc/pgx/v5/pgxpool"
	"time"
)

type ProductReader struct{ Pool *pgxpool.Pool }
type ProductRow struct {
	ID        string    `json:"id"`
	Name      string    `json:"name,omitempty"`
	CreatedAt time.Time `json:"created_at"`
}

// 表名只来自固定资源清单；cursor 值始终参数绑定。
var productTables = map[string]string{
	"cases": "evaluation_cases", "datasets": "evaluation_datasets", "suites": "evaluation_suites",
	"metrics": "evaluation_metric_definitions", "evaluators": "evaluation_evaluator_definitions", "policies": "evaluation_policies",
	"experiments": "evaluation_experiments", "runs": "evaluation_runs", "gates": "evaluation_gate_receipts",
	"rules": "evaluation_online_rules", "reviews": "evaluation_review_items", "calibrations": "evaluation_calibration_reports",
	"drafts": "evaluation_reviewed_case_drafts", "api-keys": "product_api_credentials",
}

func (k ProductReader) List(ctx context.Context, s asset.Scope, kind, after string, limit int) ([]ProductRow, error) {
	table, ok := productTables[kind]
	if !ok || s.Validate(false) != nil || limit < 1 || limit > 101 || after != "" && !asset.ValidID(after) {
		return nil, asset.ErrInvalid
	}
	name := "''"
	id := "r.id"
	if kind == "gates" {
		id = "r.gate_id"
	}
	switch kind {
	case "cases", "datasets", "suites", "metrics", "evaluators", "policies", "experiments", "rules", "api-keys":
		name = "r.name"
	}
	rows, err := k.Pool.Query(ctx, `SELECT `+id+`::text,`+name+`,r.created_at FROM `+table+` r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND ($3::uuid IS NULL OR `+id+`>$3) ORDER BY `+id+` LIMIT $4`, s.ProjectID, s.OrganizationID, nullableID(after), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ProductRow{}
	for rows.Next() {
		var v ProductRow
		if err = rows.Scan(&v.ID, &v.Name, &v.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
func (k ProductReader) Versions(ctx context.Context, s asset.Scope, kind, id, after string, limit int) ([]string, error) {
	table, ok := productTables[kind]
	if !ok || !asset.ValidID(id) || limit < 1 || limit > 101 || len(after) > 255 {
		return nil, asset.ErrInvalid
	}
	switch kind {
	case "cases":
		table = "evaluation_case_versions"
	case "datasets":
		table = "evaluation_dataset_versions"
	case "suites":
		table = "evaluation_suite_versions"
	case "metrics":
		table = "evaluation_metric_definition_versions"
	case "evaluators":
		table = "evaluation_evaluator_definition_versions"
	case "policies":
		table = "evaluation_policy_versions"
	case "rules":
		table = "evaluation_online_rule_versions"
	default:
		return nil, asset.ErrInvalid
	}
	rows, err := k.Pool.Query(ctx, `SELECT v.version FROM `+table+` v JOIN projects p ON p.id=v.project_id WHERE v.project_id=$1 AND p.org_id=$2 AND v.entity_id=$3 AND v.version COLLATE "C">$4 COLLATE "C" ORDER BY v.version COLLATE "C" LIMIT $5`, s.ProjectID, s.OrganizationID, id, after, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var v string
		if err = rows.Scan(&v); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
