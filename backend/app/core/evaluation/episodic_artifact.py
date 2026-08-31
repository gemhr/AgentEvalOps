"""WP6-E aggregate experiment artifact（``stateful-episodic-evaluation-artifact.v1``）。

artifact 是 JSON-ready、append-only、可追溯的 experiment 级聚合快照。默认不复制
Episode 正文（canonical_text / situation / goal / lesson）；只保存 assertion result、
hashes、symbolic 字段结果与 synthetic fixture 引用。Dataset 本身的 synthetic fixture
可按现有 artifact policy 保留（private_evaluation_artifact=True）。
"""
# ruff: noqa: D101, D105, D415

# ruff: noqa: D105, D415

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from app.core.evaluation.immutable import freeze_json, json_compatible


class EpisodicRunAttemptRecord(BaseModel):
    """一个 run 的 attempt/run artifact 投影（不含 secret/raw provider 内容）。"""

    model_config = ConfigDict(extra="forbid")

    scenario_id: StrictStr
    case_code: StrictStr
    dataset_run_id: StrictStr
    actual_runtime_run_id: StrictStr
    execution_status: StrictStr
    terminal_status: StrictStr | None = None
    delivery_status: StrictStr | None = None
    evaluation_controls_sent: list[str] = Field(default_factory=list)
    target_status: StrictStr | None = None
    target_stop_reason: StrictStr | None = None
    target_error_code: StrictStr | None = None
    evaluation_control_status: StrictStr | None = None
    evaluation_error_code: StrictStr | None = None
    capture_status: StrictStr | None = None
    capture_error_code: StrictStr | None = None
    formation_receipt_summary: dict[str, object] | None = None
    fixture_receipt_summary: dict[str, object] | None = None
    replay_receipt_summary: dict[str, object] | None = None
    capture_artifact_reference: str | None = None
    journal_artifact_reference: str | None = None
    sqlite_projection_reference: str | None = None
    evaluation_infra_status: StrictStr | None = None
    journal_step_facts: dict[str, object] | None = None
    journal_error: str | None = None


class ExperimentExecutionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


class ExperimentBlockReason(StrEnum):
    PREREQUISITE = "PREREQUISITE"
    EVALUATION_INFRA = "EVALUATION_INFRA"


class EpisodicScenarioArtifact(BaseModel):
    """一个 scenario 的完整 evaluation 聚合。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictStr = "stateful-episodic-scenario-aggregate.v1"
    evaluation_run_id: StrictStr
    scenario_id: StrictStr
    case_code: StrictStr
    truthfulness_origin: StrictStr
    episode_origin_kind: StrictStr
    scenario_outcome: StrictStr
    scenario_outcome_assertion: dict[str, object]
    assertion_results: list[dict[str, object]] = Field(default_factory=list)
    metric_aggregates: dict[str, dict[str, object]] = Field(default_factory=dict)
    failure_taxonomies: list[str] = Field(default_factory=list)
    run_attempts: list[EpisodicRunAttemptRecord] = Field(default_factory=list)
    identity_resolutions: list[dict[str, object]] = Field(default_factory=list)
    episode_projection_summary: list[dict[str, object]] = Field(default_factory=list)
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


class EpisodicExperimentArtifact(BaseModel):
    """``stateful-episodic-evaluation-artifact.v1`` experiment 级聚合。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictStr = "stateful-episodic-evaluation-artifact.v1"
    experiment_id: StrictStr
    dataset: dict[str, object]
    agentevalops_implementation_ref: str
    target_evaluation_implementation_ref: str
    execution_policy: StrictStr
    experiment_execution_status: ExperimentExecutionStatus = ExperimentExecutionStatus.COMPLETED
    experiment_block_reason: ExperimentBlockReason | None = None
    scenario_execution_started: bool = True
    scenario_artifacts: list[EpisodicScenarioArtifact] = Field(default_factory=list)
    assertion_summary: dict[str, int] = Field(default_factory=dict)
    failure_taxonomy: list[str] = Field(default_factory=list)
    blocked_taxonomy: list[str] = Field(default_factory=list)
    metrics: dict[str, dict[str, object]] = Field(default_factory=dict)
    layer1_gate: dict[str, object]
    environment_provenance: dict[str, object] = Field(default_factory=dict)
    created_at: StrictStr
    tooling_runtime_warnings: list[str] = Field(default_factory=list)
    baseline: dict[str, object] | None = None
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
        """返回纯 JSON-compatible dict（artifact 持久化/比较用）。"""
        projected = json_compatible(self.model_dump(mode="python"))
        if not isinstance(projected, dict):
            raise TypeError("experiment artifact must project to a JSON object")
        return projected


__all__ = [
    "EpisodicExperimentArtifact",
    "EpisodicRunAttemptRecord",
    "EpisodicScenarioArtifact",
    "ExperimentBlockReason",
    "ExperimentExecutionStatus",
]
