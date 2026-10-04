package worker

import (
	"context"
	"log/slog"
	"runtime/debug"
	"sync"
	"sync/atomic"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	ob "agentevalops/go-backend/internal/observation"
	"agentevalops/go-backend/internal/postgres"
)

type OnlineEvaluator interface {
	EvaluateOnline(context.Context, ob.Scope, ob.Work) (ob.Value, error)
}
type OnlineRuntime struct {
	Config    Config
	Store     postgres.Online
	Epoch     int64
	Evaluator OnlineEvaluator
	Log       *slog.Logger
}

func (r *OnlineRuntime) scope(ctx context.Context, p string) (ob.Scope, error) {
	s, e := (postgres.Evaluation{Pool: r.Store.Pool}).WorkerScope(ctx, p, r.Config.WorkerID, r.Epoch)
	return ob.Scope{Scope: s.Scope, Epoch: s.Epoch, Evaluate: true, Reconcile: true}, e
}
func (r *OnlineRuntime) Run(ctx context.Context) {
	poll, stop := context.WithCancel(context.WithoutCancel(ctx))
	defer stop()
	operations, cancel := context.WithCancel(context.WithoutCancel(ctx))
	defer cancel()
	var loops sync.WaitGroup
	start := func(fn func()) { loops.Add(1); go func() { defer loops.Done(); fn() }() }
	for i := 0; i < r.Config.OnlineConcurrency; i++ {
		start(func() { r.scan(poll, false, func(c ob.Candidate) { r.evaluate(poll, operations, c) }) })
	}
	start(func() {
		r.scan(poll, true, func(c ob.Candidate) {
			db, cancel := context.WithTimeout(poll, r.Config.DBTimeout)
			defer cancel()
			s, e := r.scope(db, c.ProjectID)
			if e == nil {
				_, _ = r.Store.ExpireOnlineWork(db, s, c.ID)
			}
		})
	})
	start(func() {
		var cursor ob.Cursor
		b := backoff{minimum: r.Config.PollInterval, maximum: r.Config.MaxBackoff}
		for poll.Err() == nil {
			db, cancel := context.WithTimeout(poll, r.Config.DBTimeout)
			items, e := r.Store.MaterializationCandidates(db, cursor, r.Config.ScanBatch)
			cancel()
			if e == nil {
				for _, c := range items {
					cursor = ob.Cursor{Created: c.Created, ID: c.ID}
					db, cancel := context.WithTimeout(poll, r.Config.DBTimeout)
					s, e := r.scope(db, c.ProjectID)
					if e == nil {
						_, _ = r.Store.Materialize(db, s, c.Rule, c.ID, false)
					}
					cancel()
				}
				if len(items) == 0 {
					cursor = ob.Cursor{}
				}
			}
			if !wait(poll, b.next(e == nil && len(items) > 0)) {
				return
			}
		}
	})
	start(func() {
		var cursor ob.Cursor
		b := backoff{minimum: r.Config.PollInterval, maximum: r.Config.MaxBackoff}
		for poll.Err() == nil {
			db, cancel := context.WithTimeout(poll, r.Config.DBTimeout)
			items, e := r.Store.BackfillCandidates(db, cursor, r.Config.ScanBatch)
			cancel()
			if e == nil {
				for _, c := range items {
					cursor = ob.Cursor{Created: c.Created, ID: c.ID}
					db, cancel := context.WithTimeout(poll, r.Config.DBTimeout)
					s, e := r.scope(db, c.ProjectID)
					if e == nil {
						_, _ = r.Store.ContinueBackfill(db, s, c.ID, r.Config.ScanBatch)
					}
					cancel()
				}
				if len(items) == 0 {
					cursor = ob.Cursor{}
				}
			}
			if !wait(poll, b.next(e == nil && len(items) > 0)) {
				return
			}
		}
	})
	r.Log.Info("online_runtime_started", "event", "online_runtime_started", "online_capacity", r.Config.OnlineConcurrency)
	<-ctx.Done()
	stop()
	done := make(chan struct{})
	go func() { loops.Wait(); close(done) }()
	timer := time.NewTimer(r.Config.DrainTimeout)
	select {
	case <-done:
	case <-timer.C:
		cancel()
		<-done
	}
	timer.Stop()
	r.Log.Info("online_runtime_stopped", "event", "online_runtime_stopped")
}
func (r *OnlineRuntime) scan(ctx context.Context, expired bool, fn func(ob.Candidate)) {
	var cursor ob.Cursor
	b := backoff{minimum: r.Config.PollInterval, maximum: r.Config.MaxBackoff}
	for ctx.Err() == nil {
		db, cancel := context.WithTimeout(ctx, r.Config.DBTimeout)
		items, e := r.Store.OnlineCandidates(db, cursor, expired, r.Config.ScanBatch)
		cancel()
		progress := false
		if e == nil {
			for _, c := range items {
				if ctx.Err() != nil {
					return
				}
				cursor = ob.Cursor{Created: c.Created, ID: c.ID}
				fn(c)
				progress = true
			}
			if len(items) == 0 {
				cursor = ob.Cursor{}
			}
		}
		if !wait(ctx, b.next(progress)) {
			return
		}
	}
}
func (r *OnlineRuntime) evaluate(poll, operations context.Context, c ob.Candidate) {
	db, cancel := context.WithTimeout(poll, r.Config.DBTimeout)
	s, e := r.scope(db, c.ProjectID)
	if e != nil {
		cancel()
		return
	}
	claim, e := r.Store.ClaimOnlineWork(db, s, c.ID, r.Config.WorkerID, r.Config.LeaseDuration)
	cancel()
	if e != nil || claim.Code != ev.Applied {
		return
	}
	ctx, cancelOp := context.WithCancel(operations)
	defer cancelOp()
	renew, stopRenew := context.WithCancel(context.WithoutCancel(operations))
	done := make(chan struct{})
	var lost atomic.Bool
	// safeUntil 使用请求开始的保守下界；最终写权仍由锁后的 DB clock 决定。
	safeUntil := time.Now().Add(r.Config.LeaseDuration - r.Config.DBTimeout)
	go func() {
		defer close(done)
		for wait(renew, r.Config.RenewInterval) {
			started := time.Now()
			db, cancel := context.WithTimeout(renew, r.Config.DBTimeout)
			reply, err := r.Store.RenewOnlineLease(db, s, c.ID, claim.Token, r.Config.LeaseDuration)
			cancel()
			if err == nil && reply.Code == ev.Applied {
				safeUntil = started.Add(r.Config.LeaseDuration)
			} else if err == nil || !time.Now().Before(safeUntil) {
				lost.Store(true)
				cancelOp()
				return
			}
		}
	}()
	defer func() {
		stopRenew()
		<-done
		if p := recover(); p != nil {
			r.Log.Error("online_task_panic", "event", "online_task_panic", "work_id", c.ID, "stack", string(debug.Stack()))
		}
	}()
	db, cancel = context.WithTimeout(ctx, r.Config.DBTimeout)
	w, _, e := r.Store.GetOnlineEvaluationState(db, s.Scope, c.ID)
	cancel()
	if e != nil {
		return
	}
	w.Token = claim.Token
	budget, cancelBudget := context.WithTimeout(ctx, time.Duration(w.Binding.Evaluator.Definition.Budget.TotalMilliseconds)*time.Millisecond)
	defer cancelBudget()
	value, e := r.Evaluator.EvaluateOnline(budget, s, w)
	if e != nil || lost.Load() {
		return
	}
	command, resultID := asset.NewID(), asset.NewID()
	db, cancel = context.WithTimeout(context.WithoutCancel(ctx), r.Config.DBTimeout)
	defer cancel()
	for !lost.Load() {
		reply, err := r.Store.FinalizeOnlineResult(db, s, c.ID, claim.Token, command, resultID, value)
		if err == nil {
			if reply.Code != ev.Applied && reply.Code != ev.AlreadyApplied {
				r.Log.Warn("online_finalize_rejected", "event", "online_finalize_rejected", "work_id", c.ID, "reason", reply.Reason)
			}
			return
		}
		if !wait(db, 10*time.Millisecond) {
			return
		}
	}
}
