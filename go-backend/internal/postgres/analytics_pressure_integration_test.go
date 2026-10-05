//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/httpapi"
	"context"
	"github.com/jackc/pgx/v5/pgxpool"
	"net/http"
	"testing"
	"time"
)

func TestG9PoolPressureBounded503(t *testing.T) {
	config := httpapi.DefaultConfig()
	config.DBTimeout = 150 * time.Millisecond
	x := g8WithConfig(t, config)
	conns := []*pgxpool.Conn{}
	for i := int32(0); i < x.pool.Config().MaxConns; i++ {
		c, e := x.pool.Acquire(context.Background())
		mustG7(t, e)
		conns = append(conns, c)
	}
	start := time.Now()
	req, e := http.NewRequest("GET", x.url("/analytics/quality"), nil)
	mustG7(t, e)
	req.Header.Set("Authorization", "Bearer "+x.tokens["reader"])
	response, e := http.DefaultClient.Do(req)
	mustG7(t, e)
	response.Body.Close()
	for _, c := range conns {
		c.Release()
	}
	if response.StatusCode != 503 || time.Since(start) > time.Second {
		t.Fatal("unbounded pool pressure", response.StatusCode, time.Since(start))
	}
	x.call(t, "reader", "GET", x.url("/analytics/quality"), "", "", 200)
	tx, e := x.db.pool.Begin(context.Background())
	mustG7(t, e)
	defer tx.Rollback(context.Background())
	_, e = tx.Exec(context.Background(), "LOCK TABLE product_project_memberships IN ACCESS EXCLUSIVE MODE")
	mustG7(t, e)
	response, e = http.DefaultClient.Do(req)
	mustG7(t, e)
	response.Body.Close()
	if response.StatusCode != 503 {
		t.Fatal("slow database request did not remain bounded", response.StatusCode)
	}
	mustG7(t, tx.Rollback(context.Background()))
	x.call(t, "reader", "GET", x.url("/analytics/quality"), "", "", 200)
	g9Evidence(t, "pool-pressure.json", map[string]any{"pool_max": x.pool.Config().MaxConns, "db_deadline_ms": config.DBTimeout.Milliseconds(), "saturation": "503_then_recovered", "slow_database_lock": "503_then_recovered"})
}
