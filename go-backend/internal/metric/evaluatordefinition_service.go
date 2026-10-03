package metric

import (
	"context"

	"agentevalops/go-backend/internal/asset"
)

// EvaluatorDefinitionStore 是应用消费的命令/查询端口；每个 Publish 必须在一个短事务中完成。
type EvaluatorDefinitionStore interface {
	CreateEvaluatorDefinition(context.Context, asset.Scope, asset.Create) (asset.Logical, error)
	PublishEvaluatorDefinitionVersion(context.Context, asset.Scope, asset.Publish[EvaluatorDefinition]) (EvaluatorDefinitionVersion, error)
	GetEvaluatorDefinition(context.Context, asset.Scope, string) (asset.Logical, error)
	ListEvaluatorDefinitions(context.Context, asset.Scope, int) ([]asset.Logical, error)
	GetEvaluatorDefinitionVersion(context.Context, asset.Scope, asset.Ref) (EvaluatorDefinitionVersion, error)
	ListEvaluatorDefinitionVersions(context.Context, asset.Scope, string, int) ([]EvaluatorDefinitionVersion, error)
}

type DefinitionReader interface {
	GetMetricDefinitionVersion(context.Context, asset.Scope, asset.Ref) (DefinitionVersion, error)
}
type EvaluatorDefinitionService struct {
	Store   EvaluatorDefinitionStore
	Metrics DefinitionReader
}

func (s EvaluatorDefinitionService) CreateEvaluatorDefinition(ctx context.Context, scope asset.Scope, cmd asset.Create) (asset.Logical, error) {
	if err := scope.Validate(true); err != nil {
		return asset.Logical{}, err
	}
	if err := cmd.Validate(); err != nil {
		return asset.Logical{}, err
	}
	return s.Store.CreateEvaluatorDefinition(ctx, scope, cmd)
}
func (s EvaluatorDefinitionService) PublishEvaluatorDefinitionVersion(ctx context.Context, scope asset.Scope, cmd asset.Publish[EvaluatorDefinition]) (EvaluatorDefinitionVersion, error) {
	if err := scope.Validate(true); err != nil {
		return EvaluatorDefinitionVersion{}, err
	}
	if err := cmd.Ref.Validate(); err != nil {
		return EvaluatorDefinitionVersion{}, err
	}
	if err := cmd.Source.Validate(); err != nil {
		return EvaluatorDefinitionVersion{}, err
	}
	if err := cmd.Body.Validate(); err != nil {
		return EvaluatorDefinitionVersion{}, err
	}
	for _, ref := range cmd.Body.OutputMetrics {
		v, err := s.Metrics.GetMetricDefinitionVersion(ctx, scope, ref)
		if err != nil {
			return EvaluatorDefinitionVersion{}, err
		}
		if v.ProjectID() != scope.ProjectID {
			return EvaluatorDefinitionVersion{}, asset.ErrNotFound
		}
	}
	return s.Store.PublishEvaluatorDefinitionVersion(ctx, scope, cmd)
}
func (s EvaluatorDefinitionService) GetEvaluatorDefinition(ctx context.Context, scope asset.Scope, id string) (asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return asset.Logical{}, err
	}
	if !asset.ValidID(id) {
		return asset.Logical{}, asset.ErrInvalid
	}
	return s.Store.GetEvaluatorDefinition(ctx, scope, id)
}
func (s EvaluatorDefinitionService) ListEvaluatorDefinitions(ctx context.Context, scope asset.Scope, limit int) ([]asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return nil, err
	}
	if limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	return s.Store.ListEvaluatorDefinitions(ctx, scope, limit)
}
func (s EvaluatorDefinitionService) GetEvaluatorDefinitionVersion(ctx context.Context, scope asset.Scope, ref asset.Ref) (EvaluatorDefinitionVersion, error) {
	if err := scope.Validate(false); err != nil {
		return EvaluatorDefinitionVersion{}, err
	}
	if err := ref.Validate(); err != nil {
		return EvaluatorDefinitionVersion{}, err
	}
	return s.Store.GetEvaluatorDefinitionVersion(ctx, scope, ref)
}
func (s EvaluatorDefinitionService) ListEvaluatorDefinitionVersions(ctx context.Context, scope asset.Scope, id string, limit int) ([]EvaluatorDefinitionVersion, error) {
	if err := scope.Validate(false); err != nil {
		return nil, err
	}
	if !asset.ValidID(id) || limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	return s.Store.ListEvaluatorDefinitionVersions(ctx, scope, id, limit)
}
