// cutoverctl 是离线 operator 工具；所有状态修改默认 dry-run。
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"regexp"
	"time"

	"agentevalops/go-backend/internal/buildinfo"
	"agentevalops/go-backend/internal/cutover"
	"agentevalops/go-backend/internal/postgres"
	"github.com/jackc/pgx/v5/pgxpool"
)

func main() {
	if buildinfo.Requested("cutoverctl") {
		return
	}
	os.Exit(run())
}
func output(v any) { _ = json.NewEncoder(os.Stdout).Encode(v) }
func run() int {
	f := flag.NewFlagSet("cutoverctl", flag.ContinueOnError)
	command := f.String("command", "status", "status/preflight/verify/enter-drain/enter-barrier/activate-go/abort-before-go-write")
	execute := f.Bool("execute", false, "显式执行；省略时仅 dry-run")
	controlled := f.Bool("controlled", false, "仅允许本机隔离数据库")
	production := f.Bool("production", false, "G10A 生产操作保持 BLOCKED")
	evidence := f.String("evidence", "", "operator 审核后的 JSON 证据")
	manifest := f.String("manifest", "", "固定 release manifest")
	root := f.String("repo-root", "..", "核对 release source 的仓库路径")
	receipt := f.String("receipt", "", "新建不可覆盖的外部 receipt 文件")
	if f.Parse(os.Args[1:]) != nil || f.NArg() != 0 {
		return 3
	}
	if *production {
		output(map[string]string{"status": "BLOCKED_BY_REAL_WORLD_CONTEXT", "reason": "PRODUCTION_OPERATOR_AUTHORIZATION_NOT_IMPLEMENTED"})
		return 2
	}
	if *command == "abort-before-go-write" {
		output(map[string]string{"status": "BLOCKED", "reason": "ROLLBACK_REQUIRES_RESTORE_OR_FORWARD_FIX"})
		return 2
	}
	mutating := *command == "enter-drain" || *command == "enter-barrier" || *command == "activate-go"
	if !mutating && *command != "status" && *command != "preflight" && *command != "verify" {
		output(map[string]string{"status": "INVALID_COMMAND"})
		return 3
	}
	var e cutover.Evidence
	if *evidence != "" {
		file, err := os.Open(*evidence)
		if err != nil {
			return safeFailure("EVIDENCE_UNAVAILABLE")
		}
		defer file.Close()
		d := json.NewDecoder(io.LimitReader(file, 4<<20))
		d.DisallowUnknownFields()
		if d.Decode(&e) != nil || d.Decode(new(any)) != io.EOF {
			return safeFailure("EVIDENCE_INVALID")
		}
	}
	// 不读取 .env；先限定 host/port/database，再建立任何连接。
	u, err := url.Parse(os.Getenv("DATABASE_URL"))
	if err != nil || !*controlled || (u.Scheme != "postgres" && u.Scheme != "postgresql") || (u.Hostname() != "127.0.0.1" && u.Hostname() != "localhost") || u.Port() != "55432" || !regexp.MustCompile(`^/(agentevalops_g1_|agentevalops_g10_)[0-9a-f]{32}$`).MatchString(u.Path) {
		return safeFailure("EXPLICIT_ISOLATED_DATABASE_REQUIRED")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cfg, err := pgxpool.ParseConfig(u.String())
	if err != nil || !isolatedConfig(cfg) {
		return safeFailure("EXPLICIT_ISOLATED_DATABASE_REQUIRED")
	}
	p, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return safeFailure("DATABASE_UNAVAILABLE")
	}
	defer p.Close()
	k := postgres.Cutover{Pool: p}
	s, err := k.Status(ctx)
	if err != nil {
		return safeFailure("STATUS_UNAVAILABLE")
	}
	if *command == "status" {
		output(s)
		return 0
	}
	r, err := k.Preflight(ctx, e, true)
	if err != nil {
		return safeFailure("PREFLIGHT_UNAVAILABLE")
	}
	releaseOK := true
	for _, c := range cutover.VerifyRelease(*manifest, *root, e.ReleaseDigest, true) {
		r.Checks = append(r.Checks, c)
		if c.Status != "PASS" {
			r.Status = "BLOCKED"
			releaseOK = false
		}
	}
	if !mutating {
		output(r)
		if r.Status == "PASS" {
			return 0
		}
		return 2
	}
	n, err := cutover.Next(s, *command)
	if err != nil {
		return safeFailure("INVALID_WRITER_TRANSITION")
	}
	if !*execute {
		output(map[string]any{"status": "DRY_RUN", "from": s, "to": n, "preflight": r})
		return 0
	}
	if !releaseOK {
		output(r)
		return 2
	}
	if *command == "enter-drain" {
		if e.Checks["admission_stopped"] != "VERIFIED" || e.Checks["beat_stopped"] != "VERIFIED" {
			output(r)
			return 2
		}
	} else if r.Status != "PASS" {
		output(r)
		return 2
	}
	if *receipt == "" {
		return safeFailure("IMMUTABLE_RECEIPT_PATH_REQUIRED")
	}
	file, err := os.OpenFile(*receipt, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return safeFailure("RECEIPT_ALREADY_EXISTS_OR_UNAVAILABLE")
	}
	defer file.Close()
	n, err = k.Transition(ctx, *command, e)
	decision := "APPLIED"
	if err != nil {
		decision = "BLOCKED_OR_COMMIT_UNKNOWN_QUERY_STATUS"
	}
	record := map[string]any{"scope": s.Scope, "cutover_id": e.CutoverID, "from": s, "to": n, "operator": e.Operator, "timestamp": time.Now().UTC(), "release_manifest_digest": e.ReleaseDigest, "consumer_checklist_digest": e.ConsumerDigest, "backup_evidence_ref": e.BackupRef, "decision": decision, "blockers": r.Checks}
	if json.NewEncoder(file).Encode(record) != nil || file.Sync() != nil {
		return safeFailure("RECEIPT_WRITE_FAILED_QUERY_STATUS")
	}
	output(record)
	if err != nil {
		return 2
	}
	return 0
}

// isolatedConfig 检查 pgx 实际目标，避免 query/environment 覆盖 URL authority。
func isolatedConfig(cfg *pgxpool.Config) bool {
	c := cfg.ConnConfig
	local := func(host string, port uint16) bool {
		return (host == "127.0.0.1" || host == "localhost") && port == 55432
	}
	if !local(c.Host, c.Port) || !regexp.MustCompile(`^(agentevalops_g1_|agentevalops_g10_)[0-9a-f]{32}$`).MatchString(c.Database) {
		return false
	}
	for _, fallback := range c.Fallbacks {
		if !local(fallback.Host, fallback.Port) {
			return false
		}
	}
	return true
}
func safeFailure(reason string) int {
	output(map[string]string{"status": "BLOCKED", "reason": reason})
	fmt.Fprintln(os.Stderr, "cutoverctl: "+reason)
	return 3
}
