package agentquality

import (
	"context"
	"encoding/json"
	"reflect"
	"regexp"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
)

const LoopEvidenceSchema = "loop-evidence.v1"
const LoopImplementation = "loop-integrity.v1"
const LoopInputContract = "loop-integrity-input.v1"
const LoopOutputSchema = "loop-integrity-result.v1"
const LoopNormalization = "loop-integrity-null-preserving.v1"
const MaxLoopEvidenceBytes = 256 * 1024

type LoopCorrelation struct {
	EvaluationRunID string `json:"evaluation_run_id"`
	AttemptID       string `json:"attempt_id"`
	RequestID       string `json:"request_id"`
}
type LoopAction struct {
	ActionID              string  `json:"action_id"`
	InvocationID          *string `json:"invocation_id"`
	IdempotencyDigest     *string `json:"idempotency_key_digest"`
	ToolIdentity          *string `json:"tool_identity"`
	ArgumentsDigest       *string `json:"arguments_digest"`
	SideEffecting         bool    `json:"side_effecting"`
	ToolAttemptRef        *string `json:"tool_attempt_ref"`
	ResultRef             *string `json:"result_ref"`
	ResultDigest          *string `json:"result_digest"`
	ObservationSource     *string `json:"observation_source"`
	ObservationDigest     *string `json:"observation_digest"`
	ObservationCursor     *int    `json:"observation_cursor"`
	SideEffectProof       string  `json:"side_effect_proof"`
	ProviderReceiptDigest *string `json:"provider_receipt_digest"`
}
type LoopIteration struct {
	Iteration          int         `json:"iteration"`
	DecisionID         *string     `json:"decision_id"`
	Kind               *string     `json:"kind"`
	ModelAttemptRef    *string     `json:"model_attempt_ref"`
	AcceptedGeneration *int64      `json:"accepted_generation"`
	ResumeGeneration   *int64      `json:"resume_generation"`
	Action             *LoopAction `json:"action"`
	Aborted            bool        `json:"aborted"`
}
type LoopBudget struct {
	ModelCalls       int               `json:"model_calls"`
	ToolCalls        int               `json:"tool_calls"`
	Iterations       int               `json:"iterations"`
	MaxIterations    int               `json:"max_iterations"`
	MaxModelCalls    int               `json:"max_model_calls"`
	MaxToolCalls     int               `json:"max_tool_calls"`
	StoppedDimension *string           `json:"stopped_dimension"`
	StopRequested    *int64            `json:"stop_requested"`
	StopRemaining    *int64            `json:"stop_remaining"`
	ConsumedUsage    map[string]*int64 `json:"consumed_usage"`
	MaxUsage         map[string]*int64 `json:"max_usage"`
	CostBasis        string            `json:"cost_basis"`
}
type LoopStop struct {
	Code                string `json:"code"`
	NoProgressCount     int    `json:"no_progress_count"`
	NoProgressThreshold int    `json:"no_progress_threshold"`
}
type LoopFinal struct {
	DecisionID    string `json:"decision_id"`
	DeliveryID    string `json:"delivery_id"`
	Cursor        int64  `json:"cursor"`
	ContentDigest string `json:"content_digest"`
}
type LoopRoot struct {
	Status           string `json:"status"`
	StopReason       string `json:"stop_reason"`
	TerminalSequence *int64 `json:"terminal_sequence"`
}
type LoopEvidence struct {
	Schema         string          `json:"schema_version"`
	Producer       string          `json:"producer_version"`
	Policy         string          `json:"policy_version"`
	Algorithm      string          `json:"algorithm_version"`
	ID             string          `json:"evidence_id"`
	FrameID        string          `json:"frame_id"`
	RunID          string          `json:"run_id"`
	StepID         string          `json:"step_id"`
	GoalDigest     string          `json:"goal_digest"`
	ConfigDigest   string          `json:"config_digest"`
	Correlation    LoopCorrelation `json:"correlation"`
	IterationCount int             `json:"iteration_count"`
	Iterations     []LoopIteration `json:"iterations"`
	Budget         LoopBudget      `json:"budget"`
	Stop           LoopStop        `json:"stop"`
	Final          *LoopFinal      `json:"final"`
	Root           LoopRoot        `json:"root"`
	Coverage       string          `json:"coverage"`
	Sealed         bool            `json:"sealed"`
	Gaps           []string        `json:"gaps"`
}

// StrictLoopShape 要求全部字段显式存在，包括 nullable 字段，拒绝未知字段。
func StrictLoopShape(raw []byte, dst any) error {
	j, err := asset.ParseJSON(raw)
	if err != nil || j.Decode(dst) != nil {
		return asset.ErrInvalid
	}
	var value any
	if json.Unmarshal(raw, &value) != nil {
		return asset.ErrInvalid
	}
	return loopShape(value, reflect.TypeOf(dst).Elem())
}
func loopShape(value any, typ reflect.Type) error {
	if typ.Kind() == reflect.Pointer {
		if value == nil {
			return nil
		}
		return loopShape(value, typ.Elem())
	}
	if value == nil {
		return asset.ErrInvalid
	}
	switch typ.Kind() {
	case reflect.Struct:
		m, ok := value.(map[string]any)
		if !ok || len(m) != typ.NumField() {
			return asset.ErrInvalid
		}
		for i := 0; i < typ.NumField(); i++ {
			f := typ.Field(i)
			v, ok := m[f.Tag.Get("json")]
			if !ok || loopShape(v, f.Type) != nil {
				return asset.ErrInvalid
			}
		}
	case reflect.Slice:
		a, ok := value.([]any)
		if !ok {
			return asset.ErrInvalid
		}
		for _, v := range a {
			if loopShape(v, typ.Elem()) != nil {
				return asset.ErrInvalid
			}
		}
	case reflect.Map:
		m, ok := value.(map[string]any)
		if !ok {
			return asset.ErrInvalid
		}
		for _, v := range m {
			if loopShape(v, typ.Elem()) != nil {
				return asset.ErrInvalid
			}
		}
	}
	return nil
}

var loopDigest = regexp.MustCompile(`^[0-9a-f]{64}$`)
var loopSafeIdentity = regexp.MustCompile(`^[A-Za-z0-9._:/-]{1,512}$`)
var loopBudgetDimensions = []string{"step_starts", "model_calls", "remote_model_calls", "tool_calls", "input_tokens", "output_tokens", "total_tokens", "cost_units", "retries", "retrieval_calls", "embedding_calls", "vector_queries", "keyword_queries", "bm25_queries", "rrf_fusions", "document_reads", "context_chars"}

func loopBudgetUsageValid(b LoopBudget) bool {
	if len(b.ConsumedUsage) != len(loopBudgetDimensions) || len(b.MaxUsage) != len(loopBudgetDimensions) || !oneOf(b.CostBasis, "PINNED_BUDGET_UNITS", "UNKNOWN") {
		return false
	}
	for _, key := range loopBudgetDimensions {
		used, ok := b.ConsumedUsage[key]
		maximum, maxOK := b.MaxUsage[key]
		if !ok || !maxOK || (used == nil && !(key == "cost_units" && b.CostBasis == "UNKNOWN")) || (used != nil && *used < 0) || (maximum != nil && *maximum < 0) {
			return false
		}
	}
	return b.CostBasis != "UNKNOWN" || b.ConsumedUsage["cost_units"] == nil
}

func loopSafeValue(value reflect.Value) bool {
	if value.Kind() == reflect.Pointer {
		return value.IsNil() || loopSafeValue(value.Elem())
	}
	switch value.Kind() {
	case reflect.Struct:
		for i := 0; i < value.NumField(); i++ {
			if !loopSafeValue(value.Field(i)) {
				return false
			}
		}
	case reflect.Slice:
		for i := 0; i < value.Len(); i++ {
			if !loopSafeValue(value.Index(i)) {
				return false
			}
		}
	case reflect.String:
		return loopSafeIdentity.MatchString(value.String())
	}
	return true
}

func DecodeLoopEvidence(j asset.JSON, remoteRunID string, correlation LoopCorrelation) (LoopEvidence, error) {
	var v LoopEvidence
	if len(j.Bytes()) > MaxLoopEvidenceBytes || StrictLoopShape(j.Bytes(), &v) != nil ||
		v.Schema != LoopEvidenceSchema || v.Producer != "localagent-loop-producer.v1" || v.Policy != "durable-agent-loop.v1" || v.Algorithm != LoopImplementation ||
		v.RunID != remoteRunID || v.ID != "loop-evidence://"+remoteRunID || v.Correlation != correlation || !loopSafeValue(reflect.ValueOf(v)) || !asset.Text(v.FrameID) || !asset.Text(v.StepID) ||
		!loopDigest.MatchString(v.GoalDigest) || !loopDigest.MatchString(v.ConfigDigest) || !oneOf(v.Coverage, "COMPLETE", "PARTIAL") ||
		!oneOf(v.Root.Status, "SUCCEEDED", "FAILED", "CANCELLED") || !loopBudgetUsageValid(v.Budget) || v.IterationCount < 0 || v.IterationCount > 64 {
		return v, asset.ErrInvalid
	}
	return v, nil
}

func LoopSupported(d metric.EvaluatorDefinition) bool {
	var c Config
	return d.Validate() == nil && d.Availability == asset.ContractOnly && d.Kind == metric.Deterministic &&
		d.ImplementationRef == LoopImplementation && d.InputContract == LoopInputContract && d.SchemaVersion == LoopOutputSchema &&
		d.Normalization == LoopNormalization && len(d.OutputMetrics) == 1 && d.Budget.MaxProviderCalls == 0 &&
		d.Config.Decode(&c) == nil && c.Metric == LoopImplementation && c.K == 0
}

// LoopIntegrity 只判断执行证据完整性，不将 FINAL/REFUSAL 当成任务质量成功。
func LoopIntegrity(v LoopEvidence) (string, string) {
	if !v.Sealed || v.Coverage != "COMPLETE" || len(v.Gaps) > 0 {
		return "INCONCLUSIVE", "LOOP_EVIDENCE_INCOMPLETE"
	}
	if v.Root.TerminalSequence == nil || *v.Root.TerminalSequence < 1 || len(v.Iterations) != v.IterationCount ||
		v.Budget.Iterations != v.IterationCount || v.Budget.MaxIterations < 1 || v.Budget.MaxIterations > 64 ||
		v.Budget.MaxModelCalls < 1 || v.Budget.MaxModelCalls > 32 || v.Budget.MaxToolCalls < 1 || v.Budget.MaxToolCalls > 16 ||
		v.Budget.ModelCalls < 0 || v.Budget.ToolCalls < 0 || v.Budget.ModelCalls > v.Budget.MaxModelCalls || v.Budget.ToolCalls > v.Budget.MaxToolCalls {
		return "FAIL", "LOOP_COUNTER_BINDING_INVALID"
	}
	if *v.Budget.ConsumedUsage["model_calls"] != int64(v.Budget.ModelCalls) || *v.Budget.ConsumedUsage["tool_calls"] != int64(v.Budget.ToolCalls) || v.IterationCount > v.Budget.MaxIterations {
		return "FAIL", "LOOP_BUDGET_BINDING_INVALID"
	}
	if v.Stop.Code == "LOOP_BUDGET_EXHAUSTED" {
		if v.Budget.StoppedDimension == nil || v.Budget.StopRequested == nil || v.Budget.StopRemaining == nil {
			return "INCONCLUSIVE", "LOOP_BUDGET_STOP_EVIDENCE_UNAVAILABLE"
		}
		maximum, ok := v.Budget.MaxUsage[*v.Budget.StoppedDimension]
		if !ok || maximum == nil {
			return "INCONCLUSIVE", "LOOP_BUDGET_STOP_EVIDENCE_UNAVAILABLE"
		}
		if *v.Budget.StopRequested <= *v.Budget.StopRemaining || *v.Budget.StopRequested < 0 || *v.Budget.StopRemaining < 0 || v.Root.StopReason != "BUDGET_EXHAUSTED" {
			return "FAIL", "LOOP_BUDGET_STOP_BINDING_INVALID"
		}
	} else if v.Budget.StoppedDimension != nil || v.Budget.StopRequested != nil || v.Budget.StopRemaining != nil {
		return "FAIL", "LOOP_BUDGET_STOP_BINDING_INVALID"
	}
	decisions, invocations, attempts := map[string]bool{}, map[string]bool{}, map[string]bool{}
	actions, sideEffects := map[string]bool{}, map[string]bool{}
	cursor, toolCount, finalCount := 0, 0, 0
	for n, it := range v.Iterations {
		if it.Iteration != n+1 {
			return "FAIL", "LOOP_ITERATION_ORDER_INVALID"
		}
		if it.Aborted {
			if n != len(v.Iterations)-1 || it.DecisionID != nil || it.Kind != nil || it.Action != nil || v.Root.Status == "SUCCEEDED" {
				return "FAIL", "LOOP_ABORT_INVALID"
			}
			continue
		}
		if it.DecisionID == nil || it.Kind == nil || it.ModelAttemptRef == nil || it.AcceptedGeneration == nil || it.ResumeGeneration == nil {
			return "INCONCLUSIVE", "LOOP_DECISION_BINDING_MISSING"
		}
		if !asset.Text(*it.DecisionID) || !asset.Text(*it.ModelAttemptRef) || decisions[*it.DecisionID] || attempts[*it.ModelAttemptRef] || *it.AcceptedGeneration < 0 || *it.ResumeGeneration < *it.AcceptedGeneration {
			return "FAIL", "LOOP_DECISION_BINDING_INVALID"
		}
		decisions[*it.DecisionID] = true
		attempts[*it.ModelAttemptRef] = true
		if *it.Kind == "TOOL_CALL" {
			a := it.Action
			if a == nil || a.InvocationID == nil || a.IdempotencyDigest == nil || a.ArgumentsDigest == nil || a.ToolIdentity == nil || a.ResultRef == nil || a.ResultDigest == nil || a.ObservationSource == nil || a.ObservationDigest == nil || a.ObservationCursor == nil {
				return "INCONCLUSIVE", "LOOP_OBSERVATION_MISSING"
			}
			if !asset.Text(a.ActionID) || actions[a.ActionID] || !asset.Text(*a.InvocationID) || invocations[*a.InvocationID] || !asset.Text(*a.ToolIdentity) || !loopDigest.MatchString(*a.IdempotencyDigest) || !loopDigest.MatchString(*a.ArgumentsDigest) || !loopDigest.MatchString(*a.ResultDigest) || !loopDigest.MatchString(*a.ObservationSource) || *a.ObservationSource != *a.ResultDigest || !loopDigest.MatchString(*a.ObservationDigest) || *a.ObservationCursor != cursor+1 || *a.ResultRef != "tool-result://"+*a.InvocationID {
				return "FAIL", "LOOP_ACTION_BINDING_INVALID"
			}
			invocations[*a.InvocationID] = true
			actions[a.ActionID] = true
			cursor++
			toolCount++
			if a.SideEffecting && (a.SideEffectProof != "VERIFIED" || a.ProviderReceiptDigest == nil || !loopDigest.MatchString(*a.ProviderReceiptDigest) || a.ToolAttemptRef == nil || !asset.Text(*a.ToolAttemptRef)) {
				return "INCONCLUSIVE", "LOOP_SIDE_EFFECT_INDETERMINATE"
			}
			if a.SideEffecting {
				if sideEffects[*a.IdempotencyDigest] {
					return "FAIL", "LOOP_SIDE_EFFECT_DUPLICATE"
				}
				sideEffects[*a.IdempotencyDigest] = true
			}
			if !a.SideEffecting && a.SideEffectProof != "NOT_APPLICABLE" {
				return "FAIL", "LOOP_PROOF_INVALID"
			}
		} else if oneOf(*it.Kind, "FINAL", "REFUSAL") {
			finalCount++
			if it.Action != nil || n != len(v.Iterations)-1 || v.Final == nil || v.Final.DecisionID != *it.DecisionID {
				return "FAIL", "LOOP_FINAL_BINDING_INVALID"
			}
		} else {
			return "FAIL", "LOOP_DECISION_KIND_INVALID"
		}
	}
	if toolCount > v.Budget.ToolCalls || len(attempts) > v.Budget.ModelCalls {
		return "FAIL", "LOOP_BUDGET_BINDING_INVALID"
	}
	if v.Root.Status == "SUCCEEDED" {
		if finalCount != 1 || v.Final == nil || !asset.Text(v.Final.DeliveryID) || v.Final.Cursor < 1 || !loopDigest.MatchString(v.Final.ContentDigest) || v.Root.StopReason != "COMPLETED" || !oneOf(v.Stop.Code, "FINAL", "REFUSAL") {
			return "FAIL", "LOOP_FINAL_BINDING_INVALID"
		}
	} else if v.Final != nil && (finalCount != 1 || !asset.Text(v.Final.DeliveryID) || v.Final.Cursor < 1 || !loopDigest.MatchString(v.Final.ContentDigest)) {
		return "FAIL", "LOOP_STOP_BINDING_INVALID"
	}
	if v.Stop.Code == "LOOP_NO_PROGRESS" && (v.Stop.NoProgressThreshold != 2 || v.Stop.NoProgressCount < 2 || v.Root.StopReason != "REPEATED_ACTION") {
		return "FAIL", "LOOP_NO_PROGRESS_BINDING_INVALID"
	}
	if v.Stop.Code == "LOOP_ITERATION_LIMIT" && (v.IterationCount < v.Budget.MaxIterations || v.Root.StopReason != "BUDGET_EXHAUSTED") {
		return "FAIL", "LOOP_LIMIT_BINDING_INVALID"
	}
	return "PASS", "LOOP_INTEGRITY_CONFIRMED"
}

func evaluateLoop(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if err := ctx.Err(); err != nil {
		return worker.EvaluationOutput{}, err
	}
	value := BaseValue(in, "INCONCLUSIVE", "LOOP_EVIDENCE_MISSING")
	count := 0
	for _, b := range in.Attempt.Metadata.Observation.Evidence {
		if b.Schema != LoopEvidenceSchema {
			continue
		}
		count++
		if !bound(b, in) || b.Availability != "AVAILABLE" {
			return worker.EvaluationOutput{Value: value}, nil
		}
		v, err := DecodeLoopEvidence(b.Body, in.Attempt.ID, LoopCorrelation{in.Attempt.RunID, in.Attempt.ID, in.Attempt.RequestID})
		if err != nil {
			value = BaseValue(in, "ERROR", "LOOP_EVIDENCE_INVALID")
			return worker.EvaluationOutput{Value: value}, nil
		}
		value.Verdict, value.SourceError = LoopIntegrity(v)
	}
	if count != 1 {
		value = BaseValue(in, "INCONCLUSIVE", "LOOP_EVIDENCE_MISSING")
	}
	value.SourceVerdict = value.Verdict
	value.Reason = value.SourceError
	if oneOf(value.Verdict, "PASS", "FAIL") {
		score := 0.0
		if value.Verdict == "PASS" {
			score = 1
		}
		value.Score = &score
	}
	applicability := asset.Applicable
	if value.Verdict == "INCONCLUSIVE" {
		applicability = asset.MissingEvidence
	}
	value.Provenance, _ = asset.Freeze(map[string]any{"metric": LoopImplementation, "applicability": applicability, "implementation_ref": LoopImplementation, "required_evidence": LoopEvidenceSchema})
	return worker.EvaluationOutput{Value: value}, nil
}

// LoopEvaluatorDefinition 通过现有目录发布/冻结，不引入额外注册表。
func LoopEvaluatorDefinition(output asset.Ref) metric.EvaluatorDefinition {
	config, _ := asset.Freeze(Config{Metric: LoopImplementation})
	return metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: LoopInputContract, OutputMetrics: []asset.Ref{output},
		Applicability: asset.Applicability{RequiredEvidence: []asset.EvidenceRequirement{{Kind: "loop_integrity", Schema: LoopEvidenceSchema, BodyRequired: true}}, RuleRef: LoopImplementation},
		Availability:  asset.ContractOnly, ImplementationRef: LoopImplementation, SchemaVersion: LoopOutputSchema, Config: config,
		Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 4096}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}, Normalization: LoopNormalization}
}
