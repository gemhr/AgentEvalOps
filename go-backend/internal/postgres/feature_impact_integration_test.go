//go:build integration

package postgres_test

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/bootstrap"
	ev "agentevalops/go-backend/internal/evaluation"
	fi "agentevalops/go-backend/internal/featureimpact"
	"agentevalops/go-backend/internal/httpapi"
	"agentevalops/go-backend/internal/identity"
	"bytes"
	"github.com/jackc/pgx/v5"
	"net/http"
	"net/http/httptest"

	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
)

// 真实 Python 业务服务 + HTTP Target + Go Worker + 受限 PG role；无 fixture Target/Provider。
func TestFeatureImpactWorkerHTTPPostgres(t *testing.T) {
	ctx := context.Background()
	python, root := os.Getenv("FEATURE_IMPACT_PYTHON"), os.Getenv("FEATURE_IMPACT_AGENTCORE")
	if python == "" || root == "" {
		t.Fatal("NOT_EXECUTED: explicit AgentCore Python/workspace required")
	}
	command := exec.Command(python, "-u", filepath.Join(root, "scripts", "feature_impact_rehearsal_server.py"))
	command.Dir = root
	stdin, e := command.StdinPipe()
	if e != nil {
		t.Fatal(e)
	}
	stdout, e := command.StdoutPipe()
	if e != nil {
		t.Fatal(e)
	}
	var stderr processLog
	command.Stderr = &stderr
	if command.Start() != nil {
		t.Fatal("Python business process start failed")
	}
	t.Cleanup(func() { _ = stdin.Close(); _ = command.Wait() })
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 65536), 1<<20)
	if !scanner.Scan() {
		t.Fatal("Python business process did not start", stderr.text())
	}
	var live struct {
		BaseURL string   `json:"base_url"`
		Token   string   `json:"service_token"`
		Human   string   `json:"human_token"`
		TaskID  string   `json:"task_id"`
		Input   fi.Input `json:"input"`
	}
	if json.Unmarshal(scanner.Bytes(), &live) != nil || live.Token == "" {
		t.Fatal("private test handshake invalid")
	}
	// 永不打印 handshake、token 或凭据。
	t.Setenv("FEATURE_IMPACT_TEST_TOKEN", live.Token)
	d := newDatabase(t)
	d.migrate(t, "head")
	_ = g2Writer(t, d)
	scope := seedProject(t, d.pool, asset.NewID())
	pool := g2Pool(t, d)
	must := func(e error) {
		t.Helper()
		if e != nil {
			t.Fatal(e)
		}
	}
	input, e := asset.Freeze(live.Input)
	must(e)
	var snap struct {
		Input  string `json:"input_digest"`
		Corpus string `json:"corpus_digest"`
	}
	must(json.Unmarshal(live.Input.Snapshot.Bytes(), &snap))
	// 独立参考来自已批准 v0.3 开发合同，先于候选输出冻结；不从执行结果/规则输出生成。
	truth := fi.Truth{Version: "feature-impact-truth.v1", InputDigest: snap.Input, CorpusDigest: snap.Corpus, Units: []fi.Unit{
		{Feature: "H1", Mechanism: "SHARED_DELAYED_REUSE", Conditions: []string{"DELAYED_CONSUME", "PRODUCER_COMPLETE", "SUBSEQUENT_WRITE"}, Class: "MUST_TEST", Support: []string{"history:H1:1", "resources:1"}, Existing: "PARTIAL"},
		{Feature: "H2", Mechanism: "SHARED_DELAYED_REUSE", Conditions: []string{"DELAYED_CONSUME", "PRODUCER_COMPLETE", "SUBSEQUENT_WRITE"}, Class: "MUST_TEST", Known: true, Support: []string{"history:H2:1", "resources:1"}, Existing: "PARTIAL"},
		{Feature: "H3", Mechanism: "COPY_ISOLATION", Conditions: []string{"COPY_AT_DELIVERY", "LATER_PUBLIC_WRITE"}, Class: "REASONABLE_CHECK", Support: []string{"history:H3:1"}, Counter: []string{"history:H3:1"}, Existing: "PARTIAL"},
		{Feature: "H4", Mechanism: "RESOURCE_ISOLATION", Conditions: []string{"INDEPENDENT_RESOURCE"}, Class: "UNSUPPORTED", Support: []string{"history:H4:1"}, Counter: []string{"history:H4:1"}, Existing: "PARTIAL"},
		{Feature: "H5", Mechanism: "OWNERSHIP_UNKNOWN", Conditions: []string{"OWNERSHIP_OR_TIMING_MISSING"}, Class: "INSUFFICIENT_EVIDENCE", Support: []string{"history:H5:1"}, Existing: "UNKNOWN"},
	}}
	gt, e := asset.Freeze(truth)
	must(e)

	transport := provider.DefaultLocalAgentConfig(live.BaseURL, "FEATURE_IMPACT_TEST_TOKEN")
	targetConfig, _ := json.Marshal(transport)
	t.Setenv("FEATURE_IMPACT_ENABLED", "1")
	t.Setenv("FEATURE_IMPACT_TARGET_CONFIG", string(targetConfig))
	apiRole := "feature_api_" + asset.NewID()[0:8]
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
	aq := apiURL.Query()
	aq.Set("options", "-c role="+apiRole)
	apiURL.RawQuery = aq.Encode()

	pepper := []byte(asset.NewID() + asset.NewID())
	uid := asset.NewID()
	password := asset.NewID()
	capabilities := []string{}
	for _, cap := range identity.All {
		capabilities = append(capabilities, string(cap))
	}
	_, e = d.pool.Exec(ctx, `INSERT INTO users(id,external_id,email,display_name,created_at) VALUES($1::uuid,$1::text,'feature@controlled.invalid','Feature impact',clock_timestamp())`, uid)
	must(e)
	_, e = d.pool.Exec(ctx, `INSERT INTO memberships(id,user_id,org_id,role,created_at) VALUES($1,$2,$3,'MEMBER',clock_timestamp())`, asset.NewID(), uid, scope.OrganizationID)
	must(e)
	_, e = d.pool.Exec(ctx, `INSERT INTO product_project_memberships(project_id,org_id,principal_id,capabilities) VALUES($1,$2,$3,$4)`, scope.ProjectID, scope.OrganizationID, uid, capabilities)
	must(e)
	dev := identity.DevAuth{Environment: "test", PrincipalID: uid, OrganizationID: scope.OrganizationID, Password: password, SigningKey: pepper, Lifetime: 15 * time.Minute}
	key, e := dev.Login(password)
	must(e)
	t.Setenv("DATABASE_URL", apiURL.String())
	t.Setenv("PRODUCT_API_PEPPER", string(pepper))
	t.Setenv("FEATURE_PRODUCT_TOKEN", key)
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
	bundle, _ := json.Marshal(map[string]any{"purpose": "DEVELOPMENT_EXPOSED", "reference_review_ref": "approved-business-contract-v0.3", "input": live.Input, "coverage_truth": truth})
	bundlePath, outputPath := filepath.Join(t.TempDir(), "bundle.json"), filepath.Join(t.TempDir(), "publication.json")
	must(os.WriteFile(bundlePath, bundle, 0600))
	publisher := exec.Command(python, filepath.Join(root, "..", "AgentEvalOps", "scripts", "prepare-feature-impact-evaluation.py"), "--bundle", bundlePath, "--project", scope.ProjectID, "--base-url", httpServer.URL, "--token-env", "FEATURE_PRODUCT_TOKEN", "--output", outputPath, "--run")
	publisher.Env = append(os.Environ(), "PYTHONIOENCODING=utf-8")
	if output, err := publisher.CombinedOutput(); err != nil {
		t.Fatalf("real catalog publication failed: %v %s", err, output)
	}
	var publication struct {
		Suite  asset.Ref `json:"suite"`
		Policy asset.Ref `json:"policy"`
		Run    struct {
			ID string `json:"id"`
		} `json:"run"`
	}
	published, e := os.ReadFile(outputPath)
	must(e)
	must(json.Unmarshal(published, &publication))
	created := ev.Reply{ID: publication.Run.ID}
	if created.ID == "" {
		t.Fatal("real API did not create Run")
	}
	k := postgres.Evaluation{Pool: pool}
	k.Capabilities, e = k.LoadEvaluatorCapabilities(ctx, agentquality.FeatureImpactSupported)
	must(e)
	cfg := worker.DefaultConfig()
	cfg.ExecutionConcurrency = 1
	cfg.EvaluatorConcurrency = 1
	cfg.OnlineConcurrency = 0
	cfg.PollInterval = 10 * time.Millisecond
	cfg.MaxBackoff = 40 * time.Millisecond
	t.Setenv("FEATURE_IMPACT_ENABLED", "1")

	role := ""
	must(pool.QueryRow(ctx, "SELECT current_user").Scan(&role))
	workerURL := *d.url
	query := workerURL.Query()
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
		state, e = k.ReadRunState(ctx, scope, created.ID)
		must(e)
		if len(state.Results) == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("Worker did not persist result: %+v", state.Attempts)
		}
		time.Sleep(50 * time.Millisecond)
	}
	result := state.Results[0]
	var actual fi.Output
	must(result.Value.Artifact.Body.Decode(&actual))
	judgment := fi.Score(live.Input, truth, actual)
	if judgment.MustFound != 2 || judgment.IncrementalFound != 1 || judgment.Reasonable != 1 || judgment.Uncertainty != 1 || !judgment.EvidenceAuthentic || judgment.Verdict != "INCONCLUSIVE" || result.Value.Score != nil || result.Value.Reason != "SEMANTIC_SUPPORT_REVIEW_PENDING" {
		t.Fatalf("independent score: %+v, result %+v", judgment, result.Value)
	}
	// 同一真实报告变体只验证 evaluator，不宣称为 Agent 输出。
	fullClaim := actual
	fullClaim.Report.Findings = append([]fi.Finding{}, actual.Report.Findings...)
	fullClaim.Report.Findings[0].Existing = "FULL"
	fullClaim.Report.Findings[0].Tests = nil
	if scored := fi.Score(live.Input, truth, fullClaim); scored.MustActionable != 1 || scored.Verdict != "FAIL" {
		t.Fatal("self-reported full coverage accepted")
	}
	changed := actual
	changed.Report.Findings = append([]fi.Finding{}, actual.Report.Findings...)
	changed.Report.Findings[2].Class = "MUST_TEST"
	if fi.Score(live.Input, truth, changed).Invalid != 1 {
		t.Fatal("copy corruption assertion must not pass")
	}
	changed = actual
	changed.Report.Findings = append(append([]fi.Finding{}, actual.Report.Findings...), actual.Report.Findings[0])
	if fi.Score(live.Input, truth, changed).Duplicates != 1 {
		t.Fatal("duplicate missing")
	}
	changed = actual
	changed.Report.Findings = append([]fi.Finding{}, actual.Report.Findings...)
	changed.Report.Findings[0].Support = append([]fi.Reference{}, actual.Report.Findings[0].Support...)
	changed.Report.Findings[0].Support[0].Digest = "invalid"
	if fi.Score(live.Input, truth, changed).EvidenceAuthentic {
		t.Fatal("fabricated digest accepted")
	}
	var count, providerCalls int
	must(d.pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_results WHERE run_id=$1", created.ID).Scan(&count))
	must(d.pool.QueryRow(ctx, "SELECT COALESCE(sum(provider_call_count),0) FROM evaluation_evaluator_works WHERE run_id=$1", created.ID).Scan(&providerCalls))
	if count != 1 || providerCalls != 0 {
		t.Fatal("unexpected persisted rows/provider calls")
	}
	again, e := k.ReadRunState(ctx, scope, created.ID)
	must(e)
	a, _ := json.Marshal(result.Value)
	b, _ := json.Marshal(again.Results[0].Value)
	if string(a) != string(b) {
		t.Fatal("result changed on read")
	}

	gateBody, _ := json.Marshal(map[string]any{"baseline": map[string]any{"kind": "RUN_SET", "runs": []string{created.ID}}, "candidate": map[string]any{"kind": "RUN_SET", "runs": []string{created.ID}}, "policy": publication.Policy})
	gateRequest, e := http.NewRequest("POST", httpServer.URL+"/api/v1/projects/"+scope.ProjectID+"/gates", bytes.NewReader(gateBody))
	must(e)
	gateRequest.Header.Set("Authorization", "Bearer "+key)
	gateRequest.Header.Set("Content-Type", "application/json")
	gateRequest.Header.Set("Idempotency-Key", asset.NewID())
	gateResponse, e := http.DefaultClient.Do(gateRequest)
	must(e)
	defer gateResponse.Body.Close()
	gateRaw, e := io.ReadAll(gateResponse.Body)
	must(e)
	if gateResponse.StatusCode != 200 && gateResponse.StatusCode != 201 {
		t.Fatalf("real Gate HTTP status=%d %s", gateResponse.StatusCode, gateRaw)
	}
	var gate map[string]any
	must(json.Unmarshal(gateRaw, &gate))
	if gate["decision"] == "PASS" {
		t.Fatal("pending semantic review must block Gate")
	}
	evidence := map[string]any{"transport": "REAL_PYTHON_HTTP", "storage": "POSTGRESQL_RESTRICTED_ROLE", "project_id": scope.ProjectID, "run_id": created.ID, "result_id": result.ID, "input_digest": input.Digest(), "ground_truth_digest": gt.Digest(), "judgment": judgment, "result_value": result.Value, "provider_calls": providerCalls, "development_exposed": true, "gate": gate, "publication": publication}
	if path := os.Getenv("FEATURE_IMPACT_EVIDENCE_PATH"); path != "" {
		raw, _ := json.MarshalIndent(evidence, "", "  ")
		must(os.WriteFile(path, raw, 0600))
	}
	t.Logf("real Worker/Target/PG: run=%s result=%s must=2/2 incremental=1/1 reasonable=1 uncertainty=1 semantic=PENDING provider_calls=0", created.ID, result.ID)
}
