package metric

import (
	"context"

	"agentevalops/go-backend/internal/asset"
)

// MetricDefinitionStore 是应用消费的命令/查询端口；每个 Publish 必须在一个短事务中完成。
type MetricDefinitionStore interface {
	CreateMetricDefinition(context.Context, asset.Scope, asset.Create) (asset.Logical, error)
	PublishMetricDefinitionVersion(context.Context, asset.Scope, asset.Publish[Definition]) (DefinitionVersion, error)
	GetMetricDefinition(context.Context, asset.Scope, string) (asset.Logical, error)
	ListMetricDefinitions(context.Context, asset.Scope, int) ([]asset.Logical, error)
	GetMetricDefinitionVersion(context.Context, asset.Scope, asset.Ref) (DefinitionVersion, error)
	ListMetricDefinitionVersions(context.Context, asset.Scope, string, int) ([]DefinitionVersion, error)
}

type MetricDefinitionService struct{ Store MetricDefinitionStore }

func (s MetricDefinitionService) CreateMetricDefinition(ctx context.Context, scope asset.Scope, cmd asset.Create) (asset.Logical, error) {
	if err := scope.Validate(true); err != nil {
		return asset.Logical{}, err
	}
	if err := cmd.Validate(); err != nil {
		return asset.Logical{}, err
	}
	return s.Store.CreateMetricDefinition(ctx, scope, cmd)
}
func (s MetricDefinitionService) PublishMetricDefinitionVersion(ctx context.Context, scope asset.Scope, cmd asset.Publish[Definition]) (DefinitionVersion, error) {
	if err := scope.Validate(true); err != nil {
		return DefinitionVersion{}, err
	}
	if err := cmd.Ref.Validate(); err != nil {
		return DefinitionVersion{}, err
	}
	if err := cmd.Source.Validate(); err != nil {
		return DefinitionVersion{}, err
	}
	if err := cmd.Body.Validate(); err != nil {
		return DefinitionVersion{}, err
	}
	return s.Store.PublishMetricDefinitionVersion(ctx, scope, cmd)
}
func (s MetricDefinitionService) GetMetricDefinition(ctx context.Context, scope asset.Scope, id string) (asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return asset.Logical{}, err
	}
	if !asset.ValidID(id) {
		return asset.Logical{}, asset.ErrInvalid
	}
	return s.Store.GetMetricDefinition(ctx, scope, id)
}
func (s MetricDefinitionService) ListMetricDefinitions(ctx context.Context, scope asset.Scope, limit int) ([]asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return nil, err
	}
	if limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	return s.Store.ListMetricDefinitions(ctx, scope, limit)
}
func (s MetricDefinitionService) GetMetricDefinitionVersion(ctx context.Context, scope asset.Scope, ref asset.Ref) (DefinitionVersion, error) {
	if err := scope.Validate(false); err != nil {
		return DefinitionVersion{}, err
	}
	if err := ref.Validate(); err != nil {
		return DefinitionVersion{}, err
	}
	return s.Store.GetMetricDefinitionVersion(ctx, scope, ref)
}
func (s MetricDefinitionService) ListMetricDefinitionVersions(ctx context.Context, scope asset.Scope, id string, limit int) ([]DefinitionVersion, error) {
	if err := scope.Validate(false); err != nil {
		return nil, err
	}
	if !asset.ValidID(id) || limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	return s.Store.ListMetricDefinitionVersions(ctx, scope, id, limit)
}
