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
	"bytes"
	"context"
	"encoding/json"
	"github.com/jackc/pgx/v5"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

// 完整 Python 后端 -> SERVICE 只读规则投影 -> Go API/Worker -> 受限 PG。
// 不把 RULE 结果声称为真实模型 Candidate。
func TestBasebandWorkerHTTPPostgres(t *testing.T) {
	joint := os.Getenv("BASEBAND_METRIC_VERSION") == "v3"
	numeric := os.Getenv("BASEBAND_METRIC_VERSION") == "v2" || joint
	count := 3
	bundleName := "baseband_development.json"
	wire := "baseband-quality-request.v1"
	if joint {
		count = 4
		bundleName = "oracle_chain_development.json"
		wire = "baseband-quality-request.v2"
	}
	historyPath := os.Getenv("BASEBAND_REPLAY_EVIDENCE")
	if joint {
		historyPath = os.Getenv("BASEBAND_SOURCE_REPLAY")
	}
	var history []byte
	if numeric {
		if historyPath == "" || os.Getenv("BASEBAND_REAL_AGENT") == "1" {
			t.Fatal("v2 requires sealed replay; new model execution forbidden")
		}
		var err error
		history, err = os.ReadFile(historyPath)
		if err != nil {
			t.Fatal(err)
		}
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
	command := exec.Command(python, "-u", filepath.Join(root, "scripts", "baseband_rehearsal_server.py"))
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
		BaseURL string `json:"base_url"`
		Token   string `json:"service_token"`
		Human   string `json:"human_token"`
		Sources []struct {
			Key              string     `json:"case_key"`
			IncidentID       string     `json:"incident_id"`
			InstanceID       string     `json:"instance_id"`
			ExecutionRequest string     `json:"execution_request_id"`
			ExecutionID      string     `json:"execution_id"`
			Input            asset.JSON `json:"input"`
			InvestigationID  string     `json:"investigation_id"`
			RunID            string     `json:"run_id"`
		} `json:"sources"`
	}
	started := false
	for scanner.Scan() {
		line := scanner.Text()
		const prefix = "BASEBAND_PRIVATE:"
		if len(line) > len(prefix) && line[:len(prefix)] == prefix {
			must(json.Unmarshal([]byte(line[len(prefix):]), &live))
			started = true
			break
		}
	}
	if !started || live.Token == "" || len(live.Sources) != count {
		t.Fatal("full Python backend startup failed")
	}
	go io.Copy(io.Discard, stdout)
	t.Setenv("BASEBAND_TEST_TOKEN", live.Token)
	selectors := map[string]aq.BasebandSelector{}
	bundleRaw, e := os.ReadFile(filepath.Join("..", "..", "testdata", bundleName))
	must(e)
	var public struct {
		Cases []struct {
			Key   string     `json:"case_key"`
			Input asset.JSON `json:"input"`
		} `json:"cases"`
	}
	must(json.Unmarshal(bundleRaw, &public))
	for i := range live.Sources {
		if live.Sources[i].Key == "" {
			for _, sample := range public.Cases {
				if sample.Input.Digest() == live.Sources[i].Input.Digest() {
					live.Sources[i].Key = sample.Key
				}
			}
		}
	}
	for _, source := range live.Sources {
		selectors[source.Key] = aq.BasebandSelector{IncidentID: source.IncidentID, Kind: "RULE"}
	}
	// SERVICE 不能越过 HUMAN 写入边界。
	denied, _ := http.NewRequest("GET", live.BaseURL+"/api/ci/incidents", nil)
	denied.Header.Set("Authorization", "Bearer "+live.Token)
	response, e := http.DefaultClient.Do(denied)
	must(e)
	response.Body.Close()
	if response.StatusCode != 403 {
		t.Fatal("SERVICE enumeration accepted")
	}
	wrongBody, _ := json.Marshal(map[string]any{"protocol": wire, "project_id": asset.NewID(), "run_id": "r", "attempt_id": "a", "request_id": "q", "case_id": "c", "frozen_input_digest": live.Sources[0].Input.Digest(), "input": live.Sources[0].Input, "selector": selectors[live.Sources[0].Key]})
	denied, _ = http.NewRequest("POST", live.BaseURL+"/api/ci/quality/baseband/evaluate", bytes.NewReader(wrongBody))
	denied.Header.Set("Authorization", "Bearer "+live.Token)
	denied.Header.Set("Content-Type", "application/json")
	response, e = http.DefaultClient.Do(denied)
	must(e)
	response.Body.Close()
	if response.StatusCode != 403 {
		t.Fatal("client project expanded source scope")
	}
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
	api, closeAPI, e := bootstrap.API(ctx, httpapi.DefaultConfig(), true, false, slog.New(slog.NewTextHandler(io.Discard, nil)))
	must(e)
	defer closeAPI()
	httpServer := httptest.NewServer(api.Handler())
	defer httpServer.Close()
	selectorPath, outputPath := filepath.Join(t.TempDir(), "selectors.json"), filepath.Join(t.TempDir(), "publication.json")
	raw, _ = json.Marshal(selectors)
	must(os.WriteFile(selectorPath, raw, 0600))
	publisher := exec.Command(python, filepath.Join(root, "..", "AgentEvalOps", "scripts", "prepare-baseband-evaluation.py"), "--bundle", filepath.Join("..", "..", "testdata", bundleName), "--selectors", selectorPath, "--project", scope.ProjectID, "--base-url", httpServer.URL, "--token-env", "BASEBAND_PRODUCT_TOKEN", "--output", outputPath)
	publisher.Env = append(os.Environ(), "PYTHONIOENCODING=utf-8")
	if numeric {
		version := "v2"
		if joint {
			version = "v3"
		}
		publisher.Args = append(publisher.Args, "--version", version)
	}
	if output, err := publisher.CombinedOutput(); err != nil {
		t.Fatalf("catalog publication failed: %v %s", err, output)
	}
	var publication struct {
		Suite     asset.Ref                      `json:"suite"`
		Policy    asset.Ref                      `json:"policy"`
		Selectors map[string]aq.BasebandSelector `json:"selectors"`
		Run       struct {
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
		if len(state.Results) == count {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("Worker results incomplete")
		}
		time.Sleep(50 * time.Millisecond)
	}
	for _, result := range state.Results {
		if result.Value.Verdict != "PASS" {
			t.Fatalf("RULE score=%s reason=%s", result.Value.Verdict, result.Value.Reason)
		}
		if numeric && (result.Value.Score == nil || *result.Value.Score != 1) {
			t.Fatal("RULE numeric score must equal 1")
		}
	}
	evidence := map[string]any{"transport": "REAL_PYTHON_HTTP", "storage": "POSTGRESQL_RESTRICTED_ROLE", "source_kind": "RULE", "candidate": "NOT_EXECUTED", "project_id": scope.ProjectID, "run_id": publication.Run.ID, "results": state.Results, "publication": json.RawMessage(raw)}
	if os.Getenv("BASEBAND_REAL_AGENT") == "1" || numeric {
		postAt := func(url, token string, body any) []byte {
			t.Helper()
			payload, _ := json.Marshal(body)
			req, e := http.NewRequest("POST", url, bytes.NewReader(payload))
			must(e)
			req.Header.Set("Authorization", "Bearer "+token)
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("Idempotency-Key", asset.NewID())
			response, e := http.DefaultClient.Do(req)
			must(e)
			defer response.Body.Close()
			result, e := io.ReadAll(response.Body)
			must(e)
			if response.StatusCode != 200 && response.StatusCode != 201 && response.StatusCode != 202 {
				t.Fatalf("business POST status=%d %s", response.StatusCode, result)
			}
			return result
		}
		candidateSelectors := map[string]aq.BasebandSelector{}
		investigations := []json.RawMessage{}
		for caseID, original := range publication.Selectors {
			if numeric {
				for _, source := range live.Sources {
					if source.IncidentID == original.IncidentID {
						run, investigation := source.RunID, source.InvestigationID
						if run == "" || investigation == "" {
							t.Fatal("sealed Agent selector missing")
						}
						candidateSelectors[caseID] = aq.BasebandSelector{IncidentID: source.IncidentID, RunID: &run, InvestigationID: &investigation, Kind: "AGENT"}
					}
				}
				continue
			}
			linksRaw := postAt(live.BaseURL+"/api/ci/incidents/"+original.IncidentID+"/investigations", live.Human, map[string]any{"request_id": asset.NewID()})
			var links struct {
				RunID           string `json:"run_id"`
				InvestigationID string `json:"investigation_id"`
				ReportURL       string `json:"report_url"`
			}
			must(json.Unmarshal(linksRaw, &links))
			end := time.Now().Add(210 * time.Second)
			for {
				req, e := http.NewRequest("GET", live.BaseURL+links.ReportURL, nil)
				must(e)
				req.Header.Set("Authorization", "Bearer "+live.Human)
				response, e := http.DefaultClient.Do(req)
				must(e)
				result, e := io.ReadAll(response.Body)
				response.Body.Close()
				must(e)
				var report struct {
					Status string  `json:"investigation_status"`
					Error  *string `json:"safe_error_code"`
				}
				must(json.Unmarshal(result, &report))
				if report.Status == "COMPLETED" {
					investigations = append(investigations, json.RawMessage(result))
					break
				}
				if report.Status == "FAILED" || report.Status == "UNCONFIRMED" || time.Now().After(end) {
					code := ""
					if report.Error != nil {
						code = *report.Error
					}
					t.Fatalf("Agent investigation status=%s error=%s run=%s", report.Status, code, links.RunID)
				}
				time.Sleep(300 * time.Millisecond)
			}
			candidateSelectors[caseID] = aq.BasebandSelector{IncidentID: original.IncidentID, InvestigationID: &links.InvestigationID, RunID: &links.RunID, Kind: "AGENT"}
			t.Logf("real Agent FINAL: run=%s", links.RunID)
		}
		candidateRaw := postAt(httpServer.URL+"/api/v1/projects/"+scope.ProjectID+"/baseband/runs", key, map[string]any{"suite": publication.Suite, "selectors": candidateSelectors})
		var candidate struct {
			ID string `json:"id"`
		}
		must(json.Unmarshal(candidateRaw, &candidate))
		end := time.Now().Add(30 * time.Second)
		var candidateState ev.RunState
		for {
			candidateState, e = k.ReadRunState(ctx, scope, candidate.ID)
			must(e)
			if len(candidateState.Results) == count && candidateState.Run.Status.Terminal() {
				break
			}
			if time.Now().After(end) {
				t.Fatal("Candidate Worker results incomplete")
			}
			time.Sleep(50 * time.Millisecond)
		}
		// Baseline 也必须已经终态，不能拿中间 Work 状态比较。
		state, e = k.ReadRunState(ctx, scope, publication.Run.ID)
		must(e)
		gateRaw := postAt(httpServer.URL+"/api/v1/projects/"+scope.ProjectID+"/gates", key, map[string]any{"baseline": map[string]any{"kind": "RUN_SET", "runs": []string{publication.Run.ID}}, "candidate": map[string]any{"kind": "RUN_SET", "runs": []string{candidate.ID}}, "policy": publication.Policy})
		var gate map[string]any
		must(json.Unmarshal(gateRaw, &gate))
		// 在质量断言之前保存真实失败 Result/Gate，避免失败证据只留退出码。
		evidence["candidate"] = "EXECUTED"
		evidence["candidate_run_id"] = candidate.ID
		evidence["candidate_results"] = candidateState.Results
		evidence["comparison"] = gate
		if path := os.Getenv("BASEBAND_EVIDENCE_PATH"); path != "" {
			saved, _ := json.MarshalIndent(evidence, "", "  ")
			must(os.WriteFile(path, saved, 0600))
		}
		if numeric {
			for _, result := range candidateState.Results {
				if result.Value.Verdict != "PASS" || result.Value.Score == nil || *result.Value.Score != 1 {
					t.Fatal("Candidate numeric score must equal 1")
				}
			}
			left, _ := asset.Freeze(state.Run.Snapshot.Input)
			right, _ := asset.Freeze(candidateState.Run.Snapshot.Input)
			if left.Digest() != right.Digest() {
				t.Fatal("Baseline and Candidate frozen inputs/evaluators differ")
			}
			if gate["comparability"] != "COMPARABLE" || gate["decision"] != "PASS" {
				t.Fatalf("numeric comparison must PASS: %s", gateRaw)
			}
			after, e := os.ReadFile(historyPath)
			must(e)
			if !bytes.Equal(history, after) {
				t.Fatal("v1 history changed")
			}
			var old struct {
				Comparison struct {
					Decision string `json:"decision"`
					ID       string `json:"gate_id"`
				} `json:"comparison"`
			}
			must(json.Unmarshal(history, &old))
			if !joint && old.Comparison.Decision != "BLOCKED" {
				t.Fatal("old v1 BLOCKED missing")
			}
			if !joint {
				evidence["v1_comparison_preserved"] = old.Comparison
			}
			evidence["model_execution"] = "NOT_EXECUTED_SEALED_REPLAY"
		}
		evidence["candidate"] = "EXECUTED"
		evidence["candidate_run_id"] = candidate.ID
		evidence["candidate_results"] = candidateState.Results
		evidence["investigations"] = investigations
		evidence["comparison"] = gate
		t.Logf("real Candidate Worker/PG: run=%s results=%d comparison=%v", candidate.ID, len(candidateState.Results), gate["decision"])
		for _, result := range candidateState.Results {
			t.Logf("Candidate result=%s verdict=%s", result.ID, result.Value.Verdict)
		}
	}
	if path := os.Getenv("BASEBAND_EVIDENCE_PATH"); path != "" {
		raw, _ = json.MarshalIndent(evidence, "", "  ")
		must(os.WriteFile(path, raw, 0600))
	}
	t.Logf("RULE real HTTP/PG: run=%s results=%d/%d PASS", publication.Run.ID, len(state.Results), count)
}
