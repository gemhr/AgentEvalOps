package bootstrap

import (
	"flag"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	"os"
	"strconv"
	"strings"
	"time"
)

// ApplyFlagEnvironment 在 Parse 之前设置环境默认值，命令行仍具有最高优先级。
func ApplyFlagEnvironment(f *flag.FlagSet, prefix string) error {
	var failure error
	f.VisitAll(func(v *flag.Flag) {
		if raw, ok := os.LookupEnv(prefix + strings.ToUpper(strings.ReplaceAll(v.Name, "-", "_"))); ok {
			if e := v.Value.Set(raw); e != nil {
				failure = fmt.Errorf("CONFIG_REJECTED: %s", v.Name)
			}
		}
	})
	return failure
}

func ConfigurePool(c *pgxpool.Config, defaultMax int32) error {
	c.MaxConns = defaultMax
	c.MinConns = 0
	c.MaxConnLifetime = 30 * time.Minute
	c.MaxConnIdleTime = 5 * time.Minute
	c.HealthCheckPeriod = 30 * time.Second
	for name, dst := range map[string]*int32{"DB_POOL_MAX_CONNS": &c.MaxConns, "DB_POOL_MIN_CONNS": &c.MinConns} {
		if raw, ok := os.LookupEnv(name); ok {
			n, e := strconv.ParseInt(raw, 10, 32)
			if e != nil {
				return fmt.Errorf("POOL_CONFIG_REJECTED: %s", name)
			}
			*dst = int32(n)
		}
	}
	for name, dst := range map[string]*time.Duration{"DB_POOL_MAX_CONN_LIFETIME": &c.MaxConnLifetime, "DB_POOL_MAX_CONN_IDLE_TIME": &c.MaxConnIdleTime, "DB_POOL_HEALTH_CHECK_PERIOD": &c.HealthCheckPeriod} {
		if raw, ok := os.LookupEnv(name); ok {
			n, e := time.ParseDuration(raw)
			if e != nil {
				return fmt.Errorf("POOL_CONFIG_REJECTED: %s", name)
			}
			*dst = n
		}
	}
	if c.MaxConns < 1 || c.MaxConns > 256 || c.MinConns < 0 || c.MinConns > c.MaxConns || c.MaxConnLifetime < time.Second || c.MaxConnIdleTime < time.Second || c.HealthCheckPeriod < time.Second {
		return fmt.Errorf("POOL_CONFIG_REJECTED")
	}
	c.ConnConfig.ConnectTimeout = 3 * time.Second
	return nil
}
