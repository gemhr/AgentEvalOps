package postgres

import (
	"context"
	"encoding/json"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5"
)

// 一个只读 RepeatableRead 快照内重读原 Comparison/Receipt/Run/Result/Review。
func (k Decisions) ReadCanonicalPair(ctx context.Context, s asset.Scope, id string, frozenAt time.Time) (decision.Snapshot, decision.Receipt, []ev.RunState, error) {
	var snapshot decision.Snapshot
	var receipt decision.Receipt
	states := []ev.RunState{}
	if s.Validate(false) != nil || !asset.ValidID(id) || frozenAt.IsZero() {
		return snapshot, receipt, states, asset.ErrInvalid
	}
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if e != nil {
		return snapshot, receipt, states, e
	}
	defer rollback(ctx, tx)
	snapshot, _, e = comparison(ctx, tx, s, id)
	if e != nil {
		return snapshot, receipt, states, e
	}
	var raw []byte
	var storedDigest string
	e = tx.QueryRow(ctx, `SELECT receipt_bytes,receipt_digest FROM evaluation_gate_receipts WHERE project_id=$1 AND gate_id=$2`, s.ProjectID, id).Scan(&raw, &storedDigest)
	if e != nil {
		return snapshot, receipt, states, dbError(e)
	}
	canonical, e := asset.ParseJSON(raw)
	if e != nil || string(raw) != canonical.String() || canonical.Digest() != storedDigest || json.Unmarshal(raw, &receipt) != nil {
		return snapshot, receipt, states, asset.ErrInvalid
	}
	for _, ref := range []decision.SourceRef{snapshot.Command.Baseline, snapshot.Command.Candidate} {
		if ref.Kind != "RUN_SET" || len(ref.Runs) != 1 {
			return snapshot, receipt, states, asset.ErrInvalid
		}
		var created time.Time
		if e = tx.QueryRow(ctx, `SELECT created_at FROM evaluation_runs WHERE project_id=$1 AND id=$2`, s.ProjectID, ref.Runs[0]).Scan(&created); e != nil {
			return snapshot, receipt, states, dbError(e)
		}
		if !created.After(frozenAt) {
			return snapshot, receipt, states, asset.ErrForbidden
		}
		state, e := decisionRun(ctx, tx, s, ref.Runs[0])
		if e != nil {
			return snapshot, receipt, states, e
		}
		if len(state.Run.Snapshot.Input.Manifest) != 1 {
			return snapshot, receipt, states, asset.ErrInvalid
		}
		if e = verifyIndependentReview(ctx, tx, s, state.Run.Snapshot.Input.Manifest[0], true); e != nil {
			return snapshot, receipt, states, e
		}
		states = append(states, state)
	}
	e = tx.Commit(ctx)
	return snapshot, receipt, states, e
}

func verifyIndependentReview(ctx context.Context, tx pgx.Tx, s asset.Scope, c ev.CaseInput, requirePublication bool) error {
	m, e := catalog.ReadCIDiagnosisMetadata(c.Case.Metadata)
	if e != nil {
		return e
	}
	var content asset.Content[catalog.CaseContent]
	if c.Identity.CanonicalContent.Decode(&content) != nil || content.Source.Kind != "REVIEWED_CASE" || !asset.ValidID(content.Source.Ref) || decisionHash(content.Body) != decisionHash(c.Case) {
		return asset.ErrForbidden
	}
	var draft rv.Draft
	if e = reviewDecode(ctx, tx, s, "evaluation_reviewed_case_drafts", "draft_bytes", content.Source.Ref, &draft); e != nil {
		return e
	}
	item, e := reviewItem(ctx, tx, s, draft.ItemID, false)
	if e != nil {
		return e
	}
	if item.ID != m.ReviewID || item.Command.Criticality != string(c.Case.Criticality) || draft.Case != c.Identity.Ref || draft.GoldenID == nil || draft.Ready(item) != nil || draft.Reviewer.Type != "HUMAN" || draft.Reviewer.Principal != m.Reviewer || draft.Reviewer.Principal == m.Developer || decisionHash(draft.Body) != decisionHash(c.Case) || decisionHash(reviewCaseSource(draft, item)) != decisionHash(content.Source) || item.Source.Digest != m.SourceDigest {
		return asset.ErrForbidden
	}
	var exists bool
	e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM evaluation_review_case_publications WHERE project_id=$1 AND draft_id=$2 AND case_id=$3 AND case_version=$4)`, s.ProjectID, draft.ID, c.Identity.Ref.EntityID, c.Identity.Ref.Version).Scan(&exists)
	if e != nil {
		return e
	}
	if requirePublication && !exists {
		return asset.ErrForbidden
	}
	var golden rv.GoldenLabel
	if e = reviewDecode(ctx, tx, s, "evaluation_golden_labels", "golden_bytes", *draft.GoldenID, &golden); e != nil {
		return e
	}
	if golden.ItemID != item.ID || golden.SourceDigest != item.Source.Digest || golden.Decision.Value == "INCONCLUSIVE" {
		return asset.ErrForbidden
	}
	annotations, e := reviewAnnotations(ctx, tx, s, item.ID)
	if e != nil {
		return e
	}
	if len(annotations) != item.Command.Policy.Reviews || len(annotations) == 0 {
		return asset.ErrForbidden
	}
	for _, a := range annotations {
		if a.Reviewer.Type != "HUMAN" || a.Reviewer.Principal == m.Developer || a.SourceDigest != item.Source.Digest {
			return asset.ErrForbidden
		}
	}
	if m.Purpose == "FAILURE" {
		if item.Source.Ref.Type != "BUSINESS_FEEDBACK" || item.Source.Ref.FeedbackID != m.FeedbackID {
			return asset.ErrForbidden
		}
		var raw []byte
		if e = tx.QueryRow(ctx, `SELECT source_bytes FROM evaluation_business_feedback WHERE project_id=$1 AND id=$2`, s.ProjectID, m.FeedbackID).Scan(&raw); e != nil {
			return dbError(e)
		}
		wire, projection, e := rv.DecodeFeedback(raw)
		if e != nil || wire.Digest != m.ProjectionDigest || decisionHash(projection.Attribution) != m.AttributionDigest || projection.Status != "ACTIVE" || projection.Decision == "WITHDRAW" {
			return asset.ErrForbidden
		}
	}
	return nil
}
