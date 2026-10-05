package bootstrap

import (
	"flag"
	"github.com/jackc/pgx/v5/pgxpool"
	"testing"
	"time"
)

func TestG9ConfigPrecedenceAndPoolBounds(t *testing.T) {
	f := flag.NewFlagSet("config", flag.ContinueOnError)
	value := f.Int("slots", 2, "")
	t.Setenv("G9_SLOTS", "3")
	if ApplyFlagEnvironment(f, "G9_") != nil || f.Parse([]string{"--slots=4"}) != nil || *value != 4 {
		t.Fatal("flags > environment > default")
	}
	c, e := pgxpool.ParseConfig("postgres://controlled@localhost/isolated")
	if e != nil {
		t.Fatal(e)
	}
	t.Setenv("DB_POOL_MAX_CONNS", "8")
	if ConfigurePool(c, 24) != nil || c.MaxConns != 8 || c.MinConns != 0 || c.MaxConnIdleTime != 5*time.Minute {
		t.Fatal("pool defaults", c)
	}
	t.Setenv("DB_POOL_MIN_CONNS", "9")
	if ConfigurePool(c, 24) == nil {
		t.Fatal("accepted min > max")
	}
}
