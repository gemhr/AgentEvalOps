"""WP6-E episodic metrics（denominator 只使用 evaluable assertions；禁止 fake 数值）。

- ``evaluable_denominator = PASS + FAIL``；BLOCKED / NOT_APPLICABLE 不进 denominator。
- 无 evaluable unit 时 ``value=None``（NOT_EVALUABLE），不显示 0 或 1。
- 0-best 指标（fabricated fact / scope leakage / instruction elevation）用
  ``build_failure_rate_aggregate``（value = failed / (passed + failed)）。
- Formation Precision / Recall 按 TP/FP/FN 显式分类；Trivial Rejection Rate 是
  显式 rate。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.core.evaluation.episodic_assertion import (
    EpisodicAssertion,
    EpisodicAssertionGroup,
    EpisodicFailureTaxonomy,
    PERSISTED_OBSERVATION_FIDELITY_ASSERTION,
)
from app.core.evaluation.episodic_dataset import EpisodicFormationOutcome
from app.core.evaluation.episodic_evidence import (
    EpisodicRunEvidence,
    EpisodicScenarioEvaluationEvidence,
    RunExecutionStatus,
)
from app.core.evaluation.stateful_assertion import AssertionStatus
from app.core.evaluation.stateful_metrics import (
    EVALUATION_INFRA_FAILURE_RATE_METRIC,
    MetricAggregate,
    RatioMetric,
    RUNTIME_BLOCK_RATE_METRIC,
    build_evaluation_infra_failure_rate,
    build_failure_rate_aggregate,
    build_metric_aggregate,
    build_runtime_block_rate,
)

FORMATION_PRECISION_METRIC: Final[str] = "episode_formation_precision"
FORMATION_RECALL_METRIC: Final[str] = "episode_formation_recall"
TRIVIAL_REJECTION_RATE_METRIC: Final[str] = "trivial_run_rejection_rate"
GROUNDING_ACCURACY_METRIC: Final[str] = "episode_grounding_accuracy"
FABRICATED_FACT_RATE_METRIC: Final[str] = "fabricated_runtime_fact_rate"
EXPECTED_EPISODE_RECALL_AT_K_METRIC: Final[str] = "expected_episode_recall_at_k"
HIT_AT_K_METRIC: Final[str] = "hit_at_k"
IRRELEVANT_EPISODE_SELECTION_RATE_METRIC: Final[str] = "irrelevant_episode_selection_rate"
EPISODE_INJECTION_SUCCESS_RATE_METRIC: Final[str] = "episode_injection_success_rate"
EPISODE_SCOPE_LEAKAGE_RATE_METRIC: Final[str] = "episode_scope_leakage_rate"
INSTRUCTION_ELEVATION_VIOLATION_RATE_METRIC: Final[str] = "instruction_elevation_violation_rate"
STATEFUL_EPISODIC_SCENARIO_SUCCESS_RATE_METRIC: Final[str] = "stateful_episodic_scenario_success_rate"

#: 0-best failure-rate metrics：denominator 只使用 evaluable applicable assertion。
_ZERO_BEST_RATE_METRICS: Final[dict[str, EpisodicAssertionGroup]] = {
    FABRICATED_FACT_RATE_METRIC: EpisodicAssertionGroup.EVIDENCE_GROUNDING,
    IRRELEVANT_EPISODE_SELECTION_RATE_METRIC: EpisodicAssertionGroup.RETRIEVAL,
    EPISODE_SCOPE_LEAKAGE_RATE_METRIC: EpisodicAssertionGroup.SCOPE_ISOLATION,
    INSTRUCTION_ELEVATION_VIOLATION_RATE_METRIC: EpisodicAssertionGroup.TRUST_BOUNDARY,
}


def _classification_counts(
    evidence: EpisodicScenarioEvaluationEvidence,
) -> tuple[int, int, int]:
    """Formation Precision/Recall 的 TP / FP / FN 计数。

    - expected CREATED + actual CREATED -> TP；
    - expected CREATED + actual 非 CREATED -> FN（EPISODE_FORMATION_FALSE_NEGATIVE）；
    - expected 非 CREATED（SKIPPED）+ actual CREATED -> FP（EPISODE_FORMATION_FALSE_POSITIVE）。
    """
    tp = 0
    fp = 0
    fn = 0
    for run_evidence in evidence.run_evidence_by_dataset_run_id.values():
        if run_evidence.execution_status is not RunExecutionStatus.EXECUTED:
            continue
        expected_created = False
        for run in evidence.scenario.runs:
            if run.run_id != run_evidence.dataset_run_id or run.expected_formation is None:
                continue
            expected_created = run.expected_formation.expected_formation_outcome is EpisodicFormationOutcome.CREATED
        actual_created = (
            run_evidence.formation_receipt is not None
            and run_evidence.formation_receipt.outcome == EpisodicFormationOutcome.CREATED.value
        ) or (
            run_evidence.runtime_receipt is not None
            and run_evidence.runtime_receipt.formation_outcome == EpisodicFormationOutcome.CREATED.value
        )
        if expected_created and actual_created:
            tp += 1
        elif expected_created and not actual_created:
            fn += 1
        elif not expected_created and actual_created:
            fp += 1
    return tp, fp, fn


def _ratio(metric_name: str, numerator: int, denominator: int) -> RatioMetric:
    if denominator <= 0:
        return RatioMetric(metric_name, 0, 0, 0, None)
    return RatioMetric(metric_name, numerator, denominator, 0, numerator / denominator)


def _required_applicable(assertions: list[EpisodicAssertion]) -> list[EpisodicAssertion]:
    from app.core.evaluation.episodic_assertion import required_assertions
    from app.core.evaluation.stateful_assertion import AssertionStatus

    return [item for item in required_assertions(assertions) if item.status is not AssertionStatus.NOT_APPLICABLE]


def build_episodic_runtime_block_rate(assertions: list[EpisodicAssertion]) -> RatioMetric:
    """Runtime Block Rate = runtime_blocked_required / required_applicable（显式 rate）。"""
    from app.core.evaluation.episodic_assertion import EpisodicBlockReason
    from app.core.evaluation.stateful_assertion import AssertionStatus

    required = _required_applicable(assertions)
    runtime_blocked = sum(
        item.status is AssertionStatus.BLOCKED and item.blocked_by is EpisodicBlockReason.RUNTIME_BLOCKED
        for item in required
    )
    return _ratio(RUNTIME_BLOCK_RATE_METRIC, runtime_blocked, len(required))


def build_episodic_evaluation_infra_failure_rate(assertions: list[EpisodicAssertion]) -> RatioMetric:
    """Evaluation Infrastructure Failure Rate = infra_blocked / required_applicable。

    ``EVIDENCE_CAPTURE`` 与 ``EVALUATION_INFRA`` 计入 numerator；
    ``EXPECTED_EVIDENCE_LIMITATION``（Layer2 accepted limitation）不计入。
    """
    from app.core.evaluation.episodic_assertion import EpisodicBlockReason
    from app.core.evaluation.stateful_assertion import AssertionStatus

    required = _required_applicable(assertions)
    infra_blocked = sum(
        item.status is AssertionStatus.BLOCKED
        and item.blocked_by in {EpisodicBlockReason.EVIDENCE_CAPTURE, EpisodicBlockReason.EVALUATION_INFRA}
        for item in required
    )
    return _ratio(EVALUATION_INFRA_FAILURE_RATE_METRIC, infra_blocked, len(required))


def build_episodic_scenario_metrics(
    evidence: EpisodicScenarioEvaluationEvidence,
    assertions: list[EpisodicAssertion],
) -> dict[str, MetricAggregate | RatioMetric]:
    """构建一个 scenario 的 episodic metrics（含 runtime block / eval infra rate）。"""
    metrics: dict[str, MetricAggregate | RatioMetric] = {}

    tp, fp, fn = _classification_counts(evidence)
    metrics[FORMATION_PRECISION_METRIC] = _ratio(FORMATION_PRECISION_METRIC, tp, tp + fp)
    metrics[FORMATION_RECALL_METRIC] = _ratio(FORMATION_RECALL_METRIC, tp, tp + fn)

    # Trivial Rejection Rate：E03 trivial run 被正确拒绝的比率（0-best? 否，越高越好）。
    trivial = [
        run
        for run in evidence.scenario.runs
        if run.expected_eligibility is not None and not run.expected_eligibility.eligible
    ]
    trivial_rejected = 0
    for run in trivial:
        run_evidence = evidence.run_evidence_by_dataset_run_id.get(run.run_id)
        if run_evidence is None:
            continue
        actual_created = (
            run_evidence.formation_receipt is not None
            and run_evidence.formation_receipt.outcome == EpisodicFormationOutcome.CREATED.value
        ) or (
            run_evidence.runtime_receipt is not None
            and run_evidence.runtime_receipt.formation_outcome == EpisodicFormationOutcome.CREATED.value
        )
        if not actual_created:
            trivial_rejected += 1
    metrics[TRIVIAL_REJECTION_RATE_METRIC] = _ratio(TRIVIAL_REJECTION_RATE_METRIC, trivial_rejected, len(trivial))

    grounding_assertions = [item for item in assertions if item.group is EpisodicAssertionGroup.EVIDENCE_GROUNDING]
    metrics[GROUNDING_ACCURACY_METRIC] = build_metric_aggregate(GROUNDING_ACCURACY_METRIC, grounding_assertions)
    # Fabricated Runtime Fact Rate = fabricated-fact FAIL / (PASS + FAIL) over persisted
    # fidelity assertions（fabricated fact 只在 fidelity 路径评价；identity assertions
    # 不测试 fabricated fact，不进 denominator）。0 = best。
    fidelity_assertions = [
        item for item in grounding_assertions if item.assertion_id.endswith(PERSISTED_OBSERVATION_FIDELITY_ASSERTION)
    ]
    fabricated_fail = sum(
        item.status is AssertionStatus.FAIL
        and item.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_FABRICATED_FACT
        for item in fidelity_assertions
    )
    grounding_pass = sum(item.status is AssertionStatus.PASS for item in fidelity_assertions)
    grounding_fail = sum(item.status is AssertionStatus.FAIL for item in fidelity_assertions)
    fabricated_denominator = grounding_pass + grounding_fail
    metrics[FABRICATED_FACT_RATE_METRIC] = _ratio(FABRICATED_FACT_RATE_METRIC, fabricated_fail, fabricated_denominator)

    retrieval_assertions = [item for item in assertions if item.group is EpisodicAssertionGroup.RETRIEVAL]
    recall_assertions = [
        item
        for item in retrieval_assertions
        if "identity" in item.assertion_id or "score" in item.assertion_id or "hit" in item.assertion_id
    ]
    metrics[EXPECTED_EPISODE_RECALL_AT_K_METRIC] = build_metric_aggregate(
        EXPECTED_EPISODE_RECALL_AT_K_METRIC, recall_assertions
    )
    hit_assertions = [item for item in retrieval_assertions if "hit" in item.assertion_id]
    metrics[HIT_AT_K_METRIC] = build_metric_aggregate(HIT_AT_K_METRIC, hit_assertions)

    ranking_assertions = [item for item in assertions if item.group is EpisodicAssertionGroup.RANKING]
    metrics[IRRELEVANT_EPISODE_SELECTION_RATE_METRIC] = build_failure_rate_aggregate(
        IRRELEVANT_EPISODE_SELECTION_RATE_METRIC, ranking_assertions
    )

    # Episode Injection Success Rate 只依据 actual ContextBuilder acceptance
    # （context_record_count assertion），绝不用 selected count 代替。
    injection_assertions = [
        item
        for item in assertions
        if item.group is EpisodicAssertionGroup.INJECTION and "context_record_count" in item.assertion_id
    ]
    metrics[EPISODE_INJECTION_SUCCESS_RATE_METRIC] = build_metric_aggregate(
        EPISODE_INJECTION_SUCCESS_RATE_METRIC, injection_assertions
    )

    scope_assertions = [item for item in assertions if item.group is EpisodicAssertionGroup.SCOPE_ISOLATION]
    metrics[EPISODE_SCOPE_LEAKAGE_RATE_METRIC] = build_failure_rate_aggregate(
        EPISODE_SCOPE_LEAKAGE_RATE_METRIC, scope_assertions
    )

    trust_assertions = [item for item in assertions if item.group is EpisodicAssertionGroup.TRUST_BOUNDARY]
    metrics[INSTRUCTION_ELEVATION_VIOLATION_RATE_METRIC] = build_failure_rate_aggregate(
        INSTRUCTION_ELEVATION_VIOLATION_RATE_METRIC, trust_assertions
    )

    metrics[RUNTIME_BLOCK_RATE_METRIC] = build_episodic_runtime_block_rate(assertions)
    metrics[EVALUATION_INFRA_FAILURE_RATE_METRIC] = build_episodic_evaluation_infra_failure_rate(assertions)
    return metrics


def build_episodic_scenario_success_aggregate(
    scenario_outcomes: list[AssertionStatus],
) -> MetricAggregate:
    """Scenario Success Rate = PASS / (PASS + FAIL)；BLOCKED 单独报告，不进 denominator。"""
    passed = sum(item is AssertionStatus.PASS for item in scenario_outcomes)
    failed = sum(item is AssertionStatus.FAIL for item in scenario_outcomes)
    blocked = sum(item is AssertionStatus.BLOCKED for item in scenario_outcomes)
    denominator = passed + failed
    value = passed / denominator if denominator > 0 else None
    return MetricAggregate(
        metric_name=STATEFUL_EPISODIC_SCENARIO_SUCCESS_RATE_METRIC,
        passed=passed,
        failed=failed,
        blocked=blocked,
        not_applicable=0,
        evaluable_denominator=denominator,
        value=value,
    )


def build_episodic_experiment_metrics(
    scenario_metrics: list[dict[str, MetricAggregate | RatioMetric]],
) -> dict[str, MetricAggregate | RatioMetric]:
    """聚合各 scenario metrics 为 experiment-level aggregates。

    对 ``MetricAggregate``：passed/failed/blocked/not_applicable 相加；对
    ``RatioMetric``：把 zero-best rate 的 numerator/denominator 相加后重新计算。
    ``_ZERO_BEST_RATE_METRICS``（0-best failure-rate）重算 value 时使用
    ``failed / (passed + failed)``；其余 accuracy/success metric 使用
    ``passed / (passed + failed)``。
    """
    aggregated: dict[str, MetricAggregate | RatioMetric] = {}
    for scenario in scenario_metrics:
        for name, metric in scenario.items():
            if isinstance(metric, MetricAggregate):
                current = aggregated.get(name)
                if isinstance(current, MetricAggregate):
                    passed = current.passed + metric.passed
                    failed = current.failed + metric.failed
                    blocked = current.blocked + metric.blocked
                    not_applicable = current.not_applicable + metric.not_applicable
                    denominator = passed + failed
                    value = (
                        failed / denominator
                        if denominator > 0 and name in _ZERO_BEST_RATE_METRICS
                        else passed / denominator
                        if denominator > 0
                        else None
                    )
                    aggregated[name] = MetricAggregate(
                        metric_name=name,
                        passed=passed,
                        failed=failed,
                        blocked=blocked,
                        not_applicable=not_applicable,
                        evaluable_denominator=denominator,
                        value=value,
                    )
                else:
                    aggregated[name] = metric
            elif isinstance(metric, RatioMetric):
                current = aggregated.get(name)
                if isinstance(current, RatioMetric):
                    numerator = current.numerator + metric.numerator
                    denominator = current.denominator + metric.denominator
                    not_applicable = current.not_applicable + metric.not_applicable
                    value = numerator / denominator if denominator > 0 else None
                    aggregated[name] = RatioMetric(
                        metric_name=name,
                        numerator=numerator,
                        denominator=denominator,
                        not_applicable=not_applicable,
                        value=value,
                    )
                else:
                    aggregated[name] = metric
    return aggregated


__all__ = [
    "EPISODE_INJECTION_SUCCESS_RATE_METRIC",
    "EPISODE_SCOPE_LEAKAGE_RATE_METRIC",
    "EXPECTED_EPISODE_RECALL_AT_K_METRIC",
    "FABRICATED_FACT_RATE_METRIC",
    "FORMATION_PRECISION_METRIC",
    "FORMATION_RECALL_METRIC",
    "GROUNDING_ACCURACY_METRIC",
    "HIT_AT_K_METRIC",
    "INSTRUCTION_ELEVATION_VIOLATION_RATE_METRIC",
    "STATEFUL_EPISODIC_SCENARIO_SUCCESS_RATE_METRIC",
    "TRIVIAL_REJECTION_RATE_METRIC",
    "IRRELEVANT_EPISODE_SELECTION_RATE_METRIC",
    "build_episodic_experiment_metrics",
    "build_episodic_scenario_metrics",
    "build_episodic_scenario_success_aggregate",
]
