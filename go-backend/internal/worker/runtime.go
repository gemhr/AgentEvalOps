package worker

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log/slog"
	"runtime/debug"
	"sync"
	"sync/atomic"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/postgres"
)

// Backend 是 Worker 的真实消费端口；scanner 只读取候选，写入继续经过 G2。
type Backend interface {
	ev.Persistence
	ev.Reader
	Candidates(context.Context, postgres.CandidateKind, postgres.EvaluationCursor, int) ([]postgres.EvaluationCandidate, error)
	WorkerScope(context.Context, string, string, int64) (ev.Scope, error)
	ReconcileEvaluatorSlots(context.Context, ev.Scope, ev.Owned) (ev.Reply, error)
}

// 实现必须响应 context 取消；返回 error 只表示本地无法确认，不代表业务终态。
type ExecutionTarget interface {
	Execute(context.Context, ev.Scope, ev.Request) (ev.Outcome, error)
}
type EvaluationInput struct {
	Run         ev.Run
	Attempt     ev.Attempt
	Work        ev.Work
	Scope       ev.Scope
	Owned       ev.Owned
	Persistence ev.Persistence
}
type EvaluationOutput struct {
	Value     ev.ResultValue
	Retryable bool
}
type Evaluator interface {
	Evaluate(context.Context, EvaluationInput) (EvaluationOutput, error)
}

type Counters struct{ Claims, LostRaces, Renewed, RenewalErrors, OwnershipLost, Reconciled, Panics atomic.Uint64 }
type Runtime struct {
	Config    Config
	Backend   Backend
	Target    ExecutionTarget
	Evaluator Evaluator
	Epoch     int64
	Log       *slog.Logger
	Counters  Counters
	admission sync.Mutex
	accepting bool
}

func wait(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

func (r *Runtime) Run(ctx context.Context) error {
	if err := r.Config.Validate(); err != nil {
		return err
	}
	if r.Backend == nil || r.Epoch < 1 || (r.Config.ExecutionConcurrency > 0 && r.Target == nil) || (r.Config.EvaluatorConcurrency > 0 && r.Evaluator == nil) {
		return asset.ErrInvalid
	}
	if r.Log == nil {
		r.Log = slog.Default()
	}
	poll, stopPolling := context.WithCancel(context.WithoutCancel(ctx))
	defer stopPolling()
	operations, cancelOperations := context.WithCancel(context.WithoutCancel(ctx))
	defer cancelOperations()
	r.admission.Lock()
	r.accepting = ctx.Err() == nil
	r.admission.Unlock()
	var loops sync.WaitGroup
	start := func(fn func()) { loops.Add(1); go func() { defer loops.Done(); fn() }() }
	// 固定 N/M 个 slot：每个循环在同步任务返回后才领取下一个任务，没有已 claim 的本地队列。
	for i := 0; i < r.Config.ExecutionConcurrency; i++ {
		start(func() {
			r.poll(poll, postgres.PendingExecution, func(c postgres.EvaluationCandidate) bool { return r.task(poll, operations, c, false) })
		})
	}
	for i := 0; i < r.Config.EvaluatorConcurrency; i++ {
		start(func() {
			r.poll(poll, postgres.PendingEvaluator, func(c postgres.EvaluationCandidate) bool { return r.task(poll, operations, c, true) })
		})
	}
	if r.Config.Reconcile {
		for _, kind := range []postgres.CandidateKind{postgres.ExpiredExecution, postgres.ExpiredEvaluator, postgres.MissingEvaluatorSlots} {
			start(func() {
				r.poll(poll, kind, func(c postgres.EvaluationCandidate) bool { return r.reconcile(poll, kind, c) })
			})
		}
	}
	if r.Config.Coordinate {
		start(func() {
			r.poll(poll, postgres.RunningRun, func(c postgres.EvaluationCandidate) bool { return r.reconcile(poll, postgres.RunningRun, c) })
		})
	}
	r.Log.Info("runtime_started", "event", "runtime_started", "worker_id", r.Config.WorkerID, "execution_capacity", r.Config.ExecutionConcurrency, "evaluator_capacity", r.Config.EvaluatorConcurrency)
	<-ctx.Done()
	stopPolling() // 取消候选查询及尚未提交的 claim，不取消 owned operation/renewer。
	r.admission.Lock()
	r.accepting = false
	r.admission.Unlock()
	r.Log.Info("admission_stopped", "event", "admission_stopped", "worker_id", r.Config.WorkerID)
	done := make(chan struct{})
	go func() { loops.Wait(); close(done) }() // 由 Run 启动、下方 join；仅持有 WaitGroup。
	timer := time.NewTimer(r.Config.DrainTimeout)
	select {
	case <-done:
	case <-timer.C:
		r.Log.Info("drain_timeout", "event", "drain_timeout", "worker_id", r.Config.WorkerID)
		cancelOperations() // 操作取消与续租取消分开，允许 owner 记录保守 observation。
		<-done
	}
	timer.Stop()
	r.Log.Info("runtime_stopped", "event", "runtime_stopped", "worker_id", r.Config.WorkerID, "claims", r.Counters.Claims.Load(), "lost_races", r.Counters.LostRaces.Load(), "renewed", r.Counters.Renewed.Load(), "renewal_errors", r.Counters.RenewalErrors.Load(), "ownership_lost", r.Counters.OwnershipLost.Load(), "reconciled", r.Counters.Reconciled.Load(), "panics", r.Counters.Panics.Load())
	return nil
}

func (r *Runtime) poll(ctx context.Context, kind postgres.CandidateKind, handle func(postgres.EvaluationCandidate) bool) {
	cursor := postgres.EvaluationCursor{}
	b := backoff{minimum: r.Config.PollInterval, maximum: r.Config.MaxBackoff}
	for ctx.Err() == nil {
		query, cancel := context.WithTimeout(ctx, r.Config.DBTimeout)
		page, err := r.Backend.Candidates(query, kind, cursor, r.Config.ScanBatch)
		cancel()
		progress := false
		if err != nil {
			r.Log.Warn("scan_error", "event", "scan_error", "worker_id", r.Config.WorkerID, "kind", kind)
		} else {
			if len(page) == 0 {
				cursor = postgres.EvaluationCursor{}
			} // 完整一轮回绕；不可完成的最老 row 不阻塞后面的 row。
			for _, c := range page {
				if ctx.Err() != nil {
					break
				}
				cursor = c.Cursor
				if handle(c) {
					progress = true
				}
			}
		}
		if !wait(ctx, b.next(progress)) {
			return
		}
	}
}

func (r *Runtime) scope(ctx context.Context, c postgres.EvaluationCandidate) (ev.Scope, error) {
	query, cancel := context.WithTimeout(ctx, r.Config.DBTimeout)
	defer cancel()
	return r.Backend.WorkerScope(query, c.ProjectID, r.Config.WorkerID, r.Epoch)
}
func (r *Runtime) reconcile(ctx context.Context, kind postgres.CandidateKind, c postgres.EvaluationCandidate) bool {
	s, err := r.scope(ctx, c)
	if err != nil {
		return false
	}
	query, cancel := context.WithTimeout(ctx, r.Config.DBTimeout)
	defer cancel()
	o := ev.Owned{RunID: c.RunID, AttemptID: c.AttemptID, WorkID: c.WorkID}
	var reply ev.Reply
	switch kind {
	case postgres.ExpiredExecution:
		reply, err = r.Backend.ExpireExecutionAttempt(query, s, o)
	case postgres.ExpiredEvaluator:
		reply, err = r.Backend.ExpireEvaluatorWork(query, s, o)
	case postgres.MissingEvaluatorSlots:
		reply, err = r.Backend.ReconcileEvaluatorSlots(query, s, o)
	case postgres.RunningRun:
		reply, err = r.Backend.CoordinateRun(query, s, c.RunID)
	}
	if err != nil {
		r.event("reconcile_error", s, o)
		return false
	}
	if kind == postgres.RunningRun {
		r.event("coordinator_checked", s, o, "code", reply.Code, "status", reply.Status)
	}
	if reply.Code == ev.Applied {
		r.Counters.Reconciled.Add(1)
		r.event("reconciled", s, o, "kind", kind, "status", reply.Status, "reason", reply.Reason)
		return true
	}
	if reply.Code == ev.IntegrityBlocked {
		r.event("integrity_blocked", s, o, "reason", reply.Reason)
	}
	return false
}

type ownedTask struct {
	identity          ev.Owned
	scope             ev.Scope
	epoch             int64
	lease             *time.Time
	cancelOperation   context.CancelFunc
	stopRenewal       context.CancelFunc
	renewalDone, done chan struct{}
	lost              atomic.Bool
	// safeUntil 是从请求开始计算的单调时钟保守下界，不替代 DB fencing。
	safeUntil atomic.Int64
}

func (r *Runtime) event(event string, s ev.Scope, o ev.Owned, extra ...any) {
	args := []any{"event", event, "worker_id", r.Config.WorkerID, "project_id", s.ProjectID, "run_id", o.RunID, "attempt_id", o.AttemptID, "work_id", o.WorkID}
	if o.Token != "" {
		hash := sha256.Sum256([]byte(o.Token))
		args = append(args, "claim_hash", fmt.Sprintf("%x", hash[:6]))
	}
	r.Log.Info(event, append(args, extra...)...)
}

func (r *Runtime) lose(o *ownedTask) {
	if o.lost.CompareAndSwap(false, true) {
		r.Counters.OwnershipLost.Add(1)
		r.event("ownership_lost", o.scope, o.identity)
		o.cancelOperation()
	}
}

func (r *Runtime) task(poll, operations context.Context, c postgres.EvaluationCandidate, evaluator bool) (progress bool) {
	s, err := r.scope(poll, c)
	if err != nil {
		return false
	}
	o := ev.Owned{RunID: c.RunID, AttemptID: c.AttemptID, WorkID: c.WorkID}
	r.admission.Lock()
	if !r.accepting || poll.Err() != nil {
		r.admission.Unlock()
		return false
	}
	query, cancel := context.WithTimeout(poll, r.Config.DBTimeout)
	started := time.Now()
	var reply ev.Reply
	if evaluator {
		reply, err = r.Backend.ClaimEvaluatorWork(query, s, o, r.Config.WorkerID, r.Config.LeaseDuration)
	} else {
		reply, err = r.Backend.ClaimExecutionAttempt(query, s, o, r.Config.WorkerID, r.Config.LeaseDuration)
	}
	cancel()
	r.admission.Unlock()
	if err != nil {
		r.event("claim_error", s, o)
		return false
	} // commit 不明不 dispatch，等待 PG expiry/recovery。
	if reply.Code != ev.Applied {
		if reply.Code == ev.NotClaimed {
			r.Counters.LostRaces.Add(1)
			r.event("claim_lost_race", s, o)
		}
		return false
	}
	o.Token = reply.Token
	r.Counters.Claims.Add(1)
	r.event("claim_success", s, o)
	op, cancelOp := context.WithCancel(operations)
	renew, stopRenew := context.WithCancel(context.Background())
	owned := &ownedTask{identity: o, scope: s, epoch: s.Epoch, lease: reply.Lease, cancelOperation: cancelOp, stopRenewal: stopRenew, renewalDone: make(chan struct{}), done: make(chan struct{})}
	// time.Since 保留 monotonic clock；续租成功更新此相对起点。
	anchor := time.Now()
	owned.safeUntil.Store(r.Config.LeaseDuration.Nanoseconds() - anchor.Sub(started).Nanoseconds())
	renewing := false
	defer func() {
		stopRenew()
		if renewing {
			<-owned.renewalDone
		}
		cancelOp()
		close(owned.done)
		r.event("task_finished", s, o, "duration_ms", time.Since(started).Milliseconds())
	}()
	defer func() {
		if v := recover(); v != nil {
			r.Counters.Panics.Add(1)
			r.event("task_panic", s, o, "panic_type", fmt.Sprintf("%T", v), "stack", string(debug.Stack()))
		}
	}()
	if !evaluator {
		if !r.command(owned, anchor, func(ctx context.Context) (ev.Reply, error) { return r.Backend.StartExecutionAttempt(ctx, s, o) }) {
			return true
		}
	}
	go r.renew(renew, owned, evaluator, anchor)
	renewing = true
	query, cancel = context.WithTimeout(context.Background(), r.Config.DBTimeout)
	state, err := r.Backend.ReadRunState(query, s.Scope, c.RunID)
	cancel()
	if err != nil {
		r.event("read_owned_error", s, o)
		return true
	}
	var attempt ev.Attempt
	for _, a := range state.Attempts {
		if a.ID == c.AttemptID {
			attempt = a
			break
		}
	}
	if attempt.ID == "" {
		return true
	}
	if owned.lost.Load() || op.Err() != nil {
		return true
	}
	if evaluator {
		r.evaluate(op, owned, anchor, state, attempt)
	} else {
		r.event("execution_started", s, o)
		out, err := r.Target.Execute(op, s, attempt.Request)
		if err != nil {
			r.event("execution_unconfirmed", s, o)
			return true
		}
		cmd := ev.FinalizeOutcome{Owned: o, CommandID: asset.NewID(), Outcome: out}
		if r.command(owned, anchor, func(ctx context.Context) (ev.Reply, error) { return r.Backend.FinalizeExecutionOutcome(ctx, s, cmd) }) {
			r.event("execution_finalized", s, o, "outcome", out.Kind)
		}
	}
	return true
}

func (r *Runtime) renew(ctx context.Context, o *ownedTask, evaluator bool, anchor time.Time) {
	defer close(o.renewalDone)
	for {
		remaining := time.Duration(o.safeUntil.Load()) - time.Since(anchor)
		if remaining <= 0 {
			r.lose(o)
			return
		}
		if !wait(ctx, min(r.Config.RenewInterval, remaining)) {
			return
		}
		remaining = time.Duration(o.safeUntil.Load()) - time.Since(anchor)
		if remaining <= 0 {
			r.lose(o)
			return
		}
		query, cancel := context.WithTimeout(ctx, min(r.Config.DBTimeout, remaining))
		began := time.Since(anchor)
		var reply ev.Reply
		var err error
		if evaluator {
			reply, err = r.Backend.RenewEvaluatorLease(query, o.scope, o.identity, r.Config.LeaseDuration)
		} else {
			reply, err = r.Backend.RenewExecutionLease(query, o.scope, o.identity, r.Config.LeaseDuration)
		}
		cancel()
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			r.Counters.RenewalErrors.Add(1)
			r.event("lease_renew_error", o.scope, o.identity)
			continue
		}
		if reply.Code != ev.Applied {
			r.lose(o)
			return
		}
		o.safeUntil.Store((began + r.Config.LeaseDuration).Nanoseconds())
		r.Counters.Renewed.Add(1)
		r.event("lease_renew_success", o.scope, o.identity)
	}
}

// 同一稳定 command 重交仅重试 DB；不会重新调用 target/evaluator。
func (r *Runtime) command(o *ownedTask, anchor time.Time, fn func(context.Context) (ev.Reply, error)) bool {
	b := backoff{minimum: r.Config.PollInterval, maximum: r.Config.MaxBackoff}
	deadline := time.Now().Add(r.Config.DBTimeout)
	for !o.lost.Load() {
		leaseRemaining := time.Duration(o.safeUntil.Load()) - time.Since(anchor)
		if leaseRemaining <= 0 {
			r.lose(o)
			return false
		}
		remaining := min(leaseRemaining, time.Until(deadline))
		if remaining <= 0 {
			return false
		}
		query, cancel := context.WithTimeout(context.Background(), min(r.Config.DBTimeout, remaining))
		reply, err := fn(query)
		cancel()
		if err == nil {
			if reply.Code == ev.OwnershipLost || (reply.Code == ev.Rejected && reply.Reason == "WRITER_NOT_ACTIVE") {
				r.lose(o)
			}
			if reply.Code == ev.Applied || reply.Code == ev.AlreadyApplied {
				return true
			}
			r.event("command_rejected", o.scope, o.identity, "code", reply.Code, "reason", reply.Reason)
			return false
		}
		r.event("command_db_error", o.scope, o.identity)
		// DB 长期不可用时有界放弃；续租线程不能令 shutdown 无限等待。
		if !wait(context.Background(), min(b.next(false), remaining)) {
			return false
		}
	}
	return false
}

func (r *Runtime) evaluate(ctx context.Context, o *ownedTask, anchor time.Time, state ev.RunState, attempt ev.Attempt) {
	var work ev.Work
	for _, w := range state.Works {
		if w.ID == o.identity.WorkID {
			work = w
			break
		}
	}
	if work.ID == "" || attempt.Metadata.Observation.Artifact == nil {
		return
	}
	value := errorValue(work, attempt, "EVALUATION_BUDGET_EXHAUSTED")
	// G2 的 FINALIZE_ERROR 允许重用已 durable 且完整验证的 draft，不重复 provider。
	draftFound := false
	for _, call := range work.Calls {
		if call.Classification == "RESPONSE_RECEIVED" && call.Draft != nil {
			value = *call.Draft
			draftFound = true
			break
		}
	}
	if work.Metadata.CompletionMode != "FINALIZE_ERROR" && !draftFound {
		command := asset.NewID()
		if !r.command(o, anchor, func(db context.Context) (ev.Reply, error) {
			return r.Backend.BeginEvaluatorAttempt(db, o.scope, o.identity, command, work.Evaluations+1)
		}) {
			return
		}
		work.Evaluations++
		r.event("evaluator_started", o.scope, o.identity, "evaluation_no", work.Evaluations)
		input := EvaluationInput{Run: state.Run, Attempt: attempt, Work: work, Scope: o.scope, Owned: o.identity, Persistence: r.Backend}
		budget := time.Duration(work.Metadata.Spec.Definition.Budget.TotalMilliseconds) * time.Millisecond
		if work.Metadata.DeadlineAt != nil {
			budget = min(budget, time.Until(*work.Metadata.DeadlineAt))
		}
		evalCtx, cancel := context.WithTimeout(ctx, budget)
		out, err := r.Evaluator.Evaluate(evalCtx, input)
		cancel()
		if err != nil {
			r.event("evaluation_unconfirmed", o.scope, o.identity)
			return
		}
		if out.Retryable {
			command = asset.NewID()
			r.command(o, anchor, func(db context.Context) (ev.Reply, error) {
				return r.Backend.ReleaseEvaluatorWorkRetry(db, o.scope, o.identity, command, "CONTROLLED_RETRYABLE")
			})
			return
		}
		value = out.Value
	}
	cmd := ev.FinalizeResult{Owned: o.identity, CommandID: asset.NewID(), ResultID: asset.NewID(), Value: value}
	if r.command(o, anchor, func(db context.Context) (ev.Reply, error) {
		return r.Backend.FinalizeEvaluationResult(db, o.scope, cmd)
	}) {
		r.event("result_finalized", o.scope, o.identity, "verdict", value.Verdict)
	}
}

func errorValue(work ev.Work, attempt ev.Attempt, reason string) ev.ResultValue {
	return ev.ResultValue{Verdict: "ERROR", SourceVerdict: "ERROR", SourceError: reason, Reason: reason,
		SpecDigest: work.SpecDigest, InputDigest: attempt.Request.Case.Identity.ContentDigest,
		Artifact: *attempt.Metadata.Observation.Artifact, Evidence: attempt.Metadata.Observation.Evidence}
}
