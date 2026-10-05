package httpapi

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"context"
	"encoding/json"
	"errors"
	"github.com/jackc/pgx/v5/pgxpool"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestTransportAndProjection(t *testing.T) {
	for _, body := range []string{`{"name":"a","reviewer_id":"admin"}`, `{"name":"a","can_adjudicate":true}`, `{"name":"a","project_id":"x"}`, `{"name":"a","name":"b"}`} {
		j, e := asset.ParseJSON([]byte(body))
		var d createLogicalRequest
		if e == nil && j.Decode(&d) == nil {
			t.Fatalf("forged transport accepted: %s", body)
		}
	}
	for _, v := range []struct {
		err    error
		status int
		code   string
	}{{identity.ErrUnauthenticated, 401, "UNAUTHENTICATED"}, {asset.ErrForbidden, 403, "FORBIDDEN"}, {asset.ErrNotFound, 404, "NOT_FOUND"}, {rv.ErrOwnershipLost, 409, "OWNERSHIP_LOST"}, {asset.ErrConflict, 409, "CONFLICT"}, {asset.ErrUnsupported, 412, "PRECONDITION_FAILED"}, {context.DeadlineExceeded, 503, "UNAVAILABLE"}, {errors.New("postgres secret"), 500, "INTERNAL"}} {
		status, code := mapError(v.err)
		if status != v.status || code != v.code {
			t.Fatal(status, code)
		}
	}
	p := identity.Principal{ID: "A", Capabilities: []identity.Capability{identity.Review}}
	v := rv.ReadModel{Item: rv.Item{ID: asset.NewID(), Status: "ADJUDICATION_REQUIRED"}}
	v.Item.Command.Policy.Protocol.Blind = true
	v.Annotations = []rv.Annotation{{Reviewer: rv.Reviewer{ID: "B"}}}
	v.Item.Source.Automatic = &rv.Automatic{Decision: "FAIL"}
	raw, _ := json.Marshal(reviewProjection(v, p, false))
	if strings.Contains(string(raw), "FAIL") || strings.Contains(string(raw), "annotations") {
		t.Fatal("blind/double-review disclosure")
	}
	p.Capabilities = append(p.Capabilities, identity.Adjudicate)
	raw, _ = json.Marshal(reviewProjection(v, p, true))
	if !strings.Contains(string(raw), "annotations") || !strings.Contains(string(raw), "FAIL") {
		t.Fatal("authorized adjudication")
	}
}

func TestCursorAndOpenAPI(t *testing.T) {
	pool, e := pgxpool.New(context.Background(), "postgres://localhost:1/unused")
	if e != nil {
		t.Fatal(e)
	}
	defer pool.Close()
	s, e := New(Server{Pool: pool, Identity: postgres.ProductIdentity{Pool: pool, Pepper: []byte(strings.Repeat("p", 32))}, Config: DefaultConfig()})
	if e != nil {
		t.Fatal(e)
	}
	project := asset.NewID()
	key := *s.encodeCursor(project, "runs", "safe")
	h, _ := http.NewRequest("GET", "http://localhost/?cursor="+key+"&limit=2", nil)
	r := request{server: s, HTTP: h, Access: identity.Access{Scope: asset.Scope{ProjectID: project}}}
	after, n, e := r.pagination("runs")
	if e != nil || after != "safe" || n != 2 {
		t.Fatal(e)
	}
	if _, _, e = r.pagination("gates"); e == nil {
		t.Fatal("cursor resource swap")
	}
	h.URL.RawQuery = "cursor=" + key + "x"
	if _, _, e = r.pagination("runs"); e == nil {
		t.Fatal("cursor tamper")
	}
	spec := s.OpenAPI()
	paths := spec["paths"].(map[string]any)
	for _, route := range s.routes {
		entry := paths[route.Path].(map[string]any)[strings.ToLower(route.Method)].(map[string]any)
		if entry["responses"] == nil || entry["x-capability"] != route.Capability {
			t.Fatal(route.Path)
		}
		if route.Input != nil && entry["requestBody"] == nil {
			t.Fatal("missing request schema", route.Path)
		}
	}
	raw, _ := json.Marshal(spec)
	for _, sensitive := range []string{"postgres://", "Bearer ey", "aep_", "provider_token"} {
		if strings.Contains(string(raw), sensitive) {
			t.Fatal("secret example")
		}
	}
}

func TestGracefulLifecycle(t *testing.T) {
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithCancel(context.Background())
	started := make(chan struct{})
	release := make(chan struct{})
	done := make(chan error, 1)
	go func() {
		done <- Serve(ctx, l, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { close(started); <-release; w.Write([]byte("completed")) }), time.Second)
	}()
	response := make(chan string, 1)
	go func() {
		res, e := http.Get("http://" + l.Addr().String())
		if e != nil {
			response <- "error"
			return
		}
		defer res.Body.Close()
		b, _ := io.ReadAll(res.Body)
		response <- string(b)
	}()
	<-started
	cancel()
	time.Sleep(20 * time.Millisecond)
	if c, e := net.DialTimeout("tcp", l.Addr().String(), 100*time.Millisecond); e == nil {
		c.Close()
		t.Fatal("listener still accepting")
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if v := <-response; v != "completed" {
		t.Fatal("in-flight lost")
	}
}
