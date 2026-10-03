package catalog

import (
	"context"

	"agentevalops/go-backend/internal/asset"
)

// SuiteStore 是应用消费的命令/查询端口；每个 Publish 必须在一个短事务中完成。
type SuiteStore interface {
	CreateSuite(context.Context, asset.Scope, asset.Create) (asset.Logical, error)
	PublishSuiteVersion(context.Context, asset.Scope, asset.Publish[SuiteContent]) (SuiteVersion, error)
	GetSuite(context.Context, asset.Scope, string) (asset.Logical, error)
	ListSuites(context.Context, asset.Scope, int) ([]asset.Logical, error)
	GetSuiteVersion(context.Context, asset.Scope, asset.Ref) (SuiteVersion, error)
	ListSuiteVersions(context.Context, asset.Scope, string, int) ([]SuiteVersion, error)
}

type SuiteService struct {
	Store      SuiteStore
	References SuiteReferenceReader
}

func (s SuiteService) CreateSuite(ctx context.Context, scope asset.Scope, cmd asset.Create) (asset.Logical, error) {
	if err := scope.Validate(true); err != nil {
		return asset.Logical{}, err
	}
	if err := cmd.Validate(); err != nil {
		return asset.Logical{}, err
	}
	return s.Store.CreateSuite(ctx, scope, cmd)
}
func (s SuiteService) PublishSuiteVersion(ctx context.Context, scope asset.Scope, cmd asset.Publish[SuiteContent]) (SuiteVersion, error) {
	if err := scope.Validate(true); err != nil {
		return SuiteVersion{}, err
	}
	if err := cmd.Ref.Validate(); err != nil {
		return SuiteVersion{}, err
	}
	if err := cmd.Source.Validate(); err != nil {
		return SuiteVersion{}, err
	}
	if err := cmd.Body.Validate(); err != nil {
		return SuiteVersion{}, err
	}
	if err := validateSuiteReferences(ctx, s.References, scope, cmd.Body); err != nil {
		return SuiteVersion{}, err
	}
	return s.Store.PublishSuiteVersion(ctx, scope, cmd)
}
func (s SuiteService) GetSuite(ctx context.Context, scope asset.Scope, id string) (asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return asset.Logical{}, err
	}
	if !asset.ValidID(id) {
		return asset.Logical{}, asset.ErrInvalid
	}
	return s.Store.GetSuite(ctx, scope, id)
}
func (s SuiteService) ListSuites(ctx context.Context, scope asset.Scope, limit int) ([]asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return nil, err
	}
	if limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	return s.Store.ListSuites(ctx, scope, limit)
}
func (s SuiteService) GetSuiteVersion(ctx context.Context, scope asset.Scope, ref asset.Ref) (SuiteVersion, error) {
	if err := scope.Validate(false); err != nil {
		return SuiteVersion{}, err
	}
	if err := ref.Validate(); err != nil {
		return SuiteVersion{}, err
	}
	return s.Store.GetSuiteVersion(ctx, scope, ref)
}
func (s SuiteService) ListSuiteVersions(ctx context.Context, scope asset.Scope, id string, limit int) ([]SuiteVersion, error) {
	if err := scope.Validate(false); err != nil {
		return nil, err
	}
	if !asset.ValidID(id) || limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	return s.Store.ListSuiteVersions(ctx, scope, id, limit)
}
