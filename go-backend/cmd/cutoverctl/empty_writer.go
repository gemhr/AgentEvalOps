package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"agentevalops/go-backend/internal/postgres"
	"github.com/jackc/pgx/v5/pgxpool"
)

// sourceDigest 仅哈希源码；不读取 .env、凭据、运行目录或 Handoff。
func sourceDigest(root string, migrations bool) (string, error) {
	var paths []string
	sub := "go-backend"
	if migrations {
		sub = "backend/migrations/versions"
	}
	base := filepath.Join(root, sub)
	err := filepath.WalkDir(base, func(path string, d os.DirEntry, e error) error {
		if e != nil {
			return e
		}
		if d.IsDir() {
			if d.Name() == ".git" || strings.HasPrefix(d.Name(), ".e2e-") {
				return filepath.SkipDir
			}
			return nil
		}
		if d.Type()&os.ModeSymlink != 0 {
			return nil
		}
		if migrations && strings.HasSuffix(path, ".py") || !migrations && (strings.HasSuffix(path, ".go") || d.Name() == "go.mod" || d.Name() == "go.sum") {
			paths = append(paths, path)
		}
		return nil
	})
	if err != nil {
		return "", err
	}
	sort.Strings(paths)
	h := sha256.New()
	for _, path := range paths {
		data, e := os.ReadFile(path)
		if e != nil {
			return "", e
		}
		relative, e := filepath.Rel(root, path)
		if e != nil {
			return "", e
		}
		sum := sha256.Sum256(data)
		h.Write([]byte(filepath.ToSlash(relative) + "\x00" + hex.EncodeToString(sum[:]) + "\n"))
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func verifyEmptyDocker(ctx context.Context, i postgres.EmptyWriterIdentity) bool {
	raw, e := exec.CommandContext(ctx, "docker", "inspect", i.ContainerID).Output()
	if e != nil || len(raw) > 1024*1024 {
		return false
	}
	var containers []struct {
		ID              string
		Name            string
		Config          struct{ Labels map[string]string }
		State           struct{ Running bool }
		NetworkSettings struct {
			Ports map[string][]struct {
				HostIP   string `json:"HostIp"`
				HostPort string
			}
		}
		Mounts []struct{ Type, Name, Destination string }
	}
	if json.Unmarshal(raw, &containers) != nil || len(containers) != 1 {
		return false
	}
	c := containers[0]
	ports := c.NetworkSettings.Ports["5432/tcp"]
	if c.ID != i.ContainerID || c.Name != "/ci-e2e-eopg-"+i.TaskID || !c.State.Running || c.Config.Labels["ci-e2e-owner"] != i.TaskID || len(ports) != 1 || ports[0].HostIP != "127.0.0.1" || ports[0].HostPort != "55435" {
		return false
	}
	mounted := false
	for _, m := range c.Mounts {
		if m.Type == "volume" && m.Name == i.Volume && m.Destination == "/var/lib/postgresql/data" {
			mounted = true
		}
	}
	if !mounted {
		return false
	}
	raw, e = exec.CommandContext(ctx, "docker", "volume", "inspect", i.Volume).Output()
	if e != nil {
		return false
	}
	var volumes []struct {
		Name   string
		Labels map[string]string
	}
	return json.Unmarshal(raw, &volumes) == nil && len(volumes) == 1 && volumes[0].Name == i.Volume && volumes[0].Labels["ci-e2e-owner"] == i.TaskID
}

func initializeEmptyWriter(controlled, execute bool, path, receipt, root string) int {
	if !controlled || path == "" || receipt == "" {
		return safeFailure("EMPTY_WRITER_EXPLICIT_CONFIGURATION_REQUIRED")
	}
	f, e := os.Open(path)
	if e != nil {
		return safeFailure("EMPTY_WRITER_IDENTITY_UNAVAILABLE")
	}
	defer f.Close()
	var i postgres.EmptyWriterIdentity
	dec := json.NewDecoder(io.LimitReader(f, 16385))
	dec.DisallowUnknownFields()
	if dec.Decode(&i) != nil || dec.Decode(new(any)) != io.EOF || i.Validate() != nil || i.Port != 55435 {
		return safeFailure("EMPTY_WRITER_IDENTITY_INVALID")
	}
	cfg, e := pgxpool.ParseConfig(os.Getenv("DATABASE_URL"))
	if e != nil || cfg.ConnConfig.Host != i.Host || cfg.ConnConfig.Port != i.Port || cfg.ConnConfig.Database != i.Database {
		return safeFailure("EMPTY_WRITER_TARGET_REJECTED")
	}
	for _, fallback := range cfg.ConnConfig.Fallbacks {
		if fallback.Host != i.Host || fallback.Port != i.Port {
			return safeFailure("EMPTY_WRITER_TARGET_REJECTED")
		}
	}
	migration, e := sourceDigest(root, true)
	if e != nil || migration != i.MigrationDigest {
		return safeFailure("EMPTY_WRITER_MIGRATION_SOURCE_MISMATCH")
	}
	code, e := sourceDigest(root, false)
	if e != nil || code != i.CodeDigest {
		return safeFailure("EMPTY_WRITER_CODE_SOURCE_MISMATCH")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if !verifyEmptyDocker(ctx, i) {
		return safeFailure("EMPTY_WRITER_DOCKER_IDENTITY_REJECTED")
	}
	// 在状态修改前独占新建回执；不覆盖既有操作，提交不明只读查询。
	out, e := os.OpenFile(receipt, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return safeFailure("EMPTY_WRITER_NEW_RECEIPT_REQUIRED")
	}
	defer out.Close()
	cfg.MaxConns = 1
	p, e := pgxpool.NewWithConfig(ctx, cfg)
	if e != nil {
		return safeFailure("EMPTY_WRITER_CONNECTION_REJECTED")
	}
	defer p.Close()
	result, err := (postgres.Cutover{Pool: p}).InitializeEmptyGo(ctx, i, execute)
	if json.NewEncoder(out).Encode(result) != nil || out.Sync() != nil {
		return safeFailure("EMPTY_WRITER_RECEIPT_WRITE_FAILED_QUERY_STATUS")
	}
	output(result)
	if err != nil {
		return safeFailure("EMPTY_WRITER_PREFLIGHT_OR_COMMIT_REJECTED_QUERY_STATUS")
	}
	return 0
}
