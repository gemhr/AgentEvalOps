package catalog

import (
	"context"

	"agentevalops/go-backend/internal/asset"
)

// DatasetStore 是应用消费的命令/查询端口；每个 Publish 必须在一个短事务中完成。
type DatasetStore interface {
	CreateDataset(context.Context, asset.Scope, asset.Create) (asset.Logical, error)
	PublishDatasetVersion(context.Context, asset.Scope, asset.Publish[DatasetContent]) (DatasetVersion, error)
	GetDataset(context.Context, asset.Scope, string) (asset.Logical, error)
	ListDatasets(context.Context, asset.Scope, int) ([]asset.Logical, error)
	GetDatasetVersion(context.Context, asset.Scope, asset.Ref) (DatasetVersion, error)
	ListDatasetVersions(context.Context, asset.Scope, string, int) ([]DatasetVersion, error)
}

type DatasetService struct {
	Store DatasetStore
	Cases CaseReader
}

func (s DatasetService) CreateDataset(ctx context.Context, scope asset.Scope, cmd asset.Create) (asset.Logical, error) {
	if err := scope.Validate(true); err != nil {
		return asset.Logical{}, err
	}
	if err := cmd.Validate(); err != nil {
		return asset.Logical{}, err
	}
	return s.Store.CreateDataset(ctx, scope, cmd)
}
func (s DatasetService) PublishDatasetVersion(ctx context.Context, scope asset.Scope, cmd asset.Publish[DatasetContent]) (DatasetVersion, error) {
	if err := scope.Validate(true); err != nil {
		return DatasetVersion{}, err
	}
	if err := cmd.Ref.Validate(); err != nil {
		return DatasetVersion{}, err
	}
	if err := cmd.Source.Validate(); err != nil {
		return DatasetVersion{}, err
	}
	if err := cmd.Body.Validate(); err != nil {
		return DatasetVersion{}, err
	}
	for _, ref := range cmd.Body.Cases {
		v, err := s.Cases.GetCaseVersion(ctx, scope, ref)
		if err != nil {
			return DatasetVersion{}, err
		}
		if v.ProjectID() != scope.ProjectID {
			return DatasetVersion{}, asset.ErrNotFound
		}
	}
	return s.Store.PublishDatasetVersion(ctx, scope, cmd)
}
func (s DatasetService) GetDataset(ctx context.Context, scope asset.Scope, id string) (asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return asset.Logical{}, err
	}
	if !asset.ValidID(id) {
		return asset.Logical{}, asset.ErrInvalid
	}
	return s.Store.GetDataset(ctx, scope, id)
}
func (s DatasetService) ListDatasets(ctx context.Context, scope asset.Scope, limit int) ([]asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return nil, err
	}
	if limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	return s.Store.ListDatasets(ctx, scope, limit)
}
func (s DatasetService) GetDatasetVersion(ctx context.Context, scope asset.Scope, ref asset.Ref) (DatasetVersion, error) {
	if err := scope.Validate(false); err != nil {
		return DatasetVersion{}, err
	}
	if err := ref.Validate(); err != nil {
		return DatasetVersion{}, err
	}
	return s.Store.GetDatasetVersion(ctx, scope, ref)
}
func (s DatasetService) ListDatasetVersions(ctx context.Context, scope asset.Scope, id string, limit int) ([]DatasetVersion, error) {
	if err := scope.Validate(false); err != nil {
		return nil, err
	}
	if !asset.ValidID(id) || limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	return s.Store.ListDatasetVersions(ctx, scope, id, limit)
}
