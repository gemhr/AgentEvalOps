//go:build integration

package postgres_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	ob "agentevalops/go-backend/internal/observation"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5"
)

type g7Fixture struct {
	*g6Fixture
	reviews          postgres.Reviews
	reviewScope      rv.Scope
	quality, failure asset.Ref
}

func g7(t *testing.T) *g7Fixture {
	t.Helper()
	f := g6(t)
	ctx := context.Background()
	var role string
	mustG7(t, f.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&role))
	tables := []string{"evaluation_review_items", "evaluation_review_slots", "evaluation_human_annotations", "evaluation_adjudications", "evaluation_adjudication_inputs", "evaluation_golden_labels", "evaluation_golden_annotation_inputs", "evaluation_calibration_snapshots", "evaluation_calibration_samples", "evaluation_calibration_reports", "evaluation_reviewed_case_drafts", "evaluation_review_case_publications", "evaluation_gate_exception_reviews"}
	grant := "GRANT INSERT ON " + strings.Join(tables, ",") + " TO " + pgx.Identifier{role}.Sanitize() + "; GRANT UPDATE ON evaluation_review_items,evaluation_review_slots TO " + pgx.Identifier{role}.Sanitize()
	_, e := f.db.pool.Exec(ctx, grant)
	mustG7(t, e)
	s := rv.Scope{Scope: f.s, ReviewerType: "HUMAN", IdentitySource: "CONTROLLED_TEST_REVIEWER", Queue: true, Review: true, Adjudicate: true, PublishGolden: true, Calibrate: true, Feedback: true, ApproveException: true}
	s.Principal = "CONTROLLED_TEST_REVIEWER:coordinator"
	x := &g7Fixture{g6Fixture: f, reviews: postgres.Reviews{Pool: f.k.Pool}, reviewScope: s}
	x.quality = x.schema(t, rv.Quality)
	x.failure = x.schema(t, rv.FailureCategory)
	return x
}
func mustG7(t *testing.T, e error) {
	t.Helper()
	if e != nil {
		t.Fatal(e)
	}
}
func (f *g7Fixture) reviewer(name string) rv.Scope {
	s := f.reviewScope
	s.Principal = "CONTROLLED_TEST_REVIEWER:" + name
	return s
}
func (f *g7Fixture) schema(t *testing.T, kind rv.DecisionKind) asset.Ref {
	t.Helper()
	r := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	d := metric.Builtins()[0].Definition
	d.Name = "human-" + string(kind)
	d.Labels = rv.Labels(kind)
	store := postgres.MetricDefinitions{Pool: f.k.Pool}
	ctx := context.Background()
	_, e := store.CreateMetricDefinition(ctx, f.s.Scope, asset.Create{ID: r.EntityID, Name: d.Name})
	mustG7(t, e)
	_, e = store.PublishMetricDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.Definition]{Ref: r, Body: d, Source: asset.Source{Kind: "TEST", Ref: "G7_SCHEMA", Principal: f.s.Principal}})
	mustG7(t, e)
	return r
}
func (f *g7Fixture) enqueue(t *testing.T, source rv.SourceRef, schema asset.Ref, kind rv.DecisionKind, reviews int, blind bool) rv.Item {
	t.Helper()
	c := rv.Enqueue{ID: asset.NewID(), Source: source, Schema: schema, Kind: kind, Policy: rv.Policy{Ref: fmt.Sprintf("g7-%s-%d-%t.v1", kind, reviews, blind), Reviews: reviews, Protocol: rv.Protocol{Blind: blind, JudgeShown: !blind}, Sampling: rv.Sampling{Version: "review-hash.v1", Seed: "G7_CONTROLLED", BasisPoints: 10000}}, Reason: "受控人工评审", Priority: "NORMAL", Criticality: "NORMAL"}
	item, e := f.reviews.EnqueueReview(context.Background(), f.reviewScope, c)
	mustG7(t, e)
	return item
}
func humanDecision(item rv.Item, value string) rv.Judgment {
	refs := []string{}
	for _, b := range item.Source.Evidence {
		refs = append(refs, b.Ref)
	}
	return rv.Judgment{Kind: item.Command.Kind, Value: value, Reason: "CONTROLLED_TEST_REVIEWER 判断理由", EvidenceRefs: refs}
}
func submitG7(t *testing.T, k postgres.Reviews, s rv.Scope, item rv.Item, c rv.Claim, value string) rv.Annotation {
	t.Helper()
	a, e := k.SubmitAnnotation(context.Background(), s, rv.Submit{ID: asset.NewID(), ItemID: item.ID, Slot: c.Slot, Token: c.Token, SourceDigest: item.Source.Digest, SchemaDigest: item.SchemaDigest, Decision: humanDecision(item, value)})
	mustG7(t, e)
	return a
}
func (f *g7Fixture) double(t *testing.T, item rv.Item, a, b string) rv.GoldenLabel {
	t.Helper()
	ctx := context.Background()
	sa, sb := f.reviewer("A"), f.reviewer("B")
	ca, e := f.reviews.ClaimReview(ctx, sa, item.ID, time.Minute)
	mustG7(t, e)
	cb, e := f.reviews.ClaimReview(ctx, sb, item.ID, time.Minute)
	mustG7(t, e)
	submitG7(t, f.reviews, sa, item, ca, a)
	hidden, e := f.reviews.GetReview(ctx, sb, item.ID)
	mustG7(t, e)
	if len(hidden.Annotations) != 0 || hidden.Item.Source.Automatic != nil && item.Command.Policy.Protocol.Blind {
		t.Fatal("independent blind decision leaked")
	}
	submitG7(t, f.reviews, sb, item, cb, b)
	if a != b {
		m, e := f.reviews.GetReview(ctx, f.reviewScope, item.ID)
		mustG7(t, e)
		if m.Item.Status != "ADJUDICATION_REQUIRED" {
			t.Fatal(m.Item.Status)
		}
		_, e = f.reviews.AdjudicateReview(ctx, f.reviewer("adjudicator"), asset.NewID(), item.ID, humanDecision(item, b), "第三人裁决", nil)
		mustG7(t, e)
	}
	g, e := f.reviews.PublishGoldenLabel(ctx, f.reviewScope, asset.NewID(), item.ID)
	mustG7(t, e)
	return g
}
func g7Source(t *testing.T, state ev.RunState, ref asset.Ref, index int) rv.SourceRef {
	t.Helper()
	c := state.Run.Snapshot.Input.Manifest[index].Identity.Ref
	for _, r := range state.Results {
		if r.CaseID == c.EntityID && r.EvaluatorID == ref.EntityID && r.EvaluatorVersion == ref.Version {
			return rv.SourceRef{Type: "CALIBRATION_SAMPLE", RunID: state.Run.ID, ResultID: r.ID}
		}
	}
	t.Fatal("result not found")
	return rv.SourceRef{}
}
func (f *g7Fixture) judgeVersions(t *testing.T) (asset.Ref, asset.Ref) {
	t.Helper()
	ctx := context.Background()
	store := postgres.EvaluatorDefinitions{Pool: f.k.Pool}
	old, e := store.GetEvaluatorDefinitionVersion(ctx, f.s.Scope, f.bindings[0].Evaluator)
	mustG7(t, e)
	d := old.Content().Body
	d.Kind = metric.LLMJudge
	d.Model = &metric.ModelBinding{Provider: "controlled", Model: "judge", Revision: "r1"}
	d.PromptRef = &asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	d.Budget.MaxProviderCalls = 1
	a, b := old.Ref(), old.Ref()
	a.Version = "judge-v1"
	b.Version = "judge-v2"
	for _, r := range []asset.Ref{a, b} {
		_, e = store.PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: r, Body: d, Source: asset.Source{Kind: "TEST", Ref: "G7_CONTROLLED_JUDGE", Principal: f.s.Principal}})
		mustG7(t, e)
	}
	bindings := append([]catalog.EvaluatorBinding{}, f.bindings...)
	bindings[0].Evaluator = a
	other := bindings[0]
	other.Evaluator = b
	bindings = append(bindings, other)
	f.build(t, bindings)
	return a, b
}

func TestG7ControlledReviewCalibrationFeedback(t *testing.T) {
	f := g7(t)
	ctx := context.Background()
	a, b := f.judgeVersions(t)
	run := f.run(t, "candidate", g6Plan{Success: 10, Latency: 100})
	state, e := f.k.ReadRunState(ctx, f.s.Scope, run.Runs[0])
	mustG7(t, e)
	// G2 的 Works 查询未承诺行顺序；按事实 ID 比较所有字段，避免将计划顺序视为 mutation。
	sort.Slice(state.Works, func(i, j int) bool { return state.Works[i].ID < state.Works[j].ID })
	before, _ := asset.Freeze(state)
	golden := map[string]rv.GoldenLabel{}
	for i, values := range [][2]string{{"SUCCESS", "SUCCESS"}, {"FAILURE", "FAILURE"}, {"SUCCESS", "FAILURE"}} {
		source := g7Source(t, state, a, i)
		if i == 0 {
			source.Type = "OFFLINE_RESULT"
		}
		item := f.enqueue(t, source, f.refs[0], rv.TaskSuccess, 2, true)
		if item.Source.Automatic != nil {
			t.Fatal("blind source leaked judge")
		}
		g := f.double(t, item, values[0], values[1])
		golden[state.Run.Snapshot.Input.Manifest[i].Identity.Ref.EntityID] = g
		t.Logf("H-E%02d PASS origin=%s", i+1, g.Origin)
	}
	firstGolden := golden[state.Run.Snapshot.Input.Manifest[0].Identity.Ref.EntityID]
	body := testCase()
	body.BodyPolicy = catalog.Redacted
	gtDraft, e := f.reviews.CreateReviewedCaseDraft(ctx, f.reviewScope, rv.Draft{ID: asset.NewID(), ItemID: firstGolden.ItemID, GoldenID: &firstGolden.ID, Case: asset.Ref{EntityID: asset.NewID(), Version: "golden-gt-v1"}, Body: body, Sanitization: "REDACTED", Policy: "explicit-golden-gt.v1", Reason: "明确采用 Golden 作为 GT", HumanSupplement: true, UseGoldenGroundTruth: true})
	mustG7(t, e)
	gtCase, e := f.reviews.PublishReviewedCase(ctx, f.reviewScope, gtDraft.ID, "reviewed golden GT")
	mustG7(t, e)
	if !strings.Contains(gtCase.Content().Body.GroundTruth.String(), firstGolden.ID) || !strings.Contains(gtCase.Content().Source.Metadata.String(), firstGolden.AnnotationIDs[0]) || len(gtDraft.AnnotationIDs) != 2 {
		t.Fatal("Golden GT or exact annotation lineage missing")
	}
	makeCalibration := func(ref asset.Ref, protocol rv.Protocol, labels map[string]rv.GoldenLabel) rv.CalibrationCommand {
		c := rv.CalibrationCommand{ID: asset.NewID(), Dataset: f.dataset, Evaluator: ref, Schema: f.refs[0], Protocol: protocol, Sampling: rv.Sampling{Version: "review-hash.v1", Seed: "same-golden-set", BasisPoints: 10000}, PositiveClass: "SUCCESS"}
		for i, v := range state.Run.Snapshot.Input.Manifest {
			sample := rv.CalibrationSample{Case: v.Identity.Ref, Result: g7Source(t, state, ref, i)}
			if g, ok := labels[v.Identity.Ref.EntityID]; ok {
				sample.GoldenID = g.ID
			}
			c.Samples = append(c.Samples, sample)
		}
		return c
	}
	c1 := makeCalibration(a, rv.Protocol{Blind: true}, golden)
	_, e = f.reviews.PrepareCalibration(ctx, f.reviewScope, c1)
	mustG7(t, e)
	r1, e := f.reviews.CompleteCalibration(ctx, f.reviewScope, c1.ID)
	mustG7(t, e)
	if r1.Agreement.N != 10 || r1.Agreement.Matches != 1 || r1.Agreement.Disagreements != 2 || r1.Agreement.MissingHuman != 7 || r1.Agreement.FalsePositive != 2 || r1.Agreement.ProviderErrors != 0 || len(r1.Pairs[0].Judge.Calls) != 1 {
		t.Fatalf("calibration %+v", r1.Agreement)
	}
	for _, sql := range []string{`UPDATE evaluation_calibration_reports SET report_digest='changed'`, `DELETE FROM evaluation_calibration_reports`} {
		if _, e = f.db.pool.Exec(ctx, sql); e == nil {
			t.Fatal("calibration report mutable")
		}
	}
	c2 := makeCalibration(b, rv.Protocol{Blind: true}, golden)
	_, e = f.reviews.PrepareCalibration(ctx, f.reviewScope, c2)
	mustG7(t, e)
	r2, e := f.reviews.CompleteCalibration(ctx, f.reviewScope, c2.ID)
	mustG7(t, e)
	regression, e := f.reviews.CompareEvaluatorCalibrations(ctx, f.reviewScope, r1.ID, r2.ID)
	mustG7(t, e)
	if regression.Status != "COMPARABLE" || r1.Evaluator == r2.Evaluator {
		t.Fatal(regression)
	}
	t.Log("H-E07 PASS separate judge versions, same Golden/evidence/provider provenance")
	nonblind := f.enqueue(t, g7Source(t, state, a, 3), f.refs[0], rv.TaskSuccess, 2, false)
	if nonblind.Source.Automatic == nil {
		t.Fatal("nonblind judge missing")
	}
	ng := f.double(t, nonblind, "SUCCESS", "SUCCESS")
	c3 := makeCalibration(a, rv.Protocol{JudgeShown: true}, map[string]rv.GoldenLabel{ng.Case.EntityID: ng})
	_, e = f.reviews.PrepareCalibration(ctx, f.reviewScope, c3)
	mustG7(t, e)
	r3, e := f.reviews.CompleteCalibration(ctx, f.reviewScope, c3.ID)
	mustG7(t, e)
	if !r3.Protocol.JudgeShown || rv.CompareEvaluators(r1, r3).Status != "INCOMPARABLE" {
		t.Fatal("blind/nonblind mixed")
	}
	c3.Samples[0].GoldenID = c1.Samples[0].GoldenID
	c3.ID = asset.NewID()
	if _, e = f.reviews.PrepareCalibration(ctx, f.reviewScope, c3); e == nil {
		t.Fatal("mixed blind golden accepted")
	}
	t.Log("H-E08 PASS bias protocol preserved")
	after, e := f.k.ReadRunState(ctx, f.s.Scope, state.Run.ID)
	mustG7(t, e)
	sort.Slice(after.Works, func(i, j int) bool { return after.Works[i].ID < after.Works[j].ID })
	afterBytes, _ := asset.Freeze(after)
	if !bytes.Equal(before.Bytes(), afterBytes.Bytes()) {
		t.Fatal("Human changed automatic truth")
	}
	t.Run("H-E04 reviewed failure feedback", func(t *testing.T) {
		id := asset.NewID()
		out, e := f.online.IngestStrict(ctx, f.s5, "LOCALAGENT_TRACE_V1", bytes.NewReader(traceBody(asset.NewID(), asset.NewID(), asset.NewID(), "ERROR")))
		reply(t, out, e, ev.Applied)
		id = out.ID
		candidates, e := f.online.ListFailureCandidates(ctx, f.s.Scope, ob.Cursor{}, 100)
		mustG7(t, e)
		var candidate ob.FailureCandidate
		for _, c := range candidates {
			if c.ObservationID == id {
				candidate = c
			}
		}
		if candidate.ObservationID == "" {
			t.Fatal("failure candidate absent")
		}
		item := f.enqueue(t, rv.SourceRef{Type: "FAILURE_CANDIDATE", ObservationID: id, CandidateSource: candidate.Source, Classification: candidate.Classification}, f.failure, rv.FailureCategory, 1, true)
		sa := f.reviewer("failure-reviewer")
		claim, e := f.reviews.ClaimReview(ctx, sa, item.ID, time.Minute)
		mustG7(t, e)
		submitG7(t, f.reviews, sa, item, claim, "TRUE_AGENT_FAILURE")
		old, e := (postgres.Datasets{Pool: f.k.Pool}).GetDatasetVersion(ctx, f.s.Scope, f.dataset)
		mustG7(t, e)
		body := testCase()
		body.Input, _ = asset.ParseJSON([]byte(`{"query":"人工脱敏的新输入"}`))
		body.BodyPolicy = catalog.Redacted
		draft := rv.Draft{ID: asset.NewID(), ItemID: item.ID, Case: asset.Ref{EntityID: asset.NewID(), Version: "reviewed-v1"}, Body: body, Sanitization: "NOT_REVIEWED", Policy: "human-sanitization.v1", Reason: "人工确认脱敏策略", HumanSupplement: true}
		d, e := f.reviews.CreateReviewedCaseDraft(ctx, sa, draft)
		mustG7(t, e)
		if _, e = f.reviews.PublishReviewedCase(ctx, sa, d.ID, "reviewed failure"); e == nil {
			t.Fatal("unreviewed published")
		}
		previous := d.ID
		draft.ID = asset.NewID()
		draft.Supersedes = &previous
		draft.Sanitization = "REDACTED"
		d, e = f.reviews.CreateReviewedCaseDraft(ctx, sa, draft)
		mustG7(t, e)
		published, e := f.reviews.PublishReviewedCase(ctx, sa, d.ID, "reviewed failure")
		mustG7(t, e)
		source := published.Content().Source
		if source.Ref != d.ID || source.Kind != "REVIEWED_CASE" || !strings.Contains(source.Metadata.String(), id) {
			t.Fatal("trace lineage lost")
		}
		target := f.dataset
		target.Version = "reviewed-revision"
		revision, e := f.reviews.BuildDatasetRevisionFromReviewedCases(ctx, sa, f.dataset, target, []string{d.ID})
		mustG7(t, e)
		if len(revision.Content().Body.Cases) != 11 {
			t.Fatal("revision missing case")
		}
		again, e := f.reviews.BuildDatasetRevisionFromReviewedCases(ctx, sa, f.dataset, target, []string{d.ID})
		mustG7(t, e)
		if again.ContentDigest() != revision.ContentDigest() {
			t.Fatal("revision retry changed")
		}
		oldAfter, e := (postgres.Datasets{Pool: f.k.Pool}).GetDatasetVersion(ctx, f.s.Scope, f.dataset)
		mustG7(t, e)
		if !bytes.Equal(old.Bytes(), oldAfter.Bytes()) {
			t.Fatal("old dataset changed")
		}
		t.Log("H-E04 PASS explicit review/sanitize/new Case/new Dataset; old bytes preserved")
	})
	t.Run("H-E05 and H-E06 exception boundary", func(t *testing.T) {
		// G6 的 Agent Gate 不接受同 metric 的两个 evaluator；校准保持独立双版本报告。
		f.build(t, f.bindings)
		policy := f.policy(t, nil)
		baseline := f.run(t, "base", g6Plan{Success: 10, Latency: 100})
		bad := f.run(t, "candidate", g6Plan{Success: 10, CriticalFail: true, Latency: 100})
		receipt, e := f.gate.CreateGate(ctx, f.s, f.gateCommand(baseline, bad, policy))
		mustG7(t, e)
		if receipt.Decision != decision.Fail {
			t.Fatal(receipt.Decision, receipt.Reasons)
		}
		original, _ := asset.Freeze(receipt)
		deniedItem := f.enqueue(t, rv.SourceRef{Type: "GATE_DECISION", GateID: receipt.GateID}, f.quality, rv.Quality, 2, true)
		f.double(t, deniedItem, "PASS", "FAIL")
		for _, issue := range receipt.Issues {
			if issue.Decision == decision.Fail {
				_, e = f.reviews.ReviewGateException(ctx, f.reviewer("approver"), rv.GateException{ID: asset.NewID(), ItemID: deniedItem.ID, GateID: receipt.GateID, Code: issue.Code, Scope: issue.Scope, RequestedReason: "双人最终裁决拒绝", Decision: "APPROVED", Reason: "不能只采用其中一人的 PASS", Expiry: time.Now().Add(time.Hour)})
				if !errors.Is(e, asset.ErrForbidden) {
					t.Fatal("approval ignored final adjudication", e)
				}
				break
			}
		}
		item := f.enqueue(t, rv.SourceRef{Type: "GATE_DECISION", GateID: receipt.GateID}, f.quality, rv.Quality, 1, true)
		approver := f.reviewer("approver")
		claim, e := f.reviews.ClaimReview(ctx, approver, item.ID, time.Minute)
		mustG7(t, e)
		submitG7(t, f.reviews, approver, item, claim, "PASS")
		p, e := f.gate.GetPolicyVersion(ctx, f.s.Scope, policy)
		mustG7(t, e)
		newPolicy := p.Content().Body
		for _, issue := range receipt.Issues {
			if issue.Decision != decision.Fail {
				continue
			}
			approval, e := f.reviews.ReviewGateException(ctx, approver, rv.GateException{ID: asset.NewID(), ItemID: item.ID, GateID: receipt.GateID, Code: issue.Code, Scope: issue.Scope, RequestedReason: "受控质量例外", Decision: "APPROVED", Reason: "明确批准冻结例外", Expiry: time.Now().Add(time.Hour)})
			mustG7(t, e)
			proof, e := f.reviews.AcceptedExceptionProof(ctx, approver, approval.ID)
			mustG7(t, e)
			newPolicy.Exceptions = append(newPolicy.Exceptions, proof)
		}
		newRef := policy
		newRef.Version = "approved-v2"
		_, e = f.gate.PublishPolicyVersion(ctx, f.s.Scope, asset.Publish[decision.Policy]{Ref: newRef, Body: newPolicy, Source: asset.Source{Kind: "TEST", Ref: "G7_EXPLICIT_NEW_POLICY", Principal: f.s.Principal}})
		mustG7(t, e)
		newReceipt, e := f.gate.CreateGate(ctx, f.s, f.gateCommand(baseline, bad, newRef))
		mustG7(t, e)
		if newReceipt.Decision != decision.Pass {
			t.Fatal(newReceipt.Decision, newReceipt.Reasons)
		}
		oldReceipt, e := f.gate.GetGateReceipt(ctx, f.s.Scope, receipt.GateID)
		mustG7(t, e)
		oldBytes, _ := asset.Freeze(oldReceipt)
		if !bytes.Equal(original.Bytes(), oldBytes.Bytes()) {
			t.Fatal("old FAIL overwritten")
		}
		t.Log("H-E05 PASS old FAIL retained, new policy/new receipt")
		missing := f.run(t, "candidate", g6Plan{Success: 10, CriticalMissing: true, Latency: 100})
		blocked, e := f.gate.CreateGate(ctx, f.s, f.gateCommand(baseline, missing, policy))
		mustG7(t, e)
		if blocked.Decision != decision.Blocked {
			t.Fatal(blocked.Decision)
		}
		bi := f.enqueue(t, rv.SourceRef{Type: "GATE_DECISION", GateID: blocked.GateID}, f.quality, rv.Quality, 1, true)
		bc, e := f.reviews.ClaimReview(ctx, approver, bi.ID, time.Minute)
		mustG7(t, e)
		submitG7(t, f.reviews, approver, bi, bc, "PASS")
		for _, issue := range blocked.Issues {
			if issue.Decision == decision.Blocked {
				_, e = f.reviews.ReviewGateException(ctx, approver, rv.GateException{ID: asset.NewID(), ItemID: bi.ID, GateID: blocked.GateID, Code: issue.Code, Scope: issue.Scope, RequestedReason: "无法创造证据", Decision: "APPROVED", Reason: "测试拒绝", Expiry: time.Now().Add(time.Hour)})
				if !errors.Is(e, asset.ErrForbidden) {
					t.Fatal("BLOCKED approval bypass", e)
				}
				break
			}
		}
		t.Log("H-E06 PASS approver cannot create missing evidence")
	})
	coverage, e := f.reviews.GetHumanCoverage(ctx, f.reviewScope)
	mustG7(t, e)
	if coverage.IndependentPairs != 5 || coverage.Adjudicated != 2 || coverage.Golden != 5 || coverage.Missing < 1 {
		t.Fatal(coverage)
	}
}

func TestG7OwnershipAtomicityHistoryAndIsolation(t *testing.T) {
	f := g7(t)
	ctx := context.Background()
	run := f.run(t, "test", g6Plan{Success: 10})
	state, e := f.k.ReadRunState(ctx, f.s.Scope, run.Runs[0])
	mustG7(t, e)
	ref := g7Source(t, state, f.bindings[0].Evaluator, 0)
	item := f.enqueue(t, ref, f.refs[0], rv.TaskSuccess, 1, true)
	// 同 source/policy 精确幂等，变 intent 冲突。
	repeat := item.Command
	repeat.ID = asset.NewID()
	again, e := f.reviews.EnqueueReview(ctx, f.reviewScope, repeat)
	mustG7(t, e)
	if again.ID != item.ID {
		t.Fatal("duplicate review")
	}
	repeat.Reason = "different"
	if _, e = f.reviews.EnqueueReview(ctx, f.reviewScope, repeat); !errors.Is(e, asset.ErrConflict) {
		t.Fatal(e)
	}
	sa, sb := f.reviewer("A"), f.reviewer("B")
	var wg sync.WaitGroup
	claims := make(chan rv.Claim, 2)
	errs := make(chan error, 2)
	for _, s := range []rv.Scope{sa, sb} {
		wg.Add(1)
		go func(s rv.Scope) {
			defer wg.Done()
			c, e := f.reviews.ClaimReview(ctx, s, item.ID, time.Second)
			claims <- c
			errs <- e
		}(s)
	}
	wg.Wait()
	close(claims)
	close(errs)
	winners := 0
	var claim rv.Claim
	for e := range errs {
		if e == nil {
			winners++
		} else if !errors.Is(e, asset.ErrConflict) {
			t.Fatal(e)
		}
	}
	for c := range claims {
		if c.Token != "" {
			claim = c
		}
	}
	if winners != 1 {
		t.Fatal("single slot", winners)
	}
	model, e := f.reviews.GetReview(ctx, f.reviewScope, item.ID)
	mustG7(t, e)
	owner := sa
	if model.Slots[0].Reviewer.ID != sa.Principal {
		owner = sb
	}
	oldToken := claim.Token
	_, e = f.db.pool.Exec(ctx, `UPDATE evaluation_review_slots SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE project_id=$1 AND item_id=$2`, f.s.ProjectID, item.ID)
	mustG7(t, e)
	c := rv.Submit{ID: asset.NewID(), ItemID: item.ID, Slot: claim.Slot, Token: claim.Token, SourceDigest: item.Source.Digest, SchemaDigest: item.SchemaDigest, Decision: humanDecision(item, "SUCCESS")}
	if _, e = f.reviews.SubmitAnnotation(ctx, owner, c); !errors.Is(e, rv.ErrOwnershipLost) {
		t.Fatal("stale submit", e)
	}
	if _, e = f.reviews.RenewReview(ctx, owner, claim, time.Minute); !errors.Is(e, rv.ErrOwnershipLost) {
		t.Fatal("expired renew", e)
	}
	fresh := f.reviewer("fresh")
	claim, e = f.reviews.ClaimReview(ctx, fresh, item.ID, time.Minute)
	mustG7(t, e)
	if claim.Token == oldToken {
		t.Fatal("reused token")
	}
	claim, e = f.reviews.RenewReview(ctx, fresh, claim, time.Minute)
	mustG7(t, e)
	mustG7(t, f.reviews.ReleaseReview(ctx, fresh, claim))
	if _, e = f.reviews.RenewReview(ctx, fresh, claim, time.Minute); !errors.Is(e, rv.ErrOwnershipLost) {
		t.Fatal("released owner renewed", e)
	}
	releasedToken := claim.Token
	claim, e = f.reviews.ClaimReview(ctx, fresh, item.ID, time.Minute)
	mustG7(t, e)
	if claim.Token == releasedToken {
		t.Fatal("released token reused")
	}
	// 数据库 CHECK 注入，证明 annotation INSERT 和 slot completion 整体回滚。
	_, e = f.db.pool.Exec(ctx, `ALTER TABLE evaluation_review_slots ADD CONSTRAINT g7_test_fail CHECK(status!='COMPLETED')`)
	mustG7(t, e)
	c.ID = asset.NewID()
	c.Token = claim.Token
	c.Slot = claim.Slot
	if _, e = f.reviews.SubmitAnnotation(ctx, fresh, c); e == nil {
		t.Fatal("fault did not reject")
	}
	var count int
	mustG7(t, f.k.Pool.QueryRow(ctx, `SELECT count(*) FROM evaluation_human_annotations WHERE project_id=$1 AND item_id=$2`, f.s.ProjectID, item.ID).Scan(&count))
	if count != 0 {
		t.Fatal("orphan annotation")
	}
	_, e = f.db.pool.Exec(ctx, `ALTER TABLE evaluation_review_slots DROP CONSTRAINT g7_test_fail`)
	mustG7(t, e)
	annotation, e := f.reviews.SubmitAnnotation(ctx, fresh, c)
	mustG7(t, e)
	orphan := annotation
	orphan.ID = asset.NewID()
	orphanBytes, e := asset.FreezeBytes(orphan)
	mustG7(t, e)
	if _, e = f.db.pool.Exec(ctx, `INSERT INTO evaluation_human_annotations(id,project_id,item_id,slot,reviewer_id,token,intent_digest,annotation_bytes) VALUES($1,$2,$3,$4,$5,$6,'orphan',$7)`, orphan.ID, f.s.ProjectID, item.ID, claim.Slot, fresh.Principal, claim.Token, orphanBytes); e == nil {
		t.Fatal("annotation without atomic slot accepted")
	}
	same, e := f.reviews.SubmitAnnotation(ctx, fresh, c)
	mustG7(t, e)
	if same.ID != annotation.ID {
		t.Fatal("submit retry")
	}
	c.Decision.Value = "FAILURE"
	if _, e = f.reviews.SubmitAnnotation(ctx, fresh, c); !errors.Is(e, asset.ErrConflict) {
		t.Fatal("submit overwrite", e)
	}
	if _, e = f.reviews.PublishGoldenLabel(ctx, f.reviewScope, asset.NewID(), item.ID); !errors.Is(e, asset.ErrConflict) {
		t.Fatal("single human became golden", e)
	}
	correction, e := f.reviews.CorrectAnnotation(ctx, fresh, asset.NewID(), annotation.ID, humanDecision(item, "FAILURE"))
	mustG7(t, e)
	if correction.Supersedes == nil || *correction.Supersedes != annotation.ID {
		t.Fatal("history lost")
	}
	adjudicator := f.reviewer("third")
	adj, e := f.reviews.AdjudicateReview(ctx, adjudicator, asset.NewID(), item.ID, humanDecision(item, "FAILURE"), "纠正后第三方裁决", nil)
	mustG7(t, e)
	gold, e := f.reviews.PublishGoldenLabel(ctx, f.reviewScope, asset.NewID(), item.ID)
	mustG7(t, e)
	if gold.Origin != "ADJUDICATED" {
		t.Fatal(gold)
	}
	adj2, e := f.reviews.AdjudicateReview(ctx, adjudicator, asset.NewID(), item.ID, humanDecision(item, "SUCCESS"), "裁决更正", &adj.ID)
	mustG7(t, e)
	if adj2.Supersedes == nil {
		t.Fatal("adjudication lineage")
	}
	if _, e = f.reviews.PublishGoldenLabel(ctx, f.reviewScope, gold.ID, item.ID); !errors.Is(e, asset.ErrConflict) {
		t.Fatal("changed golden intent reused ID", e)
	}
	updatedGold, e := f.reviews.PublishGoldenLabel(ctx, f.reviewScope, asset.NewID(), item.ID)
	mustG7(t, e)
	if updatedGold.ID == gold.ID || updatedGold.Decision.Value != "SUCCESS" {
		t.Fatal("golden correction failed")
	}
	for _, table := range []string{"evaluation_human_annotations", "evaluation_adjudications", "evaluation_golden_labels"} {
		for _, sql := range []string{"UPDATE " + table + " SET id=id", "DELETE FROM " + table, "TRUNCATE " + table + " CASCADE"} {
			if _, e = f.db.pool.Exec(ctx, sql); e == nil {
				t.Fatal("immutable failed", sql)
			}
		}
	}
	if _, e = f.db.pool.Exec(ctx, `UPDATE evaluation_review_items SET source_digest='changed' WHERE id=$1`, item.ID); e == nil {
		t.Fatal("source mutable")
	}
	// 双 reviewer 不同、独立、一致自动形成 Golden；同 reviewer 无法领取两槽。
	double := f.enqueue(t, g7Source(t, state, f.bindings[0].Evaluator, 1), f.refs[0], rv.TaskSuccess, 2, true)
	first, e := f.reviews.ClaimReview(ctx, sa, double.ID, time.Minute)
	mustG7(t, e)
	if _, e = f.reviews.ClaimReview(ctx, sa, double.ID, time.Minute); !errors.Is(e, asset.ErrConflict) {
		t.Fatal("same reviewer twice", e)
	}
	second, e := f.reviews.ClaimReview(ctx, sb, double.ID, time.Minute)
	mustG7(t, e)
	submitG7(t, f.reviews, sa, double, first, "SUCCESS")
	submitG7(t, f.reviews, sb, double, second, "SUCCESS")
	g, e := f.reviews.PublishGoldenLabel(ctx, f.reviewScope, asset.NewID(), double.ID)
	mustG7(t, e)
	againGolden, e := f.reviews.PublishGoldenLabel(ctx, f.reviewScope, g.ID, double.ID)
	mustG7(t, e)
	if againGolden.ID != g.ID {
		t.Fatal("golden idempotency")
	}
	againGolden, e = f.reviews.PublishGoldenLabel(ctx, f.reviewScope, asset.NewID(), double.ID)
	mustG7(t, e)
	if againGolden.ID != g.ID {
		t.Fatal("same golden content created duplicate")
	}
	if _, e = f.db.pool.Exec(ctx, `INSERT INTO evaluation_golden_annotation_inputs(project_id,golden_id,annotation_id) VALUES($1,$2,$3)`, f.s.ProjectID, g.ID, annotation.ID); e == nil {
		t.Fatal("late input inserted into immutable golden")
	}
	if _, e = f.reviews.ClaimReview(ctx, sa, double.ID, time.Nanosecond); !errors.Is(e, asset.ErrInvalid) {
		t.Fatal("zero DB lease accepted", e)
	}
	// 等待行锁期间过期：验证在拿到锁之后读取 DB clock，而不是只在命令入口检查。
	blockedItem := f.enqueue(t, g7Source(t, state, f.bindings[0].Evaluator, 2), f.refs[0], rv.TaskSuccess, 1, true)
	blockedClaim, e := f.reviews.ClaimReview(ctx, sa, blockedItem.ID, 100*time.Millisecond)
	mustG7(t, e)
	lockTx, e := f.db.pool.Begin(ctx)
	mustG7(t, e)
	defer lockTx.Rollback(ctx)
	_, e = lockTx.Exec(ctx, `SELECT id FROM evaluation_review_items WHERE project_id=$1 AND id=$2 FOR UPDATE`, f.s.ProjectID, blockedItem.ID)
	mustG7(t, e)
	done := make(chan error, 1)
	go func() {
		_, e := f.reviews.SubmitAnnotation(ctx, sa, rv.Submit{ID: asset.NewID(), ItemID: blockedItem.ID, Slot: blockedClaim.Slot, Token: blockedClaim.Token, SourceDigest: blockedItem.Source.Digest, SchemaDigest: blockedItem.SchemaDigest, Decision: humanDecision(blockedItem, "SUCCESS")})
		done <- e
	}()
	for {
		var expired bool
		mustG7(t, f.db.pool.QueryRow(ctx, `SELECT clock_timestamp()>$1::timestamptz`, blockedClaim.Lease).Scan(&expired))
		if expired {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	mustG7(t, lockTx.Commit(ctx))
	if e = <-done; !errors.Is(e, rv.ErrOwnershipLost) {
		t.Fatal("lock wait bypassed lease expiry", e)
	}
	other := seedProject(t, f.db.pool, asset.NewID())
	cross := f.reviewScope
	cross.Scope.Scope = other
	cross.Principal = f.reviewScope.Principal
	if _, e = f.reviews.GetReview(ctx, cross, item.ID); !errors.Is(e, asset.ErrNotFound) {
		t.Fatal("cross project read", e)
	}
	if _, e = f.reviews.ClaimReview(ctx, cross, item.ID, time.Minute); !errors.Is(e, asset.ErrNotFound) {
		t.Fatal("cross project claim", e)
	}
	tx, e := f.db.pool.Begin(ctx)
	mustG7(t, e)
	_, e = tx.Exec(ctx, `INSERT INTO evaluation_golden_annotation_inputs(project_id,golden_id,annotation_id) VALUES($1,$2,$3)`, other.ProjectID, g.ID, annotation.ID)
	if e == nil {
		t.Fatal("cross project FK accepted")
	}
	_ = tx.Rollback(ctx)
	t.Log("immutable sources/history, lease reclaim, single/double ownership, atomic rollback, scoped FK PASS")
}
