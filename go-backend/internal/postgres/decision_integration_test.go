//go:build integration

package postgres_test

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	ob "agentevalops/go-backend/internal/observation"
	"agentevalops/go-backend/internal/postgres"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type g6Fixture struct {
	*g5Fixture
	gate           postgres.Decisions
	dataset        asset.Ref
	bindings       []catalog.EvaluatorBinding
	refs           []asset.Ref
	runtimeCommand ev.CreateRun
}

func g6(t *testing.T) *g6Fixture {
	t.Helper()
	f := g5(t)
	ctx := context.Background()
	var role string
	_ = f.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&role)
	_, e := f.db.pool.Exec(ctx, `GRANT INSERT,UPDATE ON evaluation_policies,evaluation_policy_versions TO `+pgx.Identifier{role}.Sanitize()+`; GRANT INSERT ON evaluation_comparisons,evaluation_gate_receipts,evaluation_case_comparisons TO `+pgx.Identifier{role}.Sanitize())
	if e != nil {
		t.Fatal(e)
	}
	x := &g6Fixture{g5Fixture: f, gate: postgres.Decisions{Pool: f.k.Pool}, dataset: asset.Ref{EntityID: asset.NewID(), Version: "g6"}, runtimeCommand: f.cmd}
	readers := g6Readers(x)
	source := asset.Source{Kind: "TEST", Ref: "G6_CONTROLLED_CANONICAL_FACTS", Principal: f.s.Principal}
	must := func(e error) {
		if e != nil {
			t.Fatal(e)
		}
	}
	cases := catalog.CaseService{Store: readers.Cases}
	ds := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	metrics := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
	evals := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	cr := []asset.Ref{}
	for i := 0; i < 10; i++ {
		r := asset.Ref{EntityID: asset.NewID(), Version: "g6"}
		c := testCase()
		c.Criticality = catalog.Normal
		if i == 0 {
			c.Criticality = catalog.Critical
		}
		_, e = cases.CreateCase(ctx, f.s.Scope, asset.Create{ID: r.EntityID, Name: fmt.Sprint("G6 case", i)})
		must(e)
		_, e = cases.PublishCaseVersion(ctx, f.s.Scope, asset.Publish[catalog.CaseContent]{Ref: r, Body: c, Source: source})
		must(e)
		cr = append(cr, r)
	}
	_, e = ds.CreateDataset(ctx, f.s.Scope, asset.Create{ID: x.dataset.EntityID, Name: "G6 dataset"})
	must(e)
	_, e = ds.PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: x.dataset, Body: catalog.DatasetContent{Cases: cr}, Source: source})
	must(e)
	for i, builtin := range []int{0, 13, 15} {
		r := asset.Ref{EntityID: asset.NewID(), Version: "g6"}
		d := metric.Builtins()[builtin].Definition
		if i == 0 {
			d.Name = "task_success.v1"
		} else {
			d.Aggregation = "mean with coverage"
		}
		_, e = metrics.CreateMetricDefinition(ctx, f.s.Scope, asset.Create{ID: r.EntityID, Name: d.Name})
		must(e)
		_, e = metrics.PublishMetricDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.Definition]{Ref: r, Body: d, Source: source})
		must(e)
		x.refs = append(x.refs, r)
		er := asset.Ref{EntityID: asset.NewID(), Version: "g6"}
		cfg, _ := asset.Freeze(map[string]string{"metric": d.Name})
		definition := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: "g6-controlled.v1", OutputMetrics: []asset.Ref{r}, Applicability: asset.Applicability{RuleRef: "g6.v1"}, Availability: asset.ContractOnly, ImplementationRef: "g6-controlled-import.v1", Config: cfg, SchemaVersion: "g6.v1", Normalization: "null-preserving.v1", Budget: metric.Budget{TotalMilliseconds: 30000, MaxResponseBytes: 65536}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}}
		_, e = evals.CreateEvaluatorDefinition(ctx, f.s.Scope, asset.Create{ID: er.EntityID, Name: fmt.Sprint("g6-e", i)})
		must(e)
		_, e = evals.PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: er, Body: definition, Source: source})
		must(e)
		x.bindings = append(x.bindings, catalog.EvaluatorBinding{Evaluator: er, Metrics: []asset.Ref{r}, Required: true, Applicability: definition.Applicability})
	}
	x.build(t, x.bindings)
	return x
}
func g6Readers(f *g6Fixture) postgres.PublishedAssets {
	return postgres.PublishedAssets{Cases: postgres.Cases{Pool: f.k.Pool}, Datasets: postgres.Datasets{Pool: f.k.Pool}, Suites: postgres.Suites{Pool: f.k.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: f.k.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: f.k.Pool}}
}
func (f *g6Fixture) build(t *testing.T, bindings []catalog.EvaluatorBinding) {
	t.Helper()
	snapshot, e := (ev.Builder{Assets: g6Readers(f)}).BuildRunSnapshot(context.Background(), f.s.Scope, ev.BuildRunSnapshot{Dataset: &f.dataset, Evaluators: bindings})
	if e != nil {
		t.Fatal(e)
	}
	f.cmd.Snapshot = snapshot
	f.k.Capabilities = ev.EvaluatorExecutionCapability{}
	for _, spec := range snapshot.Input().Evaluators {
		f.k.Capabilities.Bindings = append(f.k.Capabilities.Bindings, ev.Capability{Evaluator: spec.Identity.Ref, ImplementationRef: spec.Definition.ImplementationRef, DefinitionDigest: spec.Identity.ContentDigest})
	}
	f.cmd.Subject, _ = asset.Freeze(map[string]string{"agent": "agent", "revision": "base", "environment": "test", "run_mode": "controlled"})
}
func (f *g6Fixture) policy(t *testing.T, change func(*decision.Policy)) asset.Ref {
	t.Helper()
	p := decision.Policy{Offline: true, Online: true, Criticalities: []string{"CRITICAL"}, CriticalMissing: true, Coverage: decision.CoveragePolicy{MinimumDecision: .95, MinimumEvaluation: .95}, TaskSuccess: &decision.MetricRule{Metric: f.refs[0], Aggregation: "rate", AbsoluteTolerance: floatRef(.01), MinimumCoverage: .95}}
	if change != nil {
		change(&p)
	}
	r := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	ctx := context.Background()
	if _, e := f.gate.CreatePolicy(ctx, f.s.Scope, asset.Create{ID: r.EntityID, Name: "G6 release policy"}); e != nil {
		t.Fatal(e)
	}
	if _, e := f.gate.PublishPolicyVersion(ctx, f.s.Scope, asset.Publish[decision.Policy]{Ref: r, Body: p, Source: asset.Source{Kind: "TEST", Ref: "G6_POLICY", Principal: f.s.Principal}}); e != nil {
		t.Fatal(e)
	}
	return r
}
func floatRef(v float64) *float64 { return &v }

type g6Plan struct {
	Success                                    int
	CriticalFail, CriticalMissing, CostUnknown bool
	Latency                                    float64
	Missing, Unknown, Error                    bool
	ActualModel                                string
}

func (f *g6Fixture) run(t *testing.T, version string, p g6Plan) decision.SourceRef {
	t.Helper()
	ctx := context.Background()
	cmd := f.cmd
	cmd.CommandID = asset.NewID()
	cmd.Subject, _ = asset.Freeze(map[string]string{"agent": "agent", "revision": version, "environment": "test", "run_mode": "controlled"})
	r, e := f.k.CreateRun(ctx, f.s, cmd)
	reply(t, r, e, ev.Applied)
	state, e := f.k.ReadRunState(ctx, f.s.Scope, r.ID)
	if e != nil {
		t.Fatal(e)
	}
	for i, c := range cmd.Snapshot.Input().Manifest {
		var a ev.Attempt
		for _, candidate := range state.Attempts {
			if candidate.CaseID == c.Identity.Ref.EntityID {
				a = candidate
			}
		}
		owner := start(t, f.kernelFixture, ev.Owned{RunID: r.ID, AttemptID: a.ID}, time.Minute)
		kind := ev.Success
		if p.Unknown && i == 9 {
			kind = ev.Unknown
		}
		out := outcome(t, f.kernelFixture, owner, kind)
		v, e := f.k.FinalizeExecutionOutcome(ctx, f.s, out)
		reply(t, v, e, ev.Applied)
		if kind != ev.Success {
			continue
		}
		for _, w := range works(t, f.kernelFixture, owner) {
			if w.AttemptID != a.ID {
				continue
			}
			o := ev.Owned{RunID: r.ID, AttemptID: a.ID, WorkID: w.ID}
			claim, e := f.k.ClaimEvaluatorWork(ctx, f.s, o, "g6-import-test", time.Minute)
			reply(t, claim, e, ev.Applied)
			o.Token = claim.Token
			v, e := f.k.BeginEvaluatorAttempt(ctx, f.s, o, asset.NewID(), 1)
			reply(t, v, e, ev.Applied)
			value := ev.ResultValue{Verdict: "PASS", SourceVerdict: "PASS", Reason: "G6 controlled canonical evidence", SpecDigest: w.SpecDigest, InputDigest: c.Identity.ContentDigest, Artifact: *out.Outcome.Artifact, Evidence: out.Outcome.Evidence}
			app := asset.Applicable
			task := "SUCCESS"
			score := 1.0
			if i >= p.Success || p.CriticalFail && i == 0 {
				task = "FAILURE"
				value.Verdict = "FAIL"
				value.SourceVerdict = "FAIL"
				score = 0
			}
			metricName := w.Metadata.Spec.Definition.Config
			var cfg struct{ Metric string }
			_ = metricName.Decode(&cfg)
			meta := map[string]any{"metric": cfg.Metric, "applicability": app, "clock_boundary": "remote_start_to_terminal.v1", "currency": "USD", "cost_source": "controlled-price-receipt.v1", "price_ref": "test-price:v1"}
			value.Score = &score
			if cfg.Metric == "task_success.v1" {
				if p.Missing && i >= 4 || p.CriticalMissing && i == 0 {
					app = asset.MissingEvidence
					task = "INCONCLUSIVE"
					value.Verdict = "INCONCLUSIVE"
					value.Score = nil
					value.SourceError = "MISSING_EVIDENCE"
				}
				if p.Error && i >= 8 {
					task = "INCONCLUSIVE"
					value.Verdict = "ERROR"
					value.Score = nil
					value.SourceError = "JUDGE_ERROR"
				}
				meta["applicability"] = app
				meta["task_success"] = map[string]any{"version": "task_success.v1", "decision": task, "method": "IMPORTED", "producer_ref": "G6_CONTROLLED_IMPORT", "reason": "controlled", "evidence_refs": []string{value.Artifact.Ref}}
			} else if cfg.Metric == "Latency" {
				value.Score = floatRef(p.Latency)
				value.Verdict = "PASS"
			} else {
				value.Score = floatRef(1)
				value.Verdict = "PASS"
				if p.CostUnknown {
					value.Score = nil
					value.Verdict = "INCONCLUSIVE"
					meta["cost_availability"] = "UNKNOWN"
				} else {
					meta["cost_availability"] = "AVAILABLE"
				}
			}
			if w.Metadata.Spec.Definition.Kind == metric.LLMJudge {
				d := w.Metadata.Spec.Definition
				id := asset.NewID()
				evidence, _ := ev.Intent(value.Evidence)
				call := ev.ProviderCall{ID: id, Number: 1, EvaluationNumber: 1, Suboperation: "task", RequestedProvider: d.Model.Provider, RequestedModel: d.Model.Model, PromptRef: d.PromptRef, PromptDigest: "controlled-prompt", Schema: d.SchemaVersion, InputDigest: c.Identity.ContentDigest, EvidenceDigest: evidence}
				v, e = f.k.BeginProviderCall(ctx, f.s, ev.BeginCall{Owned: o, Call: call})
				reply(t, v, e, ev.Applied)
				actual := d.Model.Model
				if p.ActualModel != "" {
					actual = p.ActualModel
				}
				v, e = f.k.FinishProviderCall(ctx, f.s, ev.FinishCall{Owned: o, CallID: id, Classification: "RESPONSE_RECEIVED", ActualProvider: d.Model.Provider, ActualModel: actual, ActualRevision: d.Model.Revision, UsageAvailability: "UNKNOWN", CostAvailability: "UNKNOWN", ResponseDigest: "controlled-response"})
				reply(t, v, e, ev.Applied)
				value.SelectedCallIDs = []string{id}
				if actual != d.Model.Model {
					value.Verdict = "INCONCLUSIVE"
					value.Score = nil
				}
			}
			value.Provenance, _ = asset.Freeze(meta)
			v, e = f.k.FinalizeEvaluationResult(ctx, f.s, ev.FinalizeResult{Owned: o, CommandID: asset.NewID(), ResultID: asset.NewID(), Value: value})
			reply(t, v, e, ev.Applied)
		}
	}
	v, e := f.k.CoordinateRun(ctx, f.s, r.ID)
	reply(t, v, e, ev.Applied)
	return decision.SourceRef{Kind: "RUN_SET", Runs: []string{r.ID}}
}
func (f *g6Fixture) gateCommand(b, c decision.SourceRef, p asset.Ref) decision.Command {
	return decision.Command{ID: asset.NewID(), Baseline: b, Candidate: c, Policy: p}
}
func TestG6ControlledComparisonAndPersistence(t *testing.T) {
	f := g6(t)
	ctx := context.Background()
	baseline := f.run(t, "base", g6Plan{Success: 8, Latency: 100})
	policy := f.policy(t, nil)
	var good decision.Command
	for _, tc := range []struct {
		name   string
		plan   g6Plan
		change func(*decision.Policy)
		want   decision.Decision
	}{
		{"C01", g6Plan{Success: 9, Latency: 100}, nil, decision.Pass},
		{"C02", g6Plan{Success: 10, CriticalFail: true, Latency: 100}, nil, decision.Fail},
		{"C03", g6Plan{Success: 10, Missing: true, Latency: 100}, nil, decision.Blocked},
		{"C04", g6Plan{Success: 10, Unknown: true, Latency: 100}, nil, decision.Blocked},
		{"C07", g6Plan{Success: 9, Latency: 111}, func(p *decision.Policy) {
			p.Metrics = []decision.MetricRule{{Metric: f.refs[1], Aggregation: "mean", RelativeTolerance: floatRef(.1), MinimumCoverage: 1, Required: true}}
		}, decision.Fail},
		{"C08", g6Plan{Success: 9, Latency: 100, CostUnknown: true}, func(p *decision.Policy) {
			p.Metrics = []decision.MetricRule{{Metric: f.refs[2], Aggregation: "mean", Maximum: floatRef(2), MinimumCoverage: 1, Required: true}}
		}, decision.Blocked},
		{"C11", g6Plan{Success: 10, Error: true, Latency: 100}, nil, decision.Blocked},
		{"C12", g6Plan{Success: 10, CriticalMissing: true, Latency: 100}, func(p *decision.Policy) {
			p.Coverage.MinimumDecision = 0
			p.Coverage.MaximumMissing = 1
			p.Coverage.DropTolerance = 1
			p.TaskSuccess.MinimumCoverage = 0
		}, decision.Blocked},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := policy
			if tc.change != nil {
				p = f.policy(t, tc.change)
			}
			candidate := f.run(t, "candidate", tc.plan)
			cmd := f.gateCommand(baseline, candidate, p)
			r, e := f.gate.CreateGate(ctx, f.s, cmd)
			if e != nil || r.Decision != tc.want {
				t.Fatalf("want %s got %s reasons=%v error=%v", tc.want, r.Decision, r.Reasons, e)
			}
			if tc.name == "C01" {
				good = cmd
				if r.Coverage.Baseline.TaskRate == nil || *r.Coverage.Baseline.TaskRate != .8 || *r.Coverage.Candidate.TaskRate != .9 {
					t.Fatal(r.Coverage)
				}
			}
			if tc.name == "C02" && len(r.CriticalRegressions) != 1 {
				t.Fatal(r)
			}
			if tc.name == "C12" && !strings.Contains(strings.Join(r.Reasons, ","), "CRITICAL_EVIDENCE_MISSING") {
				t.Fatal(r.Reasons)
			}
			t.Logf("%s decision=%s reasons=%v", tc.name, r.Decision, r.Reasons)
		})
	}
	// exact intent / late independent Runs / project isolation / immutable DB guards。
	first, e := f.gate.GetGateReceipt(ctx, f.s.Scope, good.ID)
	if e != nil {
		t.Fatal(e)
	}
	j, _ := asset.Freeze(first)
	_ = f.run(t, "candidate", g6Plan{Success: 10, Latency: 100})
	again, e := f.gate.CreateGate(ctx, f.s, good)
	other, _ := asset.Freeze(again)
	if e != nil || !bytes.Equal(j.Bytes(), other.Bytes()) {
		t.Fatal("late facts changed receipt", e)
	}
	bad := good
	bad.Policy = f.policy(t, nil)
	_, e = f.gate.CreateGate(ctx, f.s, bad)
	expectError(t, e, asset.ErrConflict)
	// 同 label 重试返回原版本；内容改变被拒绝；新版本对同 facts 重新作政策决定。
	pv, e := f.gate.GetPolicyVersion(ctx, f.s.Scope, policy)
	if e != nil {
		t.Fatal(e)
	}
	publish := asset.Publish[decision.Policy]{Ref: policy, Body: pv.Content().Body, Source: pv.Content().Source}
	if _, e = f.gate.PublishPolicyVersion(ctx, f.s.Scope, publish); e != nil {
		t.Fatal("policy retry", e)
	}
	publish.Body.TaskSuccess.Minimum = floatRef(.95)
	_, e = f.gate.PublishPolicyVersion(ctx, f.s.Scope, publish)
	expectError(t, e, asset.ErrConflict)
	publish.Ref.Version = "v2"
	if _, e = f.gate.PublishPolicyVersion(ctx, f.s.Scope, publish); e != nil {
		t.Fatal(e)
	}
	strict := f.gateCommand(baseline, good.Candidate, publish.Ref)
	changedPolicy, e := f.gate.CreateGate(ctx, f.s, strict)
	if e != nil || changedPolicy.Decision != decision.Fail {
		t.Fatal("new policy", changedPolicy.Decision, e)
	}
	publish.Ref.Version = "v3"
	publish.Body.Exceptions = []decision.Exception{{ReasonCode: "TASK_RATE_REGRESSED", Scope: "task_success", Actor: f.s.Principal, Reason: "controlled approval", Proof: "g6-approval:1"}}
	if _, e = f.gate.PublishPolicyVersion(ctx, f.s.Scope, publish); e != nil {
		t.Fatal(e)
	}
	approved, e := f.gate.CreateGate(ctx, f.s, f.gateCommand(baseline, good.Candidate, publish.Ref))
	if e != nil || approved.Decision != decision.Pass || len(approved.Exceptions) != 1 {
		t.Fatal("immutable exception", approved.Decision, e)
	}
	if unchanged, e := f.gate.GetGateReceipt(ctx, f.s.Scope, good.ID); e != nil || unchanged.Decision != decision.Pass || unchanged.PolicyRef != policy {
		t.Fatal("old policy receipt changed", e)
	}
	cross := f.s
	cross.ProjectID = seedProject(t, f.db.pool, f.s.OrganizationID).ProjectID
	_, e = f.gate.CreateGate(ctx, cross, f.gateCommand(baseline, good.Candidate, policy))
	if e == nil {
		t.Fatal("cross project accepted")
	}
	for _, table := range []string{"evaluation_comparisons", "evaluation_gate_receipts", "evaluation_case_comparisons"} {
		rejectSQL(t, f.k.Pool, "42501", "DELETE FROM "+table)
		rejectSQL(t, f.db.pool, "55000", "DELETE FROM "+table)
		rejectSQL(t, f.db.pool, "55000", "UPDATE "+table+" SET project_id=project_id")
		rejectSQL(t, f.db.pool, "55000", "TRUNCATE "+table+" CASCADE")
	}
	rejectSQL(t, f.k.Pool, "55000", "UPDATE evaluation_policy_versions SET content=content")
	rejectSQL(t, f.k.Pool, "55000", `INSERT INTO evaluation_case_comparisons(project_id,gate_id,unit_key,comparison_bytes) VALUES($1,$2,'late','\x7b7d')`, f.s.ProjectID, good.ID)
	if rows, e := f.gate.ListCaseComparisons(ctx, f.s.Scope, good.ID, 0, 100); e != nil || len(rows) != 10 {
		t.Fatal(rows, e)
	}
	// 新 evaluator label 未提供 equivalence proof；同事实受不同 policy 判定。
	spec := f.cmd.Snapshot.Input().Evaluators[0]
	changed := spec.Identity.Ref
	changed.Version = "new"
	_, e = (postgres.EvaluatorDefinitions{Pool: f.k.Pool}).PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: changed, Body: spec.Definition, Source: asset.Source{Kind: "TEST", Ref: "G6_CHANGED", Principal: f.s.Principal}})
	if e != nil {
		t.Fatal(e)
	}
	bindings := append([]catalog.EvaluatorBinding{}, f.bindings...)
	bindings[0].Evaluator = changed
	f.build(t, bindings)
	candidate := f.run(t, "candidate", g6Plan{Success: 9, Latency: 100})
	r, e := f.gate.CreateGate(ctx, f.s, f.gateCommand(baseline, candidate, policy))
	if e != nil || r.Compatibility != decision.Incomparable || r.Decision != decision.Blocked {
		t.Fatal("C05", r.Decision, r.Reasons, e)
	}
	t.Log("C05 PASS")
	// G4 requested != actual 的有效 provenance 进入 Gate 后必须 BLOCKED。
	d := spec.Definition
	d.Kind = metric.LLMJudge
	d.Model = &metric.ModelBinding{Provider: "controlled", Model: "judge", Revision: "v1"}
	prompt := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	d.PromptRef = &prompt
	d.Budget.MaxProviderCalls = 1
	changed.Version = "judge"
	_, e = (postgres.EvaluatorDefinitions{Pool: f.k.Pool}).PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: changed, Body: d, Source: asset.Source{Kind: "TEST", Ref: "G6_JUDGE", Principal: f.s.Principal}})
	if e != nil {
		t.Fatal(e)
	}
	bindings[0].Evaluator = changed
	f.build(t, bindings)
	b := f.run(t, "base", g6Plan{Success: 8, Latency: 100})
	c := f.run(t, "candidate", g6Plan{Success: 9, Latency: 100, ActualModel: "other"})
	r, e = f.gate.CreateGate(ctx, f.s, f.gateCommand(b, c, policy))
	if e != nil || r.Decision != decision.Blocked || r.Compatibility != decision.Incomparable {
		t.Fatal("C06", r.Decision, r.Reasons, e)
	}
	t.Log("C06 PASS")
}

func (f *g6Fixture) onlineSource(t *testing.T, ref asset.Ref, version, status string) decision.SourceRef {
	t.Helper()
	ctx := context.Background()
	rule, e := f.online.GetOnlineRuleVersion(ctx, f.s.Scope, ref)
	if e != nil {
		t.Fatal(e)
	}
	from := time.Date(2026, 10, 4, 0, 0, 0, 0, time.UTC)
	until := from.Add(24 * time.Hour)
	sampled := 0
	for i := 0; i < 200 && sampled < 2; i++ {
		id := asset.NewID()
		trace := asset.NewID()
		metadata, _ := asset.Freeze(map[string]string{"subject": "agent", "agent_version": version, "environment": "test", "run_mode": "controlled"})
		envelope, _ := asset.Freeze(map[string]string{"status": status})
		o := ob.Observation{Ref: ob.Ref{ID: id, ProjectID: f.s.ProjectID, Kind: ob.TraceKind, Schema: "trace-v1"}, TraceID: trace, RuntimeID: asset.NewID(), Source: "G6_CONTROLLED_OBSERVATION", Trust: "NORMAL_OBSERVATION", Status: status, Completed: from.Add(time.Hour), Started: from, Metadata: metadata, Envelope: envelope, Retention: "EXPLICIT_EVALUATION_RETENTION"}
		v, e := f.online.StoreObservation(ctx, f.s5, o)
		reply(t, v, e, ev.Applied)
		v, e = f.online.Materialize(ctx, f.s5, ref, id, false)
		reply(t, v, e, ev.Applied)
		selection := ob.Sample(f.s.ProjectID, ref, trace, rule.Body.Sampling, true)
		if !selection.Sampled {
			continue
		}
		sampled++
		work := workID(t, f.g5Fixture, ref, id)
		claim, e := f.online.ClaimOnlineWork(ctx, f.s5, work, "g6-online", time.Minute)
		reply(t, claim, e, ev.Applied)
		value := ob.Value{Verdict: "PASS", SourceVerdict: "PASS", Reason: "controlled observed status", Score: floatRef(1), Applicability: asset.Applicable}
		v, e = f.online.FinalizeOnlineResult(ctx, f.s5, work, claim.Token, asset.NewID(), asset.NewID(), value)
		reply(t, v, e, ev.Applied)
	}
	if sampled != 2 {
		t.Fatal("deterministic fixture failed to find samples")
	}
	return decision.SourceRef{Kind: "ONLINE", Rule: ref, From: &from, Until: &until, Subject: "agent", SubjectVersion: version}
}
func TestG6OnlineBoundarySnapshotAndCLI(t *testing.T) {
	f := g6(t)
	ctx := context.Background()
	rule, _ := f.rule(t, func(r *ob.Rule) {
		r.Source = "G6_CONTROLLED_OBSERVATION"
		r.Trust = "NORMAL_OBSERVATION"
		r.Filter.Environment = "test"
		r.Filter.RunMode = "controlled"
		r.Sampling.Policy = "HASH_PERCENTAGE"
		r.Sampling.BasisPoints = 1000
	})
	b := f.onlineSource(t, rule, "base", "OK")
	c := f.onlineSource(t, rule, "candidate", "OK")
	p := f.policy(t, func(p *decision.Policy) {
		p.TaskSuccess = nil
		p.Metrics = []decision.MetricRule{{Metric: f.binding.Metric.Identity.Ref, Aggregation: "coverage", Minimum: floatRef(1), MinimumCoverage: 1, Required: true}}
	})
	cmd := f.gateCommand(b, c, p)
	r, e := f.gate.CreateGate(ctx, f.s, cmd)
	if e != nil || r.Decision != decision.Pass || r.Compatibility != decision.Comparable || r.Candidate.Interpretation != "SAMPLED_ONLY_NOT_POPULATION_ESTIMATE" {
		t.Fatal("C10", r.Decision, r.Reasons, e)
	}
	t.Log("C10 PASS; deterministic sampled population; no deployment claim")
	// G02 计算后、commit 前新的 Online Result 不会加入旧 snapshot。
	cmd2 := f.gateCommand(b, c, p)
	snap, e := f.gate.PrepareComparison(ctx, f.s, cmd2)
	if e != nil {
		t.Fatal(e)
	}
	computed, e := decision.Compare(snap)
	if e != nil {
		t.Fatal(e)
	}
	_ = f.onlineSource(t, rule, "candidate", "OK")
	stored, e := f.gate.CompleteGate(ctx, f.s, cmd2.ID)
	if e != nil || stored.Candidate.Digest != computed.Candidate.Digest || stored.Candidate.SampleCount != 2 {
		t.Fatal("G02 snapshot changed", stored.Candidate.SampleCount, e)
	}
	errRule, _ := f.rule(t, func(r *ob.Rule) {
		r.Source = "G6_CONTROLLED_OBSERVATION"
		r.Trust = "NORMAL_OBSERVATION"
		r.Filter.Environment = "test"
		r.Filter.RunMode = "controlled"
		yes := true
		r.Filter.Error = &yes
	})
	online := f.onlineSource(t, errRule, "candidate", "ERROR")
	offline := f.run(t, "base", g6Plan{Success: 8, Latency: 100})
	r, e = f.gate.CreateGate(ctx, f.s, f.gateCommand(offline, online, p))
	if e != nil || r.Compatibility != decision.Incomparable || r.Decision != decision.Blocked {
		t.Fatal("C09", r.Decision, r.Reasons, e)
	}
	t.Log("C09 PASS")
	// CLI binary 从 durable receipt 输出，而不是重新以本地数据评分。
	binary := filepath.Join(t.TempDir(), "evalgate.exe")
	args := []string{"build", "-o", binary, "../../cmd/evalgate"}
	if os.Getenv("G3_RACE_WORKER") == "1" {
		args = []string{"build", "-race", "-o", binary, "../../cmd/evalgate"}
	}
	if out, e := exec.Command("go", args...).CombinedOutput(); e != nil {
		t.Fatal(string(out), e)
	}
	dsn := g6DSN(t, f)
	for _, tc := range []struct {
		b, c   decision.SourceRef
		id     string
		policy asset.Ref
		want   int
	}{{b, c, cmd.ID, p, 0}, {offline, online, r.GateID, p, 2}} {
		rawB, _ := json.Marshal(tc.b)
		rawC, _ := json.Marshal(tc.c)
		command := exec.Command(binary, "--gate", tc.id, "--project", f.s.ProjectID, "--baseline", string(rawB), "--candidate", string(rawC), "--policy", tc.policy.EntityID+"@"+tc.policy.Version)
		command.Env = append(os.Environ(), "DATABASE_URL="+dsn, "EVALGATE_PRINCIPAL="+f.s.Principal)
		out, e := command.Output()
		if g6Exit(e) != tc.want {
			t.Fatalf("CLI exit %d want %d: %s", g6Exit(e), tc.want, out)
		}
		var receipt decision.Receipt
		if e = json.Unmarshal(out, &receipt); e != nil || !asset.ValidID(receipt.GateID) {
			t.Fatal(string(out), e)
		}
		again, e := f.gate.GetGateReceipt(ctx, f.s.Scope, receipt.GateID)
		if e != nil || again.Decision != receipt.Decision {
			t.Fatal("CLI did not output durable receipt", e)
		}
	}
	failed := f.run(t, "candidate", g6Plan{Success: 10, CriticalFail: true, Latency: 100})
	pp := f.policy(t, nil)
	command := exec.Command(binary, "--project", f.s.ProjectID, "--baseline", offline.Runs[0], "--candidate", failed.Runs[0], "--policy", pp.EntityID+"@"+pp.Version)
	command.Env = append(os.Environ(), "DATABASE_URL="+dsn, "EVALGATE_PRINCIPAL="+f.s.Principal)
	out, e := command.Output()
	if g6Exit(e) != 1 {
		t.Fatalf("CLI FAIL exit %d: %s", g6Exit(e), out)
	}
	command = exec.Command(binary, "--project", "invalid")
	command.Env = append(os.Environ(), "DATABASE_URL="+dsn, "EVALGATE_PRINCIPAL="+f.s.Principal)
	out, e = command.Output()
	if g6Exit(e) != 3 || !bytes.Contains(out, []byte("INTERNAL_ERROR")) {
		t.Fatal("CLI internal error", g6Exit(e))
	}
}
func g6Exit(e error) int {
	if e == nil {
		return 0
	}
	if x, ok := e.(*exec.ExitError); ok {
		return x.ExitCode()
	}
	return -1
}
func g6DSN(t *testing.T, f *g6Fixture) string {
	t.Helper()
	var role string
	if e := f.k.Pool.QueryRow(context.Background(), "SELECT current_user").Scan(&role); e != nil {
		t.Fatal(e)
	}
	u := *f.db.url
	q := u.Query()
	q.Set("options", "-c role="+role)
	u.RawQuery = q.Encode()
	return u.String()
}

func TestG6ProcessHelper(t *testing.T) {
	if os.Getenv("G6_HELPER") == "" {
		return
	}
	ctx := context.Background()
	pool, e := pgxpool.New(ctx, os.Getenv("G6_DSN"))
	if e != nil {
		t.Fatal("helper pool error")
	}
	defer pool.Close()
	var s ev.Scope
	var c decision.Command
	if json.Unmarshal([]byte(os.Getenv("G6_SCOPE")), &s) != nil || json.Unmarshal([]byte(os.Getenv("G6_COMMAND")), &c) != nil {
		t.Fatal("helper input")
	}
	k := postgres.Decisions{Pool: pool}
	if os.Getenv("G6_HELPER") == "snapshot" {
		if _, e = k.PrepareComparison(ctx, s, c); e != nil {
			t.Fatal(e)
		}
		fmt.Println("G6_SNAPSHOT_COMMITTED")
	} else {
		r, e := k.CreateGate(ctx, s, c)
		if e != nil {
			t.Fatal(e)
		}
		fmt.Println("G6_RECEIPT_COMMITTED " + r.GateID)
	}
	// 父测试会真正 OS kill；已 commit 的 marker 为同步边界。
	_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
}
func TestG6GateProcesses(t *testing.T) {
	f := g6(t)
	b := f.run(t, "base", g6Plan{Success: 8, Latency: 100})
	c := f.run(t, "candidate", g6Plan{Success: 9, Latency: 100})
	p := f.policy(t, nil)
	cmd := f.gateCommand(b, c, p)
	ctx := context.Background()
	spawn := func(mode string, c decision.Command) (*exec.Cmd, <-chan string) {
		t.Helper()
		exe, e := os.Executable()
		if e != nil {
			t.Fatal(e)
		}
		command := exec.Command(exe, "-test.run=^TestG6ProcessHelper$", "-test.v")
		scope, _ := json.Marshal(f.s)
		raw, _ := json.Marshal(c)
		command.Env = append(os.Environ(), "G6_HELPER="+mode, "G6_DSN="+g6DSN(t, f), "G6_SCOPE="+string(scope), "G6_COMMAND="+string(raw))
		pipe, e := command.StdoutPipe()
		if e != nil {
			t.Fatal(e)
		}
		stdin, e := command.StdinPipe()
		if e != nil {
			t.Fatal(e)
		}
		command.Stderr = os.Stderr
		if e = command.Start(); e != nil {
			t.Fatal(e)
		}
		lines := make(chan string, 16)
		go func() {
			defer close(lines)
			scan := bufio.NewScanner(pipe)
			for scan.Scan() {
				if strings.HasPrefix(scan.Text(), "G6_") {
					lines <- scan.Text()
				}
			}
		}()
		t.Cleanup(func() { _ = stdin.Close(); _ = command.Process.Kill() })
		return command, lines
	}
	marker := func(lines <-chan string) {
		t.Helper()
		select {
		case v := <-lines:
			if !strings.Contains(v, "COMMITTED") {
				t.Fatal("process no commit marker")
			}
		case <-time.After(15 * time.Second):
			t.Fatal("process marker timeout")
		}
	}
	a, al := spawn("gate", cmd)
	z, zl := spawn("gate", cmd)
	marker(al)
	marker(zl)
	_ = a.Process.Kill()
	_ = z.Process.Kill()
	_ = a.Wait()
	_ = z.Wait()
	var count int
	if e := f.db.pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_gate_receipts WHERE gate_id=$1", cmd.ID).Scan(&count); e != nil || count != 1 {
		t.Fatal("G01 canonical receipt", count, e)
	}
	t.Log("G01 PASS; two independent OS processes, one receipt")
	newCmd := f.gateCommand(b, c, p)
	a, al = spawn("snapshot", newCmd)
	marker(al)
	_ = a.Process.Kill()
	_ = a.Wait()
	snapshot, e := f.gate.GetComparison(ctx, f.s.Scope, newCmd.ID)
	if e != nil {
		t.Fatal(e)
	}
	expected, e := decision.Compare(snapshot)
	if e != nil {
		t.Fatal(e)
	}
	z, zl = spawn("gate", newCmd)
	marker(zl)
	_ = z.Process.Kill()
	_ = z.Wait()
	receipt, e := f.gate.GetGateReceipt(ctx, f.s.Scope, newCmd.ID)
	if e != nil || receipt.SnapshotDigest != expected.SnapshotDigest || receipt.Decision != expected.Decision {
		t.Fatal("G03 restart snapshot", e)
	}
	t.Log("G03/G04 PASS; snapshot kill/restart, receipt commit kill then durable read")
}
