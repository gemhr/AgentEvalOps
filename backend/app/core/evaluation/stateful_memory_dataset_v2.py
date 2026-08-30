"""Stateful Memory Evaluation Dataset v2 —— ``stateful-memory-scenario.v2`` typed contract。

本模块是 V1（``stateful_memory_dataset.py``）的严格不兼容演进，不共享可变 model：

- ``InitialMemoryStateV2.records`` 使用 ``SeededMemoryRecord``（仅用于
  ``InitialMemoryStateKind.SEEDED``），携带 seeded ``canonical_text``；
  ``expected_state`` 继续使用现有 ``MemoryRecordExpectation``，不新增 final-state
  canonical_text ground truth。
- ``RetrievalExpectationV2`` 新增 typed ``identity_evidence_by_layer``
  （``layer_1`` / ``layer_2``，值域 ``IdentityEvidenceRequirement``），只作用于
  selected/excluded identity assertions，不控制 count-level / injection / ranking。
- V2 SEEDED non-FORGOTTEN record 的 ``canonical_text`` 是 required（UTF-8 JSON
  string、strip 后非空、长度不超过 LocalAgent formation 的
  ``FORMATION_MAX_CANONICAL_TEXT_CHARS``），保存前使用 trimmed value；缺失必须在
  dataset validation 阶段失败，早于 runtime provisioning，绝不回落到
  ``<logical_key>: <value>``。
- ``status == FORGOTTEN`` 的 seed 只允许唯一 tombstone shape：``canonical_text`` 必须
  absent/null，fixture writer 负责写入 ``[FORGOTTEN]`` + ``payload={}`` +
  ``superseded_by=None``；Dataset 本身不保存原始事实文本。
- 一个 ``StatefulMemoryScenarioV2`` 是 evaluation-only 的权威 Ground Truth 单元；
  ``truthfulness_origin`` 等共享 enum/常量从 V1 复用（V1 语义保持不动），但 V1 model
  不会接受 V2-only 字段，V2 model 也不会接受 V1 legacy seed fallback。

Loader dispatch：

- ``load_stateful_dataset`` 是 V1/V2 统一入口：按 ``dataset_schema_version`` 严格
  分派，V1 bytes -> V1 DTO，V2 bytes -> V2 DTO；未知 version 报
  ``EvaluationDatasetLoadError``。
- ``load_stateful_memory_dataset_v2`` / ``validate_stateful_dataset_v2`` 是 V2 专用入口。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from app.core.evaluation.dataset import (
    EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION,
    EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION_V2,
    EvaluationDatasetLoadError,
    _require_wire_id,
)
from app.core.evaluation.stateful_memory_dataset import (
    FormationDecision,
    FormationExpectation,
    GenerationExpectation,
    GenerationExpectationKind,
    InitialMemoryStateKind,
    InjectionExpectation,
    LifecycleOperation,
    MemoryRecordExpectation,
    MemoryStatus,
    MemoryType,
    PredicateClassification,
    PredicateExpectation,
    RegressionTag,
    TruthfulnessOrigin,
    content_digest,
    stateful_dataset_bytes,
)

# LocalAgent 当前 frozen 的 canonical text 长度上限；本仓库显式 mirror 该 contract，
# 并在 fixture 测试中与 LocalAgent 源码常量核对（不可复制无出处的 magic value）。
FORMATION_MAX_CANONICAL_TEXT_CHARS: int = 400

FORGET_SEED_TOMBSTONE_TEXT = "[FORGOTTEN]"


class IdentityEvidenceRequirement(StrEnum):
    """某一 evaluation layer 的 identity evidence 政策（严格 typed，禁止 bool/string）。"""

    REQUIRED = "REQUIRED"
    EXPECTED_LIMITATION = "EXPECTED_LIMITATION"


class SeededMemoryRecord(BaseModel):
    """V2 SEEDED fixture 使用的 typed seed DTO（final-state 语义与它隔离）。

    字段与当前 V1 seed 能力对齐（alias/agent/scope/type/logical_key/status/value/
    supersession relation/required），并新增 ``canonical_text``。
    """

    model_config = ConfigDict(extra="forbid")

    alias: StrictStr
    agent_id: StrictStr
    memory_scope: StrictStr
    memory_type: MemoryType = MemoryType.SEMANTIC
    logical_key: StrictStr | None = None
    status: MemoryStatus
    value: Any = None
    superseded_by_alias: StrictStr | None = None
    canonical_text: str | None = None
    required: bool = True

    @field_validator("alias")
    @classmethod
    def _alias(cls, value: str) -> str:
        return _require_wire_id(value, "alias")

    @field_validator("canonical_text")
    @classmethod
    def _canonical_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("canonical_text must be a string")
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("canonical_text must not be blank after strip")
        if len(trimmed) > FORMATION_MAX_CANONICAL_TEXT_CHARS:
            raise ValueError(f"canonical_text exceeds {FORMATION_MAX_CANONICAL_TEXT_CHARS} characters after strip")
        return trimmed

    @model_validator(mode="after")
    def _coherent(self) -> "SeededMemoryRecord":
        if self.status is MemoryStatus.FORGOTTEN:
            if self.canonical_text is not None:
                raise ValueError("FORGOTTEN seed record must not declare canonical_text")
            if self.superseded_by_alias is not None:
                raise ValueError("FORGOTTEN seed record must not declare superseded_by_alias")
        else:
            if self.canonical_text is None:
                raise ValueError(
                    "non-FORGOTTEN SEEDED record requires canonical_text; "
                    "the legacy <logical_key>: <value> fallback is not allowed in V2"
                )
        if self.superseded_by_alias is not None:
            if self.status is not MemoryStatus.SUPERSEDED:
                raise ValueError("superseded_by_alias requires status SUPERSEDED")
            if self.logical_key is None:
                raise ValueError("OPEN record must not be SUPERSEDED")
        return self


class InitialMemoryStateV2(BaseModel):
    """Scenario 的初始 Memory 状态声明（V2：SEEDED records 使用 SeededMemoryRecord）。"""

    model_config = ConfigDict(extra="forbid")

    kind: InitialMemoryStateKind = InitialMemoryStateKind.EMPTY
    records: list[SeededMemoryRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _seeded_records(self) -> "InitialMemoryStateV2":
        if self.kind is InitialMemoryStateKind.EMPTY and self.records:
            raise ValueError("EMPTY initial state must not declare records")
        if self.kind is InitialMemoryStateKind.SEEDED and not self.records:
            raise ValueError("SEEDED initial state requires at least one record")
        return self


class IdentityEvidenceByLayer(BaseModel):
    """某一 retrieval expectation 的两层 identity evidence 政策声明。"""

    model_config = ConfigDict(extra="forbid")

    layer_1: IdentityEvidenceRequirement
    layer_2: IdentityEvidenceRequirement


class RetrievalExpectationV2(BaseModel):
    """V2 期望的 retrieval selection（V1 字段 + typed identity evidence policy）。"""

    model_config = ConfigDict(extra="forbid")

    expected_selected: list[StrictStr] = Field(min_length=1)
    expected_excluded: list[StrictStr] = Field(default_factory=list)
    expected_ranked_order: list[StrictStr] = Field(default_factory=list)
    k: int = Field(default=5, ge=1)
    identity_evidence_by_layer: IdentityEvidenceByLayer | None = None

    @model_validator(mode="after")
    def _aliases(self) -> "RetrievalExpectationV2":
        aliases = [*self.expected_selected, *self.expected_excluded]
        if len(aliases) != len(set(aliases)):
            raise ValueError("retrieval aliases must be unique across selected and excluded")
        for alias in aliases:
            _require_wire_id(alias, "retrieval alias")
        if self.expected_selected and self.expected_excluded:
            overlap = set(self.expected_selected) & set(self.expected_excluded)
            if overlap:
                raise ValueError("retrieval alias must not be both selected and excluded")
        if self.expected_ranked_order:
            if len(self.expected_ranked_order) != len(set(self.expected_ranked_order)):
                raise ValueError("expected_ranked_order must be unique")
            if set(self.expected_ranked_order) != set(self.expected_selected):
                raise ValueError("expected_ranked_order must cover exactly the expected_selected aliases")
            for alias in self.expected_ranked_order:
                _require_wire_id(alias, "expected_ranked_order alias")
        return self


class StatefulMemoryStepV2(BaseModel):
    """Scenario 中一个 ordered step（V2 使用 RetrievalExpectationV2）。"""

    model_config = ConfigDict(extra="forbid")

    step_id: StrictStr
    agent_id: StrictStr
    memory_scope: StrictStr
    query: StrictStr = Field(min_length=1)
    expected_formation: FormationExpectation | None = None
    expected_lifecycle: LifecycleOperation | None = None
    expected_retrieval: RetrievalExpectationV2 | None = None
    expected_injection: InjectionExpectation | None = None
    required: bool = True

    @field_validator("step_id")
    @classmethod
    def _step_id(cls, value: str) -> str:
        return _require_wire_id(value, "step_id")

    @model_validator(mode="after")
    def _coherent(self) -> "StatefulMemoryStepV2":
        if self.expected_formation is not None:
            if self.expected_formation.decision is FormationDecision.REMEMBER and self.expected_lifecycle is None:
                raise ValueError("REMEMBER formation step requires expected_lifecycle")
            if (
                self.expected_formation.decision is FormationDecision.IGNORE
                and self.expected_lifecycle is not None
                and self.expected_lifecycle is not LifecycleOperation.POLICY_IGNORED
            ):
                raise ValueError(
                    "IGNORE formation step must not declare expected_lifecycle unless it is POLICY_IGNORED"
                )
        elif self.expected_lifecycle is not None:
            raise ValueError("expected_lifecycle requires a REMEMBER formation expectation")
        return self


class StatefulMemoryScenarioV2(BaseModel):
    """一个 evaluation-only stateful Memory scenario（V2 Ground Truth 单元）。"""

    model_config = ConfigDict(extra="forbid")

    scenario_id: StrictStr
    description: StrictStr = Field(min_length=1)
    truthfulness_origin: TruthfulnessOrigin
    tags: list[StrictStr] = Field(default_factory=list)
    regression_tags: list[RegressionTag] = Field(default_factory=list)
    initial_state: InitialMemoryStateV2 = Field(default_factory=InitialMemoryStateV2)
    steps: list[StatefulMemoryStepV2] = Field(min_length=1)
    expected_state: list[MemoryRecordExpectation] = Field(default_factory=list)
    generation_expectation: GenerationExpectation | None = None
    required: bool = True
    deterministic_denominator: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("scenario_id")
    @classmethod
    def _scenario_id(cls, value: str) -> str:
        return _require_wire_id(value, "scenario_id")

    @field_validator("tags")
    @classmethod
    def _tags(cls, values: list[str]) -> list[str]:
        checked = [_require_wire_id(value, "tag") for value in values]
        if len(checked) != len(set(checked)):
            raise ValueError("duplicate tag is not allowed")
        return checked

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        from app.core.evaluation.immutable import freeze_json

        if not value:
            return value
        try:
            freeze_json(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"metadata must be JSON-compatible: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _coherent(self) -> "StatefulMemoryScenarioV2":
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("duplicate step_id within a scenario is not allowed")
        aliases = [record.alias for record in self.expected_state]
        if len(aliases) != len(set(aliases)):
            raise ValueError("duplicate expected_state alias is not allowed")
        if self.truthfulness_origin is TruthfulnessOrigin.REAL_BAD_CASE and not self.regression_tags:
            raise ValueError("REAL_BAD_CASE requires at least one regression_tag")
        if (
            TruthfulnessOrigin.REAL_BAD_CASE in {self.truthfulness_origin}
            and not self.required
            and TruthfulnessOrigin.HUMAN_REVIEWED not in {self.truthfulness_origin}
            and self.deterministic_denominator
        ):
            raise ValueError(
                "non-required REAL_BAD_CASE without human freeze must not enter deterministic denominator"
            )
        selected_owner: dict[str, str] = {}
        excluded_aliases: set[str] = set()
        for step in self.steps:
            if step.expected_retrieval is None:
                continue
            selected = set(step.expected_retrieval.expected_selected)
            excluded = set(step.expected_retrieval.expected_excluded)
            overlap = selected & excluded
            if overlap:
                raise ValueError(
                    f"retrieval alias {sorted(overlap)} is both selected and excluded in step {step.step_id}"
                )
            for alias in selected:
                previous = selected_owner.setdefault(alias, step.step_id)
                if previous != step.step_id:
                    raise ValueError(
                        f"retrieval alias {alias} is selected in multiple steps ({previous}, {step.step_id})"
                    )
                if alias in excluded_aliases:
                    raise ValueError(f"retrieval alias {alias} is selected in one step and excluded in another")
            excluded_aliases.update(excluded)
        return self


class StatefulMemoryDatasetV2(BaseModel):
    """versioned Stateful Memory Scenario 集合（``stateful-memory-scenario.v2`` 测试资产）。"""

    model_config = ConfigDict(extra="forbid")

    dataset_schema_version: StrictStr
    dataset_id: StrictStr
    version: StrictStr
    name: StrictStr = Field(min_length=1)
    description: StrictStr | None = None
    scenarios: list[StatefulMemoryScenarioV2] = Field(min_length=1)
    content_digest: StrictStr | None = None

    @field_validator("dataset_schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value != EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION_V2:
            raise ValueError(
                f"unsupported stateful dataset schema version: {value}; "
                f"expected {EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION_V2}"
            )
        return value

    @field_validator("dataset_id", "version")
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return _require_wire_id(value, info.field_name)

    @model_validator(mode="after")
    def _coherent(self) -> "StatefulMemoryDatasetV2":
        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("duplicate scenario_id is not allowed")
        if self.dataset_id == "stateful_memory_v1" or self.version == "v1":
            raise ValueError(
                "V2 dataset must not mix V1 dataset identity (dataset_id=stateful_memory_v1 or version=v1)"
            )
        return self

    def __len__(self) -> int:
        return len(self.scenarios)


def validate_stateful_dataset_v2(payload: object) -> StatefulMemoryDatasetV2:
    """校验 V2 stateful dataset payload；失败抛 pydantic ValidationError。"""
    return StatefulMemoryDatasetV2.model_validate(payload)


def _load_raw_stateful_payload(path: str | Path) -> tuple[bytes, dict[str, Any]]:
    """读取并解析 UTF-8 JSON stateful dataset 文件；返回 (raw_bytes, payload)。"""
    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise EvaluationDatasetLoadError(f"cannot read stateful dataset file: {file_path}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvaluationDatasetLoadError(f"stateful dataset file is not UTF-8: {file_path}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvaluationDatasetLoadError(f"stateful dataset file is not valid JSON: {file_path}") from exc
    if not isinstance(payload, dict):
        raise EvaluationDatasetLoadError(f"stateful dataset file must contain a JSON object: {file_path}")
    return raw, payload


def load_stateful_memory_dataset_v2(path: str | Path) -> StatefulMemoryDatasetV2:
    """从 UTF-8 JSON 文件加载并严格校验 ``stateful-memory-scenario.v2`` dataset。

    Args:
        path: UTF-8 JSON dataset 文件路径。

    Returns:
        校验通过并携带 ``content_digest`` 的 StatefulMemoryDatasetV2。

    Raises:
        EvaluationDatasetLoadError: 文件不可读或不是合法 JSON object。
        pydantic.ValidationError: 内容不符合 V2 stateful dataset schema。
    """
    raw, payload = _load_raw_stateful_payload(path)
    dataset = StatefulMemoryDatasetV2.model_validate(payload)
    return dataset.model_copy(update={"content_digest": content_digest(raw)})


def load_stateful_dataset(path: str | Path) -> object:
    """V1/V2 统一 dispatch 入口：按 ``dataset_schema_version`` 严格分派。

    - ``stateful-memory-scenario.v1`` bytes -> V1 ``StatefulMemoryDataset``。
    - ``stateful-memory-scenario.v2`` bytes -> V2 ``StatefulMemoryDatasetV2``。
    - 未知 schema version -> ``EvaluationDatasetLoadError``。

    V1 与 V2 的 model 不共享可变结构，V1 不接受 V2-only 字段，V2 不接受 V1 legacy
    seed fallback；两条路径明确隔离。
    """
    raw, payload = _load_raw_stateful_payload(path)
    version = payload.get("dataset_schema_version")
    if version == EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION:
        from app.core.evaluation.stateful_memory_dataset import StatefulMemoryDataset

        dataset: object = StatefulMemoryDataset.model_validate(payload)
    elif version == EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION_V2:
        dataset = StatefulMemoryDatasetV2.model_validate(payload)
    else:
        raise EvaluationDatasetLoadError(
            f"unsupported stateful dataset schema version: {version}; "
            f"expected {EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION} or "
            f"{EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION_V2}"
        )
    return dataset.model_copy(update={"content_digest": content_digest(raw)})


__all__ = [
    "EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION_V2",
    "FORGET_SEED_TOMBSTONE_TEXT",
    "FORMATION_MAX_CANONICAL_TEXT_CHARS",
    "IdentityEvidenceByLayer",
    "IdentityEvidenceRequirement",
    "InitialMemoryStateV2",
    "RetrievalExpectationV2",
    "SeededMemoryRecord",
    "StatefulMemoryDatasetV2",
    "StatefulMemoryScenarioV2",
    "StatefulMemoryStepV2",
    "content_digest",
    "load_stateful_dataset",
    "load_stateful_memory_dataset_v2",
    "stateful_dataset_bytes",
    "validate_stateful_dataset_v2",
]
