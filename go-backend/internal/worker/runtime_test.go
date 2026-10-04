package worker

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
)

// 只证明进程内调度；claim/lease/CAS 真实性由独立 PG / OS process tests 验证。
type runtimeBackend struct {
	ev.Persistence
	mu                                                      sync.Mutex
	scope                                                   ev.Scope
	attempts                                                []ev.Attempt
	work                                                    ev.Work
	stuck                                                   string
	claims, active, maxActive, finalizes, renewals, queries int
	lost, dbFailure                                         bool
	temporaryRenew, temporaryFinalize                       bool
}

func newBackend(n int, plan FixturePlan) *runtimeBackend {
	b := &runtimeBackend{scope: ev.Scope{Scope: asset.Scope{ProjectID: asset.NewID(), OrganizationID: asset.NewID(), Principal: "unit"}, Epoch: 1}}
	config, _ := asset.Freeze(plan)
	body, _ := asset.Freeze(map[string]string{"answer": "ok"})
	for i := 0; i < n; i++ {
		id := asset.NewID()
		run := asset.NewID()
		q := asset.NewID()
		b.attempts = append(b.attempts, ev.Attempt{ID: id, RunID: run, Status: "PENDING", RequestID: q, Request: ev.Request{ID: q, RunID: run, AttemptID: id, Case: ev.CaseInput{Case: catalog.CaseContent{Input: body}}, Target: ev.Target{Kind: "FIXTURE", Version: "v1", Config: config}}})
	}
	return b
}
func (b *runtimeBackend) WorkerScope(context.Context, string, string, int64) (ev.Scope, error) {
	return b.scope, nil
}
func (b *runtimeBackend) Candidates(_ context.Context, kind postgres.CandidateKind, after postgres.EvaluationCursor, limit int) ([]postgres.EvaluationCandidate, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.queries++
	out := []postgres.EvaluationCandidate{}
	if kind == postgres.PendingEvaluator && b.work.ID != "" && b.work.Status == "PENDING" {
		return []postgres.EvaluationCandidate{{ProjectID: b.scope.ProjectID, RunID: b.attempts[0].RunID, AttemptID: b.attempts[0].ID, WorkID: b.work.ID, Cursor: postgres.EvaluationCursor{CreatedAt: time.Unix(1, 0), ID: b.work.ID}}}, nil
	}
	if kind != postgres.PendingExecution {
		return out, nil
	}
	for i, a := range b.attempts {
		cursor := postgres.EvaluationCursor{CreatedAt: time.Unix(int64(i+1), 0), ID: a.ID}
		if a.Status == "PENDING" && (after.ID == "" || cursor.CreatedAt.After(after.CreatedAt)) {
			out = append(out, postgres.EvaluationCandidate{ProjectID: b.scope.ProjectID, RunID: a.RunID, AttemptID: a.ID, Cursor: cursor})
			if len(out) == limit {
				break
			}
		}
	}
	return out, nil
}
func (b *runtimeBackend) ClaimExecutionAttempt(_ context.Context, _ ev.Scope, o ev.Owned, _ string, d time.Duration) (ev.Reply, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if o.AttemptID == b.stuck {
		return ev.Reply{Code: ev.NotClaimed}, nil
	}
	for i := range b.attempts {
		a := &b.attempts[i]
		if a.ID == o.AttemptID && a.Status == "PENDING" {
			a.Status = "CLAIMED"
			b.claims++
			b.active++
			b.maxActive = max(b.maxActive, b.active)
			lease := time.Now().Add(d)
			return ev.Reply{Code: ev.Applied, Token: asset.NewID(), Lease: &lease}, nil
		}
	}
	return ev.Reply{Code: ev.NotClaimed}, nil
}
func (b *runtimeBackend) StartExecutionAttempt(_ context.Context, _ ev.Scope, o ev.Owned) (ev.Reply, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for i := range b.attempts {
		if b.attempts[i].ID == o.AttemptID {
			b.attempts[i].Status = "RUNNING"
		}
	}
	return ev.Reply{Code: ev.Applied}, nil
}
func (b *runtimeBackend) ReadRunState(_ context.Context, _ asset.Scope, id string) (ev.RunState, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	state := ev.RunState{Run: ev.Run{ID: id}, Works: []ev.Work{b.work}}
	for _, a := range b.attempts {
		if a.RunID == id {
			state.Attempts = append(state.Attempts, a)
		}
	}
	return state, nil
}
func (b *runtimeBackend) RenewExecutionLease(context.Context, ev.Scope, ev.Owned, time.Duration) (ev.Reply, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.renewals++
	if b.lost {
		return ev.Reply{Code: ev.OwnershipLost}, nil
	}
	if b.dbFailure || (b.temporaryRenew && b.renewals == 1) {
		return ev.Reply{}, errors.New("temporary DB unavailable")
	}
	return ev.Reply{Code: ev.Applied}, nil
}
func (b *runtimeBackend) FinalizeExecutionOutcome(_ context.Context, _ ev.Scope, c ev.FinalizeOutcome) (ev.Reply, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.finalizes++
	if b.temporaryFinalize && b.finalizes == 1 {
		return ev.Reply{}, errors.New("temporary DB unavailable")
	}
	for i := range b.attempts {
		a := &b.attempts[i]
		if a.ID == c.AttemptID {
			a.Status = "TERMINAL"
			a.Outcome = c.Outcome.Kind
			b.active--
		}
	}
	return ev.Reply{Code: ev.Applied}, nil
}
func (b *runtimeBackend) ClaimEvaluatorWork(_ context.Context, _ ev.Scope, _ ev.Owned, _ string, d time.Duration) (ev.Reply, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.work.Status = "CLAIMED"
	b.claims++
	lease := time.Now().Add(d)
	return ev.Reply{Code: ev.Applied, Token: asset.NewID(), Lease: &lease}, nil
}
func (b *runtimeBackend) BeginEvaluatorAttempt(context.Context, ev.Scope, ev.Owned, string, int) (ev.Reply, error) {
	return ev.Reply{Code: ev.Applied}, nil
}
func (b *runtimeBackend) RenewEvaluatorLease(ctx context.Context, s ev.Scope, o ev.Owned, d time.Duration) (ev.Reply, error) {
	return b.RenewExecutionLease(ctx, s, o, d)
}
func (b *runtimeBackend) FinalizeEvaluationResult(context.Context, ev.Scope, ev.FinalizeResult) (ev.Reply, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.finalizes++
	return ev.Reply{Code: ev.Applied}, nil
}
func (b *runtimeBackend) ReconcileEvaluatorSlots(context.Context, ev.Scope, ev.Owned) (ev.Reply, error) {
	return ev.Reply{Code: ev.AlreadyApplied}, nil
}

func testRuntime(b *runtimeBackend) *Runtime {
	c := DefaultConfig()
	c.ExecutionConcurrency = 1
	c.EvaluatorConcurrency = 0
	c.Reconcile = false
	c.Coordinate = false
	c.PollInterval = 2 * time.Millisecond
	c.MaxBackoff = 8 * time.Millisecond
	c.LeaseDuration = 120 * time.Millisecond
	c.RenewInterval = 20 * time.Millisecond
	c.DBTimeout = 30 * time.Millisecond
	c.DrainTimeout = 200 * time.Millisecond
	c.ScanBatch = 1
	return &Runtime{Config: c, Backend: b, Target: FixtureExecutionTarget{}, Evaluator: FixtureEvaluator{}, Epoch: 1, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
}
func eventually(t *testing.T, fn func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if fn() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("condition not reached")
}
func running(t *testing.T, r *Runtime) (context.CancelFunc, <-chan error) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- r.Run(ctx); close(done) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-done:
			if err != nil {
				t.Error(err)
			}
		case <-time.After(2 * time.Second):
			t.Error("owned runtime did not join")
		}
	})
	return cancel, done
}

func TestConfigBackoffAndRuntimeCapacityDrain(t *testing.T) {
	c := DefaultConfig()
	if c.Validate() != nil {
		t.Fatal("defaults invalid")
	}
	c.RenewInterval = c.LeaseDuration
	if c.Validate() == nil {
		t.Fatal("renew>=lease accepted")
	}
	bk := backoff{minimum: time.Millisecond, maximum: 4 * time.Millisecond}
	for _, want := range []time.Duration{time.Millisecond, 2 * time.Millisecond, 4 * time.Millisecond, 4 * time.Millisecond} {
		if got := bk.next(false); got != want {
			t.Fatal(got, want)
		}
	}
	if bk.next(true) != time.Millisecond {
		t.Fatal("progress did not reset backoff")
	}
	b := newBackend(5, FixturePlan{Mode: "DELAY", DelayMilliseconds: 120})
	b.stuck = b.attempts[0].ID
	r := testRuntime(b)
	r.Config.ExecutionConcurrency = 2
	cancel, done := running(t, r)
	eventually(t, func() bool { b.mu.Lock(); defer b.mu.Unlock(); return b.active == 2 })
	cancel()
	eventually(t, func() bool { r.admission.Lock(); defer r.admission.Unlock(); return !r.accepting })
	b.mu.Lock()
	claimsAtStop := b.claims
	b.mu.Unlock()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("drain hung")
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.maxActive != 2 || b.claims != claimsAtStop || b.finalizes != 2 || b.renewals < 2 || b.attempts[0].Status != "PENDING" {
		t.Fatalf("capacity/fairness/drain: %+v", b)
	}
}

type lateTarget struct{ calls atomic.Int64 }

func (t *lateTarget) Execute(ctx context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	t.calls.Add(1)
	<-ctx.Done()
	return (FixtureExecutionTarget{}).Execute(context.Background(), s, q)
}

type lateEvaluator struct{}

func (lateEvaluator) Evaluate(ctx context.Context, in EvaluationInput) (EvaluationOutput, error) {
	<-ctx.Done()
	v := errorValue(in.Work, in.Attempt, "late")
	return EvaluationOutput{Value: v}, nil
}

func TestOwnershipLossAndTransientDatabaseFailure(t *testing.T) {
	for _, evaluation := range []bool{false, true} {
		t.Run(map[bool]string{false: "execution", true: "evaluator"}[evaluation], func(t *testing.T) {
			b := newBackend(1, FixturePlan{Mode: "SUCCESS"})
			b.lost = true
			r := testRuntime(b)
			if evaluation {
				artifact := ev.Binding{Ref: "unit", Schema: "v1"}
				b.attempts[0].Metadata.Observation.Artifact = &artifact
				b.work = ev.Work{ID: asset.NewID(), Status: "PENDING", Metadata: ev.WorkMetadata{CompletionMode: "NORMAL", Spec: ev.EvaluatorSpec{Definition: metric.EvaluatorDefinition{Budget: metric.Budget{TotalMilliseconds: 1000}}}}}
				r.Config.ExecutionConcurrency = 0
				r.Config.EvaluatorConcurrency = 1
				r.Evaluator = lateEvaluator{}
			} else {
				r.Target = &lateTarget{}
			}
			cancel, _ := running(t, r)
			eventually(t, func() bool { return r.Counters.OwnershipLost.Load() == 1 })
			cancel()
			eventually(t, func() bool { b.mu.Lock(); defer b.mu.Unlock(); return b.renewals > 0 })
			b.mu.Lock()
			defer b.mu.Unlock()
			if b.finalizes != 0 {
				t.Fatal("late result submitted after ownership loss")
			}
		})
	}
	t.Run("temporary_DB_error_retries_only_DB", func(t *testing.T) {
		b := newBackend(1, FixturePlan{Mode: "DELAY", DelayMilliseconds: 65})
		b.temporaryRenew = true
		b.temporaryFinalize = true
		r := testRuntime(b)
		cancel, _ := running(t, r)
		eventually(t, func() bool { b.mu.Lock(); defer b.mu.Unlock(); return b.active == 0 && b.claims == 1 })
		cancel()
		b.mu.Lock()
		defer b.mu.Unlock()
		if b.finalizes != 2 || b.attempts[0].Outcome != ev.Success || r.Counters.RenewalErrors.Load() != 1 {
			t.Fatal("DB error fabricated failure or duplicated execution")
		}
	})
	t.Run("lease_uncertainty_cancels_without_finalize", func(t *testing.T) {
		b := newBackend(1, FixturePlan{Mode: "SUCCESS"})
		b.dbFailure = true
		r := testRuntime(b)
		r.Target = &lateTarget{}
		cancel, _ := running(t, r)
		eventually(t, func() bool { return r.Counters.OwnershipLost.Load() == 1 })
		cancel()
		b.mu.Lock()
		defer b.mu.Unlock()
		if b.finalizes != 0 {
			t.Fatal("uncertain owner finalized")
		}
	})
}

func TestDrainTimeoutAndPanicBoundary(t *testing.T) {
	b := newBackend(1, FixturePlan{Mode: "BLOCK"})
	r := testRuntime(b)
	r.Config.DrainTimeout = 15 * time.Millisecond
	cancel, _ := running(t, r)
	eventually(t, func() bool { b.mu.Lock(); defer b.mu.Unlock(); return b.attempts[0].Status == "RUNNING" })
	cancel()
	eventually(t, func() bool { b.mu.Lock(); defer b.mu.Unlock(); return b.finalizes == 1 })
	b.mu.Lock()
	if b.attempts[0].Outcome != ev.Unknown {
		t.Fatal("local cancel fabricated CANCELLED/FAILED")
	}
	b.mu.Unlock()
	p := newBackend(2, FixturePlan{Mode: "PANIC"})
	rr := testRuntime(p)
	stop, _ := running(t, rr)
	eventually(t, func() bool { return rr.Counters.Panics.Load() == 2 })
	stop()
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.finalizes != 0 || p.claims != 2 {
		t.Fatal("panic polluted truth or killed runtime")
	}
}

func TestControlledFixturePlans(t *testing.T) {
	for _, mode := range []string{"SUCCESS", "FAILURE", "CONFIRMED_TIMEOUT", "CONFIRMED_CANCELLED", "UNKNOWN", "BLOCK", "DELAY"} {
		b := newBackend(1, FixturePlan{Mode: mode, ConfirmedCancellation: mode == "BLOCK"})
		ctx, cancel := context.WithCancel(context.Background())
		if mode == "BLOCK" {
			cancel()
		}
		out, err := (FixtureExecutionTarget{}).Execute(ctx, b.scope, b.attempts[0].Request)
		cancel()
		if err != nil || out.Validate(b.scope.ProjectID, b.attempts[0].RunID, b.attempts[0].ID, b.attempts[0].RequestID) != nil {
			t.Fatal(mode, out, err)
		}
	}
	for _, mode := range []string{"PASS", "FAIL", "INCONCLUSIVE", "ERROR", "DELAY", "RETRYABLE"} {
		b := newBackend(1, FixturePlan{Mode: "SUCCESS"})
		out, _ := (FixtureExecutionTarget{}).Execute(context.Background(), b.scope, b.attempts[0].Request)
		b.attempts[0].Metadata.Observation = out
		config, _ := asset.Freeze(FixturePlan{Mode: mode})
		definition := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: "v1", ImplementationRef: FixtureImplementation, SchemaVersion: "v1", Availability: asset.ContractOnly, OutputMetrics: []asset.Ref{{EntityID: asset.NewID(), Version: "v1"}}, Applicability: asset.Applicability{RuleRef: "v1"}, Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 4096}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 2}, Normalization: "v1", Config: config}
		work := ev.Work{Metadata: ev.WorkMetadata{Spec: ev.EvaluatorSpec{Definition: definition}}}
		result, err := (FixtureEvaluator{}).Evaluate(context.Background(), EvaluationInput{Attempt: b.attempts[0], Work: work})
		if err != nil || (mode == "RETRYABLE" && !result.Retryable) || ((mode == "ERROR" || mode == "INCONCLUSIVE") && result.Value.Score != nil) {
			t.Fatal(mode, result, err)
		}
	}
}
