package bootstrap

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"time"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/httpapi"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"github.com/jackc/pgx/v5/pgxpool"
)

func API(ctx context.Context, config httpapi.Config, controlledDev, fixtureOnly bool, log *slog.Logger) (*httpapi.Server, func(), error) {
	environment := os.Getenv("APP_ENV")
	if log == nil {
		log = slog.Default()
	}
	if os.Getenv("DATABASE_URL") == "" || len(os.Getenv("PRODUCT_API_PEPPER")) < 32 {
		return nil, nil, fmt.Errorf("API_SECRET_CONFIG_REJECTED")
	}
	if (controlledDev || fixtureOnly) && environment != "test" && environment != "development" {
		return nil, nil, fmt.Errorf("CONTROLLED_MODE_PRODUCTION_FORBIDDEN")
	}
	c, err := pgxpool.ParseConfig(os.Getenv("DATABASE_URL"))
	if err != nil {
		return nil, nil, fmt.Errorf("DATABASE_CONFIG_REJECTED")
	}
	c.MaxConns = 24
	pool, err := pgxpool.NewWithConfig(ctx, c)
	if err != nil {
		return nil, nil, fmt.Errorf("DATABASE_CONFIG_REJECTED")
	}
	closeDB := func() { pool.Close() }
	ident := postgres.ProductIdentity{Pool: pool, Pepper: []byte(os.Getenv("PRODUCT_API_PEPPER"))}
	epoch, err := ident.VerifyAPI(ctx)
	if err != nil {
		closeDB()
		return nil, nil, err
	}
	k := postgres.Evaluation{Pool: pool}
	httpConfig := provider.DefaultHTTPConfig()
	if raw := os.Getenv("JUDGE_HTTP_CONFIG"); raw != "" {
		j, e := asset.ParseJSON([]byte(raw))
		if e != nil || j.Decode(&httpConfig) != nil {
			closeDB()
			return nil, nil, asset.ErrInvalid
		}
	}
	supported := func(d metric.EvaluatorDefinition) bool {
		if agentquality.DeterministicSupported(d) {
			return true
		}
		var c provider.JudgeConfig
		return provider.JudgeDefinitionSupported(d) && d.Config.Decode(&c) == nil && c.HTTP == httpConfig
	}
	if fixtureOnly {
		supported = worker.FixtureDefinitionSupported
	}
	k.Capabilities, err = k.LoadEvaluatorCapabilities(ctx, supported)
	if err != nil {
		closeDB()
		return nil, nil, fmt.Errorf("CAPABILITY_CONFIG_REJECTED")
	}
	s := httpapi.Server{Pool: pool, Identity: ident, Config: config, Epoch: epoch, Kernel: k, SupportsEvaluator: supported, Log: log}
	if controlledDev {
		s.Dev = &identity.DevAuth{Environment: environment, PrincipalID: os.Getenv("PRODUCT_DEV_PRINCIPAL_ID"), OrganizationID: os.Getenv("PRODUCT_DEV_ORGANIZATION_ID"), Password: os.Getenv("PRODUCT_DEV_PASSWORD"), SigningKey: []byte(os.Getenv("PRODUCT_DEV_SIGNING_KEY")), Lifetime: 15 * time.Minute}
		log.Warn("CONTROLLED_DEV_AUTH", "environment", environment)
	}
	api, err := httpapi.New(s)
	if err != nil {
		closeDB()
		return nil, nil, fmt.Errorf("API_CONFIG_REJECTED")
	}
	return api, closeDB, nil
}
