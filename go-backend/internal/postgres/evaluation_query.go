package postgres

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"encoding/json"
	"github.com/jackc/pgx/v5"
)

// ReadRunState 给 G3/G8 一致的 canonical facts；不创建 legacy projection。
func (k Evaluation) ReadRunState(ctx context.Context, scope asset.Scope, id string) (ev.RunState, error) {
	if scope.Validate(false) != nil || !asset.ValidID(id) {
		return ev.RunState{}, asset.ErrInvalid
	}
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if e != nil {
		return ev.RunState{}, e
	}
	defer rollback(ctx, tx)
	var found bool
	e = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM projects WHERE id=$1 AND org_id=$2)", scope.ProjectID, scope.OrganizationID).Scan(&found)
	if e != nil {
		return ev.RunState{}, e
	}
	if !found {
		return ev.RunState{}, asset.ErrNotFound
	}
	s := ev.Scope{Scope: scope}
	r, e := getRun(ctx, tx, s, id, false)
	if e != nil {
		return ev.RunState{}, e
	}
	attempts, works, e := attemptsAndWorks(ctx, tx, s, id)
	if e != nil {
		return ev.RunState{}, e
	}
	for i := range attempts {
		if attempts[i].Metadata.ObservationBytes != nil {
			if e = json.Unmarshal(attempts[i].Metadata.ObservationBytes, &attempts[i].Metadata.Observation); e != nil {
				return ev.RunState{}, e
			}
		}
		for _, c := range r.Snapshot.Input.Manifest {
			if c.Identity.Ref.EntityID == attempts[i].CaseID && c.Identity.Ref.Version == attempts[i].CaseVersion {
				attempts[i].Request = ev.Request{ID: attempts[i].RequestID, IdempotencyKey: attempts[i].Key, Case: c, Target: r.Snapshot.Target, RunID: r.ID, AttemptID: attempts[i].ID}
			}
			for i := range works {
				if e = restoreCalls(&works[i]); e != nil {
					return ev.RunState{}, e
				}
			}
		}
	}
	results := []ev.EvaluationResult{}
	rows, e := tx.Query(ctx, "SELECT to_jsonb(x) FROM evaluation_results x WHERE project_id=$1 AND run_id=$2 ORDER BY created_at,id", scope.ProjectID, id)
	if e != nil {
		return ev.RunState{}, e
	}
	for rows.Next() {
		var b []byte
		var v ev.EvaluationResult
		if e = rows.Scan(&b); e == nil {
			e = json.Unmarshal(b, &v)
		}
		if e != nil {
			rows.Close()
			return ev.RunState{}, e
		}
		var meta struct{ ValueBytes []byte }
		if e = json.Unmarshal(v.Metadata.Bytes(), &meta); e != nil {
			rows.Close()
			return ev.RunState{}, e
		}
		if meta.ValueBytes != nil {
			if e = json.Unmarshal(meta.ValueBytes, &v.Value); e != nil {
				rows.Close()
				return ev.RunState{}, e
			}
		}
		results = append(results, v)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return ev.RunState{}, e
	}
	if e = tx.Commit(ctx); e != nil {
		return ev.RunState{}, e
	}
	return ev.RunState{Run: r, Attempts: attempts, Works: works, Results: results}, nil
}
