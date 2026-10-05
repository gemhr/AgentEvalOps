//go:build integration

package postgres_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/cutover"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"agentevalops/go-backend/internal/worker"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func g10Evidence(epoch int64, roles ...string) cutover.Evidence {
	e := cutover.Evidence{CutoverID: asset.NewID(), Epoch: epoch, Operator: "G10A_CONTROLLED_OPERATOR", LegacyRoles: roles, Checks: map[string]string{}, Drain: map[string]*int64{}, BackupRef: "CONTROLLED_FIXTURE_ONLY", BackupDigest: strings.Repeat("a", 64), Consumers: []cutover.Consumer{{Name: "controlled-client", Owner: "test fixture", Current: "controlled-old", Target: "controlled-go", Status: "VERIFIED", Rollback: "restore or forward repair", VerifiedAt: time.Now().UTC().Format(time.RFC3339), Kind: "CONTROLLED"}}}
	for _, name := range cutover.MandatoryChecks {
		e.Checks[name] = "VERIFIED"
	}
	for _, name := range cutover.DrainMetrics {
		e.Drain[name] = new(int64)
	}
	e.ConsumerDigest = cutover.ConsumerDigest(e.Consumers)
	return e
}
func g10Role(t *testing.T, d *database) string {
	t.Helper()
	role := "g10_retired_" + strings.TrimPrefix(d.name, "agentevalops_g1_")
	q := pgx.Identifier{role}.Sanitize()
	_, err := d.admin.Exec(context.Background(), "CREATE ROLE "+q+" NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS")
	mustG7(t, err)
	t.Cleanup(func() {
		_, e := d.pool.Exec(context.Background(), "DROP OWNED BY "+q)
		mustG7(t, e)
		_, e = d.admin.Exec(context.Background(), "DROP ROLE "+q)
		mustG7(t, e)
	})
	return role
}
func g10ArtifactEvidence(t *testing.T, e *cutover.Evidence) {
	t.Helper()
	path := os.Getenv("G10_RELEASE_MANIFEST")
	b, err := os.ReadFile(path)
	mustG7(t, err)
	e.ReleaseDigest = cutover.Digest(b)
	for _, c := range cutover.VerifyRelease(path, "../../..", e.ReleaseDigest, true) {
		if c.Status != "PASS" {
			t.Fatal("release mismatch", c)
		}
	}
	dirtyBlocked := false
	for _, c := range cutover.VerifyRelease(path, "../../..", e.ReleaseDigest, false) {
		if c.Name == "artifact_cleanliness" && c.Status == "BLOCKED" {
			dirtyBlocked = true
		}
	}
	var manifest cutover.Manifest
	mustG7(t, json.Unmarshal(b, &manifest))
	if manifest.Dirty == "true" && !dirtyBlocked {
		t.Fatal("dirty controlled release not blocked for production")
	}
}
func g10CLI(t *testing.T, d *database, e cutover.Evidence, command string, execute bool, receipts ...string) ([]byte, error) {
	t.Helper()
	b, err := json.Marshal(e)
	mustG7(t, err)
	p := filepath.Join(t.TempDir(), "evidence.json")
	mustG7(t, os.WriteFile(p, b, 0600))
	args := []string{"--controlled", "--command", command, "--evidence", p, "--manifest", os.Getenv("G10_RELEASE_MANIFEST"), "--repo-root", "../../.."}
	if execute {
		receipt := filepath.Join(t.TempDir(), "receipt.json")
		if len(receipts) > 0 {
			receipt = receipts[0]
		}
		args = append(args, "--execute", "--receipt", receipt)
	}
	cmd := exec.Command(g9Binary(t, "cutoverctl"), args...)
	cmd.Env = g9Env(map[string]string{"DATABASE_URL": d.url.String()})
	return cmd.CombinedOutput()
}

func g10PythonTrace(t *testing.T, d *database, role string, rejected bool) {
	t.Helper()
	backend, err := filepath.Abs("../../../backend")
	mustG7(t, err)
	python := os.Getenv("G1_TEST_PYTHON")
	if python == "" {
		if runtime.GOOS == "windows" {
			python = filepath.Join(backend, ".venv", "Scripts", "python.exe")
		} else {
			python = filepath.Join(backend, ".venv", "bin", "python")
		}
	}
	payload, _ := json.Marshal(map[string]any{"trace_id": asset.NewID(), "project_id": os.Getenv("G10_PYTHON_PROJECT"), "name": "controlled-old-celery-body", "started_at": "2026-10-05T00:00:00Z"})
	script := `import asyncio,json,os,sys
from app.infrastructure.queue.tasks import _persist_trace
try:
    asyncio.run(_persist_trace(json.loads(os.environ['G10_TRACE_PAYLOAD'])))
except Exception as exc:
    code=getattr(getattr(exc,'orig',None),'sqlstate',None)
    print(json.dumps({'status':'REJECTED','sqlstate':code}))
    sys.exit(0 if code=='42501' and os.environ['G10_REJECT']=='true' else 1)
else:
    print(json.dumps({'status':'APPLIED'}))
    sys.exit(0 if os.environ['G10_REJECT']=='false' else 1)
`
	command := exec.Command(python, "-c", script)
	command.Dir = backend
	command.Env = g9Env(map[string]string{"POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "55432", "POSTGRES_DB": d.name, "POSTGRES_USER": role, "POSTGRES_PASSWORD": "g10a-reader-fixture-only", "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "56379", "APP_ENV": "test", "AUTH_ENABLED": "false", "G10_TRACE_PAYLOAD": string(payload), "G10_REJECT": fmt.Sprint(rejected), "LOG_DIR": t.TempDir(), "PYTHONIOENCODING": "utf-8"})
	raw, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("controlled Celery task body: %v %s", err, raw)
	}
	if rejected && !bytes.Contains(raw, []byte(`"sqlstate": "42501"`)) {
		t.Fatal("wrong Python rejection", string(raw))
	}
}

func TestG10ControlledWriterBarrierAndReader(t *testing.T) {
	f := fixtureWithWriter(t, false)
	ctx := context.Background()
	k := postgres.Cutover{Pool: f.db.pool}
	legacy := g10Role(t, f.db)
	quoted := pgx.Identifier{legacy}.Sanitize()
	_, err := f.db.pool.Exec(ctx, "GRANT USAGE ON SCHEMA public TO "+quoted+";GRANT SELECT,INSERT,UPDATE,DELETE,TRUNCATE ON ALL TABLES IN SCHEMA public TO "+quoted)
	mustG7(t, err)
	_, err = f.db.pool.Exec(ctx, "ALTER ROLE "+quoted+" LOGIN PASSWORD 'g10a-reader-fixture-only'")
	mustG7(t, err)
	t.Setenv("G10_PYTHON_PROJECT", f.s.ProjectID)
	e := g10Evidence(1, legacy)
	g10ArtifactEvidence(t, &e)
	s, err := k.Status(ctx)
	mustG7(t, err)
	if s.Mode != "PYTHON_ACTIVE" || s.Epoch != 1 {
		t.Fatal(s)
	}
	raw, err := g10CLI(t, f.db, e, "enter-drain", false)
	mustG7(t, err)
	if !bytes.Contains(raw, []byte("DRY_RUN")) {
		t.Fatal(string(raw))
	}
	s, err = k.Status(ctx)
	mustG7(t, err)
	if s.Mode != "PYTHON_ACTIVE" {
		t.Fatal("dry-run mutated", s)
	}
	receiptPath := filepath.Join(t.TempDir(), "drain-receipt.json")
	_, err = g10CLI(t, f.db, e, "enter-drain", true, receiptPath)
	mustG7(t, err)
	s, err = k.Status(ctx)
	mustG7(t, err)
	if s.Epoch != 1 || s.Mode != "PYTHON_DRAINING" {
		t.Fatal(s)
	}
	g10PythonTrace(t, f.db, legacy, false)
	r, err := k.Preflight(ctx, e, true)
	mustG7(t, err)
	if r.Status != "BLOCKED" {
		t.Fatal("unrevoked legacy passed")
	}
	// 旧任务已收尾后撤权；这些零值只描述本测试，无 Celery 生产统计含义。
	readerSQL, err := postgres.LegacyReaderGrants(legacy, []string{"traces", "spans", "projects"})
	mustG7(t, err)
	_, err = f.db.pool.Exec(ctx, readerSQL)
	mustG7(t, err)
	// 表级 REVOKE 不删除既有列授权，必须由 preflight 检出。
	_, err = f.db.pool.Exec(ctx, "GRANT UPDATE(id) ON projects TO "+quoted)
	mustG7(t, err)
	r, err = k.Preflight(ctx, e, true)
	mustG7(t, err)
	if r.Status != "BLOCKED" {
		t.Fatal("column privilege escaped")
	}
	_, err = f.db.pool.Exec(ctx, "REVOKE UPDATE(id) ON projects FROM "+quoted)
	mustG7(t, err)
	r, err = k.Preflight(ctx, e, true)
	mustG7(t, err)
	if r.Status != "PASS" {
		t.Fatal(r)
	}
	receiptBefore, err := os.ReadFile(receiptPath)
	mustG7(t, err)
	if _, err = g10CLI(t, f.db, e, "enter-barrier", true, receiptPath); err == nil {
		t.Fatal("receipt overwritten")
	}
	receiptAfter, err := os.ReadFile(receiptPath)
	mustG7(t, err)
	if !bytes.Equal(receiptBefore, receiptAfter) {
		t.Fatal("receipt changed")
	}
	// 两个真实 CLI 进程竞争同一 epoch：只有一次 canonical 转换。
	var wg sync.WaitGroup
	results := make(chan error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _, e2 := g10CLI(t, f.db, e, "enter-barrier", true); results <- e2 }()
	}
	wg.Wait()
	close(results)
	success := 0
	for e2 := range results {
		if e2 == nil {
			success++
		}
	}
	if success != 1 {
		t.Fatalf("M04 successful transitions %d", success)
	}
	s, err = k.Status(ctx)
	mustG7(t, err)
	if s.Mode != "BARRIER" || s.Epoch != 2 {
		t.Fatal(s)
	}
	if _, err = k.Transition(ctx, "abort-before-go-write", e); err == nil {
		t.Fatal("frozen abort accepted")
	}
	e.Epoch = 2
	s, err = k.Transition(ctx, "activate-go", e)
	mustG7(t, err)
	if s.Mode != "GO_ACTIVE" || s.Epoch != 3 {
		t.Fatal(s)
	}
	f.s.Epoch = s.Epoch
	// 用独立 SET ROLE 会话证明 SELECT 可用，而全域 DML/DDL/control 均被 DB 拒绝。
	c, err := pgx.Connect(ctx, f.db.url.String())
	mustG7(t, err)
	defer c.Close(ctx)
	_, err = c.Exec(ctx, "SET ROLE "+quoted)
	mustG7(t, err)
	_, err = c.Exec(ctx, "SELECT trace_id FROM traces LIMIT 1")
	mustG7(t, err)
	for _, sql := range []string{"INSERT INTO evaluation_runs DEFAULT VALUES", "UPDATE evaluation_results SET id=id WHERE false", "DELETE FROM evaluation_gate_receipts WHERE false", "UPDATE evaluation_review_items SET id=id WHERE false", "TRUNCATE evaluation_attempts", "UPDATE evaluation_writer_control SET writer_epoch=writer_epoch+1", "INSERT INTO traces DEFAULT VALUES", "CREATE TABLE g10_forbidden(id int)"} {
		if _, err = c.Exec(ctx, sql); err == nil {
			t.Fatal("legacy reader mutation succeeded", sql)
		}
	}
	g10PythonTrace(t, f.db, legacy, true)
	// 首个 Go canonical 写入之后，Python 恢复和旧 epoch 均保持拒绝。
	controlled := g3FromKernel(t, f, worker.FixturePlan{Mode: "SUCCESS"}, worker.FixturePlan{Mode: "PASS"})
	o := g3Create(t, controlled, nil)
	spawnWorker(t, g9Binary(t, "worker"), controlled, "g10-new-epoch")
	await(t, func() bool { return g3State(t, controlled, o.RunID).Run.Status == ev.RunCompleted })
	stale := f.s
	stale.Epoch = 1
	cmd := f.cmd
	cmd.CommandID = asset.NewID()
	reply, e2 := f.k.CreateRun(ctx, stale, cmd)
	if e2 == nil && reply.Code == ev.Applied {
		t.Fatal("old epoch accepted")
	}
	if _, err = f.db.pool.Exec(ctx, "UPDATE evaluation_writer_control SET mode='PYTHON_ACTIVE',active_writer='PYTHON',writer_epoch=writer_epoch+1"); err == nil {
		t.Fatal("post-write Python resume accepted")
	}
	t.Log("C01-C09/C12/C16: Python baseline → drain → revoked role → barrier E2 → Go E3 → completed Run; M04 one transition; frozen abort requires restore/forward fix")
}

func TestG10TraceFacadeAndOldProcesses(t *testing.T) {
	x := g8(t)
	ctx := context.Background()
	k := postgres.Cutover{Pool: x.db.pool}
	legacy := g10Role(t, x.db)
	_, err := x.db.pool.Exec(ctx, postgres.TraceIngestRoleGrants(x.role))
	mustG7(t, err)
	dev := identity.DevAuth{Environment: "test", PrincipalID: x.principals["judge"].ID, OrganizationID: x.s.OrganizationID, Password: strings.Repeat("g10-control-", 3), SigningKey: []byte(strings.Repeat("g10-sign-", 4)), Lifetime: time.Hour}
	binary := g9Binary(t, "api")
	old := startG9API(t, x, binary, dev)
	oldWorker := spawnWorker(t, g9Binary(t, "worker"), x.g3Fixture, "g10-old-epoch", "--online-concurrency", "0")
	login := g9Request(t, old, "", "POST", "/api/v1/auth/dev-login", "", map[string]any{"password": dev.Password}, 200)
	token := login["access_token"].(string)
	prefix := "/api/v1/projects/" + x.s.ProjectID
	key := g9Request(t, old, token, "POST", prefix+"/api-keys", "g10-trace-key", map[string]any{"name": "G10 fixture only", "capabilities": []string{"READ", "WRITE"}}, 200)["secret"].(string)
	// 故意留下旧进程持有的工作，验证 epoch 失效；不代表正常 drain 已结束。
	blocked := x.runtimeCommand
	blocked.CommandID = asset.NewID()
	blocked.Target.Config, _ = asset.Freeze(worker.FixturePlan{Mode: "BLOCK"})
	input := blocked.Snapshot.Input()
	bindings := []catalog.EvaluatorBinding{}
	for _, spec := range input.Evaluators {
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: spec.Identity.Ref, Metrics: spec.Metrics, Required: spec.Required, Applicability: spec.Applicability})
	}
	body := map[string]any{"dataset": input.Dataset.Ref, "evaluators": bindings, "target": map[string]any{"id": blocked.Target.ID, "kind": blocked.Target.Kind, "version": blocked.Target.Version, "config": blocked.Target.Config, "capabilities": blocked.Target.Capabilities, "timeout_milliseconds": blocked.Target.TimeoutMilliseconds}, "subject": blocked.Subject, "retry": blocked.Retry}
	oldRun := g9Request(t, old, token, "POST", prefix+"/runs", "g10-owned-old-worker", body, 200)
	await(t, func() bool { return strings.Contains(oldWorker.log.text(), "execution_started") })
	e := g10Evidence(x.s.Epoch, legacy)
	g10ArtifactEvidence(t, &e)
	for _, cmd := range []string{"enter-drain", "enter-barrier", "activate-go"} {
		s, e2 := k.Transition(ctx, cmd, e)
		mustG7(t, e2)
		e.Epoch = s.Epoch
	}
	trace := traceBody(asset.NewID(), asset.NewID(), asset.NewID(), "OK")
	call := func(p *g9APIProcess, project, secret string, body []byte, want int) map[string]any {
		req, e2 := http.NewRequest("POST", p.url+"/api/v1/projects/"+project+"/trace-envelopes", bytes.NewReader(body))
		mustG7(t, e2)
		req.Header.Set("X-API-Key", secret)
		req.Header.Set("Content-Type", "application/json")
		res, e2 := http.DefaultClient.Do(req)
		mustG7(t, e2)
		defer res.Body.Close()
		b, e2 := io.ReadAll(res.Body)
		mustG7(t, e2)
		if res.StatusCode != want {
			t.Fatalf("trace status %d want %d %s", res.StatusCode, want, b)
		}
		var out map[string]any
		mustG7(t, json.Unmarshal(b, &out))
		return out
	}
	rejected := call(old, x.s.ProjectID, key, trace, 409) // G5 owner 用缓存旧 epoch 拒绝。
	if rejected["error"].(map[string]any)["code"] != "OWNERSHIP_LOST" {
		t.Fatal("old epoch rejection changed", rejected)
	}
	var before int
	mustG7(t, x.db.pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_results").Scan(&before))
	await(t, func() bool { return strings.Contains(oldWorker.log.text(), "ownership_lost") })
	if strings.Contains(oldWorker.log.text(), "execution_finalized") {
		t.Fatal("stale Worker finalized old owned work")
	}
	var oldWorks int
	mustG7(t, x.db.pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_evaluator_works WHERE run_id=$1", oldRun["id"]).Scan(&oldWorks))
	if oldWorks != 0 {
		t.Fatal("stale worker materialized evaluation works")
	}
	x.s.Epoch = e.Epoch
	x.s5.Epoch = e.Epoch
	x.reviewScope.Epoch = e.Epoch
	current := startG9API(t, x, binary, dev)
	first := call(current, x.s.ProjectID, key, trace, 200)
	dup := call(current, x.s.ProjectID, key, trace, 200)
	if first["id"] != dup["id"] || dup["command_status"] != string(ev.AlreadyApplied) {
		t.Fatal("trace duplicate changed identity")
	}
	call(current, x.s.ProjectID, key, bytes.Replace(trace, []byte(`"duration_ms":0.1`), []byte(`"duration_ms":0.2`), 1), 409)
	call(current, x.s.ProjectID, key, bytes.Repeat([]byte("x"), 16385), 413)
	call(current, asset.NewID(), key, trace, 404)
	call(current, x.s.ProjectID, "", trace, 401)
	login = g9Request(t, current, "", "POST", "/api/v1/auth/dev-login", "", map[string]any{"password": dev.Password}, 200)
	token = login["access_token"].(string)
	g9Request(t, current, token, "GET", prefix+"/overview", "", nil, 200)
	successConfig, _ := asset.Freeze(worker.FixturePlan{Mode: "SUCCESS"})
	body["target"].(map[string]any)["config"] = successConfig
	newRun := g9Request(t, current, token, "POST", prefix+"/runs", "g10-new-epoch-api-run", body, 200)
	newWorker := spawnWorker(t, g9Binary(t, "worker"), x.g3Fixture, "g10-new-epoch-api-worker", "--online-concurrency", "1")
	await(t, func() bool {
		var status string
		e2 := x.db.pool.QueryRow(ctx, "SELECT status FROM evaluation_runs WHERE id=$1", newRun["id"]).Scan(&status)
		return e2 == nil && status == "COMPLETED"
	})
	g9Request(t, current, token, "GET", prefix+"/runs/"+newRun["id"].(string), "", nil, 200)
	newWorker.kill(t)
	base := x.run(t, "g10-after-activation-base", g6Plan{Success: 10})
	candidate := x.run(t, "g10-after-activation-candidate", g6Plan{Success: 9, CriticalFail: true})
	policy := x.policy(t, nil)
	g9Request(t, current, token, "POST", prefix+"/gates", "g10-new-epoch-gate", map[string]any{"Baseline": base, "Candidate": candidate, "Policy": policy}, 200)
	state, e2 := x.k.ReadRunState(ctx, x.s.Scope, base.Runs[0])
	mustG7(t, e2)
	item := x.enqueue(t, rv.SourceRef{Type: "OFFLINE_RESULT", RunID: state.Run.ID, ResultID: state.Results[0].ID}, x.refs[0], rv.TaskSuccess, 1, false)
	g9Request(t, current, token, "GET", prefix+"/reviews/"+item.ID, "", nil, 200)
	// G1 无 epoch 参数：撤销旧 API role 才能阻断目录/credential 等非 G2 写入。
	_, err = x.db.pool.Exec(ctx, "REVOKE INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public FROM "+pgx.Identifier{x.role}.Sanitize())
	mustG7(t, err)
	g9Request(t, old, token, "POST", prefix+"/cases", "g10-stale-catalog", map[string]any{"name": "must reject"}, 500)
	// Worker E2 的实际成功路径在 TestG10ControlledWriterBarrierAndReader；这里 E1 无新事实。
	var after int
	mustG7(t, x.db.pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_results").Scan(&after))
	if after <= before {
		t.Fatal("new epoch Gate fixtures missing results")
	}
	if strings.Contains(oldWorker.log.text(), "execution_finalized") {
		t.Fatal("stale worker wrote outcome")
	}
	t.Log("M01 old API trace epoch rejected, G1 rejected after role revocation; M02 old Worker owned mutation rejected; C13 trace persisted/duplicate/conflict; C14 Product overview; C15 Gate/Review after activation")
}

// TestG10OperatorChild 是测试子进程，永不接受生产连接或 CLI 用户输入。
func TestG10OperatorChild(t *testing.T) {
	if os.Getenv("G10_CHILD") != "CONTROLLED" {
		return
	}
	ctx := context.Background()
	var e cutover.Evidence
	mustG7(t, json.Unmarshal([]byte(os.Getenv("G10_CHILD_EVIDENCE")), &e))
	// 包复用 pool，仅连接父测试刚创建的隔离库。
	d := newChildCutoverPool(t)
	k := postgres.Cutover{Pool: d}
	defer d.Close()
	os.Stdout.WriteString("G10_CHILD_STARTED\n")
	var err error
	if os.Getenv("G10_CHILD_COMMAND") == "preflight" {
		_, err = k.Preflight(ctx, e, true)
	} else {
		_, err = k.Transition(ctx, "enter-barrier", e)
	}
	mustG7(t, err)
	os.Stdout.WriteString("G10_CHILD_COMMITTED\n")
	_, _ = io.Copy(io.Discard, os.Stdin)
}

func newChildCutoverPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	u, err := url.Parse(os.Getenv("G10_CHILD_DATABASE"))
	mustG7(t, err)
	if u.Hostname() != "127.0.0.1" && u.Hostname() != "localhost" || u.Port() != "55432" || !regexp.MustCompile(`^/agentevalops_g1_[0-9a-f]{32}$`).MatchString(u.Path) {
		t.Fatal("unsafe child DB")
	}
	p, err := pgxpool.New(context.Background(), u.String())
	mustG7(t, err)
	return p
}

func g10DatabaseDigests(t *testing.T, d *database) map[string]string {
	t.Helper()
	ctx := context.Background()
	rows, err := d.pool.Query(ctx, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
	mustG7(t, err)
	names := []string{}
	for rows.Next() {
		var name string
		mustG7(t, rows.Scan(&name))
		names = append(names, name)
	}
	mustG7(t, rows.Err())
	rows.Close()
	digests := map[string]string{}
	for _, name := range names {
		var data string
		sql := "SELECT COALESCE(string_agg(value,E'\\n' ORDER BY value COLLATE \"C\"),'') FROM (SELECT row_to_json(t)::text value FROM " + pgx.Identifier{name}.Sanitize() + " t) data"
		mustG7(t, d.pool.QueryRow(ctx, sql).Scan(&data))
		digests[name] = cutover.Digest([]byte(data))
	}
	return digests
}

func TestG10OperatorCrashBoundaries(t *testing.T) {
	d := newDatabase(t)
	d.migrate(t, "head")
	ctx := context.Background()
	k := postgres.Cutover{Pool: d.pool}
	role := g10Role(t, d)
	e := g10Evidence(1, role)
	g10ArtifactEvidence(t, &e)
	_, err := k.Transition(ctx, "enter-drain", e)
	mustG7(t, err)
	exe, err := os.Executable()
	mustG7(t, err)
	child := func(command string) (*exec.Cmd, *processLog, chan struct{}) {
		b, _ := json.Marshal(e)
		cmd := exec.Command(exe, "-test.run=^TestG10OperatorChild$", "-test.v")
		cmd.Env = g9Env(map[string]string{"G10_CHILD": "CONTROLLED", "G10_CHILD_DATABASE": d.url.String(), "G10_CHILD_EVIDENCE": string(b), "G10_CHILD_COMMAND": command})
		log := &processLog{}
		cmd.Stdout = log
		cmd.Stderr = log
		stdin, e2 := cmd.StdinPipe()
		mustG7(t, e2)
		mustG7(t, cmd.Start())
		done := make(chan struct{})
		go func() { _ = cmd.Wait(); close(done) }()
		t.Cleanup(func() {
			stdin.Close()
			select {
			case <-done:
			default:
				_ = cmd.Process.Kill()
				<-done
			}
		})
		await(t, func() bool { return strings.Contains(log.text(), "G10_CHILD_STARTED") })
		return cmd, log, done
	}
	lock, err := d.pool.Begin(ctx)
	mustG7(t, err)
	_, err = lock.Exec(ctx, "LOCK TABLE evaluation_writer_control IN ACCESS EXCLUSIVE MODE")
	mustG7(t, err)
	cmd, _, done := child("preflight")
	mustG7(t, cmd.Process.Kill())
	<-done
	mustG7(t, lock.Rollback(ctx))
	s, err := k.Status(ctx)
	mustG7(t, err)
	if s.Mode != "PYTHON_DRAINING" || s.Epoch != 1 {
		t.Fatal("M05 preflight kill mutated", s)
	}
	cmd, log, done := child("barrier")
	await(t, func() bool { return strings.Contains(log.text(), "G10_CHILD_COMMITTED") })
	mustG7(t, cmd.Process.Kill())
	<-done
	raw, err := g10CLI(t, d, e, "status", false)
	mustG7(t, err)
	mustG7(t, json.Unmarshal(raw, &s))
	if s.Mode != "BARRIER" || s.Epoch != 2 || s.CutoverID != e.CutoverID {
		t.Fatal("M06 restart lost committed barrier", s)
	}
	t.Log("M05 killed while preflight SELECT blocked: no mutation; M06 killed after commit: restarted operator sees exact Barrier/E2/cutover ID")
}

func TestG10BackupRestoreAndRecovery(t *testing.T) {
	x := g8(t)
	ctx := context.Background()
	container := os.Getenv("G10_PG_CONTAINER")
	label, err := exec.Command("docker", "inspect", "--format", `{{index .Config.Labels "agentevalops.task"}}`, container).Output()
	mustG7(t, err)
	if strings.TrimSpace(string(label)) != "g10a" {
		t.Fatal("backup target is not task-owned")
	}
	base := x.run(t, "g10-backup-base", g6Plan{Success: 10})
	candidate := x.run(t, "g10-backup-candidate", g6Plan{Success: 9, CriticalFail: true})
	policy := x.policy(t, nil)
	gate := x.call(t, "judge", "POST", x.url("/gates"), "g10-backup-gate", map[string]any{"Baseline": base, "Candidate": candidate, "Policy": policy}, 200)
	state, err := x.k.ReadRunState(ctx, x.s.Scope, base.Runs[0])
	mustG7(t, err)
	item := x.enqueue(t, rv.SourceRef{Type: "OFFLINE_RESULT", RunID: state.Run.ID, ResultID: state.Results[0].ID}, x.refs[0], rv.TaskSuccess, 1, false)
	_ = item
	x.ingest(t, "OK")
	before := g10DatabaseDigests(t, x.db)
	p := filepath.Join(t.TempDir(), "canonical.dump")
	file, err := os.Create(p)
	mustG7(t, err)
	dump := exec.Command("docker", "exec", container, "pg_dump", "-U", "postgres", "--format=custom", "--no-owner", "--no-acl", x.db.name)
	dump.Stdout = file
	var diagnostics bytes.Buffer
	dump.Stderr = &diagnostics
	err = dump.Run()
	file.Close()
	if err != nil {
		t.Fatalf("isolated pg_dump: %v %s", err, diagnostics.Bytes())
	}
	backup, err := os.ReadFile(p)
	mustG7(t, err)
	if len(backup) == 0 {
		t.Fatal("empty backup")
	}
	restored := newDatabase(t)
	restore := exec.Command("docker", "exec", "-i", container, "pg_restore", "-U", "postgres", "--no-owner", "--no-acl", "--exit-on-error", "-d", restored.name)
	restore.Stdin = bytes.NewReader(backup)
	raw, err := restore.CombinedOutput()
	if err != nil {
		t.Fatalf("isolated pg_restore: %v %s", err, raw)
	}
	after := g10DatabaseDigests(t, restored)
	a, _ := json.Marshal(before)
	b, _ := json.Marshal(after)
	if !bytes.Equal(a, b) {
		t.Fatal("restored schema/facts digests differ")
	}
	// ACL 不由 dump 复活：在新库显式恢复受限 API/Worker grant，角色从不升级。
	_, err = restored.pool.Exec(ctx, postgres.ProductRoleGrants(x.role))
	mustG7(t, err)
	var workerRole string
	mustG7(t, x.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&workerRole))
	_, err = restored.pool.Exec(ctx, "GRANT USAGE ON SCHEMA public TO "+pgx.Identifier{workerRole}.Sanitize()+"; GRANT SELECT ON ALL TABLES IN SCHEMA public TO "+pgx.Identifier{workerRole}.Sanitize()+";GRANT UPDATE(domain) ON evaluation_writer_control TO "+pgx.Identifier{workerRole}.Sanitize())
	mustG7(t, err)
	oldDB := x.db
	x.db = restored
	defer func() { x.db = oldDB }()
	dev := identity.DevAuth{Environment: "test", PrincipalID: x.principals["judge"].ID, OrganizationID: x.s.OrganizationID, Password: strings.Repeat("g10-restore-", 3), SigningKey: []byte(strings.Repeat("g10-restore-sign-", 3)), Lifetime: time.Hour}
	api := startG9API(t, x, g9Binary(t, "api"), dev)
	login := g9Request(t, api, "", "POST", "/api/v1/auth/dev-login", "", map[string]any{"password": dev.Password}, 200)
	token := login["access_token"].(string)
	g9Request(t, api, token, "GET", "/api/v1/projects/"+x.s.ProjectID+"/runs/"+state.Run.ID, "", nil, 200)
	grants, err := oldDB.pool.Query(ctx, `SELECT table_name,privilege_type FROM information_schema.role_table_grants WHERE grantee=$1 AND table_schema='public'`, workerRole)
	mustG7(t, err)
	statements := []string{}
	for grants.Next() {
		var table, priv string
		mustG7(t, grants.Scan(&table, &priv))
		statements = append(statements, "GRANT "+priv+" ON "+pgx.Identifier{table}.Sanitize()+" TO "+pgx.Identifier{workerRole}.Sanitize())
	}
	mustG7(t, grants.Err())
	grants.Close()
	grants, err = oldDB.pool.Query(ctx, `SELECT table_name,column_name,privilege_type FROM information_schema.role_column_grants WHERE grantee=$1 AND table_schema='public'`, workerRole)
	mustG7(t, err)
	for grants.Next() {
		var table, column, priv string
		mustG7(t, grants.Scan(&table, &column, &priv))
		statements = append(statements, "GRANT "+priv+"("+pgx.Identifier{column}.Sanitize()+") ON "+pgx.Identifier{table}.Sanitize()+" TO "+pgx.Identifier{workerRole}.Sanitize())
	}
	mustG7(t, grants.Err())
	grants.Close()
	for _, sql := range statements {
		_, err = restored.pool.Exec(ctx, sql)
		mustG7(t, err)
	}
	boot := spawnWorker(t, g9Binary(t, "worker"), x.g3Fixture, "g10-restored-worker", "--online-concurrency", "1")
	boot.kill(t)
	_, err = restored.pool.Exec(ctx, "UPDATE evaluation_writer_control SET mode='PYTHON_ACTIVE',active_writer='PYTHON',writer_epoch=writer_epoch+1")
	if err == nil {
		t.Fatal("restore enabled Python writer")
	}
	// 停掉 API 再重启，仍读取相同 canonical fact；不向 Python 回退。
	mustG7(t, api.cmd.Process.Kill())
	<-api.done
	api = startG9API(t, x, g9Binary(t, "api"), dev)
	g9Request(t, api, token, "GET", "/api/v1/projects/"+x.s.ProjectID+"/gates/"+gate["gate_id"].(string), "", nil, 200)
	t.Logf("pg_dump/pg_restore all %d public-table digests equal; dump sha256=%s; release API read/kill/restart and Worker process boot PASS", len(before), cutover.Digest(backup))
}
