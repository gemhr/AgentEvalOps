// Package bootstrap 负责显式装配；fixture 模式必须由 caller 主动启用。
package bootstrap

import (
	"context"
	"fmt"
	"log/slog"
	"os"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/provider"

	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/worker"
	"github.com/jackc/pgx/v5/pgxpool"
)

const WorkerSchema = "c12a00600001"

func Worker(ctx context.Context, url string, config worker.Config, fixtureOnly bool, log *slog.Logger) (*worker.Runtime, func(), error) {
	if log == nil {
		log = slog.Default()
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
	poolConfig.MaxConns = int32(config.ExecutionConcurrency + config.EvaluatorConcurrency + config.OnlineConcurrency + 12)
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
	var target worker.ExecutionTarget = worker.FixtureExecutionTarget{}
	var evaluator worker.Evaluator = worker.FixtureEvaluator{DBTimeout: config.DBTimeout}
	supported := worker.FixtureDefinitionSupported
	closeAdapters := func() {}
	var onlineJudge *provider.LLMJudgeEvaluator
	if !fixtureOnly {
		var targetConfig provider.LocalAgentConfig
		httpConfig := provider.DefaultHTTPConfig()
		if raw := os.Getenv("JUDGE_HTTP_CONFIG"); raw != "" {
			j, e := asset.ParseJSON([]byte(raw))
			if e != nil || j.Decode(&httpConfig) != nil {
				pool.Close()
				return nil, nil, asset.ErrInvalid
			}
		}
		judge, e := provider.NewLLMJudgeEvaluator(httpConfig, config.DBTimeout, log)
		if e != nil {
			pool.Close()
			return nil, nil, e
		}
		onlineJudge = judge
		var local *provider.LocalAgentHttpExecutionTarget
		if config.ExecutionConcurrency > 0 {
			j, e := asset.ParseJSON([]byte(os.Getenv("LOCALAGENT_TARGET_CONFIG")))
			if e != nil || j.Decode(&targetConfig) != nil {
				judge.Close()
				pool.Close()
				return nil, nil, fmt.Errorf("缺少有效 LOCALAGENT_TARGET_CONFIG")
			}
			local, e = provider.NewLocalAgentHttpExecutionTarget(targetConfig, log)
			if e != nil {
				judge.Close()
				pool.Close()
				return nil, nil, e
			}
			target = local
		} else {
			target = nil
		}
		evaluator = qualityEvaluator{Judge: judge}
		supported = func(d metric.EvaluatorDefinition) bool {
			if agentquality.DeterministicSupported(d) {
				return true
			}
			var c provider.JudgeConfig
			return provider.JudgeDefinitionSupported(d) && d.Config.Decode(&c) == nil && c.HTTP == httpConfig
		}
		closeAdapters = func() {
			judge.Close()
			if local != nil {
				local.Close()
			}
		}
	}
	caps, err := k.LoadEvaluatorCapabilities(ctx, supported)
	if err != nil {
		closeAdapters()
		pool.Close()
		return nil, nil, err
	}
	k.Capabilities = caps
	r := &worker.Runtime{Config: config, Backend: k, Target: target, Evaluator: evaluator, Epoch: epoch, Log: log}
	if config.OnlineConcurrency > 0 {
		if onlineJudge == nil {
			onlineJudge, err = provider.NewLLMJudgeEvaluator(provider.DefaultHTTPConfig(), config.DBTimeout, log)
			if err != nil {
				closeAdapters()
				pool.Close()
				return nil, nil, err
			}
			previous := closeAdapters
			closeAdapters = func() { previous(); onlineJudge.Close() }
		}
		store := postgres.Online{Pool: pool}
		r.Online = &worker.OnlineRuntime{Config: config, Store: store, Epoch: epoch, Log: log, Evaluator: provider.OnlineEvaluator{Judge: onlineJudge, Store: store, DBTimeout: config.DBTimeout}}
	}
	return r, func() { closeAdapters(); pool.Close() }, nil
}

type qualityEvaluator struct{ Judge *provider.LLMJudgeEvaluator }

func (q qualityEvaluator) Evaluate(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if in.Work.Metadata.Spec.Definition.Kind == metric.LLMJudge {
		return q.Judge.Evaluate(ctx, in)
	}
	return (agentquality.DeterministicEvaluator{}).Evaluate(ctx, in)
}
