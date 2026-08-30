"""WP5 Stateful Memory Hard Gate Policy（E1-R2 冻结）。

Gate membership 从 typed Scenario contract 派生（不接受任意 caller list）：

- ``deterministic_gate_eligible``：``required == true`` AND
  ``deterministic_denominator == true`` AND ``truthfulness_origin`` 属于 Layer-1
  allowed origins（``DETERMINISTIC_GROUND_TRUTH``）。该字段由
  ``evaluate_scenario`` 依据 dataset/scenario typed contract 计算。

Layer 分离：

- ``LAYER_1_DETERMINISTIC``：deterministic contract regression gate —— required
  eligible scenarios 必须 100% PASS。
- ``LAYER_2_REAL_MODEL``：real-model behavioral evaluation —— report-only /
  baseline-relative；不把 real-model 未 100% PASS 当作 deterministic contract suite
  failed。

两组 correctness gate 两种 layer 都适用：forgotten/scope leakage failures、keyed
ACTIVE invariant violations、evidence/provision failures 不静默排除、artifact 保留。
Leakage 为 NA（无 identity evidence）时绝不声称 zero leakage proven。
"""

# ruff: noqa: D415

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.evaluation.stateful_assertion import (
    AssertionDimension,
    AssertionStatus,
    BlockReason,
    EvaluationLayer,
    EvidenceGapClassification,
)
from app.core.evaluation.stateful_evaluators import (
    FORGOTTEN_LEAKAGE_RATE_METRIC,
    SCOPE_LEAKAGE_RATE_METRIC,
    ScenarioEvaluation,
)


@dataclass(frozen=True, slots=True)
class HardGateResult:
    """一次 hard gate 判定的不可变结果。"""

    passed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    layer: EvaluationLayer = EvaluationLayer.LAYER_2_REAL_MODEL
    deterministic_gate_required_total: int = 0
    deterministic_gate_required_passed: int = 0
    execution_required_total: int = 0
    forgotten_leakage_failures: int = 0
    scope_leakage_failures: int = 0
    keyed_active_invariant_violations: int = 0
    evidence_failure_retained: bool = True
    forgotten_leakage_not_evaluable: bool = False
    scope_leakage_not_evaluable: bool = False
    expected_evidence_limitation_blocked_count: int = 0
    identity_safety_not_evaluable: bool = False


def _leakage_failures(evaluations: list[ScenarioEvaluation], metric_name: str) -> int:
    count = 0
    for evaluation in evaluations:
        aggregate = evaluation.metrics.get(metric_name)
        if aggregate is not None:
            count += aggregate.failed
    return count


def _leakage_not_evaluable(evaluations: list[ScenarioEvaluation], metric_name: str) -> bool:
    for evaluation in evaluations:
        aggregate = evaluation.metrics.get(metric_name)
        if aggregate is not None and aggregate.evaluable_denominator == 0:
            return True
    return False


def _keyed_active_invariant_violations(evaluations: list[ScenarioEvaluation]) -> int:
    count = 0
    for evaluation in evaluations:
        for assertion in evaluation.assertions:
            if assertion.assertion_id.endswith("keyed_active_le_1") and assertion.status is AssertionStatus.FAIL:
                count += 1
    return count


def _all_underlying_required_blocks_expected_limitation(evaluation: ScenarioEvaluation) -> bool:
    """该 evaluation 的除 outcome roll-up 外的 required BLOCKED 是否全部为 expected limitation。

    用于判断 scenario outcome 的 PREREQUISITE BLOCKED 是否纯粹由已声明的 expected
    limitation 派生（此时它不是独立的 evidence/provision failure）。
    """
    outcome_id = f"{evaluation.scenario_id}.outcome"
    for assertion in evaluation.assertions:
        if assertion.status is not AssertionStatus.BLOCKED or not assertion.required:
            continue
        if assertion.assertion_id == outcome_id:
            continue
        if not (
            assertion.blocked_by is BlockReason.NOT_SUPPORTED_BY_CURRENT_EVIDENCE
            and assertion.evidence_gap_classification is EvidenceGapClassification.EXPECTED_EVIDENCE_LIMITATION
        ):
            return False
    return True


def _has_evidence_failure(evaluations: list[ScenarioEvaluation]) -> bool:
    """是否存在必须保留的 evidence/provision failure。

    R3-B：Dataset 已声明为 EXPECTED_LIMITATION 的 identity BLOCKED
    （``EXPECTED_EVIDENCE_LIMITATION``）不是 infra failure；若 scenario outcome 的
    PREREQUISITE BLOCKED 纯粹由这些 expected limitation 派生，也不构成 evidence
    failure。其余任何 BLOCKED（含未分类的 evidence gap）仍视为 evidence failure。
    """
    for evaluation in evaluations:
        outcome_id = f"{evaluation.scenario_id}.outcome"
        for assertion in evaluation.assertions:
            if assertion.status is not AssertionStatus.BLOCKED:
                continue
            if (
                assertion.blocked_by is BlockReason.NOT_SUPPORTED_BY_CURRENT_EVIDENCE
                and assertion.evidence_gap_classification is EvidenceGapClassification.EXPECTED_EVIDENCE_LIMITATION
            ):
                continue
            if assertion.assertion_id == outcome_id and _all_underlying_required_blocks_expected_limitation(
                evaluation
            ):
                continue
            return True
    return False


def _expected_limitation_blocked_count(evaluations: list[ScenarioEvaluation]) -> int:
    """统计已声明 expected limitation 的 identity BLOCKED 断言总数。"""
    count = 0
    for evaluation in evaluations:
        for assertion in evaluation.assertions:
            if assertion.status is AssertionStatus.BLOCKED and (
                assertion.evidence_gap_classification is EvidenceGapClassification.EXPECTED_EVIDENCE_LIMITATION
            ):
                count += 1
    return count


def _identity_safety_not_evaluable(evaluations: list[ScenarioEvaluation]) -> bool:
    """Identity safety 是否因 expected limitation 而 NOT_EVALUABLE。"""
    return any(
        assertion.dimension is AssertionDimension.RETRIEVAL
        and assertion.status is AssertionStatus.BLOCKED
        and assertion.evidence_gap_classification is EvidenceGapClassification.EXPECTED_EVIDENCE_LIMITATION
        for evaluation in evaluations
        for assertion in evaluation.assertions
    )


def evaluate_hard_gate(
    evaluations: list[ScenarioEvaluation],
    *,
    layer: EvaluationLayer = EvaluationLayer.LAYER_2_REAL_MODEL,
    artifacts_retained: bool = True,
) -> HardGateResult:
    """对 evaluations 执行 hard gate；Layer-1 deterministic 门槛与 Layer-2 report-only 分离。

    Args:
        evaluations: 全部 scenario evaluations（gate membership 由每个 evaluation 的
            ``deterministic_gate_eligible`` 派生，不接受任意 caller list）。
        layer: 执行层。LAYER_1 应用 deterministic 100% PASS 门槛；
            LAYER_2 不把 real-model 非全 PASS 当作 deterministic failure。
        artifacts_retained: evidence/provision 失败是否保留在 artifact。
    """
    reasons: list[str] = []
    gate_required = [item for item in evaluations if item.required and item.deterministic_gate_eligible]
    execution_required = [item for item in evaluations if item.scenario_id and item.required]
    gate_required_pass = sum(item.scenario_outcome is AssertionStatus.PASS for item in gate_required)
    if layer is EvaluationLayer.LAYER_1_DETERMINISTIC:
        if gate_required and gate_required_pass != len(gate_required):
            reasons.append(
                f"required deterministic scenarios not 100% PASS ({gate_required_pass}/{len(gate_required)})"
            )

    forgotten_leakage = _leakage_failures(evaluations, FORGOTTEN_LEAKAGE_RATE_METRIC)
    if forgotten_leakage > 0:
        reasons.append(f"forgotten memory leakage failures: {forgotten_leakage}")

    scope_leakage = _leakage_failures(evaluations, SCOPE_LEAKAGE_RATE_METRIC)
    if scope_leakage > 0:
        reasons.append(f"scope leakage failures: {scope_leakage}")

    invariant_violations = _keyed_active_invariant_violations(evaluations)
    if invariant_violations > 0:
        reasons.append(f"keyed ACTIVE invariant violations: {invariant_violations}")

    if _has_evidence_failure(evaluations):
        reasons.append("evidence/provision failures are present and must not be silently excluded")

    if not artifacts_retained:
        reasons.append("evidence/provision failures not retained in artifacts")

    passed = not reasons
    expected_limitation_count = _expected_limitation_blocked_count(evaluations)
    return HardGateResult(
        passed=passed,
        reasons=tuple(reasons),
        layer=layer,
        deterministic_gate_required_total=len(gate_required),
        deterministic_gate_required_passed=gate_required_pass,
        execution_required_total=len(execution_required),
        forgotten_leakage_failures=forgotten_leakage,
        scope_leakage_failures=scope_leakage,
        keyed_active_invariant_violations=invariant_violations,
        evidence_failure_retained=artifacts_retained,
        forgotten_leakage_not_evaluable=_leakage_not_evaluable(evaluations, FORGOTTEN_LEAKAGE_RATE_METRIC),
        scope_leakage_not_evaluable=_leakage_not_evaluable(evaluations, SCOPE_LEAKAGE_RATE_METRIC),
        expected_evidence_limitation_blocked_count=expected_limitation_count,
        identity_safety_not_evaluable=_identity_safety_not_evaluable(evaluations),
    )


def scenario_has_blocked(evaluation: ScenarioEvaluation) -> bool:
    """判断一个 scenario evaluation 是否含有任何 BLOCKED assertion。"""
    return any(assertion.status is AssertionStatus.BLOCKED for assertion in evaluation.assertions)


__all__ = [
    "EvaluationLayer",
    "HardGateResult",
    "evaluate_hard_gate",
    "scenario_has_blocked",
]
