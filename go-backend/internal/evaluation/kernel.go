package evaluation

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/cigovernance"
	"context"
	"fmt"
	"math"
	"slices"
	"time"
)

const DurableContract = "stage12.durable-evaluation.v1"
const SchemaVersion = "c12a00200001"

type Code string

const (
	Applied          Code = "APPLIED"
	AlreadyApplied   Code = "ALREADY_APPLIED"
	NotClaimed       Code = "NOT_CLAIMED"
	NotReady         Code = "NOT_READY"
	NotFound         Code = "NOT_FOUND"
	OwnershipLost    Code = "OWNERSHIP_LOST"
	Conflict         Code = "CONFLICT"
	Rejected         Code = "REJECTED"
	IntegrityBlocked Code = "INTEGRITY_BLOCKED"
)

type Reply struct {
	Code                      Code
	Reason, ID, Token, Status string
	Lease                     *time.Time
}
type Scope struct {
	asset.Scope
	Epoch                                                   int64
	Execute, Evaluate, Coordinate, Repair, Create, Operator bool
}
type RunStatus string

const (
	RunPending   RunStatus = "PENDING"
	RunRunning   RunStatus = "RUNNING"
	RunCompleted RunStatus = "COMPLETED"
	RunFailed    RunStatus = "FAILED"
	RunUnknown   RunStatus = "OUTCOME_UNKNOWN"
)

func (s RunStatus) Terminal() bool { return s == RunCompleted || s == RunFailed || s == RunUnknown }

type OutcomeKind string

const (
	Success   OutcomeKind = "SUCCESS"
	Failure   OutcomeKind = "FAILURE"
	Timeout   OutcomeKind = "TIMEOUT"
	Cancelled OutcomeKind = "CANCELLED"
	Unknown   OutcomeKind = "OUTCOME_UNKNOWN"
)

type RetryPolicy struct {
	Version          string        `json:"version"`
	MaxAttempts      int           `json:"max_attempts"`
	Allowed          []OutcomeKind `json:"allowed_outcomes"`
	RetrySafeSource  string        `json:"retry_safe_source"`
	AllowUnknownFork bool          `json:"allow_unknown_fork"`
}

func (p RetryPolicy) Validate() error {
	if p.MaxAttempts < 1 {
		return asset.ErrInvalid
	}
	switch p.Version {
	case "NO_RETRY.v1":
		if p.MaxAttempts != 1 || len(p.Allowed) > 0 || p.AllowUnknownFork {
			return asset.ErrInvalid
		}
	case "EXPLICIT_RETRY.v1":
	case "SAFE_AUTO_RETRY.v1":
		if !asset.Text(p.RetrySafeSource) {
			return asset.ErrInvalid
		}
	default:
		return asset.ErrInvalid
	}
	for _, k := range p.Allowed {
		if k != Failure && k != Timeout && k != Cancelled {
			return asset.ErrInvalid
		}
	}
	return nil
}
func (p RetryPolicy) Allows(a Attempt, automatic, authorized, unknownAck bool) bool {
	if a.Status != "TERMINAL" || a.Outcome == Success || a.Number >= p.MaxAttempts {
		return false
	}
	if a.Outcome == Unknown {
		return !automatic && authorized && unknownAck && p.AllowUnknownFork
	}
	if automatic && p.Version != "SAFE_AUTO_RETRY.v1" {
		return false
	}
	if !automatic && !authorized {
		return false
	}
	for _, k := range p.Allowed {
		if k == a.Outcome {
			return true
		}
	}
	return false
}

type Target struct {
	ID, Kind, Version    string
	Config, Capabilities asset.JSON
	TimeoutMilliseconds  int64
}
type Capability struct {
	Evaluator                           asset.Ref
	ImplementationRef, DefinitionDigest string
}

// 能力由受控 composition boundary 提供；目录存在不代表该 binary 能执行。
type EvaluatorExecutionCapability struct{ Bindings []Capability }

func (c EvaluatorExecutionCapability) Supports(e EvaluatorSpec) bool {
	if e.Definition.Availability == asset.Unsupported {
		return false
	}
	for _, b := range c.Bindings {
		if b.Evaluator == e.Identity.Ref && b.ImplementationRef == e.Definition.ImplementationRef && b.DefinitionDigest == e.Identity.ContentDigest {
			return true
		}
	}
	return false
}

type RunSnapshot struct {
	Intent                      string `json:"Intent,omitempty"`
	Contract                    string
	Input                       SnapshotInput
	InputBytes                  []byte
	InputDigest, InputAlgorithm string
	Target                      Target
	Subject                     asset.JSON
	Retry                       RetryPolicy
	Capabilities                []Capability
}
type CreateRun struct {
	Intent    string `json:"Intent,omitempty"`
	CommandID string
	Snapshot  Snapshot
	Target    Target
	Subject   asset.JSON
	Retry     RetryPolicy
}

func FreezeRun(c CreateRun, project string, caps EvaluatorExecutionCapability) (RunSnapshot, error) {
	in := c.Snapshot.Input()
	governed := false
	for _, item := range in.Manifest {
		p, err := cigovernance.ReadPolicy(item.Case.Metadata)
		if err != nil {
			return RunSnapshot{}, err
		}
		if p != nil {
			governed = true
			if cigovernance.CheckIntent(p.Role, c.Intent) != nil || !cigovernance.HardGolden(p.State) {
				return RunSnapshot{}, asset.ErrForbidden
			}
		}
	}
	if c.Intent != "" && !governed {
		return RunSnapshot{}, asset.ErrForbidden
	}
	if governed {
		if in.Dataset == nil {
			return RunSnapshot{}, asset.ErrForbidden
		}
		var content asset.Content[catalog.DatasetContent]
		if in.Dataset.CanonicalContent.Decode(&content) != nil {
			return RunSnapshot{}, asset.ErrInvalid
		}
		p, err := cigovernance.ReadPolicy(content.Body.Metadata)
		if err != nil || p == nil || cigovernance.CheckIntent(p.Role, c.Intent) != nil {
			return RunSnapshot{}, asset.ErrForbidden
		}
		actual := []asset.Ref{}
		for _, item := range in.Manifest {
			actual = append(actual, item.Identity.Ref)
		}
		if !slices.Equal(actual, content.Body.Cases) {
			return RunSnapshot{}, asset.ErrForbidden
		}
	}
	if !asset.ValidID(c.CommandID) || in.Contract != SnapshotContract || in.ProjectID != project || in.Origin != PublishedCatalog || len(in.Manifest) == 0 || len(in.Evaluators) == 0 || !asset.Text(c.Target.ID) || !asset.Text(c.Target.Kind) || !asset.Text(c.Target.Version) || c.Target.TimeoutMilliseconds <= 0 || c.Subject.String() == "null" || c.Retry.Validate() != nil {
		return RunSnapshot{}, asset.ErrInvalid
	}
	seen := map[string]bool{}
	for _, item := range in.Manifest {
		if item.Identity.ProjectID != project || item.Identity.Ref.Validate() != nil || seen[item.Identity.Ref.EntityID] {
			return RunSnapshot{}, asset.ErrInvalid
		}
		seen[item.Identity.Ref.EntityID] = true
	}
	ev := map[asset.Ref]bool{}
	for _, e := range in.Evaluators {
		if e.Identity.ProjectID != project || e.Identity.Ref.Validate() != nil || ev[e.Identity.Ref] || e.Definition.Validate() != nil {
			return RunSnapshot{}, asset.ErrInvalid
		}
		if !caps.Supports(e) {
			return RunSnapshot{}, asset.ErrUnsupported
		}
		ev[e.Identity.Ref] = true
	}
	retry := c.Retry
	retry.Allowed = append([]OutcomeKind(nil), c.Retry.Allowed...)
	return RunSnapshot{Intent: c.Intent, Contract: DurableContract, Input: in, InputBytes: c.Snapshot.Bytes(), InputDigest: c.Snapshot.Digest(), InputAlgorithm: c.Snapshot.Algorithm(), Target: c.Target, Subject: c.Subject, Retry: retry, Capabilities: append([]Capability(nil), caps.Bindings...)}, nil
}

type Binding struct {
	ProjectID, RunID, AttemptID, RequestID, Ref, Digest, Schema, Availability string
	Body                                                                      asset.JSON
}

func (b Binding) Matches(p, r, a, q string) bool {
	return b.ProjectID == p && b.RunID == r && b.AttemptID == a && b.RequestID == q && asset.Text(b.Ref) && asset.Text(b.Schema) && (b.Availability == "AVAILABLE" || b.Availability == "UNAVAILABLE") && (b.Availability != "AVAILABLE" || b.Body.String() != "null")
}

type Outcome struct {
	Kind                                                                                               OutcomeKind
	RequestID, RemoteID, Protocol, Source, DispatchCertainty, TerminalCertainty, ErrorCategory, Reason string
	Artifact                                                                                           *Binding
	Evidence                                                                                           []Binding
	RemoteStartedAt, RemoteFinishedAt                                                                  *time.Time
	Cleanup                                                                                            asset.JSON
}

func (o Outcome) Validate(p, r, a, q string) error {
	if o.RequestID != q || !asset.Text(o.Source) {
		return asset.ErrInvalid
	}
	switch o.DispatchCertainty {
	case "NOT_DISPATCHED", "MAY_HAVE_DISPATCHED", "REMOTE_ACCEPTED":
	default:
		return asset.ErrInvalid
	}
	switch o.TerminalCertainty {
	case "REMOTE_CONFIRMED", "NOT_DISPATCHED", "UNCONFIRMED":
	default:
		return asset.ErrInvalid
	}
	switch o.Kind {
	case Success:
		if o.TerminalCertainty != "REMOTE_CONFIRMED" || !asset.Text(o.RemoteID) || !asset.Text(o.Protocol) || o.Artifact == nil || !o.Artifact.Matches(p, r, a, q) || o.ErrorCategory != "" {
			return asset.ErrInvalid
		}
	case Failure:
		if o.TerminalCertainty == "UNCONFIRMED" {
			return asset.ErrInvalid
		}
	case Timeout, Cancelled:
		if o.TerminalCertainty != "REMOTE_CONFIRMED" {
			return asset.ErrInvalid
		}
	case Unknown:
		if o.TerminalCertainty != "UNCONFIRMED" {
			return asset.ErrInvalid
		}
	default:
		return asset.ErrInvalid
	}
	if o.Kind != Success && (o.Artifact != nil || !asset.Text(o.ErrorCategory) || !asset.ContentText(o.Reason)) {
		return asset.ErrInvalid
	}
	for _, b := range o.Evidence {
		if !b.Matches(p, r, a, q) {
			return asset.ErrInvalid
		}
	}
	return nil
}

type Receipt struct {
	CommandID, Digest, Token string
	Epoch                    int64
}

func Intent(v any) (string, error) { j, e := asset.Freeze(v); return j.Digest(), e }

type Run struct {
	ID         string      `json:"id"`
	ProjectID  string      `json:"project_id"`
	Status     RunStatus   `json:"status"`
	Snapshot   RunSnapshot `json:"kernel_snapshot"`
	CreationID string      `json:"creation_command_id"`
	Intent     string      `json:"creation_intent_digest"`
	SourceID   *string     `json:"source_run_id"`
}
type Request struct {
	ID, IdempotencyKey string
	Case               CaseInput
	Target             Target
	RunID, AttemptID   string
}
type Attempt struct {
	ID          string          `json:"id"`
	ProjectID   string          `json:"project_id"`
	RunID       string          `json:"run_id"`
	CaseID      string          `json:"case_id"`
	CaseVersion string          `json:"case_version"`
	Number      int             `json:"attempt_no"`
	Parent      *string         `json:"retry_of_attempt_id"`
	RequestID   string          `json:"execution_request_id"`
	Key         string          `json:"idempotency_key"`
	Request     Request         `json:"request_snapshot"`
	Status      string          `json:"status"`
	Token       *string         `json:"claim_token"`
	Epoch       *int64          `json:"writer_epoch"`
	Lease       *time.Time      `json:"lease_expires_at"`
	Outcome     OutcomeKind     `json:"execution_outcome_kind"`
	Metadata    AttemptMetadata `json:"outcome_metadata"`
	RetryIntent *string         `json:"retry_intent_digest"`
}
type AttemptMetadata struct {
	Receipt          Receipt
	Observation      Outcome
	ObservationBytes []byte
}
type Owned struct{ RunID, AttemptID, WorkID, Token string }
type FinalizeOutcome struct {
	Owned
	CommandID string
	Outcome   Outcome
}
type Retry struct {
	RunID, AttemptID, CommandID, Reason string
	Authorized, UnknownAck              bool
}
type Rerun struct {
	SourceRunID, CommandID, Reason, Trigger string
	UnknownAck                              bool
}
type Work struct {
	ContractKind     string         `json:"contract_kind"`
	ID               string         `json:"id"`
	ProjectID        string         `json:"project_id"`
	RunID            string         `json:"run_id"`
	AttemptID        string         `json:"attempt_id"`
	CaseID           string         `json:"case_id"`
	CaseVersion      string         `json:"case_version"`
	EvaluatorID      string         `json:"evaluator_id"`
	EvaluatorVersion string         `json:"evaluator_version"`
	SpecDigest       string         `json:"spec_digest"`
	Status           string         `json:"status"`
	Token            *string        `json:"claim_token"`
	Epoch            *int64         `json:"writer_epoch"`
	Lease            *time.Time     `json:"lease_expires_at"`
	Claims           int            `json:"claim_count"`
	Evaluations      int            `json:"evaluation_attempt_count"`
	CallCount        int            `json:"provider_call_count"`
	Calls            []ProviderCall `json:"bounded_call_provenance"`
	ResultID         *string        `json:"result_id"`
	Metadata         WorkMetadata   `json:"metadata"`
	Next             time.Time      `json:"next_available_at"`
}
type WorkMetadata struct {
	Spec               EvaluatorSpec
	CompletionMode     string
	EvaluationReceipts []Receipt
	ReleaseReceipts    []Receipt
	RecoveryPolicyRef  string
	DeadlineAt         *time.Time
}
type ProviderCall struct {
	ID                                                                                           string
	Number, EvaluationNumber                                                                     int
	Suboperation, RequestedProvider, RequestedModel, ActualProvider, ActualModel, ActualRevision string
	PromptRef                                                                                    *asset.Ref
	PromptDigest, Schema, InputDigest, EvidenceDigest                                            string
	Author                                                                                       Receipt
	Classification                                                                               string
	StartedAt                                                                                    time.Time
	FinishedAt                                                                                   *time.Time
	Usage, Cost, Response                                                                        asset.JSON
	UsageAvailability, CostAvailability, ResponseDigest, ResponseRef, RemoteRequestID            string
	FinishDigest                                                                                 string
	Draft                                                                                        *ResultValue
	ResponseBytes                                                                                []byte
	DraftBytes                                                                                   []byte
}
type BeginCall struct {
	Owned
	Call ProviderCall
}
type FinishCall struct {
	Owned
	CallID                                                                                                                                         string
	Classification, ActualProvider, ActualModel, ActualRevision, ResponseDigest, ResponseRef, RemoteRequestID, UsageAvailability, CostAvailability string
	Usage, Cost, Response                                                                                                                          asset.JSON
	Draft                                                                                                                                          *ResultValue
}
type ResultValue struct {
	Verdict, Reason, SourceVerdict, SourceError string
	Score                                       *float64
	SpecDigest, InputDigest                     string
	Artifact                                    Binding
	Evidence                                    []Binding
	SelectedCallIDs                             []string
	Provenance                                  asset.JSON
}

func (v ResultValue) Validate(spec EvaluatorSpec, input string, artifact Binding) error {
	switch v.Verdict {
	case "PASS", "FAIL", "INCONCLUSIVE", "ERROR":
	default:
		return asset.ErrInvalid
	}
	if !asset.ContentText(v.Reason) || !asset.Text(v.SourceVerdict) || v.SpecDigest != spec.Identity.ContentDigest || v.InputDigest != input {
		return asset.ErrInvalid
	}
	x, _ := Intent(v.Artifact)
	y, _ := Intent(artifact)
	if x != y {
		return asset.ErrInvalid
	}
	if v.Score != nil {
		if math.IsNaN(*v.Score) || math.IsInf(*v.Score, 0) {
			return asset.ErrInvalid
		}

	}
	return nil
}

type FinalizeResult struct {
	Owned
	CommandID, ResultID string
	Value               ResultValue
}
type EvaluationResult struct {
	ID               string      `json:"id"`
	WorkID           *string     `json:"work_id"`
	Value            ResultValue `json:"kernel_value"`
	Receipt          Receipt     `json:"kernel_receipt"`
	ProjectID        string      `json:"project_id"`
	RunID            string      `json:"run_id"`
	AttemptID        string      `json:"attempt_id"`
	CaseID           string      `json:"case_id"`
	CaseVersion      string      `json:"case_version"`
	EvaluatorID      string      `json:"evaluator_id"`
	EvaluatorVersion string      `json:"evaluator_version"`
	Metadata         asset.JSON  `json:"metadata"`
}

// Persistence 是后续 evaluation application 的消费端口，不拥有运行循环或远程调用。
type Persistence interface {
	CreateRun(context.Context, Scope, CreateRun) (Reply, error)
	ClaimExecutionAttempt(context.Context, Scope, Owned, string, time.Duration) (Reply, error)
	StartExecutionAttempt(context.Context, Scope, Owned) (Reply, error)
	RenewExecutionLease(context.Context, Scope, Owned, time.Duration) (Reply, error)
	FinalizeExecutionOutcome(context.Context, Scope, FinalizeOutcome) (Reply, error)
	ExpireExecutionAttempt(context.Context, Scope, Owned) (Reply, error)
	CreateExecutionRetry(context.Context, Scope, Retry) (Reply, error)
	CreateRunRerun(context.Context, Scope, Rerun) (Reply, error)
	CoordinateRun(context.Context, Scope, string) (Reply, error)
	ClaimEvaluatorWork(context.Context, Scope, Owned, string, time.Duration) (Reply, error)
	BeginEvaluatorAttempt(context.Context, Scope, Owned, string, int) (Reply, error)
	RenewEvaluatorLease(context.Context, Scope, Owned, time.Duration) (Reply, error)
	ReleaseEvaluatorWorkRetry(context.Context, Scope, Owned, string, string) (Reply, error)
	ExpireEvaluatorWork(context.Context, Scope, Owned) (Reply, error)
	BeginProviderCall(context.Context, Scope, BeginCall) (Reply, error)
	FinishProviderCall(context.Context, Scope, FinishCall) (Reply, error)
	FinalizeEvaluationResult(context.Context, Scope, FinalizeResult) (Reply, error)
}

func Latest(attempts []Attempt) (map[string]Attempt, error) {
	out := map[string]Attempt{}
	for _, a := range attempts {
		key := a.CaseID
		old, ok := out[key]
		if ok && old.CaseVersion != a.CaseVersion {
			return nil, fmt.Errorf("case versions conflict")
		}
		if !ok || a.Number > old.Number {
			out[key] = a
		} else if a.Number == old.Number {
			return nil, fmt.Errorf("duplicate attempt number")
		}
	}
	return out, nil
}
func DecideRun(s RunSnapshot, attempts []Attempt, works []Work) Reply {
	latest, err := Latest(attempts)
	if err != nil || len(s.Input.Manifest) == 0 || len(s.Input.Evaluators) == 0 || len(latest) != len(s.Input.Manifest) {
		return Reply{Code: IntegrityBlocked}
	}
	unknown, failed := false, false
	for _, c := range s.Input.Manifest {
		a, ok := latest[c.Identity.Ref.EntityID]
		if !ok || a.CaseVersion != c.Identity.Ref.Version {
			return Reply{Code: IntegrityBlocked}
		}
		if a.Status != "TERMINAL" {
			return Reply{Code: NotReady}
		}
		if a.Outcome == Success {
			for _, e := range s.Input.Evaluators {
				count := 0
				for _, w := range works {
					if w.AttemptID == a.ID && w.EvaluatorID == e.Identity.Ref.EntityID && w.EvaluatorVersion == e.Identity.Ref.Version {
						if w.CaseID != a.CaseID || w.CaseVersion != a.CaseVersion || w.SpecDigest != e.Identity.ContentDigest {
							return Reply{Code: IntegrityBlocked, Reason: "WORK_MANIFEST_BINDING"}
						}
						count++
						if w.Status == "COMPLETED" && w.ResultID == nil {
							return Reply{Code: IntegrityBlocked}
						}
						if w.Status != "COMPLETED" {
							return Reply{Code: NotReady}
						}
					}
				}
				if count != 1 {
					return Reply{Code: IntegrityBlocked}
				}
			}
		} else if a.Outcome == Unknown {
			unknown = true
		} else if a.Outcome == Failure || a.Outcome == Timeout || a.Outcome == Cancelled {
			failed = true
		} else {
			return Reply{Code: IntegrityBlocked}
		}
	}
	status := RunCompleted
	if failed {
		status = RunFailed
	}
	if unknown {
		status = RunUnknown
	}
	return Reply{Code: Applied, Status: string(status)}
}

type RunState struct {
	Run      Run
	Attempts []Attempt
	Works    []Work
	Results  []EvaluationResult
}
type Reader interface {
	ReadRunState(context.Context, asset.Scope, string) (RunState, error)
}
