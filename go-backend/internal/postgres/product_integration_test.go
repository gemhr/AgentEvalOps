//go:build integration

package postgres_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/httpapi"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/metric"
	ob "agentevalops/go-backend/internal/observation"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type apiBearer struct{ users map[string]identity.Principal }

func (a apiBearer) AuthenticateBearer(_ context.Context, token string) (identity.Principal, error) {
	p, ok := a.users[token]
	if !ok {
		return p, identity.ErrUnauthenticated
	}
	return p, nil
}

type g8Fixture struct {
	*g7Fixture
	pool       *pgxpool.Pool
	role       string
	api        *httpapi.Server
	tokens     map[string]string
	principals map[string]identity.Principal
	server     *httptest.Server
	logs       *bytes.Buffer
}

func g8(t *testing.T) *g8Fixture {
	return g8WithConfig(t, httpapi.DefaultConfig())
}
func g8WithConfig(t *testing.T, config httpapi.Config) *g8Fixture {
	t.Helper()
	f := g7(t)
	ctx := context.Background()
	role := "g8_api_" + strings.TrimPrefix(f.db.name, "agentevalops_g1_")
	quoted := pgx.Identifier{role}.Sanitize()
	_, err := f.db.admin.Exec(ctx, "CREATE ROLE "+quoted+" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS")
	mustG7(t, err)
	_, err = f.db.pool.Exec(ctx, postgres.ProductRoleGrants(role))
	mustG7(t, err)
	cfg, err := pgxpool.ParseConfig(f.db.url.String())
	mustG7(t, err)
	cfg.MaxConns = 16
	cfg.AfterConnect = func(ctx context.Context, c *pgx.Conn) error { _, e := c.Exec(ctx, "SET ROLE "+quoted); return e }
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	mustG7(t, err)
	t.Cleanup(func() {
		pool.Close()
		_, e := f.db.pool.Exec(context.Background(), "DROP OWNED BY "+quoted)
		mustG7(t, e)
		_, e = f.db.admin.Exec(context.Background(), "DROP ROLE "+quoted)
		mustG7(t, e)
	})
	x := &g8Fixture{g7Fixture: f, pool: pool, role: role, tokens: map[string]string{}, principals: map[string]identity.Principal{}, logs: &bytes.Buffer{}}
	bearer := apiBearer{users: map[string]identity.Principal{}}
	for _, name := range []string{"A", "B", "judge", "reader"} {
		id := asset.NewID()
		caps := identity.All
		if name == "reader" {
			caps = []identity.Capability{identity.Read}
		}
		if name == "A" || name == "B" {
			caps = []identity.Capability{identity.Read, identity.Review}
		}
		_, err = f.db.pool.Exec(ctx, `INSERT INTO users(id,external_id,email,display_name,created_at) VALUES($1::uuid,$1::text,$2,$2,clock_timestamp())`, id, name+"@controlled.invalid")
		mustG7(t, err)
		_, err = f.db.pool.Exec(ctx, `INSERT INTO memberships(id,user_id,org_id,role,created_at) VALUES($1,$2,$3,'MEMBER',clock_timestamp())`, asset.NewID(), id, f.s.OrganizationID)
		mustG7(t, err)
		values := []string{}
		for _, c := range caps {
			values = append(values, string(c))
		}
		_, err = f.db.pool.Exec(ctx, `INSERT INTO product_project_memberships(project_id,org_id,principal_id,capabilities) VALUES($1,$2,$3,$4)`, f.s.ProjectID, f.s.OrganizationID, id, values)
		mustG7(t, err)
		p := identity.Principal{ID: id, Type: "HUMAN", OrganizationID: f.s.OrganizationID, AuthMethod: "CONTROLLED_TEST"}
		token, _ := identity.NewKey()
		x.tokens[name] = token
		x.principals[name] = p
		bearer.users[token] = p
	}
	ident := postgres.ProductIdentity{Pool: pool, Pepper: []byte(strings.Repeat("controlled-pepper-", 3))}
	epoch, err := ident.VerifyAPI(ctx)
	mustG7(t, err)
	k := f.k
	k.Pool = pool
	api, err := httpapi.New(httpapi.Server{Pool: pool, Identity: ident, Bearer: bearer, Config: config, Epoch: epoch, Kernel: k, Log: slog.New(slog.NewJSONHandler(x.logs, nil))})
	mustG7(t, err)
	x.api = api
	x.server = httptest.NewServer(api.Handler())
	t.Cleanup(x.server.Close)
	return x
}
func (x *g8Fixture) url(path string) string {
	return x.server.URL + "/api/v1/projects/" + x.s.ProjectID + path
}
func (x *g8Fixture) call(t *testing.T, who, method, path, key string, body any, status int) map[string]any {
	t.Helper()
	raw, _ := json.Marshal(body)
	if body == nil {
		raw = nil
	}
	req, e := http.NewRequest(method, path, bytes.NewReader(raw))
	mustG7(t, e)
	if who != "" {
		req.Header.Set("Authorization", "Bearer "+x.tokens[who])
	}
	if key != "" {
		req.Header.Set("Idempotency-Key", key)
	}
	res, e := http.DefaultClient.Do(req)
	mustG7(t, e)
	defer res.Body.Close()
	b, e := io.ReadAll(res.Body)
	mustG7(t, e)
	if res.StatusCode != status {
		t.Fatalf("%s %s: HTTP %d want %d body %s", method, req.URL.Path, res.StatusCode, status, b)
	}
	var out map[string]any
	mustG7(t, json.Unmarshal(b, &out))
	checkHTTPResponseContract(t, x.api, req.URL.Path, method, status, out)
	return out
}

// 将真实 HTTP JSON 与同一 registry 生成的公开 schema 对照，捕捉字段/状态漂移。
func checkHTTPResponseContract(t *testing.T, api *httpapi.Server, path, method string, status int, value any) {
	t.Helper()
	raw, _ := json.Marshal(api.OpenAPI())
	var spec map[string]any
	mustG7(t, json.Unmarshal(raw, &spec))
	for pattern, methods := range spec["paths"].(map[string]any) {
		want, got := strings.Split(pattern, "/"), strings.Split(path, "/")
		if len(want) != len(got) {
			continue
		}
		match := true
		for i, p := range want {
			if !strings.HasPrefix(p, "{") && p != got[i] {
				match = false
			}
		}
		if !match {
			continue
		}
		op, ok := methods.(map[string]any)[strings.ToLower(method)].(map[string]any)
		if !ok {
			continue
		}
		response, ok := op["responses"].(map[string]any)[fmt.Sprint(status)].(map[string]any)
		if !ok {
			t.Fatalf("undocumented status %s %s %d", method, path, status)
		}
		content, ok := response["content"].(map[string]any)
		if !ok {
			return
		}
		schema := content["application/json"].(map[string]any)["schema"].(map[string]any)
		if err := checkJSONSchema(schema, value, "response"); err != nil {
			t.Fatalf("%s %s: %v", method, path, err)
		}
		return
	}
	if status != 404 {
		t.Fatalf("undocumented HTTP route %s %s", method, path)
	}
}
func checkJSONSchema(schema map[string]any, value any, path string) error {
	if choices, ok := schema["anyOf"].([]any); ok {
		for _, s := range choices {
			if checkJSONSchema(s.(map[string]any), value, path) == nil {
				return nil
			}
		}
		return fmt.Errorf("%s no matching anyOf", path)
	}
	typeName, _ := schema["type"].(string)
	switch typeName {
	case "null":
		if value != nil {
			return fmt.Errorf("%s expected null", path)
		}
	case "string":
		if _, ok := value.(string); !ok {
			return fmt.Errorf("%s expected string", path)
		}
	case "boolean":
		if _, ok := value.(bool); !ok {
			return fmt.Errorf("%s expected boolean", path)
		}
	case "number", "integer":
		if _, ok := value.(float64); !ok {
			return fmt.Errorf("%s expected number", path)
		}
	case "array":
		a, ok := value.([]any)
		if !ok {
			return fmt.Errorf("%s expected array", path)
		}
		if s, ok := schema["items"].(map[string]any); ok {
			for i, v := range a {
				if e := checkJSONSchema(s, v, fmt.Sprintf("%s[%d]", path, i)); e != nil {
					return e
				}
			}
		}
	case "object":
		m, ok := value.(map[string]any)
		if !ok {
			return fmt.Errorf("%s expected object", path)
		}
		if required, ok := schema["required"].([]any); ok {
			for _, name := range required {
				if _, ok := m[name.(string)]; !ok {
					return fmt.Errorf("%s missing %s", path, name)
				}
			}
		}
		props, _ := schema["properties"].(map[string]any)
		for k, v := range m {
			if s, ok := props[k].(map[string]any); ok {
				if e := checkJSONSchema(s, v, path+"."+k); e != nil {
					return e
				}
			} else if schema["additionalProperties"] == false {
				return fmt.Errorf("%s unexpected %s", path, k)
			}
		}
	}
	if choices, ok := schema["enum"].([]any); ok {
		found := false
		for _, v := range choices {
			if v == value {
				found = true
			}
		}
		if !found {
			return fmt.Errorf("%s unknown enum", path)
		}
	}
	return nil
}

func TestG8RealHTTPAuthCatalogAndCommands(t *testing.T) {
	x := g8(t)
	x.call(t, "", "GET", x.url("/runs"), "", nil, 401)
	x.call(t, "reader", "POST", x.url("/cases"), "forbidden", map[string]any{"name": "x"}, 403)
	other := seedProject(t, x.db.pool, x.s.OrganizationID)
	x.call(t, "judge", "GET", x.server.URL+"/api/v1/projects/"+other.ProjectID+"/runs", "", nil, 404)
	for _, d := range []any{map[string]any{"name": "x", "reviewer_id": "admin"}, map[string]any{"name": "x", "permission": "ADJUDICATE"}, map[string]any{"name": "x", "project_id": other.ProjectID}} {
		x.call(t, "judge", "POST", x.url("/cases"), asset.NewID(), d, 400)
	}
	one := x.call(t, "judge", "POST", x.url("/cases"), "same", map[string]any{"name": "API case"}, 200)
	two := x.call(t, "judge", "POST", x.url("/cases"), "same", map[string]any{"name": "API case"}, 200)
	if one["id"] != two["id"] {
		t.Fatal("replay")
	}
	x.call(t, "judge", "POST", x.url("/cases"), "same", map[string]any{"name": "different"}, 409)
	id := one["id"].(string)
	x.call(t, "judge", "POST", x.url("/cases/"+id+"/versions"), "publish", map[string]any{"version": "v1", "body": testCase()}, 200)
	x.call(t, "judge", "GET", x.url("/cases/"+id+"/versions/v1"), "", nil, 200)
	x.call(t, "judge", "DELETE", x.url("/cases/"+id+"/versions/v1"), "", nil, 404)
	page := x.call(t, "judge", "GET", x.url("/cases?limit=2"), "", nil, 200)
	cursor := page["next_cursor"].(string)
	next := x.call(t, "judge", "GET", x.url("/cases?limit=2&cursor="+cursor), "", nil, 200)
	a := page["items"].([]any)
	b := next["items"].([]any)
	if a[0].(map[string]any)["id"] == b[0].(map[string]any)["id"] {
		t.Fatal("pagination overlap")
	}
	x.call(t, "judge", "GET", x.url("/datasets?cursor="+cursor), "", nil, 400)
	x.call(t, "judge", "POST", x.url("/cases"), "oversize", map[string]any{"name": strings.Repeat("x", 2<<20)}, 413)
	created := x.call(t, "judge", "POST", x.url("/api-keys"), "key", map[string]any{"name": "controlled machine", "capabilities": []string{"READ"}}, 200)
	secret := created["secret"].(string)
	credential := created["credential"].(map[string]any)
	keyID := credential["id"].(string)
	replay := x.call(t, "judge", "POST", x.url("/api-keys"), "key", map[string]any{"name": "controlled machine", "capabilities": []string{"READ"}}, 200)
	if replay["secret"] != nil {
		t.Fatal("key replay leaks secret")
	}
	p, e := x.api.Identity.AuthenticateKey(context.Background(), secret)
	mustG7(t, e)
	if p.Type != "MACHINE" {
		t.Fatal(p.Type)
	}
	if _, e = x.api.Identity.Authorize(context.Background(), p, other.ProjectID, identity.Read); e == nil {
		t.Fatal("machine cross-project")
	}
	x.call(t, "judge", "POST", x.url("/api-keys/"+keyID+"/revoke"), "", map[string]any{}, 200)
	if _, e = x.api.Identity.AuthenticateKey(context.Background(), secret); e == nil {
		t.Fatal("revoked key")
	}
	var bytes string
	mustG7(t, x.db.pool.QueryRow(context.Background(), `SELECT key_digest FROM product_api_credentials WHERE id=$1`, keyID).Scan(&bytes))
	if bytes == secret || strings.Contains(x.logs.String(), secret) {
		t.Fatal("secret persisted/logged")
	}
	x.call(t, "", "GET", x.server.URL+"/health/live", "", nil, 200)
	x.call(t, "", "GET", x.server.URL+"/health/ready", "", nil, 200)
	rejectSQL(t, x.pool, "42501", "UPDATE evaluation_writer_control SET writer_epoch=writer_epoch+1")
	rejectSQL(t, x.pool, "42501", "UPDATE evaluation_results SET id=id")
	rejectSQL(t, x.pool, "42501", "TRUNCATE evaluation_review_items CASCADE")
}

func TestG8RealHTTPGateAndBlindReview(t *testing.T) {
	x := g8(t)
	policy := x.policy(t, nil)
	baseline := x.run(t, "base", g6Plan{Success: 10})
	candidate := x.run(t, "candidate", g6Plan{Success: 9, CriticalFail: true})
	fail := x.call(t, "judge", "POST", x.url("/gates"), "fail", map[string]any{"baseline": baseline, "candidate": candidate, "policy": policy}, 200)
	if fail["decision"] != "FAIL" {
		t.Fatal(fail["decision"])
	}
	missing := x.run(t, "missing", g6Plan{Success: 10, Missing: true})
	blocked := x.call(t, "judge", "POST", x.url("/gates"), "blocked", map[string]any{"baseline": baseline, "candidate": missing, "policy": policy}, 200)
	if blocked["decision"] != "BLOCKED" {
		t.Fatal(blocked["decision"])
	}
	x.call(t, "reader", "GET", x.url("/gates/"+fail["gate_id"].(string)), "", nil, 200)
	state, e := x.k.ReadRunState(context.Background(), x.s.Scope, baseline.Runs[0])
	mustG7(t, e)
	source := g7Source(t, state, x.bindings[0].Evaluator, 0)
	item := x.enqueue(t, source, x.refs[0], rv.TaskSuccess, 2, true)
	path := "/reviews/" + item.ID
	claimA := x.call(t, "A", "POST", x.url(path+"/claim"), "", map[string]any{"lease_seconds": 60}, 200)
	claimB := x.call(t, "B", "POST", x.url(path+"/claim"), "", map[string]any{"lease_seconds": 60}, 200)
	view := x.call(t, "B", "GET", x.url(path), "", nil, 200)
	safe, _ := json.Marshal(view)
	if strings.Contains(string(safe), x.principals["A"].ID) || strings.Contains(string(safe), "automatic_decision") || strings.Contains(string(safe), "annotations") {
		t.Fatal("blind API disclosure")
	}
	x.call(t, "A", "GET", x.url(path+"/adjudication-view"), "", nil, 403)
	x.call(t, "judge", "GET", x.url(path+"/adjudication-view"), "", nil, 412)
	refs := []string{}
	for _, b := range item.Source.Evidence {
		refs = append(refs, b.Ref)
	}
	for _, v := range []struct {
		who, value string
		claim      map[string]any
	}{{"A", "SUCCESS", claimA}, {"B", "FAILURE", claimB}} {
		x.call(t, v.who, "POST", x.url(path+"/annotations"), "annotation", map[string]any{"slot": v.claim["slot"], "token": v.claim["token"], "decision": map[string]any{"kind": "TASK_SUCCESS", "value": v.value, "reason": "独立判断", "evidence_refs": refs}}, 200)
	}
	ordinary := x.call(t, "A", "GET", x.url(path+"?show_all=true"), "", nil, 200)
	if ordinary["annotations"] != nil {
		t.Fatal("ordinary completed leak")
	}
	judging := x.call(t, "judge", "GET", x.url(path+"/adjudication-view"), "", nil, 200)
	if len(judging["annotations"].([]any)) != 2 {
		t.Fatal("frozen annotations")
	}
	x.call(t, "judge", "POST", x.url(path+"/adjudications"), "adjudicate", map[string]any{"decision": map[string]any{"kind": "TASK_SUCCESS", "value": "SUCCESS", "reason": "裁决", "evidence_refs": refs}, "reason": "核对证据"}, 200)
	golden := x.call(t, "judge", "POST", x.url(path+"/golden"), "golden", map[string]any{}, 200)
	x.call(t, "judge", "GET", x.url("/golden/"+golden["id"].(string)), "", nil, 200)
	samples := []rv.CalibrationSample{}
	for i, c := range state.Run.Snapshot.Input.Manifest {
		sample := rv.CalibrationSample{Case: c.Identity.Ref, Result: g7Source(t, state, x.bindings[0].Evaluator, i)}
		if i == 0 {
			sample.GoldenID = golden["id"].(string)
		}
		samples = append(samples, sample)
	}
	cal := x.call(t, "judge", "POST", x.url("/calibrations"), "calibration", map[string]any{"dataset": x.dataset, "evaluator": x.bindings[0].Evaluator, "schema": x.refs[0], "samples": samples, "protocol": rv.Protocol{Blind: true}, "sampling": rv.Sampling{Version: "review-hash.v1", Seed: "G8_HTTP", BasisPoints: 10000}, "positive_class": "SUCCESS"}, 200)
	x.call(t, "judge", "GET", x.url("/calibrations/"+cal["id"].(string)), "", nil, 200)
	body := testCase()
	body.BodyPolicy = catalog.Redacted
	draft := x.call(t, "judge", "POST", x.url("/drafts"), "draft", map[string]any{"item_id": item.ID, "golden_id": golden["id"], "case_version": "reviewed.v1", "body": body, "sanitization": "REDACTED", "policy": "controlled.v1", "reason": "人工脱敏反馈", "human_supplement": true}, 200)
	x.call(t, "judge", "GET", x.url("/drafts/"+draft["id"].(string)), "", nil, 200)
	x.call(t, "judge", "POST", x.url("/drafts/"+draft["id"].(string)+"/publish"), "publish-feedback", map[string]any{"name": "审核用例"}, 200)
	x.call(t, "judge", "POST", x.url("/dataset-feedback"), "feedback", map[string]any{"base": x.dataset, "target": asset.Ref{EntityID: x.dataset.EntityID, Version: "reviewed.v2"}, "draft_ids": []string{draft["id"].(string)}}, 200)
	exceptionReview := x.call(t, "judge", "POST", x.url("/reviews"), "exception-review", map[string]any{"source": map[string]any{"type": "GATE_DECISION", "gate_id": fail["gate_id"]}, "schema": x.quality, "kind": rv.Quality, "policy": rv.Policy{Ref: "g8-exception.v1", Reviews: 1, Protocol: rv.Protocol{Blind: true}, Sampling: rv.Sampling{Version: "review-hash.v1", Seed: "G8_EXCEPTION", BasisPoints: 10000}}, "reason": "显式请求例外审核", "priority": "NORMAL", "criticality": "NORMAL"}, 200)
	itemPath := "/reviews/" + exceptionReview["id"].(string)
	owned := x.call(t, "judge", "POST", x.url(itemPath+"/claim"), "", map[string]any{"lease_seconds": 60}, 200)
	presented := x.call(t, "judge", "GET", x.url(itemPath), "", nil, 200)
	evidence := []string{}
	for _, b := range presented["evidence"].([]any) {
		evidence = append(evidence, b.(map[string]any)["ref"].(string))
	}
	x.call(t, "judge", "POST", x.url(itemPath+"/annotations"), "exception-annotation", map[string]any{"slot": owned["slot"], "token": owned["token"], "decision": map[string]any{"kind": rv.Quality, "value": "PASS", "reason": "核对冻结例外依据", "evidence_refs": evidence}}, 200)
	for _, raw := range fail["issues"].([]any) {
		issue := raw.(map[string]any)
		if issue["Decision"] != "FAIL" {
			continue
		}
		approval := x.call(t, "judge", "POST", x.url("/gate-exceptions"), "exception-approval", map[string]any{"item_id": exceptionReview["id"], "gate_id": fail["gate_id"], "code": issue["Code"], "scope": issue["Scope"], "requested_reason": "显式例外审核", "decision": "APPROVED", "reason": "批准冻结证明", "expiry": time.Now().UTC().Add(time.Hour)}, 200)
		x.call(t, "judge", "GET", x.url("/gate-exceptions/"+approval["ID"].(string)+"/proof"), "", nil, 200)
		p, e := x.gate.GetPolicyVersion(context.Background(), x.s.Scope, policy)
		mustG7(t, e)
		x.call(t, "judge", "POST", x.url("/policies/"+policy.EntityID+"/versions-with-exceptions"), "approved-policy", map[string]any{"version": "approved.http.v2", "body": p.Content().Body, "exception_proof_ids": []string{approval["ID"].(string)}}, 200)
		break
	}
	old := x.call(t, "judge", "GET", x.url("/gates/"+fail["gate_id"].(string)), "", nil, 200)
	if old["decision"] != "FAIL" {
		t.Fatal("approval overwrote old gate")
	}
	x.call(t, "A", "POST", x.url(path+"/annotations"), "stale", map[string]any{"slot": claimA["slot"], "token": asset.NewID(), "decision": map[string]any{"kind": "TASK_SUCCESS", "value": "SUCCESS", "reason": "旧决定", "evidence_refs": refs}}, 409)
	for _, kind := range []string{"datasets", "runs", "experiments", "gates", "rules", "calibrations", "drafts", "api-keys"} {
		x.call(t, "judge", "GET", x.url("/"+kind), "", nil, 200)
	}
	// 数据库撤销 membership 立即作用于后续 Bearer 请求，不缓存授权。
	_, e = x.db.pool.Exec(context.Background(), `DELETE FROM product_project_memberships WHERE project_id=$1 AND principal_id=$2`, x.s.ProjectID, x.principals["A"].ID)
	mustG7(t, e)
	x.call(t, "A", "GET", x.url(path), "", nil, 404)

}

func TestTraceHTTPReceipt(t *testing.T) {
	x := g8(t)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	if _, err := x.db.pool.Exec(ctx, postgres.TraceIngestRoleGrants(x.role)); err != nil {
		t.Fatal(err)
	}
	created := x.call(t, "judge", "POST", x.url("/api-keys"), "trace-receipt-key", map[string]any{"name": "Trace receipt integration", "capabilities": []string{"READ", "WRITE"}}, 200)
	secret := created["secret"].(string)
	producerRoot := os.Getenv("LOCALAGENT_TRACE_PRODUCER_ROOT")
	if producerRoot == "" {
		t.Fatal("LOCALAGENT_TRACE_PRODUCER_ROOT is required for producer-to-PostgreSQL integration")
	}
	// 捕获真实 exporter 的 wire body，再转交同一个生产 HTTP handler。
	var captureMu sync.Mutex
	var bodies [][]byte
	capture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "capture failed", http.StatusBadRequest)
			return
		}
		captureMu.Lock()
		bodies = append(bodies, append([]byte(nil), raw...))
		captureMu.Unlock()
		r.Body = io.NopCloser(bytes.NewReader(raw))
		x.api.Handler().ServeHTTP(w, r)
	}))
	defer capture.Close()
	fixture, err := filepath.Abs("../../testdata/trace_contract/runtime_receipt.py")
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.CommandContext(ctx, "uv", "run", "--frozen", "--no-sync", "python", fixture)
	cmd.Dir = producerRoot
	cmd.Env = append(os.Environ(),
		"PYTHONPATH="+producerRoot,
		"PYTHONIOENCODING=utf-8",
		"LOCALAGENT_TRACE_API_BASE_URL="+capture.URL,
		"LOCALAGENT_TRACE_API_KEY="+secret,
		"LOCALAGENT_TRACE_PROJECT_ID="+x.s.ProjectID,
	)
	produced, err := cmd.Output()
	if err != nil {
		if failure, ok := err.(*exec.ExitError); ok {
			t.Fatalf("Python Runtime/exporter failed: %v\n%s", err, strings.ReplaceAll(string(failure.Stderr), secret, "[redacted]"))
		}
		t.Fatal(err)
	}
	var run struct {
		RunID      string `json:"run_id"`
		TraceID    string `json:"trace_id"`
		ModelCalls int    `json:"model_calls"`
		Sent       int    `json:"sent_count"`
	}
	if err = json.Unmarshal(produced, &run); err != nil {
		t.Fatal(err)
	}
	captureMu.Lock()
	runtimeBodies := append([][]byte(nil), bodies...)
	captureMu.Unlock()
	if run.RunID == "" || run.TraceID == "" || run.ModelCalls < 4 || run.Sent != len(runtimeBodies) {
		t.Fatalf("incomplete Runtime execution: %+v, wire bodies=%d", run, len(runtimeBodies))
	}
	sendURL := func(method, target string, body []byte) (int, map[string]any) {
		t.Helper()
		req, err := http.NewRequest(method, target, bytes.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("X-API-Key", secret)
		req.Header.Set("Content-Type", "application/json")
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		response, err := io.ReadAll(res.Body)
		if err != nil {
			t.Fatal(err)
		}
		var decoded map[string]any
		if err = json.Unmarshal(response, &decoded); err != nil {
			t.Fatalf("invalid HTTP JSON: %v", err)
		}
		for _, marker := range []string{"MARKER_PROMPT_PRIVATE", "MARKER_TOOL_ARGUMENTS_PRIVATE", "MARKER_MEMORY_PRIVATE", secret} {
			if bytes.Contains(response, []byte(marker)) {
				t.Fatal("sensitive marker in HTTP response")
			}
		}
		return res.StatusCode, decoded
	}
	send := func(method, path string, body []byte) (int, map[string]any) {
		t.Helper()
		return sendURL(method, x.url(path), body)
	}

	operations := map[string]bool{}
	steps := map[string]bool{}
	spanIDs := map[string]bool{}
	parents := map[string]string{}
	var raw []byte
	var id string
	for _, body := range runtimeBodies {
		d, err := ob.DecodeTrace(body)
		if err != nil || d.Trace.RunID != run.RunID || d.Trace.TraceID != run.TraceID {
			t.Fatalf("Runtime wire identity mismatch: %v", err)
		}
		if spanIDs[d.Trace.SpanID] {
			t.Fatal("Runtime sent a duplicate span before replay")
		}
		spanIDs[d.Trace.SpanID] = true
		operations[d.Trace.Operation] = true
		if d.Trace.Step != nil {
			steps[*d.Trace.Step] = true
		}
		if d.Trace.Parent != nil {
			parents[d.Trace.SpanID] = *d.Trace.Parent
		}
		var receiptID, source, schema, fingerprint, digest, parentIntegrity string
		var canonical, envelope, attributes, observation []byte
		var version int
		err = x.db.pool.QueryRow(ctx, `SELECT o.id::text,o.source,o.schema_version,sc.contract_version,sc.contract_fingerprint,sc.canonical_payload_digest,o.canonical_bytes,o.envelope_bytes,sc.attributes::text::bytea,o.observation_bytes FROM evaluation_observations o JOIN localagent_trace_envelope_sidecars sc ON sc.envelope_id=o.receipt_id WHERE o.project_id=$1 AND o.source_identity=$2`, x.s.ProjectID, d.Trace.SpanID).Scan(&receiptID, &source, &schema, &version, &fingerprint, &digest, &canonical, &envelope, &attributes, &observation)
		if err != nil || version != 2 || fingerprint != ob.TraceV2Fingerprint || source != ob.TraceV2Source || schema != "trace-v2" || digest != d.Digest || !bytes.Equal(canonical, d.Canonical) || !bytes.Equal(envelope, body) {
			t.Fatalf("persisted Runtime receipt mismatch: %v", err)
		}
		status, receipt := send(http.MethodGet, "/observations/"+receiptID, nil)
		parentIntegrity, _ = receipt["parent_integrity"].(string)
		wantParent := "NOT_APPLICABLE"
		if d.Trace.Parent != nil {
			wantParent = "MATCHED"
		}
		if status != http.StatusOK || receipt["id"] != receiptID || receipt["digest"] != d.Digest || receipt["schema"] != schema || receipt["operation"] != d.Trace.Operation || parentIntegrity != wantParent {
			t.Fatal("independent Runtime receipt GET mismatch")
		}
		for _, marker := range []string{"MARKER_PROMPT_PRIVATE", "MARKER_TOOL_ARGUMENTS_PRIVATE", "MARKER_MEMORY_PRIVATE", secret} {
			if bytes.Contains(body, []byte(marker)) || bytes.Contains(canonical, []byte(marker)) || bytes.Contains(attributes, []byte(marker)) || bytes.Contains(observation, []byte(marker)) {
				t.Fatal("sensitive marker in exported or persisted Trace")
			}
		}
		if d.Trace.Operation == "runtime.run" {
			raw, id = body, receiptID
		}
	}
	for _, operation := range []string{"runtime.run", "runtime.planning", "runtime.step", "runtime.synthesis", "runtime.output_delivery", "runtime.final_memory_commit"} {
		if !operations[operation] {
			t.Fatalf("Runtime did not naturally emit %s", operation)
		}
	}
	if len(steps) < 3 {
		t.Fatal("specialists and synthesis did not execute")
	}
	for _, parent := range parents {
		if !spanIDs[parent] {
			t.Fatal("Runtime Trace references an unreceived parent")
		}
	}
	counts := func() [3]int {
		t.Helper()
		var n [3]int
		if err := x.db.pool.QueryRow(ctx, `SELECT (SELECT count(*) FROM localagent_trace_envelope_sidecars WHERE external_run_id=$1),(SELECT count(*) FROM evaluation_observations WHERE runtime_id=$1),(SELECT count(*) FROM spans WHERE trace_id IN (SELECT internal_trace_uuid FROM localagent_external_trace_identity WHERE run_id=$1))`, run.RunID).Scan(&n[0], &n[1], &n[2]); err != nil {
			t.Fatal(err)
		}
		return n
	}
	before := counts()
	if before != [3]int{run.Sent, run.Sent, run.Sent} {
		t.Fatalf("Runtime receipt coverage mismatch: %v", before)
	}
	otherProject := seedProject(t, x.db.pool, x.s.OrganizationID)
	status, _ := sendURL(http.MethodPost, x.server.URL+"/api/v1/projects/"+otherProject.ProjectID+"/trace-envelopes", raw)
	if status != http.StatusNotFound {
		t.Fatalf("cross-project returned %d, want 404", status)
	}
	otherOrganizationProject := seedProject(t, x.db.pool, asset.NewID())
	status, _ = sendURL(http.MethodPost, x.server.URL+"/api/v1/projects/"+otherOrganizationProject.ProjectID+"/trace-envelopes", raw)
	if status != http.StatusNotFound {
		t.Fatalf("cross-organization returned %d, want 404", status)
	}
	var unauthorized int
	if err = x.db.pool.QueryRow(ctx, `SELECT count(*) FROM localagent_trace_envelope_sidecars WHERE project_id IN ($1,$2)`, otherProject.ProjectID, otherOrganizationProject.ProjectID).Scan(&unauthorized); err != nil || unauthorized != 0 {
		t.Fatal("unauthorized project received a Trace", err)
	}
	status, first := send(http.MethodPost, "/trace-envelopes", raw)
	if status != http.StatusOK || first["command_status"] != string(ev.AlreadyApplied) || first["id"] != id {
		t.Fatalf("repeat after Python export: status=%d response=%v", status, first)
	}
	unknownProfile := bytes.Replace(raw, []byte(ob.TraceV2Fingerprint), []byte(strings.Repeat("0", 64)), 1)
	status, _ = send(http.MethodPost, "/trace-envelopes", unknownProfile)
	if status != http.StatusBadRequest {
		t.Fatalf("unknown profile returned HTTP %d", status)
	}
	status, repeated := send(http.MethodPost, "/trace-envelopes", raw)
	if status != http.StatusOK || repeated["command_status"] != string(ev.AlreadyApplied) || repeated["id"] != id {
		t.Fatalf("repeated ingest: status=%d response=%v", status, repeated)
	}
	var changedEnvelope map[string]any
	if err = json.Unmarshal(raw, &changedEnvelope); err != nil {
		t.Fatal(err)
	}
	changedEnvelope["duration_ms"] = changedEnvelope["duration_ms"].(float64) + 0.1
	changed, err := json.Marshal(changedEnvelope)
	if err != nil {
		t.Fatal(err)
	}
	status, _ = send(http.MethodPost, "/trace-envelopes", changed)
	if status != http.StatusConflict {
		t.Fatalf("same span with changed digest returned HTTP %d", status)
	}
	status, _ = send(http.MethodGet, "/observations/"+id, nil)
	if status != http.StatusOK {
		t.Fatalf("receipt GET returned HTTP %d", status)
	}

	if after := counts(); after != before {
		t.Fatalf("replay/conflict/authorization changed row counts: before=%v after=%v", before, after)
	}
	for _, marker := range []string{"MARKER_PROMPT_PRIVATE", "MARKER_TOOL_ARGUMENTS_PRIVATE", "MARKER_MEMORY_PRIVATE", secret} {
		if bytes.Contains(x.logs.Bytes(), []byte(marker)) {
			t.Fatal("sensitive marker in HTTP logs")
		}
	}
	t.Logf("Runtime receipt coverage: operations=%d spans=%d steps=%d model_calls=%d", len(operations), run.Sent, len(steps), run.ModelCalls)

	scope := x.s5
	scope.Ingest = true
	envelope, err := asset.ParseJSON([]byte(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	forged := ob.Observation{Ref: ob.Ref{ProjectID: scope.ProjectID, ID: asset.NewID(), Kind: ob.TraceKind}, Trust: "NORMAL_OBSERVATION", Source: ob.TraceV2Source, TraceID: "forged-trace", Completed: time.Now(), Envelope: envelope, Retention: "EXPLICIT_EVALUATION_RETENTION"}
	forgedReply, err := x.online.StoreObservation(ctx, scope, forged)
	if err != nil || forgedReply.Code != ev.Rejected {
		t.Fatalf("normal ingest forged strict source: reply=%+v err=%v", forgedReply, err)
	}
}

func TestG8PublishedEvaluatorExperimentAndOnlinePagination(t *testing.T) {
	x := g8(t)
	ctx := context.Background()
	v, e := g6Readers(x.g6Fixture).GetEvaluatorDefinitionVersion(ctx, x.s.Scope, x.bindings[0].Evaluator)
	mustG7(t, e)
	implementation := v.Content().Body.ImplementationRef
	x.api.SupportsEvaluator = func(d metric.EvaluatorDefinition) bool { return d.ImplementationRef == implementation }
	// 模拟启动后发布：新版本不存在于启动时的能力 inventory。
	x.call(t, "judge", "POST", x.url("/evaluators/"+v.Ref().EntityID+"/versions"), "new-evaluator", map[string]any{"version": "new.v2", "body": v.Content().Body}, 200)
	binding := x.bindings[0]
	binding.Evaluator.Version = "new.v2"
	run := map[string]any{"dataset": x.dataset, "evaluators": []catalog.EvaluatorBinding{binding}, "target": map[string]any{"id": "controlled", "kind": "FIXTURE", "version": "v1", "config": map[string]any{}, "capabilities": map[string]any{}, "timeout_milliseconds": 1000}, "subject": map[string]any{"agent": "G8_HTTP", "revision": "v1"}}
	created := x.call(t, "judge", "POST", x.url("/runs"), "new-run", run, 200)
	x.call(t, "judge", "GET", x.url("/runs/"+created["id"].(string)), "", nil, 200)
	experiment := x.call(t, "judge", "POST", x.url("/experiments"), "experiment", map[string]any{"name": "HTTP实验", "candidate": map[string]any{"version": "v1"}, "repeat": 2, "run": run}, 200)
	x.call(t, "judge", "POST", x.url("/experiments/"+experiment["id"].(string)+"/materialize"), "materialize", map[string]any{}, 200)
	view := x.call(t, "judge", "GET", x.url("/experiments/"+experiment["id"].(string)), "", nil, 200)
	if len(view["runs"].([]any)) != 2 {
		t.Fatal("experiment runs missing")
	}
	metadata, _ := asset.Freeze(map[string]string{"subject": "G8_HTTP", "agent_version": "v1", "environment": "test", "run_mode": "controlled"})
	envelope, _ := asset.Freeze(map[string]any{"attributes": map[string]string{"delivery_status": "OUTCOME_UNKNOWN"}})
	o := ob.Observation{Ref: ob.Ref{ID: asset.NewID(), ProjectID: x.s.ProjectID, Kind: ob.TraceKind, Schema: "trace-v1"}, TraceID: asset.NewID(), RuntimeID: asset.NewID(), Source: "G8_CONTROLLED", Trust: "NORMAL_OBSERVATION", Status: "ERROR", Started: time.Now().UTC().Add(-time.Minute), Completed: time.Now().UTC(), Metadata: metadata, Envelope: envelope, BodyAvailability: "SAFE_METADATA_ONLY", Retention: "EXPLICIT_EVALUATION_RETENTION"}
	r, e := x.online.StoreObservation(ctx, x.s5, o)
	reply(t, r, e, "APPLIED")
	x.call(t, "judge", "GET", x.url("/observations/"+o.Ref.ID), "", nil, 200)
	first := x.call(t, "judge", "GET", x.url("/online/failure-candidates?limit=1"), "", nil, 200)
	second := x.call(t, "judge", "GET", x.url("/online/failure-candidates?limit=1&cursor="+url.QueryEscape(first["next_cursor"].(string))), "", nil, 200)
	a, b := first["items"].([]any), second["items"].([]any)
	if len(a) != 1 || len(b) != 1 || a[0].(map[string]any)["Classification"] == b[0].(map[string]any)["Classification"] {
		t.Fatal("same observation candidates lost across pages")
	}
	logical := x.call(t, "judge", "POST", x.url("/rules"), "online-rule", map[string]any{"name": "HTTP在线规则"}, 200)
	id := logical["id"].(string)
	ref := asset.Ref{EntityID: id, Version: "http.v2"}
	x.call(t, "judge", "POST", x.url("/rules/"+id+"/versions"), "rule-version", map[string]any{"version": ref.Version, "enabled": true, "scope": "TRACE", "trust": "NORMAL_OBSERVATION", "source": "G8_CONTROLLED", "filter": ob.Filter{}, "sampling": ob.Sampling{Policy: "ALL", Algorithm: "sha256-trace-basis-points", Version: 1, BasisPoints: 10000}, "budget": ob.Budget{MaxSampled: 100, MaxWorks: 100, MaxProviderCalls: 100, MaxEstimatedTokens: 1000000, MaxReportedTokens: 1000000}, "rate": ob.Rate{Mode: "BOUNDED_CONCURRENCY"}, "allow_backfill": true, "backfill_max": 100, "bindings": []any{map[string]any{"evaluator": x.binding.Evaluator.Identity.Ref, "metric": x.binding.Metric.Identity.Ref, "required": true, "applicability": x.binding.Evaluator.Applicability}}}, 200)
	x.call(t, "judge", "GET", x.url("/rules/"+id+"/versions"), "", nil, 200)
	x.call(t, "judge", "GET", x.url("/rules/"+id+"/versions/"+ref.Version), "", nil, 200)
	x.call(t, "judge", "GET", x.url("/rules/"+id+"/versions/"+ref.Version+"/coverage"), "", nil, 200)
	backfill := x.call(t, "judge", "POST", x.url("/online/backfills"), "backfill", map[string]any{"rule": ref, "from": o.Started.Add(-time.Minute), "until": o.Completed.Add(time.Minute), "max_records": 10}, 200)
	x.call(t, "judge", "GET", x.url("/online/backfills/"+backfill["id"].(string)), "", nil, 200)
	r, e = x.online.Materialize(ctx, x.s5, ref, o.Ref.ID, true)
	reply(t, r, e, "APPLIED")
	work := workID(t, x.g5Fixture, ref, o.Ref.ID)
	x.call(t, "judge", "GET", x.url("/online/works/"+work), "", nil, 200)
}
