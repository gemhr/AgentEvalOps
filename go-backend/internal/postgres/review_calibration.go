package postgres

import (
	"context"
	"errors"
	"slices"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/metric"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5"
)

func (k Reviews) PrepareCalibration(ctx context.Context, s rv.Scope, c rv.CalibrationCommand) (rv.CalibrationSnapshot, error) {
	var snapshot rv.CalibrationSnapshot
	if !s.Calibrate || s.ValidateReviewer() != nil || c.Validate() != nil {
		return snapshot, asset.ErrInvalid
	}
	intent := reviewHash(struct {
		Command rv.CalibrationCommand
		Actor   rv.Reviewer
	}{c, s.Reviewer()})
	e := reviewDecode(ctx, k.Pool, s.Scope.Scope, "evaluation_calibration_snapshots", "snapshot_bytes", c.ID, &snapshot)
	if e == nil {
		if snapshot.Intent != intent {
			return snapshot, asset.ErrConflict
		}
		return snapshot, nil
	}
	if !errors.Is(e, asset.ErrNotFound) {
		return snapshot, e
	}
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if e != nil {
		return snapshot, e
	}
	defer rollback(ctx, tx)
	dataset, e := loadVersion[catalog.DatasetContent](ctx, tx, datasetTables, s.Scope.Scope, c.Dataset)
	if e != nil {
		return snapshot, e
	}
	evaluator, e := loadVersion[metric.EvaluatorDefinition](ctx, tx, evaluatorTables, s.Scope.Scope, c.Evaluator)
	if e != nil {
		return snapshot, e
	}
	schema, e := loadVersion[metric.Definition](ctx, tx, metricTables, s.Scope.Scope, c.Schema)
	if e != nil {
		return snapshot, e
	}
	if !slices.Contains(evaluator.Content().Body.OutputMetrics, c.Schema) || schema.Content().Body.ValueType != metric.Enum {
		return snapshot, asset.ErrInvalid
	}
	if c.PositiveClass != "" && !slices.Contains(schema.Content().Body.Labels, c.PositiveClass) {
		return snapshot, asset.ErrInvalid
	}
	// precision/recall/F1 只在二分类的可判标签空间中输出。
	decidable := 0
	for _, v := range schema.Content().Body.Labels {
		if rv.Decidable(v) {
			decidable++
		}
	}
	if c.PositiveClass != "" && decidable != 2 {
		return snapshot, asset.ErrInvalid
	}
	snapshot = rv.CalibrationSnapshot{ID: c.ID, ProjectID: s.ProjectID, Actor: s.Principal, Intent: intent, Command: c, DatasetDigest: dataset.ContentDigest(), EvaluatorDigest: evaluator.ContentDigest(), SchemaDigest: schema.ContentDigest(), Definition: evaluator.Content().Body}
	selected := []asset.Ref{}
	for _, ref := range dataset.Content().Body.Cases {
		if c.Sampling.Selected(s.ProjectID, rv.SourceRef{Type: "CALIBRATION_CASE", ObservationID: ref.EntityID, CandidateSource: ref.Version}) {
			selected = append(selected, ref)
		}
	}
	if len(selected) != len(c.Samples) {
		return snapshot, asset.ErrInvalid
	}
	for i, sample := range c.Samples {
		if sample.Case != selected[i] {
			return snapshot, asset.ErrInvalid
		}
		pair := rv.CalibrationPair{Case: sample.Case}
		if sample.GoldenID != "" {
			var g rv.GoldenLabel
			if e = reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_golden_labels", "golden_bytes", sample.GoldenID, &g); e != nil {
				return snapshot, e
			}
			if g.Case == nil || *g.Case != sample.Case || g.Schema != c.Schema || g.SchemaDigest != snapshot.SchemaDigest || g.Protocol != c.Protocol {
				return snapshot, asset.ErrConflict
			}
			item, e := reviewItem(ctx, tx, s.Scope.Scope, g.ItemID, false)
			if e != nil {
				return snapshot, e
			}
			a, e := reviewAnnotations(ctx, tx, s.Scope.Scope, g.ItemID)
			if e != nil {
				return snapshot, e
			}
			ids := []string{}
			for _, v := range a {
				ids = append(ids, v.ID)
			}
			if item.Status != "COMPLETED" || !slices.Equal(g.AnnotationIDs, ids) {
				return snapshot, asset.ErrConflict
			}
			if g.AdjudicationID != nil {
				latest, e := latestAdjudication(ctx, tx, s.Scope.Scope, g.ItemID)
				if e != nil {
					return snapshot, e
				}
				if latest == nil || latest.ID != *g.AdjudicationID {
					return snapshot, asset.ErrConflict
				}
			}
			pair.Golden = &g
		}
		if sample.Result.Type != "" {
			source, e := resolveReviewSource(ctx, tx, s.Scope.Scope, sample.Result)
			if e != nil {
				return snapshot, e
			}
			if source.Automatic == nil || source.Case == nil || *source.Case != sample.Case || source.Automatic.Evaluator != c.Evaluator || source.Automatic.Metric != c.Schema || source.Automatic.MetricDigest != snapshot.SchemaDigest {
				return snapshot, asset.ErrConflict
			}
			pair.Judge = source.Automatic
			if pair.Golden != nil && pair.Golden.EvidenceDigest != pair.Judge.EvidenceDigest {
				return snapshot, asset.ErrConflict
			}
		}
		snapshot.Pairs = append(snapshot.Pairs, pair)
	}
	// Golden 集合 identity 不包括 Judge version/result，允许同证据上 v1/v2 的独立校准。
	goldenSet := []struct {
		Case   asset.Ref
		Golden *rv.GoldenLabel
	}{}
	for _, p := range snapshot.Pairs {
		goldenSet = append(goldenSet, struct {
			Case   asset.Ref
			Golden *rv.GoldenLabel
		}{p.Case, p.Golden})
	}
	snapshot.GoldenSetDigest = reviewHash(goldenSet)
	if e = tx.QueryRow(ctx, "SELECT clock_timestamp()").Scan(&snapshot.CreatedAt); e != nil {
		return snapshot, e
	}
	if e = tx.Commit(ctx); e != nil {
		return snapshot, e
	}
	frozen, e := asset.FreezeBytes(snapshot)
	if e != nil {
		return snapshot, e
	}
	e = k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		if _, e := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", s.ProjectID+":calibration:"+c.ID); e != nil {
			return e
		}
		var old rv.CalibrationSnapshot
		e := reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_calibration_snapshots", "snapshot_bytes", c.ID, &old)
		if e == nil {
			if old.Intent != intent {
				return asset.ErrConflict
			}
			snapshot = old
			return nil
		}
		if !errors.Is(e, asset.ErrNotFound) {
			return e
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_calibration_snapshots(id,project_id,dataset_id,dataset_version,evaluator_id,evaluator_version,schema_id,schema_version,intent_digest,snapshot_bytes) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`, c.ID, s.ProjectID, c.Dataset.EntityID, c.Dataset.Version, c.Evaluator.EntityID, c.Evaluator.Version, c.Schema.EntityID, c.Schema.Version, intent, frozen)
		if e != nil {
			return e
		}
		for _, p := range snapshot.Pairs {
			var golden *string
			if p.Golden != nil {
				golden = &p.Golden.ID
			}
			if _, e = tx.Exec(ctx, `INSERT INTO evaluation_calibration_samples(project_id,calibration_id,case_id,case_version,golden_id) VALUES($1,$2,$3,$4,$5)`, s.ProjectID, c.ID, p.Case.EntityID, p.Case.Version, golden); e != nil {
				return e
			}
		}
		return nil
	})
	return snapshot, e
}
func (k Reviews) CompleteCalibration(ctx context.Context, s rv.Scope, id string) (rv.CalibrationReport, error) {
	var out rv.CalibrationReport
	if !s.Calibrate || s.ValidateReviewer() != nil || !asset.ValidID(id) {
		return out, asset.ErrInvalid
	}
	var snapshot rv.CalibrationSnapshot
	if e := reviewDecode(ctx, k.Pool, s.Scope.Scope, "evaluation_calibration_snapshots", "snapshot_bytes", id, &snapshot); e != nil {
		return out, e
	}
	if snapshot.Actor != s.Principal {
		return out, asset.ErrForbidden
	}
	out, e := rv.Compute(snapshot)
	if e != nil {
		return out, e
	}
	raw, e := asset.Freeze(out)
	if e != nil {
		return out, e
	}
	e = k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		_, e := tx.Exec(ctx, `INSERT INTO evaluation_calibration_reports(project_id,id,report_bytes,report_digest) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, s.ProjectID, id, raw.Bytes(), raw.Digest())
		if e != nil {
			return e
		}
		return reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_calibration_reports", "report_bytes", id, &out)
	})
	if e == nil {
		reviewEvent("calibration_completed", s.ProjectID, id)
		if out.Agreement.Disagreements > 0 {
			reviewEvent("judge_disagreement_detected", s.ProjectID, id)
		}
	}
	return out, e
}
func (k Reviews) GetCalibrationReport(ctx context.Context, s rv.Scope, id string) (rv.CalibrationReport, error) {
	var r rv.CalibrationReport
	if !s.Calibrate || s.ValidateReviewer() != nil || !asset.ValidID(id) {
		return r, asset.ErrForbidden
	}
	e := reviewDecode(ctx, k.Pool, s.Scope.Scope, "evaluation_calibration_reports", "report_bytes", id, &r)
	return r, e
}
func (k Reviews) CompareEvaluatorCalibrations(ctx context.Context, s rv.Scope, baseline, candidate string) (rv.EvaluatorRegression, error) {
	a, e := k.GetCalibrationReport(ctx, s, baseline)
	if e != nil {
		return rv.EvaluatorRegression{}, e
	}
	b, e := k.GetCalibrationReport(ctx, s, candidate)
	if e != nil {
		return rv.EvaluatorRegression{}, e
	}
	return rv.CompareEvaluators(a, b), nil
}
