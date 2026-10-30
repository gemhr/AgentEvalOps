//go:build integration

package postgres_test

import (
	"context"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/postgres"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func emptyWriterDB(t *testing.T, revision string) (*database, postgres.EmptyWriterIdentity) {
	t.Helper()
	raw := os.Getenv("EMPTY_WRITER_TEST_POSTGRES_URL")
	u, e := url.Parse(raw)
	if e != nil || u.Hostname() != "127.0.0.1" || u.Port() != "55435" || u.Path != "/postgres" || u.User.Username() != "postgres" {
		t.Fatal("explicit isolated loopback:55435/postgres required")
	}
	ctx := context.Background()
	admin, e := pgxpool.New(ctx, raw)
	if e != nil {
		t.Fatal("admin configuration rejected")
	}
	task := strings.ReplaceAll(asset.NewID(), "-", "")
	name := "ci_evaluation_" + task
	if _, e = admin.Exec(ctx, "CREATE DATABASE "+pgx.Identifier{name}.Sanitize()); e != nil {
		t.Fatal(e)
	}
	v := *u
	v.Path = "/" + name
	config, e := pgxpool.ParseConfig(v.String())
	if e != nil {
		t.Fatal(e)
	}
	config.MaxConns = 1
	pool, e := pgxpool.NewWithConfig(ctx, config)
	if e != nil {
		t.Fatal(e)
	}
	d := &database{admin: admin, pool: pool, url: &v, name: name}
	t.Cleanup(func() {
		pool.Close()
		if _, e := admin.Exec(context.Background(), "DROP DATABASE "+pgx.Identifier{name}.Sanitize()); e != nil {
			t.Error(e)
		}
		admin.Close()
	})
	emptyWriterMigrate(t, d, revision)
	identity := postgres.EmptyWriterIdentity{TaskID: task, OperationID: asset.NewID(), Host: "127.0.0.1", Port: 55435, Database: name,
		ContainerID: strings.Repeat("a", 64), Volume: "ci-e2e-eopg-" + task + "-data", MigrationDigest: strings.Repeat("b", 64), CodeDigest: strings.Repeat("c", 64)}
	e = pool.QueryRow(ctx, `SELECT d.oid,pg_get_userbyid(d.datdba),s.system_identifier::text FROM pg_database d CROSS JOIN pg_control_system() s WHERE d.datname=current_database()`).Scan(&identity.DatabaseOID, &identity.Owner, &identity.SystemIdentifier)
	if e != nil {
		t.Fatal(e)
	}
	return d, identity
}

func writerRole(t *testing.T, d *database, api bool) *pgxpool.Pool {
	t.Helper()
	role := "empty_role_" + strings.TrimPrefix(d.name, "ci_evaluation_")
	ctx := context.Background()
	id := pgx.Identifier{role}.Sanitize()
	if _, e := d.pool.Exec(ctx, "CREATE ROLE "+id+" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; REVOKE CREATE ON SCHEMA public FROM PUBLIC; GRANT USAGE ON SCHEMA public TO "+id+"; GRANT SELECT ON ALL TABLES IN SCHEMA public TO "+id); e != nil {
		t.Fatal(e)
	}
	if api {
		if _, e := d.pool.Exec(ctx, postgres.ProductRoleGrants(role)); e != nil {
			t.Fatal(e)
		}
	}
	cfg, e := pgxpool.ParseConfig(d.url.String())
	if e != nil {
		t.Fatal(e)
	}
	cfg.MaxConns = 1
	cfg.AfterConnect = func(ctx context.Context, c *pgx.Conn) error { _, e := c.Exec(ctx, "SET ROLE "+id); return e }
	p, e := pgxpool.NewWithConfig(ctx, cfg)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() {
		p.Close()
		if _, e := d.pool.Exec(context.Background(), "DROP OWNED BY "+id+"; DROP ROLE "+id); e != nil {
			t.Error(e)
		}
	})
	return p
}

// 原 Alembic 工程在隔离任务 cwd 执行；不加载 backend 下的本机 .env。
func emptyWriterMigrate(t *testing.T, d *database, revision string) {
	t.Helper()
	backend, e := filepath.Abs("../../../backend")
	if e != nil {
		t.Fatal(e)
	}
	python := os.Getenv("G1_TEST_PYTHON")
	work := os.Getenv("EMPTY_WRITER_TEST_WORKDIR")
	if python == "" || work == "" {
		t.Fatal("approved Python and isolated workdir required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	script := `import sys;sys.path.insert(0,sys.argv[1]);from alembic.config import Config;from alembic import command;c=Config();c.set_main_option('script_location',sys.argv[1]+'/migrations');command.upgrade(c,sys.argv[2])`
	cmd := exec.CommandContext(ctx, python, "-c", script, backend, revision)
	cmd.Dir = work
	password, _ := d.url.User.Password()
	env := map[string]string{}
	for _, entry := range os.Environ() {
		k, v, ok := strings.Cut(entry, "=")
		if ok {
			env[k] = v
		}
	}
	for k, v := range map[string]string{"POSTGRES_HOST": d.url.Hostname(), "POSTGRES_PORT": d.url.Port(), "POSTGRES_USER": d.url.User.Username(), "POSTGRES_PASSWORD": password, "POSTGRES_DB": d.name, "APP_ENV": "test", "PYTHONIOENCODING": "utf-8"} {
		env[k] = v
	}
	for k, v := range env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	output, e := cmd.CombinedOutput()
	if e != nil {
		t.Fatalf("isolated migration failed: %s", strings.ReplaceAll(string(output), password, "[REDACTED]"))
	}
}

func TestEmptyGoWriterInitializesAndOriginalGuards(t *testing.T) {
	d, i := emptyWriterDB(t, "head")
	ctx := context.Background()
	k := postgres.Cutover{Pool: d.pool}
	// 运行角色在初始化期间不连接；角色本身可先由 operator 授权。
	role := writerRole(t, d, true)
	if _, e := (postgres.ProductIdentity{Pool: role}).VerifyAPI(ctx); e == nil {
		t.Fatal("unsafe initial writer accepted")
	}
	role.Reset()
	r, e := k.InitializeEmptyGo(ctx, i, false)
	if e != nil || r.Decision != "DRY_RUN" {
		t.Fatalf("dry-run %s %v", r.Decision, e)
	}
	status, _ := k.Status(ctx)
	if status.Epoch != 1 || status.Mode != "PYTHON_ACTIVE" {
		t.Fatal(status)
	}
	r, e = k.InitializeEmptyGo(ctx, i, true)
	if e != nil || r.Decision != "APPLIED" {
		t.Fatalf("initialize %s %v", r.Decision, e)
	}
	for n, want := range []int64{1, 1, 2, 3} {
		if r.States[n].Epoch != want {
			t.Fatal(r.States)
		}
	}
	if _, e = k.InitializeEmptyGo(ctx, i, true); e == nil {
		t.Fatal("duplicate applied")
	}
	status, _ = k.Status(ctx)
	if status.Epoch != 3 || status.Mode != "GO_ACTIVE" || status.CutoverID != i.OperationID {
		t.Fatal(status)
	}
	if epoch, e := (postgres.ProductIdentity{Pool: role}).VerifyAPI(ctx); e != nil || epoch != 3 {
		t.Fatalf("original VerifyAPI %d %v", epoch, e)
	}
	if epoch, e := (postgres.Evaluation{Pool: role}).VerifyWorker(ctx, "c13a00100001"); e != nil || epoch != 3 {
		t.Fatalf("original VerifyWorker %d %v", epoch, e)
	}
	t.Logf("original Next states=1,1,2,3; original guards accepted epoch=%d; empty_tables=%d", status.Epoch, len(r.EmptyTables))
}

func TestEmptyGoWriterRejectsDataAndResourceMismatch(t *testing.T) {
	d, i := emptyWriterDB(t, "head")
	ctx := context.Background()
	k := postgres.Cutover{Pool: d.pool}
	for _, mutate := range []func(*postgres.EmptyWriterIdentity){func(b *postgres.EmptyWriterIdentity) { b.TaskID = strings.Repeat("f", 32) }, func(b *postgres.EmptyWriterIdentity) { b.DatabaseOID++ }, func(b *postgres.EmptyWriterIdentity) { b.SystemIdentifier = "1234567890123456789" }, func(b *postgres.EmptyWriterIdentity) { b.Volume = "old-volume" }} {
		bad := i
		mutate(&bad)
		if _, e := k.InitializeEmptyGo(ctx, bad, true); e == nil {
			t.Fatal("wrong resource accepted")
		}
	}
	if _, e := d.pool.Exec(ctx, `CREATE TABLE unknown_seed(value text);INSERT INTO unknown_seed VALUES('not-a-migration-seed')`); e != nil {
		t.Fatal(e)
	}
	if _, e := k.InitializeEmptyGo(ctx, i, true); e == nil {
		t.Fatal("unknown seed accepted")
	}
	if _, e := d.pool.Exec(ctx, `INSERT INTO organizations(id,name,created_at) VALUES($1,'E2E_TEST historical',clock_timestamp())`, asset.NewID()); e != nil {
		t.Fatal(e)
	}
	if _, e := k.InitializeEmptyGo(ctx, i, true); e == nil {
		t.Fatal("historical data accepted")
	}
	status, _ := k.Status(ctx)
	if status.Epoch != 1 || status.Mode != "PYTHON_ACTIVE" {
		t.Fatal("rejection mutated writer")
	}
}

func TestEmptyGoWriterRejectsOwnerSchemaAndConcurrentClaim(t *testing.T) {
	d, i := emptyWriterDB(t, "c13a00300001")
	ctx := context.Background()
	k := postgres.Cutover{Pool: d.pool}
	if _, e := k.InitializeEmptyGo(ctx, i, true); e == nil {
		t.Fatal("non-current schema accepted")
	}
	emptyWriterMigrate(t, d, "head")
	role := writerRole(t, d, false)
	if _, e := (postgres.Cutover{Pool: role}).InitializeEmptyGo(ctx, i, true); e == nil {
		t.Fatal("non-owner accepted")
	}
	role.Reset()
	tx, e := d.pool.Begin(ctx)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(173021,$1::integer)`, int64(i.DatabaseOID)); e != nil {
		t.Fatal(e)
	}
	cfg, _ := pgxpool.ParseConfig(d.url.String())
	cfg.MaxConns = 1
	other, e := pgxpool.NewWithConfig(ctx, cfg)
	if e != nil {
		t.Fatal(e)
	}
	_, err := (postgres.Cutover{Pool: other}).InitializeEmptyGo(ctx, i, true)
	other.Close()
	_ = tx.Rollback(ctx)
	if err == nil {
		t.Fatal("concurrent initialization accepted")
	}
	status, _ := k.Status(ctx)
	if status.Epoch != 1 {
		t.Fatal("rejected concurrent command changed epoch")
	}
}
