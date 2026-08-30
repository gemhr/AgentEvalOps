"""WP5 metric aggregate 与 denominator 契约。

每个 aggregate 必须保存 ``passed`` / ``failed`` / ``blocked`` / ``not_applicable`` /
``evaluable_denominator`` 与 ``value``。规则：

- ``evaluable_denominator = PASS + FAIL``；BLOCKED / NOT_APPLICABLE 不进入 denominator。
- 没有任何 evaluable unit 时 metric 为 NOT_APPLICABLE、``value=None``，不显示 0 或 1。
- Runtime Block Rate = required blocked(runtime) / required applicable assertions，
  与 Evaluation Infrastructure Failure Rate 独立报告，禁止让 BLOCKED 隐形消失。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from app.core.evaluation.immutable import require_text
from app.core.evaluation.stateful_assertion import (
    AssertionDimension,
    AssertionStatus,
    BlockReason,
    EvidenceGapClassification,
    MemoryAssertion,
)

RUNTIME_BLOCK_RATE_METRIC: Final[str] = "runtime_block_rate"
EVALUATION_INFRA_FAILURE_RATE_METRIC: Final[str] = "evaluation_infra_failure_rate"
SCENARIO_SUCCESS_RATE_METRIC: Final[str] = "stateful_memory_scenario_success_rate"
EXPECTED_EVIDENCE_LIMITATION_BLOCKED_COUNT_METRIC: Final[str] = "expected_evidence_limitation_blocked_count"


@dataclass(frozen=True, slots=True)
class RatioMetric:
    """显式 rate metric：``value = numerator / denominator``。

    与 ``MetricAggregate``（accuracy，``value = passed / evaluable``）语义不同：
    rate 的 numerator 是相关 failure/blocked 计数。denominator 为 0 时
    ``value=None``（NOT_APPLICABLE），不显示 0。
    """

    metric_name: str
    numerator: int
    denominator: int
    not_applicable: int
    value: float | None

    def __post_init__(self) -> None:
        require_text(self.metric_name, "metric_name")
        if self.numerator < 0 or self.denominator < 0 or self.not_applicable < 0:
            raise ValueError("ratio counts must be non-negative")
        if self.numerator > self.denominator:
            raise ValueError("numerator must not exceed denominator")
        if self.value is not None:
            if not math.isfinite(self.value):
                raise ValueError("ratio value must be finite")
            if not 0.0 <= self.value <= 1.0:
                raise ValueError("ratio value must be within [0, 1]")
            if self.denominator == 0:
                raise ValueError("evaluable ratio requires a positive denominator")
        elif self.denominator > 0:
            raise ValueError("evaluable ratio requires a value")

    def as_dict(self) -> dict[str, object]:
        """序列化为 JSON-ready ratio 快照（kind=ratio 供报告区分）。"""
        return {
            "metric_name": self.metric_name,
            "kind": "ratio",
            "numerator": self.numerator,
            "denominator": self.denominator,
            "not_applicable": self.not_applicable,
            "value": self.value,
        }


def _ratio(metric_name: str, numerator: int, denominator: int, not_applicable: int) -> RatioMetric:
    value: float | None = None
    if denominator > 0:
        value = numerator / denominator
    return RatioMetric(
        metric_name=metric_name,
        numerator=numerator,
        denominator=denominator,
        not_applicable=not_applicable,
        value=value,
    )


@dataclass(frozen=True, slots=True)
class MetricAggregate:
    """一个 metric 的完整状态与 denominator 投影。"""

    metric_name: str
    passed: int
    failed: int
    blocked: int
    not_applicable: int
    evaluable_denominator: int
    value: float | None

    def __post_init__(self) -> None:
        require_text(self.metric_name, "metric_name")
        counts = (self.passed, self.failed, self.blocked, self.not_applicable)
        if any(count < 0 for count in counts):
            raise ValueError("metric counts must be non-negative")
        if self.passed + self.failed != self.evaluable_denominator:
            raise ValueError("evaluable_denominator must equal passed + failed")
        if self.value is not None:
            if not math.isfinite(self.value):
                raise ValueError("metric value must be finite")
            if not 0.0 <= self.value <= 1.0:
                raise ValueError("metric value must be within [0, 1]")
            if self.evaluable_denominator == 0:
                raise ValueError("evaluable metric must have a positive denominator")
        elif self.evaluable_denominator > 0:
            raise ValueError("evaluable metric requires a value")

    def as_dict(self) -> dict[str, object]:
        """序列化为 JSON-ready aggregate 快照。"""
        return {
            "metric_name": self.metric_name,
            "passed": self.passed,
            "failed": self.failed,
            "blocked": self.blocked,
            "not_applicable": self.not_applicable,
            "evaluable_denominator": self.evaluable_denominator,
            "value": self.value,
        }


def build_metric_aggregate(metric_name: str, assertions: list[MemoryAssertion]) -> MetricAggregate:
    """从一个 assertion 集合构建 denominator 正确的 metric aggregate。"""
    passed = sum(item.status is AssertionStatus.PASS for item in assertions)
    failed = sum(item.status is AssertionStatus.FAIL for item in assertions)
    blocked = sum(item.status is AssertionStatus.BLOCKED for item in assertions)
    not_applicable = sum(item.status is AssertionStatus.NOT_APPLICABLE for item in assertions)
    denominator = passed + failed
    value: float | None = None
    if denominator > 0:
        value = passed / denominator
    return MetricAggregate(
        metric_name=metric_name,
        passed=passed,
        failed=failed,
        blocked=blocked,
        not_applicable=not_applicable,
        evaluable_denominator=denominator,
        value=value,
    )


def build_failure_rate_aggregate(metric_name: str, assertions: list[MemoryAssertion]) -> MetricAggregate:
    """构建 bad-event rate aggregate：``value = failed / (passed + failed)``。

    断言状态计数保持原样；仅用于被冻结为 failure-rate 的 leakage metrics。
    """
    passed = sum(item.status is AssertionStatus.PASS for item in assertions)
    failed = sum(item.status is AssertionStatus.FAIL for item in assertions)
    blocked = sum(item.status is AssertionStatus.BLOCKED for item in assertions)
    not_applicable = sum(item.status is AssertionStatus.NOT_APPLICABLE for item in assertions)
    denominator = passed + failed
    value = failed / denominator if denominator > 0 else None
    return MetricAggregate(
        metric_name=metric_name,
        passed=passed,
        failed=failed,
        blocked=blocked,
        not_applicable=not_applicable,
        evaluable_denominator=denominator,
        value=value,
    )


def _required_assertions(assertions: list[MemoryAssertion]) -> list[MemoryAssertion]:
    return [item for item in assertions if item.required]


def build_runtime_block_rate(assertions: list[MemoryAssertion]) -> RatioMetric:
    """Runtime Block Rate = runtime_blocked_required / required_applicable。

    这是显式 rate（numerator/denominator），绝不是 ``passed/denominator`` 的 accuracy
    aggregate。``applicable`` 指非 NOT_APPLICABLE 的 required assertion。
    """
    required = _required_assertions(assertions)
    applicable = [item for item in required if item.status is not AssertionStatus.NOT_APPLICABLE]
    runtime_blocked = sum(
        item.status is AssertionStatus.BLOCKED and item.blocked_by is BlockReason.RUNTIME for item in applicable
    )
    denominator = len(applicable)
    return _ratio(RUNTIME_BLOCK_RATE_METRIC, runtime_blocked, denominator, len(required) - denominator)


def build_evaluation_infra_failure_rate(assertions: list[MemoryAssertion]) -> RatioMetric:
    """Evaluation Infrastructure Failure Rate = infra_failures / required_applicable。

    显式 rate；BLOCKED(evidence_capture/evaluation_infrastructure) 为 numerator。
    单独报告，不混入 Runtime Block Rate，也不进入质量 denominator。

    R3-B：Dataset 已声明为 ``EXPECTED_EVIDENCE_LIMITATION`` 的 BLOCKED 不算 infra
    failure；``NOT_SUPPORTED_BY_CURRENT_EVIDENCE`` 从 R2 起就不进入 numerator。
    unexpected evidence gap（未分类 identity BLOCKED）、journal capture failure、
    projection failure、artifact failure 继续进入 numerator。
    """
    required = _required_assertions(assertions)
    applicable = [item for item in required if item.status is not AssertionStatus.NOT_APPLICABLE]
    infra_blocked = sum(
        item.status is AssertionStatus.BLOCKED
        and item.evidence_gap_classification is not EvidenceGapClassification.EXPECTED_EVIDENCE_LIMITATION
        and item.blocked_by in {BlockReason.EVIDENCE_CAPTURE, BlockReason.EVALUATION_INFRASTRUCTURE}
        for item in applicable
    )
    denominator = len(applicable)
    return _ratio(EVALUATION_INFRA_FAILURE_RATE_METRIC, infra_blocked, denominator, len(required) - denominator)


def expected_evidence_limitation_blocked_count(assertions: list[MemoryAssertion]) -> int:
    """已声明 expected limitation 的 BLOCKED 断言计数（不得藏在 reason 文本里）。"""
    return sum(
        item.status is AssertionStatus.BLOCKED
        and item.evidence_gap_classification is EvidenceGapClassification.EXPECTED_EVIDENCE_LIMITATION
        for item in assertions
    )


def build_expected_evidence_limitation_blocked_aggregate(
    assertions: list[MemoryAssertion],
) -> MetricAggregate:
    """把 expected-limitation BLOCKED 计数投影为统一 MetricAggregate（blocked 保留）。

    该维度无 PASS/FAIL evaluable 单元（限制 = not evaluable），故
    ``evaluable_denominator=0``、``value=None``；只把数量放进 ``blocked`` 字段，
    与既有 blocked-classification aggregate 体系一致，不新建第二套 report framework。
    """
    count = expected_evidence_limitation_blocked_count(assertions)
    return MetricAggregate(
        metric_name=EXPECTED_EVIDENCE_LIMITATION_BLOCKED_COUNT_METRIC,
        passed=0,
        failed=0,
        blocked=count,
        not_applicable=0,
        evaluable_denominator=0,
        value=None,
    )


def group_assertions_by_dimension(
    assertions: list[MemoryAssertion],
) -> dict[AssertionDimension, list[MemoryAssertion]]:
    """按 dimension 分组 assertion（preserve 顺序）。"""
    grouped: dict[AssertionDimension, list[MemoryAssertion]] = {}
    for item in assertions:
        grouped.setdefault(item.dimension, []).append(item)
    return grouped


def scenario_success_assertion(
    assertion_id: str,
    required_pass_count: int,
    required_fail_count: int,
    required_blocked_count: int,
    optional_fail_count: int,
    child_failure_taxonomies: list[str] | tuple[str, ...] = (),
) -> MemoryAssertion:
    """Scenario PASS 规则：所有 required applicable assertion PASS，无 FAIL/BLOCKED。

    optional failure 不阻塞 scenario；BLOCKED 使 scenario BLOCKED；FAIL 使 scenario FAIL。

    E1-R2 冻结：scenario roll-up 的 outcome assertion 不虚构任何 dimension taxonomy。
    E2E FAIL 允许 ``failure_taxonomy=None``，并把失败的 child primary taxonomies 保留在
    ``actual_evidence`` 中（不再统一贴 FINAL_STATE_MISMATCH）。
    """
    if required_fail_count > 0:
        return MemoryAssertion(
            assertion_id=assertion_id,
            dimension=AssertionDimension.E2E,
            status=AssertionStatus.FAIL,
            expected={"required_pass": required_pass_count, "required_fail": 0},
            actual_evidence={
                "required_fail": required_fail_count,
                "required_blocked": required_blocked_count,
                "optional_fail": optional_fail_count,
                "child_failure_taxonomies": sorted(set(child_failure_taxonomies)),
            },
            failure_taxonomy=None,
            reason="required applicable assertion failed",
        )
    if required_blocked_count > 0:
        return MemoryAssertion(
            assertion_id=assertion_id,
            dimension=AssertionDimension.E2E,
            status=AssertionStatus.BLOCKED,
            expected={"required_pass": required_pass_count, "required_blocked": 0},
            actual_evidence={
                "required_fail": required_fail_count,
                "required_blocked": required_blocked_count,
                "optional_fail": optional_fail_count,
            },
            blocked_by=BlockReason.PREREQUISITE,
            reason="required applicable assertion is blocked",
        )
    return MemoryAssertion(
        assertion_id=assertion_id,
        dimension=AssertionDimension.E2E,
        status=AssertionStatus.PASS,
        expected={"required_pass": required_pass_count},
        actual_evidence={
            "required_fail": 0,
            "required_blocked": 0,
            "optional_fail": optional_fail_count,
        },
        reason="all required applicable assertions passed",
    )


def status_counts(assertions: list[MemoryAssertion]) -> dict[str, int]:
    """统计全部 assertion（含 optional/NA）的 status 计数。"""
    counts: dict[str, int] = {
        AssertionStatus.PASS.value: 0,
        AssertionStatus.FAIL.value: 0,
        AssertionStatus.BLOCKED.value: 0,
        AssertionStatus.NOT_APPLICABLE.value: 0,
    }
    for item in assertions:
        counts[item.status.value] += 1
    return counts


def optional_assertions(assertions: list[MemoryAssertion]) -> list[MemoryAssertion]:
    """返回 optional（non-required）assertions。"""
    return [item for item in assertions if not item.required]


def required_assertions(assertions: list[MemoryAssertion]) -> list[MemoryAssertion]:
    """返回 required assertions。"""
    return [item for item in assertions if item.required]


__all__ = [
    "EVALUATION_INFRA_FAILURE_RATE_METRIC",
    "EXPECTED_EVIDENCE_LIMITATION_BLOCKED_COUNT_METRIC",
    "MetricAggregate",
    "RUNTIME_BLOCK_RATE_METRIC",
    "RatioMetric",
    "SCENARIO_SUCCESS_RATE_METRIC",
    "build_evaluation_infra_failure_rate",
    "build_expected_evidence_limitation_blocked_aggregate",
    "build_failure_rate_aggregate",
    "build_metric_aggregate",
    "build_runtime_block_rate",
    "expected_evidence_limitation_blocked_count",
    "group_assertions_by_dimension",
    "optional_assertions",
    "required_assertions",
    "scenario_success_assertion",
    "status_counts",
]
