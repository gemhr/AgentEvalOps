package main

import (
	"github.com/jackc/pgx/v5/pgxpool"
	"io"
	"os"
	"strings"
	"testing"
)

func TestProductionAndUnsupportedAbortRejectBeforeConnection(t *testing.T) {
	t.Setenv("DATABASE_URL", "postgres://fixture:must-never-appear@production.invalid/production")
	oldArgs := os.Args
	defer func() { os.Args = oldArgs }()
	for _, args := range [][]string{{"cutoverctl", "--production", "--command", "activate-go", "--execute"}, {"cutoverctl", "--command", "abort-before-go-write"}, {"cutoverctl", "--command", "status"}} {
		read, write, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		oldOut := os.Stdout
		os.Stdout = write
		os.Args = args
		code := run()
		os.Stdout = oldOut
		write.Close()
		raw, _ := io.ReadAll(read)
		read.Close()
		if code == 0 || strings.Contains(string(raw), "must-never-appear") || strings.Contains(string(raw), "production.invalid") {
			t.Fatal("unsafe boundary", code, string(raw))
		}
	}
}

func TestActualPGXTargetCannotOverrideIsolatedURL(t *testing.T) {
	base := "postgres://fixture:fixture-only@127.0.0.1:55432/agentevalops_g1_00000000000000000000000000000001"
	for _, query := range []string{"", "?host=production.invalid", "?port=5432", "?dbname=production", "?host=127.0.0.1,production.invalid"} {
		cfg, err := pgxpool.ParseConfig(base + query)
		if err != nil {
			t.Fatal(err)
		}
		if isolatedConfig(cfg) != (query == "") {
			t.Fatal("unsafe actual target accepted", query)
		}
	}
}
