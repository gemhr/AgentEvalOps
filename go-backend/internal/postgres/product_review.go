package postgres

import (
	"agentevalops/go-backend/internal/asset"
	rv "agentevalops/go-backend/internal/review"
	"context"
)

// GetAdjudicationReview 保留 G7 read owner；只有实际裁决条件满足才恢复冻结自动上下文。
func (k Reviews) GetAdjudicationReview(ctx context.Context, s rv.Scope, id string) (rv.ReadModel, error) {
	if !s.Adjudicate {
		return rv.ReadModel{}, asset.ErrForbidden
	}
	v, e := k.GetReview(ctx, s, id)
	if e != nil {
		return v, e
	}
	if v.Item.Status != "ADJUDICATION_REQUIRED" && v.Item.Status != "COMPLETED" {
		return rv.ReadModel{}, asset.ErrUnsupported
	}
	original, e := reviewItem(ctx, k.Pool, s.Scope.Scope, id, false)
	if e != nil {
		return rv.ReadModel{}, e
	}
	v.Item.Source.Automatic = original.Source.Automatic
	return v, nil
}
func (k Reviews) GetGoldenLabel(ctx context.Context, s rv.Scope, id string) (rv.GoldenLabel, error) {
	var v rv.GoldenLabel
	if s.ValidateReviewer() != nil || !s.PublishGolden && !s.Calibrate || !asset.ValidID(id) {
		return v, asset.ErrForbidden
	}
	e := reviewDecode(ctx, k.Pool, s.Scope.Scope, "evaluation_golden_labels", "golden_bytes", id, &v)
	return v, e
}
