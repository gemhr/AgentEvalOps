// Package review 记录独立人工事实；不拥有自动 Result 或执行终态。
package review

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"math"
	"slices"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
)

var ErrOwnershipLost = errors.New("OWNERSHIP_LOST")

// Scope 由受控内部边界注入，Reviewer 不来自命令正文。G8 接入认证后沿用此边界。
type Scope struct {
	ev.Scope
	ReviewerType, IdentitySource                                                    string
	Queue, Review, Adjudicate, PublishGolden, Calibrate, Feedback, ApproveException bool
}
type Reviewer struct{ ID, Type, Principal, Source string }

func (s Scope) Reviewer() Reviewer {
	return Reviewer{s.Principal, s.ReviewerType, s.Principal, s.IdentitySource}
}
func (s Scope) ValidateReviewer() error {
	if s.Scope.Validate(false) != nil || (s.ReviewerType != "HUMAN" && s.ReviewerType != "SYSTEM_IMPORT") || !asset.Text(s.IdentitySource) {
		return asset.ErrForbidden
	}
	return nil
}

type DecisionKind string

const (
	TaskSuccess     DecisionKind = "TASK_SUCCESS"
	Quality         DecisionKind = "QUALITY_VERDICT"
	Preference      DecisionKind = "PREFERENCE"
	FailureCategory DecisionKind = "FAILURE_CATEGORY"
)

var FailureLabels = []string{"TRUE_AGENT_FAILURE", "FALSE_POSITIVE", "EVALUATOR_FAILURE", "MISSING_EVIDENCE", "DATA_QUALITY_ISSUE", "NOT_ACTIONABLE"}

type Judgment struct {
	Kind         DecisionKind
	Value        string
	Reason       string
	EvidenceRefs []string
	Confidence   *float64
}

func Labels(k DecisionKind) []string {
	switch k {
	case TaskSuccess:
		return []string{"SUCCESS", "FAILURE", "INCONCLUSIVE", "NOT_APPLICABLE"}
	case Quality:
		return []string{"PASS", "FAIL", "INCONCLUSIVE", "ERROR"}
	case Preference:
		return []string{"A", "B", "TIE", "INCONCLUSIVE"}
	case FailureCategory:
		return slices.Clone(FailureLabels)
	}
	return nil
}
func (j Judgment) Validate(schema metric.Definition, evidence []ev.Binding) error {
	labels := Labels(j.Kind)
	if schema.ValueType != metric.Enum || !slices.Equal(schema.Labels, labels) || !slices.Contains(labels, j.Value) || !asset.ContentText(j.Reason) {
		return asset.ErrInvalid
	}
	if j.Confidence != nil && (math.IsNaN(*j.Confidence) || math.IsInf(*j.Confidence, 0) || *j.Confidence < 0 || *j.Confidence > 1) {
		return asset.ErrInvalid
	}
	seen := map[string]bool{}
	for _, ref := range j.EvidenceRefs {
		if seen[ref] {
			return asset.ErrInvalid
		}
		seen[ref] = true
		found := false
		for _, b := range evidence {
			found = found || b.Ref == ref
		}
		if !found {
			return asset.ErrInvalid
		}
	}
	if len(evidence) > 0 && len(j.EvidenceRefs) == 0 {
		return asset.ErrInvalid
	}
	return nil
}
func Decidable(v string) bool {
	return v != "" && v != "INCONCLUSIVE" && v != "NOT_APPLICABLE" && v != "ERROR"
}

type SourceRef struct {
	Type                                   string
	RunID, ResultID, ObservationID, GateID string
	// FailureCandidate 来源由 G5 分类函数重新核对，caller 不能编造类别。
	CandidateSource, Classification string
}

func (r SourceRef) Validate() error {
	switch r.Type {
	case "OFFLINE_RESULT", "CALIBRATION_SAMPLE":
		if !asset.ValidID(r.RunID) || !asset.ValidID(r.ResultID) || r.ObservationID != "" || r.GateID != "" || r.CandidateSource != "" || r.Classification != "" {
			return asset.ErrInvalid
		}
	case "ONLINE_RESULT":
		if !asset.ValidID(r.ResultID) || r.RunID != "" || r.ObservationID != "" || r.GateID != "" || r.CandidateSource != "" || r.Classification != "" {
			return asset.ErrInvalid
		}
	case "FAILURE_CANDIDATE":
		if !asset.ValidID(r.ObservationID) || !asset.Text(r.CandidateSource) || !asset.Text(r.Classification) || r.RunID != "" || r.GateID != "" {
			return asset.ErrInvalid
		}
		if r.ResultID != "" && !asset.ValidID(r.ResultID) {
			return asset.ErrInvalid
		}
		if (r.CandidateSource == "ONLINE_RESULT") != (r.ResultID != "") {
			return asset.ErrInvalid
		}
	case "GATE_DECISION":
		if !asset.ValidID(r.GateID) || r.RunID != "" || r.ResultID != "" || r.ObservationID != "" || r.CandidateSource != "" || r.Classification != "" {
			return asset.ErrInvalid
		}
	default:
		return asset.ErrInvalid
	}
	return nil
}

type Protocol struct{ Blind, JudgeShown, CandidateIdentityShown, BaselineIdentityShown bool }

func (p Protocol) Validate() error {
	if p.Blind == p.JudgeShown {
		return asset.ErrInvalid
	}
	return nil
}

type Policy struct {
	Ref      string
	Reviews  int
	Protocol Protocol
	Sampling Sampling
}

func (p Policy) Validate() error {
	if !asset.Text(p.Ref) || (p.Reviews != 1 && p.Reviews != 2) || p.Protocol.Validate() != nil {
		return asset.ErrInvalid
	}
	return p.Sampling.Validate()
}

type Sampling struct {
	Version, Seed string
	BasisPoints   int
	Biased        bool
}

func (p Sampling) Validate() error {
	if p.Version != "review-hash.v1" || !asset.Text(p.Seed) || p.BasisPoints < 0 || p.BasisPoints > 10000 {
		return asset.ErrInvalid
	}
	return nil
}
func (p Sampling) Selected(project string, r SourceRef) bool {
	b, _ := asset.FreezeBytes(struct {
		Project string
		Source  SourceRef
		Policy  Sampling
	}{project, r, p})
	h := sha256.Sum256(b)
	return int(binary.BigEndian.Uint64(h[:8])%10000) < p.BasisPoints
}

type Enqueue struct {
	ID                            string
	Source                        SourceRef
	Schema                        asset.Ref
	Kind                          DecisionKind
	Policy                        Policy
	Reason, Priority, Criticality string
}

func (c Enqueue) Validate() error {
	if !asset.ValidID(c.ID) || c.Source.Validate() != nil || c.Schema.Validate() != nil || len(Labels(c.Kind)) == 0 || c.Policy.Validate() != nil || !asset.ContentText(c.Reason) || !slices.Contains([]string{"CRITICAL", "HIGH", "NORMAL"}, c.Priority) || !slices.Contains([]string{"CRITICAL", "NORMAL"}, c.Criticality) {
		return asset.ErrInvalid
	}
	return nil
}

type Automatic struct {
	Source                                      SourceRef
	Decision, Category                          string
	Evaluator                                   asset.Ref
	EvaluatorDigest                             string
	Definition                                  metric.EvaluatorDefinition
	Metric                                      asset.Ref
	MetricDigest                                string
	Calls                                       []ev.ProviderCall
	EvidenceDigest                              string
	ProviderError, InvalidOutput, IdentityDrift bool
}
type Source struct {
	Ref                         SourceRef
	Digest, EvidenceDigest      string
	Case                        *asset.Ref
	CaseBody                    *catalog.CaseContent
	ObservationID               string
	Evidence                    []ev.Binding
	BodyAvailability, Retention string
	Automatic                   *Automatic
}
type Item struct {
	ID, ProjectID string
	Command       Enqueue
	Source        Source
	Schema        metric.Definition
	SchemaDigest  string
	Status        string
	CreatedAt     time.Time
}
type Slot struct {
	Number                    int
	Reviewer                  *Reviewer
	Token                     string
	Lease                     *time.Time
	Status                    string
	Claims, Expired, Released int
}
type Claim struct {
	ItemID, Token string
	Slot          int
	Lease         time.Time
}
type Submit struct {
	ID, ItemID, Token, SourceDigest, SchemaDigest string
	Slot                                          int
	Decision                                      Judgment
}
type Annotation struct {
	ID, ProjectID, ItemID                      string
	Slot                                       int
	Reviewer                                   Reviewer
	Schema                                     asset.Ref
	SchemaDigest, SourceDigest, EvidenceDigest string
	EvidencePresented                          []ev.Binding
	Protocol                                   Protocol
	Decision                                   Judgment
	Supersedes                                 *string
	CreatedAt, SubmittedAt                     time.Time
}
type Adjudication struct {
	ID, ProjectID, ItemID string
	Inputs                []string
	Reviewer              Reviewer
	Decision              Judgment
	Reason                string
	Supersedes            *string
	CreatedAt             time.Time
}
type GoldenLabel struct {
	ID, ProjectID, ItemID                              string
	Schema                                             asset.Ref
	SchemaDigest, SourceDigest, EvidenceDigest, Origin string
	AnnotationIDs                                      []string
	AdjudicationID                                     *string
	Case                                               *asset.Ref
	Decision                                           Judgment
	Protocol                                           Protocol
	CreatedAt                                          time.Time
}
type ReadModel struct {
	Item          Item
	Slots         []Slot
	Annotations   []Annotation
	Adjudications []Adjudication
	Golden        []GoldenLabel
}
type Coverage struct {
	Eligible, Assigned, Submitted, Adjudicated, Golden, Missing, Expired, Released, IndependentPairs, AgreedPairs int
	HumanLabelCoverage, ReviewerAgreement                                                                         *float64
}

func Ratio(a, b int) *float64 {
	if b == 0 {
		return nil
	}
	v := float64(a) / float64(b)
	return &v
}

// Consensus 只比较 typed decision；理由或 confidence 不代替独立一致性。
func Consensus(a, b Annotation) bool {
	return a.Reviewer.ID != b.Reviewer.ID && a.Decision.Kind == b.Decision.Kind && a.Decision.Value == b.Decision.Value
}

type Draft struct {
	ID, ProjectID, ItemID                 string
	GoldenID                              *string
	AnnotationIDs                         []string
	AdjudicationID                        *string
	Supersedes                            *string
	Case                                  asset.Ref
	Body                                  catalog.CaseContent
	Sanitization, Policy, Reason          string
	SourceDigest, SanitizedDigest         string
	Reviewer                              Reviewer
	HumanSupplement, UseGoldenGroundTruth bool
	CreatedAt                             time.Time
}

func (d Draft) Ready(item Item) error {
	if item.Status != "COMPLETED" || !slices.Contains([]string{"APPROVED", "REDACTED"}, d.Sanitization) || !asset.Text(d.Policy) || !asset.ContentText(d.Reason) || d.Body.Validate() != nil || d.Body.Input.String() == "null" || d.Case.Validate() != nil {
		return asset.ErrInvalid
	}
	if item.Source.BodyAvailability != "AVAILABLE" && !d.HumanSupplement {
		return asset.ErrInvalid
	}
	if item.Source.Retention == "PROHIBITED" && !d.HumanSupplement {
		return asset.ErrForbidden
	}
	if d.Sanitization == "REDACTED" && d.Body.BodyPolicy != catalog.Redacted {
		return asset.ErrInvalid
	}
	return nil
}

type GateException struct {
	ID, ProjectID, ItemID, GateID, Code, Scope, RequestedReason, Decision, Reason string
	Expiry                                                                        time.Time
	Approver                                                                      Reviewer
	CreatedAt                                                                     time.Time
}
