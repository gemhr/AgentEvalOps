package metric

import "agentevalops/go-backend/internal/asset"

// Builtin 是可发布的定义 fixture。所有 producer 均延后 G4+，不注册可执行 evaluator。
type Builtin struct {
	Key, Version string
	Definition   Definition
}

func Builtins() []Builtin {
	requirement := func(kind, schema string, body bool) asset.EvidenceRequirement {
		return asset.EvidenceRequirement{Kind: kind, Schema: schema, BodyRequired: body}
	}
	base := func(name string, grain Grain, value ValueType, unit string, direction Direction, evidence ...asset.EvidenceRequirement) Definition {
		return Definition{Name: name, Grain: grain, ValueType: value, Unit: unit, Direction: direction,
			Denominator:   "eligible_decidable_grain; disclose eligible_total and coverage",
			Missing:       "MISSING_EVIDENCE/INCONCLUSIVE/UNKNOWN separate; never impute 0 or 1",
			Aggregation:   "valid observations only; report coverage; N/A excluded explicitly",
			Comparison:    "same definition/evidence/subject version; otherwise incomparable",
			Applicability: asset.Applicability{RequiredEvidence: evidence, RuleRef: "eligible-evidence.v1"}, Availability: asset.ContractOnly}
	}
	task := base("Task Success", Case, Enum, "decision", None, requirement("task_goal", "task-goal.v1", true), requirement("acceptance_criteria", "acceptance.v1", true), requirement("actual_output", "artifact.v1", true), requirement("execution_outcome", "execution-outcome.v1", false))
	task.Labels = []string{"SUCCESS", "FAILURE", "INCONCLUSIVE", "NOT_APPLICABLE"}
	task.Aggregation = "success / eligible_decidable; decision_coverage=decidable/eligible_total; UNKNOWN counted separately"
	execution := base("Execution Success", Case, Boolean, "decision", Higher, requirement("confirmed_execution_outcome", "execution-outcome.v1", false))
	execution.Applicability.RuleRef = "actual-target-execution-only.v1"
	execution.Denominator = "executed case-run population; StoredObservation excluded; UNKNOWN remains explicit"
	coverage := base("Evaluation Coverage", Run, Rate, "ratio", Higher, requirement("expected_manifest", "run-input.v1", false), requirement("metric_observations", "metric-observation.v1", false))
	coverage.Range = &[2]float64{0, 1}
	coverage.Denominator = "expected eligible metric grain"
	coverage.Aggregation = "valid_count / eligible_count; disclose all exclusive missing states"
	result := []Builtin{{"task_success", "v1", task}, {"execution_success", "v1", execution}, {"evaluation_coverage", "v1", coverage}}
	for _, key := range []string{"recall@k", "mrr", "ndcg"} {
		d := base(key, Retrieval, Scalar, "ratio", Higher, requirement("ranked_retrieval", "ranked-retrieval.v1", true), requirement("relevance_ground_truth", "relevance.v1", true))
		d.Range = &[2]float64{0, 1}
		d.Denominator = "declared relevant identity namespace; absent required GT is MISSING_EVIDENCE"
		d.Aggregation = "per-case valid ranking observations; mean with coverage"
		switch key {
		case "recall@k":
			d.Denominator = "relevant identity count; declared zero-relevant population is NOT_APPLICABLE"
			d.Comparison = "same k, namespace, GT digest; duplicates occupy cutoff positions"
			d.Parameters, _ = asset.ParseJSON([]byte(`{"k":5,"rank_source":"actual_retrieval_or_fused_rank"}`))
		case "mrr":
			d.Comparison = "actual rank source; first relevant reciprocal rank; no evidence-derived reranking"
		case "ndcg":
			d.Comparison = "gain=2^relevance-1; log2(rank+1); IDCG=0 yields 0; same k, GT and ranking source"
			d.Parameters, _ = asset.ParseJSON([]byte(`{"k":5}`))
		}
		result = append(result, Builtin{key, "v1", d})
	}
	correctness := base("Answer Correctness", Case, Scalar, "score", Higher, requirement("actual_output", "artifact.v1", true), requirement("reference_or_rubric", "answer-reference.v1", true))
	correctness.Range = &[2]float64{0, 1}
	grounded := base("Answer Groundedness", Case, Scalar, "score", Higher, requirement("actual_output", "artifact.v1", true), requirement("retrieved_context", "context-body.v1", true))
	grounded.Range = &[2]float64{0, 1}
	result = append(result, Builtin{"answer_correctness", "v1", correctness}, Builtin{"answer_groundedness", "v1", grounded})
	for _, item := range []struct {
		key          string
		grain        Grain
		kind, schema string
	}{
		{"plan_quality", Plan, "structured_plan", "plan.v1"}, {"plan_adherence", Plan, "plan_and_steps", "plan-steps.v1"},
		{"tool_correctness", ToolCall, "actual_and_expected_tool_calls", "tool-calls.v1"}, {"tool_argument_correctness", ToolCall, "tool_arguments_and_policy", "tool-arguments.v1"},
		{"step_efficiency", Step, "ordered_complete_trajectory", "trajectory.v1"},
	} {
		d := base(item.key, item.grain, Scalar, "score", Higher, requirement(item.kind, item.schema, true))
		d.Range = &[2]float64{0, 1}
		d.Availability = asset.Unsupported
		d.Applicability.RuleRef = "complete-typed-evidence-only; strict-trace-v1-unsupported"
		result = append(result, Builtin{item.key, "v1", d})
	}
	latency := base("Latency", Case, Duration, "millisecond", Lower, requirement("execution_clock_boundaries", "clock.v1", false))
	latency.Aggregation = "mean or nearest-rank percentile of valid observations with coverage"
	tokens := base("Token Usage", Case, Count, "token", Lower, requirement("actual_token_usage", "usage.v1", false))
	cost := base("Cost", Case, Cost, "declared_currency", Lower, requirement("actual_cost_receipt", "cost.v1", false))
	cost.Missing = "unknown cost is null; do not estimate zero; judge and execution separated"
	return append(result, Builtin{"latency", "v1", latency}, Builtin{"token_usage", "v1", tokens}, Builtin{"cost", "v1", cost})
}
