// Package postgres 实现 owner-specific 的目录命令和 scoped SQL；不运行迁移。
package postgres

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"time"

	"agentevalops/go-backend/internal/asset"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

type tables struct{ logical, version string }

var (
	caseTables      = tables{"evaluation_cases", "evaluation_case_versions"}
	datasetTables   = tables{"evaluation_datasets", "evaluation_dataset_versions"}
	suiteTables     = tables{"evaluation_suites", "evaluation_suite_versions"}
	metricTables    = tables{"evaluation_metric_definitions", "evaluation_metric_definition_versions"}
	evaluatorTables = tables{"evaluation_evaluator_definitions", "evaluation_evaluator_definition_versions"}
)

// SQL 标识符全部来自本包静态 tables，不接受调用方提供的表名。
type queryer interface {
	QueryRow(context.Context, string, ...any) pgx.Row
	Query(context.Context, string, ...any) (pgx.Rows, error)
}

func dbError(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return asset.ErrNotFound
	}
	var e *pgconn.PgError
	if errors.As(err, &e) {
		switch e.Code {
		case "23503":
			return fmt.Errorf("%w: invalid scoped reference", asset.ErrNotFound)
		case "23505":
			return fmt.Errorf("%w: %s", asset.ErrConflict, e.ConstraintName)
		case "23514":
			return fmt.Errorf("%w: %s", asset.ErrInvalid, e.ConstraintName)
		}
	}
	return err
}
func rollback(ctx context.Context, tx pgx.Tx) {
	cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	_ = tx.Rollback(cleanup)
}
func lockProject(ctx context.Context, tx pgx.Tx, scope asset.Scope) error {
	var id string
	return dbError(tx.QueryRow(ctx, "SELECT id::text FROM projects WHERE id=$1 AND org_id=$2 FOR KEY SHARE", scope.ProjectID, scope.OrganizationID).Scan(&id))
}
func scanLogical(row pgx.Row) (asset.Logical, error) {
	var v asset.Logical
	err := row.Scan(&v.ID, &v.ProjectID, &v.Name, &v.CreatedBy, &v.CreatedAt)
	return v, dbError(err)
}
func getLogical(ctx context.Context, q queryer, t tables, scope asset.Scope, id string) (asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return asset.Logical{}, err
	}
	if !asset.ValidID(id) {
		return asset.Logical{}, asset.ErrInvalid
	}
	return scanLogical(q.QueryRow(ctx, "SELECT l.id::text,l.project_id::text,l.name,l.created_by,l.created_at FROM "+t.logical+" l JOIN projects p ON p.id=l.project_id WHERE l.project_id=$1 AND p.org_id=$2 AND l.id=$3", scope.ProjectID, scope.OrganizationID, id))
}
func createLogical(ctx context.Context, pool *pgxpool.Pool, t tables, scope asset.Scope, cmd asset.Create) (asset.Logical, error) {
	if err := scope.Validate(true); err != nil {
		return asset.Logical{}, err
	}
	if err := cmd.Validate(); err != nil {
		return asset.Logical{}, err
	}
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return asset.Logical{}, err
	}
	defer rollback(ctx, tx)
	if err = lockProject(ctx, tx, scope); err != nil {
		return asset.Logical{}, err
	}
	_, err = tx.Exec(ctx, "INSERT INTO "+t.logical+"(id,project_id,name,created_by) VALUES($1,$2,$3,$4) ON CONFLICT(id) DO NOTHING", cmd.ID, scope.ProjectID, cmd.Name, scope.Principal)
	if err != nil {
		return asset.Logical{}, dbError(err)
	}
	v, err := getLogical(ctx, tx, t, scope, cmd.ID)
	if err != nil {
		return v, err
	}
	if v.Name != cmd.Name || v.CreatedBy != scope.Principal {
		return asset.Logical{}, asset.ErrConflict
	}
	if err = tx.Commit(ctx); err != nil {
		return asset.Logical{}, dbError(err)
	}
	return v, nil
}
func listLogical(ctx context.Context, q queryer, t tables, scope asset.Scope, limit int) ([]asset.Logical, error) {
	if err := scope.Validate(false); err != nil {
		return nil, err
	}
	if limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	rows, err := q.Query(ctx, "SELECT l.id::text,l.project_id::text,l.name,l.created_by,l.created_at FROM "+t.logical+" l JOIN projects p ON p.id=l.project_id WHERE l.project_id=$1 AND p.org_id=$2 ORDER BY l.created_at,l.id LIMIT $3", scope.ProjectID, scope.OrganizationID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []asset.Logical{}
	for rows.Next() {
		v, err := scanLogical(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, v)
	}
	return result, rows.Err()
}

const versionColumns = "v.project_id::text,v.entity_id::text,v.version,v.canonical_bytes,v.content_digest,v.semantic_digest,v.published_by,v.published_at,v.algorithm_ref"

func scanVersion[T any](row pgx.Row) (asset.Version[T], error) {
	var project, actor, contentDigest, semanticDigest, algorithm string
	var ref asset.Ref
	var raw []byte
	var at time.Time
	err := row.Scan(&project, &ref.EntityID, &ref.Version, &raw, &contentDigest, &semanticDigest, &actor, &at, &algorithm)
	if err != nil {
		return asset.Version[T]{}, dbError(err)
	}
	if algorithm != asset.CatalogAlgorithm {
		return asset.Version[T]{}, asset.ErrUnsupported
	}
	return asset.Restore[T](project, ref, raw, contentDigest, semanticDigest, actor, at)
}
func loadVersion[T any](ctx context.Context, q queryer, t tables, scope asset.Scope, ref asset.Ref) (asset.Version[T], error) {
	if err := scope.Validate(false); err != nil {
		return asset.Version[T]{}, err
	}
	if err := ref.Validate(); err != nil {
		return asset.Version[T]{}, err
	}
	return scanVersion[T](q.QueryRow(ctx, "SELECT "+versionColumns+" FROM "+t.version+" v JOIN projects p ON p.id=v.project_id WHERE v.project_id=$1 AND p.org_id=$2 AND v.entity_id=$3 AND v.version=$4 AND v.sealed", scope.ProjectID, scope.OrganizationID, ref.EntityID, ref.Version))
}
func listVersions[T any](ctx context.Context, q queryer, t tables, scope asset.Scope, id string, limit int) ([]asset.Version[T], error) {
	if _, err := getLogical(ctx, q, t, scope, id); err != nil {
		return nil, err
	}
	if limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	rows, err := q.Query(ctx, "SELECT "+versionColumns+" FROM "+t.version+" v JOIN projects p ON p.id=v.project_id WHERE v.project_id=$1 AND p.org_id=$2 AND v.entity_id=$3 AND v.sealed ORDER BY v.published_at,v.version LIMIT $4", scope.ProjectID, scope.OrganizationID, id, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []asset.Version[T]{}
	for rows.Next() {
		v, err := scanVersion[T](rows)
		if err != nil {
			return nil, err
		}
		result = append(result, v)
	}
	return result, rows.Err()
}

func publish[T interface{ Validate() error }](ctx context.Context, pool *pgxpool.Pool, t tables, scope asset.Scope, cmd asset.Publish[T], dataset *asset.Ref, refs func(context.Context, pgx.Tx, T) error) (asset.Version[T], error) {
	if err := scope.Validate(true); err != nil {
		return asset.Version[T]{}, err
	}
	if err := cmd.Body.Validate(); err != nil {
		return asset.Version[T]{}, err
	}
	candidate, err := asset.NewVersion(scope.ProjectID, cmd.Ref, cmd.Body, cmd.Source, scope.Principal, time.Time{})
	if err != nil {
		return candidate, err
	}
	// 冻结后重新 decode，事务仅消费该不可变输入，而非原调用方 slice/map。
	body := candidate.Content().Body
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return asset.Version[T]{}, err
	}
	defer rollback(ctx, tx)
	if err = lockProject(ctx, tx, scope); err != nil {
		return asset.Version[T]{}, err
	}
	var id string
	if err = tx.QueryRow(ctx, "SELECT id::text FROM "+t.logical+" WHERE project_id=$1 AND id=$2 FOR UPDATE", scope.ProjectID, cmd.Ref.EntityID).Scan(&id); err != nil {
		return asset.Version[T]{}, dbError(err)
	}
	existing, err := loadVersion[T](ctx, tx, t, scope, cmd.Ref)
	if err == nil {
		if !bytes.Equal(existing.Bytes(), candidate.Bytes()) || existing.PublishedBy() != scope.Principal {
			return asset.Version[T]{}, asset.ErrConflict
		}
		if err = tx.Commit(ctx); err != nil {
			return asset.Version[T]{}, dbError(err)
		}
		return existing, nil
	}
	if !errors.Is(err, asset.ErrNotFound) {
		return asset.Version[T]{}, err
	}
	columns := "project_id,entity_id,version,algorithm_ref,content_digest,semantic_digest,canonical_bytes,content,published_by"
	placeholders := "$1,$2,$3,$4,$5,$6,$7,$8,$9"
	args := []any{scope.ProjectID, cmd.Ref.EntityID, cmd.Ref.Version, candidate.Algorithm(), candidate.ContentDigest(), candidate.SemanticDigest(), candidate.Bytes(), candidate.Bytes(), scope.Principal}
	if t == suiteTables && dataset != nil {
		columns += ",dataset_id,dataset_version"
		placeholders += ",$10,$11"
		args = append(args, dataset.EntityID, dataset.Version)
	}
	_, err = tx.Exec(ctx, "INSERT INTO "+t.version+"("+columns+") VALUES("+placeholders+")", args...)
	if err != nil {
		return asset.Version[T]{}, dbError(err)
	}
	if refs != nil {
		if err = refs(ctx, tx, body); err != nil {
			return asset.Version[T]{}, dbError(err)
		}
	}
	_, err = tx.Exec(ctx, "UPDATE "+t.version+" SET sealed=true WHERE project_id=$1 AND entity_id=$2 AND version=$3", scope.ProjectID, cmd.Ref.EntityID, cmd.Ref.Version)
	if err != nil {
		return asset.Version[T]{}, err
	}
	v, err := loadVersion[T](ctx, tx, t, scope, cmd.Ref)
	if err != nil {
		return v, err
	}
	if err = tx.Commit(ctx); err != nil {
		return asset.Version[T]{}, dbError(err)
	}
	return v, nil
}
