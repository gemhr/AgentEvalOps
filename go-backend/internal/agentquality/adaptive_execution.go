package agentquality

import (
	"context"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
)

const AdaptiveSchema = "adaptive-execution-evidence.v1"
const AdaptiveImplementation = "adaptive-execution-metrics.v1"

type AdaptiveIdentity struct {
	ProjectID       string `json:"project_id"`
	EvaluationRunID string `json:"evaluation_run_id"`
	AttemptID       string `json:"attempt_id"`
	RequestID       string `json:"request_id"`
	CaseID          string `json:"case_id"`
	CaseVersion     string `json:"case_version"`
	CaseDigest      string `json:"case_digest"`
	DatasetDigest   string `json:"dataset_digest"`
	InputDigest     string `json:"input_digest"`
	RunID           string `json:"run_id"`
}
type AdaptiveStop struct {
	Version            string `json:"version"`
	PolicyDigest       string `json:"policy_digest"`
	ObservationCursor  int    `json:"observation_cursor"`
	EvidenceDigest     string `json:"evidence_digest"`
	EffectiveIteration int    `json:"effective_iteration"`
	AllowedActions     string `json:"allowed_actions"`
	Reason             string `json:"reason"`
	Insufficient       bool   `json:"insufficient"`
	DecisionID         string `json:"decision_id"`
}
type AdaptiveFrame struct {
	Ref          string              `json:"ref"`
	Role         string              `json:"role"`
	ConfigDigest string              `json:"config_digest"`
	SeedDigest   string              `json:"seed_digest"`
	Iterations   int                 `json:"iteration_count"`
	Observations int                 `json:"observation_count"`
	Stop         *AdaptiveStop       `json:"stop_decision"`
	Evidence     []AdaptiveIteration `json:"iterations"`
}

type AdaptiveIteration struct {
	Number      int     `json:"number"`
	State       string  `json:"state"`
	Kind        *string `json:"kind"`
	Decision    *string `json:"decision_ref"`
	Cursor      *int    `json:"observation_cursor"`
	Observation *string `json:"observation_digest"`
	Source      *string `json:"source_digest"`
}

type AdaptiveToolAction struct {
	Frame      string  `json:"frame_ref"`
	Iteration  int     `json:"iteration"`
	Invocation *string `json:"invocation_ref"`
	Started    bool    `json:"started"`
}
type AdaptiveStrategy struct {
	Group        string  `json:"group"`
	Version      string  `json:"version"`
	Digest       string  `json:"digest"`
	Mode         string  `json:"mode"`
	Engine       string  `json:"engine_policy"`
	RouteVersion *string `json:"route_policy_version"`
	RouteDigest  *string `json:"route_policy_digest"`
	RouteReason  *string `json:"route_reason"`
}
type AdaptiveRuntime struct {
	Status    string  `json:"status"`
	Stop      string  `json:"stop_reason"`
	Error     *string `json:"error_code"`
	Delivered bool    `json:"delivered"`
}
type AdaptiveBusiness struct {
	Slice      string  `json:"slice"`
	Outcome    string  `json:"outcome"`
	Validation string  `json:"validation"`
	Output     *string `json:"output"`
}
type AdaptiveUsage struct {
	Input  int64 `json:"input_tokens"`
	Output int64 `json:"output_tokens"`
	Total  int64 `json:"total_tokens"`
}
type AdaptiveCost struct {
	Amount   int64  `json:"amount_micros"`
	Currency string `json:"currency"`
}
type AdaptiveMetrics struct {
	Models         int            `json:"model_attempts_started"`
	Tools          int            `json:"tool_invocations_started"`
	ModelStates    map[string]int `json:"model_states"`
	ToolStates     map[string]int `json:"tool_states"`
	Usage          *AdaptiveUsage `json:"provider_usage"`
	UsageAvailable int            `json:"usage_available_attempts"`
	Cost           *AdaptiveCost  `json:"actual_cost"`
	ModelMS        *int64         `json:"model_duration_ms"`
	ToolMS         *int64         `json:"tool_duration_ms"`
	RouterMS       *int64         `json:"router_duration_ms"`
	AdmissionMS    *int64         `json:"admission_duration_ms"`
	DeliveryMS     *int64         `json:"delivery_duration_ms"`
	MeaningfulMS   *int64         `json:"first_meaningful_response_ms"`
}
type AdaptiveFast struct {
	Steps int      `json:"step_count"`
	Tools []string `json:"tool_refs"`
}
type AdaptiveLoop struct {
	Frames  []AdaptiveFrame      `json:"frames"`
	Actions []AdaptiveToolAction `json:"tool_actions"`
}
type AdaptiveMode struct {
	Fast *AdaptiveFast `json:"fast"`
	Loop *AdaptiveLoop `json:"loop"`
}
type AdaptiveEvidence struct {
	Schema        string                `json:"schema_version"`
	Identity      AdaptiveIdentity      `json:"identity"`
	RunID         string                `json:"run_id"`
	Strategy      AdaptiveStrategy      `json:"strategy"`
	Runtime       AdaptiveRuntime       `json:"runtime"`
	Business      AdaptiveBusiness      `json:"business"`
	Metrics       AdaptiveMetrics       `json:"metrics"`
	Mode          AdaptiveMode          `json:"mode_evidence"`
	Comparability AdaptiveComparability `json:"comparability"`
}

type AdaptiveComparability struct {
	Model         string  `json:"model_configuration_digest"`
	Tools         string  `json:"tool_configuration_digest"`
	Authorization string  `json:"authorization_digest"`
	Timeout       int     `json:"timeout_milliseconds"`
	Input         string  `json:"business_input_digest"`
	Seed          *string `json:"investigation_seed_digest"`
}
type AdaptiveClientObservation = ev.ClientObservation

func AdaptivePolicyDigest(group string) string {
	if !oneOf(group, "BASELINE", "CANDIDATE_A", "CANDIDATE_B") {
		return ""
	}
	mode := "FAST_PATH"
	if group == "BASELINE" {
		mode = "AGENT_LOOP"
	}
	j, _ := asset.Freeze(map[string]any{"version": "ci-adaptive-evaluation.v1", "group": group, "query_mode": mode, "diagnosis_stop": group == "CANDIDATE_B"})
	return j.Digest()
}

func DecodeAdaptive(j asset.JSON) (AdaptiveEvidence, error) {
	var v AdaptiveEvidence
	if StrictLoopShape(j.Bytes(), &v) != nil || v.Schema != AdaptiveSchema || v.RunID != v.Identity.RunID || v.RunID != v.Identity.AttemptID ||
		!asset.ValidID(v.RunID) || !asset.ValidID(v.Identity.EvaluationRunID) || !asset.ValidID(v.Identity.RequestID) || !asset.ValidID(v.Identity.CaseID) ||
		!asset.ValidID(v.Identity.ProjectID) || !asset.Text(v.Identity.CaseVersion) || !hex64(v.Identity.CaseDigest) || !hex64(v.Identity.DatasetDigest) || !hex64(v.Identity.InputDigest) ||
		v.Strategy.Version != "ci-adaptive-evaluation.v1" || v.Strategy.Digest != AdaptivePolicyDigest(v.Strategy.Group) ||
		!oneOf(v.Runtime.Status, "SUCCEEDED", "FAILED", "CANCELLED") || !oneOf(v.Business.Slice, "STATUS", "DIAGNOSIS") ||
		!oneOf(v.Business.Validation, "ACCEPTED", "REJECTED", "NOT_EVALUATED") {
		return v, asset.ErrInvalid
	}
	if !hex64(v.Comparability.Model) || !hex64(v.Comparability.Tools) || !hex64(v.Comparability.Authorization) || v.Comparability.Timeout < 1 || v.Comparability.Timeout > 300000 || v.Comparability.Input != v.Identity.InputDigest || v.Business.Slice == "DIAGNOSIS" && (v.Comparability.Seed == nil || !hex64(*v.Comparability.Seed)) || v.Business.Slice == "STATUS" && v.Comparability.Seed != nil {
		return v, asset.ErrInvalid
	}
	if v.Runtime.Status == "SUCCEEDED" && v.Runtime.Stop != "COMPLETED" || v.Runtime.Status != "SUCCEEDED" && v.Runtime.Stop == "COMPLETED" {
		return v, asset.ErrInvalid
	}
	fast := v.Strategy.Mode == "FAST_PATH"
	if fast != (v.Business.Slice == "STATUS" && v.Strategy.Group != "BASELINE") || !oneOf(v.Strategy.Mode, "FAST_PATH", "AGENT_LOOP") {
		return v, asset.ErrInvalid
	}
	if fast {
		if v.Strategy.Engine != "fixed-plan.v1" || v.Mode.Fast == nil || v.Mode.Loop != nil || v.Mode.Fast.Steps != 1 || v.Metrics.Models != 0 || len(v.Metrics.ModelStates) != 5 {
			return v, asset.ErrInvalid
		}
		for _, r := range v.Mode.Fast.Tools {
			if !hex64(r) {
				return v, asset.ErrInvalid
			}
		}
	} else {
		if v.Strategy.Engine != "durable-agent-loop.v2" || v.Mode.Fast != nil || v.Mode.Loop == nil {
			return v, asset.ErrInvalid
		}
		roots := 0
		for _, f := range v.Mode.Loop.Frames {
			if f.Role == "ROOT" {
				roots++
			}
			if !oneOf(f.Role, "ROOT", "CHILD") || !hex64(f.Ref) || !hex64(f.ConfigDigest) || !hex64(f.SeedDigest) || f.Observations < 0 || f.Iterations < f.Observations {
				return v, asset.ErrInvalid
			}
			if len(f.Evidence) != f.Iterations {
				return v, asset.ErrInvalid
			}
			cursor := 0
			for n, it := range f.Evidence {
				if it.Number != n+1 || !oneOf(it.State, "OPEN", "ACCEPTED", "OBSERVED", "CLOSED") || it.Kind != nil && !oneOf(*it.Kind, "TOOL_CALL", "TOOL_BATCH", "HANDOFF", "FINAL", "REFUSAL") || it.Decision != nil && !hex64(*it.Decision) {
					return v, asset.ErrInvalid
				}
				if it.State == "OBSERVED" {
					cursor++
					if it.Cursor == nil || *it.Cursor != cursor || it.Observation == nil || !hex64(*it.Observation) || it.Source == nil || !hex64(*it.Source) {
						return v, asset.ErrInvalid
					}
				}
				if f.Stop != nil && it.Number >= f.Stop.EffectiveIteration && it.Kind != nil && !oneOf(*it.Kind, "FINAL", "REFUSAL") {
					return v, asset.ErrInvalid
				}
			}
			if cursor != f.Observations {
				return v, asset.ErrInvalid
			}
			if d := f.Stop; d != nil {
				stopPolicy, _ := asset.Freeze(map[string]any{"version": "ci-investigation-stop.v1", "coverage_rule": "ci-observed-minimum.v1", "reserve_seconds": 15})
				if v.Strategy.Group != "CANDIDATE_B" || v.Business.Slice != "DIAGNOSIS" || f.Role != "ROOT" || d.Version != "final-only.v1" ||
					d.PolicyDigest != stopPolicy.Digest() || !hex64(d.EvidenceDigest) || d.ObservationCursor != f.Observations || d.EffectiveIteration < 1 || d.EffectiveIteration > f.Iterations+1 ||
					d.AllowedActions != "FINAL_ONLY" || !oneOf(d.Reason, "COVERAGE_SUFFICIENT", "BUDGET_RESERVE") || d.Insufficient != (d.Reason == "BUDGET_RESERVE") {
					return v, asset.ErrInvalid
				}
				raw, _ := asset.Freeze(d)
				var payload map[string]any
				_ = raw.Decode(&payload)
				delete(payload, "decision_id")
				proof, _ := asset.Freeze(payload)
				// decision_id 使用 Core 的 canonical 算法；字段只含 ASCII/整数，摘要一致。
				if d.DecisionID != proof.Digest() {
					return v, asset.ErrInvalid
				}
			}
		}
		if roots != 1 {
			return v, asset.ErrInvalid
		}
		for _, action := range v.Mode.Loop.Actions {
			found := false
			for _, f := range v.Mode.Loop.Frames {
				if f.Ref == action.Frame {
					found = true
					if action.Iteration < 1 || action.Iteration > f.Iterations || action.Started && (action.Invocation == nil || !hex64(*action.Invocation) || f.Stop != nil && action.Iteration >= f.Stop.EffectiveIteration) {
						return v, asset.ErrInvalid
					}
				}
			}
			if !found {
				return v, asset.ErrInvalid
			}
		}
	}
	if v.Business.Slice == "STATUS" && (v.Strategy.RouteVersion == nil || v.Strategy.RouteDigest == nil || v.Strategy.RouteReason == nil || !hex64(*v.Strategy.RouteDigest)) {
		return v, asset.ErrInvalid
	}
	if v.Business.Slice == "STATUS" {
		policy, _ := asset.Freeze(map[string]any{"version": "ci-status-routing.v1", "modes": map[string]string{"FAST_PATH": "fixed-plan.v1", "AGENT_LOOP": "durable-agent-loop.v2"}, "task_type": "CI_EXECUTION_STATUS"})
		if *v.Strategy.RouteVersion != "ci-status-routing.v1" || *v.Strategy.RouteDigest != policy.Digest() || *v.Strategy.RouteReason != "EXPLICIT_STATUS_QUERY" {
			return v, asset.ErrInvalid
		}
	}
	if v.Business.Validation == "ACCEPTED" && (v.Business.Output == nil || !v.Runtime.Delivered || v.Runtime.Status != "SUCCEEDED") {
		return v, asset.ErrInvalid
	}
	if v.Business.Output != nil {
		if _, e := asset.ParseJSON([]byte(*v.Business.Output)); e != nil {
			return v, asset.ErrInvalid
		}
		if v.Business.Validation != "ACCEPTED" {
			return v, asset.ErrInvalid
		}
		if v.Business.Slice == "STATUS" {
			var output struct {
				Schema    string          `json:"schema_version"`
				Kind      string          `json:"kind"`
				Incident  string          `json:"incident_id"`
				Execution string          `json:"execution_id"`
				Revision  int             `json:"state_revision"`
				Summary   map[string]*any `json:"summary"`
			}
			if StrictLoopShape([]byte(*v.Business.Output), &output) != nil || output.Schema != "ci-status.v1" || output.Kind != "STATUS" || v.Business.Outcome != "STATUS" || !asset.ValidID(output.Incident) || !asset.ValidID(output.Execution) || output.Revision < 0 {
				return v, asset.ErrInvalid
			}
		} else {
			var output diagnosisReport
			if StrictLoopShape([]byte(*v.Business.Output), &output) != nil || output.Schema != "ci-diagnosis.v1" || output.Kind != "REPORT" || !asset.ValidID(output.Incident) || output.Status != v.Business.Outcome || !oneOf(output.Status, "SUPPORTED", "INSUFFICIENT_EVIDENCE") {
				return v, asset.ErrInvalid
			}
		}
	}
	count := func(values map[string]int, keys ...string) (int, bool) {
		if len(values) != len(keys) {
			return 0, false
		}
		n := 0
		for _, k := range keys {
			v, ok := values[k]
			if !ok || v < 0 {
				return 0, false
			}
			n += v
		}
		return n, true
	}
	models, ok := count(v.Metrics.ModelStates, "not_started", "started", "completed", "unknown", "rejected")
	if !ok {
		return v, asset.ErrInvalid
	}
	tools, ok := count(v.Metrics.ToolStates, "prepared", "started", "committed", "unknown", "not_committed")
	if !ok || v.Metrics.Models < 0 || v.Metrics.Tools < 0 || v.Metrics.Models > models || v.Metrics.Tools > tools || v.Metrics.UsageAvailable < 0 || v.Metrics.UsageAvailable > v.Metrics.Models {
		return v, asset.ErrInvalid
	}
	if fast && models != 0 {
		return v, asset.ErrInvalid
	}
	if u := v.Metrics.Usage; u != nil && (u.Input < 0 || u.Output < 0 || u.Total < u.Input+u.Output || v.Metrics.UsageAvailable != v.Metrics.Models) {
		return v, asset.ErrInvalid
	}
	for _, n := range []*int64{v.Metrics.ModelMS, v.Metrics.ToolMS, v.Metrics.RouterMS, v.Metrics.AdmissionMS, v.Metrics.DeliveryMS, v.Metrics.MeaningfulMS} {
		if n != nil && *n < 0 {
			return v, asset.ErrInvalid
		}
	}
	return v, nil
}

func hex64(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, r := range s {
		if !(r >= '0' && r <= '9' || r >= 'a' && r <= 'f') {
			return false
		}
	}
	return true
}

func AdaptiveSupported(d metric.EvaluatorDefinition) bool {
	var c Config
	return d.Validate() == nil && d.ImplementationRef == AdaptiveImplementation && d.Kind == metric.Deterministic && d.InputContract == AdaptiveSchema && d.SchemaVersion == "adaptive-metric-result.v1" && len(d.OutputMetrics) == 1 && d.Config.Decode(&c) == nil && oneOf(c.Metric, "quality", "client_rt_ms", "model_attempts", "tool_invocations", "actual_cost_micros", "provider_tokens")
}

func AdaptiveEvaluatorDefinition(output asset.Ref, name string) metric.EvaluatorDefinition {
	c, _ := asset.Freeze(Config{Metric: name})
	return metric.EvaluatorDefinition{Kind: metric.Deterministic, ImplementationRef: AdaptiveImplementation, InputContract: AdaptiveSchema, SchemaVersion: "adaptive-metric-result.v1", Normalization: "adaptive-null-preserving.v1", OutputMetrics: []asset.Ref{output}, Config: c, Availability: asset.ContractOnly, Applicability: asset.Applicability{RuleRef: AdaptiveImplementation}, Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 4096}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}}
}

func AdaptiveMetricDefinition(name string) metric.Definition {
	direction := metric.Lower
	if name == "quality" {
		direction = metric.Higher
	}
	return metric.Definition{Name: "Adaptive " + name, Grain: metric.Case, ValueType: metric.Scalar, Unit: name, Direction: direction, Denominator: "all planned attempts; unknown/missing remain explicit", Missing: "null; never impute", Aggregation: "mean; disclose eligible/known; latency quantiles nearest-rank", Comparison: "same frozen case/environment; exact strategy difference requires proof", Applicability: asset.Applicability{RuleRef: AdaptiveImplementation}, Availability: asset.ContractOnly}
}

func evaluateAdaptive(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	v := BaseValue(in, "INCONCLUSIVE", "ADAPTIVE_EVIDENCE_MISSING")
	var config Config
	_ = in.Work.Metadata.Spec.Definition.Config.Decode(&config)
	var a *AdaptiveEvidence
	for _, b := range in.Attempt.Metadata.Observation.Evidence {
		if b.Schema == AdaptiveSchema {
			parsed, e := DecodeAdaptive(b.Body)
			if a != nil || !bound(b, in) || e != nil {
				return worker.EvaluationOutput{Value: BaseValue(in, "ERROR", "ADAPTIVE_EVIDENCE_INVALID")}, nil
			}
			a = &parsed
		}
	}
	var cleanup AdaptiveClientObservation
	// Cleanup 含协议归因等其他固定字段，仅读取已持久化的 RT，不能覆盖执行状态。
	_ = in.Attempt.Metadata.Observation.Cleanup.Decode(&cleanup)
	var score *float64
	set := func(n float64) { score = &n }
	if config.Metric == "client_rt_ms" {
		if cleanup.ClientRT != nil {
			set(float64(*cleanup.ClientRT))
		}
	} else if a != nil {
		id := a.Identity
		if id.ProjectID != in.Attempt.ProjectID || id.CaseVersion != in.Attempt.CaseVersion || id.EvaluationRunID != in.Attempt.RunID || id.AttemptID != in.Attempt.ID || id.RequestID != in.Attempt.RequestID || id.CaseID != in.Attempt.CaseID || id.CaseDigest != in.Attempt.Request.Case.Identity.ContentDigest || id.InputDigest != in.Attempt.Request.Case.Case.Input.Digest() || in.Run.Snapshot.Input.Dataset == nil || id.DatasetDigest != in.Run.Snapshot.Input.Dataset.ContentDigest {
			return worker.EvaluationOutput{Value: BaseValue(in, "ERROR", "ADAPTIVE_IDENTITY_INVALID")}, nil
		}
		switch config.Metric {
		case "model_attempts":
			set(float64(a.Metrics.Models))
		case "tool_invocations":
			set(float64(a.Metrics.Tools))
		case "provider_tokens":
			if a.Metrics.Usage != nil {
				set(float64(a.Metrics.Usage.Total))
			}
		case "actual_cost_micros":
			if a.Metrics.Cost != nil {
				set(float64(a.Metrics.Cost.Amount))
			}
		case "quality":
			if a.Runtime.Status != "SUCCEEDED" || a.Business.Validation != "ACCEPTED" || a.Business.Outcome == "INSUFFICIENT_EVIDENCE" {
				set(0)
			} else if a.Business.Output != nil {
				output, _ := asset.ParseJSON([]byte(*a.Business.Output))
				c := in.Attempt.Request.Case.Case
				if a.Business.Slice == "STATUS" && c.ExpectedOutput.String() != "null" {
					set(0)
					if output.String() == c.ExpectedOutput.String() {
						set(1)
					}
				} else if a.Business.Slice == "DIAGNOSIS" {
					var gt map[string]any
					var report map[string]any
					if c.GroundTruth.Decode(&gt) == nil && gt["root_cause_hypothesis"] != nil {
						_ = output.Decode(&report)
						set(0)
						if report["root_cause_hypothesis"] == gt["root_cause_hypothesis"] {
							set(1)
						}
					}
				}
			}
		}
	}
	if score != nil {
		v.Score = score
		v.Verdict = "PASS"
		v.SourceError = ""
		v.Reason = "ADAPTIVE_METRIC_OBSERVED"
		if config.Metric == "quality" && *score == 0 {
			v.Verdict = "FAIL"
		}
	}
	if score == nil && a != nil {
		switch config.Metric {
		case "quality":
			v.SourceError = "ADAPTIVE_GROUND_TRUTH_MISSING"
		case "provider_tokens":
			v.SourceError = "PROVIDER_USAGE_NOT_AVAILABLE"
		case "actual_cost_micros":
			v.SourceError = "ACTUAL_COST_NOT_AVAILABLE"
		}
		v.Reason = v.SourceError
	}
	v.SourceVerdict = v.Verdict
	provenance := map[string]any{"metric": config.Metric, "applicability": func() asset.ApplicabilityState {
		if score == nil {
			return asset.MissingEvidence
		}
		return asset.Applicable
	}(), "sampling_unit": "EXECUTION_ATTEMPT", "actual_provider_cost": nil, "task_completion_denominator": "ALL_PLANNED_ATTEMPTS"}
	if config.Metric == "quality" {
		decision := TaskInconclusive
		if score != nil {
			decision = TaskFailure
			if *score == 1 {
				decision = TaskSuccess
			}
		}
		provenance["task_success"] = TaskJudgment{Version: "task_success.v1", Decision: decision, Method: "DETERMINISTIC", Reason: "冻结 Ground Truth 与原业务 Validator；不足不计完整成功", ProducerRef: AdaptiveImplementation}
	}
	v.Provenance, _ = asset.Freeze(provenance)
	return worker.EvaluationOutput{Value: v}, nil
}

// Nearest-rank 只消费已知的本地观察耗时；全体 Attempt 的缺失/状态计数另行保留。
func SummarizeAdaptiveLatency(attempts []ev.Attempt) ev.ObservedLatencySummary {
	return ev.SummarizeObservedLatency(attempts)
}
