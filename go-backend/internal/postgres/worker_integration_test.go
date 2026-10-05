//go:build integration

package postgres_test

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/bootstrap"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/experiment"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/worker"
	"github.com/jackc/pgx/v5"
)

type g3Fixture struct {
	*kernelFixture
	unsupported ev.CreateRun
	builder     ev.Builder
}

func g3(t *testing.T, target worker.FixturePlan, evalPlan worker.FixturePlan) *g3Fixture {
	t.Helper()
	f := fixture(t)
	return g3FromKernel(t, f, target, evalPlan)
}
func g3FromKernel(t *testing.T, f *kernelFixture, target worker.FixturePlan, evalPlan worker.FixturePlan) *g3Fixture {
	t.Helper()
	t.Setenv("APP_ENV", "test")
	var role string
	if err := f.k.Pool.QueryRow(context.Background(), "SELECT current_user").Scan(&role); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.pool.Exec(context.Background(), "GRANT INSERT,UPDATE ON evaluation_experiments,evaluation_experiment_runs TO "+pgx.Identifier{role}.Sanitize()); err != nil {
		t.Fatal(err)
	}
	original := f.cmd
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: f.k.Pool}, Datasets: postgres.Datasets{Pool: f.k.Pool}, Suites: postgres.Suites{Pool: f.k.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: f.k.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: f.k.Pool}}
	service := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	bindings := []catalog.EvaluatorBinding{}
	ctx := context.Background()
	config, _ := asset.Freeze(evalPlan)
	for _, spec := range original.Snapshot.Input().Evaluators {
		ref := spec.Identity.Ref
		ref.Version = "g3"
		definition := spec.Definition
		definition.Config = config
		definition.ImplementationRef = worker.FixtureImplementation
		definition.Budget.TotalMilliseconds = 30000
		definition.Budget.MaxResponseBytes = 16384
		definition.Retry.InitialBackoffMilliseconds = 30
		definition.Retry.MaxBackoffMilliseconds = 60
		if definition.Kind == metric.LLMJudge {
			definition.ImplementationRef = worker.FixtureProviderImplementation
		}
		if _, err := service.PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: ref, Body: definition, Source: asset.Source{Kind: "TEST", Ref: "CONTROLLED_TEST_ONLY", Principal: f.s.Principal}}); err != nil {
			t.Fatal(err)
		}
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: ref, Metrics: spec.Metrics, Required: spec.Required, Applicability: spec.Applicability})
	}
	builder := ev.Builder{Assets: readers}
	dataset := original.Snapshot.Input().Dataset.Ref
	snapshot, err := builder.BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &dataset, Evaluators: bindings})
	if err != nil {
		t.Fatal(err)
	}
	f.cmd.Snapshot = snapshot
	f.cmd.Target.Config, _ = asset.Freeze(target)
	// 原 G2 capability 仅用于创建故意不可完成的旧 fixture Run；binary 不装配它。
	for _, spec := range snapshot.Input().Evaluators {
		f.k.Capabilities.Bindings = append(f.k.Capabilities.Bindings, ev.Capability{Evaluator: spec.Identity.Ref, ImplementationRef: spec.Definition.ImplementationRef, DefinitionDigest: spec.Identity.ContentDigest})
	}
	return &g3Fixture{kernelFixture: f, unsupported: original, builder: builder}
}

type processLog struct {
	mu     sync.Mutex
	buffer bytes.Buffer
}

func (l *processLog) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buffer.Write(p)
}
func (l *processLog) text() string { l.mu.Lock(); defer l.mu.Unlock(); return l.buffer.String() }

type workerProcess struct {
	cmd  *exec.Cmd
	log  *processLog
	done chan struct{}
	err  error
}

func buildWorker(t *testing.T) string {
	t.Helper()
	binary := filepath.Join(t.TempDir(), "worker")
	if runtime.GOOS == "windows" {
		binary += ".exe"
	}
	cmd := exec.Command("go", "build", "-o", binary, "../../cmd/worker")
	if os.Getenv("G3_RACE_WORKER") == "1" {
		cmd = exec.Command("go", "build", "-race", "-o", binary, "../../cmd/worker")
	}
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("worker build: %v\n%s", err, output)
	}
	return binary
}
func runtimeURL(t *testing.T, f *g3Fixture) string {
	t.Helper()
	copyURL := *f.db.url
	var role string
	if err := f.k.Pool.QueryRow(context.Background(), "SELECT current_user").Scan(&role); err != nil {
		t.Fatal(err)
	}
	query := copyURL.Query()
	query.Set("options", "-c role="+role)
	copyURL.RawQuery = query.Encode()
	return copyURL.String()
}
func spawnWorker(t *testing.T, binary string, f *g3Fixture, id string, extra ...string) *workerProcess {
	t.Helper()
	args := []string{"--fixture-only", "--worker-id", id, "--execution-concurrency", "1", "--evaluator-concurrency", "1", "--poll-interval", "10ms", "--max-backoff", "40ms", "--lease-duration", "800ms", "--renew-interval", "100ms", "--db-timeout", "200ms", "--drain-timeout", "2s", "--scan-batch", "1"}
	args = append(args, extra...)
	command := exec.Command(binary, args...)
	// 明确注入独立隔离库与受限 runtime role，连接串不输出到日志。
	for _, v := range os.Environ() {
		if !strings.HasPrefix(v, "DATABASE_URL=") {
			command.Env = append(command.Env, v)
		}
	}
	command.Env = append(command.Env, "DATABASE_URL="+runtimeURL(t, f))
	log := &processLog{}
	command.Stdout = log
	command.Stderr = log
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	p := &workerProcess{cmd: command, log: log, done: make(chan struct{})}
	go func() { p.err = command.Wait(); close(p.done) }()
	t.Logf("spawn worker_id=%s pid=%d", id, command.Process.Pid)
	t.Cleanup(func() {
		select {
		case <-p.done:
		default:
			_ = command.Process.Kill()
			<-p.done
		}
		if strings.Contains(log.text(), "WARNING: DATA RACE") {
			t.Errorf("worker process race detected pid=%d\n%s", command.Process.Pid, log.text())
		}
	})
	await(t, func() bool {
		select {
		case <-p.done:
			t.Fatalf("worker exited early: %v\n%s", p.err, log.text())
		default:
		}
		return strings.Contains(log.text(), "runtime_started")
	})
	return p
}
func (p *workerProcess) kill(t *testing.T) {
	t.Helper()
	if err := p.cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-p.done:
	case <-time.After(5 * time.Second):
		t.Fatal("killed OS worker did not exit")
	}
	t.Logf("OS kill/wait pid=%d exit=%v", p.cmd.Process.Pid, p.err)
}
func await(t *testing.T, fn func() bool) {
	t.Helper()
	deadline := time.Now().Add(12 * time.Second)
	for time.Now().Before(deadline) {
		if fn() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("durable state deadline exceeded")
}
func g3State(t *testing.T, f *g3Fixture, run string) ev.RunState {
	t.Helper()
	s, err := f.k.ReadRunState(context.Background(), f.s.Scope, run)
	if err != nil {
		t.Fatal(err)
	}
	return s
}
func g3Create(t *testing.T, f *g3Fixture, plan *worker.FixturePlan) ev.Owned {
	t.Helper()
	saved := f.cmd
	if plan != nil {
		f.cmd.Target.Config, _ = asset.Freeze(*plan)
	}
	o := create(t, f.kernelFixture)
	f.cmd = saved
	return o
}

func TestG3WorkerProcesses(t *testing.T) {
	binary := buildWorker(t)
	t.Run("M01_M02_competition_execution_kill_UNKNOWN", func(t *testing.T) {
		f := g3(t, worker.FixturePlan{Mode: "BLOCK"}, worker.FixturePlan{Mode: "PASS"})
		o := g3Create(t, f, nil)
		one := spawnWorker(t, binary, f, "compete-one", "--evaluator-concurrency", "0", "--reconcile=false", "--coordinate=false")
		two := spawnWorker(t, binary, f, "compete-two", "--evaluator-concurrency", "0", "--reconcile=false", "--coordinate=false")
		await(t, func() bool { return strings.Count(one.log.text()+two.log.text(), `"msg":"execution_started"`) == 1 })
		state := g3State(t, f, o.RunID)
		a := state.Attempts[0]
		o.Token = *a.Token
		var owner string
		if err := f.db.pool.QueryRow(context.Background(), "SELECT worker_ref FROM evaluation_attempts WHERE id=$1", o.AttemptID).Scan(&owner); err != nil {
			t.Fatal(err)
		}
		if owner == "compete-one" {
			one.kill(t)
		} else {
			two.kill(t)
		}
		scanner := spawnWorker(t, binary, f, "execution-recovery", "--evaluator-concurrency", "0", "--coordinate=false")
		await(t, func() bool { return g3State(t, f, o.RunID).Attempts[0].Outcome == ev.Unknown })
		cmd := outcome(t, f.kernelFixture, o, ev.Success)
		r, e := f.k.FinalizeExecutionOutcome(context.Background(), f.s, cmd)
		reply(t, r, e, ev.OwnershipLost)
		r, e = f.k.ClaimExecutionAttempt(context.Background(), f.s, ev.Owned{RunID: o.RunID, AttemptID: o.AttemptID}, "reclaim-forbidden", time.Second)
		reply(t, r, e, ev.NotClaimed)
		if strings.Contains(scanner.log.text(), "execution_started") {
			t.Fatal("expired execution re-dispatched")
		}
		scanner.kill(t)
		spawnWorker(t, binary, f, "unknown-coordinator", "--execution-concurrency", "0", "--evaluator-concurrency", "0")
		await(t, func() bool { return g3State(t, f, o.RunID).Run.Status == ev.RunUnknown })
	})
	t.Run("M03_M05_M06_success_handoff_and_coordinator_crash", func(t *testing.T) {
		f := g3(t, worker.FixturePlan{Mode: "SUCCESS"}, worker.FixturePlan{Mode: "PASS"})
		o := g3Create(t, f, nil)
		producer := spawnWorker(t, binary, f, "success-producer", "--evaluator-concurrency", "0", "--coordinate=false", "--reconcile=false")
		await(t, func() bool {
			state := g3State(t, f, o.RunID)
			return state.Attempts[0].Outcome == ev.Success && len(state.Works) == 2
		})
		producer.kill(t)
		coordinator := spawnWorker(t, binary, f, "coordinator-before-crash", "--execution-concurrency", "0", "--evaluator-concurrency", "0", "--reconcile=false", "--poll-interval", "10s", "--max-backoff", "10s")
		await(t, func() bool { return strings.Contains(coordinator.log.text(), "coordinator_checked") })
		consumer := spawnWorker(t, binary, f, "result-recovery", "--execution-concurrency", "0", "--coordinate=false")
		await(t, func() bool { return len(g3State(t, f, o.RunID).Results) == 2 })
		if g3State(t, f, o.RunID).Run.Status != ev.RunRunning {
			t.Fatal("test missed coordinator crash window")
		}
		coordinator.kill(t)
		consumer.kill(t)
		spawnWorker(t, binary, f, "coordinator-restarted", "--execution-concurrency", "0", "--evaluator-concurrency", "0")
		await(t, func() bool { return g3State(t, f, o.RunID).Run.Status == ev.RunCompleted })
		state := g3State(t, f, o.RunID)
		for _, result := range state.Results {
			if result.Value.Verdict != "PASS" {
				t.Fatal(result.Value)
			}
		}
	})
	t.Run("M04_evaluator_kill_fresh_token_stale_rejection", func(t *testing.T) {
		f := g3(t, worker.FixturePlan{Mode: "SUCCESS"}, worker.FixturePlan{Mode: "DELAY", DelayMilliseconds: 1500})
		o := g3Create(t, f, nil)
		producer := spawnWorker(t, binary, f, "evaluator-producer", "--evaluator-concurrency", "0", "--coordinate=false")
		await(t, func() bool { return len(g3State(t, f, o.RunID).Works) == 2 })
		producer.kill(t)
		old := spawnWorker(t, binary, f, "evaluator-old", "--execution-concurrency", "0", "--coordinate=false")
		var oldWork ev.Work
		await(t, func() bool {
			for _, w := range g3State(t, f, o.RunID).Works {
				if w.Status == "CLAIMED" && w.CallCount == 1 {
					oldWork = w
					return true
				}
			}
			return false
		})
		oldOwner := ev.Owned{RunID: o.RunID, AttemptID: o.AttemptID, WorkID: oldWork.ID, Token: *oldWork.Token}
		old.kill(t)
		spawnWorker(t, binary, f, "evaluator-new", "--execution-concurrency", "0", "--coordinate=false", "--evaluator-concurrency", "2")
		await(t, func() bool {
			for _, w := range g3State(t, f, o.RunID).Works {
				if w.ID == oldWork.ID && w.Status == "CLAIMED" && w.Token != nil && *w.Token != oldOwner.Token {
					return true
				}
			}
			return false
		})
		stale := result(t, f.kernelFixture, oldWork, oldOwner)
		r, e := f.k.FinalizeEvaluationResult(context.Background(), f.s, stale)
		reply(t, r, e, ev.OwnershipLost)
		r, e = f.k.RenewEvaluatorLease(context.Background(), f.s, oldOwner, time.Second)
		reply(t, r, e, ev.OwnershipLost)
		await(t, func() bool { return len(g3State(t, f, o.RunID).Results) == 2 })
		state := g3State(t, f, o.RunID)
		for _, w := range state.Works {
			if w.ID == oldWork.ID {
				if w.Claims != 2 || w.Evaluations != 2 || len(w.Calls) != 2 || w.Calls[0].Classification != "RESPONSE_UNKNOWN" || w.Calls[1].Classification != "RESPONSE_RECEIVED" {
					t.Fatalf("budget/provenance lost: %+v", w)
				}
			}
		}
	})
	t.Run("M07_cursor_wrap_oldest_stuck_run", func(t *testing.T) {
		f := g3(t, worker.FixturePlan{Mode: "SUCCESS"}, worker.FixturePlan{Mode: "PASS"})
		supported := f.cmd
		f.cmd = f.unsupported
		old := g3Create(t, f, nil)
		f.cmd = supported
		owner := start(t, f.kernelFixture, old, time.Second)
		r, e := f.k.FinalizeExecutionOutcome(context.Background(), f.s, outcome(t, f.kernelFixture, owner, ev.Success))
		reply(t, r, e, ev.Applied)
		later := g3Create(t, f, nil)
		spawnWorker(t, binary, f, "fairness-worker")
		await(t, func() bool { return g3State(t, f, later.RunID).Run.Status == ev.RunCompleted })
		if g3State(t, f, old.RunID).Run.Status != ev.RunRunning {
			t.Fatal("unsupported oldest row should remain running")
		}
	})
	t.Run("M10_panic_runtime_survives_and_scanner_recovers", func(t *testing.T) {
		f := g3(t, worker.FixturePlan{Mode: "PANIC"}, worker.FixturePlan{Mode: "PASS"})
		bad := g3Create(t, f, nil)
		good := g3Create(t, f, &worker.FixturePlan{Mode: "SUCCESS"})
		process := spawnWorker(t, binary, f, "panic-worker")
		await(t, func() bool {
			return g3State(t, f, bad.RunID).Run.Status == ev.RunUnknown && g3State(t, f, good.RunID).Run.Status == ev.RunCompleted
		})
		if !strings.Contains(process.log.text(), "task_panic") || !strings.Contains(process.log.text(), "stack") {
			t.Fatal("panic evidence absent")
		}
		judge := g3(t, worker.FixturePlan{Mode: "SUCCESS"}, worker.FixturePlan{Mode: "PANIC"})
		run := g3Create(t, judge, nil)
		judger := spawnWorker(t, binary, judge, "evaluator-panic-worker")
		await(t, func() bool { return g3State(t, judge, run.RunID).Run.Status == ev.RunCompleted })
		for _, result := range g3State(t, judge, run.RunID).Results {
			if result.Value.Verdict != "ERROR" || result.Value.Score != nil {
				t.Fatal("panic recovery fabricated judge quality")
			}
		}
		if !strings.Contains(judger.log.text(), "task_panic") {
			t.Fatal("evaluator panic boundary absent")
		}
	})
	t.Run("live_execution_and_evaluator_ownership_loss", func(t *testing.T) {
		f := g3(t, worker.FixturePlan{Mode: "DELAY", DelayMilliseconds: 2000}, worker.FixturePlan{Mode: "PASS"})
		o := g3Create(t, f, nil)
		old := spawnWorker(t, binary, f, "live-execution-old", "--coordinate=false")
		await(t, func() bool { return strings.Contains(old.log.text(), "execution_started") })
		forceExpiry(t, f.kernelFixture, "evaluation_attempts", o.AttemptID)
		await(t, func() bool {
			return strings.Contains(old.log.text(), "ownership_lost") && g3State(t, f, o.RunID).Attempts[0].Outcome == ev.Unknown
		})
		if strings.Contains(old.log.text(), "execution_finalized") || len(g3State(t, f, o.RunID).Works) != 0 {
			t.Fatal("late execution escaped fence")
		}
		judge := g3(t, worker.FixturePlan{Mode: "SUCCESS"}, worker.FixturePlan{Mode: "DELAY", DelayMilliseconds: 1500})
		run := g3Create(t, judge, nil)
		before := spawnWorker(t, binary, judge, "live-evaluator-old", "--coordinate=false", "--poll-interval", "1s", "--max-backoff", "1s")
		var work ev.Work
		await(t, func() bool {
			for _, w := range g3State(t, judge, run.RunID).Works {
				if w.Status == "CLAIMED" && w.CallCount == 1 {
					work = w
					return true
				}
			}
			return false
		})
		forceExpiry(t, judge.kernelFixture, "evaluation_evaluator_works", work.ID)
		await(t, func() bool { return strings.Contains(before.log.text(), "ownership_lost") })
		before.kill(t)
		spawnWorker(t, binary, judge, "live-evaluator-recovery", "--coordinate=false")
		await(t, func() bool {
			for _, w := range g3State(t, judge, run.RunID).Works {
				if w.ID == work.ID && w.Status == "CLAIMED" && *w.Token != *work.Token {
					return true
				}
			}
			return false
		})
		staleOwner := ev.Owned{RunID: run.RunID, AttemptID: run.AttemptID, WorkID: work.ID, Token: *work.Token}
		r, e := judge.k.FinalizeEvaluationResult(context.Background(), judge.s, result(t, judge.kernelFixture, work, staleOwner))
		reply(t, r, e, ev.OwnershipLost)
		await(t, func() bool { return len(g3State(t, judge, run.RunID).Results) == 2 })
	})
	t.Run("M11_two_coordinators_unique_retry_and_seal", func(t *testing.T) {
		f := g3(t, worker.FixturePlan{Mode: "FAILURE"}, worker.FixturePlan{Mode: "PASS"})
		f.cmd.Retry = ev.RetryPolicy{Version: "SAFE_AUTO_RETRY.v1", MaxAttempts: 2, Allowed: []ev.OutcomeKind{ev.Failure}, RetrySafeSource: "CONTROLLED_TEST_ONLY"}
		o := g3Create(t, f, nil)
		spawnWorker(t, binary, f, "coordinator-one")
		spawnWorker(t, binary, f, "coordinator-two")
		await(t, func() bool { return g3State(t, f, o.RunID).Run.Status == ev.RunFailed })
		state := g3State(t, f, o.RunID)
		if len(state.Attempts) != 2 || state.Attempts[1].Parent == nil || *state.Attempts[1].Parent != state.Attempts[0].ID || state.Attempts[0].Key == state.Attempts[1].Key {
			t.Fatal("coordinator duplicated retry/seal")
		}
	})
	t.Run("retryable_and_exhausted_fixture_budget", func(t *testing.T) {
		f := g3(t, worker.FixturePlan{Mode: "SUCCESS"}, worker.FixturePlan{Mode: "RETRYABLE", RetryBefore: 1})
		o := g3Create(t, f, nil)
		spawnWorker(t, binary, f, "retryable-worker")
		await(t, func() bool { return g3State(t, f, o.RunID).Run.Status == ev.RunCompleted })
		for _, result := range g3State(t, f, o.RunID).Results {
			if result.Value.Verdict != "PASS" {
				t.Fatal("retryable did not converge")
			}
		}
		for _, w := range g3State(t, f, o.RunID).Works {
			if w.Evaluations != 2 {
				t.Fatal("retry budget missing")
			}
		}
		exhausted := g3(t, worker.FixturePlan{Mode: "SUCCESS"}, worker.FixturePlan{Mode: "RETRYABLE"})
		other := g3Create(t, exhausted, nil)
		spawnWorker(t, binary, exhausted, "exhausted-worker")
		await(t, func() bool { return g3State(t, exhausted, other.RunID).Run.Status == ev.RunCompleted })
		for _, result := range g3State(t, exhausted, other.RunID).Results {
			if result.Value.Verdict != "ERROR" || result.Value.Score != nil {
				t.Fatal("exhausted budget did not close with nullable error")
			}
		}
	})
	// 停机和 Experiment Gate 使用同一真实 binary/PG，Linux 额外发真实 SIGTERM。
	t.Run("M08_M09_capacity_shutdown_and_bounded_drain", func(t *testing.T) { testG3Shutdown(t, binary) })
	t.Run("M12_experiment_repeat_materialization", func(t *testing.T) { testG3Experiment(t, binary) })
}

// Windows 无 POSIX Process.Signal；host 验证同一 Runtime cancellation，Linux 发真实 SIGTERM。
func shutdownSubject(t *testing.T, binary string, f *g3Fixture, drain time.Duration) (*processLog, func(), <-chan struct{}) {
	t.Helper()
	if runtime.GOOS != "windows" {
		p := spawnWorker(t, binary, f, "sigterm-worker", "--drain-timeout", drain.String())
		return p.log, func() {
			if err := p.cmd.Process.Signal(syscall.SIGTERM); err != nil {
				t.Fatal(err)
			}
		}, p.done
	}
	c := worker.DefaultConfig()
	c.WorkerID = "shutdown-parent-runtime"
	c.ExecutionConcurrency = 1
	c.EvaluatorConcurrency = 1
	c.ScanBatch = 1
	c.PollInterval = 10 * time.Millisecond
	c.MaxBackoff = 40 * time.Millisecond
	c.LeaseDuration = 800 * time.Millisecond
	c.RenewInterval = 100 * time.Millisecond
	c.DBTimeout = 200 * time.Millisecond
	c.DrainTimeout = drain
	log := &processLog{}
	ctx, cancel := context.WithCancel(context.Background())
	r, closeDB, err := bootstrap.Worker(ctx, runtimeURL(t, f), c, true, slog.New(slog.NewJSONHandler(log, nil)))
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		defer closeDB()
		if e := r.Run(ctx); e != nil {
			t.Error(e)
		}
	}()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("shutdown runtime not joined")
		}
	})
	await(t, func() bool { return strings.Contains(log.text(), "runtime_started") })
	return log, cancel, done
}
func testG3Shutdown(t *testing.T, binary string) {
	t.Run("independent_capacity_stop_admission_drain_with_renewal", func(t *testing.T) {
		f := g3(t, worker.FixturePlan{Mode: "DELAY", DelayMilliseconds: 1600}, worker.FixturePlan{Mode: "PASS"})
		active := g3Create(t, f, nil)
		ready := g3Create(t, f, nil)
		owned := start(t, f.kernelFixture, ready, time.Second)
		r, e := f.k.FinalizeExecutionOutcome(context.Background(), f.s, outcome(t, f.kernelFixture, owned, ev.Success))
		reply(t, r, e, ev.Applied)
		log, stop, done := shutdownSubject(t, binary, f, 2*time.Second)
		await(t, func() bool {
			return len(g3State(t, f, ready.RunID).Results) == 2 && g3State(t, f, active.RunID).Attempts[0].Status == "RUNNING"
		})
		stop()
		await(t, func() bool { return strings.Contains(log.text(), "admission_stopped") })
		extra := g3Create(t, f, nil)
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Fatal("bounded drain did not join")
		}
		if g3State(t, f, active.RunID).Attempts[0].Outcome != ev.Success || g3State(t, f, extra.RunID).Attempts[0].Status != "PENDING" || !strings.Contains(log.text(), "lease_renew_success") {
			t.Fatal("capacity/drain/admission/renewal contract failed")
		}
	})
	for _, confirmed := range []bool{false, true} {
		t.Run(fmt.Sprintf("drain_timeout_confirmed=%v", confirmed), func(t *testing.T) {
			f := g3(t, worker.FixturePlan{Mode: "BLOCK", ConfirmedCancellation: confirmed}, worker.FixturePlan{Mode: "PASS"})
			o := g3Create(t, f, nil)
			log, stop, done := shutdownSubject(t, binary, f, 50*time.Millisecond)
			await(t, func() bool { return strings.Contains(log.text(), "execution_started") })
			stop()
			select {
			case <-done:
			case <-time.After(5 * time.Second):
				t.Fatal("timeout cleanup hung")
			}
			want := ev.Unknown
			if confirmed {
				want = ev.Cancelled
			}
			if g3State(t, f, o.RunID).Attempts[0].Outcome != want || !strings.Contains(log.text(), "drain_timeout") {
				t.Fatal("local cancel terminal certainty mishandled")
			}
		})
	}
}

func testG3Experiment(t *testing.T, binary string) {
	f := g3(t, worker.FixturePlan{Mode: "FAILURE"}, worker.FixturePlan{Mode: "PASS"})
	f.cmd.Retry = ev.RetryPolicy{Version: "SAFE_AUTO_RETRY.v1", MaxAttempts: 2, Allowed: []ev.OutcomeKind{ev.Failure}, RetrySafeSource: "CONTROLLED_TEST_ONLY"}
	service := experiment.Service{Store: postgres.Experiments{Pool: f.k.Pool}, Kernel: f.k, Reader: f.k, Builder: f.builder, Capabilities: f.k.Capabilities}
	candidate, _ := asset.ParseJSON([]byte(`{"agent":"fixture","candidate":"g3","revision":"v1"}`))
	cmd := experiment.Create{ID: asset.NewID(), Name: "controlled repeat", Candidate: candidate, Repeat: 3, Run: f.cmd}
	ctx := context.Background()
	exp, err := service.Create(ctx, f.s, cmd)
	if err != nil {
		t.Fatal(err)
	}
	if len(exp.Slots) != 3 || exp.CreatedAt.IsZero() {
		t.Fatal("experiment manifest not durable")
	}
	duplicate, err := service.Create(ctx, f.s, cmd)
	if err != nil || duplicate.Slots[0].CommandID != exp.Slots[0].CommandID {
		t.Fatal("duplicate experiment did not return frozen identity", err)
	}
	changed := cmd
	changed.Repeat = 2
	_, err = service.Create(ctx, f.s, changed)
	expectError(t, err, asset.ErrConflict)
	// 实际执行一次 CreateRun 后，故意不 Attach；模拟掉电留下的 durable 窗口。
	runCmd := f.cmd
	runCmd.CommandID = exp.Slots[0].CommandID
	r, e := f.k.CreateRun(ctx, f.s, runCmd)
	reply(t, r, e, ev.Applied)
	exp, err = service.Materialize(ctx, f.s, exp.ID)
	if err != nil {
		t.Fatal(err)
	}
	if *exp.Slots[0].RunID != r.ID {
		t.Fatal("materialization duplicated already committed run")
	}
	if _, err = service.Materialize(ctx, f.s, exp.ID); err != nil {
		t.Fatal(err)
	}
	items, err := service.List(ctx, f.s.Scope, 10)
	if err != nil || len(items) != 1 {
		t.Fatal("experiment list", err)
	}
	foreign := f.s.Scope
	foreign.ProjectID = asset.NewID()
	_, err = service.Get(ctx, foreign, exp.ID)
	expectError(t, err, asset.ErrNotFound)
	rejectSQL(t, f.k.Pool, "55000", "UPDATE evaluation_experiments SET name='changed' WHERE id=$1", exp.ID)
	rejectSQL(t, f.k.Pool, "55000", "UPDATE evaluation_experiment_runs SET run_id=run_id WHERE experiment_id=$1", exp.ID)
	spawnWorker(t, binary, f, "experiment-worker")
	await(t, func() bool {
		view, err := service.Get(ctx, f.s.Scope, exp.ID)
		if err != nil {
			t.Fatal(err)
		}
		return view.Status == "FINISHED"
	})
	view, err := service.Get(ctx, f.s.Scope, exp.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(view.Runs) != 3 {
		t.Fatal("repeat count is not Run count")
	}
	ids := map[string]bool{}
	for _, state := range view.Runs {
		if ids[state.Run.ID] || len(state.Attempts) != 2 || state.Run.Status != ev.RunFailed {
			t.Fatal("repeat/retry lifecycle conflated")
		}
		ids[state.Run.ID] = true
	}
	// optional baseline 是同项目引用；不产生 comparison/Gate。
	baseCmd := cmd
	baseCmd.ID = asset.NewID()
	baseCmd.Repeat = 1
	baseCmd.Baseline = &experiment.Baseline{ExperimentID: exp.ID}
	if _, err = service.Create(ctx, f.s, baseCmd); err != nil {
		t.Fatal(err)
	}
	baseCmd.ID = asset.NewID()
	baseCmd.Baseline = &experiment.Baseline{RunID: asset.NewID()}
	_, err = service.Create(ctx, f.s, baseCmd)
	expectError(t, err, asset.ErrNotFound)
	// G2 数据升级到 G3 保持完整行，schema / runtime role 在真实 PG bootstrap 校验。
	if _, err = f.k.VerifyWorker(ctx, bootstrap.WorkerSchema); err != nil {
		t.Fatal(err)
	}
	if _, err = (postgres.Evaluation{Pool: f.db.pool}).VerifyWorker(ctx, bootstrap.WorkerSchema); err == nil {
		t.Fatal("superuser runtime accepted")
	}
	if _, err = f.k.VerifyWorker(ctx, ev.SchemaVersion); err == nil {
		t.Fatal("schema mismatch accepted")
	}
}
