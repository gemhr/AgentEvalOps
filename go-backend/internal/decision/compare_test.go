package decision

import (
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
)

func testSnapshot() Snapshot {
	ref := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	p := Policy{Offline: true, Online: true, Criticalities: []string{"CRITICAL"}, CriticalMissing: true, Coverage: CoveragePolicy{MinimumDecision: .95, MinimumEvaluation: .95}, TaskSuccess: &MetricRule{Metric: ref, Aggregation: "rate", AbsoluteTolerance: number(.01), MinimumCoverage: .95}}
	u := Unit{Key: "case/1", CaseID: "case", CaseVersion: "v1", CaseDigest: "digest", Outcome: ev.Success, Task: "SUCCESS", Criticality: "CRITICAL"}
	cfg, _ := asset.Freeze(map[string]string{"metric": "task_success.v1"})
	u.Metrics = []MetricFact{{ResultID: "r1", Verdict: "PASS", Applicability: asset.Applicable, Value: number(1), Metric: ev.MetricInput{Identity: ev.AssetIdentity{Ref: ref}}, Evaluator: ev.EvaluatorSpec{Definition: metric.EvaluatorDefinition{Config: cfg, ImplementationRef: "test-task.v1", SchemaVersion: "test.v1", Normalization: "null-preserving.v1"}}}}
	s := Source{Owner: "OFFLINE", SamplingUnit: "case-run", Units: []Unit{u}, ExpectedSlots: 1, CompletedSlots: 1, Dimensions: map[string]string{"subject": "a", "subject_version": "v1", "environment": "test", "run_mode": "test"}}
	c := s
	c.Units = append([]Unit{}, s.Units...)
	c.Dimensions = map[string]string{"subject": "a", "subject_version": "v2", "environment": "test", "run_mode": "test"}
	return Snapshot{ProjectID: asset.NewID(), Command: Command{ID: asset.NewID(), Policy: ref}, Policy: p, PolicyDigest: "p", Baseline: s, Candidate: c, CapturedAt: time.Now().UTC()}
}
func TestTaskCoverageCriticalAndUnknown(t *testing.T) {
	for _, tc := range []struct {
		task  string
		want  Decision
		class Classification
	}{{"SUCCESS", Pass, Unchanged}, {"FAILURE", Fail, CriticalRegression}, {"INCONCLUSIVE", Blocked, Insufficient}, {"UNKNOWN_EXECUTION", Blocked, Insufficient}, {"MISSING_EVIDENCE", Blocked, Insufficient}, {"EVALUATOR_ERROR", Blocked, Insufficient}, {"UNSUPPORTED", Blocked, Insufficient}} {
		t.Run(tc.task, func(t *testing.T) {
			s := testSnapshot()
			s.Candidate.Units[0].Task = tc.task
			r, e := Compare(s)
			if e != nil || r.Decision != tc.want || r.Cases[0].Classification != tc.class {
				t.Fatalf("%+v %v", r, e)
			}
		})
	}
	s := testSnapshot()
	s.Baseline.Units[0].Task = "FAILURE"
	r, e := Compare(s)
	if e != nil || r.Cases[0].TaskTransition != "FAILURE→SUCCESS" || r.Cases[0].Classification != Improved {
		t.Fatal(r, e)
	}
}
func TestMetricDirectionsThresholdsAndNullable(t *testing.T) {
	for _, tc := range []struct {
		name string
		d    metric.Direction
		r    MetricRule
		b, c *float64
		want Classification
	}{
		{"higher", metric.Higher, MetricRule{AbsoluteTolerance: number(.1)}, number(.9), number(.7), Regressed},
		{"lower_relative", metric.Lower, MetricRule{RelativeTolerance: number(.1)}, number(100), number(111), Regressed},
		{"tolerance", metric.Lower, MetricRule{RelativeTolerance: number(.1)}, number(100), number(109), Unchanged},
		{"minimum", metric.Higher, MetricRule{Minimum: number(.9)}, number(.7), number(.8), Regressed},
		{"maximum", metric.Lower, MetricRule{Maximum: number(10)}, number(5), number(11), Regressed},
		{"range", metric.None, MetricRule{TargetRange: &[2]float64{2, 4}}, number(3), number(5), Regressed},
		{"categorical", metric.None, MetricRule{}, number(1), number(0), CannotCompare},
		{"null", metric.Lower, MetricRule{}, number(1), nil, Insufficient},
		{"relative_zero", metric.Lower, MetricRule{RelativeTolerance: number(.1)}, number(0), number(1), Insufficient},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := compareValues(tc.r, metric.Definition{Direction: tc.d}, tc.b, tc.c)
			if r.Classification != tc.want {
				t.Fatal(r)
			}
		})
	}
}
func TestComparabilityPolicyExceptionsAndProvider(t *testing.T) {
	s := testSnapshot()
	s.Candidate.Dimensions["dataset"] = "other"
	s.Baseline.Dimensions["dataset"] = "base"
	r, e := Compare(s)
	if e != nil || r.Compatibility != Incomparable || r.TaskSuccess.Delta != nil {
		t.Fatal(r, e)
	}
	s.Policy.AcceptedDifferences = []AcceptedDifference{{Dimension: "dataset", Baseline: "base", Candidate: "other", Proof: "source-receipt:1", Actor: "approver", Reason: "equivalent case population"}}
	r, e = Compare(s)
	if e != nil || r.Compatibility != Accepted || r.Decision != Pass {
		t.Fatal(r, e)
	}
	s.Candidate.Units[0].Task = "FAILURE"
	s.Policy.Exceptions = []Exception{{ReasonCode: "CRITICAL_REGRESSION", Scope: "case/1", Actor: "release-owner", Reason: "approved controlled regression", Proof: "approval:1"}, {ReasonCode: "TASK_RATE_REGRESSED", Scope: "task_success", Actor: "release-owner", Reason: "approved", Proof: "approval:1"}}
	r, e = Compare(s)
	if e != nil || r.Decision != Pass || len(r.Exceptions) != 2 {
		t.Fatal(r, e)
	}
	past := s.CapturedAt.Add(-time.Second)
	s.Policy.Exceptions[0].ExpiresAt = &past
	r, _ = Compare(s)
	if r.Decision != Fail {
		t.Fatal(r)
	}
	ref := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	m := MetricFact{Metric: ev.MetricInput{Identity: ev.AssetIdentity{Ref: ref}}, Evaluator: ev.EvaluatorSpec{Definition: metric.EvaluatorDefinition{Kind: metric.LLMJudge, Model: &metric.ModelBinding{Provider: "p", Model: "m", Revision: "r"}}}, SelectedCallIDs: []string{"call"}, Calls: []ev.ProviderCall{{ID: "call", ActualProvider: "p", ActualModel: "wrong", ActualRevision: "r"}}}
	if providerIdentities(Source{Units: []Unit{{Metrics: []MetricFact{m}}}}, ref.EntityID) != "REQUESTED_ACTUAL_MISMATCH" {
		t.Fatal("requested model used as actual")
	}
	m.Calls[0].ActualModel = "m"
	m.Calls[0].ActualRevision = "UNKNOWN"
	if providerIdentities(Source{Units: []Unit{{Metrics: []MetricFact{m}}}}, ref.EntityID) != "UNKNOWN" {
		t.Fatal("unknown revision accepted")
	}
}
func TestSamplingAggregateAndExitContract(t *testing.T) {
	s := testSnapshot()
	s.Candidate.Owner = "ONLINE"
	s.Candidate.SamplingUnit = "trace"
	r, e := Compare(s)
	if e != nil || r.Compatibility != Incomparable || r.Decision != Blocked {
		t.Fatal(r, e)
	}
	ref := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	m := MetricFact{Metric: ev.MetricInput{Identity: ev.AssetIdentity{Ref: ref}, Definition: metric.Definition{Name: "Latency", ValueType: metric.Duration, Direction: metric.Lower, Aggregation: "mean"}}, ResultID: "r", Verdict: "PASS", Value: number(100), Applicability: asset.Applicable}
	a := Source{Units: []Unit{{Metrics: []MetricFact{m}}, {Metrics: []MetricFact{m}}}}
	b := a
	b.Units = append([]Unit{}, a.Units...)
	b.Units[1].Metrics = []MetricFact{m}
	b.Units[1].Metrics[0].Value = nil
	result := aggregateComparison(MetricRule{Metric: ref, Aggregation: "mean", MinimumCoverage: 1}, a, b)
	if result.Classification != Insufficient || result.Candidate == nil || *result.Candidate != 100 || *result.CandidateCoverage != .5 {
		t.Fatal(result)
	}
	for d, want := range map[Decision]int{Pass: 0, Fail: 1, Blocked: 2, "INVALID": 3} {
		if ExitCode(d) != want {
			t.Fatal(d)
		}
	}
	s = testSnapshot()
	other := s.Baseline.Units[0].Metrics[0]
	other.Metric.Identity.Ref = asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	other.Evaluator.Definition.Config, _ = asset.Freeze(map[string]string{"metric": "other.scalar.v1"})
	s.Baseline.Units[0].Metrics = append(append([]MetricFact{}, s.Baseline.Units[0].Metrics...), other)
	other.Verdict, other.Value = "ERROR", nil
	s.Candidate.Units[0].Metrics = append(append([]MetricFact{}, s.Candidate.Units[0].Metrics...), other)
	s.Baseline.ExpectedSlots, s.Candidate.ExpectedSlots = 2, 2
	s.Baseline.CompletedSlots, s.Candidate.CompletedSlots = 2, 2
	r, e = Compare(s)
	if e != nil || r.Decision != Blocked || !one("EVALUATOR_ERROR_LIMIT", r.Reasons...) || r.Coverage.Candidate.TaskRate == nil || *r.Coverage.Candidate.TaskRate != 1 || r.Coverage.CandidateEvaluation.Error != 1 {
		t.Fatal("non-task evaluator error hidden", r.Decision, r.Reasons, e)
	}
}

// 重试只取最终 attempt；不同 Run repeat 保留独立 case-run。
func TestOfflineFinalAttemptRepeatAndFrozenIdentity(t *testing.T) {
	ref := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	subject, _ := asset.Freeze(map[string]string{"subject": "agent", "subject_version": "v1", "environment": "test", "run_mode": "offline"})
	input := ev.SnapshotInput{Origin: ev.PublishedCatalog, Manifest: []ev.CaseInput{{Identity: ev.AssetIdentity{Ref: ref, ContentDigest: "case-digest"}, Case: catalog.CaseContent{Criticality: catalog.Critical}}}}
	state := ev.RunState{Run: ev.Run{ID: asset.NewID(), Status: ev.RunStatus("COMPLETED"), Snapshot: ev.RunSnapshot{Input: input, Subject: subject, Target: ev.Target{Kind: "CONTROLLED", Version: "v1"}}}, Attempts: []ev.Attempt{{ID: "retry-2", CaseID: ref.EntityID, CaseVersion: ref.Version, Number: 2, Status: "TERMINAL", Outcome: ev.Success}, {ID: "attempt-1", CaseID: ref.EntityID, CaseVersion: ref.Version, Number: 1, Status: "TERMINAL", Outcome: ev.Failure}}}
	repeat := state
	repeat.Run.ID = asset.NewID()
	source, e := Offline(SourceRef{Kind: "EXPERIMENT", Experiment: asset.NewID()}, []ev.RunState{state, repeat})
	if e != nil || len(source.Units) != 2 || source.Units[0].AttemptID != "retry-2" || source.Units[0].Key == source.Units[1].Key || source.Units[1].Repeat != 2 {
		t.Fatal(source, e)
	}
	if source.Units[0].Task != "MISSING_EVIDENCE" {
		t.Fatal("execution success became task success")
	}
	s := testSnapshot()
	s.Candidate.Units[0].Metrics = append([]MetricFact{}, s.Candidate.Units[0].Metrics...)
	rag, _ := asset.Freeze(map[string]int{"k": 3})
	s.Candidate.Units[0].Metrics[0].Metric.Definition.Parameters = rag
	r, e := Compare(s)
	if e != nil || r.Decision != Blocked || r.Compatibility != Incomparable || r.TaskSuccess.Delta != nil {
		t.Fatal(r.Decision, r.Reasons, e)
	}
	expected, _ := asset.Freeze(r)
	for i := 0; i < 3; i++ {
		again, _ := Compare(s)
		raw, _ := asset.Freeze(again)
		if expected.String() != raw.String() {
			t.Fatal("nondeterministic frozen receipt")
		}
	}
	m := MetricFact{Metric: ev.MetricInput{Definition: metric.Definition{ValueType: metric.Cost}}, Value: number(7), Verdict: "PASS", Applicability: asset.Applicable}
	if MetricValue(m) != nil {
		t.Fatal("unknown cost accepted")
	}
	m.Metric.Definition.ValueType = metric.Count
	m.Provenance, _ = asset.Freeze(map[string]string{"usage_availability": "AVAILABLE"})
	if MetricValue(m) == nil || *MetricValue(m) != 7 {
		t.Fatal("reported token count lost")
	}
	// 总体 schema 集合相同仍不能遮蔽逐 Case schema 被交换。
	s = testSnapshot()
	u1, u2 := s.Baseline.Units[0], s.Baseline.Units[0]
	u2.Key = "case-2/1"
	u1.Metrics = append([]MetricFact{}, u1.Metrics...)
	u2.Metrics = append([]MetricFact{}, u2.Metrics...)
	u1.Metrics[0].ResultID, u2.Metrics[0].ResultID = "r1", "r2"
	u1.Metrics[0].EvidenceSchemas, u2.Metrics[0].EvidenceSchemas = []string{"a.v1"}, []string{"b.v1"}
	s.Baseline.Units = []Unit{u1, u2}
	u1.Metrics, u2.Metrics = append([]MetricFact{}, u1.Metrics...), append([]MetricFact{}, u2.Metrics...)
	u1.Metrics[0].EvidenceSchemas, u2.Metrics[0].EvidenceSchemas = []string{"b.v1"}, []string{"a.v1"}
	s.Candidate.Units = []Unit{u1, u2}
	r, e = Compare(s)
	if e != nil || r.Decision != Blocked || !one("EVIDENCE_SEMANTICS_MISMATCH", r.Reasons...) || r.TaskSuccess.Delta != nil {
		t.Fatal("case schema mismatch hidden", r.Decision, r.Reasons, e)
	}
}
