package decision

import (
	"strings"
	"testing"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
	"agentevalops/go-backend/internal/metric"
)

func v2Snapshot() Snapshot {
	s := testSnapshot()
	s.Policy = Policy{Offline: true, Stage13ReleaseVersion: Stage13ReleaseV2, Criticalities: []string{"CRITICAL"}, CriticalMissing: true, BlockPerCase: true, Coverage: CoveragePolicy{MinimumDecision: 1, MinimumEvaluation: 1}}
	base := s.Baseline.Units[0]
	base.Metrics = nil
	for _, name := range citriage.Metrics {
		ref := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
		m := s.Baseline.Units[0].Metrics[0]
		m.Metric.Identity.Ref = ref
		m.Metric.Definition.Direction = metric.Higher
		m.Metric.Definition.Aggregation = "mean"
		m.Evaluator.Definition.Config, _ = asset.Freeze(map[string]string{"metric": name})
		base.Metrics = append(base.Metrics, m)
		s.Policy.Metrics = append(s.Policy.Metrics, MetricRule{Metric: ref, Aggregation: "mean", AbsoluteTolerance: number(0), RelativeTolerance: number(0), MinimumCoverage: 1, PerCase: true, Required: true})
	}
	s.Baseline.Units[0] = triageUnit(base, 1, 1, "PASS")
	s.Candidate.Units[0] = triageUnit(base, 1, 1, "PASS")
	s.Baseline.ExpectedSlots, s.Baseline.CompletedSlots = 7, 7
	s.Candidate.ExpectedSlots, s.Candidate.CompletedSlots = 7, 7
	return s
}

func TestStage13V2ComparatorQualityAndSafety(t *testing.T) {
	for _, tc := range []struct {
		name    string
		mutate  func(*Snapshot)
		want    Decision
		valid   bool
		quality string
	}{
		{"candidate-pass", func(s *Snapshot) {}, Pass, true, "BASELINE_QUALITY_MEETS_THRESHOLD"},
		{"baseline-low-quality-valid", func(s *Snapshot) { s.Baseline.Units[0] = triageUnit(s.Baseline.Units[0], .5, 1, "FAIL") }, Pass, true, "BASELINE_QUALITY_BELOW_THRESHOLD"},
		{"baseline-invalid-evidence", func(s *Snapshot) { s.Baseline.Units[0].Metrics[1].ResultID = "" }, Blocked, false, "BASELINE_QUALITY_UNAVAILABLE"},
		{"baseline-tampered-score", func(s *Snapshot) { s.Baseline.Units[0].Metrics[1].Value = number(.5) }, Blocked, false, "BASELINE_QUALITY_UNAVAILABLE"},
		{"identity-invalid", func(s *Snapshot) { s.Baseline.Reasons = []string{"STAGE13_ACTUAL_MODEL_IDENTITY_BLOCKED"} }, Blocked, false, "BASELINE_QUALITY_UNAVAILABLE"},
		{"candidate-absolute-low", func(s *Snapshot) { s.Candidate.Units[0] = triageUnit(s.Candidate.Units[0], .9, 1, "PASS") }, Fail, true, "BASELINE_QUALITY_MEETS_THRESHOLD"},
		{"critical-regression", func(s *Snapshot) { s.Candidate.Units[0] = triageUnit(s.Candidate.Units[0], 0, 1, "FAIL") }, Fail, true, "BASELINE_QUALITY_MEETS_THRESHOLD"},
		{"normal-regression-zero-tolerance", func(s *Snapshot) {
			s.Baseline.Units[0].Criticality = "NORMAL"
			s.Candidate.Units[0] = triageUnit(s.Baseline.Units[0], .99, 1, "PASS")
		}, Fail, true, "BASELINE_QUALITY_MEETS_THRESHOLD"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := v2Snapshot()
			tc.mutate(&s)
			// triageUnit 的通用旧 fixture value 与新同源检查保持一致。
			for _, side := range []*Source{&s.Baseline, &s.Candidate} {
				for j := range side.Units {
					score, _ := stage13Score(side.Units[j])
					for i := range side.Units[j].Metrics {
						m := &side.Units[j].Metrics[i]
						if tc.name != "baseline-tampered-score" || side != &s.Baseline || i != 1 {
							m.Value = number(score.Values[metricKey(m.Evaluator)])
						}
					}
				}
			}
			r, err := Compare(s)
			if err != nil || r.Decision != tc.want || r.Stage13.BaselineValid != tc.valid || r.Stage13.BaselineQualityStatus != tc.quality {
				t.Fatalf("unexpected v2 outcome: %v %v reasons=%v gate=%+v", r.Decision, err, r.Reasons, r.Stage13)
			}
			if tc.name == "critical-regression" && (r.Stage13.CriticalGate != Fail || len(r.CriticalRegressions) != 1) {
				t.Fatal("critical veto lost")
			}
			if tc.name == "normal-regression-zero-tolerance" && r.Stage13.RegressionGate != Fail {
				t.Fatal("normal regression tolerated")
			}
			if !tc.valid && !strings.Contains(strings.Join(r.Reasons, ","), "COMPARATOR_INVALID") {
				t.Fatal("invalid comparator not classified")
			}
		})
	}
	s := v2Snapshot()
	s.Policy.Metrics[0].AbsoluteTolerance = number(1.0 / 6)
	if _, err := Compare(s); err == nil {
		t.Fatal("unversioned tolerance accepted")
	}
}

func TestStage13V2ProjectionPreservesV1(t *testing.T) {
	s := testSnapshot()
	s.Baseline.Units[0] = triageUnit(s.Baseline.Units[0], .5, 1, "PASS")
	s.Candidate.Units[0] = triageUnit(s.Candidate.Units[0], 1, 1, "PASS")
	before, err := Compare(s)
	if err != nil || before.Decision != Blocked {
		t.Fatal("v1 semantics changed")
	}
	bytes, _ := asset.Freeze(before)
	projected := v2Snapshot()
	projected.Baseline.Units[0] = triageUnit(projected.Baseline.Units[0], .5, 1, "PASS")
	for i := range projected.Baseline.Units[0].Metrics {
		m := &projected.Baseline.Units[0].Metrics[i]
		m.Value = number(.5)
		if i == 6 {
			m.Value = number(1)
		}
	}
	if r, e := Compare(projected); e != nil || r.Decision != Pass {
		t.Fatal("new projection", e, r.Reasons)
	}
	after, _ := Compare(s)
	frozen, _ := asset.Freeze(after)
	if bytes.String() != frozen.String() {
		t.Fatal("projection mutated v1 receipt")
	}
}
