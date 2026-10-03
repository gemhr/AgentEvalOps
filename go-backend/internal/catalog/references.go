package catalog

import (
	"context"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"
)

type CaseReader interface {
	GetCaseVersion(context.Context, asset.Scope, asset.Ref) (CaseVersion, error)
}
type SuiteReferenceReader interface {
	CaseReader
	GetDatasetVersion(context.Context, asset.Scope, asset.Ref) (DatasetVersion, error)
	GetMetricDefinitionVersion(context.Context, asset.Scope, asset.Ref) (metric.DefinitionVersion, error)
	GetEvaluatorDefinitionVersion(context.Context, asset.Scope, asset.Ref) (metric.EvaluatorDefinitionVersion, error)
}

func ValidateMetricBinding(binding EvaluatorBinding, outputs []asset.Ref) error {
	// G1 不支持只执行 evaluator 的部分输出；避免 execution manifest 内产生无定义结果。
	if len(binding.Metrics) != len(outputs) {
		return asset.ErrInvalid
	}
	for i, r := range outputs {
		if binding.Metrics[i] != r {
			return asset.ErrInvalid
		}
	}
	return nil
}
func checkProject(project string, scope asset.Scope) error {
	if project != scope.ProjectID {
		return asset.ErrNotFound
	}
	return nil
}
func validateSuiteReferences(ctx context.Context, reader SuiteReferenceReader, scope asset.Scope, body SuiteContent) error {
	if body.Dataset != nil {
		v, err := reader.GetDatasetVersion(ctx, scope, *body.Dataset)
		if err != nil {
			return err
		}
		if err = checkProject(v.ProjectID(), scope); err != nil {
			return err
		}
	}
	for _, r := range body.Cases {
		v, err := reader.GetCaseVersion(ctx, scope, r)
		if err != nil {
			return err
		}
		if err = checkProject(v.ProjectID(), scope); err != nil {
			return err
		}
	}
	for _, r := range body.Metrics {
		v, err := reader.GetMetricDefinitionVersion(ctx, scope, r)
		if err != nil {
			return err
		}
		if err = checkProject(v.ProjectID(), scope); err != nil {
			return err
		}
	}
	for _, b := range body.Evaluators {
		v, err := reader.GetEvaluatorDefinitionVersion(ctx, scope, b.Evaluator)
		if err != nil {
			return err
		}
		if err = checkProject(v.ProjectID(), scope); err != nil {
			return err
		}
		if err = ValidateMetricBinding(b, v.Content().Body.OutputMetrics); err != nil {
			return err
		}
	}
	return nil
}
