// stage13-delivery-probe 只读真实隔离 Gate，使用正式项目认证 HTTP handler。
package main

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/delivery"
	"agentevalops/go-backend/internal/httpapi"
	"agentevalops/go-backend/internal/postgres"
	"context"
	"github.com/jackc/pgx/v5/pgxpool"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

func must(e error) {
	if e != nil {
		panic(e)
	}
}
func main() {
	if os.Getenv("STAGE13_WP06_CONTROLLED") != "1" || len(os.Args) != 3 {
		panic("TEST_SCOPE_REQUIRED")
	}
	ctx := context.Background()
	cfg, e := pgxpool.ParseConfig(os.Getenv("STAGE13_WP06_DATABASE_URL"))
	must(e)
	if cfg.ConnConfig.Host != "127.0.0.1" || cfg.ConnConfig.Port != 55432 || cfg.ConnConfig.Database != "stage13_wp06_evalops_test" {
		panic("ISOLATED_DATABASE_REQUIRED")
	}
	cfg.ConnConfig.RuntimeParams["role"] = "stage13_wp06_reader"
	pool, e := pgxpool.NewWithConfig(ctx, cfg)
	must(e)
	defer pool.Close()
	scope := asset.Scope{ProjectID: "71300005-0000-4000-8000-000000000001", OrganizationID: "71300005-0000-4000-8000-000000000002", Principal: "system:wp06-consumer"}
	k := postgres.Decisions{Pool: pool}
	if os.Args[1] == "prepare" {
		gateID := "e7ffaf81-3660-475a-929c-cb816676290c"
		r, e := k.GetGateReceipt(ctx, scope, gateID)
		must(e)
		for _, u := range r.Candidate.Units {
			valid := false
			if r.Stage13 != nil {
				for _, c := range r.Stage13.Cases {
					if c.CaseID == u.CaseID && c.Candidate.SchemaValid && c.Candidate.SemanticValid {
						valid = true
					}
				}
			}
			if !valid {
				continue
			}
			g, o, _, e := k.Stage13DeliveryBinding(ctx, scope, gateID, u.AttemptID)
			if e != nil {
				continue
			}
			q := delivery.Request{Gate: g, Outcome: o, Destination: delivery.Destination{Kind: delivery.SinkKind, Scope: "TEST_SCOPE", ID: "wp06-current-candidate"}}
			a, e := k.AuthorizeStage13Delivery(ctx, scope, q)
			must(e)
			for name, v := range map[string]any{"current-blocked-gate": r, "current-request": q, "delivery-authorization": a} {
				j, e := asset.Freeze(v)
				must(e)
				must(os.WriteFile(filepath.Join(os.Args[2], name+".json"), j.Bytes(), 0600))
			}
			return
		}
		panic("NO_BINDABLE_CURRENT_OUTCOME")
	}
	if os.Args[1] != "serve" {
		panic("INVALID_MODE")
	}
	api, e := httpapi.New(httpapi.Server{Pool: pool, Identity: postgres.ProductIdentity{Pool: pool, Pepper: []byte(os.Getenv("PRODUCT_API_PEPPER"))}, Config: httpapi.DefaultConfig()})
	must(e)
	server := &http.Server{Addr: os.Args[2], Handler: api.Handler(), ReadHeaderTimeout: 5 * time.Second}
	must(server.ListenAndServe())
}
