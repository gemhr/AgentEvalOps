//go:build integration

package postgres_test

import (
	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/bootstrap"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/httpapi"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"bufio"
	"context"
	"encoding/json"
	"github.com/jackc/pgx/v5"
	"io"
	"log/slog"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

// 封存 HUMAN 工作流的真实 SERVICE HTTP → Go API/Worker → 受限 PG；不调用模型。
func TestCoverageExecutionHTTPPostgres(t *testing.T) {
	if os.Getenv("COVERAGE_REPLAY_EVIDENCE") == "" {
		t.Fatal("explicit sealed workflow required")
	}
	ctx := context.Background()
	must := func(e error) {
		t.Helper()
		if e != nil {
			t.Fatal(e)
		}
	}
	python, root := os.Getenv("BASEBAND_PYTHON"), os.Getenv("BASEBAND_AGENTCORE")
	if python == "" || root == "" {
		t.Fatal("explicit AgentCore interpreter/workspace required")
	}
	d := newDatabase(t)
	d.migrate(t, "head")
	_ = g2Writer(t, d)
	scope := seedProject(t, d.pool, asset.NewID())
	pool := g2Pool(t, d)
	command := exec.Command(python, "-u", filepath.Join(root, "scripts", "coverage_execution_server.py"))
	command.Dir = root
	command.Env = append(os.Environ(), "BASEBAND_PROJECT_ID="+scope.ProjectID)
	stdin, e := command.StdinPipe()
	must(e)
	stdout, e := command.StdoutPipe()
	must(e)
	var logs processLog
	command.Stderr = &logs
	must(command.Start())
	t.Cleanup(func() { _ = stdin.Close(); _ = command.Wait() })
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 65536), 1<<20)

	var live struct {
		BaseURL  string              `json:"base_url"`
		Token    string              `json:"service_token"`
		Human    string              `json:"human_token"`
		Input    asset.JSON          `json:"input"`
		Selector aq.CoverageSelector `json:"selector"`
	}
	started := false
	for scanner.Scan() {
		line := scanner.Text()
		const prefix = "COVERAGE_PRIVATE:"
		if len(line) > len(prefix) && line[:len(prefix)] == prefix {
			must(json.Unmarshal([]byte(line[len(prefix):]), &live))
			started = true
			break
		}
	}
	if !started || live.Token == "" || live.Selector.ExecutionID == "" {
		t.Fatal("Python backend startup failed")
	}
	go io.Copy(io.Discard, stdout)
	t.Setenv("BASEBAND_TEST_TOKEN", live.Token)
	config := provider.DefaultLocalAgentConfig(live.BaseURL, "BASEBAND_TEST_TOKEN")
	raw, _ := json.Marshal(config)
	t.Setenv("BASEBAND_ENABLED", "1")
	t.Setenv("BASEBAND_TARGET_CONFIG", string(raw))
	apiRole := "baseband_api_" + asset.NewID()[:8]
	quoted := pgx.Identifier{apiRole}.Sanitize()
	_, e = d.admin.Exec(ctx, "CREATE ROLE "+quoted+" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS")
	must(e)
	_, e = d.pool.Exec(ctx, postgres.ProductRoleGrants(apiRole))
	must(e)
	t.Cleanup(func() {
		_, e := d.pool.Exec(ctx, "DROP OWNED BY "+quoted)
		must(e)
		_, e = d.admin.Exec(ctx, "DROP ROLE "+quoted)
		must(e)
	})
	apiURL := *d.url
	query := apiURL.Query()
	query.Set("options", "-c role="+apiRole)
	apiURL.RawQuery = query.Encode()
	pepper := []byte(asset.NewID() + asset.NewID())
	uid, password := asset.NewID(), asset.NewID()
	caps := []string{}
	for _, cap := range identity.All {
		caps = append(caps, string(cap))
	}
	_, e = d.pool.Exec(ctx, `INSERT INTO users(id,external_id,email,display_name,created_at) VALUES($1::uuid,$1::text,'baseband@controlled.invalid','Baseband',clock_timestamp())`, uid)
	must(e)
	_, e = d.pool.Exec(ctx, `INSERT INTO memberships(id,user_id,org_id,role,created_at) VALUES($1,$2,$3,'MEMBER',clock_timestamp())`, asset.NewID(), uid, scope.OrganizationID)
	must(e)
	_, e = d.pool.Exec(ctx, `INSERT INTO product_project_memberships(project_id,org_id,principal_id,capabilities) VALUES($1,$2,$3,$4)`, scope.ProjectID, scope.OrganizationID, uid, caps)
	must(e)
	dev := identity.DevAuth{Environment: "test", PrincipalID: uid, OrganizationID: scope.OrganizationID, Password: password, SigningKey: pepper, Lifetime: 15 * time.Minute}
	key, e := dev.Login(password)
	must(e)
	t.Setenv("DATABASE_URL", apiURL.String())
	t.Setenv("PRODUCT_API_PEPPER", string(pepper))
	t.Setenv("BASEBAND_PRODUCT_TOKEN", key)
	t.Setenv("APP_ENV", "test")
	t.Setenv("PRODUCT_DEV_PRINCIPAL_ID", uid)
	t.Setenv("PRODUCT_DEV_ORGANIZATION_ID", scope.OrganizationID)
	t.Setenv("PRODUCT_DEV_PASSWORD", password)
	t.Setenv("PRODUCT_DEV_SIGNING_KEY", string(pepper))
	apiConfig := httpapi.DefaultConfig()
	apiConfig.AllowedOrigins = []string{"http://localhost:3000"}
	api, closeAPI, e := bootstrap.API(ctx, apiConfig, true, false, slog.New(slog.NewTextHandler(io.Discard, nil)))
	must(e)
	defer closeAPI()
	httpServer := httptest.NewServer(api.Handler())
	defer httpServer.Close()

	selectorPath, outputPath := filepath.Join(t.TempDir(), "selectors.json"), filepath.Join(t.TempDir(), "publication.json")
	raw, _ = json.Marshal(map[string]aq.CoverageSelector{"sampling-range": live.Selector})
	must(os.WriteFile(selectorPath, raw, 0600))
	publisher := exec.Command(python, filepath.Join(root, "..", "AgentEvalOps", "scripts", "prepare-coverage-execution-evaluation.py"), "--bundle", filepath.Join("..", "..", "testdata", "coverage_execution_development.json"), "--selectors", selectorPath, "--project", scope.ProjectID, "--base-url", httpServer.URL, "--token-env", "BASEBAND_PRODUCT_TOKEN", "--output", outputPath)
	publisher.Env = append(os.Environ(), "PYTHONIOENCODING=utf-8")
	if output, err := publisher.CombinedOutput(); err != nil {
		t.Fatalf("catalog publication failed: %v %s", err, output)
	}
	var publication struct {
		Run struct {
			ID string `json:"id"`
		} `json:"run"`
	}
	raw, e = os.ReadFile(outputPath)
	must(e)
	must(json.Unmarshal(raw, &publication))
	k := postgres.Evaluation{Pool: pool}
	k.Capabilities, e = k.LoadEvaluatorCapabilities(ctx, aq.BasebandSupported)
	must(e)
	cfg := worker.DefaultConfig()
	cfg.ExecutionConcurrency = 1
	cfg.EvaluatorConcurrency = 1
	cfg.OnlineConcurrency = 0
	cfg.PollInterval = 10 * time.Millisecond
	cfg.MaxBackoff = 40 * time.Millisecond
	var role string
	must(pool.QueryRow(ctx, "SELECT current_user").Scan(&role))
	workerURL := *d.url
	query = workerURL.Query()
	query.Set("options", "-c role="+role)
	workerURL.RawQuery = query.Encode()
	runtime, closeWorker, e := bootstrap.Worker(ctx, workerURL.String(), cfg, false, slog.New(slog.NewTextHandler(io.Discard, nil)))
	must(e)
	defer closeWorker()
	runctx, cancel := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- runtime.Run(runctx) }()
	defer func() { cancel(); must(<-done) }()

	deadline := time.Now().Add(30 * time.Second)
	var state ev.RunState
	for {
		state, e = k.ReadRunState(ctx, scope, publication.Run.ID)
		must(e)
		if len(state.Results) == 1 && state.Run.Status.Terminal() {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("workflow evaluation incomplete")
		}
		time.Sleep(50 * time.Millisecond)
	}
	evidence := map[string]any{"transport": "REAL_PYTHON_HTTP", "storage": "POSTGRESQL_RESTRICTED_ROLE", "publication": json.RawMessage(raw), "selector": live.Selector, "run_id": publication.Run.ID, "results": state.Results, "model_calls": 0, "incremental_agent_value": "NOT_VALIDATED"}
	if path := os.Getenv("COVERAGE_EVIDENCE_PATH"); path != "" {
		body, _ := json.MarshalIndent(evidence, "", "  ")
		must(os.WriteFile(path, body, 0600))
	}
	result := state.Results[0]
	if result.Value.Verdict != "PASS" || result.Value.Score == nil || *result.Value.Score != 1 {
		t.Fatalf("workflow score=%v verdict=%s reason=%s", result.Value.Score, result.Value.Verdict, result.Value.Reason)
	}
	var provenance map[string]any
	must(result.Value.Provenance.Decode(&provenance))
	checks, ok := provenance["checks"].(map[string]any)
	if !ok || len(checks) != 5 {
		t.Fatal("five checks missing")
	}
	for name, value := range checks {
		if value != true {
			t.Fatal(name)
		}
	}
	t.Logf("sealed workflow run=%s result=%s score=1 checks=5/5", publication.Run.ID, result.ID)
	if os.Getenv("COVERAGE_PAGE_TEST") == "1" {
		page := exec.Command("corepack.cmd", "yarn", "playwright", "test", "--config", "playwright.coverage.config.ts", "--reporter=line")
		page.Dir = filepath.Join(root, "..", "AgentEvalOps", "frontend")
		page.Env = append(os.Environ(), "NEXT_PUBLIC_GO_PRODUCT_API_URL="+httpServer.URL, "COVERAGE_GO_DEV_PASSWORD="+password, "COVERAGE_GO_PROJECT="+scope.ProjectID, "COVERAGE_GO_ORG="+scope.OrganizationID, "COVERAGE_GO_RESULT="+result.ID)
		output, err := page.CombinedOutput()
		if path := os.Getenv("COVERAGE_PAGE_LOG"); path != "" {
			must(os.WriteFile(path, output, 0600))
		}
		if err != nil {
			t.Fatalf("targeted page validation failed: %v %s", err, output)
		}
		t.Log("real coverage page PASS")
	}
}
