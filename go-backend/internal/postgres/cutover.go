package postgres

import (
	"context"
	"errors"
	"strings"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/cutover"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Cutover struct{ Pool *pgxpool.Pool }

func cutoverState(ctx context.Context, q queryer, lock bool) (cutover.State, error) {
	s := cutover.State{Scope: "system:evaluation"}
	suffix := ""
	if lock {
		suffix = " FOR UPDATE OF c"
	}
	err := q.QueryRow(ctx, `SELECT c.mode,c.active_writer,c.writer_epoch,COALESCE(c.cutover_id::text,''),v.version_num FROM evaluation_writer_control c CROSS JOIN alembic_version v WHERE c.domain='evaluation'`+suffix).Scan(&s.Mode, &s.Writer, &s.Epoch, &s.CutoverID, &s.Schema)
	return s, err
}
func (k Cutover) Status(ctx context.Context) (cutover.State, error) {
	return cutoverState(ctx, k.Pool, false)
}

// revokedRoles 验证有效权限，包括列授权；marker 不能替代撤权。
func revokedRoles(ctx context.Context, q queryer, roles []string) bool {
	if len(roles) == 0 {
		return false
	}
	for _, role := range roles {
		var safe bool
		err := q.QueryRow(ctx, `SELECT NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolbypassrls) AND NOT has_schema_privilege(r.oid,'public','CREATE') AND NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid) AND NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p') AND (c.relowner=r.oid OR has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE') OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE'))) AND NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef AND has_function_privilege(r.oid,p.oid,'EXECUTE')) AND NOT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=r.rolname AND pid<>pg_backend_pid()) FROM pg_roles r WHERE r.rolname=$1`, role).Scan(&safe)
		if err != nil || !safe {
			return false
		}
	}
	return true
}

func (k Cutover) Preflight(ctx context.Context, e cutover.Evidence, controlled bool) (cutover.Report, error) {
	tx, err := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return cutover.Report{}, err
	}
	defer rollback(ctx, tx)
	s, err := cutoverState(ctx, tx, false)
	if err != nil {
		return cutover.Report{}, err
	}
	r := cutover.Evaluate(s, e, controlled)
	status := "PASS"
	if !revokedRoles(ctx, tx, e.LegacyRoles) {
		status = "BLOCKED"
		r.Status = "BLOCKED"
	}
	r.Checks = append(r.Checks, cutover.Check{Name: "legacy_effective_privileges_and_sessions", Status: status})
	return r, tx.Commit(ctx)
}

// Transition 只复用 G2 冻结转换；生产连接由 CLI 在连接前拒绝。
func (k Cutover) Transition(ctx context.Context, command string, e cutover.Evidence) (cutover.State, error) {
	if !asset.ValidID(e.CutoverID) || !asset.Text(e.Operator) {
		return cutover.State{}, asset.ErrInvalid
	}
	tx, err := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return cutover.State{}, err
	}
	defer rollback(ctx, tx)
	s, err := cutoverState(ctx, tx, true)
	if err != nil {
		return s, err
	}
	var operator bool
	err = tx.QueryRow(ctx, `SELECT current_user=pg_get_userbyid(relowner) FROM pg_class WHERE oid='evaluation_writer_control'::regclass`).Scan(&operator)
	if err != nil || !operator {
		return s, asset.ErrForbidden
	}
	if s.Epoch != e.Epoch {
		return s, asset.ErrConflict
	}
	n, err := cutover.Next(s, command)
	if err != nil {
		return s, err
	}
	if command == "enter-drain" {
		if e.Checks["admission_stopped"] != "VERIFIED" || e.Checks["beat_stopped"] != "VERIFIED" {
			return s, errors.New("DRAIN_ADMISSION_NOT_VERIFIED")
		}
	} else {
		if cutover.Evaluate(s, e, true).Status != "PASS" || !revokedRoles(ctx, tx, e.LegacyRoles) {
			return s, errors.New("CUTOVER_PREFLIGHT_BLOCKED")
		}
		if command == "activate-go" && s.CutoverID != e.CutoverID {
			return s, asset.ErrConflict
		}
	}
	_, err = tx.Exec(ctx, `UPDATE evaluation_writer_control SET mode=$1,active_writer=$2,writer_epoch=$3,cutover_id=$4,operator_ref=$5,changed_at=clock_timestamp() WHERE domain='evaluation'`, n.Mode, n.Writer, n.Epoch, e.CutoverID, e.Operator)
	if err != nil {
		return s, err
	}
	n.CutoverID = e.CutoverID
	if err = tx.Commit(ctx); err != nil {
		return s, errors.New("COMMIT_UNKNOWN_QUERY_STATUS")
	}
	return n, nil
}

// LegacyReaderGrants 只生成 operator SQL；真实最小读取集合由部署方决定。
func LegacyReaderGrants(role string, tables []string) (string, error) {
	allowed := map[string]bool{"traces": true, "spans": true, "trace_scores": true, "session_scores": true, "eval_runs": true, "projects": true, "organizations": true, "memberships": true, "users": true, "localagent_trace_envelope_sidecars": true}
	if role == "" || len(tables) == 0 {
		return "", asset.ErrInvalid
	}
	id := pgx.Identifier{role}.Sanitize()
	sql := "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM " + id + "; REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM " + id + "; REVOKE CREATE ON SCHEMA public FROM " + id + "; GRANT USAGE ON SCHEMA public TO " + id + ";"
	for _, table := range tables {
		if !allowed[table] {
			return "", asset.ErrInvalid
		}
		sql += "GRANT SELECT ON " + pgx.Identifier{table}.Sanitize() + " TO " + id + ";"
	}
	return strings.TrimSpace(sql), nil
}
