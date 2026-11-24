//go:build integration

package postgres_test

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/bootstrap"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/httpapi"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"github.com/jackc/pgx/v5"
)

func TestCIRepairHTTPWorkerPostgres(t *testing.T) {
	python, root := os.Getenv("CI_REPAIR_TEST_PYTHON"), os.Getenv("CI_REPAIR_AGENTCORE")
	if python == "" || root == "" || os.Getenv("LOCAL_AGENT_TEST_DATABASE_URL") == "" {
		t.Fatal("explicit AgentCore interpreter, workspace and isolated replay test database required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	must := func(e error) {
		t.Helper()
		if e != nil {
			t.Fatal(e)
		}
	}
	d := newDatabase(t)
	d.migrate(t, "head")
	g2Writer(t, d)
	scope := seedProject(t, d.pool, asset.NewID())
	workerPool := g2Pool(t, d)
	pepper := []byte(asset.NewID() + asset.NewID())
	uid, password := asset.NewID(), asset.NewID()
	caps := []string{}
	for _, c := range identity.All {
		caps = append(caps, string(c))
	}
	_, err := d.pool.Exec(ctx, `INSERT INTO users(id,external_id,email,display_name,created_at) VALUES($1::uuid,$1::text,'ci-repair@controlled.invalid','CI Repair',clock_timestamp())`, uid)
	must(err)
	_, err = d.pool.Exec(ctx, `INSERT INTO memberships(id,user_id,org_id,role,created_at) VALUES($1,$2,$3,'MEMBER',clock_timestamp())`, asset.NewID(), uid, scope.OrganizationID)
	must(err)
	_, err = d.pool.Exec(ctx, `INSERT INTO product_project_memberships(project_id,org_id,principal_id,capabilities) VALUES($1,$2,$3,$4)`, scope.ProjectID, scope.OrganizationID, uid, caps)
	must(err)
	dev := identity.DevAuth{Environment: "test", PrincipalID: uid, OrganizationID: scope.OrganizationID, Password: password, SigningKey: pepper, Lifetime: 15 * time.Minute}
	key, err := dev.Login(password)
	must(err)
	ident := postgres.ProductIdentity{Pool: d.pool, Pepper: pepper}
	_, readKey, err := ident.CreateCredential(ctx, identity.Access{Scope: scope, Principal: identity.Principal{
		ID: uid, OrganizationID: scope.OrganizationID, Capabilities: []identity.Capability{identity.Read, identity.ManageAPIKey}}},
		asset.NewID(), "Workspace Gate readback", []identity.Capability{identity.Read}, nil)
	must(err)
	mux := http.NewServeMux()
	apiHTTP := httptest.NewServer(mux)
	defer apiHTTP.Close()
	command := exec.CommandContext(ctx, python, "-u", filepath.Join(root, "tests", "ci_repair_rehearsal_server.py"))
	command.Dir = root
	command.Env = append(os.Environ(), "CI_REPAIR_REHEARSAL_PROJECT="+scope.ProjectID,
		"CI_REPAIR_REHEARSAL_GO_ORIGIN="+apiHTTP.URL, "CI_REPAIR_REHEARSAL_GO_READ_KEY="+readKey)
	stdin, err := command.StdinPipe()
	must(err)
	stdout, err := command.StdoutPipe()
	must(err)
	var logs processLog
	command.Stderr = &logs
	must(command.Start())
	defer func() {
		_ = stdin.Close()
		if err := command.Wait(); err != nil {
			t.Errorf("Python rehearsal exit: %v", err)
		}
	}()
	var live struct {
		URL             string          `json:"url"`
		Service         string          `json:"service"`
		OtherService    string          `json:"other_service"`
		WrongSpace      string          `json:"wrong_space_project"`
		Human           string          `json:"human"`
		Workflow        string          `json:"workflow"`
		Receipt         json.RawMessage `json:"receipt"`
		Digest          string          `json:"evidence_digest"`
		Failure         string          `json:"failure_digest"`
		Disagreement    string          `json:"disagreement_digest"`
		ChangedApproval string          `json:"changed_approval_digest"`
	}
	scanner := bufio.NewScanner(stdout)
	for scanner.Scan() {
		if private, ok := strings.CutPrefix(scanner.Text(), "CI_REPAIR_PRIVATE:"); ok {
			if json.Unmarshal([]byte(private), &live) != nil {
				t.Fatal("invalid private rehearsal record")
			}
			break
		}
	}
	if live.Service == "" || !asset.ValidID(live.Workflow) {
		t.Fatalf("Python rehearsal startup failed: %s", logs.text())
	}
	go io.Copy(io.Discard, stdout)

	config, err := json.Marshal(provider.DefaultLocalAgentConfig(live.URL, "CI_REPAIR_TEST_SERVICE_TOKEN"))
	must(err)
	t.Setenv("BASEBAND_ENABLED", "1")
	t.Setenv("BASEBAND_TARGET_CONFIG", string(config))
	t.Setenv("CI_REPAIR_TEST_SERVICE_TOKEN", live.Service)
	t.Setenv("CI_REPAIR_TEST_PRODUCT_TOKEN", key)
	t.Setenv("PRODUCT_API_PEPPER", string(pepper))
	t.Setenv("PRODUCT_DEV_PRINCIPAL_ID", uid)
	t.Setenv("PRODUCT_DEV_ORGANIZATION_ID", scope.OrganizationID)
	t.Setenv("PRODUCT_DEV_PASSWORD", password)
	t.Setenv("PRODUCT_DEV_SIGNING_KEY", string(pepper))
	t.Setenv("APP_ENV", "test")
	apiRole := "ci_repair_api_" + asset.NewID()[:8]
	quoted := pgx.Identifier{apiRole}.Sanitize()
	_, err = d.admin.Exec(ctx, "CREATE ROLE "+quoted+" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS")
	must(err)
	_, err = d.pool.Exec(ctx, postgres.ProductRoleGrants(apiRole))
	must(err)
	t.Cleanup(func() {
		_, e := d.pool.Exec(context.Background(), "DROP OWNED BY "+quoted)
		must(e)
		_, e = d.admin.Exec(context.Background(), "DROP ROLE "+quoted)
		must(e)
	})
	apiURL := *d.url
	query := apiURL.Query()
	query.Set("options", "-c role="+apiRole)
	apiURL.RawQuery = query.Encode()
	t.Setenv("DATABASE_URL", apiURL.String())
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	api, closeAPI, err := bootstrap.API(ctx, httpapi.DefaultConfig(), true, false, log)
	must(err)
	defer closeAPI()
	mux.Handle("/", api.Handler())

	client := &http.Client{Timeout: 10 * time.Second}
	call := func(method, url, bearer string, body any, want int) ([]byte, http.Header) {
		t.Helper()
		var payload io.Reader
		if body != nil {
			raw, e := json.Marshal(body)
			must(e)
			payload = bytes.NewReader(raw)
		}
		req, e := http.NewRequestWithContext(ctx, method, url, payload)
		must(e)
		req.Header.Set("Authorization", "Bearer "+bearer)
		if body != nil {
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("Idempotency-Key", asset.NewID())
		}
		response, e := client.Do(req)
		must(e)
		defer response.Body.Close()
		raw, e := io.ReadAll(response.Body)
		must(e)
		if (want != 0 && response.StatusCode != want) || (want == 0 && response.StatusCode/100 != 2) {
			t.Fatalf("%s %s: HTTP %d %s", method, url, response.StatusCode, raw)
		}
		return raw, response.Header
	}
	evidence := func(project, workflow, digest string) string {
		return live.URL + "/api/ci/quality/repair-evidence/" + workflow + "/" + digest + "?project_id=" + project
	}
	realURL := evidence(scope.ProjectID, live.Workflow, live.Digest)
	raw, headers := call("GET", realURL, live.Service, nil, 200)
	if aq.RepairSHA256(string(raw)) != live.Digest || headers.Get("X-Content-SHA256") != live.Digest ||
		bytes.Contains(raw, []byte("root_path")) || bytes.Contains(raw, []byte("api_key")) ||
		bytes.Contains(raw, []byte("root_cause_hypothesis")) {
		t.Fatal("export identity or allowlist invalid")
	}
	call("GET", realURL, live.Human, nil, 403)
	call("GET", realURL, live.OtherService, nil, 403)
	call("GET", evidence(live.WrongSpace, live.Workflow, live.Digest), live.Service, nil, 404)
	call("GET", evidence(asset.NewID(), live.Workflow, live.Digest), live.Service, nil, 403)
	call("GET", evidence(scope.ProjectID, asset.NewID(), live.Digest), live.Service, nil, 404)
	call("GET", evidence(scope.ProjectID, live.Workflow, strings.Repeat("0", 64)), live.Service, nil, 404)

	workDir := t.TempDir()
	output := filepath.Join(workDir, "publication.json")
	publisher := exec.CommandContext(ctx, python, filepath.Join(root, "..", "AgentEvalOps", "scripts", "prepare-ci-repair-evaluation.py"),
		"--project", scope.ProjectID, "--base-url", apiHTTP.URL, "--token-env", "CI_REPAIR_TEST_PRODUCT_TOKEN", "--output", output)
	if body, e := publisher.CombinedOutput(); e != nil {
		t.Fatalf("catalog publication: %v %s", e, body)
	}
	var publication struct {
		Dataset asset.Ref `json:"dataset"`
		Suite   asset.Ref `json:"suite"`
		Policy  asset.Ref `json:"policy"`
	}
	published, err := os.ReadFile(output)
	must(err)
	must(json.Unmarshal(published, &publication))

	var workerRole string
	must(workerPool.QueryRow(ctx, "SELECT current_user").Scan(&workerRole))
	workerURL := *d.url
	query = workerURL.Query()
	query.Set("options", "-c role="+workerRole)
	workerURL.RawQuery = query.Encode()
	cfg := worker.DefaultConfig()
	cfg.ExecutionConcurrency, cfg.EvaluatorConcurrency, cfg.OnlineConcurrency = 2, 2, 0
	cfg.PollInterval, cfg.MaxBackoff = 15*time.Millisecond, 50*time.Millisecond
	runtime, closeWorker, err := bootstrap.Worker(ctx, workerURL.String(), cfg, false, log)
	must(err)
	defer closeWorker()
	runCtx, stop := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- runtime.Run(runCtx) }()
	defer func() { stop(); must(<-done) }()
	base := apiHTTP.URL + "/api/v1/projects/" + scope.ProjectID
	k := postgres.Evaluation{Pool: workerPool}
	results := map[string]any{}
	for _, scenario := range []struct{ name, digest, decision string }{
		{"production", live.Digest, "PASS"}, {"controlled_wrong_value", live.Failure, "FAIL"},
		{"controlled_oracle_disagreement", live.Disagreement, "BLOCKED"}, {"missing", strings.Repeat("0", 64), "BLOCKED"},
		{"changed_approval", live.ChangedApproval, "BLOCKED"},
	} {
		runIDs := [2]string{}
		for i, role := range []string{"BASELINE", "CANDIDATE"} {
			raw, _ := call("POST", base+"/ci-repair/runs", key, map[string]any{"suite": publication.Suite, "role": role,
				"workflow_run_id": live.Workflow, "evidence_digest": scenario.digest}, 0)
			var created struct {
				ID string `json:"id"`
			}
			must(json.Unmarshal(raw, &created))
			if !asset.ValidID(created.ID) {
				t.Fatalf("run create invalid: %s", raw)
			}
			runIDs[i] = created.ID
		}
		attemptIDs, workIDs, resultIDs := []string{}, []string{}, []string{}
		scores := []map[string]any{}
		for _, id := range runIDs {
			var state ev.RunState
			deadline := time.Now().Add(45 * time.Second)
			for {
				state, err = k.ReadRunState(ctx, scope, id)
				must(err)
				if state.Run.Status.Terminal() {
					break
				}
				if time.Now().After(deadline) {
					t.Fatalf("real Worker timeout %s %s (%d attempts, %d works, %d results)", id, state.Run.Status, len(state.Attempts), len(state.Works), len(state.Results))
				}
				time.Sleep(50 * time.Millisecond)
			}
			if scenario.name == "missing" && (string(state.Run.Status) != "OUTCOME_UNKNOWN" || len(state.Works) != 0 || len(state.Results) != 0) {
				t.Fatalf("missing evidence advanced to evaluation: %s (%d/%d)", state.Run.Status, len(state.Works), len(state.Results))
			}
			if len(state.Attempts) != 2 || (scenario.name != "missing" && (len(state.Works) != 4 || len(state.Results) != 4)) {
				t.Fatalf("incomplete persistent evaluation: %s %s %d/%d/%d", id, state.Run.Status, len(state.Attempts), len(state.Works), len(state.Results))
			}
			for _, item := range state.Attempts {
				attemptIDs = append(attemptIDs, item.ID)
			}
			for _, item := range state.Works {
				workIDs = append(workIDs, item.ID)
			}
			for _, item := range state.Results {
				resultIDs = append(resultIDs, item.ID)
				scores = append(scores, map[string]any{"run_id": id, "case_id": item.CaseID, "verdict": item.Value.Verdict,
					"score": item.Value.Score, "reason": item.Value.Reason, "provenance": item.Value.Provenance})
			}
		}
		gate, _ := call("POST", base+"/ci-repair/gates", key, map[string]any{
			"baseline": map[string]any{"kind": "RUN_SET", "runs": runIDs[:1]}, "candidate": map[string]any{"kind": "RUN_SET", "runs": runIDs[1:]}, "policy": publication.Policy}, 0)
		var decision struct {
			ID       string `json:"gate_id"`
			Decision string `json:"decision"`
		}
		must(json.Unmarshal(gate, &decision))
		if decision.Decision != scenario.decision {
			t.Fatalf("%s gate=%s want=%s: %s scores=%v", scenario.name, decision.Decision, scenario.decision, gate, scores)
		}
		read, _ := call("GET", base+"/ci-repair/gates/"+decision.ID, key, nil, 200)
		cases, _ := call("GET", base+"/gates/"+decision.ID+"/cases", key, nil, 200)
		regress, _ := call("GET", base+"/gates/"+decision.ID+"/regressions", key, nil, 200)
		workspaceURL := live.URL + "/api/v1/workflow-runs/" + live.Workflow + "/repair-gate/" + decision.ID
		if scenario.name == "production" {
			workflow, _ := call("GET", live.URL+"/api/v1/workflow-runs/"+live.Workflow, live.Human, nil, 200)
			if !bytes.Contains(workflow, []byte(live.Digest)) {
				t.Fatal("Workspace has no replay receipt")
			}
			projection, _ := call("GET", workspaceURL, live.Human, nil, 200)
			if !bytes.Contains(projection, []byte(decision.ID)) || !bytes.Contains(projection, []byte("PASS")) {
				t.Fatal("Workspace lost verified Gate")
			}
		} else {
			call("GET", workspaceURL, live.Human, nil, 404)
		}
		results[scenario.name] = map[string]any{"gate": json.RawMessage(gate), "gate_read": json.RawMessage(read),
			"regression": json.RawMessage(regress), "case_comparison": json.RawMessage(cases), "scores": scores,
			"runs": runIDs, "attempts": attemptIDs, "works": workIDs, "results": resultIDs}
		t.Logf("%s: %s Gate %s baseline %s candidate %s", scenario.name, decision.Decision, decision.ID, runIDs[0], runIDs[1])
	}
	rawAgain, _ := call("GET", realURL, live.Service, nil, 200)
	if !bytes.Equal(raw, rawAgain) {
		t.Fatal("GET changed sealed evidence")
	}
	if destination := os.Getenv("CI_REPAIR_TEST_EVIDENCE_LOG"); destination != "" {
		body, e := json.MarshalIndent(map[string]any{"workflow_run_id": live.Workflow, "project_id": scope.ProjectID,
			"database": d.name, "receipt": live.Receipt, "source_sha256": live.Digest, "catalog": json.RawMessage(published), "evaluations": results}, "", "  ")
		must(e)
		must(os.WriteFile(destination, body, 0600))
	}
	t.Log(fmt.Sprintf("Workflow %s raw evidence SHA-256 %s project %s", live.Workflow, live.Digest, scope.ProjectID))
}
