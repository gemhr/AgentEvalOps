package decision

import (
	"math"
	"sort"
	"strings"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
)

func ratio(n, d int) *float64 {
	if d == 0 {
		return nil
	}
	v := float64(n) / float64(d)
	return &v
}
func number(v float64) *float64 { return &v }
func digest(v any) string {
	j, e := asset.Freeze(v)
	if e != nil {
		return ""
	}
	return j.Digest()
}
func unique(v []string) []string {
	sort.Strings(v)
	out := []string{}
	for _, s := range v {
		if len(out) == 0 || out[len(out)-1] != s {
			out = append(out, s)
		}
	}
	return out
}
func coverage(s Source, task bool) Coverage {
	c := Coverage{ExpectedSlots: s.ExpectedSlots, CompletedSlots: s.CompletedSlots, SamplingUnit: s.SamplingUnit}
	success := 0
	for _, u := range s.Units {
		if task {
			if u.Task == "NOT_APPLICABLE" {
				c.NotApplicable++
				continue
			}
			c.Eligible++
			switch u.Task {
			case "SUCCESS":
				c.Decidable++
				c.Success++
				success++
			case "FAILURE":
				c.Decidable++
				c.Failure++
			case "INCONCLUSIVE":
				c.Inconclusive++
			case "UNKNOWN_EXECUTION":
				c.Unknown++
			case "MISSING_EVIDENCE":
				c.Missing++
			case "UNSUPPORTED":
				c.Unsupported++
			case "EVALUATOR_ERROR":
				c.Error++
			}
		} else {
			for _, m := range u.Metrics {
				if m.Applicability == asset.NotApplicable {
					c.NotApplicable++
					continue
				}
				c.Eligible++
				switch {
				case u.Outcome == ev.Unknown:
					c.Unknown++
				case m.Verdict == "ERROR":
					c.Error++
				case m.Applicability == asset.MissingEvidence || m.ResultID == "":
					c.Missing++
				case m.Applicability == asset.UnsupportedEvidence:
					c.Unsupported++
				case m.Verdict == "INCONCLUSIVE":
					c.Inconclusive++
				case m.Verdict == "PASS" || m.Verdict == "FAIL":
					c.Decidable++
				}
			}
		}
	}
	c.Decision = ratio(c.Decidable, c.Eligible)
	c.Evaluation = ratio(c.CompletedSlots, c.ExpectedSlots)
	if task {
		c.TaskRate = ratio(success, c.Decidable)
	}
	return c
}

// Compare 是纯计算；全部输入已由 PG 冻结，不读取当前配置或 latest。
func Compare(s Snapshot) (Receipt, error) {
	if s.Policy.Validate() != nil {
		return Receipt{}, asset.ErrInvalid
	}
	r := Receipt{GateID: s.Command.ID, ProjectID: s.ProjectID, Version: Contract, Compatibility: Comparable, Baseline: s.Baseline, Candidate: s.Candidate, PolicyRef: s.Command.Policy, PolicyDigest: s.PolicyDigest, SnapshotDigest: digest(s), CreatedAt: s.CapturedAt, Actor: s.Actor, Source: s.Source, Reasons: []string{}, Cases: []CaseComparison{}, Metrics: []MetricComparison{}, CriticalRegressions: []string{}, Exceptions: []Exception{}, Differences: []Difference{}, Issues: []Issue{}}
	issue := func(code, scope string, d Decision) { r.Issues = append(r.Issues, Issue{code, scope, d}) }
	check := func(dimension, left, right, reason string, mode DimensionMode) {
		if mode == NotApplicable {
			return
		}
		if left == right && left != "UNKNOWN" && left != "" {
			return
		}
		d := Difference{Dimension: dimension, Baseline: left, Candidate: right, Reason: reason, Mode: mode}
		if mode == MayDiffer && left != "" && right != "" && left != "UNKNOWN" && right != "UNKNOWN" {
			r.Differences = append(r.Differences, d)
			return
		}
		accepted := false
		if mode == RequiresProof && left != "" && right != "" && left != "UNKNOWN" && right != "UNKNOWN" {
			for _, a := range s.Policy.AcceptedDifferences {
				if a.Dimension == dimension && a.Baseline == left && a.Candidate == right && (a.ExpiresAt == nil || a.ExpiresAt.After(s.CapturedAt)) {
					accepted = true
					d.AcceptedPolicy = s.Command.Policy.EntityID + "@" + s.Command.Policy.Version + ":" + a.Proof
					break
				}
			}
		}
		if accepted {
			if r.Compatibility == Comparable {
				r.Compatibility = Accepted
			}
		} else {
			r.Compatibility = Incomparable
			issue(reason, dimension, Blocked)
		}
		r.Differences = append(r.Differences, d)
	}
	if s.Baseline.Owner != s.Candidate.Owner {
		check("owner", s.Baseline.Owner, s.Candidate.Owner, "ONLINE_OFFLINE_POPULATION_MISMATCH", MustMatch)
	}
	if s.Candidate.Owner == "OFFLINE" && !s.Policy.Offline || s.Candidate.Owner == "ONLINE" && !s.Policy.Online {
		issue("POLICY_NOT_APPLICABLE", "source", Blocked)
	}
	for _, reason := range append(append([]string{}, s.Baseline.Reasons...), s.Candidate.Reasons...) {
		issue(reason, "source", Blocked)
		r.Compatibility = Incomparable
	}
	check("sampling_unit", s.Baseline.SamplingUnit, s.Candidate.SamplingUnit, "SAMPLING_POPULATION_MISMATCH", MustMatch)
	keys := map[string]bool{}
	for k := range s.Baseline.Dimensions {
		keys[k] = true
	}
	for k := range s.Candidate.Dimensions {
		keys[k] = true
	}
	ordered := []string{}
	for k := range keys {
		ordered = append(ordered, k)
	}
	sort.Strings(ordered)
	for _, k := range ordered {
		mode := MustMatch
		reason := "SUBJECT_MISMATCH"
		switch k {
		case "subject_version":
			mode = MayDiffer
		case "case_set":
			reason = "CASE_SET_MISMATCH"
		case "target_version", "target_config":
			mode = RequiresProof
			reason = "TARGET_CONTRACT_MISMATCH"
		case "dataset":
			mode = RequiresProof
			reason = "DATASET_VERSION_MISMATCH"
		case "suite":
			mode = RequiresProof
			reason = "SUITE_VERSION_MISMATCH"
		case "rule":
			mode = RequiresProof
			reason = "SAMPLING_POPULATION_MISMATCH"
		case "population", "filter", "sampling", "window", "source", "trust":
			reason = "SAMPLING_POPULATION_MISMATCH"
		}
		check(k, s.Baseline.Dimensions[k], s.Candidate.Dimensions[k], reason, mode)
	}
	bf, cf := facts(s.Baseline), facts(s.Candidate)
	metricKeys := map[string]bool{}
	for k := range bf {
		metricKeys[k] = true
	}
	for k := range cf {
		metricKeys[k] = true
	}
	mk := []string{}
	for k := range metricKeys {
		mk = append(mk, k)
	}
	sort.Strings(mk)
	for _, k := range mk {
		l, lok := bf[k]
		c, cok := cf[k]
		if !lok || !cok {
			check("metric:"+k, digest(l.Metric), digest(c.Metric), "METRIC_SEMANTICS_MISMATCH", MustMatch)
			continue
		}
		prefix := "metric:" + k + ":"
		check(prefix+"definition", digest(l.Metric), digest(c.Metric), "METRIC_SEMANTICS_MISMATCH", RequiresProof)
		check(prefix+"evaluator", digest(l.Evaluator.Identity), digest(c.Evaluator.Identity), "EVALUATOR_VERSION_MISMATCH", RequiresProof)
		check(prefix+"evidence_schema", schemaClasses(s.Baseline, k), schemaClasses(s.Candidate, k), "EVIDENCE_SEMANTICS_MISMATCH", RequiresProof)
		for _, dimension := range []string{"clock_boundary", "tokenizer", "usage_source", "currency", "cost_source", "price_ref"} {
			if l.ResultID == "" || c.ResultID == "" {
				continue
			}
			left, right := factDimension(l, dimension), factDimension(c, dimension)
			if left != "NOT_APPLICABLE" || right != "NOT_APPLICABLE" {
				check(prefix+dimension, left, right, "EVIDENCE_SEMANTICS_MISMATCH", RequiresProof)
			}
		}
		for dimension, pair := range map[string][2]string{"implementation": {l.Evaluator.Definition.ImplementationRef, c.Evaluator.Definition.ImplementationRef}, "prompt": {digest(l.Evaluator.Definition.PromptRef), digest(c.Evaluator.Definition.PromptRef)}, "rubric": {l.Evaluator.Definition.Rubric.String(), c.Evaluator.Definition.Rubric.String()}, "schema": {l.Evaluator.Definition.SchemaVersion, c.Evaluator.Definition.SchemaVersion}, "normalization": {l.Evaluator.Definition.Normalization, c.Evaluator.Definition.Normalization}, "config": {l.Evaluator.Definition.Config.String(), c.Evaluator.Definition.Config.String()}, "requested_model": {digest(l.Evaluator.Definition.Model), digest(c.Evaluator.Definition.Model)}} {
			check(prefix+dimension, pair[0], pair[1], "EVALUATOR_VERSION_MISMATCH", RequiresProof)
		}
		// Judge 身份逐个结果校验，不能仅检查第一个 sample。
		if l.Evaluator.Definition.Kind == metric.LLMJudge || c.Evaluator.Definition.Kind == metric.LLMJudge {
			la, lc := providerIdentities(s.Baseline, k), providerIdentities(s.Candidate, k)
			if la == "REQUESTED_ACTUAL_MISMATCH" || lc == "REQUESTED_ACTUAL_MISMATCH" {
				issue("MODEL_IDENTITY_MISMATCH", prefix+"requested_actual", Blocked)
				r.Compatibility = Incomparable
			}
			if la == "UNKNOWN" || lc == "UNKNOWN" {
				check(prefix+"actual_model", la, lc, "MODEL_IDENTITY_UNKNOWN", MustMatch)
			} else {
				check(prefix+"actual_model", la, lc, "MODEL_IDENTITY_MISMATCH", RequiresProof)
			}
		}
	}
	if s.Baseline.Owner == "OFFLINE" && s.Candidate.Owner == "OFFLINE" {
		aligned := map[string]Unit{}
		for _, u := range s.Baseline.Units {
			aligned[u.Key] = u
		}
		for _, u := range s.Candidate.Units {
			b, ok := aligned[u.Key]
			if !ok {
				continue
			}
			for _, m := range u.Metrics {
				lm := findMetric(b, m.Metric.Identity.Ref)
				if lm.ResultID == "" || m.ResultID == "" {
					continue
				}
				check("case:"+u.Key+":metric:"+m.Metric.Identity.Ref.EntityID+":evidence_schema", factDimension(lm, "evidence_schema"), factDimension(m, "evidence_schema"), "EVIDENCE_SEMANTICS_MISMATCH", RequiresProof)
			}
		}
	}
	// 同一侧也必须保持 metric/evaluator/evidence schema 一致，不能用首条结果遮蔽漂移。
	for _, side := range []Source{s.Baseline, s.Candidate} {
		first := facts(side)
		for _, u := range side.Units {
			for _, m := range u.Metrics {
				f := first[m.Metric.Identity.Ref.EntityID]
				if digest(m.Metric) != digest(f.Metric) || digest(m.Evaluator) != digest(f.Evaluator) {
					issue("METRIC_SEMANTICS_MISMATCH", u.Key, Blocked)
					r.Compatibility = Incomparable
				}
				for _, dimension := range []string{"clock_boundary", "tokenizer", "usage_source", "currency", "cost_source", "price_ref"} {
					if m.ResultID == "" || f.ResultID == "" {
						continue
					}
					if factDimension(m, dimension) != factDimension(f, dimension) {
						issue("EVIDENCE_SEMANTICS_MISMATCH", u.Key, Blocked)
						r.Compatibility = Incomparable
					}
				}
			}
		}
	}
	task := s.Policy.TaskSuccess != nil
	if task {
		for _, side := range []Source{s.Baseline, s.Candidate} {
			m, ok := facts(side)[s.Policy.TaskSuccess.Metric.EntityID]
			if !ok || m.Metric.Identity.Ref != s.Policy.TaskSuccess.Metric || metricKey(m.Evaluator) != "task_success.v1" {
				issue("TASK_METRIC_BINDING_INVALID", "task_success", Blocked)
			}
		}
	}
	r.Coverage.Baseline = coverage(s.Baseline, task)
	r.Coverage.Candidate = coverage(s.Candidate, task)
	r.Coverage.BaselineEvaluation = coverage(s.Baseline, false)
	r.Coverage.CandidateEvaluation = coverage(s.Candidate, false)
	cc, bc := r.Coverage.Candidate, r.Coverage.Baseline
	if bc.Decision == nil || *bc.Decision < s.Policy.Coverage.MinimumDecision || bc.Evaluation == nil || *bc.Evaluation < s.Policy.Coverage.MinimumEvaluation {
		issue("BASELINE_COVERAGE_INSUFFICIENT", "coverage", Blocked)
	}
	for _, v := range []struct {
		code    string
		value   *float64
		minimum float64
	}{{"DECISION_COVERAGE_INSUFFICIENT", cc.Decision, s.Policy.Coverage.MinimumDecision}, {"EVALUATION_COVERAGE_INSUFFICIENT", cc.Evaluation, s.Policy.Coverage.MinimumEvaluation}} {
		if v.value == nil || *v.value < v.minimum {
			issue(v.code, "coverage", Blocked)
		}
	}
	if cc.Decision != nil && bc.Decision != nil && *bc.Decision-*cc.Decision > s.Policy.Coverage.DropTolerance {
		issue("COVERAGE_DROP", "coverage", Blocked)
	}
	for _, v := range []struct {
		code string
		n    int
		max  float64
	}{{"MISSING_EVIDENCE_LIMIT", cc.Missing, s.Policy.Coverage.MaximumMissing}, {"UNKNOWN_EXECUTION_LIMIT", cc.Unknown, s.Policy.Coverage.MaximumUnknown}, {"EVALUATOR_ERROR_LIMIT", cc.Error, s.Policy.Coverage.MaximumError}, {"UNSUPPORTED_LIMIT", cc.Unsupported, s.Policy.Coverage.MaximumUnsupported}} {
		if q := ratio(v.n, cc.Eligible); q != nil && *q > v.max {
			issue(v.code, "coverage", Blocked)
		}
	}
	// Task decision coverage 不能遮蔽其他 evaluator 的 pipeline failure。
	ec := r.Coverage.CandidateEvaluation
	for _, v := range []struct {
		code    string
		n       int
		maximum float64
	}{
		{"MISSING_EVIDENCE_LIMIT", ec.Missing, s.Policy.Coverage.MaximumMissing},
		{"UNKNOWN_EXECUTION_LIMIT", ec.Unknown, s.Policy.Coverage.MaximumUnknown},
		{"EVALUATOR_ERROR_LIMIT", ec.Error, s.Policy.Coverage.MaximumError},
		{"UNSUPPORTED_LIMIT", ec.Unsupported, s.Policy.Coverage.MaximumUnsupported},
	} {
		if value := ratio(v.n, ec.Eligible); value != nil && *value > v.maximum {
			issue(v.code, "evaluation_coverage", Blocked)
		}
	}
	if cc.Eligible == 0 {
		issue("EMPTY_ELIGIBLE_POPULATION", "coverage", Blocked)
	}
	if r.Compatibility != Incomparable {
		if s.Candidate.Owner == "OFFLINE" {
			left := map[string]Unit{}
			for _, u := range s.Baseline.Units {
				left[u.Key] = u
			}
			for _, u := range s.Candidate.Units {
				b, ok := left[u.Key]
				if !ok {
					issue("CASE_SET_MISMATCH", u.Key, Blocked)
					r.Compatibility = Incomparable
					continue
				}
				row := CaseComparison{Key: u.Key, CaseID: u.CaseID, CaseVersion: u.CaseVersion, Criticality: u.Criticality, BaselineOutcome: b.Outcome, CandidateOutcome: u.Outcome, BaselineTask: b.Task, CandidateTask: u.Task, TaskTransition: b.Task + "→" + u.Task, BaselineRunID: b.RunID, CandidateRunID: u.RunID, BaselineAttemptID: b.AttemptID, CandidateAttemptID: u.AttemptID, Classification: Unchanged, Reasons: []string{}, Metrics: []MetricComparison{}}
				if b.CaseVersion != u.CaseVersion || b.CaseDigest != u.CaseDigest {
					row.Classification = CannotCompare
					row.Reasons = append(row.Reasons, "CASE_VERSION_MISMATCH")
					issue("CASE_VERSION_MISMATCH", u.Key, Blocked)
					r.Compatibility = Incomparable
				} else if task {
					switch {
					case b.Task == "SUCCESS" && u.Task == "FAILURE":
						row.Classification = Regressed
						row.Reasons = append(row.Reasons, "TASK_SUCCESS_REGRESSED")
					case b.Task == "FAILURE" && u.Task == "SUCCESS":
						row.Classification = Improved
					case b.Task == "SUCCESS" && u.Task != "SUCCESS":
						row.Classification = Insufficient
						row.Reasons = append(row.Reasons, "TASK_DECISION_LOST")
						issue("TASK_DECISION_LOST", u.Key, Blocked)
					}
				}
				for _, rule := range s.Policy.Metrics {
					if !rule.PerCase {
						continue
					}
					lm, cm := findMetric(b, rule.Metric), findMetric(u, rule.Metric)
					m := compareValues(rule, lm.Metric.Definition, MetricValue(lm), MetricValue(cm))
					m.BaselineApplicability, m.CandidateApplicability = lm.Applicability, cm.Applicability
					m.BaselineVerdict, m.CandidateVerdict = lm.Verdict, cm.Verdict
					if lm.Verdict == "PASS" && cm.Verdict == "FAIL" && lm.Applicability == asset.Applicable && cm.Applicability == asset.Applicable {
						m.Classification = Regressed
						m.Reasons = append(m.Reasons, "VERDICT_REGRESSED")
					}
					row.Metrics = append(row.Metrics, m)
					if m.Classification == Regressed {
						row.Classification = Regressed
						row.Reasons = append(row.Reasons, "METRIC_REGRESSED")
					} else if m.Classification == Insufficient && rule.Required {
						if row.Classification != Regressed {
							row.Classification = Insufficient
						}
						row.Reasons = append(row.Reasons, "REQUIRED_METRIC_MISSING")
						issue("REQUIRED_METRIC_MISSING", u.Key, Blocked)
					}
				}
				critical := one(u.Criticality, s.Policy.Criticalities...) || one(b.Criticality, s.Policy.Criticalities...)
				if critical {
					for _, cm := range u.Metrics {
						lm := findMetric(b, cm.Metric.Identity.Ref)
						if cm.Evaluator.Required && lm.Verdict == "PASS" && cm.Verdict == "FAIL" && lm.Applicability == asset.Applicable && cm.Applicability == asset.Applicable {
							row.Classification = Regressed
							row.Reasons = append(row.Reasons, "VERDICT_REGRESSED")
						}
					}
				}
				if critical && s.Policy.CriticalMissing {
					for _, m := range u.Metrics {
						if m.Evaluator.Required && (m.ResultID == "" || m.Applicability == asset.MissingEvidence || m.Applicability == asset.UnsupportedEvidence || one(m.Verdict, "ERROR", "INCONCLUSIVE")) {
							issue("CRITICAL_EVIDENCE_MISSING", u.Key, Blocked)
						}
					}
				}
				if row.Classification == Regressed {
					if critical {
						row.Classification = CriticalRegression
						r.CriticalRegressions = append(r.CriticalRegressions, u.Key)
						issue("CRITICAL_REGRESSION", u.Key, Fail)
					} else if s.Policy.BlockPerCase {
						issue("CASE_REGRESSION", u.Key, Fail)
					}
				}
				if critical && s.Policy.CriticalMissing && (row.Classification == Insufficient || u.Task == "MISSING_EVIDENCE" || u.Task == "UNKNOWN_EXECUTION" || u.Task == "EVALUATOR_ERROR") {
					issue("CRITICAL_EVIDENCE_MISSING", u.Key, Blocked)
				}
				r.Cases = append(r.Cases, row)
				delete(left, u.Key)
			}
			if len(left) > 0 {
				issue("CASE_SET_MISMATCH", "cases", Blocked)
				r.Compatibility = Incomparable
			}
		}
	}
	// 不可比时不计算任何 regression delta。
	if r.Compatibility != Incomparable {
		if task {
			r.TaskSuccess = compareValues(*s.Policy.TaskSuccess, metric.Definition{Direction: metric.Higher}, bc.TaskRate, cc.TaskRate)
			r.TaskSuccess.BaselineCoverage = bc.Decision
			r.TaskSuccess.CandidateCoverage = cc.Decision
			if bc.Decision == nil || cc.Decision == nil || *bc.Decision < s.Policy.TaskSuccess.MinimumCoverage || *cc.Decision < s.Policy.TaskSuccess.MinimumCoverage {
				issue("TASK_COVERAGE_INSUFFICIENT", "task_success", Blocked)
			}
			if r.TaskSuccess.Classification == Regressed {
				issue("TASK_RATE_REGRESSED", "task_success", Fail)
			} else if r.TaskSuccess.Classification == Insufficient {
				issue("TASK_RATE_UNAVAILABLE", "task_success", Blocked)
			}
		}
		for _, rule := range s.Policy.Metrics {
			m := aggregateComparison(rule, s.Baseline, s.Candidate)
			r.Metrics = append(r.Metrics, m)
			if m.Classification == Regressed {
				issue("METRIC_REGRESSED", rule.Metric.EntityID, Fail)
			} else if (m.Classification == Insufficient || m.Classification == CannotCompare) && rule.Required {
				issue("REQUIRED_METRIC_UNAVAILABLE", rule.Metric.EntityID, Blocked)
			}
		}
	}
	applyStage13(s, &r)
	for _, c := range r.Cases {
		switch c.Classification {
		case Regressed, CriticalRegression:
			r.Summary.Regressions++
		case Improved:
			r.Summary.Improvements++
		case Unchanged:
			r.Summary.Unchanged++
		case Insufficient:
			r.Summary.Insufficient++
		case CannotCompare:
			r.Summary.Incomparable++
		}
	}
	sort.Slice(r.Issues, func(i, j int) bool {
		a, b := r.Issues[i], r.Issues[j]
		return a.Code+"/"+a.Scope+"/"+string(a.Decision) < b.Code+"/"+b.Scope+"/"+string(b.Decision)
	})
	r.Decision = Pass
	for _, i := range r.Issues {
		applied := false
		for _, e := range s.Policy.Exceptions {
			if i.Decision == Fail && e.ReasonCode == i.Code && e.Scope == i.Scope && (e.ExpiresAt == nil || e.ExpiresAt.After(s.CapturedAt)) {
				r.Exceptions = append(r.Exceptions, e)
				applied = true
				break
			}
		}
		if applied {
			continue
		}
		r.Reasons = append(r.Reasons, i.Code)
		if i.Decision == Blocked {
			r.Decision = Blocked
		} else if r.Decision == Pass {
			r.Decision = Fail
		}
	}
	r.Reasons = unique(r.Reasons)
	sort.Slice(r.Differences, func(i, j int) bool { return r.Differences[i].Dimension < r.Differences[j].Dimension })
	return r, nil
}

func facts(s Source) map[string]MetricFact {
	out := map[string]MetricFact{}
	for _, u := range s.Units {
		for _, m := range u.Metrics {
			if old, ok := out[m.Metric.Identity.Ref.EntityID]; !ok || old.ResultID == "" && m.ResultID != "" {
				out[m.Metric.Identity.Ref.EntityID] = m
			}
		}
	}
	return out
}

func schemaClasses(s Source, key string) string {
	classes := []string{}
	for _, u := range s.Units {
		for _, m := range u.Metrics {
			if m.Metric.Identity.Ref.EntityID == key && m.ResultID != "" {
				classes = append(classes, factDimension(m, "evidence_schema"))
			}
		}
	}
	if len(classes) == 0 {
		return "NOT_APPLICABLE"
	}
	return digest(unique(classes))
}

func factDimension(m MetricFact, key string) string {
	if key == "evidence_schema" {
		if len(m.EvidenceSchemas) == 0 {
			return "NOT_APPLICABLE"
		}
		return digest(m.EvidenceSchemas)
	}
	var p map[string]asset.JSON
	_ = m.Provenance.Decode(&p)
	required := m.Metric.Definition.ValueType == metric.Duration && key == "clock_boundary" || m.Metric.Definition.ValueType == metric.Count && one(key, "tokenizer", "usage_source") || m.Metric.Definition.ValueType == metric.Cost && one(key, "currency", "cost_source", "price_ref")
	if !required {
		return "NOT_APPLICABLE"
	}
	var v string
	if p[key].Decode(&v) != nil || v == "" {
		return "UNKNOWN"
	}
	return v
}
func findMetric(u Unit, r asset.Ref) MetricFact {
	for _, m := range u.Metrics {
		if m.Metric.Identity.Ref == r {
			return m
		}
	}
	return MetricFact{}
}
func providerIdentities(s Source, key string) string {
	ids := []string{}
	for _, u := range s.Units {
		for _, m := range u.Metrics {
			if m.Metric.Identity.Ref.EntityID != key {
				continue
			}
			selected := map[string]bool{}
			for _, id := range m.SelectedCallIDs {
				selected[id] = true
			}
			if len(selected) == 0 {
				return "UNKNOWN"
			}
			count := 0
			for _, c := range m.Calls {
				if !selected[c.ID] {
					continue
				}
				count++
				model := m.Evaluator.Definition.Model
				if model == nil || one(c.ActualProvider, "", "UNKNOWN") || one(c.ActualModel, "", "UNKNOWN") || one(c.ActualRevision, "", "UNKNOWN") {
					return "UNKNOWN"
				}
				if c.ActualProvider != model.Provider || c.ActualModel != model.Model || c.ActualRevision != model.Revision {
					return "REQUESTED_ACTUAL_MISMATCH"
				}
				ids = append(ids, digest([]string{c.ActualProvider, c.ActualModel, c.ActualRevision}))
			}
			if count != len(selected) {
				return "UNKNOWN"
			}
		}
	}
	if len(ids) == 0 {
		return "UNKNOWN"
	}
	return digest(unique(ids))
}
func compareValues(rule MetricRule, d metric.Definition, b, c *float64) MetricComparison {
	r := MetricComparison{Metric: rule.Metric, Aggregation: rule.Aggregation, Baseline: b, Candidate: c, Classification: Unchanged, Reasons: []string{}}
	if b == nil || c == nil || !finite(*b) || !finite(*c) {
		r.Classification = Insufficient
		r.Reasons = append(r.Reasons, "NULLABLE_METRIC")
		return r
	}
	delta := *c - *b
	r.Delta = &delta
	if *b != 0 {
		r.RelativeDelta = number(delta / math.Abs(*b))
	}
	worse := 0.0
	switch d.Direction {
	case metric.Higher:
		worse = -delta
	case metric.Lower:
		worse = delta
	case metric.None:
		if rule.Aggregation == "rate" || rule.Aggregation == "count" {
			worse = -delta
		} else if rule.TargetRange != nil {
			distance := func(x float64) float64 { return math.Max(0, math.Max(rule.TargetRange[0]-x, x-rule.TargetRange[1])) }
			worse = distance(*c) - distance(*b)
		} else {
			r.Classification = CannotCompare
			r.Reasons = append(r.Reasons, "CATEGORICAL_DIRECTION_UNDEFINED")
			return r
		}
	default:
		r.Classification = CannotCompare
		return r
	}
	failed := rule.Minimum != nil && *c < *rule.Minimum || rule.Maximum != nil && *c > *rule.Maximum || rule.TargetRange != nil && (*c < rule.TargetRange[0] || *c > rule.TargetRange[1])
	if rule.Minimum == nil && rule.Maximum == nil && rule.TargetRange == nil && rule.AbsoluteTolerance == nil && rule.RelativeTolerance == nil && worse > 0 {
		failed = true
	}
	if rule.AbsoluteTolerance != nil && worse > *rule.AbsoluteTolerance {
		failed = true
	}
	if rule.RelativeTolerance != nil && worse > 0 {
		if *b == 0 {
			r.Classification = Insufficient
			r.Reasons = append(r.Reasons, "RELATIVE_BASELINE_ZERO")
			return r
		}
		if worse/math.Abs(*b) > *rule.RelativeTolerance {
			failed = true
		}
	}
	if failed {
		r.Classification = Regressed
	} else if worse < 0 {
		r.Classification = Improved
	}
	return r
}
func aggregateComparison(rule MetricRule, b, c Source) MetricComparison {
	def := facts(b)[rule.Metric.EntityID].Metric.Definition
	if !aggregationAllowed(def, rule.Aggregation) {
		return MetricComparison{Metric: rule.Metric, Aggregation: rule.Aggregation, Classification: CannotCompare, Reasons: []string{"AGGREGATION_UNDEFINED"}}
	}
	value := func(s Source) (*float64, *float64, *float64, int, int) {
		values := []float64{}
		expected := 0
		for _, u := range s.Units {
			m := findMetric(u, rule.Metric)
			m.Value = MetricValue(m)
			if m.Metric.Identity.Ref != rule.Metric {
				expected++
				continue
			}
			if m.Applicability == asset.NotApplicable {
				continue
			}
			expected++
			if m.Applicability != asset.Applicable || !one(m.Verdict, "PASS", "FAIL") {
				continue
			}
			switch rule.Aggregation {
			case "count":
				if def.ValueType == metric.Count {
					if m.Value != nil && finite(*m.Value) {
						values = append(values, *m.Value)
					}
				} else {
					values = append(values, 1)
				}
			case "rate":
				if m.Verdict == "PASS" {
					values = append(values, 1)
				} else {
					values = append(values, 0)
				}
			default:
				if m.Value != nil && finite(*m.Value) {
					values = append(values, *m.Value)
				}
			}
		}
		cov := ratio(len(values), expected)
		if rule.Aggregation == "coverage" {
			return cov, cov, nil, len(values), expected
		}
		if len(values) == 0 {
			return nil, cov, nil, len(values), expected
		}
		sum := 0.0
		for _, v := range values {
			sum += v
		}
		mean := sum / float64(len(values))
		variance := 0.0
		for _, v := range values {
			variance += (v - mean) * (v - mean)
		}
		variance /= float64(len(values))
		if rule.Aggregation == "count" {
			return number(sum), cov, number(variance), len(values), expected
		}
		if rule.Aggregation == "source_percentile" && len(values) != 1 {
			return nil, cov, nil, len(values), expected
		}
		return number(mean), cov, number(variance), len(values), expected
	}
	bv, bc, bvar, bn, be := value(b)
	cv, cc, cvar, cn, ce := value(c)
	r := compareValues(rule, def, bv, cv)
	r.BaselineCount, r.BaselineEligible = bn, be
	r.CandidateCount, r.CandidateEligible = cn, ce
	r.BaselineCoverage = bc
	r.CandidateCoverage = cc
	r.BaselineVariance = bvar
	r.CandidateVariance = cvar
	if bc == nil || cc == nil || *bc < rule.MinimumCoverage || *cc < rule.MinimumCoverage {
		r.Classification = Insufficient
		r.Reasons = append(r.Reasons, "METRIC_COVERAGE_INSUFFICIENT")
	}
	return r
}
func aggregationAllowed(d metric.Definition, mode string) bool {
	text := strings.ToLower(d.Aggregation)
	switch mode {
	case "coverage":
		return d.Name != ""
	case "rate":
		return strings.Contains(text, "rate") || strings.Contains(text, "success /") || d.ValueType == metric.Boolean
	case "count":
		return d.ValueType == metric.Count || strings.Contains(text, "count")
	case "mean":
		return d.ValueType != metric.Enum && strings.Contains(text, "mean")
	case "source_percentile":
		return d.ValueType != metric.Enum && strings.Contains(text, "source_percentile")
	}
	return false
}
