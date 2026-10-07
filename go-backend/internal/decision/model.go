// Package decision 拥有比较与发布政策决定，只消费不可变评测事实。
package decision

import (
	"crypto/sha256"
	"fmt"
	"math"
	"sort"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
)

const Contract = "stage12.comparison-gate.v1"
const Schema = "c12a00800001"

type Decision string

const (
	Pass    Decision = "PASS"
	Fail    Decision = "FAIL"
	Blocked Decision = "BLOCKED"
)

func ExitCode(d Decision) int {
	switch d {
	case Pass:
		return 0
	case Fail:
		return 1
	case Blocked:
		return 2
	default:
		return 3
	}
}

type Compatibility string

const (
	Comparable   Compatibility = "COMPARABLE"
	Incomparable Compatibility = "INCOMPARABLE"
	Accepted     Compatibility = "COMPARABLE_WITH_ACCEPTED_DIFFERENCE"
)

type Classification string

const (
	Improved           Classification = "IMPROVED"
	Unchanged          Classification = "UNCHANGED"
	Regressed          Classification = "REGRESSED"
	CriticalRegression Classification = "CRITICAL_REGRESSION"
	CannotCompare      Classification = "INCOMPARABLE"
	Insufficient       Classification = "INSUFFICIENT_COVERAGE"
)

type DimensionMode string

const (
	MustMatch     DimensionMode = "MUST_MATCH"
	MayDiffer     DimensionMode = "MAY_DIFFER"
	RequiresProof DimensionMode = "REQUIRES_EQUIVALENCE_PROOF"
	NotApplicable DimensionMode = "NOT_APPLICABLE"
)

type Difference struct {
	Dimension, Baseline, Candidate, Reason string
	Mode                                   DimensionMode
	AcceptedPolicy                         string
}

// AcceptedDifference 是已发布政策中的精确映射，Proof 为受控发布者核验的不可变证据引用。
type AcceptedDifference struct {
	Dimension, Baseline, Candidate, Proof, Actor, Reason string
	ExpiresAt                                            *time.Time
}
type Exception struct {
	ReasonCode, Scope, Actor, Reason, Proof string
	ExpiresAt                               *time.Time
}
type CoveragePolicy struct {
	MinimumDecision, MinimumEvaluation, MaximumMissing, MaximumUnknown, MaximumError, MaximumUnsupported, DropTolerance float64
}
type MetricRule struct {
	Metric                                                 asset.Ref
	Aggregation                                            string
	Minimum, Maximum, AbsoluteTolerance, RelativeTolerance *float64
	MinimumCoverage                                        float64
	Required, PerCase                                      bool
	// TARGET_RANGE 仅在定义 Direction=NONE 且值为 numeric 时启用；categorical 只计算声明的 rate/count。
	TargetRange *[2]float64
}
type Policy struct {
	Offline, Online     bool
	Criticalities       []string
	CriticalMissing     bool
	BlockPerCase        bool
	Coverage            CoveragePolicy
	TaskSuccess         *MetricRule
	Metrics             []MetricRule
	AcceptedDifferences []AcceptedDifference
	Exceptions          []Exception
}
type EvaluationPolicyVersion = asset.Version[Policy]
type PolicyVersion = EvaluationPolicyVersion

func (p Policy) Validate() error {
	if !p.Offline && !p.Online {
		return asset.ErrInvalid
	}
	for _, n := range []float64{p.Coverage.MinimumDecision, p.Coverage.MinimumEvaluation, p.Coverage.MaximumMissing, p.Coverage.MaximumUnknown, p.Coverage.MaximumError, p.Coverage.MaximumUnsupported, p.Coverage.DropTolerance} {
		if !finite(n) || n < 0 || n > 1 {
			return asset.ErrInvalid
		}
	}
	seen := map[asset.Ref]bool{}
	for _, r := range p.Metrics {
		if r.Metric.Validate() != nil || seen[r.Metric] || r.Validate() != nil {
			return asset.ErrInvalid
		}
		seen[r.Metric] = true
	}
	if p.TaskSuccess != nil && (p.TaskSuccess.Validate() != nil || p.TaskSuccess.Metric.Validate() != nil || p.TaskSuccess.Aggregation != "rate") {
		return asset.ErrInvalid
	}
	if p.TaskSuccess == nil && len(p.Metrics) == 0 {
		return asset.ErrInvalid
	}
	for _, c := range p.Criticalities {
		if c != "CRITICAL" && c != "HIGH" && c != "NORMAL" {
			return asset.ErrInvalid
		}
	}
	for _, d := range p.AcceptedDifferences {
		if !asset.Text(d.Dimension) || !asset.ContentText(d.Baseline) || !asset.ContentText(d.Candidate) || !asset.Text(d.Proof) || !asset.Text(d.Actor) || !asset.ContentText(d.Reason) {
			return asset.ErrInvalid
		}
	}
	for _, e := range p.Exceptions {
		if !asset.Text(e.ReasonCode) || !asset.Text(e.Scope) || !asset.Text(e.Actor) || !asset.ContentText(e.Reason) || !asset.Text(e.Proof) {
			return asset.ErrInvalid
		}
	}
	return nil
}
func (r MetricRule) Validate() error {
	if !one(r.Aggregation, "mean", "count", "rate", "coverage", "source_percentile") || !finite(r.MinimumCoverage) || r.MinimumCoverage < 0 || r.MinimumCoverage > 1 {
		return asset.ErrInvalid
	}
	for _, n := range []*float64{r.Minimum, r.Maximum, r.AbsoluteTolerance, r.RelativeTolerance} {
		if n != nil && !finite(*n) {
			return asset.ErrInvalid
		}
	}
	if r.AbsoluteTolerance != nil && *r.AbsoluteTolerance < 0 || r.RelativeTolerance != nil && *r.RelativeTolerance < 0 {
		return asset.ErrInvalid
	}
	if r.Minimum != nil && r.Maximum != nil && *r.Minimum > *r.Maximum {
		return asset.ErrInvalid
	}
	if r.TargetRange != nil && (!finite(r.TargetRange[0]) || !finite(r.TargetRange[1]) || r.TargetRange[0] > r.TargetRange[1]) {
		return asset.ErrInvalid
	}
	return nil
}
func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }
func one(v string, all ...string) bool {
	for _, a := range all {
		if a == v {
			return true
		}
	}
	return false
}

// SourceRef 只允许已存事实引用；在线窗口为 [From,Until)，比较窗口长度而非绝对日期。
type SourceRef struct {
	Kind                    string    `json:"kind"`
	Runs                    []string  `json:"runs,omitempty"`
	Experiment              string    `json:"experiment,omitempty"`
	Rule                    asset.Ref `json:"rule,omitempty"`
	From, Until             *time.Time
	Subject, SubjectVersion string
}

func (r *SourceRef) Validate() error {
	if r.Kind != "ONLINE" && (r.From != nil || r.Until != nil || r.Subject != "" || r.SubjectVersion != "" || r.Rule != (asset.Ref{})) {
		return asset.ErrInvalid
	}
	switch r.Kind {
	case "RUN_SET":
		if len(r.Runs) == 0 || len(r.Runs) > 100 || r.Experiment != "" {
			return asset.ErrInvalid
		}
		sort.Strings(r.Runs)
		for i, id := range r.Runs {
			if !asset.ValidID(id) || i > 0 && id == r.Runs[i-1] {
				return asset.ErrInvalid
			}
		}
	case "EXPERIMENT":
		if !asset.ValidID(r.Experiment) || len(r.Runs) > 0 {
			return asset.ErrInvalid
		}
	case "ONLINE":
		if len(r.Runs) > 0 || r.Experiment != "" {
			return asset.ErrInvalid
		}
		if r.Rule.Validate() != nil || r.From == nil || r.Until == nil || !r.Until.After(*r.From) || r.Until.Sub(*r.From) > 90*24*time.Hour || !asset.Text(r.Subject) || !asset.Text(r.SubjectVersion) {
			return asset.ErrInvalid
		}
	default:
		return asset.ErrInvalid
	}
	return nil
}

type Command struct {
	ID                  string `json:"gate_id"`
	Baseline, Candidate SourceRef
	Policy              asset.Ref
}

func (c *Command) Validate() error {
	if !asset.ValidID(c.ID) || c.Policy.Validate() != nil || c.Baseline.Validate() != nil || c.Candidate.Validate() != nil {
		return asset.ErrInvalid
	}
	return nil
}

// IntentID 是 CLI convenience；命令的持久身份不包含未来结果查询。
func IntentID(project, actor string, c Command) string {
	c.ID = ""
	j, _ := asset.Freeze(struct {
		Project, Actor string
		Command        Command
	}{project, actor, c})
	b := sha256.Sum256(j.Bytes())
	b[6] = (b[6] & 15) | 80
	b[8] = (b[8] & 63) | 128
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

type MetricFact struct {
	Metric                      ev.MetricInput
	Evaluator                   ev.EvaluatorSpec
	ResultID, Verdict, Category string
	Applicability               asset.ApplicabilityState
	Value                       *float64
	Provenance                  asset.JSON
	Calls                       []ev.ProviderCall
	SelectedCallIDs             []string
	EvidenceSchemas             []string
	EvidenceDigest              string
}
type Unit struct {
	Key, CaseID, CaseVersion, CaseDigest, Criticality, RunID, AttemptID string
	Repeat                                                              int
	Outcome                                                             ev.OutcomeKind
	Task, TaskAvailability                                              string
	Metrics                                                             []MetricFact
}
type Source struct {
	Ref                                         SourceRef
	Digest, Owner, SamplingUnit, Interpretation string
	Dimensions                                  map[string]string
	Units                                       []Unit
	EligiblePopulation, SampleCount             int
	ExpectedSlots, CompletedSlots               int
	SamplingCoverage                            *float64
	Reasons                                     []string
}
type Snapshot struct {
	ProjectID, Actor, Source string
	Command                  Command
	Policy                   Policy
	PolicyDigest             string
	Baseline, Candidate      Source
	CapturedAt               time.Time
}
type Coverage struct {
	Eligible, Decidable, Success, Failure, Inconclusive, Missing, Unknown, Error, Unsupported, NotApplicable, ExpectedSlots, CompletedSlots int
	Decision, Evaluation                                                                                                                    *float64
	TaskRate                                                                                                                                *float64
	SamplingUnit                                                                                                                            string
}
type Issue struct {
	Code, Scope string
	Decision    Decision
}
type MetricComparison struct {
	BaselineApplicability, CandidateApplicability                                  asset.ApplicabilityState
	BaselineVerdict, CandidateVerdict                                              string
	BaselineCount, CandidateCount, BaselineEligible, CandidateEligible             int
	Metric                                                                         asset.Ref
	Aggregation                                                                    string
	Baseline, Candidate, Delta, RelativeDelta, BaselineVariance, CandidateVariance *float64
	BaselineCoverage, CandidateCoverage                                            *float64
	Classification                                                                 Classification
	Reasons                                                                        []string
}
type CaseComparison struct {
	CaseID, CaseVersion, Key, Criticality                                string
	BaselineOutcome, CandidateOutcome                                    ev.OutcomeKind
	BaselineTask, CandidateTask, TaskTransition                          string
	BaselineRunID, CandidateRunID, BaselineAttemptID, CandidateAttemptID string
	Classification                                                       Classification
	Metrics                                                              []MetricComparison
	Reasons                                                              []string
}
type Summary struct{ Regressions, Improvements, Unchanged, Insufficient, Incomparable int }
type Receipt struct {
	Stage13             *Stage13Gate  `json:"stage13,omitempty"`
	GateID              string        `json:"gate_id"`
	ProjectID           string        `json:"project_id"`
	Version             string        `json:"comparison_version"`
	Decision            Decision      `json:"decision"`
	Reasons             []string      `json:"reason_codes"`
	Compatibility       Compatibility `json:"comparability"`
	Differences         []Difference  `json:"differing_dimensions"`
	Baseline, Candidate Source
	PolicyRef           asset.Ref                                                                       `json:"policy_ref"`
	PolicyDigest        string                                                                          `json:"policy_digest"`
	SnapshotDigest      string                                                                          `json:"snapshot_digest"`
	CriticalRegressions []string                                                                        `json:"critical_regressions"`
	Coverage            struct{ Baseline, Candidate, BaselineEvaluation, CandidateEvaluation Coverage } `json:"coverage"`
	TaskSuccess         MetricComparison                                                                `json:"task_success"`
	Cases               []CaseComparison                                                                `json:"case_comparisons"`
	Metrics             []MetricComparison                                                              `json:"metric_decisions"`
	Issues              []Issue                                                                         `json:"issues"`
	Exceptions          []Exception                                                                     `json:"accepted_exceptions"`
	Summary             Summary                                                                         `json:"summary"`
	CreatedAt           time.Time                                                                       `json:"created_at"`
	Actor, Source       string
}
