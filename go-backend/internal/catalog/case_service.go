package catalog

import (
	"context"

	"agentevalops/go-backend/internal/asset"
)

// CaseStore 是应用消费的命令/查询端口；每个 Publish 必须在一个短事务中完成。
type CaseStore interface {
	CreateCase(context.Context, asset.Scope, asset.Create) (asset.Logical, error)
	PublishCaseVersion(context.Context, asset.Scope, asset.Publish[CaseContent]) (CaseVersion, error)
	GetCase(context.Context, asset.Scope, string) (asset.Logical, error)
	ListCases(context.Context, asset.Scope, int) ([]asset.Logical, error)
	GetCaseVersion(context.Context, asset.Scope, asset.Ref) (CaseVersion, error)
	ListCaseVersions(context.Context, asset.Scope, string, int) ([]CaseVersion, error)
}

type CaseService struct{ Store CaseStore }

func (s CaseService) CreateCase(ctx context.Context, scope asset.Scope, cmd asset.Create) (asset.Logical, error) {
	if err := scope.Validate(true); err != nil {
		return asset.Logical{}, err
	}
	if err := cmd.Validate(); err != nil {
		return asset.Logical{}, err
	}
	return s.Store.CreateCase(ctx, scope, cmd)
}
func (s CaseService) PublishCaseVersion(ctx context.Context, scope asset.Scope, cmd asset.Publish[CaseContent]) (CaseVersion, error) {
	if err := scope.Validate(true); err != nil {
		return CaseVersion{}, err
	}
	if err := cmd.Ref.Validate(); err != nil {
		return CaseVersion{}, err
	}
	if err := cmd.Source.Validate(); err != nil {
		return CaseVersion{}, err
	}
	if err := cmd.Body.Validate(); err != nil {
		return CaseVersion{}, err
	}
	return s.Store.PublishCaseVersion(ctx, scope, cmd)
}
func (s CaseService) GetCase(ctx context.Context, scope asset.Scope, id string) (asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return asset.Logical{}, err
	}
	if !asset.ValidID(id) {
		return asset.Logical{}, asset.ErrInvalid
	}
	return s.Store.GetCase(ctx, scope, id)
}
func (s CaseService) ListCases(ctx context.Context, scope asset.Scope, limit int) ([]asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return nil, err
	}
	if limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	return s.Store.ListCases(ctx, scope, limit)
}
func (s CaseService) GetCaseVersion(ctx context.Context, scope asset.Scope, ref asset.Ref) (CaseVersion, error) {
	if err := scope.Validate(false); err != nil {
		return CaseVersion{}, err
	}
	if err := ref.Validate(); err != nil {
		return CaseVersion{}, err
	}
	return s.Store.GetCaseVersion(ctx, scope, ref)
}
func (s CaseService) ListCaseVersions(ctx context.Context, scope asset.Scope, id string, limit int) ([]CaseVersion, error) {
	if err := scope.Validate(false); err != nil {
		return nil, err
	}
	if !asset.ValidID(id) || limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	return s.Store.ListCaseVersions(ctx, scope, id, limit)
}
