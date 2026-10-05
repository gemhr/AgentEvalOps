package postgres

import (
	"agentevalops/go-backend/internal/asset"
	"context"
	"fmt"
	"github.com/jackc/pgx/v5"
	"strings"
)

var productReadTables = []string{
	"alembic_version", "projects", "memberships", "product_project_memberships", "product_api_credentials", "product_api_commands", "product_api_audit",
	"evaluation_writer_control", "evaluation_runs", "evaluation_attempts", "evaluation_evaluator_works", "evaluation_results",
	"evaluation_cases", "evaluation_case_versions", "evaluation_datasets", "evaluation_dataset_versions", "evaluation_dataset_cases",
	"evaluation_suites", "evaluation_suite_versions", "evaluation_suite_cases", "evaluation_suite_metrics", "evaluation_suite_evaluators", "evaluation_suite_evaluator_metrics",
	"evaluation_metric_definitions", "evaluation_metric_definition_versions", "evaluation_evaluator_definitions", "evaluation_evaluator_definition_versions", "evaluation_evaluator_metrics",
	"evaluation_experiments", "evaluation_experiment_runs", "evaluation_policies", "evaluation_policy_versions", "evaluation_comparisons", "evaluation_gate_receipts", "evaluation_case_comparisons",
	"evaluation_observations", "evaluation_online_rules", "evaluation_online_rule_versions", "evaluation_online_bindings", "evaluation_online_project_limits", "evaluation_online_rule_usage", "evaluation_online_admissions", "evaluation_online_works", "evaluation_online_results", "evaluation_online_backfills",
	"evaluation_review_items", "evaluation_review_slots", "evaluation_human_annotations", "evaluation_adjudications", "evaluation_adjudication_inputs", "evaluation_golden_labels", "evaluation_golden_annotation_inputs",
	"evaluation_calibration_snapshots", "evaluation_calibration_samples", "evaluation_calibration_reports", "evaluation_reviewed_case_drafts", "evaluation_review_case_publications", "evaluation_gate_exception_reviews",
}

func (k ProductReader) GetPolicy(ctx context.Context, s asset.Scope, id string) (asset.Logical, error) {
	return getLogical(ctx, k.Pool, tables{"evaluation_policies", "evaluation_policy_versions"}, s, id)
}
func (k ProductIdentity) VerifyAPI(ctx context.Context) (int64, error) {
	epoch, err := (Evaluation{Pool: k.Pool}).VerifyWorker(ctx, ProductSchema)
	if err != nil {
		return 0, fmt.Errorf("API_SCHEMA_WRITER_ROLE_REJECTED")
	}
	var unsafe bool
	err = k.Pool.QueryRow(ctx, `SELECT r.rolcreatedb OR r.rolcreaterole OR r.rolbypassrls OR has_schema_privilege(current_user,'public','CREATE') OR has_table_privilege(current_user,'evaluation_results','UPDATE,DELETE,TRUNCATE') OR has_table_privilege(current_user,'evaluation_evaluator_works','INSERT,UPDATE,DELETE,TRUNCATE') OR has_table_privilege(current_user,'evaluation_writer_control','TRUNCATE,DELETE,INSERT') OR has_table_privilege(current_user,'product_project_memberships','INSERT,UPDATE,DELETE') FROM pg_roles r WHERE r.rolname=current_user`).Scan(&unsafe)
	if err != nil || unsafe {
		return 0, fmt.Errorf("API_ROLE_PRIVILEGES_REJECTED")
	}
	var allowed bool
	err = k.Pool.QueryRow(ctx, `SELECT bool_and(has_table_privilege(current_user,t,'SELECT')) FROM unnest($1::text[]) t`, productReadTables).Scan(&allowed)
	if err != nil || !allowed {
		return 0, fmt.Errorf("API_REQUIRED_TABLE_ACCESS_REJECTED")
	}
	err = k.Pool.QueryRow(ctx, `SELECT has_column_privilege(current_user,'localagent_trace_envelope_sidecars','envelope_id','SELECT') AND has_column_privilege(current_user,'localagent_trace_envelope_sidecars','external_parent_span_id','SELECT') AND has_column_privilege(current_user,'localagent_external_span_identity','external_span_id','SELECT') AND has_column_privilege(current_user,'localagent_external_span_identity','project_id','SELECT') AND has_column_privilege(current_user,'localagent_external_span_identity','external_trace_id','SELECT')`).Scan(&allowed)
	if err != nil || !allowed {
		return 0, fmt.Errorf("API_OBSERVATION_METADATA_ACCESS_REJECTED")
	}
	err = k.Pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p') AND has_table_privilege(current_user,c.oid,'TRUNCATE')) OR has_table_privilege(current_user,'evaluation_online_works','INSERT,UPDATE,DELETE') OR has_table_privilege(current_user,'evaluation_online_results','INSERT,UPDATE,DELETE')`).Scan(&unsafe)
	if err != nil || unsafe {
		return 0, fmt.Errorf("API_WORKER_PRIVILEGES_REJECTED")
	}
	for _, table := range []string{"product_api_audit", "product_api_commands", "product_api_credentials"} {
		err = k.Pool.QueryRow(ctx, `SELECT has_table_privilege(current_user,$1,'INSERT')`, table).Scan(&allowed)
		if err != nil || !allowed {
			return 0, fmt.Errorf("API_COMMAND_ACCESS_REJECTED")
		}
	}
	return epoch, nil
}

// ProductRoleGrants 仅供 offline operator 在创建受限 role 后使用；API 启动不授予权限。
func ProductRoleGrants(role string) string {
	id := pgx.Identifier{role}.Sanitize()
	sql := "GRANT USAGE ON SCHEMA public TO " + id + ";"
	sql += "GRANT SELECT ON " + strings.Join(productReadTables, ",") + " TO " + id + ";"
	sql += "GRANT SELECT(envelope_id,external_parent_span_id) ON localagent_trace_envelope_sidecars TO " + id + ";"
	sql += "GRANT SELECT(external_span_id,project_id,external_trace_id) ON localagent_external_span_identity TO " + id + ";"
	sql += "GRANT UPDATE(id) ON projects,evaluation_runs,evaluation_attempts,evaluation_experiments TO " + id + ";"
	sql += "GRANT UPDATE(domain) ON evaluation_writer_control TO " + id + ";"
	for _, table := range []string{"evaluation_cases", "evaluation_case_versions", "evaluation_datasets", "evaluation_dataset_versions", "evaluation_suites", "evaluation_suite_versions", "evaluation_metric_definitions", "evaluation_metric_definition_versions", "evaluation_evaluator_definitions", "evaluation_evaluator_definition_versions", "evaluation_policies", "evaluation_policy_versions"} {
		sql += "GRANT INSERT,UPDATE ON " + table + " TO " + id + ";"
	}
	for _, table := range []string{"evaluation_dataset_cases", "evaluation_suite_cases", "evaluation_suite_metrics", "evaluation_suite_evaluators", "evaluation_suite_evaluator_metrics", "evaluation_evaluator_metrics", "evaluation_runs", "evaluation_attempts", "evaluation_experiments", "evaluation_comparisons", "evaluation_gate_receipts", "evaluation_case_comparisons", "evaluation_human_annotations", "evaluation_adjudications", "evaluation_golden_labels", "evaluation_calibration_snapshots", "evaluation_calibration_reports", "evaluation_reviewed_case_drafts", "evaluation_review_case_publications", "evaluation_gate_exception_reviews", "product_api_audit"} {
		sql += "GRANT INSERT ON " + table + " TO " + id + ";"
	}
	for _, table := range []string{"evaluation_experiment_runs", "evaluation_online_rules", "evaluation_online_rule_versions", "evaluation_online_bindings", "evaluation_online_project_limits", "evaluation_online_rule_usage", "evaluation_online_backfills", "evaluation_review_items", "evaluation_review_slots", "product_api_credentials", "product_api_commands"} {
		sql += "GRANT INSERT,UPDATE ON " + table + " TO " + id + ";"
	}
	for _, table := range []string{"evaluation_adjudication_inputs", "evaluation_golden_annotation_inputs", "evaluation_calibration_samples"} {
		sql += "GRANT INSERT ON " + table + " TO " + id + ";"
	}
	return sql
}

// TraceIngestRoleGrants 由 offline operator 显式启用 G5 strict ingest 所需权限。
func TraceIngestRoleGrants(role string) string {
	id := pgx.Identifier{role}.Sanitize()
	return "GRANT SELECT,INSERT ON localagent_trace_envelope_sidecars,traces,spans TO " + id + ";" +
		"GRANT SELECT,INSERT,UPDATE ON localagent_external_trace_identity,localagent_external_span_identity TO " + id + ";" +
		"GRANT INSERT ON evaluation_observations TO " + id + ";"
}
