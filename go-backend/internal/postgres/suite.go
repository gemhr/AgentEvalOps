package postgres

import (
	"context"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Suites struct{ Pool *pgxpool.Pool }

var _ catalog.SuiteStore = Suites{}

func (s Suites) CreateSuite(ctx context.Context, scope asset.Scope, cmd asset.Create) (asset.Logical, error) {
	return createLogical(ctx, s.Pool, suiteTables, scope, cmd)
}
func (s Suites) PublishSuiteVersion(ctx context.Context, scope asset.Scope, cmd asset.Publish[catalog.SuiteContent]) (catalog.SuiteVersion, error) {
	return publish(ctx, s.Pool, suiteTables, scope, cmd, cmd.Body.Dataset, func(ctx context.Context, tx pgx.Tx, body catalog.SuiteContent) error {
		return publishSuiteRefs(ctx, tx, scope, cmd.Ref, body)
	})
}
func (s Suites) GetSuite(ctx context.Context, scope asset.Scope, id string) (asset.Logical, error) {
	return getLogical(ctx, s.Pool, suiteTables, scope, id)
}
func (s Suites) ListSuites(ctx context.Context, scope asset.Scope, limit int) ([]asset.Logical, error) {
	return listLogical(ctx, s.Pool, suiteTables, scope, limit)
}
func (s Suites) GetSuiteVersion(ctx context.Context, scope asset.Scope, ref asset.Ref) (catalog.SuiteVersion, error) {
	return loadVersion[catalog.SuiteContent](ctx, s.Pool, suiteTables, scope, ref)
}
func (s Suites) ListSuiteVersions(ctx context.Context, scope asset.Scope, id string, limit int) ([]catalog.SuiteVersion, error) {
	return listVersions[catalog.SuiteContent](ctx, s.Pool, suiteTables, scope, id, limit)
}
