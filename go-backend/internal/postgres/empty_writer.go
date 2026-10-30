package postgres

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/cutover"
	ev "agentevalops/go-backend/internal/evaluation"
	"github.com/jackc/pgx/v5"
)

const EmptyWriterHead = "c17a00100001"

// EmptyWriterIdentity 由独立 operator 工件绑定；运行角色没有初始化权限。
type EmptyWriterIdentity struct {
	TaskID           string `json:"task_id"`
	OperationID      string `json:"operation_id"`
	Host             string `json:"host"`
	Port             uint16 `json:"port"`
	Database         string `json:"database"`
	DatabaseOID      uint32 `json:"database_oid"`
	Owner            string `json:"owner"`
	SystemIdentifier string `json:"system_identifier"`
	ContainerID      string `json:"container_id"`
	Volume           string `json:"volume"`
	MigrationDigest  string `json:"migration_digest"`
	CodeDigest       string `json:"code_digest"`
}

func (i EmptyWriterIdentity) Validate() error {
	hex32 := regexp.MustCompile(`^[0-9a-f]{32}$`)
	hex64 := regexp.MustCompile(`^[0-9a-f]{64}$`)
	if !hex32.MatchString(i.TaskID) || !asset.ValidID(i.OperationID) || i.Host != "127.0.0.1" || i.Port < 1024 ||
		i.Database != "ci_evaluation_"+i.TaskID || i.DatabaseOID == 0 || !asset.Text(i.Owner) ||
		!regexp.MustCompile(`^[0-9]{10,20}$`).MatchString(i.SystemIdentifier) ||
		!hex64.MatchString(i.ContainerID) || i.Volume != "ci-e2e-eopg-"+i.TaskID+"-data" ||
		!hex64.MatchString(i.MigrationDigest) || !hex64.MatchString(i.CodeDigest) {
		return asset.ErrInvalid
	}
	return nil
}

type EmptyWriterReceipt struct {
	Identity        EmptyWriterIdentity `json:"identity"`
	Operator        string              `json:"operator"`
	States          []cutover.State     `json:"states"`
	EmptyTables     []string            `json:"empty_tables"`
	SeedAllowlist   []string            `json:"seed_allowlist"`
	Timestamp       time.Time           `json:"timestamp"`
	Decision        string              `json:"decision"`
	PreflightDigest string              `json:"preflight_digest"`
}

// InitializeEmptyGo 仅接受当前 Head 的原始空库；不提供任意状态/Epoch 参数。
// execute=false 同样完成锁内检查，但不写 Writer；提交不明绝不自动重试。
func (k Cutover) InitializeEmptyGo(ctx context.Context, i EmptyWriterIdentity, execute bool) (EmptyWriterReceipt, error) {
	r := EmptyWriterReceipt{Identity: i, Timestamp: time.Now().UTC(), Decision: "REJECTED"}
	if e := i.Validate(); e != nil {
		return r, e
	}
	cfg := k.Pool.Config().ConnConfig
	if cfg.Host != i.Host || cfg.Port != i.Port || cfg.Database != i.Database {
		return r, asset.ErrForbidden
	}
	for _, f := range cfg.Fallbacks {
		if f.Host != i.Host || f.Port != i.Port {
			return r, asset.ErrForbidden
		}
	}
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if e != nil {
		return r, e
	}
	defer rollback(ctx, tx)
	if _, e = tx.Exec(ctx, `SET LOCAL lock_timeout='1s'`); e != nil {
		return r, e
	}
	var locked bool
	if e = tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock(173021,$1::integer)`, int64(i.DatabaseOID)).Scan(&locked); e != nil || !locked {
		return r, errors.New("EMPTY_WRITER_BUSY")
	}
	var oid uint32
	var owner, system, operator string
	var other int
	e = tx.QueryRow(ctx, `SELECT d.oid,pg_get_userbyid(d.datdba),s.system_identifier::text,current_user FROM pg_database d CROSS JOIN pg_control_system() s WHERE d.datname=current_database()`).Scan(&oid, &owner, &system, &operator)
	if e != nil || oid != i.DatabaseOID || owner != i.Owner || system != i.SystemIdentifier {
		return r, errors.New("EMPTY_WRITER_IDENTITY_MISMATCH")
	}
	r.Operator = operator
	var writerOwner bool
	if e = tx.QueryRow(ctx, `SELECT current_user=pg_get_userbyid(relowner) FROM pg_class WHERE oid='evaluation_writer_control'::regclass`).Scan(&writerOwner); e != nil || !writerOwner || operator != owner {
		return r, asset.ErrForbidden
	}
	// 所有非系统 schema 都纳入扫描；不接受 public 外的业务表/外部表。
	rows, e := tx.Query(ctx, `SELECT n.nspname,c.relname,c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','f','m') ORDER BY n.nspname,c.relname`)
	if e != nil {
		return r, e
	}
	var tables []string
	for rows.Next() {
		var schema, name, kind string
		if e = rows.Scan(&schema, &name, &kind); e != nil {
			rows.Close()
			return r, e
		}
		if schema != "public" || kind != "r" {
			rows.Close()
			return r, errors.New("EMPTY_WRITER_UNKNOWN_RELATION")
		}
		tables = append(tables, name)
	}
	e = rows.Err()
	rows.Close()
	if e != nil || len(tables) < 20 {
		return r, errors.New("EMPTY_WRITER_SCHEMA_INCOMPLETE")
	}
	ids := []string{}
	for _, name := range tables {
		ids = append(ids, pgx.Identifier{"public", name}.Sanitize())
	}
	if _, e = tx.Exec(ctx, "LOCK TABLE "+strings.Join(ids, ",")+" IN ACCESS EXCLUSIVE MODE NOWAIT"); e != nil {
		return r, errors.New("EMPTY_WRITER_TABLES_BUSY")
	}
	if e = tx.QueryRow(ctx, `SELECT count(*) FROM pg_stat_activity WHERE datid=$1 AND backend_type='client backend' AND pid<>pg_backend_pid()`, i.DatabaseOID).Scan(&other); e != nil || other != 0 {
		return r, errors.New("EMPTY_WRITER_OTHER_SESSIONS")
	}
	s, e := cutoverState(ctx, tx, true)
	if e != nil {
		return r, e
	}
	if s.Schema != EmptyWriterHead || s.Mode != "PYTHON_ACTIVE" || s.Writer != "PYTHON" || s.Epoch != 1 || s.CutoverID != "" {
		return r, errors.New("EMPTY_WRITER_INITIAL_STATE_REQUIRED")
	}
	var seed bool
	e = tx.QueryRow(ctx, `SELECT count(*)=1 AND bool_and(domain='evaluation' AND contract_version=$1 AND required_schema_version=$2 AND operator_ref='migration') FROM evaluation_writer_control`, ev.DurableContract, ev.SchemaVersion).Scan(&seed)
	if e != nil || !seed {
		return r, errors.New("EMPTY_WRITER_SEED_INVALID")
	}
	// 各关键表均必须有主键，数量不依赖新增约束。
	var keys int
	e = tx.QueryRow(ctx, `SELECT count(DISTINCT conrelid) FROM pg_constraint WHERE contype='p' AND conrelid IN ('evaluation_writer_control'::regclass,'evaluation_runs'::regclass,'evaluation_attempts'::regclass,'evaluation_evaluator_works'::regclass,'evaluation_results'::regclass)`).Scan(&keys)
	if e != nil {
		return r, e
	}
	var enabled bool
	e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid WHERE t.tgrelid='evaluation_writer_control'::regclass AND t.tgname='g2_writer_guard' AND t.tgenabled='O' AND p.proname='g2_control_guard')`).Scan(&enabled)
	if e != nil || keys != 5 || !enabled {
		return r, errors.New("EMPTY_WRITER_GUARD_INVALID")
	}
	for _, name := range tables {
		var count int64
		if e = tx.QueryRow(ctx, "SELECT count(*) FROM "+pgx.Identifier{"public", name}.Sanitize()).Scan(&count); e != nil {
			return r, e
		}
		switch name {
		case "alembic_version", "evaluation_writer_control":
			if count != 1 {
				return r, errors.New("EMPTY_WRITER_SEED_INVALID")
			}
		default:
			if count != 0 {
				return r, errors.New("EMPTY_WRITER_NONEMPTY:" + name)
			}
			r.EmptyTables = append(r.EmptyTables, name)
		}
	}
	r.SeedAllowlist = []string{"alembic_version=" + EmptyWriterHead, "evaluation_writer_control=original-migration-seed.v1"}
	preflight, _ := asset.Freeze(map[string]any{"empty_tables": r.EmptyTables, "seed_allowlist": r.SeedAllowlist, "initial_state": s, "contract": ev.DurableContract, "required_schema": ev.SchemaVersion, "guard_enabled": enabled, "primary_key_tables": keys})
	r.PreflightDigest = preflight.Digest()
	r.States = []cutover.State{s}
	for _, command := range []string{"enter-drain", "enter-barrier", "activate-go"} {
		next, err := cutover.Next(s, command)
		if err != nil {
			return r, err
		}
		next.CutoverID = i.OperationID
		if execute {
			tag, err := tx.Exec(ctx, `UPDATE evaluation_writer_control SET mode=$1,active_writer=$2,writer_epoch=$3,cutover_id=$4,operator_ref=$5,changed_at=clock_timestamp() WHERE domain='evaluation' AND mode=$6 AND writer_epoch=$7`, next.Mode, next.Writer, next.Epoch, i.OperationID, operator, s.Mode, s.Epoch)
			if err != nil {
				return r, err
			}
			if tag.RowsAffected() != 1 {
				return r, asset.ErrConflict
			}
		}
		s = next
		r.States = append(r.States, s)
	}
	if !execute {
		r.Decision = "DRY_RUN"
		return r, nil
	}
	if e = tx.Commit(ctx); e != nil {
		r.Decision = "COMMIT_UNKNOWN_QUERY_SAME_OPERATION"
		return r, fmt.Errorf("COMMIT_UNKNOWN_QUERY_STATUS")
	}
	r.Decision = "APPLIED"
	return r, nil
}
