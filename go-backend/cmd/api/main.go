package main

import (
	"context"
	"flag"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"agentevalops/go-backend/internal/bootstrap"
	"agentevalops/go-backend/internal/httpapi"
	"agentevalops/go-backend/internal/postgres"
)

func main() { os.Exit(run()) }
func run() int {
	config := httpapi.DefaultConfig()
	listen := flag.String("listen", "127.0.0.1:8081", "Product API listener")
	dev := flag.Bool("controlled-dev-auth", false, "development/test 显式用户认证；生产拒绝")
	fixture := flag.Bool("fixture-only", false, "development/test 明确使用 Worker fixture capability")
	origins := flag.String("cors-origins", "", "逗号分隔的精确 origin allowlist")
	grants := flag.String("print-role-grants", "", "offline operator 输出受限 API role grants，不连接或修改 DB")
	flag.DurationVar(&config.DBTimeout, "db-timeout", config.DBTimeout, "短 command DB deadline")
	flag.DurationVar(&config.ShutdownTimeout, "shutdown-timeout", config.ShutdownTimeout, "graceful shutdown deadline")
	flag.Int64Var(&config.BodyLimit, "body-limit", config.BodyLimit, "全局 JSON bytes 上限")
	flag.IntVar(&config.RequestsPerMinute, "requests-per-minute", config.RequestsPerMinute, "BEST_EFFORT_LOCAL 请求限制")
	flag.Parse()
	if *grants != "" {
		os.Stdout.WriteString(postgres.ProductRoleGrants(*grants) + "\n")
		return 0
	}
	if *origins != "" {
		config.AllowedOrigins = strings.Split(*origins, ",")
	}
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	startup, cancel := context.WithTimeout(ctx, config.DBTimeout)
	api, closeDB, err := bootstrap.API(startup, config, *dev, *fixture, log)
	cancel()
	if err != nil {
		log.Error("api_bootstrap_failed", "reason", "config/schema/writer/role/dependency")
		return 2
	}
	defer closeDB()
	l, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Error("api_listener_failed")
		return 2
	}
	log.Info("api_listening", "address", l.Addr().String(), "rate_scope", "BEST_EFFORT_LOCAL")
	if err = httpapi.Serve(ctx, l, api.Handler(), config.ShutdownTimeout); err != nil {
		log.Error("api_shutdown_failed")
		return 2
	}
	log.Info("api_stopped")
	return 0
}
