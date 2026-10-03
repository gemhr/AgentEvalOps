package postgres

import (
	"context"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/metric"

	"github.com/jackc/pgx/v5"
)

func publishDatasetRefs(ctx context.Context, tx pgx.Tx, scope asset.Scope, ref asset.Ref, body catalog.DatasetContent) error {
	for i, r := range body.Cases {
		if _, err := loadVersion[catalog.CaseContent](ctx, tx, caseTables, scope, r); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, "INSERT INTO evaluation_dataset_cases(project_id,dataset_id,dataset_version,position,case_id,case_version) VALUES($1,$2,$3,$4,$5,$6)", scope.ProjectID, ref.EntityID, ref.Version, i, r.EntityID, r.Version); err != nil {
			return err
		}
	}
	return nil
}
func publishEvaluatorDefinitionRefs(ctx context.Context, tx pgx.Tx, scope asset.Scope, ref asset.Ref, body metric.EvaluatorDefinition) error {
	for i, r := range body.OutputMetrics {
		if _, err := loadVersion[metric.Definition](ctx, tx, metricTables, scope, r); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, "INSERT INTO evaluation_evaluator_metrics(project_id,evaluator_id,evaluator_version,position,metric_id,metric_version) VALUES($1,$2,$3,$4,$5,$6)", scope.ProjectID, ref.EntityID, ref.Version, i, r.EntityID, r.Version); err != nil {
			return err
		}
	}
	return nil
}
func publishSuiteRefs(ctx context.Context, tx pgx.Tx, scope asset.Scope, ref asset.Ref, body catalog.SuiteContent) error {
	// 事务内再次核对 application 验证过的 refs；FK 仍是最后一道关系约束。
	if body.Dataset != nil {
		if _, err := loadVersion[catalog.DatasetContent](ctx, tx, datasetTables, scope, *body.Dataset); err != nil {
			return err
		}
	}
	for i, r := range body.Cases {
		if _, err := loadVersion[catalog.CaseContent](ctx, tx, caseTables, scope, r); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, "INSERT INTO evaluation_suite_cases(project_id,suite_id,suite_version,position,case_id,case_version) VALUES($1,$2,$3,$4,$5,$6)", scope.ProjectID, ref.EntityID, ref.Version, i, r.EntityID, r.Version); err != nil {
			return err
		}
	}
	for i, r := range body.Metrics {
		if _, err := loadVersion[metric.Definition](ctx, tx, metricTables, scope, r); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, "INSERT INTO evaluation_suite_metrics(project_id,suite_id,suite_version,position,metric_id,metric_version) VALUES($1,$2,$3,$4,$5,$6)", scope.ProjectID, ref.EntityID, ref.Version, i, r.EntityID, r.Version); err != nil {
			return err
		}
	}
	for i, b := range body.Evaluators {
		v, err := loadVersion[metric.EvaluatorDefinition](ctx, tx, evaluatorTables, scope, b.Evaluator)
		if err != nil {
			return err
		}
		if err = catalog.ValidateMetricBinding(b, v.Content().Body.OutputMetrics); err != nil {
			return err
		}
		raw, err := asset.FreezeBytes(b.Applicability)
		if err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, "INSERT INTO evaluation_suite_evaluators(project_id,suite_id,suite_version,position,evaluator_id,evaluator_version,required,applicability) VALUES($1,$2,$3,$4,$5,$6,$7,$8)", scope.ProjectID, ref.EntityID, ref.Version, i, b.Evaluator.EntityID, b.Evaluator.Version, b.Required, raw); err != nil {
			return err
		}
		for j, r := range b.Metrics {
			if _, err = tx.Exec(ctx, "INSERT INTO evaluation_suite_evaluator_metrics(project_id,suite_id,suite_version,evaluator_id,evaluator_version,position,metric_id,metric_version) VALUES($1,$2,$3,$4,$5,$6,$7,$8)", scope.ProjectID, ref.EntityID, ref.Version, b.Evaluator.EntityID, b.Evaluator.Version, j, r.EntityID, r.Version); err != nil {
				return err
			}
		}
	}
	return nil
}
