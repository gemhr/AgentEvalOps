//go:build integration

package postgres_test

import (
	"bytes"
	"context"
	"errors"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

const parentRevision = "a2c9f84e1b73"
const g1Revision = "c12a00100001"

type database struct {
	admin, pool *pgxpool.Pool
	url         *url.URL
	name        string
}

func newDatabase(t *testing.T) *database {
	t.Helper()
	raw := os.Getenv("G1_TEST_POSTGRES_URL")
	if raw == "" {
		t.Fatal("BLOCKED_BY_LOCAL_ENVIRONMENT: G1_TEST_POSTGRES_URL is required for real PostgreSQL integration")
	}
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	if (u.Hostname() != "127.0.0.1" && u.Hostname() != "localhost") || u.Port() != "55432" || u.Path != "/postgres" || u.User.Username() != "postgres" {
		t.Fatal("G1 tests require explicit isolated localhost:55432/postgres admin endpoint")
	}
	ctx := context.Background()
	admin, err := pgxpool.New(ctx, raw)
	if err != nil {
		t.Fatal(err)
	}
	if err = admin.Ping(ctx); err != nil {
		admin.Close()
		t.Fatal(err)
	}
	name := "agentevalops_g1_" + strings.ReplaceAll(asset.NewID(), "-", "")
	if _, err = admin.Exec(ctx, "CREATE DATABASE "+pgx.Identifier{name}.Sanitize()); err != nil {
		admin.Close()
		t.Fatal(err)
	}
	copyURL := *u
	copyURL.Path = "/" + name
	pool, err := pgxpool.New(ctx, copyURL.String())
	if err != nil {
		t.Fatal(err)
	}
	db := &database{admin: admin, pool: pool, url: &copyURL, name: name}
	t.Cleanup(func() {
		pool.Close()
		if !regexp.MustCompile(`^agentevalops_g1_[0-9a-f]{32}$`).MatchString(name) {
			t.Fatal("unsafe cleanup database identity")
		}
		if _, err := admin.Exec(context.Background(), "DROP DATABASE "+pgx.Identifier{name}.Sanitize()+" WITH (FORCE)"); err != nil {
			t.Errorf("cleanup: %v", err)
		}
		admin.Close()
	})
	return db
}
func (d *database) migrate(t *testing.T, revision string) {
	t.Helper()
	backend, err := filepath.Abs("../../../backend")
	if err != nil {
		t.Fatal(err)
	}
	python := os.Getenv("G1_TEST_PYTHON")
	if python == "" {
		if runtime.GOOS == "windows" {
			python = filepath.Join(backend, ".venv", "Scripts", "python.exe")
		} else {
			python = filepath.Join(backend, ".venv", "bin", "python")
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, python, "-m", "alembic", "upgrade", revision)
	cmd.Dir = backend
	password, _ := d.url.User.Password()
	env := map[string]string{}
	for _, entry := range os.Environ() {
		k, v, ok := strings.Cut(entry, "=")
		if ok {
			env[k] = v
		}
	}
	for k, v := range map[string]string{"POSTGRES_HOST": d.url.Hostname(), "POSTGRES_PORT": d.url.Port(), "POSTGRES_USER": d.url.User.Username(), "POSTGRES_PASSWORD": password, "POSTGRES_DB": d.name, "APP_ENV": "test", "AUTH_ENABLED": "true", "PYTHONIOENCODING": "utf-8"} {
		env[k] = v
	}
	for k, v := range env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("offline Alembic upgrade %s: %v\n%s", revision, err, output)
	}
	var actual string
	if err = d.pool.QueryRow(context.Background(), "SELECT version_num FROM alembic_version").Scan(&actual); err != nil {
		t.Fatal(err)
	}
	if revision != "head" && actual != revision {
		t.Fatalf("migration ledger: %s != %s", actual, revision)
	}
	t.Logf("offline Alembic upgrade %s -> ledger %s, PostgreSQL database %s", revision, actual, d.name)
}
func seedProject(t *testing.T, pool *pgxpool.Pool, org string) asset.Scope {
	t.Helper()
	ctx := context.Background()
	scope := asset.Scope{ProjectID: asset.NewID(), OrganizationID: org, Principal: "system:g1-test", CanPublish: true}
	if _, err := pool.Exec(ctx, "INSERT INTO organizations(id,name,created_at) VALUES($1,'G1 isolated',clock_timestamp()) ON CONFLICT(id) DO NOTHING", org); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, "INSERT INTO projects(id,org_id,name,description,created_at) VALUES($1,$2,$3,'',clock_timestamp())", scope.ProjectID, org, scope.ProjectID); err != nil {
		t.Fatal(err)
	}
	return scope
}
func (d *database) runtimePool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	role := "g1_runtime_" + strings.TrimPrefix(d.name, "agentevalops_g1_")
	ctx := context.Background()
	identifier := pgx.Identifier{role}.Sanitize()
	if _, err := d.admin.Exec(ctx, "CREATE ROLE "+identifier+" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"); err != nil {
		t.Fatal(err)
	}
	grant := `GRANT USAGE ON SCHEMA public TO ` + identifier + `; GRANT SELECT ON ALL TABLES IN SCHEMA public TO ` + identifier + `;`
	// PostgreSQL 的 SELECT FOR KEY SHARE 需要至少一个列的 UPDATE privilege。
	grant += "GRANT UPDATE(id) ON projects TO " + identifier + ";"
	for _, table := range []string{"evaluation_cases", "evaluation_case_versions", "evaluation_datasets", "evaluation_dataset_versions", "evaluation_suites", "evaluation_suite_versions", "evaluation_metric_definitions", "evaluation_metric_definition_versions", "evaluation_evaluator_definitions", "evaluation_evaluator_definition_versions"} {
		grant += "GRANT INSERT,UPDATE ON " + table + " TO " + identifier + ";"
	}
	for _, table := range []string{"evaluation_dataset_cases", "evaluation_suite_cases", "evaluation_suite_metrics", "evaluation_suite_evaluators", "evaluation_suite_evaluator_metrics", "evaluation_evaluator_metrics"} {
		grant += "GRANT INSERT ON " + table + " TO " + identifier + ";"
	}
	if _, err := d.pool.Exec(ctx, grant); err != nil {
		t.Fatal(err)
	}
	cfg, err := pgxpool.ParseConfig(d.url.String())
	if err != nil {
		t.Fatal(err)
	}
	cfg.AfterConnect = func(ctx context.Context, c *pgx.Conn) error {
		_, err := c.Exec(ctx, "SET ROLE "+identifier)
		return err
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		pool.Close()
		if _, err := d.admin.Exec(context.Background(), "DROP ROLE "+identifier); err != nil {
			t.Errorf("role cleanup: %v", err)
		}
	})
	// DB cleanup registered earlier executes last; role grants must be removed before DROP ROLE。
	t.Cleanup(func() {
		if _, err := d.pool.Exec(context.Background(), "DROP OWNED BY "+identifier); err != nil {
			t.Errorf("grant cleanup: %v", err)
		}
	})
	return pool
}
func expectError(t *testing.T, err, target error) {
	t.Helper()
	if !errors.Is(err, target) {
		t.Fatalf("got %v; want %v", err, target)
	}
}
func rejectSQL(t *testing.T, pool *pgxpool.Pool, code, sql string, args ...any) {
	t.Helper()
	_, err := pool.Exec(context.Background(), sql, args...)
	var pgerr *pgconn.PgError
	if !errors.As(err, &pgerr) || pgerr.Code != code {
		t.Fatalf("expected PostgreSQL %s; got %v", code, err)
	}
}
func testCase() catalog.CaseContent {
	input, _ := asset.ParseJSON([]byte(`{"input":"中😀<>&","large":9007199254740993,"float":1.0,"negative_zero":-0.0}`))
	return catalog.CaseContent{Input: input, TaskGoal: "完成用户目标", AcceptanceCriteria: []string{"保留证据", "答案正确"}, Type: catalog.AgentTask, Capability: "answer", Criticality: catalog.Critical, BodyPolicy: catalog.Retained, Applicability: asset.Applicability{RuleRef: "case-evidence.v1"}}
}

func TestPostgresCatalogPublicationAndSnapshots(t *testing.T) {
	db := newDatabase(t)
	db.migrate(t, "head")
	ctx := context.Background()
	a, b := seedProject(t, db.pool, asset.NewID()), seedProject(t, db.pool, asset.NewID())
	pool := db.runtimePool(t)
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: pool}, Datasets: postgres.Datasets{Pool: pool}, Suites: postgres.Suites{Pool: pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: pool}}
	cases := catalog.CaseService{Store: readers.Cases}
	datasets := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	suites := catalog.SuiteService{Store: readers.Suites, References: readers}
	metrics := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
	evaluators := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	source := asset.Source{Kind: "TEST", Ref: "g1-fixture.v1", Principal: a.Principal}
	ref := func() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "v1"} }
	c1, c2, foreign := ref(), ref(), ref()
	for _, r := range []asset.Ref{c1, c2} {
		_, err := cases.CreateCase(ctx, a, asset.Create{ID: r.EntityID, Name: "Case"})
		if err != nil {
			t.Fatal(err)
		}
		_, err = cases.PublishCaseVersion(ctx, a, asset.Publish[catalog.CaseContent]{Ref: r, Body: testCase(), Source: source})
		if err != nil {
			t.Fatal(err)
		}
	}
	_, err := cases.CreateCase(ctx, b, asset.Create{ID: foreign.EntityID, Name: "foreign"})
	if err != nil {
		t.Fatal(err)
	}
	_, err = cases.PublishCaseVersion(ctx, b, asset.Publish[catalog.CaseContent]{Ref: foreign, Body: testCase(), Source: source})
	if err != nil {
		t.Fatal(err)
	}
	d, s, m, e := ref(), ref(), ref(), ref()
	_, err = datasets.CreateDataset(ctx, a, asset.Create{ID: d.EntityID, Name: "Dataset"})
	if err != nil {
		t.Fatal(err)
	}
	datasetCmd := asset.Publish[catalog.DatasetContent]{Ref: d, Body: catalog.DatasetContent{Cases: []asset.Ref{c2, c1}}, Source: source}
	dataset, err := datasets.PublishDatasetVersion(ctx, a, datasetCmd)
	if err != nil {
		t.Fatal(err)
	}
	_, err = metrics.CreateMetricDefinition(ctx, a, asset.Create{ID: m.EntityID, Name: "Task Success"})
	if err != nil {
		t.Fatal(err)
	}
	_, err = metrics.PublishMetricDefinitionVersion(ctx, a, asset.Publish[metric.Definition]{Ref: m, Body: metric.Builtins()[0].Definition, Source: source})
	if err != nil {
		t.Fatal(err)
	}
	_, err = evaluators.CreateEvaluatorDefinition(ctx, a, asset.Create{ID: e.EntityID, Name: "future Task Success"})
	if err != nil {
		t.Fatal(err)
	}
	app := asset.Applicability{RuleRef: "eligible-evidence.v1"}
	evalBody := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: "case-input.v1", OutputMetrics: []asset.Ref{m}, Applicability: app, Availability: asset.ContractOnly, ImplementationRef: "task-success-contract-only.v1", SchemaVersion: "result.v1", Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 1024}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}, Normalization: "null-preserving.v1"}
	_, err = evaluators.PublishEvaluatorDefinitionVersion(ctx, a, asset.Publish[metric.EvaluatorDefinition]{Ref: e, Body: evalBody, Source: source})
	if err != nil {
		t.Fatal(err)
	}
	binding := catalog.EvaluatorBinding{Evaluator: e, Metrics: []asset.Ref{m}, Required: false, Applicability: app}
	_, err = suites.CreateSuite(ctx, a, asset.Create{ID: s.EntityID, Name: "Suite"})
	if err != nil {
		t.Fatal(err)
	}
	suiteBody := catalog.SuiteContent{Dataset: &d, Evaluators: []catalog.EvaluatorBinding{binding}, Metrics: []asset.Ref{m}, Applicability: app}
	_, err = suites.PublishSuiteVersion(ctx, a, asset.Publish[catalog.SuiteContent]{Ref: s, Body: suiteBody, Source: source})
	if err != nil {
		t.Fatal(err)
	}

	t.Run("project isolation and command authorization", func(t *testing.T) {
		_, err := cases.GetCaseVersion(ctx, b, c1)
		expectError(t, err, asset.ErrNotFound)
		_, err = cases.GetCase(ctx, b, c1.EntityID)
		expectError(t, err, asset.ErrNotFound)
		wrongOrg := a
		wrongOrg.OrganizationID = b.OrganizationID
		_, err = cases.GetCaseVersion(ctx, wrongOrg, c1)
		expectError(t, err, asset.ErrNotFound)
		readonly := a
		readonly.CanPublish = false
		_, err = cases.CreateCase(ctx, readonly, asset.Create{ID: asset.NewID(), Name: "denied"})
		expectError(t, err, asset.ErrForbidden)
		items, err := cases.ListCases(ctx, b, 10)
		if err != nil || len(items) != 1 || items[0].ID != foreign.EntityID {
			t.Fatalf("list scope: %+v %v", items, err)
		}
		versions, err := cases.ListCaseVersions(ctx, a, c1.EntityID, 10)
		if err != nil || len(versions) != 1 {
			t.Fatalf("list versions: %v", err)
		}
	})
	t.Run("duplicate publication and label-content separation", func(t *testing.T) {
		again, err := datasets.PublishDatasetVersion(ctx, a, datasetCmd)
		if err != nil || !bytes.Equal(again.Bytes(), dataset.Bytes()) || again.PublishedAt() != dataset.PublishedAt() {
			t.Fatalf("idempotent publication: %v", err)
		}
		changed := datasetCmd
		changed.Body.Cases = []asset.Ref{c1, c2}
		_, err = datasets.PublishDatasetVersion(ctx, a, changed)
		expectError(t, err, asset.ErrConflict)
		changed = datasetCmd
		changed.Ref.Version = "v2"
		next, err := datasets.PublishDatasetVersion(ctx, a, changed)
		if err != nil || next.SemanticDigest() != dataset.SemanticDigest() || next.Ref() == dataset.Ref() {
			t.Fatalf("label/content: %v", err)
		}
		// logical display metadata 可变，而旧 version canonical bytes 不变。
		_, err = pool.Exec(ctx, "UPDATE evaluation_datasets SET name='new display name' WHERE project_id=$1 AND id=$2", a.ProjectID, d.EntityID)
		if err != nil {
			t.Fatal(err)
		}
		frozen, err := datasets.GetDatasetVersion(ctx, a, d)
		if err != nil || !bytes.Equal(frozen.Bytes(), dataset.Bytes()) {
			t.Fatal("logical metadata rewrote publication", err)
		}
	})
	t.Run("published content and references immutable in migrated PostgreSQL", func(t *testing.T) {
		for _, table := range []string{"evaluation_case_versions", "evaluation_dataset_versions", "evaluation_suite_versions", "evaluation_metric_definition_versions", "evaluation_evaluator_definition_versions"} {
			rejectSQL(t, pool, "55000", "UPDATE "+table+" SET content=content WHERE project_id=$1", a.ProjectID)
			rejectSQL(t, db.pool, "55000", "DELETE FROM "+table+" WHERE project_id=$1", a.ProjectID)
			rejectSQL(t, pool, "42501", "TRUNCATE "+table+" CASCADE")
			rejectSQL(t, db.pool, "55000", "TRUNCATE "+table+" CASCADE")
		}
		rejectSQL(t, db.pool, "55000", "UPDATE evaluation_dataset_cases SET position=position WHERE project_id=$1", a.ProjectID)
		rejectSQL(t, db.pool, "55000", "DELETE FROM evaluation_suite_evaluator_metrics WHERE project_id=$1", a.ProjectID)
		rejectSQL(t, db.pool, "55000", "INSERT INTO evaluation_dataset_cases VALUES($1,$2,$3,2,$4,$5)", a.ProjectID, d.EntityID, d.Version, c1.EntityID, c1.Version)
		rejectSQL(t, db.pool, "23503", "DELETE FROM evaluation_cases WHERE project_id=$1 AND id=$2", a.ProjectID, c1.EntityID)
	})
	t.Run("cross-project references and atomic rollback", func(t *testing.T) {
		failed := datasetCmd
		failed.Ref.Version = "invalid"
		failed.Body.Cases = []asset.Ref{c1, foreign}
		_, err := datasets.PublishDatasetVersion(ctx, a, failed)
		expectError(t, err, asset.ErrNotFound)
		_, err = readers.Datasets.PublishDatasetVersion(ctx, a, failed)
		expectError(t, err, asset.ErrNotFound)
		var count int
		if err = pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_dataset_versions WHERE project_id=$1 AND entity_id=$2 AND version='invalid'", a.ProjectID, d.EntityID).Scan(&count); err != nil || count != 0 {
			t.Fatal("publication did not roll back", err)
		}
		if err = pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_dataset_cases WHERE project_id=$1 AND dataset_id=$2 AND dataset_version='invalid'", a.ProjectID, d.EntityID).Scan(&count); err != nil || count != 0 {
			t.Fatal("relation did not roll back", err)
		}
		// 绕过应用，真实复合 FK 仍拒绝 Project A dataset → Project B case。
		tx, err := db.pool.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback(ctx)
		_, err = tx.Exec(ctx, `INSERT INTO evaluation_dataset_versions(project_id,entity_id,version,algorithm_ref,content_digest,semantic_digest,canonical_bytes,content,published_by) SELECT project_id,entity_id,'bad-fk',algorithm_ref,content_digest,semantic_digest,canonical_bytes,content,published_by FROM evaluation_dataset_versions WHERE project_id=$1 AND entity_id=$2 AND version=$3`, a.ProjectID, d.EntityID, d.Version)
		if err != nil {
			t.Fatal(err)
		}
		_, err = tx.Exec(ctx, "INSERT INTO evaluation_dataset_cases VALUES($1,$2,'bad-fk',0,$3,$4)", a.ProjectID, d.EntityID, foreign.EntityID, foreign.Version)
		var pgerr *pgconn.PgError
		if !errors.As(err, &pgerr) || pgerr.Code != "23503" {
			t.Fatal("cross-project FK not enforced", err)
		}
	})
	t.Run("suite references and frozen ordered Run input", func(t *testing.T) {
		builder := evaluation.Builder{Assets: readers}
		snapshot, err := builder.BuildRunSnapshot(ctx, a, evaluation.BuildRunSnapshot{Suite: &s})
		if err != nil {
			t.Fatal(err)
		}
		input := snapshot.Input()
		if len(input.Manifest) != 2 || input.Manifest[0].Identity.Ref != c2 || input.Manifest[1].Identity.Ref != c1 || input.Evaluators[0].Required || input.Manifest[0].Case.TaskGoal != "完成用户目标" || input.Metrics[0].Identity.Ref != m {
			t.Fatal("snapshot contract", input)
		}
		var refs []string
		rows, err := pool.Query(ctx, "SELECT case_id::text FROM evaluation_dataset_cases WHERE project_id=$1 AND dataset_id=$2 AND dataset_version=$3 ORDER BY position", a.ProjectID, d.EntityID, d.Version)
		if err != nil {
			t.Fatal(err)
		}
		for rows.Next() {
			var id string
			if err = rows.Scan(&id); err != nil {
				t.Fatal(err)
			}
			refs = append(refs, id)
		}
		rows.Close()
		if rows.Err() != nil || len(refs) != 2 || refs[0] != c2.EntityID {
			t.Fatal("SQL case ordering", refs)
		}
		foreignMetric := ref()
		_, err = metrics.CreateMetricDefinition(ctx, b, asset.Create{ID: foreignMetric.EntityID, Name: "foreign metric"})
		if err != nil {
			t.Fatal(err)
		}
		_, err = metrics.PublishMetricDefinitionVersion(ctx, b, asset.Publish[metric.Definition]{Ref: foreignMetric, Body: metric.Builtins()[0].Definition, Source: source})
		if err != nil {
			t.Fatal(err)
		}
		foreignEval := ref()
		_, err = evaluators.CreateEvaluatorDefinition(ctx, b, asset.Create{ID: foreignEval.EntityID, Name: "foreign evaluator"})
		if err != nil {
			t.Fatal(err)
		}
		foreignBody := evalBody
		foreignBody.OutputMetrics = []asset.Ref{foreignMetric}
		_, err = evaluators.PublishEvaluatorDefinitionVersion(ctx, b, asset.Publish[metric.EvaluatorDefinition]{Ref: foreignEval, Body: foreignBody, Source: source})
		if err != nil {
			t.Fatal(err)
		}
		invalidSuite := asset.Publish[catalog.SuiteContent]{Ref: asset.Ref{EntityID: s.EntityID, Version: "bad-case"}, Body: suiteBody, Source: source}
		invalidSuite.Body.Dataset = nil
		invalidSuite.Body.Cases = []asset.Ref{foreign}
		_, err = suites.PublishSuiteVersion(ctx, a, invalidSuite)
		expectError(t, err, asset.ErrNotFound)
		invalidSuite.Body = suiteBody
		invalidSuite.Ref.Version = "bad-metric"
		invalidSuite.Body.Metrics = []asset.Ref{foreignMetric}
		invalidBinding := binding
		invalidBinding.Metrics = []asset.Ref{foreignMetric}
		invalidSuite.Body.Evaluators = []catalog.EvaluatorBinding{invalidBinding}
		_, err = suites.PublishSuiteVersion(ctx, a, invalidSuite)
		expectError(t, err, asset.ErrNotFound)
		invalidSuite.Body = suiteBody
		invalidSuite.Ref.Version = "bad-evaluator"
		invalidBinding = binding
		invalidBinding.Evaluator = foreignEval
		invalidSuite.Body.Evaluators = []catalog.EvaluatorBinding{invalidBinding}
		_, err = suites.PublishSuiteVersion(ctx, a, invalidSuite)
		expectError(t, err, asset.ErrNotFound)
		// Suite explicit-case path 同样可发布，禁止把两种来源混合。
		// 绕过 application 和 repository，分别证明 Suite 三类复合 FK。
		rejectForeignRelation := func(sql string, args ...any) {
			t.Helper()
			tx, err := db.pool.Begin(ctx)
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback(ctx)
			_, err = tx.Exec(ctx, `INSERT INTO evaluation_suite_versions(project_id,entity_id,version,algorithm_ref,content_digest,semantic_digest,canonical_bytes,content,published_by)
			 SELECT project_id,entity_id,'foreign-fk',algorithm_ref,content_digest,semantic_digest,canonical_bytes,content,published_by FROM evaluation_suite_versions WHERE project_id=$1 AND entity_id=$2 AND version=$3`, a.ProjectID, s.EntityID, s.Version)
			if err != nil {
				t.Fatal(err)
			}
			_, err = tx.Exec(ctx, sql, args...)
			var pgerr *pgconn.PgError
			if !errors.As(err, &pgerr) || pgerr.Code != "23503" {
				t.Fatalf("Suite scoped FK not enforced: %v", err)
			}
		}
		rejectForeignRelation("INSERT INTO evaluation_suite_cases VALUES($1,$2,'foreign-fk',0,$3,$4)", a.ProjectID, s.EntityID, foreign.EntityID, foreign.Version)
		rejectForeignRelation("INSERT INTO evaluation_suite_metrics VALUES($1,$2,'foreign-fk',0,$3,$4)", a.ProjectID, s.EntityID, foreignMetric.EntityID, foreignMetric.Version)
		rejectForeignRelation("INSERT INTO evaluation_suite_evaluators VALUES($1,$2,'foreign-fk',0,$3,$4,false,'{}')", a.ProjectID, s.EntityID, foreignEval.EntityID, foreignEval.Version)
		explicit := asset.Publish[catalog.SuiteContent]{Ref: asset.Ref{EntityID: s.EntityID, Version: "explicit"}, Body: suiteBody, Source: source}
		explicit.Body.Dataset = nil
		explicit.Body.Cases = []asset.Ref{c1}
		_, err = suites.PublishSuiteVersion(ctx, a, explicit)
		if err != nil {
			t.Fatal(err)
		}
	})
	t.Run("unsealed publication cannot commit", func(t *testing.T) {
		tx, err := db.pool.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		_, err = tx.Exec(ctx, `INSERT INTO evaluation_case_versions(project_id,entity_id,version,algorithm_ref,content_digest,semantic_digest,canonical_bytes,content,published_by) SELECT project_id,entity_id,'unsealed',algorithm_ref,content_digest,semantic_digest,canonical_bytes,content,published_by FROM evaluation_case_versions WHERE project_id=$1 AND entity_id=$2 AND version=$3`, a.ProjectID, c1.EntityID, c1.Version)
		if err != nil {
			t.Fatal(err)
		}
		err = tx.Commit(ctx)
		var pgerr *pgconn.PgError
		if !errors.As(err, &pgerr) || pgerr.Code != "23514" {
			t.Fatal("unsealed publication committed", err)
		}
	})
}

func TestMigrationUpgradeAndHistoricalCompatibility(t *testing.T) {
	db := newDatabase(t)
	db.migrate(t, parentRevision)
	ctx := context.Background()
	scope := seedProject(t, db.pool, asset.NewID())
	run, attempt, result := asset.NewID(), asset.NewID(), asset.NewID()
	_, err := db.pool.Exec(ctx, `INSERT INTO evaluation_runs(id,project_id,dataset_id,dataset_version,suite_id,suite_version,execution_target_id,execution_target_kind,target_version_kind,target_version_value,dataset_snapshot,suite_snapshot,execution_target_snapshot,status,metadata,created_at,started_at)
 VALUES($1,$2,'legacy-dataset','d1','legacy-suite','s1','target','FIXTURE','git','abc','{"dataset_id":"legacy-dataset","version":"d1"}','{"suite_id":"legacy-suite","version":"s1","legacy_digest":"original-digest"}','{}','RUNNING','{}',clock_timestamp(),clock_timestamp())`, run, scope.ProjectID)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.pool.Exec(ctx, `INSERT INTO evaluation_attempts(id,project_id,run_id,case_id,case_version,attempt_no,execution_target_id,execution_target_kind,target_version_kind,target_version_value,execution_request_id,idempotency_key,request_snapshot,status,claim_token,created_at,claimed_at,started_at,finished_at,lease_expires_at,execution_outcome_kind,output_artifact_ref,outcome_evidence_refs,outcome_metadata)
 VALUES($1,$2,$3,'opaque-case','v1',1,'target','FIXTURE','git','abc','request','stable','{"input_payload":{"x":1.0},"timeout_seconds":1,"case_snapshot":{"semantic_digest":"original-case-digest"}}','TERMINAL',$4,clock_timestamp(),clock_timestamp(),clock_timestamp(),clock_timestamp(),clock_timestamp(),'SUCCESS','{"artifact_id":"a"}','[]','{}')`, attempt, scope.ProjectID, run, asset.NewID())
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.pool.Exec(ctx, `INSERT INTO evaluation_results(id,project_id,run_id,attempt_id,dataset_id,dataset_version,case_id,case_version,suite_id,suite_version,evaluator_id,evaluator_version,config_ref_kind,config_ref_value,execution_target_id,target_version_kind,target_version_value,execution_request_id,verdict,reason,provenance_completeness,evidence_refs,metadata,created_at)
 VALUES($1,$2,$3,$4,'legacy-dataset','d1','opaque-case','v1','legacy-suite','s1','legacy-evaluator','e1','cfg','1','target','git','abc','request','FAIL','historical','COMPLETE','[]','{"definition_digest":"original-spec-digest"}',clock_timestamp())`, result, scope.ProjectID, run, attempt)
	if err != nil {
		t.Fatal(err)
	}
	read := func(table, id string) string {
		var row string
		if err := db.pool.QueryRow(ctx, "SELECT row_to_json(r)::text FROM "+table+" r WHERE id=$1", id).Scan(&row); err != nil {
			t.Fatal(err)
		}
		return row
	}
	beforeRun, beforeAttempt, beforeResult := read("evaluation_runs", run), read("evaluation_attempts", attempt), read("evaluation_results", result)
	db.migrate(t, "head")
	if read("evaluation_runs", run) != beforeRun || read("evaluation_attempts", attempt) != beforeAttempt || read("evaluation_results", result) != beforeResult {
		t.Fatal("additive G1 migration rewrote UUID/content/digest/Result")
	}
	builder := evaluation.Builder{Legacy: postgres.LegacyRunInputs{Pool: db.pool}}
	snapshot, err := builder.BuildLegacyRunSnapshot(ctx, scope, run)
	if err != nil {
		t.Fatal(err)
	}
	input := snapshot.Input()
	if input.Origin != evaluation.LegacySnapshot || input.Dataset != nil || input.Suite != nil || len(input.Legacy.Attempts) != 1 || input.Legacy.Attempts[0].AttemptID != attempt || !strings.Contains(input.Legacy.Attempts[0].RequestSnapshot.String(), "original-case-digest") {
		t.Fatal("legacy read compatibility", input)
	}
	foreign := seedProject(t, db.pool, asset.NewID())
	_, err = builder.BuildLegacyRunSnapshot(ctx, foreign, run)
	expectError(t, err, asset.ErrNotFound)
	var count int
	if err = db.pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_datasets").Scan(&count); err != nil || count != 0 {
		t.Fatal("history fabricated Dataset", err)
	}
	rejectSQL(t, db.pool, "P0001", "UPDATE evaluation_results SET verdict='PASS' WHERE id=$1", result)
	rejectSQL(t, db.pool, "P0001", "DELETE FROM evaluation_results WHERE id=$1", result)
	if read("evaluation_results", result) != beforeResult {
		t.Fatal("historical result changed")
	}
	t.Logf("historical UUIDs preserved; Run/Attempt/Result complete rows byte-identical before/after additive upgrade; legacy snapshot remains scoped readable")
}
