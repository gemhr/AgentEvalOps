package decision

import (
	"strings"
	"testing"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
)

func triageUnit(base Unit, value, valid float64, critical string) Unit {
	u := base
	s := citriage.Scores{Values: map[string]float64{}, SchemaValid: valid == 1, SemanticValid: valid == 1, Decidable: valid == 1, Critical: critical}
	for _, m := range citriage.Metrics {
		s.Values[m] = value
	}
	s.Values[citriage.Metrics[6]] = valid
	u.Metrics = append([]MetricFact{}, base.Metrics...)
	for i := range u.Metrics {
		u.Metrics[i].Evaluator.Definition.ImplementationRef = citriage.Implementation
		u.Metrics[i].Provenance, _ = asset.Freeze(map[string]any{"triage": s})
	}
	return u
}

func TestCITriageAbsoluteCriticalAndBlockedPrecedence(t *testing.T) {
	for _, tc := range []struct {
		name         string
		score, valid float64
		critical     string
		want         Decision
	}{
		{"good", 1, 1, "PASS", Pass},
		{"critical-wrong", 0, 1, "FAIL", Fail},
		{"invalid", 0, 0, "BLOCKED", Blocked},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := testSnapshot()
			s.Baseline.Units[0] = triageUnit(s.Baseline.Units[0], 1, 1, "PASS")
			s.Candidate.Units[0] = triageUnit(s.Candidate.Units[0], tc.score, tc.valid, tc.critical)
			r, err := Compare(s)
			if err != nil || r.Decision != tc.want || r.Stage13 == nil || r.Stage13.Candidate.Total != 1 {
				t.Fatal(r, err)
			}
			if tc.name == "invalid" && (*r.Stage13.Candidate.Scores[citriage.Metrics[0]] != 0 || r.Stage13.Candidate.Invalid != 1 || len(r.CriticalRegressions) != 0) {
				t.Fatal("invalid denominator or critical classification", r)
			}
			if tc.name == "critical-wrong" {
				if len(r.CriticalRegressions) != 1 || r.Stage13.CriticalGate != Fail {
					t.Fatal(r)
				}
				s.Candidate.Reasons = []string{"STAGE13_ACTUAL_MODEL_IDENTITY_BLOCKED"}
				r, err = Compare(s)
				if err != nil || r.Decision != Blocked || len(r.Cases) != 1 || r.Cases[0].Classification != CannotCompare || len(r.Stage13.Cases[0].Delta) != 0 || !strings.Contains(strings.Join(r.Reasons, ","), "CI_CRITICAL_FAIL") {
					t.Fatal("blocked hides known wrong or emits delta", r, err)
				}
			}
		})
	}
	s := testSnapshot()
	s.Baseline.Units[0] = triageUnit(s.Baseline.Units[0], .5, 1, "PASS")
	s.Candidate.Units[0] = triageUnit(s.Candidate.Units[0], 1, 1, "PASS")
	r, e := Compare(s)
	if e != nil || r.Decision != Blocked || r.Stage13.BaselineValid || !strings.Contains(strings.Join(r.Reasons, ","), "BASELINE_INVALID") {
		t.Fatal("relative gain cannot validate bad baseline", r, e)
	}
}

func TestCITriagePerCaseRegressionSurvivesAggregateGain(t *testing.T) {
	s := testSnapshot()
	base := s.Baseline.Units[0]
	base.Criticality = "NORMAL"
	s.Baseline.Units = []Unit{triageUnit(base, .9, 1, "PASS"), triageUnit(base, .9, 1, "PASS")}
	s.Candidate.Units = []Unit{triageUnit(base, 1, 1, "PASS"), triageUnit(base, .85, 1, "PASS")}
	for i := range s.Baseline.Units {
		key := []string{"first/1", "second/1"}[i]
		s.Baseline.Units[i].Key = key
		s.Candidate.Units[i].Key = key
	}
	r := Receipt{Compatibility: Comparable, Cases: []CaseComparison{{Key: "first/1"}, {Key: "second/1"}}}
	applyStage13(s, &r)
	if *r.Stage13.Candidate.Scores[citriage.Metrics[0]] <= *r.Stage13.Baseline.Scores[citriage.Metrics[0]] || r.Stage13.RegressionGate != Fail || r.Stage13.Cases[1].Classification != "REGRESSED" {
		t.Fatal("aggregate gain concealed per-case regression", r)
	}
}
