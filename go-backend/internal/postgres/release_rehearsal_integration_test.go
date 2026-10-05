//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/identity"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"
	"time"
)

type g9APIProcess struct {
	cmd  *exec.Cmd
	log  *processLog
	done chan struct{}
	url  string
}

func g9Binary(t *testing.T, name string) string {
	t.Helper()
	suffix := ""
	if runtime.GOOS == "windows" {
		suffix = ".exe"
	}
	if dir := os.Getenv("G9_RELEASE_DIR"); dir != "" {
		path := filepath.Join(dir, name+suffix)
		if _, e := os.Stat(path); e != nil {
			t.Fatal(e)
		}
		return path
	}
	path := filepath.Join(t.TempDir(), name+suffix)
	args := []string{"build", "-o", path, "../../cmd/" + name}
	if os.Getenv("G3_RACE_WORKER") == "1" {
		args = []string{"build", "-race", "-o", path, "../../cmd/" + name}
	}
	if raw, e := exec.Command("go", args...).CombinedOutput(); e != nil {
		t.Fatalf("build %s: %v %s", name, e, raw)
	}
	return path
}

func g9Env(overrides map[string]string) []string {
	out := []string{}
	for _, v := range os.Environ() {
		k, _, _ := strings.Cut(v, "=")
		if _, ok := overrides[k]; !ok {
			out = append(out, v)
		}
	}
	for k, v := range overrides {
		out = append(out, k+"="+v)
	}
	return out
}
func startG9API(t *testing.T, x *g8Fixture, binary string, dev identity.DevAuth) *g9APIProcess {
	t.Helper()
	u := *x.db.url
	q := u.Query()
	q.Set("options", "-c role="+x.role)
	u.RawQuery = q.Encode()
	p := &g9APIProcess{cmd: exec.Command(binary, "--listen", "127.0.0.1:0", "--controlled-dev-auth", "--fixture-only", "--requests-per-minute", "100000"), log: &processLog{}, done: make(chan struct{})}
	p.cmd.Env = g9Env(map[string]string{"DATABASE_URL": u.String(), "APP_ENV": "test", "PRODUCT_API_PEPPER": string(x.api.Identity.Pepper), "PRODUCT_DEV_PRINCIPAL_ID": dev.PrincipalID, "PRODUCT_DEV_ORGANIZATION_ID": dev.OrganizationID, "PRODUCT_DEV_PASSWORD": dev.Password, "PRODUCT_DEV_SIGNING_KEY": string(dev.SigningKey), "DB_POOL_MAX_CONNS": "8"})
	p.cmd.Stdout = p.log
	p.cmd.Stderr = p.log
	mustG7(t, p.cmd.Start())
	go func() { _ = p.cmd.Wait(); close(p.done) }()
	t.Cleanup(func() {
		select {
		case <-p.done:
		default:
			_ = p.cmd.Process.Kill()
			<-p.done
		}
		if strings.Contains(p.log.text(), "DATA RACE") {
			t.Error("API process data race")
		}
	})
	address := regexp.MustCompile(`"address":"([^"]+)"`)
	await(t, func() bool {
		select {
		case <-p.done:
			t.Fatalf("API failed bootstrap: %s", p.log.text())
		default:
		}
		m := address.FindStringSubmatch(p.log.text())
		if len(m) == 2 {
			p.url = "http://" + m[1]
			return true
		}
		return false
	})
	return p
}
func g9Request(t *testing.T, p *g9APIProcess, token, method, path, key string, body any, status int) map[string]any {
	t.Helper()
	var reader io.Reader
	if body != nil {
		raw, e := json.Marshal(body)
		mustG7(t, e)
		reader = bytes.NewReader(raw)
	}
	req, e := http.NewRequest(method, p.url+path, reader)
	mustG7(t, e)
	req.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if key != "" {
		req.Header.Set("Idempotency-Key", key)
	}
	res, e := http.DefaultClient.Do(req)
	mustG7(t, e)
	defer res.Body.Close()
	raw, e := io.ReadAll(res.Body)
	mustG7(t, e)
	if res.StatusCode != status {
		t.Fatalf("release HTTP %s %s status=%d want=%d body=%s", method, path, res.StatusCode, status, raw)
	}
	var out map[string]any
	mustG7(t, json.Unmarshal(raw, &out))
	return out
}

func TestG9ReleaseRehearsal(t *testing.T) {
	x := g8(t)
	ctx := context.Background()
	apiBinary, workerBinary, gateBinary := g9Binary(t, "api"), g9Binary(t, "worker"), g9Binary(t, "evalgate")
	identities := map[string]any{}
	for name, path := range map[string]string{"api": apiBinary, "worker": workerBinary, "evalgate": gateBinary} {
		raw, e := exec.Command(path, "--version").Output()
		mustG7(t, e)
		var v map[string]any
		mustG7(t, json.Unmarshal(raw, &v))
		if v["schema_head"] != "c12a00800001" || v["binary"] != name {
			t.Fatal("release identity", v)
		}
		identities[name] = v
	}
	dev := identity.DevAuth{Environment: "test", PrincipalID: x.principals["judge"].ID, OrganizationID: x.s.OrganizationID, Password: strings.Repeat("g9-controlled-", 3), SigningKey: []byte(strings.Repeat("g9-signing-", 4)), Lifetime: time.Hour}
	a, b := startG9API(t, x, apiBinary, dev), startG9API(t, x, apiBinary, dev)
	login := g9Request(t, a, "", "POST", "/api/v1/auth/dev-login", "", map[string]any{"password": dev.Password}, 200)
	token := login["access_token"].(string)
	baseline := x.run(t, "release-base", g6Plan{Success: 10})
	candidate := x.run(t, "release-candidate", g6Plan{Success: 9, CriticalFail: true})
	policy := x.policy(t, nil)
	w1, w2 := spawnWorker(t, workerBinary, x.g3Fixture, "g9-release-1", "--online-concurrency", "1"), spawnWorker(t, workerBinary, x.g3Fixture, "g9-release-2", "--online-concurrency", "1")
	prefix := "/api/v1/projects/" + x.s.ProjectID
	c := x.runtimeCommand
	input := c.Snapshot.Input()
	bindings := []catalog.EvaluatorBinding{}
	for _, spec := range input.Evaluators {
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: spec.Identity.Ref, Metrics: spec.Metrics, Required: spec.Required, Applicability: spec.Applicability})
	}
	body := map[string]any{"dataset": input.Dataset.Ref, "evaluators": bindings, "target": map[string]any{"id": c.Target.ID, "kind": c.Target.Kind, "version": c.Target.Version, "config": c.Target.Config, "capabilities": c.Target.Capabilities, "timeout_milliseconds": c.Target.TimeoutMilliseconds}, "subject": c.Subject, "retry": c.Retry}
	created := g9Request(t, a, token, "POST", prefix+"/runs", "g9-release-run", body, 200)
	runID := created["id"].(string)
	await(t, func() bool {
		var status string
		e := x.db.pool.QueryRow(ctx, "SELECT status FROM evaluation_runs WHERE id=$1", runID).Scan(&status)
		return e == nil && status == "COMPLETED"
	})
	gate := g9Request(t, a, token, "POST", prefix+"/gates", "g9-release-gate", map[string]any{"Baseline": baseline, "Candidate": candidate, "Policy": policy}, 200)
	cli := exec.Command(gateBinary, "--read", "--project", x.s.ProjectID, "--gate", gate["gate_id"].(string))
	u := *x.db.url
	q := u.Query()
	q.Set("options", "-c role="+x.role)
	u.RawQuery = q.Encode()
	cli.Env = g9Env(map[string]string{"DATABASE_URL": u.String(), "EVALGATE_PRINCIPAL": "G9_CONTROLLED_RELEASE"})
	raw, e := cli.Output()
	exit, ok := e.(*exec.ExitError)
	if !ok || exit.ExitCode() != 1 || !bytes.Contains(raw, []byte(`"decision":"FAIL"`)) {
		t.Fatalf("evalgate release outcome %v %s", e, raw)
	}
	ref, _ := x.rule(t, nil)
	obs, _ := x.ingest(t, "OK")
	await(t, func() bool {
		var count int
		e := x.db.pool.QueryRow(ctx, `SELECT count(*) FROM evaluation_online_works WHERE project_id=$1 AND rule_id=$2 AND observation_id=$3`, x.s.ProjectID, ref.EntityID, obs).Scan(&count)
		return e == nil && count == 1
	})
	work := workID(t, x.g5Fixture, ref, obs)
	await(t, func() bool {
		_, r, e := x.online.GetOnlineEvaluationState(ctx, x.s.Scope, work)
		return e == nil && r != nil
	})
	for _, kind := range []string{"quality", "trends", "gates", "online", "human"} {
		g9Request(t, b, token, "GET", prefix+"/analytics/"+kind, "", nil, 200)
	}
	outage := "NOT_EXECUTED unless explicit task-owned container"
	if container := os.Getenv("G9_OUTAGE_CONTAINER"); container != "" {
		var priorOutage []byte
		mustG7(t, x.db.pool.QueryRow(ctx, "SELECT convert_to(row_to_json(r)::text,'UTF8') FROM evaluation_runs r WHERE id=$1", runID).Scan(&priorOutage))
		label, e := exec.Command("docker", "inspect", "--format", `{{index .Config.Labels "task"}}`, container).Output()
		mustG7(t, e)
		if strings.TrimSpace(string(label)) != "agentevalops-g9" {
			t.Fatal("outage target is not this task's isolated container")
		}
		mustG7(t, exec.Command("docker", "pause", container).Run())
		func() {
			defer func() { mustG7(t, exec.Command("docker", "unpause", container).Run()) }()
			start := time.Now()
			response := g9Request(t, a, token, "GET", prefix+"/analytics/quality", "", nil, 503)
			if time.Since(start) > 8*time.Second || response["error"].(map[string]any)["code"] != "UNAVAILABLE" {
				t.Fatal("DB outage did not fail bounded")
			}
		}()
		g9Request(t, a, token, "GET", prefix+"/analytics/quality", "", nil, 200)
		await(t, func() bool {
			return strings.Contains(w1.log.text(), "scan_error") || strings.Contains(w2.log.text(), "scan_error")
		})
		var resumed []byte
		mustG7(t, x.db.pool.QueryRow(ctx, "SELECT convert_to(row_to_json(r)::text,'UTF8') FROM evaluation_runs r WHERE id=$1", runID).Scan(&resumed))
		if !bytes.Equal(priorOutage, resumed) {
			t.Fatal("database outage changed sealed Run")
		}
		outage = "isolated PG pause → bounded API 503 → resume/recovery; Worker DB errors did not alter sealed Run"
	}
	var before, after []byte
	mustG7(t, x.db.pool.QueryRow(ctx, "SELECT convert_to(row_to_json(r)::text,'UTF8') FROM evaluation_runs r WHERE id=$1", runID).Scan(&before))
	mustG7(t, a.cmd.Process.Kill())
	<-a.done
	a = startG9API(t, x, apiBinary, dev)
	g9Request(t, a, token, "GET", prefix+"/runs/"+runID, "", nil, 200)
	mustG7(t, w1.cmd.Process.Kill())
	<-w1.done
	w1 = spawnWorker(t, workerBinary, x.g3Fixture, "g9-release-restarted", "--online-concurrency", "1")
	mustG7(t, x.db.pool.QueryRow(ctx, "SELECT convert_to(row_to_json(r)::text,'UTF8') FROM evaluation_runs r WHERE id=$1", runID).Scan(&after))
	if !bytes.Equal(before, after) {
		t.Fatal("release restart changed canonical Run")
	}
	createdKey := g9Request(t, b, token, "POST", prefix+"/api-keys", "g9-cross-revoke", map[string]any{"name": "controlled revoke", "capabilities": []string{"READ"}}, 200)
	keyID := createdKey["credential"].(map[string]any)["id"].(string)
	secret := createdKey["secret"].(string)
	req, e := http.NewRequest("GET", a.url+prefix+"/overview", nil)
	mustG7(t, e)
	req.Header.Set("X-API-Key", secret)
	res, e := http.DefaultClient.Do(req)
	mustG7(t, e)
	res.Body.Close()
	if res.StatusCode != 200 {
		t.Fatal("new key denied")
	}
	g9Request(t, b, token, "POST", prefix+"/api-keys/"+keyID+"/revoke", "", map[string]any{}, 200)
	res, e = http.DefaultClient.Do(req)
	mustG7(t, e)
	res.Body.Close()
	if res.StatusCode != 401 {
		t.Fatal("cross-process revoke stale")
	}
	g9Evidence(t, "release-rehearsal.json", map[string]any{"binaries": identities, "database": "fresh isolated Alembic full chain", "api_processes": 2, "worker_processes": 2, "run_created_via": "release API", "run_status": "COMPLETED", "evalgate_exit": decision.ExitCode(decision.Fail), "online_result": "completed by release Worker", "api_restart": "fact preserved", "worker_restart": "fact preserved", "cross_process_revoke": "401", "writer_truth": "unchanged", "production_validation": "NOT_PRODUCTION_VALIDATED", "read_only_upgrade": "G8 schema head retained; G9 adds projections only", "db_outage": outage})
	_ = w2
}
