package postgres

import (
	"context"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"github.com/jackc/pgx/v5"

	"github.com/jackc/pgx/v5/pgxpool"
)

type Cases struct{ Pool *pgxpool.Pool }

var _ catalog.CaseStore = Cases{}

func (s Cases) CreateCase(ctx context.Context, scope asset.Scope, cmd asset.Create) (asset.Logical, error) {
	return createLogical(ctx, s.Pool, caseTables, scope, cmd)
}
func (s Cases) PublishCaseVersion(ctx context.Context, scope asset.Scope, cmd asset.Publish[catalog.CaseContent]) (catalog.CaseVersion, error) {
	return publish(ctx, s.Pool, caseTables, scope, cmd, nil, func(ctx context.Context, tx pgx.Tx, body catalog.CaseContent) error {
		return validateStage13ReviewedCase(ctx, tx, scope, cmd.Ref, body)
	})
}
func (s Cases) GetCase(ctx context.Context, scope asset.Scope, id string) (asset.Logical, error) {
	return getLogical(ctx, s.Pool, caseTables, scope, id)
}
func (s Cases) ListCases(ctx context.Context, scope asset.Scope, limit int) ([]asset.Logical, error) {
	return listLogical(ctx, s.Pool, caseTables, scope, limit)
}
func (s Cases) GetCaseVersion(ctx context.Context, scope asset.Scope, ref asset.Ref) (catalog.CaseVersion, error) {
	return loadVersion[catalog.CaseContent](ctx, s.Pool, caseTables, scope, ref)
}
func (s Cases) ListCaseVersions(ctx context.Context, scope asset.Scope, id string, limit int) ([]catalog.CaseVersion, error) {
	return listVersions[catalog.CaseContent](ctx, s.Pool, caseTables, scope, id, limit)
}
