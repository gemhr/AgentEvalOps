package main

import (
	"context"
	"flag"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"agentevalops/go-backend/internal/bootstrap"
	"agentevalops/go-backend/internal/worker"
)

func main() { os.Exit(run()) }
func run() int {
	c := worker.DefaultConfig()
	fixtureOnly := flag.Bool("fixture-only", false, "CONTROLLED / TEST ONLY：显式装配本地 fixture，无网络 provider")
	flag.StringVar(&c.WorkerID, "worker-id", c.WorkerID, "Worker 审计身份，不是 authorization")
	flag.IntVar(&c.ExecutionConcurrency, "execution-concurrency", c.ExecutionConcurrency, "独立 execution 容量；0 停用")
	flag.IntVar(&c.EvaluatorConcurrency, "evaluator-concurrency", c.EvaluatorConcurrency, "独立 evaluator 容量；0 停用")
	flag.IntVar(&c.ScanBatch, "scan-batch", c.ScanBatch, "每次候选读取上限 1–100")
	flag.DurationVar(&c.PollInterval, "poll-interval", c.PollInterval, "有工作时轮询间隔")
	flag.DurationVar(&c.MaxBackoff, "max-backoff", c.MaxBackoff, "空队列／DB 错误退避上限")
	flag.DurationVar(&c.LeaseDuration, "lease-duration", c.LeaseDuration, "G2 lease 时长")
	flag.DurationVar(&c.RenewInterval, "renew-interval", c.RenewInterval, "独立续租间隔")
	flag.DurationVar(&c.DrainTimeout, "drain-timeout", c.DrainTimeout, "停止 admission 后的 drain 时限")
	flag.DurationVar(&c.DBTimeout, "db-timeout", c.DBTimeout, "短 DB command 的本地时限")
	flag.BoolVar(&c.Reconcile, "reconcile", c.Reconcile, "启用过期／缺槽扫描")
	flag.BoolVar(&c.Coordinate, "coordinate", c.Coordinate, "启用 Run coordinator")
	flag.Parse()
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := c.Validate(); err != nil {
		log.Error("config_rejected")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	startup, cancel := context.WithTimeout(ctx, c.DBTimeout)
	r, closeDB, err := bootstrap.Worker(startup, os.Getenv("DATABASE_URL"), c, *fixtureOnly, log)
	cancel()
	if err != nil {
		log.Error("bootstrap_failed", "error_type", "schema/writer/config/connection")
		return 2
	}
	defer closeDB() // Run 已 join 全部 owned goroutine 后才关闭。
	if err = r.Run(ctx); err != nil {
		log.Error("runtime_failed")
		return 1
	}
	return 0
}
