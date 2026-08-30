"""WP5 Stateful Memory Scenario aggregate artifact（复用既有 artifact/result 语义）。

不新建 Dashboard；artifact 是 JSON-ready、append-only、可追溯的 scenario 级聚合快照。
标记为 ``private_evaluation_artifact``（允许保留 test input / expected value / 只读
isolated final-state payload；不上传 production telemetry）。
"""

# ruff: noqa: D415

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from app.core.evaluation.immutable import freeze_json, json_compatible


class StatefulStepAttemptRecord(BaseModel):
    """一个 step 的 attempt 持久化凭据（不含完整 attempt payload）。"""

    model_config = ConfigDict(extra="forbid")

    step_id: StrictStr
    case_id: StrictStr
    case_version: StrictStr
    attempt_id: StrictStr
    outcome_kind: StrictStr
    attempt_evidence_refs: list[dict[str, object]] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class StatefulSnapshotRef(BaseModel):
    """一个 read-only state snapshot 的 artifact 引用。"""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: StrictStr
    phase: StrictStr
    step_id: StrictStr | None = None
    db_path: StrictStr
    record_count: int
    evidence_ref: dict[str, object]


class StatefulScenarioAggregateV1(BaseModel):
    """一个 scenario 的完整 evaluation 聚合（JSON-ready）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictStr = "stateful-memory-scenario-aggregate.v1"
    evaluation_run_id: StrictStr
    dataset_id: StrictStr
    dataset_version: StrictStr
    dataset_digest: StrictStr | None = None
    target_id: StrictStr
    target_kind: StrictStr
    target_version_ref: dict[str, object] | None = None
    config_ref: dict[str, object] | None = None
    model_refs: list[dict[str, object]] = Field(default_factory=list)
    scenario_id: StrictStr
    truthfulness_origin: StrictStr
    regression_tags: list[str] = Field(default_factory=list)
    required: bool = True
    deterministic_denominator: bool = True
    initial_state: dict[str, object]
    step_attempts: list[StatefulStepAttemptRecord] = Field(default_factory=list)
    runtime_evidence_refs: list[dict[str, object]] = Field(default_factory=list)
    snapshot_refs: list[StatefulSnapshotRef] = Field(default_factory=list)
    expected_state: list[dict[str, object]] = Field(default_factory=list)
    actual_state: list[dict[str, object]] = Field(default_factory=list)
    state_diff: list[dict[str, object]] = Field(default_factory=list)
    assertion_results: list[dict[str, object]] = Field(default_factory=list)
    metric_aggregates: dict[str, dict[str, object]] = Field(default_factory=dict)
    failure_taxonomies: list[str] = Field(default_factory=list)
    scenario_outcome: StrictStr
    scenario_outcome_assertion: dict[str, object]
    retention_ref: dict[str, object] | None = None
    baseline_comparison: dict[str, object] | None = None
    evaluation_implementation_ref: str | None = None
    private_evaluation_artifact: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            return value
        try:
            freeze_json(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"metadata must be JSON-compatible: {exc}") from exc
        return value

    def model_dump_json_compat(self) -> dict[str, object]:
        """返回纯 JSON-compatible dict（artifact 持久化/比较用）。

        先以 ``mode="python"`` 保留 Domain 不可变 DTO（FrozenDict 等），再经
        ``json_compatible`` 递归投影为纯 JSON 类型；嵌套 evidence metadata 中的
        FrozenDict / tuple / Enum 等均被正确处理。
        """
        projected = json_compatible(self.model_dump(mode="python"))
        if not isinstance(projected, dict):
            raise TypeError("aggregate artifact must project to a JSON object")
        return projected


__all__ = [
    "StatefulScenarioAggregateV1",
    "StatefulSnapshotRef",
    "StatefulStepAttemptRecord",
]
