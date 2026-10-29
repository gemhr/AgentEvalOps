package postgres

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	rv "agentevalops/go-backend/internal/review"
	"context"
	"encoding/json"
	"github.com/jackc/pgx/v5"
)

// StoreBusinessFeedback 只存受信 HTTP 拉取后的安全投影；不创建 Annotation/GT。
func (k Reviews) StoreBusinessFeedback(ctx context.Context, s ev.Scope, incident, feedback, expected string) (rv.FeedbackWire, error) {
	w, p, e := rv.FetchFeedback(ctx, s.ProjectID, incident, feedback)
	if e != nil {
		return w, e
	}
	if w.Digest != expected || p.Status != "ACTIVE" || p.Decision == "WITHDRAW" {
		return w, asset.ErrConflict
	}
	raw, _ := asset.FreezeBytes(w)
	reply, e := (Evaluation{Pool: k.Pool}).transact(ctx, s, "", true, func(tx pgx.Tx, _ ev.Run, _ string) (ev.Reply, error) {
		_, e := tx.Exec(ctx, `INSERT INTO evaluation_business_feedback(project_id,id,incident_id,digest,source_bytes) VALUES($1,$2,$3,$4,$5) ON CONFLICT(project_id,id) DO NOTHING`, s.ProjectID, feedback, incident, w.Digest, raw)
		if e != nil {
			return ev.Reply{}, e
		}
		var old string
		e = tx.QueryRow(ctx, `SELECT digest FROM evaluation_business_feedback WHERE project_id=$1 AND id=$2`, s.ProjectID, feedback).Scan(&old)
		if e != nil {
			return ev.Reply{}, e
		}
		if old != w.Digest {
			return ev.Reply{}, asset.ErrConflict
		}
		return ev.Reply{Code: ev.Applied}, nil
	})
	if e != nil {
		return w, e
	}
	if reply.Code != ev.Applied {
		return w, asset.ErrForbidden
	}
	return w, nil
}

func businessFeedbackSource(ctx context.Context, q queryer, s asset.Scope, id string) (rv.FeedbackWire, rv.FeedbackProjection, error) {
	var raw []byte
	var stored rv.FeedbackWire
	var p rv.FeedbackProjection
	e := q.QueryRow(ctx, `SELECT source_bytes FROM evaluation_business_feedback WHERE project_id=$1 AND id=$2`, s.ProjectID, id).Scan(&raw)
	if e != nil {
		return stored, p, dbError(e)
	}
	if json.Unmarshal(raw, &stored) != nil || stored.Projection.Decode(&p) != nil {
		return stored, p, asset.ErrInvalid
	}
	current, cp, e := rv.FetchFeedback(ctx, s.ProjectID, p.IncidentID, id)
	if e != nil {
		return stored, p, e
	}
	if cp.Status != "ACTIVE" || cp.Decision == "WITHDRAW" || current.Digest != stored.Digest {
		return stored, p, asset.ErrConflict
	}
	return stored, p, nil
}

func checkBusinessFeedback(ctx context.Context, q queryer, s asset.Scope, item rv.Item) error {
	if item.Source.Ref.Type != "BUSINESS_FEEDBACK" {
		return nil
	}
	_, _, e := businessFeedbackSource(ctx, q, s, item.Source.Ref.FeedbackID)
	return e
}

func (k Reviews) BusinessFeedbackReceipt(ctx context.Context, s asset.Scope, id string) (map[string]any, error) {
	var incident, digest string
	if !asset.ValidID(id) {
		return nil, asset.ErrInvalid
	}
	e := k.Pool.QueryRow(ctx, `SELECT incident_id::text,digest FROM evaluation_business_feedback b JOIN projects p ON p.id=b.project_id WHERE b.project_id=$1 AND p.org_id=$2 AND b.id=$3`, s.ProjectID, s.OrganizationID, id).Scan(&incident, &digest)
	if e != nil {
		return nil, dbError(e)
	}
	w, p, e := rv.FetchFeedback(ctx, s.ProjectID, incident, id)
	state := "UNKNOWN"
	if e == nil {
		state = p.Status
		if state == "ACTIVE" && w.Digest != digest {
			state = "INVALID"
		}
	}
	return map[string]any{"feedback_id": id, "projection_digest": digest, "current_status": state, "requires_re_review": state != "ACTIVE", "historical_versions_unchanged": true}, nil
}
