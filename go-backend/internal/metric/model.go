// Package metric 拥有“测什么”和“如何判分”的独立定义；G1 不包含算法执行。
package metric

import (
	"math"

	"agentevalops/go-backend/internal/asset"
)

type Grain string

const (
	Case        Grain = "CASE"
	Run         Grain = "RUN"
	Trace       Grain = "TRACE"
	Observation Grain = "OBSERVATION"
	Plan        Grain = "PLAN"
	Step        Grain = "STEP"
	ToolCall    Grain = "TOOL_CALL"
	Retrieval   Grain = "RETRIEVAL"
	Session     Grain = "SESSION"
)

type ValueType string

const (
	Enum     ValueType = "ENUM"
	Boolean  ValueType = "BOOLEAN"
	Scalar   ValueType = "SCALAR"
	Rate     ValueType = "RATE"
	Count    ValueType = "COUNT"
	Duration ValueType = "DURATION"
	Cost     ValueType = "COST"
)

type Direction string

const (
	Higher Direction = "HIGHER_IS_BETTER"
	Lower  Direction = "LOWER_IS_BETTER"
	None   Direction = "NONE"
)

type Definition struct {
	Name          string              `json:"name"`
	Grain         Grain               `json:"grain"`
	ValueType     ValueType           `json:"value_type"`
	Labels        []string            `json:"labels"`
	Range         *[2]float64         `json:"score_range"`
	Unit          string              `json:"unit"`
	Direction     Direction           `json:"direction"`
	Denominator   string              `json:"denominator_semantics"`
	Missing       string              `json:"missing_semantics"`
	Aggregation   string              `json:"aggregation_semantics"`
	Comparison    string              `json:"comparison_semantics"`
	Applicability asset.Applicability `json:"applicability"`
	Availability  asset.Availability  `json:"availability"`
	Parameters    asset.JSON          `json:"parameters"`
}

func (d Definition) Validate() error {
	switch d.Grain {
	case Case, Run, Trace, Observation, Plan, Step, ToolCall, Retrieval, Session:
	default:
		return asset.ErrInvalid
	}
	switch d.ValueType {
	case Enum, Boolean, Scalar, Rate, Count, Duration, Cost:
	default:
		return asset.ErrInvalid
	}
	if d.Direction != Higher && d.Direction != Lower && d.Direction != None {
		return asset.ErrInvalid
	}
	if d.Availability != asset.ContractOnly && d.Availability != asset.Unsupported {
		return asset.ErrInvalid
	}
	for _, s := range []string{d.Name, d.Unit, d.Denominator, d.Missing, d.Aggregation, d.Comparison} {
		if !asset.Text(s) {
			return asset.ErrInvalid
		}
	}
	if d.ValueType == Enum && len(d.Labels) == 0 {
		return asset.ErrInvalid
	}
	if d.Range != nil && (math.IsNaN(d.Range[0]) || math.IsNaN(d.Range[1]) || math.IsInf(d.Range[0], 0) || math.IsInf(d.Range[1], 0) || d.Range[0] > d.Range[1]) {
		return asset.ErrInvalid
	}
	return d.Applicability.Validate()
}

type EvaluatorKind string

const (
	Deterministic EvaluatorKind = "DETERMINISTIC"
	LLMJudge      EvaluatorKind = "LLM_JUDGE"
	Human         EvaluatorKind = "HUMAN"
	Imported      EvaluatorKind = "IMPORTED"
)

type ModelBinding struct {
	Provider string `json:"provider"`
	Model    string `json:"model"`
	Revision string `json:"revision"`
}
type Budget struct {
	TotalMilliseconds int64 `json:"total_milliseconds"`
	MaxProviderCalls  int   `json:"max_provider_calls"`
	MaxResponseBytes  int   `json:"max_response_bytes"`
}
type RetryPolicy struct {
	MaxEvaluationAttempts      int   `json:"max_evaluation_attempts"`
	InitialBackoffMilliseconds int64 `json:"initial_backoff_milliseconds"`
	MaxBackoffMilliseconds     int64 `json:"max_backoff_milliseconds"`
}
type EvaluatorDefinition struct {
	Kind              EvaluatorKind       `json:"kind"`
	InputContract     string              `json:"input_contract"`
	OutputMetrics     []asset.Ref         `json:"output_metrics"`
	Applicability     asset.Applicability `json:"applicability"`
	Availability      asset.Availability  `json:"availability"`
	ImplementationRef string              `json:"implementation_ref"`
	Config            asset.JSON          `json:"config_snapshot"`
	Rubric            asset.JSON          `json:"rubric"`
	PromptRef         *asset.Ref          `json:"prompt_ref"`
	SchemaVersion     string              `json:"schema_version"`
	Model             *ModelBinding       `json:"model_binding"`
	Budget            Budget              `json:"execution_budget"`
	Retry             RetryPolicy         `json:"retry_policy"`
	Normalization     string              `json:"normalization_semantics"`
}

func (e EvaluatorDefinition) Validate() error {
	switch e.Kind {
	case Deterministic, LLMJudge, Human, Imported:
	default:
		return asset.ErrInvalid
	}
	if e.Availability != asset.ContractOnly && e.Availability != asset.Unsupported {
		return asset.ErrInvalid
	}
	for _, s := range []string{e.InputContract, e.ImplementationRef, e.SchemaVersion, e.Normalization} {
		if !asset.Text(s) {
			return asset.ErrInvalid
		}
	}
	if len(e.OutputMetrics) == 0 || e.Budget.TotalMilliseconds <= 0 || e.Budget.MaxProviderCalls < 0 || e.Budget.MaxResponseBytes <= 0 || e.Retry.MaxEvaluationAttempts <= 0 || e.Retry.InitialBackoffMilliseconds < 0 || e.Retry.MaxBackoffMilliseconds < e.Retry.InitialBackoffMilliseconds {
		return asset.ErrInvalid
	}
	if e.Kind == Deterministic && e.Budget.MaxProviderCalls != 0 {
		return asset.ErrInvalid
	}
	if e.Kind == LLMJudge && (e.Model == nil || !asset.Text(e.Model.Provider) || !asset.Text(e.Model.Model) || !asset.Text(e.Model.Revision) || e.PromptRef == nil || e.PromptRef.Validate() != nil || e.Budget.MaxProviderCalls == 0) {
		return asset.ErrInvalid
	}
	seen := map[asset.Ref]bool{}
	for _, r := range e.OutputMetrics {
		if r.Validate() != nil || seen[r] {
			return asset.ErrInvalid
		}
		seen[r] = true
	}
	return e.Applicability.Validate()
}

type DefinitionVersion = asset.Version[Definition]
type EvaluatorDefinitionVersion = asset.Version[EvaluatorDefinition]
