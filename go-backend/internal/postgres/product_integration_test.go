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
	"strings"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
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
