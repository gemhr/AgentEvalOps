package catalog

import (
	"context"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/cigovernance"
	"slices"
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
	policy, _ := cigovernance.ReadPolicy(cmd.Body.Metadata)
	families := []string{}
	for _, ref := range cmd.Body.Cases {
		v, err := s.Cases.GetCaseVersion(ctx, scope, ref)
		if err != nil {
			return DatasetVersion{}, err
		}
		if v.ProjectID() != scope.ProjectID {
			return DatasetVersion{}, asset.ErrNotFound
		}
		if policy != nil {
			p, err := cigovernance.ReadPolicy(v.Content().Body.Metadata)
			if err != nil || p == nil || p.Role != policy.Role || p.Profile != policy.Profile || p.GTMapping != policy.GTMapping || p.Split != policy.Split || !cigovernance.HardGolden(p.State) || p.ReviewID == "" || slices.Contains(families, p.Family) {
				return DatasetVersion{}, asset.ErrInvalid
			}
			families = append(families, p.Family)
		}
	}
	if policy != nil {
		slices.Sort(families)
		expected := slices.Clone(policy.Families)
		slices.Sort(expected)
		if !slices.Equal(families, expected) {
			return DatasetVersion{}, asset.ErrInvalid
		}
	}
	return s.Store.PublishDatasetVersion(ctx, scope, cmd)
}

type DevelopmentCase struct {
	Case        asset.Ref  `json:"case_version"`
	Input       asset.JSON `json:"input"`
	GroundTruth asset.JSON `json:"ground_truth"`
	ReviewID    string     `json:"review_id"`
}

// ExportDevelopment 按实际冻结 metadata 授权，caller 不能用请求里的 role 改写。
func (s DatasetService) ExportDevelopment(ctx context.Context, scope asset.Scope, ref asset.Ref) ([]DevelopmentCase, error) {
	ds, err := s.GetDatasetVersion(ctx, scope, ref)
	if err != nil {
		return nil, err
	}
	p, err := cigovernance.ReadPolicy(ds.Content().Body.Metadata)
	if err != nil || p == nil || p.Role != "DEVELOPMENT" {
		return nil, asset.ErrForbidden
	}
	out := []DevelopmentCase{}
	for _, r := range ds.Content().Body.Cases {
		v, err := s.Cases.GetCaseVersion(ctx, scope, r)
		if err != nil {
			return nil, err
		}
		cp, err := cigovernance.ReadPolicy(v.Content().Body.Metadata)
		if err != nil || cp == nil || cp.Role != "DEVELOPMENT" || !cigovernance.HardGolden(cp.State) {
			return nil, asset.ErrForbidden
		}
		out = append(out, DevelopmentCase{r, v.Content().Body.Input, v.Content().Body.GroundTruth, cp.ReviewID})
	}
	return out, nil
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
