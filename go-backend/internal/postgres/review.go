package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"slices"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Reviews struct {
	Pool                   *pgxpool.Pool
	DefaultLease, MaxLease time.Duration
}

func reviewEvent(event, project, item string) {
	slog.Info(event, "event", event, "project_id", project, "review_item_id", item)
}
func (k Reviews) transaction(ctx context.Context, s rv.Scope, admission bool, fn func(pgx.Tx) error) error {
	if s.ValidateReviewer() != nil {
		return asset.ErrForbidden
	}
	r, e := (Evaluation{Pool: k.Pool}).transact(ctx, s.Scope, "", admission, func(tx pgx.Tx, _ ev.Run, _ string) (ev.Reply, error) {
		e := fn(tx)
		return ev.Reply{Code: ev.Applied}, e
	})
	if e != nil {
		return dbError(e)
	}
	switch r.Code {
	case ev.Applied, ev.AlreadyApplied:
		return nil
	case ev.NotFound:
		return asset.ErrNotFound
	case ev.OwnershipLost:
		return rv.ErrOwnershipLost
	default:
		return asset.ErrForbidden
	}
}
func reviewItem(ctx context.Context, q queryer, s asset.Scope, id string, lock bool) (rv.Item, error) {
	var item rv.Item
	var raw []byte
	var status string
	var at time.Time
	sql := `SELECT item_bytes,status,r.created_at FROM evaluation_review_items r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND r.id=$3`
	if lock {
		sql += " FOR UPDATE OF r"
	}
	e := q.QueryRow(ctx, sql, s.ProjectID, s.OrganizationID, id).Scan(&raw, &status, &at)
	if e != nil {
		return item, dbError(e)
	}
	e = json.Unmarshal(raw, &item)
	item.Status = status
	item.CreatedAt = at
	return item, e
}
func (k Reviews) EnqueueReview(ctx context.Context, s rv.Scope, c rv.Enqueue) (rv.Item, error) {
	var saved rv.Item
	if !s.Queue || c.Validate() != nil {
		return saved, asset.ErrInvalid
	}
	if !c.Policy.Sampling.Selected(s.ProjectID, c.Source) {
		return saved, asset.ErrNotFound
	}
	key := reviewHash(c.Source)
	policy := reviewHash(struct {
		Schema asset.Ref
		Kind   rv.DecisionKind
		Policy rv.Policy
	}{c.Schema, c.Kind, c.Policy})
	normalized := c
	normalized.ID = ""
	intent := reviewHash(struct {
		Command rv.Enqueue
		Actor   rv.Reviewer
	}{normalized, s.Reviewer()})
	e := k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		if _, e := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", s.ProjectID+":"+key+":"+policy); e != nil {
			return e
		}
		var id, oldIntent string
		e := tx.QueryRow(ctx, `SELECT id::text,intent_digest FROM evaluation_review_items WHERE project_id=$1 AND source_key=$2 AND policy_digest=$3`, s.ProjectID, key, policy).Scan(&id, &oldIntent)
		if e == nil {
			if oldIntent != intent {
				return asset.ErrConflict
			}
			saved, e = reviewItem(ctx, tx, s.Scope.Scope, id, false)
			return e
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		schema, e := loadVersion[metric.Definition](ctx, tx, metricTables, s.Scope.Scope, c.Schema)
		if e != nil {
			return e
		}
		if !slices.Equal(schema.Content().Body.Labels, rv.Labels(c.Kind)) || schema.Content().Body.ValueType != metric.Enum {
			return asset.ErrInvalid
		}
		source, e := resolveReviewSource(ctx, tx, s.Scope.Scope, c.Source)
		if e != nil {
			return e
		}
		saved = rv.Item{ID: c.ID, ProjectID: s.ProjectID, Command: c, Source: source, Schema: schema.Content().Body, SchemaDigest: schema.ContentDigest(), Status: "PENDING"}
		var offline, online, observation, gate *string
		switch c.Source.Type {
		case "OFFLINE_RESULT", "CALIBRATION_SAMPLE":
			offline = &c.Source.ResultID
		case "ONLINE_RESULT":
			online = &c.Source.ResultID
		case "FAILURE_CANDIDATE":
			if c.Source.ResultID != "" {
				online = &c.Source.ResultID
			}
		case "GATE_DECISION":
			gate = &c.Source.GateID
		}
		if source.ObservationID != "" {
			observation = &source.ObservationID
		}
		raw, e := asset.FreezeBytes(saved)
		if e != nil {
			return e
		}
		if c.Source.Type == "CONTROLLED_CASE" {
			_, e = tx.Exec(ctx, `INSERT INTO evaluation_review_items(id,project_id,schema_id,schema_version,source_type,source_key,policy_digest,intent_digest,source_digest,item_bytes,controlled_case_id,controlled_case_version) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)`, c.ID, s.ProjectID, c.Schema.EntityID, c.Schema.Version, c.Source.Type, key, policy, intent, source.Digest, raw, c.Source.Case.EntityID, c.Source.Case.Version)
		} else {
			_, e = tx.Exec(ctx, `INSERT INTO evaluation_review_items(id,project_id,schema_id,schema_version,source_type,source_key,policy_digest,intent_digest,source_digest,item_bytes,offline_result_id,online_result_id,observation_id,gate_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)`, c.ID, s.ProjectID, c.Schema.EntityID, c.Schema.Version, c.Source.Type, key, policy, intent, source.Digest, raw, offline, online, observation, gate)
		}
		if e != nil {
			return e
		}
		for i := 1; i <= c.Policy.Reviews; i++ {
			if _, e = tx.Exec(ctx, `INSERT INTO evaluation_review_slots(project_id,item_id,slot) VALUES($1,$2,$3)`, s.ProjectID, c.ID, i); e != nil {
				return e
			}
		}
		saved, e = reviewItem(ctx, tx, s.Scope.Scope, c.ID, false)
		return e
	})
	if e == nil {
		reviewEvent("review_item_created", s.ProjectID, saved.ID)
	}
	return publicReviewItem(saved), e
}
func publicReviewItem(item rv.Item) rv.Item {
	if item.Command.Policy.Protocol.Blind {
		item.Source.Automatic = nil
	}
	return item
}
func (k Reviews) lease(d time.Duration) (time.Duration, error) {
	if d == 0 {
		d = k.DefaultLease
		if d == 0 {
			d = 30 * time.Minute
		}
	}
	max := k.MaxLease
	if max == 0 {
		max = 24 * time.Hour
	}
	if d < time.Millisecond || d > max {
		return 0, asset.ErrInvalid
	}
	return d, nil
}
func reviewSlots(ctx context.Context, q queryer, s asset.Scope, item string) ([]rv.Slot, error) {
	rows, e := q.Query(ctx, `SELECT slot,reviewer_bytes,token::text,lease_expires_at,status,claims,expired,released FROM evaluation_review_slots WHERE project_id=$1 AND item_id=$2 ORDER BY slot`, s.ProjectID, item)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []rv.Slot{}
	for rows.Next() {
		var v rv.Slot
		var raw []byte
		var token *string
		if e = rows.Scan(&v.Number, &raw, &token, &v.Lease, &v.Status, &v.Claims, &v.Expired, &v.Released); e != nil {
			return nil, e
		}
		if token != nil {
			v.Token = *token
		}
		if raw != nil {
			v.Reviewer = &rv.Reviewer{}
			if e = json.Unmarshal(raw, v.Reviewer); e != nil {
				return nil, e
			}
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
func (k Reviews) ClaimReview(ctx context.Context, s rv.Scope, id string, d time.Duration) (rv.Claim, error) {
	var claim rv.Claim
	duration, e := k.lease(d)
	if e != nil || !s.Review || !asset.ValidID(id) {
		return claim, asset.ErrInvalid
	}
	e = k.transaction(ctx, s, true, func(tx pgx.Tx) error {
		item, e := reviewItem(ctx, tx, s.Scope.Scope, id, true)
		if e != nil {
			return e
		}
		if item.Status == "COMPLETED" || item.Status == "ADJUDICATION_REQUIRED" {
			return asset.ErrConflict
		}
		// 失效只释放 slot；已提交 Annotation 永不撤销。
		_, e = tx.Exec(ctx, `UPDATE evaluation_review_slots SET status='PENDING',reviewer_id=NULL,reviewer_bytes=NULL,token=NULL,writer_epoch=NULL,lease_expires_at=NULL,expired=expired+1 WHERE project_id=$1 AND item_id=$2 AND status='CLAIMED' AND lease_expires_at<=clock_timestamp()`, s.ProjectID, id)
		if e != nil {
			return e
		}
		slots, e := reviewSlots(ctx, tx, s.Scope.Scope, id)
		if e != nil {
			return e
		}
		for _, v := range slots {
			if v.Reviewer != nil && v.Reviewer.ID == s.Principal {
				return asset.ErrConflict
			}
		}
		number := 0
		for _, v := range slots {
			if v.Status == "PENDING" {
				number = v.Number
				break
			}
		}
		if number == 0 {
			return asset.ErrConflict
		}
		token := asset.NewID()
		raw, e := asset.FreezeBytes(s.Reviewer())
		if e != nil {
			return e
		}
		claim = rv.Claim{ItemID: id, Slot: number, Token: token}
		e = tx.QueryRow(ctx, `UPDATE evaluation_review_slots SET status='CLAIMED',reviewer_id=$4,reviewer_bytes=$5,token=$6,writer_epoch=$7,lease_expires_at=clock_timestamp()+$8::bigint*interval '1 millisecond',claims=claims+1 WHERE project_id=$1 AND item_id=$2 AND slot=$3 AND status='PENDING' RETURNING lease_expires_at`, s.ProjectID, id, number, s.Principal, raw, token, s.Epoch, duration.Milliseconds()).Scan(&claim.Lease)
		if e != nil {
			return e
		}
		_, e = tx.Exec(ctx, `UPDATE evaluation_review_items SET status='IN_REVIEW' WHERE project_id=$1 AND id=$2`, s.ProjectID, id)
		return e
	})
	if e == nil {
		reviewEvent("review_claimed", s.ProjectID, id)
	}
	return claim, e
}
func reviewOwned(ctx context.Context, tx pgx.Tx, s rv.Scope, c rv.Claim) error {
	var ok bool
	e := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM evaluation_review_slots WHERE project_id=$1 AND item_id=$2 AND slot=$3 AND reviewer_id=$4 AND token=$5 AND writer_epoch=$6 AND status='CLAIMED' AND lease_expires_at>clock_timestamp())`, s.ProjectID, c.ItemID, c.Slot, s.Principal, c.Token, s.Epoch).Scan(&ok)
	if e != nil {
		return e
	}
	if !ok {
		return rv.ErrOwnershipLost
	}
	return nil
}
func (k Reviews) RenewReview(ctx context.Context, s rv.Scope, c rv.Claim, d time.Duration) (rv.Claim, error) {
	duration, e := k.lease(d)
	if e != nil || !s.Review || !asset.ValidID(c.ItemID) || !asset.ValidID(c.Token) {
		return c, asset.ErrInvalid
	}
	e = k.transaction(ctx, s, false, func(tx pgx.Tx) error {
		if _, e := reviewItem(ctx, tx, s.Scope.Scope, c.ItemID, true); e != nil {
			return e
		}
		if e := reviewOwned(ctx, tx, s, c); e != nil {
			return e
		}
		e := tx.QueryRow(ctx, `UPDATE evaluation_review_slots SET lease_expires_at=clock_timestamp()+$7::bigint*interval '1 millisecond' WHERE project_id=$1 AND item_id=$2 AND slot=$3 AND reviewer_id=$4 AND token=$5 AND writer_epoch=$6 AND status='CLAIMED' AND lease_expires_at>clock_timestamp() RETURNING lease_expires_at`, s.ProjectID, c.ItemID, c.Slot, s.Principal, c.Token, s.Epoch, duration.Milliseconds()).Scan(&c.Lease)
		if errors.Is(e, pgx.ErrNoRows) {
			return rv.ErrOwnershipLost
		}
		return e
	})
	return c, e
}
func (k Reviews) ReleaseReview(ctx context.Context, s rv.Scope, c rv.Claim) error {
	if !s.Review || !asset.ValidID(c.ItemID) || !asset.ValidID(c.Token) {
		return asset.ErrInvalid
	}
	return k.transaction(ctx, s, false, func(tx pgx.Tx) error {
		if _, e := reviewItem(ctx, tx, s.Scope.Scope, c.ItemID, true); e != nil {
			return e
		}
		if e := reviewOwned(ctx, tx, s, c); e != nil {
			return e
		}
		tag, e := tx.Exec(ctx, `UPDATE evaluation_review_slots SET status='PENDING',reviewer_id=NULL,reviewer_bytes=NULL,token=NULL,writer_epoch=NULL,lease_expires_at=NULL,released=released+1 WHERE project_id=$1 AND item_id=$2 AND slot=$3 AND reviewer_id=$4 AND token=$5 AND status='CLAIMED' AND lease_expires_at>clock_timestamp()`, s.ProjectID, c.ItemID, c.Slot, s.Principal, c.Token)
		if e != nil {
			return e
		}
		if tag.RowsAffected() == 0 {
			return rv.ErrOwnershipLost
		}
		return nil
	})
}
func reviewAnnotations(ctx context.Context, q queryer, s asset.Scope, item string) ([]rv.Annotation, error) {
	rows, e := q.Query(ctx, `SELECT annotation_bytes FROM evaluation_human_annotations a WHERE project_id=$1 AND item_id=$2 AND NOT EXISTS(SELECT 1 FROM evaluation_human_annotations n WHERE n.project_id=a.project_id AND n.supersedes=a.id) ORDER BY slot`, s.ProjectID, item)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []rv.Annotation{}
	for rows.Next() {
		var raw []byte
		var a rv.Annotation
		if e = rows.Scan(&raw); e != nil {
			return nil, e
		}
		if e = json.Unmarshal(raw, &a); e != nil {
			return nil, e
		}
		out = append(out, a)
	}
	return out, rows.Err()
}
func updateReviewState(ctx context.Context, tx pgx.Tx, s asset.Scope, item rv.Item) error {
	annotations, e := reviewAnnotations(ctx, tx, s, item.ID)
	if e != nil {
		return e
	}
	status := "IN_REVIEW"
	if len(annotations) == item.Command.Policy.Reviews {
		status = "COMPLETED"
		if len(annotations) == 2 && !rv.Consensus(annotations[0], annotations[1]) {
			status = "ADJUDICATION_REQUIRED"
		}
	}
	_, e = tx.Exec(ctx, `UPDATE evaluation_review_items SET status=$3 WHERE project_id=$1 AND id=$2`, s.ProjectID, item.ID, status)
	return e
}
func (k Reviews) SubmitAnnotation(ctx context.Context, s rv.Scope, c rv.Submit) (rv.Annotation, error) {
	var annotation rv.Annotation
	disagreement := false
	if !s.Review || !asset.ValidID(c.ID) || !asset.ValidID(c.ItemID) || !asset.ValidID(c.Token) {
		return annotation, asset.ErrInvalid
	}
	intent := reviewHash(struct {
		Command  rv.Submit
		Reviewer rv.Reviewer
	}{c, s.Reviewer()})
	e := k.transaction(ctx, s, false, func(tx pgx.Tx) error {
		item, e := reviewItem(ctx, tx, s.Scope.Scope, c.ItemID, true)
		if e != nil {
			return e
		}
		var oldIntent string
		e = tx.QueryRow(ctx, `SELECT intent_digest,annotation_bytes FROM evaluation_human_annotations WHERE project_id=$1 AND id=$2`, s.ProjectID, c.ID).Scan(&oldIntent, new([]byte))
		if e == nil {
			if oldIntent != intent {
				return asset.ErrConflict
			}
			return reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_human_annotations", "annotation_bytes", c.ID, &annotation)
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		if c.SourceDigest != item.Source.Digest || c.SchemaDigest != item.SchemaDigest || c.Decision.Kind != item.Command.Kind || c.Decision.Validate(item.Schema, item.Source.Evidence) != nil {
			return asset.ErrInvalid
		}
		claim := rv.Claim{ItemID: c.ItemID, Slot: c.Slot, Token: c.Token}
		if e = reviewOwned(ctx, tx, s, claim); e != nil {
			return e
		}
		now, e := dbNow(ctx, tx)
		if e != nil {
			return e
		}
		annotation = rv.Annotation{ID: c.ID, ProjectID: s.ProjectID, ItemID: c.ItemID, Slot: c.Slot, Reviewer: s.Reviewer(), Schema: item.Command.Schema, SchemaDigest: item.SchemaDigest, SourceDigest: item.Source.Digest, EvidenceDigest: item.Source.EvidenceDigest, EvidencePresented: item.Source.Evidence, Protocol: item.Command.Policy.Protocol, Decision: c.Decision, CreatedAt: now, SubmittedAt: now}
		raw, e := asset.FreezeBytes(annotation)
		if e != nil {
			return e
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_human_annotations(id,project_id,item_id,slot,reviewer_id,token,intent_digest,annotation_bytes) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, c.ID, s.ProjectID, c.ItemID, c.Slot, s.Principal, c.Token, intent, raw)
		if e != nil {
			return e
		}
		tag, e := tx.Exec(ctx, `UPDATE evaluation_review_slots SET status='COMPLETED',annotation_id=$7 WHERE project_id=$1 AND item_id=$2 AND slot=$3 AND reviewer_id=$4 AND token=$5 AND writer_epoch=$6 AND status='CLAIMED' AND lease_expires_at>clock_timestamp()`, s.ProjectID, c.ItemID, c.Slot, s.Principal, c.Token, s.Epoch, c.ID)
		if e != nil {
			return e
		}
		if tag.RowsAffected() != 1 {
			return rv.ErrOwnershipLost
		}
		if e = updateReviewState(ctx, tx, s.Scope.Scope, item); e != nil {
			return e
		}
		return tx.QueryRow(ctx, `SELECT status='ADJUDICATION_REQUIRED' FROM evaluation_review_items WHERE project_id=$1 AND id=$2`, s.ProjectID, c.ItemID).Scan(&disagreement)
	})
	if e == nil {
		reviewEvent("review_submitted", s.ProjectID, c.ItemID)
		if disagreement {
			reviewEvent("review_disagreement", s.ProjectID, c.ItemID)
		}
	}
	return annotation, e
}

// CorrectAnnotation 追加 revision；完成的 slot 与旧 Annotation 保持不可变。
func (k Reviews) CorrectAnnotation(ctx context.Context, s rv.Scope, id, supersedes string, j rv.Judgment) (rv.Annotation, error) {
	var out rv.Annotation
	if !s.Review || !asset.ValidID(id) || !asset.ValidID(supersedes) {
		return out, asset.ErrInvalid
	}
	e := k.transaction(ctx, s, false, func(tx pgx.Tx) error {
		var old rv.Annotation
		if e := reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_human_annotations", "annotation_bytes", supersedes, &old); e != nil {
			return e
		}
		item, e := reviewItem(ctx, tx, s.Scope.Scope, old.ItemID, true)
		if e != nil {
			return e
		}
		if old.Reviewer != s.Reviewer() || j.Kind != item.Command.Kind || j.Validate(item.Schema, item.Source.Evidence) != nil {
			return asset.ErrForbidden
		}
		intent := reviewHash(struct {
			Supersedes string
			Decision   rv.Judgment
			Reviewer   rv.Reviewer
		}{supersedes, j, s.Reviewer()})
		var existing string
		e = tx.QueryRow(ctx, `SELECT intent_digest FROM evaluation_human_annotations WHERE project_id=$1 AND id=$2`, s.ProjectID, id).Scan(&existing)
		if e == nil {
			if existing != intent {
				return asset.ErrConflict
			}
			return reviewDecode(ctx, tx, s.Scope.Scope, "evaluation_human_annotations", "annotation_bytes", id, &out)
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		var replaced bool
		if e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM evaluation_human_annotations WHERE project_id=$1 AND supersedes=$2)`, s.ProjectID, supersedes).Scan(&replaced); e != nil {
			return e
		}
		if replaced {
			return asset.ErrConflict
		}
		out = old
		out.ID = id
		out.Decision = j
		out.Supersedes = &supersedes
		now, e := dbNow(ctx, tx)
		if e != nil {
			return e
		}
		out.CreatedAt = now
		out.SubmittedAt = now
		raw, e := asset.FreezeBytes(out)
		if e != nil {
			return e
		}
		var token string
		e = tx.QueryRow(ctx, `SELECT token::text FROM evaluation_human_annotations WHERE project_id=$1 AND id=$2`, s.ProjectID, supersedes).Scan(&token)
		if e != nil {
			return e
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_human_annotations(id,project_id,item_id,slot,reviewer_id,token,intent_digest,annotation_bytes,supersedes) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`, id, s.ProjectID, old.ItemID, old.Slot, s.Principal, token, intent, raw, supersedes)
		if e != nil {
			return e
		}
		_, e = tx.Exec(ctx, `UPDATE evaluation_review_items SET status='ADJUDICATION_REQUIRED' WHERE project_id=$1 AND id=$2`, s.ProjectID, item.ID)
		return e
	})
	return out, e
}
