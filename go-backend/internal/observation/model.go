package observation

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"math"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
)

const Contract = "stage12.online-evaluation.v1"
const Schema = "c12a00500001"

type Kind string

const (
	TraceKind Kind = "TRACE"
	SpanKind  Kind = "SPAN"
	Plan      Kind = "PLAN"
	Step      Kind = "STEP"
	ToolCall  Kind = "TOOL_CALL"
	Retrieval Kind = "RETRIEVAL"
)

type Ref struct {
	ProjectID, ID, Digest, Schema string
	Kind                          Kind
}
type Evidence struct {
	Kind, Schema, Availability, Digest, Source string
	Body                                       asset.JSON
	Retention                                  string
}
type Observation struct {
	Ref                                                             Ref
	TraceID, RuntimeID, Source, Principal, Trust, Operation, Status string
	SpanID, Parent, StepID                                          *string
	Started, Completed, Created                                     time.Time
	Envelope                                                        asset.JSON
	Canonical                                                       []byte
	BodyAvailability, Retention                                     string
	Metadata                                                        asset.JSON
	Evidence                                                        []Evidence
	ParentIntegrity                                                 string
}

// Scope 的 trust 来自受控 adapter 的认证结果，不从上传 JSON 获得。
type Scope struct {
	asset.Scope
	Epoch                                            int64
	Ingest, AuthenticatedSource, Evaluate, Reconcile bool
}
type Filter struct {
	Subject, AgentVersion, Environment, RunMode, Operation string
	Error                                                  *bool
	Exact                                                  map[string]string
}
type Sampling struct {
	Policy, Algorithm string
	Version           int
	BasisPoints       int
}
type Selection struct {
	Eligible, Sampled bool
	Algorithm         string
	Version           int
	KeyDigest         string
	BasisPoints       int
	Unit              string
}
type Budget struct {
	MaxSampled, MaxWorks, MaxProviderCalls int
	MaxEstimatedTokens, MaxReportedTokens  int64
}
type Rate struct {
	Mode            string
	MaxCalls        int
	IntervalSeconds int
}
type Rule struct {
	Enabled             bool
	Scope               Kind
	Trust, Source       string
	Filter              Filter
	Sampling            Sampling
	Budget              Budget
	Rate                Rate
	AllowBackfill       bool
	BackfillMax         int
	LiveFrom, LiveUntil *time.Time
	Bindings            []Binding
}
type Binding struct {
	Evaluator ev.EvaluatorSpec
	Metric    ev.MetricInput
}
type RuleVersion struct {
	ProjectID   string
	Ref         asset.Ref
	Body        Rule
	Digest      string
	Canonical   []byte
	PublishedBy string
	PublishedAt time.Time
}

func (r Rule) Validate() error {
	if !has(string(r.Scope), "TRACE", "SPAN", "PLAN", "STEP", "TOOL_CALL", "RETRIEVAL") || !has(r.Trust, "STRICT_AUTHENTICATED", "NORMAL_OBSERVATION") || !asset.Text(r.Source) || r.Sampling.Algorithm != "sha256-trace-basis-points" || r.Sampling.Version != 1 || !has(r.Sampling.Policy, "ALL", "HASH_PERCENTAGE") || r.Sampling.BasisPoints < 0 || r.Sampling.BasisPoints > 10000 || (r.Sampling.Policy == "ALL" && r.Sampling.BasisPoints != 10000) || r.Budget.MaxSampled < 0 || r.Budget.MaxWorks < 0 || r.Budget.MaxProviderCalls < 0 || r.Budget.MaxEstimatedTokens < 0 || r.Budget.MaxReportedTokens < 0 || r.BackfillMax < 1 || r.BackfillMax > 1000 || len(r.Bindings) < 1 || len(r.Bindings) > 32 || !has(r.Rate.Mode, "DURABLE_INTERVAL", "BOUNDED_CONCURRENCY") || (r.Rate.Mode == "DURABLE_INTERVAL" && (r.Rate.MaxCalls < 1 || r.Rate.IntervalSeconds < 1)) || (r.LiveFrom != nil && r.LiveUntil != nil && !r.LiveUntil.After(*r.LiveFrom)) {
		return asset.ErrInvalid
	}
	seen := map[asset.Ref]bool{}
	for _, b := range r.Bindings {
		if b.Evaluator.Identity.Ref.Validate() != nil || b.Metric.Identity.Ref.Validate() != nil || b.Evaluator.Definition.Validate() != nil || b.Metric.Definition.Validate() != nil || len(b.Evaluator.Metrics) != 1 || b.Evaluator.Metrics[0] != b.Metric.Identity.Ref || seen[b.Evaluator.Identity.Ref] {
			return asset.ErrInvalid
		}
		seen[b.Evaluator.Identity.Ref] = true
	}
	return nil
}
func Match(r Rule, o Observation) bool {
	if !r.Enabled || r.Scope != o.Ref.Kind || r.Trust != o.Trust || r.Source != o.Source || (r.LiveFrom != nil && o.Completed.Before(*r.LiveFrom)) || (r.LiveUntil != nil && !o.Completed.Before(*r.LiveUntil)) {
		return false
	}
	var metadata map[string]asset.JSON
	_ = o.Metadata.Decode(&metadata)
	for key, want := range map[string]string{"subject": r.Filter.Subject, "agent_version": r.Filter.AgentVersion, "environment": r.Filter.Environment, "run_mode": r.Filter.RunMode} {
		if want != "" {
			var got string
			if metadata[key].Decode(&got) != nil || got != want {
				return false
			}
		}
	}
	if r.Filter.Operation != "" && r.Filter.Operation != o.Operation {
		return false
	}
	if r.Filter.Error != nil && *r.Filter.Error != (o.Status != "OK") {
		return false
	}
	for key, want := range r.Filter.Exact {
		var got string
		if metadata[key].Decode(&got) != nil || got != want {
			return false
		}
	}
	return true
}
func Sample(project string, rule asset.Ref, trace string, p Sampling, eligible bool) Selection {
	key, _ := asset.Freeze([]string{project, rule.EntityID, rule.Version, trace})
	sum := sha256.Sum256(key.Bytes())
	value := binary.BigEndian.Uint64(sum[:8]) % 10000
	return Selection{eligible, eligible && (p.Policy == "ALL" || value < uint64(p.BasisPoints)), p.Algorithm, p.Version, fmt.Sprintf("%x", sum), p.BasisPoints, "trace"}
}

type Work struct {
	ID, ProjectID, ObservationID string
	Rule                         asset.Ref
	Binding                      Binding
	Observation                  Observation
	Status, Token                string
	Epoch                        int64
	Lease                        *time.Time
	Claims, Calls                int
	CallRecords                  []Call
	Created                      time.Time
}
type Result struct {
	ID, WorkID, ProjectID, ObservationID string
	Rule                                 asset.Ref
	Binding                              Binding
	EvidenceDigest                       string
	Observation                          Ref
	Sampling                             Selection
	Value                                Value
	Calls                                []Call
	Created                              time.Time
}
type Call struct {
	Provenance      ev.ProviderCall
	Draft           *Value
	EstimatedTokens int64
}
type ProviderStore interface {
	BeginOnlineCall(context.Context, Scope, string, string, Call) (ev.Reply, error)
	FinishOnlineCall(context.Context, Scope, string, string, FinishCall) (ev.Reply, error)
}
type FinishCall struct {
	CallID     string
	Provenance ev.FinishCall
	Draft      *Value
}
type Value struct {
	Verdict, Category, Reason, SourceVerdict string
	Score                                    *float64
	Applicability                            asset.ApplicabilityState
	Provenance                               asset.JSON
	SelectedCallIDs                          []string
}

func (v Value) Validate(b Binding) error {
	if !has(v.Verdict, "PASS", "FAIL", "INCONCLUSIVE", "ERROR") || !asset.ContentText(v.Reason) || !has(string(v.Applicability), "APPLICABLE", "NOT_APPLICABLE", "MISSING_EVIDENCE", "UNSUPPORTED") {
		return asset.ErrInvalid
	}
	if v.Applicability != asset.Applicable && v.Verdict != "INCONCLUSIVE" {
		return asset.ErrInvalid
	}
	if v.Score != nil {
		if v.Applicability != asset.Applicable || has(v.Verdict, "INCONCLUSIVE", "ERROR") || math.IsNaN(*v.Score) || math.IsInf(*v.Score, 0) {
			return asset.ErrInvalid
		}
		if r := b.Metric.Definition.Range; r != nil && (*v.Score < r[0] || *v.Score > r[1]) {
			return asset.ErrInvalid
		}
	}
	return nil
}

type Coverage struct {
	Eligible, Sampled, WorkCreated, Completed, NotApplicable, MissingEvidence, Unsupported, BudgetSkipped, RateLimited, EvaluatorError, Decidable int
	SamplingCoverage, EvaluationCoverage, DecisionCoverage                                                                                        *float64
	SamplingUnit, Policy                                                                                                                          string
	Algorithm                                                                                                                                     string
	EligibleUnit, EvaluationUnit                                                                                                                  string
	Rule                                                                                                                                          asset.Ref
	RuleDigest                                                                                                                                    string
	Sampling                                                                                                                                      Sampling
	Filter                                                                                                                                        Filter
	QualityInterpretation                                                                                                                         string
	AdmissionBudgetSkipped                                                                                                                        int
}

func (c *Coverage) Calculate() {
	c.SamplingCoverage = ratio(c.Sampled, c.Eligible)
	c.EvaluationCoverage = ratio(c.Completed, c.WorkCreated)
	c.DecisionCoverage = ratio(c.Decidable, c.WorkCreated)
	c.SamplingUnit = "trace"
	c.EligibleUnit = "observation"
	c.EvaluationUnit = "observation-evaluator-slot"
	c.QualityInterpretation = "SAMPLED_ONLY_NOT_POPULATION_ESTIMATE"
}
func ratio(n, d int) *float64 {
	if d == 0 {
		return nil
	}
	v := float64(n) / float64(d)
	return &v
}

type FailureCandidate struct{ ProjectID, ObservationID, ResultID, Source, Classification, ClassifierVersion string }

func Classify(v Value, metricName string) string {
	if v.Category == "SKIPPED_BUDGET" || v.Category == "SKIPPED_RATE_LIMIT" {
		return ""
	}
	if v.Applicability == asset.MissingEvidence {
		return "MISSING_EVIDENCE"
	}
	if v.Verdict == "ERROR" {
		return "EVALUATOR_ERROR"
	}
	if v.Verdict == "FAIL" {
		switch metricName {
		case "task_success.v1":
			return "TASK_NOT_COMPLETED"
		case "answer_groundedness.v1":
			return "GROUNDING_FAILURE"
		default:
			return "EVALUATOR_FAIL"
		}
	}
	return ""
}

type Backfill struct {
	CommandID   string
	Rule        asset.Ref
	From, Until time.Time
	MaxRecords  int
}
type BackfillReceipt struct {
	ID        string
	Processed int
	Completed bool
}
type Candidate struct {
	ProjectID, ID string
	Created       time.Time
}
type Cursor struct {
	Created time.Time
	ID      string
}
type ProviderInput struct {
	Goal                     string
	Criteria                 []string
	Input, Answer, Reference asset.JSON
	Contexts                 []asset.JSON
	EvidenceRefs             []string
}

func EvidenceState(o Observation, b Binding) (ProviderInput, asset.ApplicabilityState, error) {
	p := ProviderInput{Contexts: []asset.JSON{}, EvidenceRefs: []string{}}
	var fields map[string]asset.JSON
	_ = o.Metadata.Decode(&fields)
	_ = fields["goal"].Decode(&p.Goal)
	_ = fields["acceptance_criteria"].Decode(&p.Criteria)
	p.Input = fields["input"]
	p.Reference = fields["reference"]
	evidence := []asset.Evidence{{Kind: "observation", Schema: o.Ref.Schema, BodyAvailable: true}}
	if p.Goal != "" {
		evidence = append(evidence, asset.Evidence{Kind: "task_goal", Schema: "task-goal.v1", BodyAvailable: true})
	}
	if len(p.Criteria) > 0 {
		evidence = append(evidence, asset.Evidence{Kind: "acceptance_criteria", Schema: "acceptance.v1", BodyAvailable: true})
	}
	if p.Reference.String() != "null" || b.Evaluator.Definition.Rubric.String() != "null" {
		evidence = append(evidence, asset.Evidence{Kind: "reference_or_rubric", Schema: "answer-reference.v1", BodyAvailable: true})
	}
	for _, e := range o.Evidence {
		available := e.Availability == "AVAILABLE"
		if available && e.Body.Digest() != e.Digest {
			return p, asset.MissingEvidence, asset.ErrInvalid
		}
		evidence = append(evidence, asset.Evidence{Kind: e.Kind, Schema: e.Schema, BodyAvailable: available})
		if available {
			p.EvidenceRefs = append(p.EvidenceRefs, e.Source)
			switch e.Kind {
			case "actual_output":
				p.Answer = e.Body
			case "retrieved_context":
				p.Contexts = append(p.Contexts, e.Body)
			}
		}
	}
	for _, a := range []asset.Applicability{b.Evaluator.Applicability, b.Evaluator.Definition.Applicability, b.Metric.Definition.Applicability} {
		state := asset.CheckApplicability(a, b.Evaluator.Definition.Availability, "OBSERVATION", evidence)
		if state != asset.Applicable {
			return p, state, nil
		}
	}
	if b.Metric.Definition.Availability == asset.Unsupported {
		return p, asset.UnsupportedEvidence, nil
	}
	return p, asset.Applicable, nil
}
