"""从 WP1/WP3 冻结事实构建 WP5 只读指标报告。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from app.core.evaluation.comparison import (
    AttemptAvailability, CaseMembership, ComparisonCompatibility, EvaluationRunComparison,
    RegressionClassification,
)
from app.core.evaluation.platform_metrics import (
    MetricAggregateV1, MetricDefinitionV1, MetricDirection, MetricFamily, MetricMaturity,
    MetricObservation, MetricReportV1, MetricState, MetricValueType, PairedMetricAggregateV1,
    aggregate_metric, canonical_digest, validate_definitions,
)
from app.core.evaluation.immutable import json_compatible
from app.core.evaluation.process_trajectory import (
    ProcessKind, parse_process_requirements, parse_process_trajectory, validate_process_requirements,
)
from app.core.evaluation.results import EvaluationResult, EvaluationVerdict
from app.core.evaluation.run_attempts import ExecutionAttempt
from app.services.evaluation.persistence import EvaluationPersistenceService


_OUTCOME_METRICS = {
    "terminalization_rate": set(AttemptAvailability) - {AttemptAvailability.MISSING},
    "successful_completion_rate": {AttemptAvailability.SUCCESS},
    "timeout_rate": {AttemptAvailability.TIMEOUT},
    "cancelled_rate": {AttemptAvailability.CANCELLED},
    "outcome_unknown_rate": {AttemptAvailability.OUTCOME_UNKNOWN},
}
_HEALTH_METRICS = {
    "evaluator_error_rate": EvaluationVerdict.ERROR,
    "evaluator_inconclusive_rate": EvaluationVerdict.INCONCLUSIVE,
    "missing_result_rate": None,
}
_CONDITIONAL_REASONS = {
    "CASE_VERSION_LABEL_DRIFT", "TARGET_BINDING_DRIFT", "SUBJECT_BINDING_DRIFT",
    "MODEL_REVISION_UNVERIFIABLE",
}


def _definition(
    metric_id: str, family: MetricFamily, value_type: MetricValueType, aggregation: str,
    *, grain: str, unit: str, parameters: tuple[tuple[str, str], ...] = (),
    source: str = "WP1", direction: MetricDirection = MetricDirection.NONE,
    maturity: MetricMaturity = MetricMaturity.IMPLEMENTABLE_NOW,
    metric_version: str = "1",
) -> MetricDefinitionV1:
    return MetricDefinitionV1(
        metric_id=metric_id, metric_version=metric_version, family=family,
        input_contract="stage11.wp1.v1+stage11.wp3.v1", value_type=value_type,
        direction=direction, unit=unit, value_range=(0.0, 1.0) if value_type is MetricValueType.RATE else None,
        grain=grain, parameters=parameters, eligibility_rule="frozen expected slots",
        denominator_rule="all eligible expected grains" if aggregation == "RATE" else "valid observations",
        missing_rule="retain null and exclusive reason", aggregation=aggregation,
        required_source=source, maturity=maturity,
    )


def _quality_definition(spec: Any, *, source_variant: str | None = None) -> MetricDefinitionV1:
    config = spec.get("config_snapshot") or {}
    metric = str(config.get("metric") or spec["evaluator_id"])
    k = config.get("k")
    namespace = "document" if metric.startswith("document_") else "chunk"
    rank_layer = "ranked_preferred_retrieved_fallback" if "mrr" in metric else "ranked" if "ndcg" in metric else "retrieved_or_fused"
    parameters = (
        ("evaluator_id", str(spec["evaluator_id"])),
        ("evaluator_version", str(spec["evaluator_version"])),
        ("evaluator_digest", str(spec.get("definition_digest") or spec)),
        ("metric", metric), ("k", str(k) if k is not None else "none"),
        ("identity_namespace", namespace), ("ground_truth_namespace", namespace),
        ("rank_layer", rank_layer), ("retrieval_strategy", str(config.get("retrieval_strategy", "frozen_spec"))),
        ("relevance_semantics", "graded_2^rel-1" if "ndcg" in metric else "positive_relevance"),
        ("source_variant", source_variant or "fixed"),
    )
    return _definition(
        str(spec["evaluator_id"]) + (f"_{source_variant}" if source_variant else ""),
        MetricFamily.AGENT_QUALITY, MetricValueType.SCALAR, "MEAN", grain="MACRO_CASE",
        unit="ratio", parameters=parameters, source="WP1_RESULT+WP4_EVIDENCE",
        direction=MetricDirection(str(spec.get("score_direction", "NONE"))),
        metric_version=str(spec["evaluator_version"]),
    )


def _specs(reference: Any) -> dict[str, Any]:
    return {str(item["evaluator_id"]): item for item in reference.evaluator_manifest}


def _retrieval_variant(result: EvaluationResult, attempt: ExecutionAttempt | None, metric: str) -> str | None:
    """仅从 WP4 sealed evidence 或原有 RAG artifact 的安全事实取排名来源。"""
    if not any(name in metric for name in ("recall", "mrr", "ndcg")):
        return None
    strategy = None
    found = False
    if attempt is not None:
        refs = [ref for ref in attempt.outcome_evidence_refs if ref.kind == "process_trajectory"]
        if len(refs) == 1:
            try:
                trajectory = parse_process_trajectory(refs[0])
                retrievals = [record.payload for record in trajectory.records if record.kind is ProcessKind.RETRIEVAL]
                if len(retrievals) == 1:
                    found = True
                    strategy = retrievals[0].retrieval_strategy
            except (TypeError, ValueError):
                pass
    if strategy is None:
        refs = [ref for ref in result.evidence_refs if ref.kind == "rag_evaluation_artifact"]
        if len(refs) == 1:
            payload = refs[0].metadata.get("payload")
            if payload is not None:
                found = True
                strategy = payload.get("retrieval_strategy")
    if not found:
        return None
    strategy = strategy or "BASELINE"
    layer = "ranked" if "ndcg" in metric else "ranked_preferred" if "mrr" in metric else (
        "fused_ranked" if strategy == "HYBRID_RRF" else "retrieved"
    )
    if "mrr" in metric:
        source = result.metadata.get("mrr_source")
        if not source:
            return None
        layer = str(source)
    return f"{strategy}:{layer}"


def _side(
    comparison: EvaluationRunComparison, side: str, attempts: tuple[ExecutionAttempt, ...],
    results: tuple[EvaluationResult, ...],
) -> tuple[dict[str, MetricAggregateV1], dict[str, dict[str, MetricObservation]]]:
    reference = comparison.baseline_reference if side == "baseline" else comparison.candidate_reference
    if reference is None:
        raise ValueError("WP5 requires WP3 frozen run reference")
    cases = tuple(str(item["case_id"]) for item in reference.case_manifest)
    specs = _specs(reference)
    latest: dict[str, ExecutionAttempt] = {}
    attempt_counts: Counter[str] = Counter()
    for attempt in attempts:
        attempt_counts[attempt.case_ref.case_id] += 1
        previous = latest.get(attempt.case_ref.case_id)
        if previous is None or attempt.attempt_no > previous.attempt_no:
            latest[attempt.case_ref.case_id] = attempt
    result_map = {(item.case_id, item.evaluator_id): item for item in results}
    aggregates: dict[str, MetricAggregateV1] = {}
    observations: dict[str, dict[str, MetricObservation]] = {}

    for name, positive in _OUTCOME_METRICS.items():
        definition = _definition(name, MetricFamily.EXECUTION_RELIABILITY, MetricValueType.RATE, "RATE",
                                 grain="EXECUTION_OBJECT", unit="ratio", parameters=(("positive", ",".join(sorted(positive))),))
        items = tuple(MetricObservation(
            case_id, MetricState.VALID, int(AttemptAvailability(
                latest[case_id].execution_outcome_kind.value if case_id in latest and latest[case_id].execution_outcome_kind
                else "MISSING") in positive), observed=case_id in latest,
            source_refs=(str(latest[case_id].attempt_id),) if case_id in latest else (),
        ) for case_id in cases)
        aggregates[name] = aggregate_metric(definition, items, scope=side)
        observations[name] = {item.key: item for item in items}

    name = "execution_attempt_retry_rate"
    definition = _definition(name, MetricFamily.EXECUTION_RELIABILITY, MetricValueType.RATE, "RATE",
                             grain="EXECUTION_OBJECT", unit="ratio", parameters=(("retry", "WP1 attempt count > 1"),))
    items = tuple(MetricObservation(case_id, MetricState.VALID, int(attempt_counts[case_id] > 1),
                                    observed=bool(attempt_counts[case_id])) for case_id in cases)
    aggregates[name] = aggregate_metric(definition, items, scope=side)
    observations[name] = {item.key: item for item in items}

    for name, verdict in _HEALTH_METRICS.items():
        definition = _definition(name, MetricFamily.EVALUATION_HEALTH, MetricValueType.RATE, "RATE",
                                 grain="EVALUATOR_SLOT", unit="ratio", parameters=(("positive", str(verdict)),))
        items = tuple(MetricObservation(
            f"{case_id}:{evaluator_id}", MetricState.VALID,
            int((result_map.get((case_id, evaluator_id)) is None) if verdict is None
                else (result_map.get((case_id, evaluator_id)) is not None
                      and result_map[(case_id, evaluator_id)].verdict is verdict)),
            observed=(case_id, evaluator_id) in result_map,
            source_refs=(result_map[(case_id, evaluator_id)].result_id,) if (case_id, evaluator_id) in result_map else (),
        ) for case_id in cases for evaluator_id in specs)
        aggregates[name] = aggregate_metric(definition, items, scope=side)
        observations[name] = {item.key: item for item in items}

    name = "required_evidence_availability_rate"
    definition = _definition(name, MetricFamily.EVALUATION_HEALTH, MetricValueType.RATE, "RATE",
                             grain="EVALUATOR_SLOT", unit="ratio", source="WP1_RESULT+WP4_EVIDENCE")
    evidence_items = []
    for case_id in cases:
        for evaluator_id, spec in specs.items():
            required = tuple(spec.get("required_evidence_kinds") or ())
            key = f"{case_id}:{evaluator_id}"
            if not required:
                evidence_items.append(MetricObservation(key, MetricState.NOT_APPLICABLE))
                continue
            result = result_map.get((case_id, evaluator_id))
            attempt = latest.get(case_id)
            satisfied = result is not None and all(
                any(ref.kind == kind for ref in result.evidence_refs) for kind in required
            )
            if satisfied and "process_trajectory" in required:
                thin = [ref for ref in result.evidence_refs if ref.kind == "process_trajectory"]
                durable = [ref for ref in attempt.outcome_evidence_refs if ref.kind == "process_trajectory"] if attempt else []
                try:
                    if len(thin) != 1 or len(durable) != 1 or thin[0].metadata.get("content_sha256") != durable[0].metadata.get("content_sha256"):
                        satisfied = False
                    else:
                        trajectory = parse_process_trajectory(durable[0])
                        config = spec.get("config_snapshot") or {}
                        requirements = parse_process_requirements(config["process_evidence_requirements"])
                        satisfied = (
                            validate_process_requirements(trajectory, requirements) is None
                            and thin[0].identifier == durable[0].identifier == trajectory.trajectory_id
                            and thin[0].schema_version == durable[0].schema_version == "stage11.wp4.v1"
                            and thin[0].metadata.get("policy_ref") == trajectory.body_policy_ref
                            and trajectory.evaluation_attempt_id == str(attempt.attempt_id)
                            and trajectory.evaluation_run_id == str(reference.run_id)
                            and trajectory.project_id == str(reference.project_id)
                            and trajectory.dataset_id == str(reference.dataset_snapshot["dataset_id"])
                            and trajectory.case_ref.case_id == result.case_id == attempt.case_ref.case_id
                            and trajectory.case_ref.version == result.case_version == attempt.case_ref.version
                            and trajectory.execution_request_id == result.execution_request_id == attempt.execution_request.request_id
                        )
                except (KeyError, TypeError, ValueError):
                    satisfied = False
            evidence_items.append(MetricObservation(key, MetricState.VALID, int(satisfied),
                                                    observed=result is not None,
                                                    source_refs=(result.result_id,) if result else ()))
    aggregates[name] = aggregate_metric(definition, tuple(evidence_items), scope=side)
    observations[name] = {item.key: item for item in evidence_items}
    evidence_by_key = observations[name]

    for evaluator_id, spec in specs.items():
        metric = str((spec.get("config_snapshot") or {}).get("metric") or evaluator_id)
        retrieval = any(name in metric for name in ("recall", "mrr", "ndcg"))
        variants = {_retrieval_variant(result, latest.get(case_id), metric)
                    for (case_id, item_id), result in result_map.items()
                    if item_id == evaluator_id and case_id in cases} if retrieval else set()
        source_variants = tuple(sorted(value for value in variants if value is not None)) if retrieval else (None,)
        if not source_variants:
            source_variants = ("UNAVAILABLE_SOURCE",) if retrieval else (None,)
        for source_variant in source_variants:
            definition = _quality_definition(spec, source_variant=source_variant)
            items: list[MetricObservation] = []
            for case_id in cases:
                result = result_map.get((case_id, evaluator_id))
                key = f"{case_id}:{evaluator_id}"
                if result is None:
                    item = MetricObservation(key, MetricState.MISSING)
                elif (retrieval and source_variant != "UNAVAILABLE_SOURCE"
                      and _retrieval_variant(result, latest.get(case_id), metric) != source_variant):
                    item = MetricObservation(key, MetricState.NOT_APPLICABLE, observed=True)
                elif result.verdict is EvaluationVerdict.ERROR:
                    item = MetricObservation(key, MetricState.ERROR, observed=True, source_refs=(result.result_id,))
                elif result.verdict is EvaluationVerdict.INCONCLUSIVE:
                    item = MetricObservation(key, MetricState.INCONCLUSIVE, observed=True, source_refs=(result.result_id,))
                elif (spec.get("required_evidence_kinds") and
                      evidence_by_key[key].value != 1):
                    item = MetricObservation(key, MetricState.UNAVAILABLE, observed=True, source_refs=(result.result_id,))
                elif (result.score is None or source_variant == "UNAVAILABLE_SOURCE"
                      or case_id not in latest or result.attempt_id != str(latest[case_id].attempt_id)):
                    item = MetricObservation(key, MetricState.UNAVAILABLE, observed=True, source_refs=(result.result_id,))
                elif latest[case_id].execution_outcome_kind and latest[case_id].execution_outcome_kind.value == "OUTCOME_UNKNOWN":
                    item = MetricObservation(key, MetricState.UNKNOWN, observed=True, source_refs=(result.result_id,))
                else:
                    item = MetricObservation(key, MetricState.VALID, result.score, observed=True,
                                             source_refs=(result.result_id,))
                items.append(item)
            aggregates[definition.metric_id] = aggregate_metric(definition, tuple(items), scope=side)
            observations[definition.metric_id] = {item.key: item for item in items}

    items = []
    for case_id in cases:
        attempt = latest.get(case_id)
        if attempt and attempt.started_at and attempt.finished_at and attempt.finished_at >= attempt.started_at:
            items.append(MetricObservation(case_id, MetricState.VALID,
                                           (attempt.finished_at - attempt.started_at).total_seconds() * 1000,
                                           observed=True, source_refs=(str(attempt.attempt_id),)))
        else:
            items.append(MetricObservation(case_id, MetricState.UNAVAILABLE, observed=attempt is not None))
    for percentile in ("P50", "P95", "P99"):
        name = f"e2e_attempt_wall_ms_{percentile.lower()}"
        definition = _definition(name, MetricFamily.PERFORMANCE, MetricValueType.DURATION, percentile,
                                 grain="EXECUTION_OBJECT", unit="ms", source="WP1_ATTEMPT",
                                 direction=MetricDirection.LOWER_IS_BETTER,
                                 parameters=(("percentile_method", "nearest-rank"),))
        aggregates[name] = aggregate_metric(definition, tuple(items), scope=side)
        observations[name] = {item.key: item for item in items}
    return aggregates, observations


def _paired(
    comparison: EvaluationRunComparison, baseline: dict[str, MetricAggregateV1],
    candidate: dict[str, MetricAggregateV1], baseline_items: dict[str, dict[str, MetricObservation]],
    candidate_items: dict[str, dict[str, MetricObservation]],
) -> tuple[PairedMetricAggregateV1, ...]:
    paired: list[PairedMetricAggregateV1] = []
    def relevant_rows(definition: MetricDefinitionV1):
        evaluator = dict(definition.parameters).get("evaluator_id")
        if evaluator is not None:
            return tuple(row for row in comparison.comparisons if row.evaluator_id == evaluator)
        if definition.grain == "EVALUATOR_SLOT":
            return comparison.comparisons
        first_by_case = {}
        for row in comparison.comparisons:
            if row.case_id not in first_by_case or row.evaluator_id < first_by_case[row.case_id].evaluator_id:
                first_by_case[row.case_id] = row
        return tuple(first_by_case[case_id] for case_id in sorted(first_by_case))

    for metric_id in sorted(set(baseline) | set(candidate)):
        if metric_id not in baseline or metric_id not in candidate:
            item = baseline.get(metric_id) or candidate[metric_id]
            count = len(relevant_rows(item.definition))
            paired.append(PairedMetricAggregateV1(
                item.definition, None, None, None, count, 0,
                (("METRIC_NOT_COMPARABLE", count),),
            ))
            continue
        left, right = baseline[metric_id], candidate[metric_id]
        if left.definition.metric_definition_digest != right.definition.metric_definition_digest:
            count = len(relevant_rows(left.definition))
            paired.append(PairedMetricAggregateV1(
                left.definition, None, None, None, count, 0,
                (("METRIC_NOT_COMPARABLE", count),),
            ))
            continue
        selected: list[tuple[float, float]] = []
        excluded: Counter[str] = Counter()
        for row in relevant_rows(left.definition):
            key = row.case_id if left.definition.grain == "EXECUTION_OBJECT" else f"{row.case_id}:{row.evaluator_id}"
            if metric_id not in baseline_items or key not in baseline_items[metric_id] or key not in candidate_items[metric_id]:
                excluded["NEW_OR_REMOVED"] += 1
                continue
            accepted = row.compatibility is ComparisonCompatibility.COMPARABLE or (
                row.compatibility is ComparisonCompatibility.CONDITIONALLY_COMPARABLE
                and (set(row.reason_codes) & _CONDITIONAL_REASONS).issubset(
                    set(comparison.accepted_conditional_reasons))
            )
            if row.case_membership is not CaseMembership.BOTH or not row.baseline_expected or not row.candidate_expected:
                excluded["NEW_OR_REMOVED"] += 1
            elif not accepted or row.classification is RegressionClassification.NOT_COMPARABLE:
                excluded["NOT_COMPARABLE"] += 1
            else:
                b, c = baseline_items[metric_id][key], candidate_items[metric_id][key]
                if b.state is MetricState.VALID and c.state is MetricState.VALID:
                    selected.append((float(b.value), float(c.value)))
                else:
                    excluded["SINGLE_SIDE_UNAVAILABLE"] += 1
        n = len(selected)
        b_mean = sum(item[0] for item in selected) / n if n else None
        c_mean = sum(item[1] for item in selected) / n if n else None
        paired.append(PairedMetricAggregateV1(
            left.definition, b_mean, c_mean, c_mean - b_mean if n else None,
            n + sum(excluded.values()), n, tuple(sorted(excluded.items())),
            sum(item[0] for item in selected) if n else None,
            sum(item[1] for item in selected) if n else None,
        ))
    return tuple(paired)


def build_metric_report(
    comparison: EvaluationRunComparison, baseline_attempts: tuple[ExecutionAttempt, ...],
    candidate_attempts: tuple[ExecutionAttempt, ...], baseline_results: tuple[EvaluationResult, ...],
    candidate_results: tuple[EvaluationResult, ...], *, computed_at: datetime | None = None,
) -> MetricReportV1:
    """消费已冻结的 WP3 比较与 WP1 原始事实，不写回任何 authority。"""
    baseline, baseline_items = _side(comparison, "baseline", baseline_attempts, baseline_results)
    candidate, candidate_items = _side(comparison, "candidate", candidate_attempts, candidate_results)
    validate_definitions(tuple(item.definition for item in (*baseline.values(), *candidate.values())))
    paired = _paired(comparison, baseline, candidate, baseline_items, candidate_items)
    refs = tuple(sorted(
        {str(item.attempt_id) for item in (*baseline_attempts, *candidate_attempts)}
        | {item.result_id for item in (*baseline_results, *candidate_results)}
        | {str(ref.metadata.get("content_sha256")) for item in (*baseline_attempts, *candidate_attempts)
           for ref in item.outcome_evidence_refs if ref.kind == "process_trajectory"
           and ref.metadata.get("content_sha256")}
        | {canonical_digest(json_compatible(reference.suite_snapshot)) for reference in
           (comparison.baseline_reference, comparison.candidate_reference) if reference is not None}
    ))
    return MetricReportV1(
        project_id=str(comparison.project_id), baseline_run_id=str(comparison.baseline_run_id),
        candidate_run_id=str(comparison.candidate_run_id),
        comparison_contract_version=comparison.comparison_contract_version,
        comparison_digest=comparison.semantic_digest, source_refs=refs,
        baseline=tuple(baseline[name] for name in sorted(baseline)),
        candidate=tuple(candidate[name] for name in sorted(candidate)), paired=paired,
        computed_at=computed_at or datetime.now(UTC),
    )


class PlatformMetricService:
    """从持久化事实读取 WP5 报告。"""

    def __init__(self, persistence: EvaluationPersistenceService) -> None:
        self._persistence = persistence

    async def build_report(self, comparison: EvaluationRunComparison) -> MetricReportV1:
        """只读加载两个 Run 的完整 Attempt 历史与 Result。"""
        project_id = comparison.project_id
        baseline_id, candidate_id = comparison.baseline_run_id, comparison.candidate_run_id
        return build_metric_report(
            comparison,
            await self._persistence.list_attempts(project_id, baseline_id),
            await self._persistence.list_attempts(project_id, candidate_id),
            await self._persistence.list_results(project_id, baseline_id),
            await self._persistence.list_results(project_id, candidate_id),
        )
