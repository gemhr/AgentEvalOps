// evalgate 只提交 canonical refs 并读取持久 Receipt；不执行部署。
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"io"
	"log/slog"
	"os"
	"strings"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/postgres"
	"github.com/jackc/pgx/v5/pgxpool"
)

func source(raw string) (decision.SourceRef, error) {
	var r decision.SourceRef
	switch {
	case strings.HasPrefix(raw, "{"):
		j, e := asset.ParseJSON([]byte(raw))
		if e != nil {
			return r, asset.ErrInvalid
		}
		d := json.NewDecoder(bytes.NewReader(j.Bytes()))
		d.DisallowUnknownFields()
		if e = d.Decode(&r); e != nil {
			return r, asset.ErrInvalid
		}
	case strings.HasPrefix(raw, "experiment:"):
		r = decision.SourceRef{Kind: "EXPERIMENT", Experiment: strings.TrimPrefix(raw, "experiment:")}
	default:
		r = decision.SourceRef{Kind: "RUN_SET", Runs: strings.Split(strings.TrimPrefix(raw, "run:"), ",")}
	}
	return r, r.Validate()
}
func execute(args []string, out, errOut io.Writer) int {
	f := flag.NewFlagSet("evalgate", flag.ContinueOnError)
	f.SetOutput(errOut)
	project := f.String("project", "", "精确 project UUID")
	baseline := f.String("baseline", "", "run:UUID[,UUID] / experiment:UUID / ONLINE SourceRef JSON")
	candidate := f.String("candidate", "", "精确 candidate source refs")
	policy := f.String("policy", "", "UUID@version")
	gate := f.String("gate", "", "可选 stable command UUID；省略则从完整意图派生")
	read := f.Bool("read", false, "只读取 --gate 的 durable receipt")
	if f.Parse(args) != nil {
		return 3
	}
	internal := func(category string) int {
		_ = json.NewEncoder(out).Encode(map[string]string{"decision": "INTERNAL_ERROR", "reason_code": category})
		return 3
	}
	if !asset.ValidID(*project) || !asset.Text(os.Getenv("EVALGATE_PRINCIPAL")) || os.Getenv("DATABASE_URL") == "" {
		return internal("CONFIGURATION_REQUIRED")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, e := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	if e != nil {
		return internal("DATABASE_CONFIGURATION_ERROR")
	}
	defer pool.Close()
	kernel := postgres.Evaluation{Pool: pool}
	epoch, e := kernel.VerifyWorker(ctx, decision.Schema)
	if e != nil {
		return internal("SCHEMA_WRITER_ROLE_REJECTED")
	}
	s, e := kernel.WorkerScope(ctx, *project, os.Getenv("EVALGATE_PRINCIPAL"), epoch)
	if e != nil {
		return internal("PROJECT_SCOPE_REJECTED")
	}
	s.Create = true
	k := postgres.Decisions{Pool: pool}
	var receipt decision.Receipt
	if *read {
		if !asset.ValidID(*gate) {
			return internal("INVALID_GATE_REF")
		}
		receipt, e = k.GetGateReceipt(ctx, s.Scope, *gate)
	} else {
		b, err := source(*baseline)
		if err != nil {
			return internal("INVALID_BASELINE_REF")
		}
		c, err := source(*candidate)
		if err != nil {
			return internal("INVALID_CANDIDATE_REF")
		}
		id, v, ok := strings.Cut(*policy, "@")
		if !ok {
			return internal("INVALID_POLICY_REF")
		}
		cmd := decision.Command{ID: *gate, Baseline: b, Candidate: c, Policy: asset.Ref{EntityID: id, Version: v}}
		if cmd.ID == "" {
			cmd.ID = decision.IntentID(s.ProjectID, s.Principal, cmd)
		}
		receipt, e = k.CreateGate(ctx, s, cmd)
	}
	if e != nil {
		return internal("GATE_COMMAND_ERROR")
	}
	if json.NewEncoder(out).Encode(receipt) != nil {
		return 3
	}
	return decision.ExitCode(receipt.Decision)
}
func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	os.Exit(execute(os.Args[1:], os.Stdout, os.Stderr))
}
