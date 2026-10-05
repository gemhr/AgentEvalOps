//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/httpapi"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"
)

type g8ChildConfig struct {
	URL, Role string
	Dev       identity.DevAuth
	Pepper    []byte
}

// 子进程真实启动同一 HTTP adapter；仅测试 binary 通过 stdin 触发跨平台退出。
func TestG8APIProcessChild(t *testing.T) {
	raw := os.Getenv("G8_API_CHILD")
	if raw == "" {
		t.Skip("child entry point")
	}
	var c g8ChildConfig
	mustG7(t, json.Unmarshal([]byte(raw), &c))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	config, e := pgxpool.ParseConfig(c.URL)
	mustG7(t, e)
	config.MaxConns = 12
	config.AfterConnect = func(ctx context.Context, conn *pgx.Conn) error {
		_, e := conn.Exec(ctx, "SET ROLE "+pgx.Identifier{c.Role}.Sanitize())
		return e
	}
	pool, e := pgxpool.NewWithConfig(ctx, config)
	mustG7(t, e)
	defer pool.Close()
	ident := postgres.ProductIdentity{Pool: pool, Pepper: c.Pepper}
	epoch, e := ident.VerifyAPI(ctx)
	mustG7(t, e)
	s, e := httpapi.New(httpapi.Server{Pool: pool, Identity: ident, Dev: &c.Dev, Config: httpapi.DefaultConfig(), Epoch: epoch, Kernel: postgres.Evaluation{Pool: pool}, Log: slog.New(slog.NewJSONHandler(io.Discard, nil))})
	mustG7(t, e)
	l, e := net.Listen("tcp", "127.0.0.1:0")
	mustG7(t, e)
	os.Stdout.WriteString("G8_READY http://" + l.Addr().String() + "\n")
	go func() { scanner := bufio.NewScanner(os.Stdin); scanner.Scan(); cancel() }()
	mustG7(t, httpapi.Serve(ctx, l, s.Handler(), time.Second))
}

type g8Process struct {
	cmd    *exec.Cmd
	input  io.WriteCloser
	url    string
	output *bytes.Buffer
	exited bool
}

func startG8Process(t *testing.T, x *g8Fixture, dev identity.DevAuth) *g8Process {
	t.Helper()
	exe, e := os.Executable()
	mustG7(t, e)
	raw, _ := json.Marshal(g8ChildConfig{URL: x.db.url.String(), Role: x.role, Dev: dev, Pepper: x.api.Identity.Pepper})
	p := &g8Process{cmd: exec.Command(exe, "-test.run=^TestG8APIProcessChild$", "-test.v"), output: &bytes.Buffer{}}
	p.cmd.Env = append(os.Environ(), "G8_API_CHILD="+string(raw))
	p.cmd.Stderr = p.output
	p.input, e = p.cmd.StdinPipe()
	mustG7(t, e)
	pipe, e := p.cmd.StdoutPipe()
	mustG7(t, e)
	mustG7(t, p.cmd.Start())
	ready := make(chan string, 1)
	go func() {
		scanner := bufio.NewScanner(pipe)
		for scanner.Scan() {
			if strings.HasPrefix(scanner.Text(), "G8_READY ") {
				ready <- strings.TrimPrefix(scanner.Text(), "G8_READY ")
				return
			}
		}
		ready <- ""
	}()
	select {
	case p.url = <-ready:
		if p.url == "" {
			t.Fatal("API child bootstrap failed")
		}
	case <-time.After(15 * time.Second):
		p.cmd.Process.Kill()
		p.cmd.Wait()
		t.Fatal("API child startup timeout")
	}
	t.Cleanup(func() {
		if !p.exited {
			p.input.Close()
			if e := p.cmd.Wait(); e != nil {
				t.Error("child shutdown failed")
			}
			p.exited = true
		}
	})
	return p
}
func processRequest(t *testing.T, p *g8Process, method, path, key, header, value string, body any, status int) map[string]any {
	t.Helper()
	raw, _ := json.Marshal(body)
	req, e := http.NewRequest(method, p.url+path, bytes.NewReader(raw))
	mustG7(t, e)
	if header != "" {
		req.Header.Set(header, value)
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
		t.Fatalf("multi-process HTTP %d want %d", res.StatusCode, status)
	}
	var v map[string]any
	mustG7(t, json.Unmarshal(b, &v))
	return v
}
func TestG8APIProcesses(t *testing.T) {
	x := g8(t)
	dev := identity.DevAuth{Environment: "test", PrincipalID: x.principals["judge"].ID, OrganizationID: x.s.OrganizationID, Password: strings.Repeat("controlled-password", 2), SigningKey: []byte(strings.Repeat("controlled-signing", 3)), Lifetime: time.Minute}
	a, b := startG8Process(t, x, dev), startG8Process(t, x, dev)
	token, e := dev.Login(dev.Password)
	mustG7(t, e)
	path := "/api/v1/projects/" + x.s.ProjectID + "/cases"
	out := make([]map[string]any, 2)
	var wg sync.WaitGroup
	for i, p := range []*g8Process{a, b} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			out[i] = processRequest(t, p, "POST", path, "process-shared", "Authorization", "Bearer "+token, map[string]any{"name": "multi-process canonical"}, 200)
		}()
	}
	wg.Wait()
	if out[0]["id"] != out[1]["id"] {
		t.Fatal("A01 duplicate fact")
	}
	var count int
	mustG7(t, x.db.pool.QueryRow(context.Background(), `SELECT count(*) FROM evaluation_cases WHERE id=$1`, out[0]["id"]).Scan(&count))
	if count != 1 {
		t.Fatal("A01 canonical count")
	}
	t.Log("A01 PASS two processes -> one canonical fact")
	mustG7(t, a.cmd.Process.Kill())
	_ = a.cmd.Wait()
	a.exited = true
	replay := processRequest(t, b, "POST", path, "process-shared", "Authorization", "Bearer "+token, map[string]any{"name": "multi-process canonical"}, 200)
	if replay["id"] != out[0]["id"] {
		t.Fatal("A02 lost durable fact")
	}
	t.Log("A02 PASS kill after durable command -> original fact")
	keyPath := "/api/v1/projects/" + x.s.ProjectID + "/api-keys"
	created := processRequest(t, b, "POST", keyPath, "process-key", "Authorization", "Bearer "+token, map[string]any{"name": "cross-process revoke", "capabilities": []string{"READ"}}, 200)
	secret := created["secret"].(string)
	keyID := created["credential"].(map[string]any)["id"].(string)
	second := startG8Process(t, x, dev)
	processRequest(t, second, "GET", path, "", "X-API-Key", secret, nil, 200)
	processRequest(t, b, "POST", keyPath+"/"+keyID+"/revoke", "", "Authorization", "Bearer "+token, map[string]any{}, 200)
	processRequest(t, second, "GET", path, "", "X-API-Key", secret, nil, 401)
	t.Log("A04 PASS second process rejects immediately; no credential cache")
	b.input.Close()
	mustG7(t, b.cmd.Wait())
	b.exited = true
	if conn, e := net.DialTimeout("tcp", strings.TrimPrefix(b.url, "http://"), time.Second); e == nil {
		conn.Close()
		t.Fatal("A03 listener remains")
	}
	t.Log("A03 PASS controlled stop closes actual process listener; bounded in-flight covered by lifecycle test")
}
