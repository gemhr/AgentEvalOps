"""Stage11 WP5 只读平台指标合同与确定性聚合。"""

# ruff: noqa: D105, D415

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from collections.abc import Mapping


CONTRACT_VERSION = "stage11.wp5.v1"
ALLOWED_GROUP_KEYS = frozenset({
    "dataset", "suite", "agent_version", "workflow_version", "toolset_binding",
    "provider_binding", "environment", "run_mode", "profile", "case_tag", "tool_identity",
})
DEFERRED_METRICS = {
    "tool_selection_adherence": "DEFERRED_PRODUCER",
    "forbidden_tool_violation": "DEFERRED_PRODUCER",
    "tool_schema_argument_validity": "DEFERRED_PRODUCER",
    "tool_execution_completed_rate": "DEFERRED_PRODUCER",
    "tool_side_effect_committed_rate": "DEFERRED_PRODUCER",
    "planning_required_step_recall": "DEFERRED_PRODUCER",
    "planning_forbidden_step_rate": "DEFERRED_PRODUCER",
    "planning_step_completion_rate": "DEFERRED_PRODUCER",
    "planning_dependency_violation_rate": "DEFERRED_PRODUCER",
    "planning_replan_rate": "DEFERRED_PRODUCER",
    "input_tokens": "DEFERRED_PRODUCER",
    "output_tokens": "DEFERRED_PRODUCER",
    "total_tokens": "DEFERRED_PRODUCER",
    "model_cost": "DEFERRED_PRODUCER",
}


class MetricIdentityCollision(ValueError):
    """同一公开 ID/version 对应不同语义。"""


class MetricFamily(StrEnum):
    """互不合成总分的指标家族。"""

    AGENT_QUALITY = "AGENT_QUALITY"
    EXECUTION_RELIABILITY = "EXECUTION_RELIABILITY"
    EVALUATION_HEALTH = "EVALUATION_HEALTH"
    PERFORMANCE = "PERFORMANCE"
    RESOURCE_COST = "RESOURCE_COST"


class MetricValueType(StrEnum):
    """数值语义。"""

    COUNT = "COUNT"
    RATE = "RATE"
    SCALAR = "SCALAR"
    DURATION = "DURATION"
    COST = "COST"
    DISTRIBUTION = "DISTRIBUTION"


class MetricDirection(StrEnum):
    """定义固定的方向。"""

    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"
    TARGET_RANGE = "TARGET_RANGE"
    NONE = "NONE"


class MetricMaturity(StrEnum):
    """producer 成熟度。"""

    IMPLEMENTABLE_NOW = "IMPLEMENTABLE_NOW"
    CONTRACT_ONLY = "CONTRACT_ONLY"
    DEFERRED_PRODUCER = "DEFERRED_PRODUCER"


class MetricState(StrEnum):
    """每个 expected grain 的互斥主状态。"""

    VALID = "VALID"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ERROR = "ERROR"
    INCONCLUSIVE = "INCONCLUSIVE"
    UNKNOWN = "UNKNOWN"
    NOT_COMPARABLE = "NOT_COMPARABLE"


def canonical_digest(value: Any) -> str:
    """对已结构化的非敏感语义事实计算稳定摘要。"""
    def canonical(item: Any) -> Any:
        if is_dataclass(item):
            return {part.name: canonical(getattr(item, part.name)) for part in fields(item)}
        if isinstance(item, Mapping):
            return {str(key): canonical(member) for key, member in item.items()}
        if isinstance(item, (tuple, list)):
            return [canonical(member) for member in item]
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, StrEnum):
            return item.value
        return item
    return hashlib.sha256(
        json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class MetricDefinitionV1:
    """指标的稳定公开语义，不含门禁阈值。"""

    metric_id: str
    metric_version: str
    family: MetricFamily
    input_contract: str
    value_type: MetricValueType
    direction: MetricDirection
    unit: str
    value_range: tuple[float, float] | None
    grain: str
    parameters: tuple[tuple[str, str], ...]
    eligibility_rule: str
    denominator_rule: str
    missing_rule: str
    aggregation: str
    required_source: str
    maturity: MetricMaturity = MetricMaturity.IMPLEMENTABLE_NOW
    display_name: str = field(default="", compare=False)
    metric_definition_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.metric_id or not self.metric_version:
            raise ValueError("metric identity is required")
        if self.aggregation not in {"COUNT", "SUM", "RATE", "MEAN", "P50", "P95", "P99"}:
            raise ValueError("unsupported aggregation")
        object.__setattr__(self, "parameters", tuple(sorted(self.parameters)))
        semantic = {name: getattr(self, name) for name in (
            "metric_id", "metric_version", "family", "input_contract", "value_type", "direction", "unit",
            "value_range", "grain", "parameters", "eligibility_rule", "denominator_rule", "missing_rule",
            "aggregation", "required_source",
        )}
        object.__setattr__(self, "metric_definition_digest", canonical_digest(semantic))


def validate_definitions(definitions: tuple[MetricDefinitionV1, ...]) -> None:
    """拒绝同名同版本异语义。"""
    seen: dict[tuple[str, str], str] = {}
    for definition in definitions:
        key = (definition.metric_id, definition.metric_version)
        previous = seen.setdefault(key, definition.metric_definition_digest)
        if previous != definition.metric_definition_digest:
            raise MetricIdentityCollision("METRIC_IDENTITY_COLLISION")


@dataclass(frozen=True, slots=True)
class MetricObservation:
    """单个预期 grain 的内部只读值。"""

    key: str
    state: MetricState
    value: int | float | Decimal | None = None
    observed: bool = False
    source_refs: tuple[str, ...] = ()
    reason: str | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        if self.state is MetricState.VALID:
            if self.value is None or isinstance(self.value, bool) or not math.isfinite(float(self.value)):
                raise ValueError("valid observation requires finite numeric value")
        elif self.value is not None:
            raise ValueError("non-valid observation cannot contain a value")


@dataclass(frozen=True, slots=True)
class MetricCoverageV1:
    """互斥状态计数及有效值覆盖率。"""

    expected_count: int
    eligible_count: int
    observed_count: int
    valid_count: int
    missing_count: int
    unavailable_count: int
    not_applicable_count: int
    error_count: int
    inconclusive_count: int
    unknown_count: int
    not_comparable_count: int
    value_denominator: int
    coverage: float | None

    def __post_init__(self) -> None:
        partition = (self.valid_count + self.missing_count + self.unavailable_count + self.error_count
                     + self.inconclusive_count + self.unknown_count + self.not_comparable_count)
        if partition != self.eligible_count or self.expected_count != self.eligible_count + self.not_applicable_count:
            raise ValueError("metric coverage states must partition expected population")
        if self.coverage != (self.valid_count / self.eligible_count if self.eligible_count else None):
            raise ValueError("metric coverage ratio is inconsistent")


@dataclass(frozen=True, slots=True)
class MetricAggregateV1:
    """一个 definition 在冻结 population 上的绝对聚合。"""

    definition: MetricDefinitionV1
    value: int | float | Decimal | None
    coverage: MetricCoverageV1
    numerator: int | float | Decimal | None
    denominator: int | None
    scope: str
    group: tuple[tuple[str, str], ...] = ()
    unavailable_reason: str | None = None
    source_refs: tuple[str, ...] = ()


def aggregate_metric(
    definition: MetricDefinitionV1, observations: tuple[MetricObservation, ...], *, scope: str,
    group: tuple[tuple[str, str], ...] = (),
) -> MetricAggregateV1:
    """只按 definition 聚合；每个 expected grain 必须唯一。"""
    if len({item.key for item in observations}) != len(observations):
        raise ValueError("duplicate metric grain")
    if any(key not in ALLOWED_GROUP_KEYS for key, _ in group):
        raise ValueError("unsupported metric group")
    if any(item.unit is not None and item.unit != definition.unit for item in observations):
        raise ValueError("metric observation unit/currency mismatch")
    counts = Counter(item.state for item in observations)
    eligible = len(observations) - counts[MetricState.NOT_APPLICABLE]
    valid = tuple(item.value for item in observations if item.state is MetricState.VALID)
    coverage = MetricCoverageV1(
        expected_count=len(observations), eligible_count=eligible,
        observed_count=sum(item.observed for item in observations), valid_count=len(valid),
        missing_count=counts[MetricState.MISSING], unavailable_count=counts[MetricState.UNAVAILABLE],
        not_applicable_count=counts[MetricState.NOT_APPLICABLE], error_count=counts[MetricState.ERROR],
        inconclusive_count=counts[MetricState.INCONCLUSIVE], unknown_count=counts[MetricState.UNKNOWN],
        not_comparable_count=counts[MetricState.NOT_COMPARABLE],
        value_denominator=eligible if definition.aggregation == "RATE" else len(valid),
        coverage=len(valid) / eligible if eligible else None,
    )
    if not eligible or not valid:
        value = None
    elif definition.aggregation == "COUNT":
        value = len(valid)
    elif definition.aggregation == "SUM":
        value = sum(valid)
    elif definition.aggregation == "RATE":
        value = sum(valid) / eligible if len(valid) == eligible else None
    elif definition.aggregation == "MEAN":
        value = sum(valid) / len(valid)
    else:
        ordered = sorted(valid)
        percentile = {"P50": 0.5, "P95": 0.95, "P99": 0.99}[definition.aggregation]
        value = ordered[math.ceil(percentile * len(ordered)) - 1]
    return MetricAggregateV1(
        definition=definition, value=value, coverage=coverage,
        numerator=sum(valid) if definition.aggregation in {"RATE", "SUM"} else None,
        denominator=eligible if definition.aggregation == "RATE" else None,
        scope=scope, group=group, unavailable_reason=None if value is not None else "NO_VALID_VALUE",
        source_refs=tuple(sorted({ref for item in observations for ref in item.source_refs})),
    )


@dataclass(frozen=True, slots=True)
class PairedMetricAggregateV1:
    """WP3 matched population 上的配对差。"""

    definition: MetricDefinitionV1
    baseline_paired_mean: float | None
    candidate_paired_mean: float | None
    paired_delta: float | None
    expected_count: int
    valid_count: int
    exclusion_reasons: tuple[tuple[str, int], ...]
    baseline_numerator: float | None = None
    candidate_numerator: float | None = None


@dataclass(frozen=True, slots=True)
class MetricReportV1:
    """只读派生的 WP5 报告。"""

    project_id: str
    baseline_run_id: str
    candidate_run_id: str
    comparison_contract_version: str
    comparison_digest: str
    source_refs: tuple[str, ...]
    baseline: tuple[MetricAggregateV1, ...]
    candidate: tuple[MetricAggregateV1, ...]
    paired: tuple[PairedMetricAggregateV1, ...]
    computed_at: datetime
    metric_contract_version: str = CONTRACT_VERSION
    semantic_digest: str = field(init=False)

    def __post_init__(self) -> None:
        definitions = tuple(item.definition for item in (*self.baseline, *self.candidate, *self.paired))
        validate_definitions(definitions)
        absolute = sorted((*self.baseline, *self.candidate), key=lambda item: (
            item.scope, item.definition.metric_id, item.definition.metric_version, item.group,
        ))
        paired = sorted(self.paired, key=lambda item: (
            item.definition.metric_id, item.definition.metric_version,
        ))
        semantic = (
            self.metric_contract_version, self.project_id, self.baseline_run_id, self.candidate_run_id,
            self.comparison_contract_version, self.comparison_digest, tuple(sorted(self.source_refs)),
            tuple((item.definition.metric_definition_digest, item.value, item.coverage, item.numerator,
                   item.denominator, item.scope, item.group, item.unavailable_reason, item.source_refs)
                  for item in absolute),
            tuple((item.definition.metric_definition_digest, item.baseline_paired_mean,
                   item.candidate_paired_mean, item.paired_delta, item.expected_count, item.valid_count,
                   item.exclusion_reasons, item.baseline_numerator, item.candidate_numerator) for item in paired),
        )
        object.__setattr__(self, "semantic_digest", canonical_digest(semantic))
