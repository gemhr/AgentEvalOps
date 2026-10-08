// Package httpapi 仅负责认证、授权、transport 与 owner 命令适配。
package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"

	"io"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Config struct {
	DBTimeout, ShutdownTimeout time.Duration
	BodyLimit                  int64
	AllowedOrigins             []string
	RequestsPerMinute          int
}

func DefaultConfig() Config {
	return Config{DBTimeout: 5 * time.Second, ShutdownTimeout: 15 * time.Second, BodyLimit: 1 << 20, RequestsPerMinute: 120}
}

type Server struct {
	Pool              *pgxpool.Pool
	Identity          postgres.ProductIdentity
	Bearer            identity.BearerProvider
	Dev               *identity.DevAuth
	Config            Config
	Log               *slog.Logger
	Epoch             int64
	Kernel            postgres.Evaluation
	SupportsEvaluator func(metric.EvaluatorDefinition) bool
	mux               *http.ServeMux
	routes            []route
	limiter           *rateLimiter
}
type route struct {
	Method, Path string
	Capability   identity.Capability
	Create       bool
	Input        reflect.Type
	handle       func(*request) (any, error)
}
type request struct {
	server        *Server
	HTTP          *http.Request
	Access        identity.Access
	ID, CommandID string
	Body          asset.JSON
	Raw           []byte
}

func (r *request) ctx() context.Context      { return r.HTTP.Context() }
func (r *request) scope() asset.Scope        { return r.Access.Scope }
func (r *request) reviewScope() rv.Scope     { return r.Access.Review(r.server.Epoch) }
func (r *request) evaluationScope() ev.Scope { return r.Access.Evaluation(r.server.Epoch) }
func (r *request) decode(v any) error {
	if r.Body.Decode(v) != nil {
		return asset.ErrInvalid
	}
	return nil
}
func New(s Server) (*Server, error) {
	if s.Pool == nil || len(s.Identity.Pepper) < 32 || s.Config.DBTimeout <= 0 || s.Config.DBTimeout > time.Minute || s.Config.ShutdownTimeout <= 0 || s.Config.BodyLimit < 16384 || s.Config.BodyLimit > 8<<20 || s.Config.RequestsPerMinute < 1 {
		return nil, asset.ErrInvalid
	}
	for _, origin := range s.Config.AllowedOrigins {
		if origin == "*" || origin == "" {
			return nil, asset.ErrInvalid
		}
	}
	if s.Dev != nil {
		if s.Dev.Validate() != nil {
			return nil, asset.ErrInvalid
		}
		s.Bearer = *s.Dev
	}
	if s.Log == nil {
		s.Log = slog.Default()
	}
	s.mux = http.NewServeMux()
	s.limiter = &rateLimiter{entries: map[string]rateEntry{}, max: s.Config.RequestsPerMinute}
	s.catalogRoutes()
	s.productRoutes()
	s.reviewRoutes()
	s.analyticsRoutes()
	s.traceRoutes()
	s.stage13DeliveryRoutes()
	s.stage13DevelopmentRoutes()
	s.mux.HandleFunc("GET /health/live", func(w http.ResponseWriter, _ *http.Request) { write(w, 200, map[string]string{"status": "live"}) })
	s.mux.HandleFunc("GET /health/ready", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), s.Config.DBTimeout)
		defer cancel()
		if _, err := s.Identity.VerifyAPI(ctx); err != nil {
			writeError(w, 503, "UNAVAILABLE", requestID(r))
			return
		}
		write(w, 200, map[string]string{"status": "ready"})
	})
	s.mux.HandleFunc("GET /api/v1/openapi.json", func(w http.ResponseWriter, _ *http.Request) { write(w, 200, s.OpenAPI()) })
	s.mux.HandleFunc("POST /api/v1/auth/dev-login", s.login)
	s.mux.HandleFunc("GET /api/v1/me", s.me)
	s.mux.HandleFunc("GET /api/v1/projects", s.projects)
	s.mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { writeError(w, 404, "NOT_FOUND", requestID(r)) })
	return &s, nil
}
func (s *Server) add(method, path string, cap identity.Capability, create bool, input any, fn func(*request) (any, error)) {
	e := route{method, "/api/v1/projects/{project_id}" + path, cap, create, reflect.TypeOf(input), fn}
	s.routes = append(s.routes, e)
	s.mux.HandleFunc(method+" "+e.Path, func(w http.ResponseWriter, h *http.Request) { s.serve(w, h, e) })
}
func (s *Server) Authenticate(ctx context.Context, h *http.Request) (identity.Principal, error) {
	key, auth := h.Header.Get("X-API-Key"), h.Header.Get("Authorization")
	if key != "" && auth != "" {
		return identity.Principal{}, identity.ErrUnauthenticated
	}
	if key != "" {
		return s.Identity.AuthenticateKey(ctx, key)
	}
	if len(auth) > 8192 || !strings.HasPrefix(auth, "Bearer ") || s.Bearer == nil {
		return identity.Principal{}, identity.ErrUnauthenticated
	}
	return s.Bearer.AuthenticateBearer(ctx, strings.TrimPrefix(auth, "Bearer "))
}
func (s *Server) serve(w http.ResponseWriter, h *http.Request, e route) {
	started := time.Now()
	id := requestID(h)
	w.Header().Set("X-Request-ID", id)
	w.Header().Set("Cache-Control", "no-store")
	status := 200
	principal := ""
	project := h.PathValue("project_id")
	s.Log.Info("request_started", "request_id", id, "route", e.Path, "method", e.Method)
	defer func() {
		s.Log.Info("request_completed", "request_id", id, "principal_id", principal, "project_id", project, "route", e.Path, "method", e.Method, "status", status, "duration_ms", time.Since(started).Milliseconds())
	}()
	ctx, cancel := context.WithTimeout(h.Context(), s.Config.DBTimeout)
	defer cancel()
	h = h.WithContext(ctx)
	fail := func(err error) { var code string; status, code = mapError(err); writeError(w, status, code, id) }
	p, err := s.Authenticate(ctx, h)
	if err != nil {
		s.Log.Info("auth_failed", "request_id", id)
		fail(err)
		return
	}
	principal = p.ID
	if !s.limiter.allow(p.ID) {
		status = 429
		s.Log.Info("rate_limited", "request_id", id, "principal_id", p.ID)
		w.Header().Set("Retry-After", "60")
		writeError(w, 429, "RATE_LIMITED", id)
		return
	}
	a, err := s.Identity.Authorize(ctx, p, project, e.Capability)
	if err != nil {
		s.Log.Info("authorization_denied", "request_id", id, "principal_id", p.ID)
		fail(err)
		return
	}
	if (e.Capability == identity.Review || e.Capability == identity.Adjudicate || e.Capability == identity.PublishGolden || e.Capability == identity.PublishDataset || e.Capability == identity.ApproveException) && p.Type != "HUMAN" {
		fail(asset.ErrForbidden)
		return
	}
	r := &request{server: s, HTTP: h, Access: a, ID: id}
	if h.Method == "POST" {
		limit := s.Config.BodyLimit
		if strings.HasSuffix(e.Path, "/trace-envelopes") {
			media, _, parseErr := mime.ParseMediaType(h.Header.Get("Content-Type"))
			if parseErr != nil || media != "application/json" {
				fail(asset.ErrInvalid)
				return
			}
			limit = 16384
		}
		if e.Capability == identity.ManageAPIKey || strings.HasSuffix(e.Path, "/claim") || strings.HasSuffix(e.Path, "/renew") || strings.HasSuffix(e.Path, "/release") {
			limit = min(limit, 16384)
		}
		raw, err := io.ReadAll(http.MaxBytesReader(w, h.Body, limit))
		if err != nil {
			status = 413
			writeError(w, status, "INVALID_ARGUMENT", id)
			return
		}
		if len(raw) == 0 {
			raw = []byte("{}")
		}
		trimmed := strings.TrimSpace(string(raw))
		if len(trimmed) == 0 || trimmed[0] != '{' {
			fail(asset.ErrInvalid)
			return
		}
		r.Body, err = asset.ParseJSON(raw)
		if err != nil {
			fail(asset.ErrInvalid)
			return
		}
		r.Raw = raw
	}
	var cmd *postgres.ProductCommand
	if e.Create {
		key := h.Header.Get("Idempotency-Key")
		if len(key) < 1 || len(key) > 128 || !asset.ContentText(key) || strings.ContainsAny(key, "\r\n\x00") {
			fail(asset.ErrInvalid)
			return
		}
		r.CommandID = identity.CommandID(p.ID, project, e.Method+" "+h.URL.Path, key)
		var replay []byte
		var replayStatus int
		cmd, replay, replayStatus, err = postgres.BeginProductCommand(ctx, s.Pool, a.Scope, r.CommandID, e.Method+" "+h.URL.Path, r.Body.Digest())
		if err != nil {
			s.Log.Info("conflict", "request_id", id)
			fail(err)
			return
		}
		defer func() {
			cleanup, cancel := context.WithTimeout(context.Background(), s.Config.DBTimeout)
			defer cancel()
			cmd.Close(cleanup)
		}()
		if replayStatus != 0 {
			s.Log.Info("idempotency_replay", "request_id", id)
			status = replayStatus
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(status)
			_, _ = w.Write(replay)
			return
		}
	}
	out, err := e.handle(r)
	if err != nil {
		fail(err)
		return
	}
	raw, err := json.Marshal(out)
	if err != nil {
		fail(err)
		return
	}
	if cmd != nil {
		saved := raw
		if created, ok := out.(credentialCreated); ok {
			created.Secret = ""
			saved, _ = json.Marshal(created)
		}
		if err = cmd.Complete(ctx, saved, status); err != nil {

			fail(err)
			return
		}
	}
	if immutableRoute(e) {
		if conditional(w, h, projectionETag(s.Identity.Pepper, p.ID, project, h.URL.Path, raw)) {
			status = 304
			return
		}
	}
	if strings.Contains(e.Path, "/exports/") && h.URL.Query().Get("format") == "ndjson" {
		var page map[string]json.RawMessage
		if err = json.Unmarshal(raw, &page); err != nil {
			fail(err)
			return
		}
		var items []json.RawMessage
		if err = json.Unmarshal(page["items"], &items); err != nil {
			fail(err)
			return
		}
		delete(page, "items")
		var buffer bytes.Buffer
		enc := json.NewEncoder(&buffer)
		_ = enc.Encode(map[string]any{"metadata": page})
		for _, item := range items {
			_ = enc.Encode(item)
		}
		w.Header().Set("Content-Type", "application/x-ndjson")
		w.WriteHeader(status)
		_, _ = w.Write(buffer.Bytes())
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(raw)
}
func (s *Server) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := asset.NewID()
		r = r.WithContext(context.WithValue(r.Context(), requestIdentityKey{}, id))
		w.Header().Set("X-Request-ID", id)
		w.Header().Set("Cache-Control", "no-store")
		if !strings.HasPrefix(r.URL.Path, "/api/v1/projects/") {
			start := time.Now()
			rw := &statusWriter{ResponseWriter: w, status: 200}
			w = rw
			s.Log.Info("request_started", "request_id", id, "method", r.Method)
			defer func() {
				s.Log.Info("request_completed", "request_id", id, "route", r.Pattern, "method", r.Method, "status", rw.status, "duration_ms", time.Since(start).Milliseconds())
			}()
		}
		w.Header().Set("X-Content-Type-Options", "nosniff")
		origin := r.Header.Get("Origin")
		if origin != "" {
			allowed := false
			for _, v := range s.Config.AllowedOrigins {
				allowed = allowed || v == origin
			}
			if !allowed {
				writeError(w, 403, "FORBIDDEN", requestID(r))
				return
			}
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Add("Vary", "Origin")
			w.Header().Set("Access-Control-Expose-Headers", "X-Request-ID, ETag")
		}
		if r.Method == "OPTIONS" {
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key, X-API-Key, If-None-Match")
			w.WriteHeader(204)
			return
		}
		s.mux.ServeHTTP(w, r)
	})
}

type requestIdentityKey struct{}

func requestID(r *http.Request) string {
	if id, ok := r.Context().Value(requestIdentityKey{}).(string); ok {
		return id
	}
	return asset.NewID()
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(status int) {
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}
func write(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func writeError(w http.ResponseWriter, status int, code, id string) {
	write(w, status, map[string]any{"error": map[string]any{"code": code, "message": messages[code], "request_id": id}})
}

var messages = map[string]string{"INVALID_ARGUMENT": "请求参数无效", "UNAUTHENTICATED": "认证无效或已过期", "FORBIDDEN": "缺少操作权限", "NOT_FOUND": "资源不存在", "CONFLICT": "命令或资源内容冲突", "ALREADY_EXISTS": "资源已存在", "PRECONDITION_FAILED": "当前状态不允许此操作", "OWNERSHIP_LOST": "领取已失效，请重新查看任务", "INTEGRITY_BLOCKED": "事实完整性阻止操作", "RATE_LIMITED": "请求过于频繁", "INTERNAL": "服务内部错误", "UNAVAILABLE": "依赖暂不可用"}

func mapError(err error) (int, string) {
	var connection *pgconn.ConnectError
	var pg *pgconn.PgError
	if errors.As(err, &connection) || errors.As(err, &pg) && (strings.HasPrefix(pg.Code, "08") || strings.HasPrefix(pg.Code, "53") || pg.Code == "57P01" || pg.Code == "57014") {
		return 503, "UNAVAILABLE"
	}
	switch {
	case errors.Is(err, identity.ErrUnauthenticated):
		return 401, "UNAUTHENTICATED"
	case errors.Is(err, asset.ErrForbidden):
		return 403, "FORBIDDEN"
	case errors.Is(err, asset.ErrNotFound):
		return 404, "NOT_FOUND"
	case errors.Is(err, rv.ErrOwnershipLost):
		return 409, "OWNERSHIP_LOST"
	case errors.Is(err, asset.ErrConflict):
		return 409, "CONFLICT"
	case errors.Is(err, asset.ErrInvalid):
		return 400, "INVALID_ARGUMENT"
	case errors.Is(err, asset.ErrUnsupported):
		return 412, "PRECONDITION_FAILED"
	case errors.Is(err, context.DeadlineExceeded):
		return 503, "UNAVAILABLE"
	}
	var e *commandError
	if errors.As(err, &e) {
		switch e.code {
		case ev.OwnershipLost:
			return 409, "OWNERSHIP_LOST"
		case ev.NotFound:
			return 404, "NOT_FOUND"
		case ev.Conflict:
			return 409, "CONFLICT"
		case ev.IntegrityBlocked:
			return 412, "INTEGRITY_BLOCKED"
		default:
			return 412, "PRECONDITION_FAILED"
		}
	}
	return 500, "INTERNAL"
}

type commandError struct{ code ev.Code }

func (e *commandError) Error() string { return string(e.code) }
func commandReply(r ev.Reply, err error) (any, error) {
	if err != nil {
		return nil, err
	}
	if r.Code != ev.Applied && r.Code != ev.AlreadyApplied {
		return nil, &commandError{r.Code}
	}
	return map[string]any{"id": r.ID, "status": r.Status, "command_status": r.Code}, nil
}

type rateEntry struct {
	start time.Time
	n     int
}
type rateLimiter struct {
	mu      sync.Mutex
	entries map[string]rateEntry
	max     int
}

func (l *rateLimiter) allow(id string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := time.Now()
	e := l.entries[id]
	if now.Sub(e.start) >= time.Minute {
		e = rateEntry{start: now}
	}
	if len(l.entries) >= 10000 {
		for key, v := range l.entries {
			if now.Sub(v.start) >= time.Minute {
				delete(l.entries, key)
			}
		}
		if _, ok := l.entries[id]; !ok && len(l.entries) >= 10000 {
			return false
		}
	}
	e.n++
	l.entries[id] = e
	return e.n <= l.max
}

type loginRequest struct {
	Password string `json:"password"`
}

func (s *Server) login(w http.ResponseWriter, r *http.Request) {
	id := requestID(r)
	w.Header().Set("X-Request-ID", id)
	if s.Dev == nil {
		writeError(w, 404, "NOT_FOUND", id)
		return
	}
	host, _, _ := net.SplitHostPort(r.RemoteAddr)
	if !s.limiter.allow("login:" + host) {
		writeError(w, 429, "RATE_LIMITED", id)
		return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 4096))
	var d loginRequest
	j, e := asset.ParseJSON(raw)
	if err != nil || e != nil || j.Decode(&d) != nil {
		writeError(w, 400, "INVALID_ARGUMENT", id)
		return
	}
	token, err := s.Dev.Login(d.Password)
	if err != nil {
		writeError(w, 401, "UNAUTHENTICATED", id)
		return
	}
	s.Log.Warn("CONTROLLED_DEV_AUTH", "request_id", id, "principal_id", s.Dev.PrincipalID)
	write(w, 200, map[string]any{"access_token": token, "expires_in": int(s.Dev.Lifetime.Seconds()), "auth_method": "CONTROLLED_DEV_AUTH"})
}
func (s *Server) me(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), s.Config.DBTimeout)
	defer cancel()
	p, err := s.Authenticate(ctx, r)
	if err != nil {
		status, code := mapError(err)
		s.Log.Info("auth_failed", "request_id", requestID(r))
		writeError(w, status, code, requestID(r))
		return
	}
	if !s.limiter.allow(p.ID) {
		s.Log.Info("rate_limited", "request_id", requestID(r), "principal_id", p.ID)
		writeError(w, 429, "RATE_LIMITED", requestID(r))
		return
	}
	write(w, 200, p)
}
func (s *Server) projects(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), s.Config.DBTimeout)
	defer cancel()
	p, err := s.Authenticate(ctx, r)
	if err != nil {
		status, code := mapError(err)
		s.Log.Info("auth_failed", "request_id", requestID(r))
		writeError(w, status, code, requestID(r))
		return
	}
	if !s.limiter.allow(p.ID) {
		s.Log.Info("rate_limited", "request_id", requestID(r), "principal_id", p.ID)
		writeError(w, 429, "RATE_LIMITED", requestID(r))
		return
	}
	after, n, err := (&request{server: s, HTTP: r, Access: identity.Access{Scope: asset.Scope{ProjectID: p.OrganizationID}}}).pagination("projects:" + p.ID)
	if err != nil {
		writeError(w, 400, "INVALID_ARGUMENT", requestID(r))
		return
	}
	v, err := s.Identity.ProjectPage(ctx, p, after, n+1)
	if err != nil {
		writeError(w, 500, "INTERNAL", requestID(r))
		return
	}
	var next *string
	if len(v) > n {
		next = s.encodeCursor(p.OrganizationID, "projects:"+p.ID, v[n-1].ID)
		v = v[:n]
	}
	write(w, 200, map[string]any{"items": v, "next_cursor": next})
}
