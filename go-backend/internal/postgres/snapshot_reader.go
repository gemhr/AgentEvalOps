package postgres

import (
	"context"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/evaluation"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// PublishedAssets 仅装配 snapshot reader 的五个 owner；不提供总 CRUD repository。
type PublishedAssets struct {
	Cases
	Datasets
	Suites
	MetricDefinitions
	EvaluatorDefinitions
}

var _ evaluation.PublishedReader = PublishedAssets{}

type LegacyRunInputs struct{ Pool *pgxpool.Pool }

func (s LegacyRunInputs) ReadLegacyRunInput(ctx context.Context, scope asset.Scope, id string) (evaluation.LegacyInput, error) {
	if err := scope.Validate(false); err != nil {
		return evaluation.LegacyInput{}, err
	}
	if !asset.ValidID(id) {
		return evaluation.LegacyInput{}, asset.ErrInvalid
	}
	tx, err := s.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return evaluation.LegacyInput{}, err
	}
	defer rollback(ctx, tx)
	var v evaluation.LegacyInput
	var dataset, suite, target, subject []byte
	err = tx.QueryRow(ctx, `SELECT r.project_id::text,r.id::text,r.dataset_snapshot,r.suite_snapshot,r.execution_target_snapshot,COALESCE(r.subject_ref,'null'::jsonb)
 FROM evaluation_runs r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND r.id=$3`, scope.ProjectID, scope.OrganizationID, id).Scan(&v.ProjectID, &v.SourceRunID, &dataset, &suite, &target, &subject)
	if err != nil {
		return v, dbError(err)
	}
	for _, pair := range []struct {
		raw []byte
		dst *asset.JSON
	}{{dataset, &v.DatasetSnapshot}, {suite, &v.SuiteSnapshot}, {target, &v.TargetSnapshot}, {subject, &v.SubjectSnapshot}} {
		parsed, err := asset.ParseJSON(pair.raw)
		if err != nil {
			return v, err
		}
		*pair.dst = parsed
	}
	rows, err := tx.Query(ctx, `SELECT id::text,case_id,case_version,attempt_no,request_snapshot FROM evaluation_attempts WHERE project_id=$1 AND run_id=$2 ORDER BY case_id,case_version,attempt_no`, scope.ProjectID, id)
	if err != nil {
		return v, err
	}
	v.Attempts = []evaluation.LegacyCaseInput{}
	for rows.Next() {
		var a evaluation.LegacyCaseInput
		var raw []byte
		if err = rows.Scan(&a.AttemptID, &a.CaseID, &a.CaseVersion, &a.AttemptNo, &raw); err != nil {
			rows.Close()
			return v, err
		}
		a.RequestSnapshot, err = asset.ParseJSON(raw)
		if err != nil {
			rows.Close()
			return v, err
		}
		v.Attempts = append(v.Attempts, a)
	}
	rows.Close()
	if err = rows.Err(); err != nil {
		return v, err
	}
	if err = tx.Commit(ctx); err != nil {
		return v, err
	}
	return v, nil
}
