package postgres

import (
	"context"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"

	"github.com/jackc/pgx/v5/pgxpool"
)

type MetricDefinitions struct{ Pool *pgxpool.Pool }

var _ metric.MetricDefinitionStore = MetricDefinitions{}

func (s MetricDefinitions) CreateMetricDefinition(ctx context.Context, scope asset.Scope, cmd asset.Create) (asset.Logical, error) {
	return createLogical(ctx, s.Pool, metricTables, scope, cmd)
}
func (s MetricDefinitions) PublishMetricDefinitionVersion(ctx context.Context, scope asset.Scope, cmd asset.Publish[metric.Definition]) (metric.DefinitionVersion, error) {
	return publish(ctx, s.Pool, metricTables, scope, cmd, nil, nil)
}
func (s MetricDefinitions) GetMetricDefinition(ctx context.Context, scope asset.Scope, id string) (asset.Logical, error) {
	return getLogical(ctx, s.Pool, metricTables, scope, id)
}
func (s MetricDefinitions) ListMetricDefinitions(ctx context.Context, scope asset.Scope, limit int) ([]asset.Logical, error) {
	return listLogical(ctx, s.Pool, metricTables, scope, limit)
}
func (s MetricDefinitions) GetMetricDefinitionVersion(ctx context.Context, scope asset.Scope, ref asset.Ref) (metric.DefinitionVersion, error) {
	return loadVersion[metric.Definition](ctx, s.Pool, metricTables, scope, ref)
}
func (s MetricDefinitions) ListMetricDefinitionVersions(ctx context.Context, scope asset.Scope, id string, limit int) ([]metric.DefinitionVersion, error) {
	return listVersions[metric.Definition](ctx, s.Pool, metricTables, scope, id, limit)
}
