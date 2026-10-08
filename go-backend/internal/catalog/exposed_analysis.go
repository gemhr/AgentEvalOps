package catalog

import (
	"context"

	"agentevalops/go-backend/internal/asset"
	gov "agentevalops/go-backend/internal/cigovernance"
)

type ExposedAnalysis struct {
	Dataset    asset.Ref         `json:"dataset_version"`
	SourceRole string            `json:"source_role"`
	Usage      string            `json:"usage"`
	Cases      []DevelopmentCase `json:"cases"`
}

// ExportExposedAnalysis 只投影已确认披露的原 WP09 资产，不改变冻结 role 或正文。
func (s DatasetService) ExportExposedAnalysis(ctx context.Context, scope asset.Scope, ref asset.Ref) (ExposedAnalysis, error) {
	out := ExposedAnalysis{Dataset: ref, SourceRole: "CONSUMED_EXPOSED", Usage: "EXPOSED_DEVELOPMENT_ANALYSIS"}
	if ref.EntityID != "6fb67037-6512-562a-8227-e842851e6f0f" || ref.Version != "golden-v1" {
		return out, asset.ErrForbidden
	}
	ds, err := s.GetDatasetVersion(ctx, scope, ref)
	if err != nil {
		return out, err
	}
	p, err := gov.ReadPolicy(ds.Content().Body.Metadata)
	if err != nil || p == nil || p.Role != "HOLDOUT" || len(ds.Content().Body.Cases) != 6 {
		return out, asset.ErrForbidden
	}
	for _, r := range ds.Content().Body.Cases {
		// 防止相同 Dataset identity 的新正文把未披露 Case 带入导出。
		if r.Version != "golden-v1" || !gov.WP09Retired(asset.Ref{}, []asset.Ref{r}) {
			return out, asset.ErrForbidden
		}
		v, err := s.Cases.GetCaseVersion(ctx, scope, r)
		if err != nil {
			return out, err
		}
		cp, err := gov.ReadPolicy(v.Content().Body.Metadata)
		if err != nil || cp == nil || cp.Role != "HOLDOUT" || !gov.HardGolden(cp.State) {
			return out, asset.ErrForbidden
		}
		out.Cases = append(out.Cases, DevelopmentCase{r, v.Content().Body.Input, v.Content().Body.GroundTruth, cp.ReviewID})
	}
	return out, nil
}
