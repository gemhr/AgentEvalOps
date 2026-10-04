package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"slices"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5"
)

func (k Reviews) CreateReviewedCaseDraft(ctx context.Context, s rv.Scope, d rv.Draft) (rv.Draft, error) {
	var out rv.Draft
	if !s.Feedback || !asset.ValidID(d.ID) || !asset.ValidID(d.ItemID) || d.Case.Validate() != nil || !slices.Contains([]string{"NOT_REVIEWED", "APPROVED", "REDACTED", "REJECTED"}, d.Sanitization) || !asset.Text(d.Policy) || !asset.ContentText(d.Reason) {
		return out, asset.ErrInvalid
	}
	// server 填充 provenance，正文是 caller 明确提供的人工版本，不复制 source body。
	d.ProjectID = s.ProjectID
	d.Reviewer = s.Reviewer()
	d.CreatedAt = out.CreatedAt
	d.SourceDigest = ""
	d.AnnotationIDs = nil
	d.AdjudicationID = nil
	d.SanitizedDigest = reviewHash(d.Body)
	intent := reviewHash(d)
	e := k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		item, e := reviewItem(ctx, tx, s.Scope.Scope, d.ItemID, true)
		if e != nil {
			return e
		}
		var oldIntent string
		e = tx.QueryRow(ctx, `SELECT intent_digest FROM evaluation_reviewed_case_drafts WHERE project_id=$1 AND id=$2`, s.ProjectID, d.ID).Scan(&oldIntent)
		if e == nil {
			if oldIntent != intent {
				return asset.ErrConflict
			}
			return reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_reviewed_case_drafts", "draft_bytes", d.ID, &out)
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		if item.Status != "COMPLETED" {
			return asset.ErrConflict
		}
		annotations, e := reviewAnnotations(ctx, tx, s.Scope.Scope, d.ItemID)
		if e != nil {
			return e
		}
		if len(annotations) != item.Command.Policy.Reviews {
			return asset.ErrConflict
		}
		for _, a := range annotations {
			d.AnnotationIDs = append(d.AnnotationIDs, a.ID)
		}
		adjudication, e := latestAdjudication(ctx, tx, s.Scope.Scope, d.ItemID)
		if e != nil {
			return e
		}
		if adjudication != nil && slices.Equal(adjudication.Inputs, d.AnnotationIDs) {
			d.AdjudicationID = &adjudication.ID
		}
		if d.Supersedes != nil {
			var old rv.Draft
			if e = reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_reviewed_case_drafts", "draft_bytes", *d.Supersedes, &old); e != nil {
				return e
			}
			if old.ItemID != d.ItemID || old.Case != d.Case {
				return asset.ErrNotFound
			}
		}
		d.SourceDigest = item.Source.Digest
		if d.GoldenID != nil {
			var g rv.GoldenLabel
			if e = reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_golden_labels", "golden_bytes", *d.GoldenID, &g); e != nil {
				return e
			}
			if g.ItemID != d.ItemID {
				return asset.ErrNotFound
			}
			a, e := reviewAnnotations(ctx, tx, s.Scope.Scope, d.ItemID)
			if e != nil {
				return e
			}
			ids := []string{}
			for _, v := range a {
				ids = append(ids, v.ID)
			}
			if !slices.Equal(ids, g.AnnotationIDs) {
				return asset.ErrConflict
			}
			if g.AdjudicationID != nil {
				latest, e := latestAdjudication(ctx, tx, s.Scope.Scope, d.ItemID)
				if e != nil {
					return e
				}
				if latest == nil || latest.ID != *g.AdjudicationID {
					return asset.ErrConflict
				}
			}
			if d.UseGoldenGroundTruth {
				d.Body.GroundTruth, e = asset.Freeze(struct {
					GoldenID     string
					Schema       asset.Ref
					Origin       string
					Decision     rv.Judgment
					Protocol     rv.Protocol
					Annotations  []string
					Adjudication *string
				}{g.ID, g.Schema, g.Origin, g.Decision, g.Protocol, g.AnnotationIDs, g.AdjudicationID})
				if e != nil {
					return e
				}
			}
		} else if d.UseGoldenGroundTruth {
			return asset.ErrInvalid
		}
		if d.Sanitization == "APPROVED" || d.Sanitization == "REDACTED" {
			if e = d.Ready(item); e != nil {
				return e
			}
		}
		d.SanitizedDigest = reviewHash(d.Body)
		now, e := dbNow(ctx, tx)
		if e != nil {
			return e
		}
		d.CreatedAt = now
		raw, e := asset.FreezeBytes(d)
		if e != nil {
			return e
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_reviewed_case_drafts(id,project_id,item_id,golden_id,supersedes,draft_bytes,intent_digest) VALUES($1,$2,$3,$4,$5,$6,$7)`, d.ID, s.ProjectID, d.ItemID, d.GoldenID, d.Supersedes, raw, intent)
		out = d
		return e
	})
	if e == nil {
		reviewEvent("reviewed_case_draft_created", s.ProjectID, d.ItemID)
	}
	return out, e
}
func (k Reviews) GetReviewedCaseDraft(ctx context.Context, s rv.Scope, id string) (rv.Draft, error) {
	var d rv.Draft
	if !s.Feedback || s.ValidateReviewer() != nil || !asset.ValidID(id) {
		return d, asset.ErrForbidden
	}
	e := reviewDecode(ctx, k.Pool, s.Scope.Scope, "evaluation_reviewed_case_drafts", "draft_bytes", id, &d)
	return d, e
}
func (k Reviews) PublishReviewedCase(ctx context.Context, s rv.Scope, id, name string) (catalog.CaseVersion, error) {
	var published catalog.CaseVersion
	var itemID string
	if !s.Feedback || !s.CanPublish || !asset.ValidID(id) || !asset.Text(name) {
		return published, asset.ErrForbidden
	}
	e := k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		var draft rv.Draft
		if e := reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_reviewed_case_drafts", "draft_bytes", id, &draft); e != nil {
			return e
		}
		item, e := reviewItem(ctx, tx, s.Scope.Scope, draft.ItemID, true)
		if e != nil {
			return e
		}
		itemID = item.ID
		if e = draft.Ready(item); e != nil {
			return e
		}
		annotations, e := reviewAnnotations(ctx, tx, s.Scope.Scope, item.ID)
		if e != nil {
			return e
		}
		ids := []string{}
		for _, a := range annotations {
			ids = append(ids, a.ID)
		}
		if !slices.Equal(ids, draft.AnnotationIDs) {
			return asset.ErrConflict
		}
		if draft.AdjudicationID != nil {
			latest, e := latestAdjudication(ctx, tx, s.Scope.Scope, item.ID)
			if e != nil {
				return e
			}
			if latest == nil || latest.ID != *draft.AdjudicationID {
				return asset.ErrConflict
			}
		}
		// 持有 ReviewItem 锁保护 publish 条件；Catalog owner 在自己的短事务中发布。
		// 两次 commit 间 crash 时 Case.Source 已包含 durable Draft/Review/Observation lineage。
		cases := catalog.CaseService{Store: Cases{Pool: k.Pool}}
		if _, e = cases.CreateCase(ctx, s.Scope.Scope, asset.Create{ID: draft.Case.EntityID, Name: name}); e != nil {
			return e
		}
		published, e = cases.PublishCaseVersion(ctx, s.Scope.Scope, asset.Publish[catalog.CaseContent]{Ref: draft.Case, Body: draft.Body, Source: reviewCaseSource(draft, item)})
		if e != nil {
			return e
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_review_case_publications(project_id,draft_id,case_id,case_version) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, s.ProjectID, id, draft.Case.EntityID, draft.Case.Version)
		return e
	})
	if e == nil {
		reviewEvent("reviewed_case_created", s.ProjectID, itemID)
	}
	return published, e
}

// BuildDatasetRevisionFromReviewedCases 是显式命令；绝不更新旧 DatasetVersion。
func (k Reviews) BuildDatasetRevisionFromReviewedCases(ctx context.Context, s rv.Scope, base, target asset.Ref, draftIDs []string) (catalog.DatasetVersion, error) {
	var result catalog.DatasetVersion
	if !s.Feedback || s.ValidateReviewer() != nil || !s.CanPublish || base.Validate() != nil || target.Validate() != nil || base.EntityID != target.EntityID || base.Version == target.Version || len(draftIDs) == 0 || len(draftIDs) > 1000 {
		return result, asset.ErrInvalid
	}
	ds := catalog.DatasetService{Store: Datasets{Pool: k.Pool}, Cases: Cases{Pool: k.Pool}}
	old, e := ds.GetDatasetVersion(ctx, s.Scope.Scope, base)
	if e != nil {
		return result, e
	}
	refs := slices.Clone(old.Content().Body.Cases)
	seen := map[string]bool{}
	for _, r := range refs {
		seen[r.EntityID] = true
	}
	for _, id := range draftIDs {
		draft, e := k.GetReviewedCaseDraft(ctx, s, id)
		if e != nil {
			return result, e
		}
		var exists bool
		e = k.Pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM evaluation_review_case_publications WHERE project_id=$1 AND draft_id=$2 AND case_id=$3 AND case_version=$4)`, s.ProjectID, id, draft.Case.EntityID, draft.Case.Version).Scan(&exists)
		if e != nil {
			return result, e
		}
		if !exists || seen[draft.Case.EntityID] {
			return result, asset.ErrConflict
		}
		seen[draft.Case.EntityID] = true
		refs = append(refs, draft.Case)
	}
	metadata, e := asset.Freeze(struct {
		Base   asset.Ref
		Drafts []string
	}{base, draftIDs})
	if e != nil {
		return result, e
	}
	// writer fence 在调用 G1 owner 前确认；目录的 published version 自身拥有 scoped immutable guard。
	e = k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		var e error
		result, e = ds.PublishDatasetVersion(ctx, s.Scope.Scope, asset.Publish[catalog.DatasetContent]{Ref: target, Body: catalog.DatasetContent{Cases: refs, Metadata: metadata}, Source: asset.Source{Kind: "REVIEWED_DATASET_REVISION", Ref: target.EntityID + "@" + target.Version, Principal: s.Principal, Metadata: metadata}})
		return e
	})
	if e == nil {
		reviewEvent("dataset_feedback_published", s.ProjectID, target.EntityID)
	}
	return result, e
}
func (k Reviews) ReviewGateException(ctx context.Context, s rv.Scope, c rv.GateException) (rv.GateException, error) {
	var out rv.GateException
	if !s.ApproveException || !asset.ValidID(c.ID) || !asset.ValidID(c.ItemID) || !asset.ValidID(c.GateID) || !asset.Text(c.Code) || !asset.Text(c.Scope) || !asset.ContentText(c.RequestedReason) || !asset.ContentText(c.Reason) || !slices.Contains([]string{"APPROVED", "REJECTED"}, c.Decision) {
		return out, asset.ErrInvalid
	}
	c.ProjectID = s.ProjectID
	c.Approver = s.Reviewer()
	c.CreatedAt = out.CreatedAt
	intent := reviewHash(c)
	e := k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		item, e := reviewItem(ctx, tx, s.Scope.Scope, c.ItemID, true)
		if e != nil {
			return e
		}
		var existing string
		e = tx.QueryRow(ctx, `SELECT intent_digest FROM evaluation_gate_exception_reviews WHERE project_id=$1 AND id=$2`, s.ProjectID, c.ID).Scan(&existing)
		if e == nil {
			if existing != intent {
				return asset.ErrConflict
			}
			return reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_gate_exception_reviews", "review_bytes", c.ID, &out)
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		if item.Status != "COMPLETED" || item.Source.Ref.Type != "GATE_DECISION" || item.Source.Ref.GateID != c.GateID {
			return asset.ErrConflict
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return e
		}
		if !c.Expiry.After(now) {
			return asset.ErrInvalid
		}
		var raw []byte
		var receipt decision.Receipt
		e = tx.QueryRow(ctx, `SELECT receipt_bytes FROM evaluation_gate_receipts WHERE project_id=$1 AND gate_id=$2`, s.ProjectID, c.GateID).Scan(&raw)
		if e != nil {
			return dbError(e)
		}
		if e = json.Unmarshal(raw, &receipt); e != nil {
			return e
		}
		matched := false
		for _, i := range receipt.Issues {
			if i.Code == c.Code && i.Scope == c.Scope {
				matched = true
				if c.Decision == "APPROVED" && (i.Decision != decision.Fail || receipt.Decision == decision.Blocked) {
					return asset.ErrForbidden
				}
			}
		}
		if !matched {
			return asset.ErrInvalid
		}
		annotations, e := reviewAnnotations(ctx, tx, s.Scope.Scope, c.ItemID)
		if e != nil {
			return e
		}
		ids := []string{}
		for _, a := range annotations {
			ids = append(ids, a.ID)
		}
		final := rv.Judgment{}
		adjudication, e := latestAdjudication(ctx, tx, s.Scope.Scope, c.ItemID)
		if e != nil {
			return e
		}
		if adjudication != nil && slices.Equal(adjudication.Inputs, ids) {
			final = adjudication.Decision
		} else if len(annotations) == 1 || len(annotations) == 2 && rv.Consensus(annotations[0], annotations[1]) {
			final = annotations[0].Decision
		}
		if c.Decision == "APPROVED" && (final.Kind != rv.Quality || final.Value != "PASS") {
			return asset.ErrForbidden
		}
		c.CreatedAt = now
		raw, e = asset.FreezeBytes(c)
		if e != nil {
			return e
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_gate_exception_reviews(id,project_id,item_id,gate_id,review_bytes,intent_digest) VALUES($1,$2,$3,$4,$5,$6)`, c.ID, s.ProjectID, c.ItemID, c.GateID, raw, intent)
		out = c
		return e
	})
	if e == nil {
		reviewEvent("gate_exception_reviewed", s.ProjectID, c.ItemID)
	}
	return out, e
}
func (k Reviews) AcceptedExceptionProof(ctx context.Context, s rv.Scope, id string) (decision.Exception, error) {
	var c rv.GateException
	if !s.ApproveException || s.ValidateReviewer() != nil || !asset.ValidID(id) {
		return decision.Exception{}, asset.ErrForbidden
	}
	e := reviewDecode(ctx, k.Pool, s.Scope.Scope, "evaluation_gate_exception_reviews", "review_bytes", id, &c)
	if e != nil {
		return decision.Exception{}, e
	}
	var fresh bool
	e = k.Pool.QueryRow(ctx, "SELECT $1::timestamptz>clock_timestamp()", c.Expiry).Scan(&fresh)
	if e != nil {
		return decision.Exception{}, e
	}
	if c.Decision != "APPROVED" || !fresh {
		return decision.Exception{}, asset.ErrForbidden
	}
	return decision.Exception{ReasonCode: c.Code, Scope: c.Scope, Actor: c.Approver.Principal, Reason: c.Reason, Proof: "review:" + c.ID + ":" + reviewHash(c), ExpiresAt: &c.Expiry}, nil
}
