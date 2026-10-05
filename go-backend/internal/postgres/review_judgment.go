package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"slices"

	"agentevalops/go-backend/internal/asset"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5"
)

func (k Reviews) AdjudicateReview(ctx context.Context, s rv.Scope, id, itemID string, j rv.Judgment, reason string, supersedes *string) (rv.Adjudication, error) {
	var out rv.Adjudication
	if !s.Adjudicate || !asset.ValidID(id) || !asset.ValidID(itemID) || !asset.ContentText(reason) || supersedes != nil && !asset.ValidID(*supersedes) {
		return out, asset.ErrInvalid
	}
	intent := reviewHash(struct {
		Item       string
		Decision   rv.Judgment
		Reason     string
		Supersedes *string
		Reviewer   rv.Reviewer
	}{itemID, j, reason, supersedes, s.Reviewer()})
	e := k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		item, e := reviewItem(ctx, tx, s.Scope.Scope, itemID, true)
		if e != nil {
			return e
		}
		var oldIntent string
		e = tx.QueryRow(ctx, `SELECT intent_digest FROM evaluation_adjudications WHERE project_id=$1 AND id=$2`, s.ProjectID, id).Scan(&oldIntent)
		if e == nil {
			if oldIntent != intent {
				return asset.ErrConflict
			}
			return reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_adjudications", "adjudication_bytes", id, &out)
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		if j.Kind != item.Command.Kind || j.Validate(item.Schema, item.Source.Evidence) != nil {
			return asset.ErrInvalid
		}
		if supersedes == nil && item.Status != "ADJUDICATION_REQUIRED" {
			return asset.ErrConflict
		}
		if supersedes != nil {
			var old rv.Adjudication
			if e = reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_adjudications", "adjudication_bytes", *supersedes, &old); e != nil {
				return e
			}
			if old.ItemID != itemID {
				return asset.ErrNotFound
			}
			var replaced bool
			if e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM evaluation_adjudications WHERE project_id=$1 AND supersedes=$2)`, s.ProjectID, *supersedes).Scan(&replaced); e != nil {
				return e
			}
			if replaced {
				return asset.ErrConflict
			}
		}
		annotations, e := reviewAnnotations(ctx, tx, s.Scope.Scope, itemID)
		if e != nil {
			return e
		}
		if len(annotations) != item.Command.Policy.Reviews {
			return asset.ErrConflict
		}
		inputs := []string{}
		for _, a := range annotations {
			if a.Reviewer.ID == s.Principal {
				return asset.ErrForbidden
			}
			inputs = append(inputs, a.ID)
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return e
		}
		out = rv.Adjudication{ID: id, ProjectID: s.ProjectID, ItemID: itemID, Inputs: inputs, Reviewer: s.Reviewer(), Decision: j, Reason: reason, Supersedes: supersedes, CreatedAt: now}
		raw, e := asset.FreezeBytes(out)
		if e != nil {
			return e
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_adjudications(id,project_id,item_id,intent_digest,adjudication_bytes,supersedes) VALUES($1,$2,$3,$4,$5,$6)`, id, s.ProjectID, itemID, intent, raw, supersedes)
		if e != nil {
			return e
		}
		for _, input := range inputs {
			if _, e = tx.Exec(ctx, `INSERT INTO evaluation_adjudication_inputs(project_id,adjudication_id,annotation_id) VALUES($1,$2,$3)`, s.ProjectID, id, input); e != nil {
				return e
			}
		}
		_, e = tx.Exec(ctx, `UPDATE evaluation_review_items SET status='COMPLETED' WHERE project_id=$1 AND id=$2`, s.ProjectID, itemID)
		return e
	})
	if e == nil {
		reviewEvent("review_adjudicated", s.ProjectID, itemID)
	}
	return out, e
}
func latestAdjudication(ctx context.Context, q queryer, s asset.Scope, item string) (*rv.Adjudication, error) {
	var raw []byte
	e := q.QueryRow(ctx, `SELECT adjudication_bytes FROM evaluation_adjudications a WHERE project_id=$1 AND item_id=$2 AND NOT EXISTS(SELECT 1 FROM evaluation_adjudications n WHERE n.project_id=a.project_id AND n.supersedes=a.id) ORDER BY created_at DESC,id DESC LIMIT 1`, s.ProjectID, item).Scan(&raw)
	if errors.Is(e, pgx.ErrNoRows) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	var a rv.Adjudication
	e = json.Unmarshal(raw, &a)
	return &a, e
}
func (k Reviews) PublishGoldenLabel(ctx context.Context, s rv.Scope, id, itemID string) (rv.GoldenLabel, error) {
	var out rv.GoldenLabel
	if !s.PublishGolden || !asset.ValidID(id) || !asset.ValidID(itemID) {
		return out, asset.ErrInvalid
	}
	e := k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		item, e := reviewItem(ctx, tx, s.Scope.Scope, itemID, true)
		if e != nil {
			return e
		}
		if item.Status != "COMPLETED" {
			return asset.ErrConflict
		}
		annotations, e := reviewAnnotations(ctx, tx, s.Scope.Scope, itemID)
		if e != nil {
			return e
		}
		if len(annotations) != item.Command.Policy.Reviews {
			return asset.ErrConflict
		}
		out = rv.GoldenLabel{ID: id, ProjectID: s.ProjectID, ItemID: itemID, Schema: item.Command.Schema, SchemaDigest: item.SchemaDigest, SourceDigest: item.Source.Digest, EvidenceDigest: item.Source.EvidenceDigest, Case: item.Source.Case, Protocol: item.Command.Policy.Protocol}
		for _, a := range annotations {
			out.AnnotationIDs = append(out.AnnotationIDs, a.ID)
		}
		adjudication, e := latestAdjudication(ctx, tx, s.Scope.Scope, itemID)
		if e != nil {
			return e
		}
		if adjudication != nil && slices.Equal(adjudication.Inputs, out.AnnotationIDs) {
			out.Origin = "ADJUDICATED"
			out.AdjudicationID = &adjudication.ID
			out.Decision = adjudication.Decision
		} else if len(annotations) == 2 && rv.Consensus(annotations[0], annotations[1]) {
			out.Origin = "CONSENSUS"
			out.Decision = annotations[0].Decision
		} else if len(annotations) == 1 && annotations[0].Reviewer.Type == "SYSTEM_IMPORT" && annotations[0].Supersedes == nil {
			out.Origin = "TRUSTED_IMPORT"
			out.Decision = annotations[0].Decision
		} else {
			return asset.ErrConflict
		}
		normalized := out
		normalized.ID = ""
		intent := reviewHash(normalized)
		var oldIntent string
		e = tx.QueryRow(ctx, `SELECT intent_digest FROM evaluation_golden_labels WHERE project_id=$1 AND id=$2`, s.ProjectID, id).Scan(&oldIntent)
		if e == nil {
			if oldIntent != intent {
				return asset.ErrConflict
			}
			return reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_golden_labels", "golden_bytes", id, &out)
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		var oldID string
		e = tx.QueryRow(ctx, `SELECT id::text FROM evaluation_golden_labels WHERE project_id=$1 AND item_id=$2 AND intent_digest=$3`, s.ProjectID, itemID, intent).Scan(&oldID)
		if e == nil {
			return reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_golden_labels", "golden_bytes", oldID, &out)
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return e
		}
		out.CreatedAt = now
		raw, e := asset.FreezeBytes(out)
		if e != nil {
			return e
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_golden_labels(id,project_id,item_id,intent_digest,golden_bytes,adjudication_id) VALUES($1,$2,$3,$4,$5,$6)`, id, s.ProjectID, itemID, intent, raw, out.AdjudicationID)
		if e != nil {
			return e
		}
		for _, a := range annotations {
			if _, e = tx.Exec(ctx, `INSERT INTO evaluation_golden_annotation_inputs(project_id,golden_id,annotation_id) VALUES($1,$2,$3)`, s.ProjectID, id, a.ID); e != nil {
				return e
			}
		}
		return nil
	})
	if e == nil {
		reviewEvent("golden_label_published", s.ProjectID, itemID)
	}
	return out, e
}

func (k Reviews) GetReview(ctx context.Context, s rv.Scope, id string) (rv.ReadModel, error) {
	var out rv.ReadModel
	if s.ValidateReviewer() != nil || !asset.ValidID(id) || !s.Review && !s.Adjudicate && !s.Queue {
		return out, asset.ErrForbidden
	}
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if e != nil {
		return out, e
	}
	defer rollback(ctx, tx)
	item, e := reviewItem(ctx, tx, s.Scope.Scope, id, false)
	if e != nil {
		return out, e
	}
	out.Item = publicReviewItem(item)
	out.Slots, e = reviewSlots(ctx, tx, s.Scope.Scope, id)
	if e != nil {
		return out, e
	}
	for i := range out.Slots {
		if out.Slots[i].Reviewer == nil || out.Slots[i].Reviewer.ID != s.Principal {
			out.Slots[i].Token = ""
		}
		if item.Command.Policy.Reviews == 2 && item.Status == "IN_REVIEW" {
			if out.Slots[i].Reviewer != nil && out.Slots[i].Reviewer.ID != s.Principal {
				out.Slots[i].Reviewer = nil
			}
		}
	}
	// 双人独立协议下双方未完成时，连 caller 自己已提交的结果也不返回。
	if item.Status == "COMPLETED" || item.Status == "ADJUDICATION_REQUIRED" {
		rows, e := tx.Query(ctx, `SELECT annotation_bytes FROM evaluation_human_annotations WHERE project_id=$1 AND item_id=$2 ORDER BY created_at,id`, s.ProjectID, id)
		if e != nil {
			return out, e
		}
		for rows.Next() {
			var raw []byte
			var a rv.Annotation
			if e = rows.Scan(&raw); e != nil {
				rows.Close()
				return out, e
			}
			if e = json.Unmarshal(raw, &a); e != nil {
				rows.Close()
				return out, e
			}
			out.Annotations = append(out.Annotations, a)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return out, e
		}
		rows, e = tx.Query(ctx, `SELECT adjudication_bytes FROM evaluation_adjudications WHERE project_id=$1 AND item_id=$2 ORDER BY created_at,id`, s.ProjectID, id)
		if e != nil {
			return out, e
		}
		for rows.Next() {
			var raw []byte
			var a rv.Adjudication
			if e = rows.Scan(&raw); e != nil {
				rows.Close()
				return out, e
			}
			if e = json.Unmarshal(raw, &a); e != nil {
				rows.Close()
				return out, e
			}
			out.Adjudications = append(out.Adjudications, a)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return out, e
		}
		rows, e = tx.Query(ctx, `SELECT golden_bytes FROM evaluation_golden_labels WHERE project_id=$1 AND item_id=$2 ORDER BY created_at,id`, s.ProjectID, id)
		if e != nil {
			return out, e
		}
		for rows.Next() {
			var raw []byte
			var g rv.GoldenLabel
			if e = rows.Scan(&raw); e != nil {
				rows.Close()
				return out, e
			}
			if e = json.Unmarshal(raw, &g); e != nil {
				rows.Close()
				return out, e
			}
			out.Golden = append(out.Golden, g)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return out, e
		}
	}
	return out, tx.Commit(ctx)
}
func (k Reviews) ListReviewQueue(ctx context.Context, s rv.Scope, after string, limit int) ([]rv.Item, error) {
	if s.ValidateReviewer() != nil || !s.Queue && !s.Review || limit < 1 || limit > 101 || after != "" && !asset.ValidID(after) {
		return nil, asset.ErrInvalid
	}
	rows, e := k.Pool.Query(ctx, `SELECT r.id::text FROM evaluation_review_items r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND r.status IN ('PENDING','IN_REVIEW','ADJUDICATION_REQUIRED') AND ($3::uuid IS NULL OR r.id>$3) ORDER BY r.id LIMIT $4`, s.ProjectID, s.OrganizationID, nullableID(after), limit)
	if e != nil {
		return nil, e
	}
	ids := []string{}
	for rows.Next() {
		var id string
		if e = rows.Scan(&id); e != nil {
			rows.Close()
			return nil, e
		}
		ids = append(ids, id)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	out := []rv.Item{}
	for _, id := range ids {
		item, e := reviewItem(ctx, k.Pool, s.Scope.Scope, id, false)
		if e != nil {
			return nil, e
		}
		out = append(out, publicReviewItem(item))
	}
	return out, nil
}
func (k Reviews) GetHumanCoverage(ctx context.Context, s rv.Scope) (rv.Coverage, error) {
	var c rv.Coverage
	if s.ValidateReviewer() != nil || !s.Queue && !s.Calibrate {
		return c, asset.ErrForbidden
	}
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if e != nil {
		return c, e
	}
	defer rollback(ctx, tx)
	var exists bool
	e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM projects WHERE id=$1 AND org_id=$2)`, s.ProjectID, s.OrganizationID).Scan(&exists)
	if e != nil {
		return c, e
	}
	if !exists {
		return c, asset.ErrNotFound
	}
	e = tx.QueryRow(ctx, `SELECT count(*) FROM evaluation_review_items WHERE project_id=$1`, s.ProjectID).Scan(&c.Eligible)
	if e != nil {
		return c, e
	}
	e = tx.QueryRow(ctx, `SELECT count(*) FILTER(WHERE status='CLAIMED'),count(*) FILTER(WHERE status='COMPLETED'),coalesce(sum(expired),0),coalesce(sum(released),0) FROM evaluation_review_slots WHERE project_id=$1`, s.ProjectID).Scan(&c.Assigned, &c.Submitted, &c.Expired, &c.Released)
	if e != nil {
		return c, e
	}
	e = tx.QueryRow(ctx, `SELECT count(DISTINCT item_id) FROM evaluation_adjudications WHERE project_id=$1`, s.ProjectID).Scan(&c.Adjudicated)
	if e != nil {
		return c, e
	}
	e = tx.QueryRow(ctx, `SELECT count(DISTINCT g.item_id) FROM evaluation_golden_labels g JOIN evaluation_review_items i ON i.project_id=g.project_id AND i.id=g.item_id WHERE g.project_id=$1 AND i.status='COMPLETED'
 AND NOT EXISTS(SELECT 1 FROM evaluation_golden_annotation_inputs gi JOIN evaluation_human_annotations n ON n.project_id=gi.project_id AND n.supersedes=gi.annotation_id WHERE gi.project_id=g.project_id AND gi.golden_id=g.id)
 AND NOT EXISTS(SELECT 1 FROM evaluation_adjudications n WHERE n.project_id=g.project_id AND n.supersedes=g.adjudication_id)`, s.ProjectID).Scan(&c.Golden)
	if e != nil {
		return c, e
	}
	rows, e := tx.Query(ctx, `SELECT id::text FROM evaluation_review_items WHERE project_id=$1`, s.ProjectID)
	if e != nil {
		return c, e
	}
	ids := []string{}
	for rows.Next() {
		var id string
		if e = rows.Scan(&id); e != nil {
			rows.Close()
			return c, e
		}
		ids = append(ids, id)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return c, e
	}
	for _, id := range ids {
		a, e := reviewAnnotations(ctx, tx, s.Scope.Scope, id)
		if e != nil {
			return c, e
		}
		if len(a) == 2 {
			c.IndependentPairs++
			if rv.Consensus(a[0], a[1]) {
				c.AgreedPairs++
			}
		}
	}
	c.Missing = c.Eligible - c.Golden
	c.HumanLabelCoverage = rv.Ratio(c.Golden, c.Eligible)
	c.ReviewerAgreement = rv.Ratio(c.AgreedPairs, c.IndependentPairs)
	return c, tx.Commit(ctx)
}
