package main

import (
	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/httpapi"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	"context"
	"encoding/json"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	"net"
	"os"
	"os/signal"
	"syscall"
)

// metadataAPI 没有 Worker/Target；凭据仅返回给 operator 匿名管道。
func metadataAPI(parent context.Context, pool *pgxpool.Pool) {
	ctx, stop := signal.NotifyContext(parent, os.Interrupt, syscall.SIGTERM)
	defer stop()
	var epoch int64
	must(pool.QueryRow(ctx, "SELECT writer_epoch FROM evaluation_writer_control").Scan(&epoch))
	ident := postgres.ProductIdentity{Pool: pool, Pepper: []byte(os.Getenv("PRODUCT_API_PEPPER"))}
	ready := map[string]any{}
	for _, p := range []struct{ id, label string }{{holdoutProject, "holdout"}, {"6a9bffe4-98fa-5def-a6f5-28f66b64e09a", "development"}} {
		var org string
		must(pool.QueryRow(ctx, "SELECT org_id::text FROM projects WHERE id=$1", p.id).Scan(&org))
		a := identity.Access{Scope: asset.Scope{ProjectID: p.id, OrganizationID: org, Principal: "system:wp11-metadata"}, Principal: identity.Principal{ID: "system:wp11-metadata", Capabilities: []identity.Capability{identity.Read, identity.ManageAPIKey}}}
		_, key, e := ident.CreateCredential(ctx, a, asset.NewID(), "WP11 "+p.label, []identity.Capability{identity.Read}, nil)
		must(e)
		ready[p.label+"_api_key"] = key
	}
	cfg, e := pgxpool.ParseConfig(os.Getenv("STAGE13_WP11_API_DATABASE_URL"))
	must(e)
	if cfg.ConnConfig.Host != "127.0.0.1" || cfg.ConnConfig.Port != 55432 || cfg.ConnConfig.Database != "stage13_wp11_evalops_test" {
		panic("ISOLATED_API_REQUIRED")
	}
	apiPool, e := pgxpool.NewWithConfig(ctx, cfg)
	must(e)
	defer apiPool.Close()
	config := httpapi.DefaultConfig()
	api, e := httpapi.New(httpapi.Server{Pool: apiPool, Identity: postgres.ProductIdentity{Pool: apiPool, Pepper: ident.Pepper}, Config: config, Epoch: epoch, Kernel: postgres.Evaluation{Pool: apiPool}, SupportsEvaluator: agentquality.DeterministicSupported})
	must(e)
	_, e = api.Identity.VerifyAPI(ctx)
	must(e)
	listener, e := net.Listen("tcp", "127.0.0.1:0")
	must(e)
	ready["base_url"] = "http://" + listener.Addr().String()
	raw, e := json.Marshal(ready)
	must(e)
	fmt.Println(string(raw))
	must(httpapi.Serve(ctx, listener, api.Handler(), config.ShutdownTimeout))
}
