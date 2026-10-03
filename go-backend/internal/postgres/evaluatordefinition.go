package postgres

import (
	"context"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type EvaluatorDefinitions struct{ Pool *pgxpool.Pool }

var _ metric.EvaluatorDefinitionStore = EvaluatorDefinitions{}

func (s EvaluatorDefinitions) CreateEvaluatorDefinition(ctx context.Context, scope asset.Scope, cmd asset.Create) (asset.Logical, error) {
	return createLogical(ctx, s.Pool, evaluatorTables, scope, cmd)
}
func (s EvaluatorDefinitions) PublishEvaluatorDefinitionVersion(ctx context.Context, scope asset.Scope, cmd asset.Publish[metric.EvaluatorDefinition]) (metric.EvaluatorDefinitionVersion, error) {
	return publish(ctx, s.Pool, evaluatorTables, scope, cmd, nil, func(ctx context.Context, tx pgx.Tx, body metric.EvaluatorDefinition) error {
		return publishEvaluatorDefinitionRefs(ctx, tx, scope, cmd.Ref, body)
	})
}
func (s EvaluatorDefinitions) GetEvaluatorDefinition(ctx context.Context, scope asset.Scope, id string) (asset.Logical, error) {
	return getLogical(ctx, s.Pool, evaluatorTables, scope, id)
}
func (s EvaluatorDefinitions) ListEvaluatorDefinitions(ctx context.Context, scope asset.Scope, limit int) ([]asset.Logical, error) {
	return listLogical(ctx, s.Pool, evaluatorTables, scope, limit)
}
func (s EvaluatorDefinitions) GetEvaluatorDefinitionVersion(ctx context.Context, scope asset.Scope, ref asset.Ref) (metric.EvaluatorDefinitionVersion, error) {
	return loadVersion[metric.EvaluatorDefinition](ctx, s.Pool, evaluatorTables, scope, ref)
}
func (s EvaluatorDefinitions) ListEvaluatorDefinitionVersions(ctx context.Context, scope asset.Scope, id string, limit int) ([]metric.EvaluatorDefinitionVersion, error) {
	return listVersions[metric.EvaluatorDefinition](ctx, s.Pool, evaluatorTables, scope, id, limit)
}
