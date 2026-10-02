"""WP5 指标政策与 Release Gate 的最小消费合同。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from app.core.evaluation.platform_metrics import MetricDirection, MetricReportV1
from app.core.evaluation.report import RegressionReport, ReleaseDecision


class MetricRuleKind(StrEnum):
    """当前支持的门禁规则。"""

    ABSOLUTE_MIN = "ABSOLUTE_MIN"
    ABSOLUTE_MAX = "ABSOLUTE_MAX"
    MAX_REGRESSION_DELTA = "MAX_REGRESSION_DELTA"
    MIN_IMPROVEMENT = "MIN_IMPROVEMENT"
    MIN_COVERAGE = "MIN_COVERAGE"


@dataclass(frozen=True, slots=True)
class MetricRule:
    """显式配置的单一指标条件。"""

    kind: MetricRuleKind
    metric_id: str
    metric_version: str
    metric_definition_digest: str
    threshold: float

    def __post_init__(self) -> None:
        if not self.metric_id or not self.metric_version or not self.metric_definition_digest:
            raise ValueError("metric policy rule requires a definition reference")
        if not math.isfinite(self.threshold):
            raise ValueError("metric policy threshold must be finite")


@dataclass(frozen=True, slots=True)
class MetricPolicy:
    """政策版本及规则；不提供生产默认阈值。"""

    policy_id: str
    policy_version: str
    rules: tuple[MetricRule, ...]
    block_unavailable: bool = True
    block_not_comparable: bool = True

    def __post_init__(self) -> None:
        if not self.policy_id or not self.policy_version:
            raise ValueError("metric policy identity is required")


class MetricGateStatus(StrEnum):
    """WP5 门禁结果，缺政策不能假装 PASS。"""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass(frozen=True, slots=True)
class MetricGateDecision:
    """只引用 canonical 报告，不保存二次计算值。"""

    status: MetricGateStatus
    reasons: tuple[str, ...]
    policy_ref: str | None
    metric_report_digest: str
    policy_rules: tuple[MetricRule, ...] = ()


def evaluate_metric_gate(
    regression: RegressionReport, metrics: MetricReportV1, policy: MetricPolicy | None,
) -> MetricGateDecision:
    """保留 WP3 blocker 优先级，只比较报告中的已聚合值。"""
    if (str(regression.project_id) != metrics.project_id
        or str(regression.baseline_run_id) != metrics.baseline_run_id
        or str(regression.candidate_run_id) != metrics.candidate_run_id
        or regression.comparison_digest != metrics.comparison_digest):
        raise ValueError("MetricReport does not match RegressionReport")
    blockers = bool(regression.critical_regressions or regression.critical_not_comparable
                    or regression.incomplete_required_evidence)
    if blockers or regression.release_decision is ReleaseDecision.FAIL:
        return MetricGateDecision(MetricGateStatus.FAIL, ("WP3_CRITICAL_OR_REQUIRED_BLOCKER",),
                                  None if policy is None else f"{policy.policy_id}@{policy.policy_version}",
                                  metrics.semantic_digest, () if policy is None else policy.rules)
    if policy is None or not policy.rules:
        return MetricGateDecision(MetricGateStatus.NOT_CONFIGURED, ("METRIC_POLICY_NOT_CONFIGURED",),
                                  None, metrics.semantic_digest)
    absolute = {item.definition.metric_id: item for item in metrics.candidate}
    paired = {item.definition.metric_id: item for item in metrics.paired}
    reasons: list[str] = []
    for rule in policy.rules:
        item = (paired if rule.kind in {MetricRuleKind.MAX_REGRESSION_DELTA,
                                       MetricRuleKind.MIN_IMPROVEMENT} else absolute).get(rule.metric_id)
        if item is None or item.definition.metric_version != rule.metric_version or (
            item.definition.metric_definition_digest != rule.metric_definition_digest
        ):
            reasons.append(f"METRIC_NOT_COMPARABLE:{rule.metric_id}")
            continue
        if rule.kind is MetricRuleKind.MIN_COVERAGE:
            value = item.coverage.coverage
        elif rule.kind in {MetricRuleKind.MAX_REGRESSION_DELTA, MetricRuleKind.MIN_IMPROVEMENT}:
            delta = item.paired_delta
            value = (delta if item.definition.direction is MetricDirection.HIGHER_IS_BETTER
                     else -delta if delta is not None and item.definition.direction is MetricDirection.LOWER_IS_BETTER
                     else None)
        elif rule.kind in {MetricRuleKind.ABSOLUTE_MIN, MetricRuleKind.ABSOLUTE_MAX}:
            value = item.value
            if item.coverage.coverage is not None and item.coverage.coverage < 1 and policy.block_unavailable:
                reasons.append(f"METRIC_PARTIAL_COVERAGE:{rule.metric_id}")
        if rule.kind in {MetricRuleKind.MAX_REGRESSION_DELTA, MetricRuleKind.MIN_IMPROVEMENT}:
            if item.valid_count < item.expected_count and policy.block_not_comparable:
                reasons.append(f"PAIRED_PARTIAL_COVERAGE:{rule.metric_id}")
        if value is None:
            if policy.block_unavailable:
                reasons.append(f"METRIC_UNAVAILABLE:{rule.metric_id}")
        elif rule.kind in {MetricRuleKind.ABSOLUTE_MIN, MetricRuleKind.MIN_IMPROVEMENT,
                           MetricRuleKind.MIN_COVERAGE} and value < rule.threshold:
            reasons.append(f"RULE_FAILED:{rule.metric_id}:{rule.kind.value}")
        elif rule.kind is MetricRuleKind.MAX_REGRESSION_DELTA and value < -rule.threshold:
            reasons.append(f"RULE_FAILED:{rule.metric_id}:{rule.kind.value}")
        elif rule.kind is MetricRuleKind.ABSOLUTE_MAX and value > rule.threshold:
            reasons.append(f"RULE_FAILED:{rule.metric_id}:{rule.kind.value}")
    return MetricGateDecision(
        MetricGateStatus.FAIL if reasons else MetricGateStatus.PASS,
        tuple(reasons), f"{policy.policy_id}@{policy.policy_version}", metrics.semantic_digest, policy.rules,
    )
