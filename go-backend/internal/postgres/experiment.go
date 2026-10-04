package postgres

import (
	"context"
	"encoding/json"
	"errors"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/experiment"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Experiments struct{ Pool *pgxpool.Pool }

var _ experiment.Store = Experiments{}

func getExperiment(ctx context.Context, q queryer, scope asset.Scope, id string) (experiment.Experiment, error) {
	var v experiment.Experiment
	var raw []byte
	err := q.QueryRow(ctx, `SELECT e.id::text,e.project_id::text,e.created_by,e.created_at,e.intent_digest,e.intent_bytes FROM evaluation_experiments e JOIN projects p ON p.id=e.project_id WHERE e.project_id=$1 AND p.org_id=$2 AND e.id=$3`, scope.ProjectID, scope.OrganizationID, id).Scan(&v.ID, &v.ProjectID, &v.CreatedBy, &v.CreatedAt, &v.Digest, &raw)
	if err != nil {
		return v, dbError(err)
	}
	if err = json.Unmarshal(raw, &v.Intent); err != nil {
		return v, err
	}
	rows, err := q.Query(ctx, "SELECT repeat_no,creation_command_id::text,run_id::text FROM evaluation_experiment_runs WHERE project_id=$1 AND experiment_id=$2 ORDER BY repeat_no", scope.ProjectID, id)
	if err != nil {
		return v, err
	}
	defer rows.Close()
	for rows.Next() {
		var slot experiment.Slot
		if err = rows.Scan(&slot.Repeat, &slot.CommandID, &slot.RunID); err != nil {
			return v, err
		}
		v.Slots = append(v.Slots, slot)
	}
	return v, rows.Err()
}
func (s Experiments) CreateExperiment(ctx context.Context, scope ev.Scope, v experiment.Experiment) (experiment.Experiment, error) {
	if !scope.Create || scope.Validate(false) != nil || v.ProjectID != scope.ProjectID || v.CreatedBy != scope.Principal || !asset.ValidID(v.ID) {
		return experiment.Experiment{}, asset.ErrInvalid
	}
	intent, err := asset.Freeze(v.Intent)
	if err != nil {
		return experiment.Experiment{}, err
	}
	kernel, err := asset.Freeze(v.Intent.Snapshot)
	if err != nil {
		return experiment.Experiment{}, err
	}
	var saved experiment.Experiment
	k := Evaluation{Pool: s.Pool}
	reply, err := k.transact(ctx, scope, "", true, func(tx pgx.Tx, _ ev.Run, _ string) (ev.Reply, error) {
		// Project 锁后对创建 identity 串行化；没有额外 durable queue/table。
		if _, e := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", v.ID); e != nil {
			return ev.Reply{}, e
		}
		old, e := getExperiment(ctx, tx, scope.Scope, v.ID)
		if e == nil {
			if old.Digest != v.Digest {
				return ev.Reply{}, asset.ErrConflict
			}
			saved = old
			return ev.Reply{Code: ev.AlreadyApplied}, nil
		}
		if !errors.Is(e, asset.ErrNotFound) {
			return ev.Reply{}, e
		}
		var baselineRun, baselineExperiment any
		if v.Intent.Baseline != nil {
			if v.Intent.Baseline.RunID != "" {
				baselineRun = v.Intent.Baseline.RunID
			}
			if v.Intent.Baseline.ExperimentID != "" {
				baselineExperiment = v.Intent.Baseline.ExperimentID
			}
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_experiments(id,project_id,name,repeat_count,created_by,intent_digest,intent_bytes,kernel_bytes,baseline_run_id,baseline_experiment_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`, v.ID, scope.ProjectID, v.Intent.Name, v.Intent.Repeat, v.CreatedBy, v.Digest, intent.Bytes(), kernel.Bytes(), baselineRun, baselineExperiment)
		if e != nil {
			return ev.Reply{}, dbError(e)
		}
		for _, slot := range v.Slots {
			if _, e = tx.Exec(ctx, "INSERT INTO evaluation_experiment_runs(project_id,experiment_id,repeat_no,creation_command_id) VALUES($1,$2,$3,$4)", scope.ProjectID, v.ID, slot.Repeat, slot.CommandID); e != nil {
				return ev.Reply{}, e
			}
		}
		saved, e = getExperiment(ctx, tx, scope.Scope, v.ID)
		return ev.Reply{Code: ev.Applied}, e
	})
	if err != nil {
		return experiment.Experiment{}, err
	}
	if reply.Code != ev.Applied && reply.Code != ev.AlreadyApplied {
		return experiment.Experiment{}, asset.ErrForbidden
	}
	return saved, nil
}
func (s Experiments) GetExperiment(ctx context.Context, scope asset.Scope, id string) (experiment.Experiment, error) {
	if scope.Validate(false) != nil || !asset.ValidID(id) {
		return experiment.Experiment{}, asset.ErrInvalid
	}
	// intent/slots 各自不可变；link 可以更新，用只读一致快照返回。
	tx, err := s.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return experiment.Experiment{}, err
	}
	defer rollback(ctx, tx)
	v, err := getExperiment(ctx, tx, scope, id)
	if err != nil {
		return v, err
	}
	err = tx.Commit(ctx)
	return v, err
}
func (s Experiments) ListExperiments(ctx context.Context, scope asset.Scope, limit int) ([]experiment.Experiment, error) {
	if scope.Validate(false) != nil || limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	rows, err := s.Pool.Query(ctx, "SELECT e.id::text FROM evaluation_experiments e JOIN projects p ON p.id=e.project_id WHERE e.project_id=$1 AND p.org_id=$2 ORDER BY e.created_at,e.id LIMIT $3", scope.ProjectID, scope.OrganizationID, limit)
	if err != nil {
		return nil, err
	}
	var ids []string
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		ids = append(ids, id)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	var out []experiment.Experiment
	for _, id := range ids {
		v, e := s.GetExperiment(ctx, scope, id)
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, nil
}
func (s Experiments) AttachRun(ctx context.Context, scope ev.Scope, id string, no int, run string) error {
	if !scope.Create || !asset.ValidID(id) || !asset.ValidID(run) || no < 1 {
		return asset.ErrInvalid
	}
	reply, err := (Evaluation{Pool: s.Pool}).transact(ctx, scope, "", true, func(tx pgx.Tx, _ ev.Run, _ string) (ev.Reply, error) {
		var found string
		if e := tx.QueryRow(ctx, "SELECT id::text FROM evaluation_experiments WHERE project_id=$1 AND id=$2 FOR UPDATE", scope.ProjectID, id).Scan(&found); e != nil {
			return ev.Reply{}, dbError(e)
		}
		var previous *string
		if e := tx.QueryRow(ctx, "SELECT run_id::text FROM evaluation_experiment_runs WHERE project_id=$1 AND experiment_id=$2 AND repeat_no=$3 FOR UPDATE", scope.ProjectID, id, no).Scan(&previous); e != nil {
			return ev.Reply{}, dbError(e)
		}
		if previous != nil {
			if *previous != run {
				return ev.Reply{}, asset.ErrConflict
			}
			return ev.Reply{Code: ev.AlreadyApplied}, nil
		}
		_, e := tx.Exec(ctx, "UPDATE evaluation_experiment_runs SET run_id=$4 WHERE project_id=$1 AND experiment_id=$2 AND repeat_no=$3", scope.ProjectID, id, no, run)
		return ev.Reply{Code: ev.Applied}, dbError(e)
	})
	if err != nil {
		return err
	}
	if reply.Code != ev.Applied && reply.Code != ev.AlreadyApplied {
		return asset.ErrForbidden
	}
	return nil
}
