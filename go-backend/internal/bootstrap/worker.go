// Package bootstrap 负责显式装配；fixture 模式必须由 caller 主动启用。
package bootstrap

import (
	"context"
	"fmt"
	"log/slog"

	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/worker"
	"github.com/jackc/pgx/v5/pgxpool"
)

const WorkerSchema = "c12a00300001"

func Worker(ctx context.Context, url string, config worker.Config, fixtureOnly bool, log *slog.Logger) (*worker.Runtime, func(), error) {
	if !fixtureOnly {
		return nil, nil, fmt.Errorf("G3 仅支持显式 FIXTURE_ONLY；真实 target/evaluator 尚未装配")
	}
	if url == "" {
		return nil, nil, fmt.Errorf("缺少 DATABASE_URL")
	}
	if err := config.Validate(); err != nil {
		return nil, nil, err
	}
	poolConfig, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, nil, fmt.Errorf("非法 DATABASE_URL")
	}
	poolConfig.MaxConns = int32(config.ExecutionConcurrency + config.EvaluatorConcurrency + 8)
	pool, err := pgxpool.NewWithConfig(ctx, poolConfig)
	if err != nil {
		return nil, nil, fmt.Errorf("数据库连接配置失败")
	}
	k := postgres.Evaluation{Pool: pool}
	epoch, err := k.VerifyWorker(ctx, WorkerSchema)
	if err != nil {
		pool.Close()
		return nil, nil, err
	}
	caps, err := k.LoadEvaluatorCapabilities(ctx, worker.FixtureDefinitionSupported)
	if err != nil {
		pool.Close()
		return nil, nil, err
	}
	k.Capabilities = caps
	r := &worker.Runtime{Config: config, Backend: k, Target: worker.FixtureExecutionTarget{}, Evaluator: worker.FixtureEvaluator{DBTimeout: config.DBTimeout}, Epoch: epoch, Log: log}
	return r, pool.Close, nil
}
