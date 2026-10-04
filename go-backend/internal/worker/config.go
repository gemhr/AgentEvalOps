// Package worker 拥有进程内容量、扫描和有界停机；持久化 authority 属于 evaluation。
package worker

import (
	"fmt"
	"time"

	"agentevalops/go-backend/internal/asset"
)

type Config struct {
	WorkerID                                                                        string
	ExecutionConcurrency, EvaluatorConcurrency, ScanBatch                           int
	PollInterval, MaxBackoff, LeaseDuration, RenewInterval, DrainTimeout, DBTimeout time.Duration
	Reconcile, Coordinate                                                           bool
}

func DefaultConfig() Config {
	return Config{WorkerID: "worker-" + asset.NewID(), ExecutionConcurrency: 4, EvaluatorConcurrency: 4,
		ScanBatch: 32, PollInterval: 100 * time.Millisecond, MaxBackoff: 2 * time.Second,
		LeaseDuration: 30 * time.Second, RenewInterval: 5 * time.Second, DrainTimeout: 20 * time.Second,
		DBTimeout: 3 * time.Second, Reconcile: true, Coordinate: true}
}

func (c Config) Validate() error {
	if !asset.Text(c.WorkerID) || c.ExecutionConcurrency < 0 || c.ExecutionConcurrency > 256 ||
		c.EvaluatorConcurrency < 0 || c.EvaluatorConcurrency > 256 || c.ScanBatch < 1 || c.ScanBatch > 100 ||
		c.PollInterval < time.Millisecond || c.MaxBackoff < c.PollInterval || c.MaxBackoff > time.Minute ||
		c.LeaseDuration < time.Millisecond || c.LeaseDuration > 5*time.Minute ||
		c.RenewInterval < time.Millisecond || c.DBTimeout < time.Millisecond ||
		c.RenewInterval+c.DBTimeout >= c.LeaseDuration || c.DrainTimeout < 0 || c.DrainTimeout > 10*time.Minute {
		return fmt.Errorf("非法 Worker 配置：容量、轮询或续租时间边界不成立")
	}
	return nil
}

type backoff struct{ current, minimum, maximum time.Duration }

func (b *backoff) next(progress bool) time.Duration {
	if progress || b.current == 0 {
		b.current = b.minimum
	} else if b.current > b.maximum/2 {
		b.current = b.maximum
	} else {
		b.current *= 2
	}
	if b.current > b.maximum {
		b.current = b.maximum
	}
	return b.current
}
