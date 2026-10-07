package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"slices"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	gov "agentevalops/go-backend/internal/cigovernance"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5"
)

func validateStage13ReviewedCase(ctx context.Context, tx pgx.Tx, s asset.Scope, ref asset.Ref, body catalog.CaseContent) error {
	p, err := gov.ReadPolicy(body.Metadata)
	if err != nil || p == nil {
		return err
	}
	if p.State == "PENDING_REVIEW" {
		return nil
	}
	if !gov.HardGolden(p.State) || !asset.ValidID(p.ReviewID) || p.Supersedes == nil {
		return asset.ErrForbidden
	}
	var raw []byte
	if err = tx.QueryRow(ctx, `SELECT decision_bytes FROM evaluation_stage13_gt_reviews WHERE project_id=$1 AND id=$2`, s.ProjectID, p.ReviewID).Scan(&raw); err != nil {
		return dbError(err)
	}
	var r gov.ReviewDecision
	if json.Unmarshal(raw, &r) != nil || r.State != p.State || r.GroundTruth.Digest() != body.GroundTruth.Digest() || r.Case != *p.Supersedes || ref.EntityID != r.Case.EntityID || ref.Version == r.Case.Version || r.GoldenID == "" {
		return asset.ErrForbidden
	}
	old, err := loadVersion[catalog.CaseContent](ctx, tx, caseTables, s, r.Case)
	if err != nil {
		return err
	}
	if old.Content().Body.Input.Digest() != body.Input.Digest() {
		return asset.ErrForbidden
	}
	previous, err := gov.ReadPolicy(old.Content().Body.Metadata)
	if err != nil {
		return err
	}
	if previous != nil && (previous.Role != p.Role || previous.Family != p.Family || previous.Profile != p.Profile || previous.GTMapping != p.GTMapping || previous.Split != p.Split) || previous == nil && p.Role == "HOLDOUT" {
		return asset.ErrForbidden
	}
	return nil
}

// StoreStage13GTReview 保存 typed GT 决策；claim/annotation/Golden 仍由既有 Review owner 提供。
func (k Reviews) StoreStage13GTReview(ctx context.Context, s rv.Scope, r gov.ReviewDecision) (gov.ReviewDecision, error) {
	var out gov.ReviewDecision
	if !s.Feedback || r.Validate() != nil {
		return out, asset.ErrInvalid
	}
	r.Reviewer = s.Principal
	r.ActorType = "CONTROLLED_REVIEWER"
	r.CreatedAt = out.CreatedAt
	r.Digest = ""
	intent := gov.Freeze(r).Digest()
	err := k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		var oldIntent string
		var raw []byte
		e := tx.QueryRow(ctx, `SELECT intent_digest,decision_bytes FROM evaluation_stage13_gt_reviews WHERE project_id=$1 AND id=$2`, s.ProjectID, r.ID).Scan(&oldIntent, &raw)
		if e == nil {
			if oldIntent != intent {
				return asset.ErrConflict
			}
			return json.Unmarshal(raw, &out)
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		item, e := reviewItem(ctx, tx, s.Scope.Scope, r.ItemID, true)
		if e != nil {
			return e
		}
		if item.Status != "COMPLETED" || item.Source.Case == nil || *item.Source.Case != r.Case || item.Source.CaseBody.GroundTruth.Digest() != r.PreviousGTDigest {
			return asset.ErrConflict
		}
		annotations, e := reviewAnnotations(ctx, tx, s.Scope.Scope, r.ItemID)
		if e != nil {
			return e
		}
		var selected *rv.Annotation
		for i := range annotations {
			if annotations[i].ID == r.AnnotationID {
				selected = &annotations[i]
			}
		}
		if selected == nil || selected.Reviewer.ID != s.Principal || !slices.Contains([]string{"CONTROLLED_REVIEWER", "CONTROLLED_TEST_REVIEWER"}, selected.Reviewer.Source) {
			return asset.ErrForbidden
		}
		value := map[string]string{"CONFIRMED": "PASS", "CORRECTED": "FAIL", "AMBIGUOUS": "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE": "INCONCLUSIVE", "REJECTED": "ERROR"}[r.State]
		if selected.Decision.Kind != rv.Quality || selected.Decision.Value != value {
			return asset.ErrConflict
		}
		if r.State == "CONFIRMED" && r.GroundTruth.Digest() != r.PreviousGTDigest || r.State == "CORRECTED" && r.GroundTruth.Digest() == r.PreviousGTDigest {
			return asset.ErrInvalid
		}
		if gov.HardGolden(r.State) {
			var g rv.GoldenLabel
			if e = reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_golden_labels", "golden_bytes", r.GoldenID, &g); e != nil {
				return e
			}
			if g.ItemID != item.ID || g.Case == nil || *g.Case != r.Case || !slices.Contains(g.AnnotationIDs, r.AnnotationID) || g.SourceDigest != item.Source.Digest {
				return asset.ErrConflict
			}
		} else if r.GoldenID != "" || r.GroundTruth.String() != "null" {
			return asset.ErrInvalid
		}
		r.CreatedAt, e = dbNow(ctx, tx)
		if e != nil {
			return e
		}
		r.Digest = gov.Freeze(r).Digest()
		var golden *string
		if r.GoldenID != "" {
			golden = &r.GoldenID
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_stage13_gt_reviews(id,project_id,item_id,case_id,case_version,annotation_id,golden_id,state,decision_bytes,intent_digest,decision_digest) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`, r.ID, s.ProjectID, r.ItemID, r.Case.EntityID, r.Case.Version, r.AnnotationID, golden, r.State, gov.Freeze(r).Bytes(), intent, r.Digest)
		out = r
		return e
	})
	return out, err
}

// PendingStage13Reviews 只决定调度优先级，不参与 metric truth。
func (k Reviews) PendingStage13Reviews(ctx context.Context, s rv.Scope) ([]string, error) {
	if s.ValidateReviewer() != nil || !s.Review {
		return nil, asset.ErrForbidden
	}
	rows, err := k.Pool.Query(ctx, `SELECT r.id::text FROM evaluation_review_items r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND r.status IN ('PENDING','IN_REVIEW') AND EXISTS(SELECT 1 FROM evaluation_review_slots w WHERE w.project_id=r.project_id AND w.item_id=r.id AND (w.status='PENDING' OR (w.status='CLAIMED' AND w.lease_expires_at<=clock_timestamp()))) ORDER BY CASE convert_from(r.item_bytes,'UTF8')::jsonb->'Command'->>'Priority' WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,r.created_at,r.id LIMIT 100`, s.ProjectID, s.OrganizationID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// StoreStage13Feedback 独立 append-only 资产；从 frozen Run/Result/Comparison 验证全部 binding。
func (k Reviews) StoreStage13Feedback(ctx context.Context, s rv.Scope, f gov.Feedback) (gov.Feedback, error) {
	var out gov.Feedback
	if !s.Feedback || f.Validate() != nil {
		return out, asset.ErrInvalid
	}
	f.ID = gov.FeedbackID(f)
	f.CreatedAt = out.CreatedAt
	f.Digest = ""
	intent := gov.Freeze(f).Digest()
	err := k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		var raw []byte
		var old string
		e := tx.QueryRow(ctx, `SELECT intent_digest,feedback_bytes FROM evaluation_stage13_feedback WHERE project_id=$1 AND id=$2`, s.ProjectID, f.ID).Scan(&old, &raw)
		if e == nil {
			if old != intent {
				return asset.ErrConflict
			}
			return json.Unmarshal(raw, &out)
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		var result ev.EvaluationResult
		e = tx.QueryRow(ctx, `SELECT to_jsonb(r) FROM evaluation_results r WHERE project_id=$1 AND id=$2`, s.ProjectID, f.ResultID).Scan(&raw)
		if e != nil {
			return dbError(e)
		}
		if json.Unmarshal(raw, &result) != nil || result.RunID != f.RunID || result.CaseID != f.Case.EntityID || result.CaseVersion != f.Case.Version || result.EvaluatorID != f.Evaluator.EntityID || result.EvaluatorVersion != f.Evaluator.Version {
			return asset.ErrConflict
		}
		var snapshotRun ev.RunSnapshot
		e = tx.QueryRow(ctx, `SELECT kernel_bytes FROM evaluation_runs WHERE project_id=$1 AND id=$2`, s.ProjectID, f.RunID).Scan(&raw)
		if e != nil {
			return dbError(e)
		}
		if json.Unmarshal(raw, &snapshotRun) != nil {
			return asset.ErrInvalid
		}
		var cfg struct {
			Expected struct {
				Digest string `json:"subject_manifest_digest"`
			} `json:"expected_subject_manifest"`
		}
		_ = json.Unmarshal(snapshotRun.Target.Config.Bytes(), &cfg)
		if cfg.Expected.Digest != f.SubjectDigest {
			return asset.ErrConflict
		}
		var digest string
		if e = tx.QueryRow(ctx, `SELECT intent_digest,snapshot_bytes FROM evaluation_comparisons WHERE project_id=$1 AND id=$2`, s.ProjectID, f.GateID).Scan(&digest, &raw); e != nil {
			return dbError(e)
		}
		var snapshot decision.Snapshot
		if json.Unmarshal(raw, &snapshot) != nil || digest != f.ComparisonDigest {
			return asset.ErrConflict
		}
		bound := false
		for _, source := range []decision.Source{snapshot.Baseline, snapshot.Candidate} {
			for _, unit := range source.Units {
				if unit.RunID == f.RunID && unit.CaseID == f.Case.EntityID && unit.CaseVersion == f.Case.Version {
					for _, metric := range unit.Metrics {
						if metric.ResultID == f.ResultID {
							bound = true
						}
					}
				}
			}
		}
		if !bound {
			return asset.ErrConflict
		}
		f.CreatedAt, e = dbNow(ctx, tx)
		if e != nil {
			return e
		}
		f.Digest = gov.Freeze(f).Digest()
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_stage13_feedback(id,project_id,case_id,case_version,result_id,gate_id,feedback_bytes,intent_digest,feedback_digest) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`, f.ID, s.ProjectID, f.Case.EntityID, f.Case.Version, f.ResultID, f.GateID, gov.Freeze(f).Bytes(), intent, f.Digest)
		out = f
		return e
	})
	return out, err
}
