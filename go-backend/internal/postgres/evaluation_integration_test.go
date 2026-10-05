//go:build integration

package postgres_test

import (
	"context"
	"encoding/json"
	"fmt"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"sync"
	"testing"
	"time"
)

func g2Writer(t *testing.T, d *database) int64 {
	t.Helper()
	ctx := context.Background()
	for _, m := range []struct{ mode, writer string }{{"PYTHON_DRAINING", "PYTHON"}, {"BARRIER", "NONE"}, {"GO_ACTIVE", "GO"}} {
		if _, e := d.pool.Exec(ctx, "UPDATE evaluation_writer_control SET mode=$1,active_writer=$2,writer_epoch=writer_epoch+1,operator_ref='isolated-test',cutover_id=$3,changed_at=clock_timestamp()", m.mode, m.writer, asset.NewID()); e != nil {
			t.Fatal(e)
		}
	}
	return 4
}
func g2Pool(t *testing.T, d *database) *pgxpool.Pool {
	p := d.runtimePool(t)
	var role string
	if e := p.QueryRow(context.Background(), "SELECT current_user").Scan(&role); e != nil {
		t.Fatal(e)
	}
	_, e := d.pool.Exec(context.Background(), `GRANT INSERT,UPDATE ON evaluation_runs,evaluation_attempts,evaluation_evaluator_works TO `+pgx.Identifier{role}.Sanitize()+`; GRANT INSERT ON evaluation_results TO `+pgx.Identifier{role}.Sanitize()+`; GRANT UPDATE(domain) ON evaluation_writer_control TO `+pgx.Identifier{role}.Sanitize())
	if e != nil {
		t.Fatal(e)
	}
	return p
}

type kernelFixture struct {
	db  *database
	k   postgres.Evaluation
	s   ev.Scope
	cmd ev.CreateRun
}

func fixture(t *testing.T) *kernelFixture {
	return fixtureWithWriter(t, true)
}
func fixtureWithWriter(t *testing.T, activate bool) *kernelFixture {
	t.Helper()
	d := newDatabase(t)
	d.migrate(t, "head")
	epoch := int64(1)
	if activate {
		epoch = g2Writer(t, d)
	}
	a := seedProject(t, d.pool, asset.NewID())
	pool := g2Pool(t, d)
	ctx := context.Background()
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: pool}, Datasets: postgres.Datasets{Pool: pool}, Suites: postgres.Suites{Pool: pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: pool}}
	cases := catalog.CaseService{Store: readers.Cases}
	datasets := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	metrics := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
	evaluators := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	ref := func() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "v1"} }
	c, dataset, m := ref(), ref(), ref()
	source := asset.Source{Kind: "TEST", Ref: "g2-controlled.v1", Principal: a.Principal}
	must := func(e error) {
		if e != nil {
			t.Fatal(e)
		}
	}
	_, e := cases.CreateCase(ctx, a, asset.Create{ID: c.EntityID, Name: "case"})
	must(e)
	_, e = cases.PublishCaseVersion(ctx, a, asset.Publish[catalog.CaseContent]{Ref: c, Body: testCase(), Source: source})
	must(e)
	_, e = datasets.CreateDataset(ctx, a, asset.Create{ID: dataset.EntityID, Name: "dataset"})
	must(e)
	_, e = datasets.PublishDatasetVersion(ctx, a, asset.Publish[catalog.DatasetContent]{Ref: dataset, Body: catalog.DatasetContent{Cases: []asset.Ref{c}}, Source: source})
	must(e)
	body := metric.Builtins()[0].Definition
	body.ValueType = metric.Scalar
	body.Range = &[2]float64{-2, 2}
	_, e = metrics.CreateMetricDefinition(ctx, a, asset.Create{ID: m.EntityID, Name: "scalar"})
	must(e)
	_, e = metrics.PublishMetricDefinitionVersion(ctx, a, asset.Publish[metric.Definition]{Ref: m, Body: body, Source: source})
	must(e)
	app := asset.Applicability{RuleRef: "controlled.v1"}
	bindings := []catalog.EvaluatorBinding{}
	for i := 0; i < 2; i++ {
		r := ref()
		_, e = evaluators.CreateEvaluatorDefinition(ctx, a, asset.Create{ID: r.EntityID, Name: fmt.Sprint("e", i)})
		must(e)
		definition := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: "case.v1", OutputMetrics: []asset.Ref{m}, Applicability: app, Availability: asset.ContractOnly, ImplementationRef: fmt.Sprint("controlled-fixture.v", i), SchemaVersion: "result.v1", Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 4096}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 2}, Normalization: "null-preserving.v1"}
		if i == 1 {
			prompt := ref()
			definition.Kind = metric.LLMJudge
			definition.PromptRef = &prompt
			definition.Model = &metric.ModelBinding{Provider: "fixture", Model: "fixture", Revision: "v1"}
			definition.Budget.MaxProviderCalls = 2
		}
		_, e = evaluators.PublishEvaluatorDefinitionVersion(ctx, a, asset.Publish[metric.EvaluatorDefinition]{Ref: r, Body: definition, Source: source})
		must(e)
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: r, Metrics: []asset.Ref{m}, Required: i == 0, Applicability: app})
	}
	snapshot, e := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, a, ev.BuildRunSnapshot{Dataset: &dataset, Evaluators: bindings})
	must(e)
	caps := ev.EvaluatorExecutionCapability{}
	for _, spec := range snapshot.Input().Evaluators {
		caps.Bindings = append(caps.Bindings, ev.Capability{Evaluator: spec.Identity.Ref, ImplementationRef: spec.Definition.ImplementationRef, DefinitionDigest: spec.Identity.ContentDigest})
	}
	subject, _ := asset.ParseJSON([]byte(`{"agent":"fixture","revision":"v1"}`))
	return &kernelFixture{d, postgres.Evaluation{Pool: pool, Capabilities: caps}, ev.Scope{Scope: a, Epoch: epoch, Create: true, Execute: true, Evaluate: true, Coordinate: true, Repair: true, Operator: true}, ev.CreateRun{CommandID: asset.NewID(), Snapshot: snapshot, Target: ev.Target{ID: "controlled", Kind: "FIXTURE", Version: "v1", TimeoutMilliseconds: 1000}, Subject: subject, Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}}
}
func reply(t *testing.T, r ev.Reply, e error, want ev.Code) ev.Reply {
	t.Helper()
	if e != nil || r.Code != want {
		t.Fatalf("want %s got %+v err %v", want, r, e)
	}
	return r
}
func create(t *testing.T, f *kernelFixture) ev.Owned {
	t.Helper()
	c := f.cmd
	c.CommandID = asset.NewID()
	r, e := f.k.CreateRun(context.Background(), f.s, c)
	reply(t, r, e, ev.Applied)
	var id string
	if e = f.db.pool.QueryRow(context.Background(), "SELECT id::text FROM evaluation_attempts WHERE run_id=$1", r.ID).Scan(&id); e != nil {
		t.Fatal(e)
	}
	return ev.Owned{RunID: r.ID, AttemptID: id}
}
func start(t *testing.T, f *kernelFixture, o ev.Owned, d time.Duration) ev.Owned {
	t.Helper()
	r, e := f.k.ClaimExecutionAttempt(context.Background(), f.s, o, "execution", d)
	reply(t, r, e, ev.Applied)
	o.Token = r.Token
	r, e = f.k.StartExecutionAttempt(context.Background(), f.s, o)
	reply(t, r, e, ev.Applied)
	return o
}
func outcome(t *testing.T, f *kernelFixture, o ev.Owned, kind ev.OutcomeKind) ev.FinalizeOutcome {
	t.Helper()
	var q string
	if e := f.db.pool.QueryRow(context.Background(), "SELECT execution_request_id FROM evaluation_attempts WHERE id=$1", o.AttemptID).Scan(&q); e != nil {
		t.Fatal(e)
	}
	out := ev.Outcome{Kind: kind, RequestID: q, Source: "controlled-receipt", RemoteID: o.AttemptID, Protocol: "fixture.v1", DispatchCertainty: "REMOTE_ACCEPTED", TerminalCertainty: "REMOTE_CONFIRMED"}
	if kind == ev.Success {
		body, _ := asset.ParseJSON([]byte(`{"answer":"fixture","float":1.0,"negative_zero":-0.0}`))
		out.Artifact = &ev.Binding{ProjectID: f.s.ProjectID, RunID: o.RunID, AttemptID: o.AttemptID, RequestID: q, Ref: "fixture-artifact", Digest: body.Digest(), Schema: "output.v1", Availability: "AVAILABLE", Body: body}
	} else {
		out.ErrorCategory = "CONFIRMED"
		out.Reason = "fixture failure"
		if kind == ev.Unknown {
			out.TerminalCertainty = "UNCONFIRMED"
		}
	}
	return ev.FinalizeOutcome{Owned: o, CommandID: asset.NewID(), Outcome: out}
}
func works(t *testing.T, f *kernelFixture, o ev.Owned) []ev.Work {
	t.Helper()
	rows, e := f.db.pool.Query(context.Background(), "SELECT to_jsonb(w) FROM evaluation_evaluator_works w WHERE run_id=$1 ORDER BY evaluator_id", o.RunID)
	if e != nil {
		t.Fatal(e)
	}
	defer rows.Close()
	out := []ev.Work{}
	for rows.Next() {
		var b []byte
		var w ev.Work
		if e = rows.Scan(&b); e == nil {
			e = json.Unmarshal(b, &w)
		}
		if e != nil {
			t.Fatal(e)
		}
		out = append(out, w)
	}
	return out
}
func result(t *testing.T, f *kernelFixture, w ev.Work, owner ev.Owned) ev.FinalizeResult {
	t.Helper()
	var metadata []byte
	if e := f.db.pool.QueryRow(context.Background(), "SELECT outcome_metadata FROM evaluation_attempts WHERE id=$1", w.AttemptID).Scan(&metadata); e != nil {
		t.Fatal(e)
	}
	var a ev.AttemptMetadata
	if e := json.Unmarshal(metadata, &a); e != nil {
		t.Fatal(e)
	}
	if a.ObservationBytes != nil {
		if e := json.Unmarshal(a.ObservationBytes, &a.Observation); e != nil {
			t.Fatal(e)
		}
	}
	return ev.FinalizeResult{Owned: owner, CommandID: asset.NewID(), ResultID: asset.NewID(), Value: ev.ResultValue{Verdict: "ERROR", SourceVerdict: "ERROR", SourceError: "controlled", Reason: "fixture closure", SpecDigest: w.SpecDigest, InputDigest: f.cmd.Snapshot.Input().Manifest[0].Identity.ContentDigest, Artifact: *a.Observation.Artifact, Evidence: a.Observation.Evidence}}
}
func forceExpiry(t *testing.T, f *kernelFixture, table, id string) {
	t.Helper()
	if table != "evaluation_attempts" && table != "evaluation_evaluator_works" {
		t.Fatal("unsafe test table")
	}
	_, e := f.db.pool.Exec(context.Background(), "UPDATE "+table+" SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1", id)
	if e != nil {
		t.Fatal(e)
	}
}

func TestG2ExecutionAndAtomicHandoff(t *testing.T) {
	f := fixture(t)
	ctx := context.Background()
	t.Run("availability_create_idempotency_frozen_roundtrip", func(t *testing.T) {
		none := f.k
		none.Capabilities = ev.EvaluatorExecutionCapability{}
		r, e := none.CreateRun(ctx, f.s, f.cmd)
		reply(t, r, e, ev.Rejected)
		r, e = f.k.CreateRun(ctx, f.s, f.cmd)
		original := reply(t, r, e, ev.Applied)
		r, e = f.k.CreateRun(ctx, f.s, f.cmd)
		reply(t, r, e, ev.AlreadyApplied)
		if r.ID != original.ID {
			t.Fatal("changed identity")
		}
		changed := f.cmd
		changed.Target.Version = "different"
		r, e = f.k.CreateRun(ctx, f.s, changed)
		reply(t, r, e, ev.Conflict)
		var b []byte
		if e = f.db.pool.QueryRow(ctx, "SELECT kernel_bytes FROM evaluation_runs WHERE id=$1", rereadID(original)).Scan(&b); e != nil {
			t.Fatal(e)
		}
		var frozen ev.RunSnapshot
		if e = json.Unmarshal(b, &frozen); e != nil {
			t.Fatal(e)
		}
		if string(frozen.InputBytes) != string(f.cmd.Snapshot.Bytes()) || frozen.InputDigest != f.cmd.Snapshot.Digest() {
			t.Fatal("frozen input/digest changed")
		}
	})
	t.Run("V01_two_independent_sessions_claim", func(t *testing.T) {
		o := create(t, f)
		var wg sync.WaitGroup
		codes := make(chan ev.Code, 2)
		for i := 0; i < 2; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				r, e := f.k.ClaimExecutionAttempt(ctx, f.s, o, "worker", time.Minute)
				if e != nil {
					codes <- "ERROR"
				} else {
					codes <- r.Code
				}
			}()
		}
		wg.Wait()
		close(codes)
		counts := map[ev.Code]int{}
		for c := range codes {
			counts[c]++
		}
		if counts[ev.Applied] != 1 || counts[ev.NotClaimed] != 1 {
			t.Fatal(counts)
		}
	})
	t.Run("V02_V03_V04_wrong_expired_old_token", func(t *testing.T) {
		o := start(t, f, create(t, f), time.Minute)
		bad := o
		bad.Token = asset.NewID()
		r, e := f.k.RenewExecutionLease(ctx, f.s, bad, time.Minute)
		reply(t, r, e, ev.OwnershipLost)
		r, e = f.k.StartExecutionAttempt(ctx, f.s, bad)
		reply(t, r, e, ev.OwnershipLost)
		forceExpiry(t, f, "evaluation_attempts", o.AttemptID)
		c := outcome(t, f, o, ev.Success)
		r, e = f.k.FinalizeExecutionOutcome(ctx, f.s, c)
		reply(t, r, e, ev.OwnershipLost)
		r, e = f.k.RenewExecutionLease(ctx, f.s, o, time.Minute)
		reply(t, r, e, ev.OwnershipLost)
		r, e = f.k.ExpireExecutionAttempt(ctx, f.s, o)
		reply(t, r, e, ev.Applied)
		r, e = f.k.FinalizeExecutionOutcome(ctx, f.s, c)
		reply(t, r, e, ev.OwnershipLost)
		r, e = f.k.ClaimExecutionAttempt(ctx, f.s, o, "reclaim", time.Minute)
		reply(t, r, e, ev.NotClaimed)
		r, e = f.k.CoordinateRun(ctx, f.s, o.RunID)
		reply(t, r, e, ev.Applied)
		if r.Status != "OUTCOME_UNKNOWN" {
			t.Fatal(r)
		}
	})
	t.Run("V05_crash_window_A", func(t *testing.T) {
		o := start(t, f, create(t, f), time.Minute)
		spec := f.cmd.Snapshot.Input().Evaluators[1].Identity.Ref.EntityID
		_, e := f.db.pool.Exec(ctx, fmt.Sprintf(`CREATE FUNCTION g2_test_fail_slot() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.evaluator_id='%s' THEN RAISE EXCEPTION 'injected slot failure'; END IF; RETURN NEW; END $$; CREATE TRIGGER g2_test_fail_slot BEFORE INSERT ON evaluation_evaluator_works FOR EACH ROW EXECUTE FUNCTION g2_test_fail_slot()`, spec))
		if e != nil {
			t.Fatal(e)
		}
		c := outcome(t, f, o, ev.Success)
		_, e = f.k.FinalizeExecutionOutcome(ctx, f.s, c)
		if e == nil {
			t.Fatal("expected transaction failure")
		}
		var state string
		f.db.pool.QueryRow(ctx, "SELECT status FROM evaluation_attempts WHERE id=$1", o.AttemptID).Scan(&state)
		if state != "RUNNING" || len(works(t, f, o)) != 0 {
			t.Fatal("partial SUCCESS escaped rollback")
		}
		if _, e = f.db.pool.Exec(ctx, "DROP TRIGGER g2_test_fail_slot ON evaluation_evaluator_works; DROP FUNCTION g2_test_fail_slot()"); e != nil {
			t.Fatal(e)
		}
		r, e := f.k.FinalizeExecutionOutcome(ctx, f.s, c)
		reply(t, r, e, ev.Applied)
		if len(works(t, f, o)) != 2 {
			t.Fatal("optional slot absent")
		}
		r, e = f.k.FinalizeExecutionOutcome(ctx, f.s, c)
		reply(t, r, e, ev.AlreadyApplied)
		c.Outcome.Source = "different"
		r, e = f.k.FinalizeExecutionOutcome(ctx, f.s, c)
		reply(t, r, e, ev.Conflict)
		r, e = f.k.ReconcileEvaluatorSlots(ctx, f.s, o)
		reply(t, r, e, ev.AlreadyApplied)
	})
	t.Run("V03_lock_wait_crosses_expiry", func(t *testing.T) {
		o := start(t, f, create(t, f), time.Minute)
		_, err := f.db.pool.Exec(ctx, "UPDATE evaluation_attempts SET lease_expires_at=clock_timestamp()+interval '500 milliseconds' WHERE id=$1", o.AttemptID)
		if err != nil {
			t.Fatal(err)
		}
		tx, err := f.k.Pool.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback(ctx)
		if _, err = tx.Exec(ctx, "SELECT id FROM evaluation_runs WHERE id=$1 FOR UPDATE", o.RunID); err != nil {
			t.Fatal(err)
		}
		type answer struct {
			r ev.Reply
			e error
		}
		done := make(chan answer, 1)
		go func() { r, e := f.k.RenewExecutionLease(ctx, f.s, o, time.Minute); done <- answer{r, e} }()
		deadline := time.Now().Add(2 * time.Second)
		waiting := false
		for time.Now().Before(deadline) {
			var count int
			err = f.db.pool.QueryRow(ctx, "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%evaluation_runs r WHERE%'").Scan(&count)
			if err != nil {
				t.Fatal(err)
			}
			if count > 0 {
				waiting = true
				break
			}
			time.Sleep(5 * time.Millisecond)
		}
		if !waiting {
			t.Fatal("independent session never reached run lock")
		}
		time.Sleep(550 * time.Millisecond)
		if err = tx.Commit(ctx); err != nil {
			t.Fatal(err)
		}
		a := <-done
		reply(t, a.r, a.e, ev.OwnershipLost)
	})
}
func rereadID(r ev.Reply) string { return r.ID }

func TestG2EvaluatorResultProvider(t *testing.T) {
	f := fixture(t)
	ctx := context.Background()
	o := start(t, f, create(t, f), time.Minute)
	c := outcome(t, f, o, ev.Success)
	r, e := f.k.FinalizeExecutionOutcome(ctx, f.s, c)
	reply(t, r, e, ev.Applied)
	ws := works(t, f, o)
	for _, w := range ws {
		t.Run(string(w.Metadata.Spec.Definition.Kind), func(t *testing.T) {
			wo := ev.Owned{RunID: o.RunID, AttemptID: o.AttemptID, WorkID: w.ID}
			var wg sync.WaitGroup
			rs := make(chan ev.Reply, 2)
			for i := 0; i < 2; i++ {
				wg.Add(1)
				go func() {
					defer wg.Done()
					r, e := f.k.ClaimEvaluatorWork(ctx, f.s, wo, "judge", time.Minute)
					if e != nil {
						r.Code = "ERROR"
					}
					rs <- r
				}()
			}
			wg.Wait()
			close(rs)
			counts := map[ev.Code]int{}
			for r := range rs {
				counts[r.Code]++
				if r.Code == ev.Applied {
					wo.Token = r.Token
				}
			}
			if counts[ev.Applied] != 1 || counts[ev.NotClaimed] != 1 {
				t.Fatal(counts)
			}
			rc := result(t, f, w, wo)
			old := wo
			forceExpiry(t, f, "evaluation_evaluator_works", w.ID)
			r, e := f.k.FinalizeEvaluationResult(ctx, f.s, rc)
			reply(t, r, e, ev.OwnershipLost)
			r, e = f.k.ExpireEvaluatorWork(ctx, f.s, wo)
			reply(t, r, e, ev.Applied)
			r, e = f.k.ClaimEvaluatorWork(ctx, f.s, wo, "new-judge", time.Minute)
			reply(t, r, e, ev.Applied)
			wo.Token = r.Token
			if wo.Token == old.Token {
				t.Fatal("token reused")
			}
			r, e = f.k.RenewEvaluatorLease(ctx, f.s, old, time.Minute)
			reply(t, r, e, ev.OwnershipLost)
			r, e = f.k.FinalizeEvaluationResult(ctx, f.s, rc)
			reply(t, r, e, ev.OwnershipLost)
			rc.Owned = wo
			badScore := rc
			score := 3.0
			badScore.Value.Score = &score
			r, e = f.k.FinalizeEvaluationResult(ctx, f.s, badScore)
			reply(t, r, e, ev.Rejected)
			r, e = f.k.BeginEvaluatorAttempt(ctx, f.s, wo, asset.NewID(), 1)
			reply(t, r, e, ev.Applied)
			released := wo
			releaseCommand := asset.NewID()
			r, e = f.k.ReleaseEvaluatorWorkRetry(ctx, f.s, released, releaseCommand, "controlled retry")
			reply(t, r, e, ev.Applied)
			r, e = f.k.ReleaseEvaluatorWorkRetry(ctx, f.s, released, releaseCommand, "controlled retry")
			reply(t, r, e, ev.AlreadyApplied)
			r, e = f.k.ReleaseEvaluatorWorkRetry(ctx, f.s, released, releaseCommand, "changed reason")
			reply(t, r, e, ev.Conflict)
			r, e = f.k.ClaimEvaluatorWork(ctx, f.s, wo, "retry-judge", time.Minute)
			reply(t, r, e, ev.Applied)
			wo.Token = r.Token
			rc.Owned = wo
			r, e = f.k.BeginEvaluatorAttempt(ctx, f.s, released, asset.NewID(), 2)
			reply(t, r, e, ev.OwnershipLost)
			r, e = f.k.BeginEvaluatorAttempt(ctx, f.s, wo, asset.NewID(), 2)
			reply(t, r, e, ev.Applied)
			r, e = f.k.ReleaseEvaluatorWorkRetry(ctx, f.s, released, releaseCommand, "controlled retry")
			reply(t, r, e, ev.AlreadyApplied)
			if w.Metadata.Spec.Definition.Kind == metric.LLMJudge {
				spec := w.Metadata.Spec
				digest, _ := ev.Intent(c.Outcome.Evidence)
				call := ev.BeginCall{Owned: wo, Call: ev.ProviderCall{ID: asset.NewID(), Number: 1, EvaluationNumber: 2, Suboperation: "controlled", RequestedProvider: "fixture", RequestedModel: "fixture", PromptRef: spec.Definition.PromptRef, PromptDigest: "fixture-prompt", Schema: spec.Definition.SchemaVersion, InputDigest: rc.Value.InputDigest, EvidenceDigest: digest}}
				r, e = f.k.BeginProviderCall(ctx, f.s, call)
				reply(t, r, e, ev.Applied)
				r, e = f.k.BeginProviderCall(ctx, f.s, call)
				reply(t, r, e, ev.AlreadyApplied)
				changed := call
				changed.Call.Suboperation = "different"
				r, e = f.k.BeginProviderCall(ctx, f.s, changed)
				reply(t, r, e, ev.Conflict)
				finish := ev.FinishCall{Owned: wo, CallID: call.Call.ID, Classification: "RESPONSE_RECEIVED", ActualProvider: "fixture", ActualModel: "fixture", ActualRevision: "v1", ResponseDigest: "opaque-fixture", ResponseRef: "durable-fixture", UsageAvailability: "UNKNOWN", CostAvailability: "UNKNOWN"}
				r, e = f.k.FinishProviderCall(ctx, f.s, finish)
				reply(t, r, e, ev.Applied)
				r, e = f.k.FinishProviderCall(ctx, f.s, finish)
				reply(t, r, e, ev.AlreadyApplied)
				stale := finish
				stale.Owned = old
				r, e = f.k.FinishProviderCall(ctx, f.s, stale)
				reply(t, r, e, ev.OwnershipLost)
				rc.Value.SelectedCallIDs = []string{call.Call.ID}
			}
			bad := rc
			bad.Token = o.Token
			r, e = f.k.FinalizeEvaluationResult(ctx, f.s, bad)
			reply(t, r, e, ev.OwnershipLost)
			// B：Result INSERT 后 Work UPDATE 的 CHECK 失败，两个事实一起回滚。
			_, e = f.db.pool.Exec(ctx, "ALTER TABLE evaluation_evaluator_works ADD CONSTRAINT g2_test_fail_complete CHECK(status<>'COMPLETED') NOT VALID")
			if e != nil {
				t.Fatal(e)
			}
			_, e = f.k.FinalizeEvaluationResult(ctx, f.s, rc)
			if e == nil {
				t.Fatal("constraint failure absent")
			}
			var count int
			f.db.pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_results WHERE id=$1", rc.ResultID).Scan(&count)
			if count != 0 {
				t.Fatal("Result committed without Work")
			}
			_, e = f.db.pool.Exec(ctx, "ALTER TABLE evaluation_evaluator_works DROP CONSTRAINT g2_test_fail_complete")
			if e != nil {
				t.Fatal(e)
			}
			r, e = f.k.FinalizeEvaluationResult(ctx, f.s, rc)
			reply(t, r, e, ev.Applied)
			r, e = f.k.FinalizeEvaluationResult(ctx, f.s, rc)
			reply(t, r, e, ev.AlreadyApplied)
			bad = rc
			bad.Value.Reason = "different"
			r, e = f.k.FinalizeEvaluationResult(ctx, f.s, bad)
			reply(t, r, e, ev.Conflict)
			rejectSQL(t, f.db.pool, "P0001", "UPDATE evaluation_results SET verdict='PASS' WHERE id=$1", rc.ResultID)
			rejectSQL(t, f.db.pool, "P0001", "DELETE FROM evaluation_results WHERE id=$1", rc.ResultID)
			rejectSQL(t, f.db.pool, "55000", "UPDATE evaluation_evaluator_works SET last_error_reason='late' WHERE id=$1", w.ID)
		})
	}
	r, e = f.k.CoordinateRun(ctx, f.s, o.RunID)
	reply(t, r, e, ev.Applied)
	if r.Status != "COMPLETED" {
		t.Fatal("evaluator ERROR should still complete pipeline", r)
	}
	r, e = f.k.CoordinateRun(ctx, f.s, o.RunID)
	reply(t, r, e, ev.AlreadyApplied)
	rejectSQL(t, f.k.Pool, "42501", "UPDATE evaluation_results SET verdict='PASS'")
	rejectSQL(t, f.k.Pool, "42501", "DELETE FROM evaluation_results")
	rejectSQL(t, f.k.Pool, "42501", "TRUNCATE evaluation_results")
	rejectSQL(t, f.k.Pool, "42501", "ALTER TABLE evaluation_results DISABLE TRIGGER ALL")
	state, err := f.k.ReadRunState(ctx, f.s.Scope, o.RunID)
	if err != nil || len(state.Results) != 2 || state.Run.Status != ev.RunCompleted {
		t.Fatal("consistent canonical read", state, err)
	}
	rejectSQL(t, f.k.Pool, "42501", "UPDATE evaluation_writer_control SET writer_epoch=writer_epoch+1")
	rejectSQL(t, f.k.Pool, "42501", "UPDATE evaluation_writer_control SET domain=domain")
	rejectSQL(t, f.k.Pool, "55000", "UPDATE evaluation_runs SET status='RUNNING',finished_at=NULL WHERE id=$1", o.RunID)
	rejectSQL(t, f.db.pool, "55000", "DELETE FROM projects WHERE id=$1", f.s.ProjectID)
}
func TestG2RetryRerunIsolationAndWriter(t *testing.T) {
	f := fixture(t)
	ctx := context.Background()
	f.cmd.Retry = ev.RetryPolicy{Version: "EXPLICIT_RETRY.v1", MaxAttempts: 2, Allowed: []ev.OutcomeKind{ev.Failure}}
	t.Run("V10_retry_vs_coordinate_E", func(t *testing.T) {
		o := start(t, f, create(t, f), time.Minute)
		c := outcome(t, f, o, ev.Failure)
		r, e := f.k.FinalizeExecutionOutcome(ctx, f.s, c)
		reply(t, r, e, ev.Applied)
		retry := ev.Retry{RunID: o.RunID, AttemptID: o.AttemptID, CommandID: asset.NewID(), Reason: "operator authorized", Authorized: true}
		var wg sync.WaitGroup
		var rr, cr ev.Reply
		var re, ce error
		wg.Add(2)
		go func() { defer wg.Done(); rr, re = f.k.CreateExecutionRetry(ctx, f.s, retry) }()
		go func() { defer wg.Done(); cr, ce = f.k.CoordinateRun(ctx, f.s, o.RunID) }()
		wg.Wait()
		if re != nil || ce != nil {
			t.Fatal(re, ce)
		}
		var state string
		var n int
		f.db.pool.QueryRow(ctx, "SELECT status FROM evaluation_runs WHERE id=$1", o.RunID).Scan(&state)
		f.db.pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_attempts WHERE run_id=$1", o.RunID).Scan(&n)
		if rr.Code == ev.Applied {
			if state != "RUNNING" || n != 2 || cr.Code != ev.NotReady {
				t.Fatal(rr, cr, state, n)
			}
			var q, key, parent string
			var no int
			f.db.pool.QueryRow(ctx, "SELECT execution_request_id,idempotency_key,retry_of_attempt_id::text,attempt_no FROM evaluation_attempts WHERE id=$1", rr.ID).Scan(&q, &key, &parent, &no)
			if q == c.Outcome.RequestID || parent != o.AttemptID || no != 2 {
				t.Fatal("lineage")
			}
			again, e := f.k.CreateExecutionRetry(ctx, f.s, retry)
			reply(t, again, e, ev.AlreadyApplied)
			retry.Reason = "different"
			again, e = f.k.CreateExecutionRetry(ctx, f.s, retry)
			reply(t, again, e, ev.Conflict)
		} else {
			if rr.Code != ev.Rejected || state != "FAILED" || n != 1 || cr.Code != ev.Applied {
				t.Fatal(rr, cr, state, n)
			}
		}
	})
	t.Run("V11_rerun_F", func(t *testing.T) {
		o := start(t, f, create(t, f), time.Minute)
		c := outcome(t, f, o, ev.Unknown)
		r, e := f.k.FinalizeExecutionOutcome(ctx, f.s, c)
		reply(t, r, e, ev.Applied)
		r, e = f.k.CreateExecutionRetry(ctx, f.s, ev.Retry{RunID: o.RunID, AttemptID: o.AttemptID, CommandID: asset.NewID(), Reason: "default unknown denied", Authorized: true})
		reply(t, r, e, ev.Rejected)
		r, e = f.k.CoordinateRun(ctx, f.s, o.RunID)
		reply(t, r, e, ev.Applied)
		var before, after string
		f.db.pool.QueryRow(ctx, "SELECT to_jsonb(r)::text FROM evaluation_runs r WHERE id=$1", o.RunID).Scan(&before)
		rerun := ev.Rerun{SourceRunID: o.RunID, CommandID: asset.NewID(), Reason: "explicit fork", Trigger: "operator"}
		r, e = f.k.CreateRunRerun(ctx, f.s, rerun)
		reply(t, r, e, ev.Rejected)
		rerun.UnknownAck = true
		r, e = f.k.CreateRunRerun(ctx, f.s, rerun)
		reply(t, r, e, ev.Applied)
		newID := r.ID
		if newID == o.RunID {
			t.Fatal("old run reused")
		}
		f.db.pool.QueryRow(ctx, "SELECT to_jsonb(r)::text FROM evaluation_runs r WHERE id=$1", o.RunID).Scan(&after)
		if before != after {
			t.Fatal("source changed")
		}
		var same bool
		f.db.pool.QueryRow(ctx, "SELECT n.kernel_bytes=o.kernel_bytes AND n.source_run_id=o.id FROM evaluation_runs n,evaluation_runs o WHERE n.id=$1 AND o.id=$2", newID, o.RunID).Scan(&same)
		if !same {
			t.Fatal("snapshot drift")
		}
		r, e = f.k.CreateRunRerun(ctx, f.s, rerun)
		reply(t, r, e, ev.AlreadyApplied)
		rejectSQL(t, f.db.pool, "55000", "UPDATE evaluation_attempts SET reason='rewrite' WHERE id=$1", o.AttemptID)
		rejectSQL(t, f.db.pool, "55000", "DELETE FROM evaluation_runs WHERE id=$1", o.RunID)
	})
	t.Run("V12_scoped_composite_relations", func(t *testing.T) {
		o := create(t, f)
		foreign := seedProject(t, f.db.pool, asset.NewID())
		bad := f.s
		bad.Scope = foreign
		r, e := f.k.ClaimExecutionAttempt(ctx, bad, o, "cross-project", time.Minute)
		reply(t, r, e, ev.NotFound)
		rejectSQL(t, f.db.pool, "23503", `INSERT INTO evaluation_evaluator_works(id,project_id,run_id,attempt_id,case_id,case_version,evaluator_id,evaluator_version,spec_digest,spec_version,required,status,metadata,contract_kind) SELECT $1,$2,run_id,id,case_id,case_version,'e','v1','digest','v1',true,'PENDING','{}','LEGACY' FROM evaluation_attempts WHERE id=$3`, asset.NewID(), foreign.ProjectID, o.AttemptID)
		rejectSQL(t, f.db.pool, "55000", "UPDATE evaluation_runs SET source_run_id=$2,rerun_reason='cross' WHERE id=$1", o.RunID, asset.NewID())
	})
	t.Run("reconciliation_candidates", func(t *testing.T) {
		for _, kind := range []postgres.CandidateKind{postgres.PendingExecution, postgres.ExpiredExecution, postgres.PendingEvaluator, postgres.ExpiredEvaluator, postgres.RunningRun, postgres.MissingEvaluatorSlots} {
			c, e := f.k.Candidates(ctx, kind, postgres.EvaluationCursor{}, 100)
			if e != nil {
				t.Fatal(kind, e)
			}
			if len(c) > 0 {
				_, e = f.k.Candidates(ctx, kind, c[0].Cursor, 100)
				if e != nil {
					t.Fatal(e)
				}
			}
		}
	})
	t.Run("V18_epoch_barrier", func(t *testing.T) {
		o := create(t, f)
		bad := f.s
		bad.Epoch--
		r, e := f.k.ClaimExecutionAttempt(ctx, bad, o, "stale", time.Minute)
		reply(t, r, e, ev.OwnershipLost)
		for _, x := range []struct{ m, w string }{{"GO_DRAINING", "GO"}, {"BARRIER", "NONE"}} {
			_, e = f.db.pool.Exec(ctx, "UPDATE evaluation_writer_control SET mode=$1,active_writer=$2,writer_epoch=writer_epoch+1", x.m, x.w)
			if e != nil {
				t.Fatal(e)
			}
		}
		s := f.s
		s.Epoch += 2
		r, e = f.k.ClaimExecutionAttempt(ctx, s, o, "barrier", time.Minute)
		reply(t, r, e, ev.Rejected)
	})
}
func TestG2MigrationHistoricalAndBarrierImport(t *testing.T) {
	d := newDatabase(t)
	d.migrate(t, g1Revision)
	ctx := context.Background()
	scope := seedProject(t, d.pool, asset.NewID())
	run, attempt, resultID, token := asset.NewID(), asset.NewID(), asset.NewID(), asset.NewID()
	suite := `{"evaluator_specs":[{"evaluator_id":"legacy-evaluator","evaluator_version":"e1","definition_digest":"original-spec-digest"},{"evaluator_id":"legacy-missing","evaluator_version":"e1","definition_digest":"missing-spec-digest"}]}`
	_, e := d.pool.Exec(ctx, `INSERT INTO evaluation_runs(id,project_id,dataset_id,dataset_version,suite_id,suite_version,execution_target_id,execution_target_kind,target_version_kind,target_version_value,dataset_snapshot,suite_snapshot,execution_target_snapshot,status,metadata,created_at,started_at) VALUES($1,$2,'legacy-dataset','d1','legacy-suite','s1','target','FIXTURE','git','abc','{"semantic_digest":"original-digest"}',$3,'{}','RUNNING','{}',clock_timestamp(),clock_timestamp())`, run, scope.ProjectID, suite)
	if e != nil {
		t.Fatal(e)
	}
	_, e = d.pool.Exec(ctx, `INSERT INTO evaluation_attempts(id,project_id,run_id,case_id,case_version,attempt_no,execution_target_id,execution_target_kind,target_version_kind,target_version_value,execution_request_id,idempotency_key,request_snapshot,status,claim_token,created_at,claimed_at,started_at,finished_at,lease_expires_at,execution_outcome_kind,output_artifact_ref,outcome_evidence_refs,outcome_metadata) VALUES($1,$2,$3,'opaque-case','v1',1,'target','FIXTURE','git','abc','old-request','old-key','{"input_payload":{"x":1.0},"case_snapshot":{"semantic_digest":"original-case-digest"}}','TERMINAL',$4,clock_timestamp(),clock_timestamp(),clock_timestamp(),clock_timestamp(),clock_timestamp(),'SUCCESS','{"artifact_id":"a"}','[]','{"legacy_observation":"unchanged"}')`, attempt, scope.ProjectID, run, token)
	if e != nil {
		t.Fatal(e)
	}
	_, e = d.pool.Exec(ctx, `INSERT INTO evaluation_results(id,project_id,run_id,attempt_id,dataset_id,dataset_version,case_id,case_version,suite_id,suite_version,evaluator_id,evaluator_version,config_ref_kind,config_ref_value,execution_target_id,target_version_kind,target_version_value,execution_request_id,verdict,reason,provenance_completeness,evidence_refs,metadata,created_at) VALUES($1,$2,$3,$4,'legacy-dataset','d1','opaque-case','v1','legacy-suite','s1','legacy-evaluator','e1','cfg','1','target','git','abc','old-request','FAIL','historical','COMPLETE','[]','{"definition_digest":"original-spec-digest"}',clock_timestamp())`, resultID, scope.ProjectID, run, attempt)
	if e != nil {
		t.Fatal(e)
	}
	// 真实 G1 publication 在升级后保持 bytea/digest/内容，非 metadata.create_all。
	cs := catalog.CaseService{Store: postgres.Cases{Pool: d.pool}}
	ref := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	_, e = cs.CreateCase(ctx, scope, asset.Create{ID: ref.EntityID, Name: "before-G2"})
	if e != nil {
		t.Fatal(e)
	}
	v, e := cs.PublishCaseVersion(ctx, scope, asset.Publish[catalog.CaseContent]{Ref: ref, Body: testCase(), Source: asset.Source{Kind: "TEST", Ref: "migration", Principal: scope.Principal}})
	if e != nil {
		t.Fatal(e)
	}
	read := func(table, id string, subtract []string) string {
		var b string
		sql := "SELECT (to_jsonb(x)-$2::text[])::text FROM " + table + " x WHERE id=$1"
		if e := d.pool.QueryRow(ctx, sql, id, subtract).Scan(&b); e != nil {
			t.Fatal(e)
		}
		return b
	}
	runCols := []string{"contract_version", "creation_command_id", "creation_intent_digest", "source_run_id", "rerun_reason", "kernel_snapshot", "kernel_bytes", "writer_epoch"}
	attemptCols := []string{"writer_epoch", "contract_version", "retry_intent_digest", "retry_command_id", "catalog_case_id", "input_origin"}
	resultCols := []string{"work_id", "contract_version", "kernel_value", "kernel_receipt"}
	beforeRun, beforeAttempt, beforeResult := read("evaluation_runs", run, runCols), read("evaluation_attempts", attempt, attemptCols), read("evaluation_results", resultID, resultCols)
	d.migrate(t, "head")
	if read("evaluation_runs", run, runCols) != beforeRun || read("evaluation_attempts", attempt, attemptCols) != beforeAttempt || read("evaluation_results", resultID, resultCols) != beforeResult {
		t.Fatal("old columns/digest/UUID rewritten")
	}
	after, e := cs.GetCaseVersion(ctx, scope, ref)
	if e != nil || string(v.Bytes()) != string(after.Bytes()) || v.ContentDigest() != after.ContentDigest() {
		t.Fatal("G1 asset changed", e)
	}
	var guards, indexes int
	d.pool.QueryRow(ctx, "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'g2_%'").Scan(&guards)
	d.pool.QueryRow(ctx, "SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'g2_%'").Scan(&indexes)
	if guards < 10 || indexes < 5 {
		t.Fatal("missing guards/indexes", guards, indexes)
	}
	activeRun, activeAttempt, oldToken := asset.NewID(), asset.NewID(), asset.NewID()
	_, e = d.pool.Exec(ctx, `INSERT INTO evaluation_runs SELECT (jsonb_populate_record(NULL::evaluation_runs,to_jsonb(r)||jsonb_build_object('id',$2::uuid))).* FROM evaluation_runs r WHERE id=$1`, run, activeRun)
	if e != nil {
		t.Fatal(e)
	}
	_, e = d.pool.Exec(ctx, `INSERT INTO evaluation_attempts SELECT (jsonb_populate_record(NULL::evaluation_attempts,to_jsonb(a)||jsonb_build_object('id',$2::uuid,'run_id',$3::uuid,'claim_token',$4::uuid,'status','RUNNING','execution_outcome_kind',NULL,'output_artifact_ref',NULL,'finished_at',NULL,'lease_expires_at',clock_timestamp()+interval '1 hour'))).* FROM evaluation_attempts a WHERE id=$1`, attempt, activeAttempt, activeRun, oldToken)
	if e != nil {
		t.Fatal(e)
	}
	cutover := asset.NewID()
	terminalRun, terminalAttempt, terminalResult := asset.NewID(), asset.NewID(), asset.NewID()
	_, e = d.pool.Exec(ctx, `INSERT INTO evaluation_runs SELECT (jsonb_populate_record(NULL::evaluation_runs,to_jsonb(r)||jsonb_build_object('id',$2::uuid))).* FROM evaluation_runs r WHERE id=$1`, run, terminalRun)
	if e != nil {
		t.Fatal(e)
	}
	_, e = d.pool.Exec(ctx, `INSERT INTO evaluation_attempts SELECT (jsonb_populate_record(NULL::evaluation_attempts,to_jsonb(a)||jsonb_build_object('id',$2::uuid,'run_id',$3::uuid,'claim_token',$4::uuid))).* FROM evaluation_attempts a WHERE id=$1`, attempt, terminalAttempt, terminalRun, asset.NewID())
	if e != nil {
		t.Fatal(e)
	}
	_, e = d.pool.Exec(ctx, `INSERT INTO evaluation_results SELECT (jsonb_populate_record(NULL::evaluation_results,to_jsonb(x)||jsonb_build_object('id',$2::uuid,'run_id',$3::uuid,'attempt_id',$4::uuid))).* FROM evaluation_results x WHERE id=$1`, resultID, terminalResult, terminalRun, terminalAttempt)
	if e != nil {
		t.Fatal(e)
	}
	_, e = d.pool.Exec(ctx, "UPDATE evaluation_runs SET status='COMPLETED',finished_at=clock_timestamp() WHERE id=$1", terminalRun)
	if e != nil {
		t.Fatal(e)
	}
	terminalBefore := read("evaluation_runs", terminalRun, runCols)
	for _, m := range []struct{ m, w string }{{"PYTHON_DRAINING", "PYTHON"}, {"BARRIER", "NONE"}} {
		_, e = d.pool.Exec(ctx, "UPDATE evaluation_writer_control SET mode=$1,active_writer=$2,writer_epoch=writer_epoch+1,cutover_id=$3", m.m, m.w, cutover)
		if e != nil {
			t.Fatal(e)
		}
	}
	legacy, e := (ev.Builder{Legacy: postgres.LegacyRunInputs{Pool: d.pool}}).BuildLegacyRunSnapshot(ctx, scope, run)
	if e != nil {
		t.Fatal(e)
	}
	in := legacy.Input()
	legacyCase := catalog.CaseContent{}
	legacyCase.Input, _ = asset.ParseJSON([]byte(`{"x":1.0}`))
	in.Manifest = []ev.CaseInput{{Identity: ev.AssetIdentity{ProjectID: scope.ProjectID, Ref: asset.Ref{EntityID: "opaque-case", Version: "v1"}, ContentDigest: "original-case-digest"}, Case: legacyCase}}
	slots := []postgres.LegacySlot{}
	for i, id := range []string{"legacy-evaluator", "legacy-missing"} {
		digest := "original-spec-digest"
		if i == 1 {
			digest = "missing-spec-digest"
		}
		spec := ev.EvaluatorSpec{Identity: ev.AssetIdentity{ProjectID: scope.ProjectID, Ref: asset.Ref{EntityID: id, Version: "e1"}, ContentDigest: digest}, Definition: metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: "legacy-case.v1", OutputMetrics: []asset.Ref{ref}, Applicability: asset.Applicability{RuleRef: "legacy-recovery.v1"}, Availability: asset.ContractOnly, ImplementationRef: "offline-recovery-contract.v1", SchemaVersion: "legacy-result.v1", Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 1024}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}, Normalization: "legacy-null-preserving.v1"}}
		slots = append(slots, postgres.LegacySlot{CaseID: "opaque-case", CaseVersion: "v1", Spec: spec})
		in.Evaluators = append(in.Evaluators, spec)
	}
	snap := ev.RunSnapshot{Contract: ev.DurableContract, Input: in, InputBytes: legacy.Bytes(), InputDigest: legacy.Digest(), InputAlgorithm: legacy.Algorithm(), Target: ev.Target{ID: "target", Kind: "FIXTURE", Version: "abc", TimeoutMilliseconds: 1000}, Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}
	k := postgres.Evaluation{Pool: d.pool}
	s := ev.Scope{Scope: scope, Epoch: 3, Operator: true, Coordinate: true, Repair: true}
	cmd := postgres.LegacyImport{RunID: run, CutoverID: cutover, InventoryRef: "verified-isolated-barrier.v1", CommandID: asset.NewID(), OldWriterRevoked: true, Snapshot: snap, Slots: slots}
	r, e := k.ImportLegacyEvaluationRun(ctx, s, cmd)
	reply(t, r, e, ev.Applied)
	r, e = k.ImportLegacyEvaluationRun(ctx, s, cmd)
	reply(t, r, e, ev.AlreadyApplied)
	activeInput, e := (ev.Builder{Legacy: postgres.LegacyRunInputs{Pool: d.pool}}).BuildLegacyRunSnapshot(ctx, scope, activeRun)
	if e != nil {
		t.Fatal(e)
	}
	activeCmd := cmd
	activeCmd.RunID = activeRun
	activeCmd.CommandID = asset.NewID()
	activeCmd.Snapshot = snap
	activeCmd.Snapshot.Input.Legacy = activeInput.Input().Legacy
	activeCmd.Snapshot.InputBytes = activeInput.Bytes()
	activeCmd.Snapshot.InputDigest = activeInput.Digest()
	r, e = k.ImportLegacyEvaluationRun(ctx, s, activeCmd)
	reply(t, r, e, ev.Applied)
	var oldOutcome, preservedToken string
	d.pool.QueryRow(ctx, "SELECT execution_outcome_kind,claim_token::text FROM evaluation_attempts WHERE id=$1", activeAttempt).Scan(&oldOutcome, &preservedToken)
	if oldOutcome != "OUTCOME_UNKNOWN" || preservedToken != oldToken {
		t.Fatal("barrier did not seal active attempt or changed old token", oldOutcome, preservedToken)
	}
	terminalInput, e := (ev.Builder{Legacy: postgres.LegacyRunInputs{Pool: d.pool}}).BuildLegacyRunSnapshot(ctx, scope, terminalRun)
	if e != nil {
		t.Fatal(e)
	}
	terminalCmd := cmd
	terminalCmd.RunID = terminalRun
	terminalCmd.CommandID = asset.NewID()
	terminalCmd.Snapshot = snap
	terminalCmd.Snapshot.Input.Legacy = terminalInput.Input().Legacy
	terminalCmd.Snapshot.InputBytes = terminalInput.Bytes()
	terminalCmd.Snapshot.InputDigest = terminalInput.Digest()
	r, e = k.ImportLegacyEvaluationRun(ctx, s, terminalCmd)
	reply(t, r, e, ev.Applied)
	var terminalPending, terminalCompleted int
	d.pool.QueryRow(ctx, "SELECT count(*) FILTER(WHERE status='PENDING'),count(*) FILTER(WHERE status='COMPLETED') FROM evaluation_evaluator_works WHERE run_id=$1", terminalRun).Scan(&terminalPending, &terminalCompleted)
	if terminalPending != 0 || terminalCompleted != 1 || read("evaluation_runs", terminalRun, runCols) != terminalBefore {
		t.Fatal("terminal legacy import changed old Run or created new Judge work")
	}
	if read("evaluation_results", resultID, resultCols) != beforeResult || read("evaluation_runs", run, runCols) != beforeRun || read("evaluation_attempts", attempt, attemptCols) != beforeAttempt {
		t.Fatal("maintenance rewrote historical facts")
	}
	var completed, pending int
	d.pool.QueryRow(ctx, "SELECT count(*) FILTER(WHERE status='COMPLETED'),count(*) FILTER(WHERE status='PENDING') FROM evaluation_evaluator_works WHERE run_id=$1", run).Scan(&completed, &pending)
	if completed != 1 || pending != 1 {
		t.Fatal("historical bindings", completed, pending)
	}
	_, e = d.pool.Exec(ctx, "UPDATE evaluation_writer_control SET mode='GO_ACTIVE',active_writer='GO',writer_epoch=writer_epoch+1")
	if e != nil {
		t.Fatal(e)
	}
	s.Epoch++
	r, e = k.ImportLegacyEvaluationRun(ctx, s, cmd)
	reply(t, r, e, ev.Rejected)
	// import receipt 为 legacy Run 提供只读恢复快照，协调仍等待 missing Result。
	r, e = k.CoordinateRun(ctx, s, run)
	reply(t, r, e, ev.NotReady)
	rejectSQL(t, d.pool, "P0001", "UPDATE evaluation_results SET reason='repair' WHERE id=$1", resultID)
}
