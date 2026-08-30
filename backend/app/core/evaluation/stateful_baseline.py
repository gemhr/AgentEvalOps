"""WP5 Stateful Memory baseline 比较（compatible comparison，拒绝误导性 delta）。

Baseline 是已冻结、可追溯的 prior scenario aggregate artifact。只有 dataset
id/version/digest、target contract（target id/version/config）与 runtime config 全部
兼容时才计算数值 delta；否则只报告 ``BASELINE_INCOMPATIBLE``，不做误导性的单数字比较。

输出至少：new failures、fixed failures、persistent failures、new blocked、
resolved blocked、scenario outcome delta、metric delta。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.evaluation.immutable import require_text
from app.core.evaluation.stateful_artifact import StatefulScenarioAggregateV1


class BaselineCompatibility(StrEnum):
    """Baseline 与 candidate 的可比较性。"""

    COMPATIBLE = "COMPATIBLE"
    BASELINE_INCOMPATIBLE = "BASELINE_INCOMPATIBLE"


class OutcomeDelta(StrEnum):
    """单个 scenario 的 outcome delta 分类。"""

    NEW_FAILURE = "NEW_FAILURE"
    FIXED_FAILURE = "FIXED_FAILURE"
    PERSISTENT_FAILURE = "PERSISTENT_FAILURE"
    NEW_PASS = "NEW_PASS"
    PERSISTENT_PASS = "PERSISTENT_PASS"
    NEW_BLOCKED = "NEW_BLOCKED"
    RESOLVED_BLOCKED = "RESOLVED_BLOCKED"
    PERSISTENT_BLOCKED = "PERSISTENT_BLOCKED"
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True, slots=True)
class CompatibilityFacts:
    """baseline 兼容性所需的 provenance facts 快照。"""

    dataset_id: str
    dataset_version: str
    dataset_digest: str | None
    target_id: str
    target_kind: str
    target_version_ref: dict[str, object] | None
    config_ref: dict[str, object] | None
    evaluation_implementation_ref: str | None = None

    @classmethod
    def from_artifact(cls, artifact: StatefulScenarioAggregateV1) -> "CompatibilityFacts":
        """从 scenario aggregate artifact 提取兼容性 facts。"""
        return cls(
            dataset_id=artifact.dataset_id,
            dataset_version=artifact.dataset_version,
            dataset_digest=artifact.dataset_digest,
            target_id=artifact.target_id,
            target_kind=artifact.target_kind,
            target_version_ref=artifact.target_version_ref,
            config_ref=artifact.config_ref,
            evaluation_implementation_ref=artifact.evaluation_implementation_ref,
        )

    def is_compatible_with(self, other: "CompatibilityFacts") -> bool:
        """两个 facts 是否完全兼容（dataset/target/config/impl-ref 逐项相等）。"""
        return (
            self.dataset_id == other.dataset_id
            and self.dataset_version == other.dataset_version
            and self.dataset_digest == other.dataset_digest
            and self.target_id == other.target_id
            and self.target_kind == other.target_kind
            and self.target_version_ref == other.target_version_ref
            and self.config_ref == other.config_ref
            and self.evaluation_implementation_ref == other.evaluation_implementation_ref
        )


@dataclass(frozen=True, slots=True)
class ScenarioBaselineComparison:
    """单个 scenario 的 baseline vs candidate 比较。"""

    scenario_id: str
    compatibility: BaselineCompatibility
    baseline_outcome: str | None
    candidate_outcome: str | None
    outcome_delta: OutcomeDelta | None
    baseline_failures: tuple[str, ...]
    candidate_failures: tuple[str, ...]
    new_failures: tuple[str, ...]
    fixed_failures: tuple[str, ...]
    persistent_failures: tuple[str, ...]
    new_blocked: tuple[str, ...]
    resolved_blocked: tuple[str, ...]
    metric_deltas: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.scenario_id, "scenario_id")
        if self.compatibility is BaselineCompatibility.BASELINE_INCOMPATIBLE:
            if self.outcome_delta is not None or self.metric_deltas:
                raise ValueError("BASELINE_INCOMPATIBLE must not carry numeric deltas")


@dataclass(frozen=True, slots=True)
class StatefulBaselineComparison:
    """一次完整的状态化 baseline 比较结果。"""

    compatibility: BaselineCompatibility
    incompatibility_reason: str | None = None
    comparisons: tuple[ScenarioBaselineComparison, ...] = ()
    new_failures: tuple[str, ...] = ()
    fixed_failures: tuple[str, ...] = ()
    persistent_failures: tuple[str, ...] = ()
    new_blocked: tuple[str, ...] = ()
    resolved_blocked: tuple[str, ...] = ()


def _scenario_index(
    artifacts: list[StatefulScenarioAggregateV1],
) -> dict[str, StatefulScenarioAggregateV1]:
    indexed: dict[str, StatefulScenarioAggregateV1] = {}
    for artifact in artifacts:
        if artifact.scenario_id in indexed:
            raise ValueError(f"duplicate scenario aggregate: {artifact.scenario_id}")
        indexed[artifact.scenario_id] = artifact
    return indexed


def _outcome_delta(baseline: str | None, candidate: str | None) -> OutcomeDelta | None:
    if baseline is None or candidate is None:
        return None
    if baseline == "FAIL" and candidate == "FAIL":
        return OutcomeDelta.PERSISTENT_FAILURE
    if baseline == "FAIL" and candidate == "PASS":
        return OutcomeDelta.FIXED_FAILURE
    if baseline == "PASS" and candidate == "FAIL":
        return OutcomeDelta.NEW_FAILURE
    if baseline == "PASS" and candidate == "PASS":
        return OutcomeDelta.PERSISTENT_PASS
    if baseline == "BLOCKED" and candidate == "BLOCKED":
        return OutcomeDelta.PERSISTENT_BLOCKED
    if baseline == "BLOCKED" and candidate != "BLOCKED":
        return OutcomeDelta.RESOLVED_BLOCKED
    if baseline != "BLOCKED" and candidate == "BLOCKED":
        return OutcomeDelta.NEW_BLOCKED
    return OutcomeDelta.UNCHANGED


def _failure_set(artifact: StatefulScenarioAggregateV1) -> tuple[str, ...]:
    return tuple(
        sorted({str(item["assertion_id"]) for item in artifact.assertion_results if item.get("status") == "FAIL"})
    )


def _blocked_set(artifact: StatefulScenarioAggregateV1) -> tuple[str, ...]:
    return tuple(
        sorted({str(item["assertion_id"]) for item in artifact.assertion_results if item.get("status") == "BLOCKED"})
    )


def _metric_deltas(
    baseline: StatefulScenarioAggregateV1,
    candidate: StatefulScenarioAggregateV1,
) -> dict[str, float]:
    deltas: dict[str, float] = {}
    for name, baseline_value in baseline.metric_aggregates.items():
        candidate_value = candidate.metric_aggregates.get(name)
        if candidate_value is None:
            continue
        b = baseline_value.get("value")
        c = candidate_value.get("value")
        if (
            b is not None
            and c is not None
            and baseline_value.get("evaluable_denominator")
            and candidate_value.get("evaluable_denominator")
        ):
            deltas[name] = float(c) - float(b)
    return deltas


def compare_stateful_baseline(
    baseline_artifacts: list[StatefulScenarioAggregateV1],
    candidate_artifacts: list[StatefulScenarioAggregateV1],
) -> StatefulBaselineComparison:
    """比较两份 frozen scenario aggregate 列表（按 scenario_id 对齐）。"""
    if not baseline_artifacts or not candidate_artifacts:
        raise ValueError("baseline and candidate artifact lists must not be empty")
    baseline_index = _scenario_index(baseline_artifacts)
    candidate_index = _scenario_index(candidate_artifacts)
    baseline_facts = CompatibilityFacts.from_artifact(baseline_artifacts[0])
    candidate_facts = CompatibilityFacts.from_artifact(candidate_artifacts[0])
    all_compatible = (
        all(
            baseline_facts.is_compatible_with(CompatibilityFacts.from_artifact(artifact))
            for artifact in baseline_artifacts
        )
        and all(
            candidate_facts.is_compatible_with(CompatibilityFacts.from_artifact(artifact))
            for artifact in candidate_artifacts
        )
        and baseline_facts.is_compatible_with(candidate_facts)
    )
    if not all_compatible:
        return StatefulBaselineComparison(
            compatibility=BaselineCompatibility.BASELINE_INCOMPATIBLE,
            incompatibility_reason=(
                "dataset version/digest, target contract or runtime config differs between baseline and candidate"
            ),
        )

    scenario_ids = sorted(set(baseline_index) | set(candidate_index))
    comparisons: list[ScenarioBaselineComparison] = []
    for scenario_id in scenario_ids:
        baseline = baseline_index.get(scenario_id)
        candidate = candidate_index.get(scenario_id)
        baseline_outcome = baseline.scenario_outcome if baseline else None
        candidate_outcome = candidate.scenario_outcome if candidate else None
        if baseline is None or candidate is None:
            comparisons.append(
                ScenarioBaselineComparison(
                    scenario_id=scenario_id,
                    compatibility=BaselineCompatibility.COMPATIBLE,
                    baseline_outcome=baseline_outcome,
                    candidate_outcome=candidate_outcome,
                    outcome_delta=_outcome_delta(baseline_outcome, candidate_outcome),
                    baseline_failures=_failure_set(baseline) if baseline else (),
                    candidate_failures=_failure_set(candidate) if candidate else (),
                    new_failures=(),
                    fixed_failures=(),
                    persistent_failures=(),
                    new_blocked=(),
                    resolved_blocked=(),
                )
            )
            continue
        baseline_failures = _failure_set(baseline)
        candidate_failures = _failure_set(candidate)
        baseline_blocked = _blocked_set(baseline)
        candidate_blocked = _blocked_set(candidate)
        new_failures = tuple(sorted(set(candidate_failures) - set(baseline_failures)))
        fixed_failures = tuple(sorted(set(baseline_failures) - set(candidate_failures)))
        persistent_failures = tuple(sorted(set(baseline_failures) & set(candidate_failures)))
        new_blocked = tuple(sorted(set(candidate_blocked) - set(baseline_blocked)))
        resolved_blocked = tuple(sorted(set(baseline_blocked) - set(candidate_blocked)))
        comparisons.append(
            ScenarioBaselineComparison(
                scenario_id=scenario_id,
                compatibility=BaselineCompatibility.COMPATIBLE,
                baseline_outcome=baseline_outcome,
                candidate_outcome=candidate_outcome,
                outcome_delta=_outcome_delta(baseline_outcome, candidate_outcome),
                baseline_failures=baseline_failures,
                candidate_failures=candidate_failures,
                new_failures=new_failures,
                fixed_failures=fixed_failures,
                persistent_failures=persistent_failures,
                new_blocked=new_blocked,
                resolved_blocked=resolved_blocked,
                metric_deltas=_metric_deltas(baseline, candidate),
            )
        )

    new_failures = tuple(sorted({failure for item in comparisons for failure in item.new_failures}))
    fixed_failures = tuple(sorted({failure for item in comparisons for failure in item.fixed_failures}))
    persistent_failures = tuple(sorted({failure for item in comparisons for failure in item.persistent_failures}))
    new_blocked = tuple(sorted({assertion for item in comparisons for assertion in item.new_blocked}))
    resolved_blocked = tuple(sorted({assertion for item in comparisons for assertion in item.resolved_blocked}))
    return StatefulBaselineComparison(
        compatibility=BaselineCompatibility.COMPATIBLE,
        comparisons=tuple(comparisons),
        new_failures=new_failures,
        fixed_failures=fixed_failures,
        persistent_failures=persistent_failures,
        new_blocked=new_blocked,
        resolved_blocked=resolved_blocked,
    )


__all__ = [
    "BaselineCompatibility",
    "CompatibilityFacts",
    "OutcomeDelta",
    "ScenarioBaselineComparison",
    "StatefulBaselineComparison",
    "compare_stateful_baseline",
]
