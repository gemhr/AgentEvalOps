package decision

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
)

type Stage13Aggregate struct {
	Total, Valid, Invalid, Evaluated, Decidable int
	Scores                                      map[string]*float64
}
type Stage13Case struct {
	CaseID              string
	Baseline, Candidate citriage.Scores
	Delta               map[string]float64
	Classification      string
	CriticalRegression  bool
}
type Stage13Gate struct {
	Version                                                             string
	Baseline, Candidate                                                 Stage13Aggregate
	Cases                                                               []Stage13Case
	Thresholds                                                          map[string]float64
	AbsoluteMetricGate, RegressionGate, CriticalGate, ComparabilityGate Decision
	BaselineValid                                                       bool
	FailingMetrics, CriticalCases, BlockedReasons                       []string
}

func stage13Score(u Unit) (citriage.Scores, bool) {
	for _, m := range u.Metrics {
		if m.Evaluator.Definition.ImplementationRef != citriage.Implementation || m.ResultID == "" || m.Verdict == "ERROR" {
			continue
		}
		var p struct {
			Triage citriage.Scores `json:"triage"`
		}
		if m.Provenance.Decode(&map[string]asset.JSON{}) != nil {
			continue
		}
		// Provenance 包含其它正式字段，按字段独立读取，不丢弃原始 artifact。
		var fields map[string]asset.JSON
		_ = m.Provenance.Decode(&fields)
		if fields["triage"].Decode(&p.Triage) == nil && p.Triage.Values != nil {
			return p.Triage, true
		}
	}
	return citriage.Scores{Critical: "BLOCKED", Reason: "MISSING_RESULT", Values: map[string]float64{}}, false
}
func stage13Aggregate(source Source) Stage13Aggregate {
	a := Stage13Aggregate{Total: len(source.Units), Scores: map[string]*float64{}}
	sums := map[string]float64{}
	eligible := map[string]int{}
	for _, u := range source.Units {
		s, ok := stage13Score(u)
		if ok {
			a.Evaluated++
			if s.SchemaValid && s.SemanticValid {
				a.Valid++
			} else {
				a.Invalid++
			}
			if s.Decidable {
				a.Decidable++
			}
		}
		for _, name := range citriage.Metrics {
			applicable := true
			for _, m := range u.Metrics {
				if metricKey(m.Evaluator) == name && m.Applicability == asset.NotApplicable {
					applicable = false
				}
			}
			if applicable {
				eligible[name]++
				sums[name] += s.Values[name]
			}
		}
	}
	for _, m := range citriage.Metrics {
		if eligible[m] > 0 {
			a.Scores[m] = number(sums[m] / float64(eligible[m]))
		}
	}
	return a
}
func combine(a, b Decision) Decision {
	if a == Blocked || b == Blocked {
		return Blocked
	}
	if a == Fail || b == Fail {
		return Fail
	}
	return Pass
}

// applyStage13 补充计划分母、绝对基线和 critical wrong；仍由原 Gate owner 原子保存。
func applyStage13(s Snapshot, r *Receipt) {
	enabled := false
	for _, u := range s.Candidate.Units {
		for _, m := range u.Metrics {
			enabled = enabled || m.Evaluator.Definition.ImplementationRef == citriage.Implementation
		}
	}
	if !enabled {
		return
	}
	g := &Stage13Gate{Version: "stage13.ci-triage-release.v1", Baseline: stage13Aggregate(s.Baseline), Candidate: stage13Aggregate(s.Candidate), Thresholds: map[string]float64{}, AbsoluteMetricGate: Pass, RegressionGate: Pass, CriticalGate: Pass, ComparabilityGate: Pass, BaselineValid: true, Cases: []Stage13Case{}, FailingMetrics: []string{}, CriticalCases: []string{}, BlockedReasons: []string{}}
	r.Stage13 = g
	// Stage13 critical status 来自完整可决性，不能把 invalid 的零分误记为已知 critical wrong。
	r.CriticalRegressions = []string{}
	kept := r.Issues[:0]
	for _, i := range r.Issues {
		if i.Code != "CRITICAL_REGRESSION" {
			kept = append(kept, i)
		}
	}
	r.Issues = kept
	issue := func(code, scope string, d Decision) {
		r.Issues = append(r.Issues, Issue{code, scope, d})
		if d == Blocked {
			g.BlockedReasons = append(g.BlockedReasons, code)
		}
	}
	if r.Compatibility == Incomparable {
		g.ComparabilityGate = Blocked
		g.RegressionGate = Blocked
		g.CriticalGate = Blocked
		g.AbsoluteMetricGate = Blocked
		g.BlockedReasons = append(g.BlockedReasons, "COMPARABILITY_BLOCKED")
	}
	if g.Baseline.Total == 0 || g.Baseline.Decidable != g.Baseline.Total || g.Baseline.Evaluated != g.Baseline.Total {
		g.BaselineValid = false
	}
	if g.Candidate.Total == 0 || g.Candidate.Decidable != g.Candidate.Total || g.Candidate.Evaluated != g.Candidate.Total {
		issue("CI_DECISION_COVERAGE_INSUFFICIENT", "coverage", Blocked)
		g.AbsoluteMetricGate = Blocked
	}
	for i, m := range citriage.Metrics[:6] {
		threshold := 0.80
		if i < 3 {
			threshold = 0.95
		}
		g.Thresholds[m] = threshold
		b, c := g.Baseline.Scores[m], g.Candidate.Scores[m]
		if b == nil || *b < threshold {
			g.BaselineValid = false
		}
		if c == nil {
			g.AbsoluteMetricGate = Blocked
			issue("CI_METRIC_UNAVAILABLE", m, Blocked)
		} else if *c < threshold {
			g.AbsoluteMetricGate = combine(g.AbsoluteMetricGate, Fail)
			g.FailingMetrics = append(g.FailingMetrics, m)
			issue("CI_ABSOLUTE_THRESHOLD", m, Fail)
		}
		if r.Compatibility != Incomparable && b != nil && c != nil && *c < *b {
			g.RegressionGate = combine(g.RegressionGate, Fail)
			issue("CI_AGGREGATE_REGRESSION", m, Fail)
		}
	}
	left := map[string]Unit{}
	for _, u := range s.Baseline.Units {
		left[u.Key] = u
	}
	for _, u := range s.Candidate.Units {
		b, exists := left[u.Key]
		found := false
		for _, c := range r.Cases {
			found = found || c.Key == u.Key
		}
		if !found {
			// Incomparable 也保留每个计划 Case 的正式比较行；不产生未获授权的 delta。
			r.Cases = append(r.Cases, CaseComparison{Key: u.Key, CaseID: u.CaseID, CaseVersion: u.CaseVersion, Criticality: u.Criticality, BaselineOutcome: b.Outcome, CandidateOutcome: u.Outcome, BaselineTask: b.Task, CandidateTask: u.Task, BaselineRunID: b.RunID, CandidateRunID: u.RunID, BaselineAttemptID: b.AttemptID, CandidateAttemptID: u.AttemptID, Classification: CannotCompare, Metrics: []MetricComparison{}, Reasons: []string{}})
		}
		bs, bok := stage13Score(b)
		cs, cok := stage13Score(u)
		row := Stage13Case{CaseID: u.CaseID, Baseline: bs, Candidate: cs, Delta: map[string]float64{}, Classification: "UNCHANGED"}
		if r.Compatibility == Incomparable || !exists || !bok || !cok {
			row.Classification = "BLOCKED"
			g.RegressionGate = combine(g.RegressionGate, Blocked)
		} else {
			improved, regressed := false, false
			for _, m := range citriage.Metrics {
				row.Delta[m] = cs.Values[m] - bs.Values[m]
				improved = improved || row.Delta[m] > 0
				regressed = regressed || row.Delta[m] < 0
			}
			if regressed {
				row.Classification = "REGRESSED"
				g.RegressionGate = combine(g.RegressionGate, Fail)
			} else if !bs.Decidable || !cs.Decidable {
				row.Classification = "BLOCKED"
				g.RegressionGate = combine(g.RegressionGate, Blocked)
			} else if improved {
				row.Classification = "IMPROVED"
			}
		}
		if u.Criticality == "CRITICAL" {
			if bs.Critical != "PASS" {
				g.BaselineValid = false
			}
			status := Decision(cs.Critical)
			if !cok {
				status = Blocked
			}
			g.CriticalGate = combine(g.CriticalGate, status)
			if status != Pass {
				g.CriticalCases = append(g.CriticalCases, u.CaseID)
				issue("CI_CRITICAL_"+string(status), u.Key, status)
			}
			row.CriticalRegression = r.Compatibility != Incomparable && bs.Critical == "PASS" && cs.Critical == "FAIL"
			if row.CriticalRegression {
				r.CriticalRegressions = append(r.CriticalRegressions, u.Key)
				issue("CRITICAL_REGRESSION", u.Key, Fail)
			}
		}
		g.Cases = append(g.Cases, row)
		for i := range r.Cases {
			if r.Cases[i].Key != u.Key {
				continue
			}
			switch row.Classification {
			case "REGRESSED":
				r.Cases[i].Classification = Regressed
				if row.CriticalRegression {
					r.Cases[i].Classification = CriticalRegression
				}
			case "IMPROVED":
				r.Cases[i].Classification = Improved
			case "BLOCKED":
				if r.Compatibility == Incomparable {
					r.Cases[i].Classification = CannotCompare
				} else {
					r.Cases[i].Classification = Insufficient
				}
			case "UNCHANGED":
				r.Cases[i].Classification = Unchanged
			}
		}
	}
	if g.RegressionGate != Pass {
		issue("CI_REGRESSION_GATE", "regression", g.RegressionGate)
	}
	if !g.BaselineValid {
		issue("BASELINE_INVALID", "baseline", Blocked)
	}
	r.CriticalRegressions = unique(r.CriticalRegressions)
	g.BlockedReasons = unique(g.BlockedReasons)
}
