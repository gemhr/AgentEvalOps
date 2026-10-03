package postgres

import (
	"context"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Datasets struct{ Pool *pgxpool.Pool }

var _ catalog.DatasetStore = Datasets{}

func (s Datasets) CreateDataset(ctx context.Context, scope asset.Scope, cmd asset.Create) (asset.Logical, error) {
	return createLogical(ctx, s.Pool, datasetTables, scope, cmd)
}
func (s Datasets) PublishDatasetVersion(ctx context.Context, scope asset.Scope, cmd asset.Publish[catalog.DatasetContent]) (catalog.DatasetVersion, error) {
	return publish(ctx, s.Pool, datasetTables, scope, cmd, nil, func(ctx context.Context, tx pgx.Tx, body catalog.DatasetContent) error {
		return publishDatasetRefs(ctx, tx, scope, cmd.Ref, body)
	})
}
func (s Datasets) GetDataset(ctx context.Context, scope asset.Scope, id string) (asset.Logical, error) {
	return getLogical(ctx, s.Pool, datasetTables, scope, id)
}
func (s Datasets) ListDatasets(ctx context.Context, scope asset.Scope, limit int) ([]asset.Logical, error) {
	return listLogical(ctx, s.Pool, datasetTables, scope, limit)
}
func (s Datasets) GetDatasetVersion(ctx context.Context, scope asset.Scope, ref asset.Ref) (catalog.DatasetVersion, error) {
	return loadVersion[catalog.DatasetContent](ctx, s.Pool, datasetTables, scope, ref)
}
func (s Datasets) ListDatasetVersions(ctx context.Context, scope asset.Scope, id string, limit int) ([]catalog.DatasetVersion, error) {
	return listVersions[catalog.DatasetContent](ctx, s.Pool, datasetTables, scope, id, limit)
}
