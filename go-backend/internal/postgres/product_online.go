package postgres

import (
	"agentevalops/go-backend/internal/asset"
	ob "agentevalops/go-backend/internal/observation"
	"context"
	"encoding/json"
	"time"
)

type FailurePageCursor struct {
	Created  time.Time
	Kind, ID string
	Offset   int
}

// ListFailureCandidatePage 合并两条有序事实流；同一 observation 的多个候选不会跨页丢失。
// 分类仍由 G5 owner 的相同函数完成，每次最多扫描 100 条事实。
func (k Online) ListFailureCandidatePage(ctx context.Context, s asset.Scope, c FailurePageCursor, limit int) ([]ob.FailureCandidate, *FailurePageCursor, error) {
	if s.Validate(false) != nil || limit < 1 || limit > 100 || c.ID != "" && (!asset.ValidID(c.ID) || c.Created.IsZero() || (c.Kind != "O" && c.Kind != "R") || c.Offset < 0 || c.Offset > 2) {
		return nil, nil, asset.ErrInvalid
	}
	rows, e := k.Pool.Query(ctx, `SELECT r.created_at,r.kind,r.id::text,r.body FROM (
SELECT created_at,'O'::text kind,id,observation_bytes body,project_id FROM evaluation_observations
UNION ALL SELECT created_at,'R'::text,id,result_bytes,project_id FROM evaluation_online_results
) r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND ($3::timestamptz IS NULL OR (r.created_at,r.kind,r.id)>=($3,$4,$5::uuid)) ORDER BY r.created_at,r.kind,r.id LIMIT 101`, s.ProjectID, s.OrganizationID, nullableTime(c.Created), c.Kind, nullableID(c.ID))
	if e != nil {
		return nil, nil, e
	}
	defer rows.Close()
	type fact struct {
		cursor FailurePageCursor
		body   []byte
	}
	facts := []fact{}
	for rows.Next() {
		var f fact
		if e = rows.Scan(&f.cursor.Created, &f.cursor.Kind, &f.cursor.ID, &f.body); e != nil {
			return nil, nil, e
		}
		facts = append(facts, f)
	}
	if e = rows.Err(); e != nil {
		return nil, nil, e
	}
	out := []ob.FailureCandidate{}
	var last FailurePageCursor
	for i, f := range facts {
		if i == 100 {
			return out, &last, nil
		}
		var candidates []ob.FailureCandidate
		if f.cursor.Kind == "O" {
			var o ob.Observation
			if e = json.Unmarshal(f.body, &o); e != nil {
				return nil, nil, e
			}
			candidates = observationFailureCandidates(s.ProjectID, o)
		} else {
			var r ob.Result
			if e = json.Unmarshal(f.body, &r); e != nil {
				return nil, nil, e
			}
			candidates = resultFailureCandidates(s.ProjectID, r)
		}
		start := 0
		if f.cursor.ID == c.ID && f.cursor.Kind == c.Kind && f.cursor.Created.Equal(c.Created) {
			start = c.Offset
		}
		if start > len(candidates) {
			return nil, nil, asset.ErrInvalid
		}
		for j := start; j < len(candidates); j++ {
			out = append(out, candidates[j])
			last = f.cursor
			last.Offset = j + 1
			if len(out) == limit {
				if j+1 < len(candidates) || i+1 < len(facts) {
					return out, &last, nil
				}
				return out, nil, nil
			}
		}
		last = f.cursor
		last.Offset = len(candidates)
	}
	return out, nil, nil
}
func (k Online) GetProductBackfill(ctx context.Context, s asset.Scope, id string) (map[string]any, error) {
	if s.Validate(false) != nil || !asset.ValidID(id) {
		return nil, asset.ErrInvalid
	}
	var processed int
	var done bool
	var created time.Time
	var rule asset.Ref
	e := k.Pool.QueryRow(ctx, `SELECT b.processed,b.completed,b.created_at,b.rule_id::text,b.rule_version FROM evaluation_online_backfills b JOIN projects p ON p.id=b.project_id WHERE b.project_id=$1 AND p.org_id=$2 AND b.id=$3`, s.ProjectID, s.OrganizationID, id).Scan(&processed, &done, &created, &rule.EntityID, &rule.Version)
	return map[string]any{"id": id, "processed": processed, "completed": done, "rule": rule, "created_at": created}, dbError(e)
}
