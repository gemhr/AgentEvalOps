"""Stateful Memory Evaluation Dataset v1 —— WP5 typed scenario/step 契约。

本模块在既有 ``app.core.evaluation.dataset`` 之上扩展 ``stateful-memory-scenario.v1``
schema variant，不建立第二套 dataset loader / validator framework：

- 复用 EvaluationDataset 的 dataset identity/version、UTF-8 JSON loader、strict
  Pydantic validation 与 artifact snapshot 约定；
- 一个 ``StatefulMemoryScenario`` 是 evaluation-only 的权威 Ground Truth 单元，
  由 ordered ``StatefulMemoryStep`` 组成；step 不代表独立 Evaluation Case；
- ``truthfulness_origin`` 是 required enum；``REAL_BAD_CASE`` 可携带 regression
  标签，但不得把未确认根因当成已修复的 regression。

核心身份原则：期望值使用 canonical identity（``alias`` + agent/scope/type/logical_key
+ status + expected value），不依赖 runtime-generated ``memory_id`` 作为主要 Ground
Truth identity；runtime ``memory_id`` 只允许在 scenario execution 中与 expected
alias 绑定，用于 retrieval/injection 证据的 relation 对齐。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from app.core.evaluation.dataset import (
    EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION,
    EvaluationDatasetLoadError,
    _require_wire_id,
)

# LocalAgent 当前已注册的 code-owned registered predicates。
# 只有这些 predicate 可被标注 REGISTERED；其余逻辑键一律 OPEN。
REGISTERED_PREDICATES: frozenset[str] = frozenset(
    {
        "project.database",
        "project.package_manager",
        "engineering.public_network_allowed",
    }
)


class TruthfulnessOrigin(StrEnum):
    """Scenario Ground Truth 的真实性来源。

    - DETERMINISTIC_GROUND_TRUTH：受控/可重复、可进 hard gate denominator。
    - HUMAN_REVIEWED：人工冻结的事实或判定，同样可作为 deterministic denominator。
    - REAL_BAD_CASE：来自真实失败案例；必须保留真实不确定性，不得虚构已修复。
    - SYNTHETIC_CASE：人工构造的受控样例。
    """

    DETERMINISTIC_GROUND_TRUTH = "DETERMINISTIC_GROUND_TRUTH"
    HUMAN_REVIEWED = "HUMAN_REVIEWED"
    REAL_BAD_CASE = "REAL_BAD_CASE"
    SYNTHETIC_CASE = "SYNTHETIC_CASE"


class MemoryStatus(StrEnum):
    """LocalAgent 业务状态 enum（只读观察），用于 final-state/invariant 比较。"""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    FORGOTTEN = "FORGOTTEN"


class MemoryType(StrEnum):
    """v1 只支持 SEMANTIC Memory。"""

    SEMANTIC = "SEMANTIC"


class InitialMemoryStateKind(StrEnum):
    """Scenario 初始 Memory 状态类别。

    - EMPTY：该 Scenario DB 无任何 Memory rows。
    - SEEDED：允许 Layer 1 deterministic test-harness fixture seed（只允许在 scenario
      DB 创建后、任何 target invocation 前写入；artifact 标记 fixture_seeded=true）。
    """

    EMPTY = "EMPTY"
    SEEDED = "SEEDED"


class FormationDecision(StrEnum):
    """expected formation decision 单元。

    - REMEMBER：应持久化为一条 Memory。
    - IGNORE：应被形成策略忽略（temporary/assistant-only inference 等）。
    - BLOCKED：planning 在 formation 前失败，formation 无法判定（不是 false negative）。
    - NA：该 step 不声明 formation assertion。
    """

    REMEMBER = "REMEMBER"
    IGNORE = "IGNORE"
    BLOCKED = "BLOCKED"
    NA = "NA"


class PredicateClassification(StrEnum):
    """expected predicate 分类单元。

    REGISTERED 必须落在 ``REGISTERED_PREDICATES``；OPEN 的 logical_key 为 None；
    BLOCKED 表示分类无法判定（formation failed-closed / runtime blocked）。
    """

    REGISTERED = "REGISTERED"
    OPEN = "OPEN"
    BLOCKED = "BLOCKED"


class LifecycleOperation(StrEnum):
    """expected lifecycle operation 单元。

    INSERT / NO_CHANGE / SUPERSEDE / FORGET 是核心 exact unit；
    NOT_FOUND / ALREADY_FORGOTTEN / POLICY_IGNORED 只在 dataset 明确期望该 outcome
    时进入 denominator，否则实际与期望不一致即为 LIFECYCLE_OPERATION_MISMATCH。
    """

    INSERT = "INSERT"
    NO_CHANGE = "NO_CHANGE"
    SUPERSEDE = "SUPERSEDE"
    FORGET = "FORGET"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_FORGOTTEN = "ALREADY_FORGOTTEN"
    POLICY_IGNORED = "POLICY_IGNORED"


class GenerationExpectationKind(StrEnum):
    """Generation 期望类型：固定简单事实用 exact；开放表述仅 human adjudication。"""

    EXACT = "EXACT"
    OPEN = "OPEN"


class RegressionTag(StrEnum):
    """``REAL_BAD_CASE`` 的真实不确定性标签（不虚构已修复）。"""

    FIXED_REGRESSION = "FIXED_REGRESSION"
    RUNTIME_RELIABILITY_OBSERVATION = "RUNTIME_RELIABILITY_OBSERVATION"
    ROOT_CAUSE_NOT_CONFIRMED = "ROOT_CAUSE_NOT_CONFIRMED"


class InitialMemoryState(BaseModel):
    """Scenario 的初始 Memory 状态声明。"""

    model_config = ConfigDict(extra="forbid")

    kind: InitialMemoryStateKind = InitialMemoryStateKind.EMPTY
    records: list["MemoryRecordExpectation"] = Field(default_factory=list)

    @model_validator(mode="after")
    def _seeded_records(self) -> "InitialMemoryState":
        if self.kind is InitialMemoryStateKind.EMPTY and self.records:
            raise ValueError("EMPTY initial state must not declare records")
        if self.kind is InitialMemoryStateKind.SEEDED and not self.records:
            raise ValueError("SEEDED initial state requires at least one record")
        return self


class PredicateExpectation(BaseModel):
    """期望的 predicate 分类与 canonical ID。"""

    model_config = ConfigDict(extra="forbid")

    classification: PredicateClassification
    predicate_id: StrictStr | None = None

    @model_validator(mode="after")
    def _coherent(self) -> "PredicateExpectation":
        if self.classification is PredicateClassification.REGISTERED:
            if self.predicate_id is None:
                raise ValueError("REGISTERED predicate requires predicate_id")
            if self.predicate_id not in REGISTERED_PREDICATES:
                raise ValueError(f"unknown registered predicate: {self.predicate_id}")
        elif self.predicate_id is not None:
            raise ValueError(f"{self.classification.value} predicate must not declare predicate_id")
        return self


class FormationExpectation(BaseModel):
    """期望的 formation decision（含 predicate 分类）。"""

    model_config = ConfigDict(extra="forbid")

    decision: FormationDecision
    predicate: PredicateExpectation | None = None

    @model_validator(mode="after")
    def _coherent(self) -> "FormationExpectation":
        if self.decision is FormationDecision.REMEMBER:
            if self.predicate is None:
                raise ValueError("REMEMBER formation requires a predicate expectation")
        elif self.predicate is not None:
            raise ValueError(f"{self.decision.value} formation must not declare a predicate expectation")
        return self


class RetrievalExpectation(BaseModel):
    """期望的 retrieval selection（canonical alias 单元）。"""

    model_config = ConfigDict(extra="forbid")

    expected_selected: list[StrictStr] = Field(min_length=1)
    expected_excluded: list[StrictStr] = Field(default_factory=list)
    expected_ranked_order: list[StrictStr] = Field(default_factory=list)
    k: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def _aliases(self) -> "RetrievalExpectation":
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


class InjectionExpectation(BaseModel):
    """期望的 Planner ContextBuilder injection（绝不把 supplied 当作 injected）。"""

    model_config = ConfigDict(extra="forbid")

    planner_context_record_count: int | None = Field(default=None, ge=0)
    planning_injected_expected: bool = Field(default=True)


class MemoryRecordExpectation(BaseModel):
    """Final-state 中一个 canonical expected record（canonical identity 单元）。"""

    model_config = ConfigDict(extra="forbid")

    alias: StrictStr
    agent_id: StrictStr
    memory_scope: StrictStr
    memory_type: MemoryType = MemoryType.SEMANTIC
    logical_key: StrictStr | None = None
    status: MemoryStatus
    value: Any = None
    superseded_by_alias: StrictStr | None = None
    required: bool = True

    @field_validator("alias")
    @classmethod
    def _alias(cls, value: str) -> str:
        return _require_wire_id(value, "alias")

    @model_validator(mode="after")
    def _coherent(self) -> "MemoryRecordExpectation":
        if self.status is MemoryStatus.FORGOTTEN:
            if self.superseded_by_alias is not None:
                raise ValueError("FORGOTTEN record must not declare superseded_by_alias")
        elif self.logical_key is None and self.superseded_by_alias is not None:
            raise ValueError("OPEN (logical_key=None) record must not declare superseded_by_alias")
        if self.superseded_by_alias is not None:
            if self.status is not MemoryStatus.SUPERSEDED:
                raise ValueError("superseded_by_alias requires status SUPERSEDED")
            if self.logical_key is None:
                raise ValueError("OPEN record must not be SUPERSEDED")
        return self


class GenerationExpectation(BaseModel):
    """独立的 optional generation 期望（不覆盖 runtime correctness）。"""

    model_config = ConfigDict(extra="forbid")

    kind: GenerationExpectationKind
    expected_value: StrictStr | None = None
    adjudication: TruthfulnessOrigin = TruthfulnessOrigin.HUMAN_REVIEWED

    @model_validator(mode="after")
    def _coherent(self) -> "GenerationExpectation":
        if self.kind is GenerationExpectationKind.EXACT:
            if self.expected_value is None:
                raise ValueError("EXACT generation expectation requires expected_value")
            if self.adjudication is not TruthfulnessOrigin.HUMAN_REVIEWED:
                raise ValueError("EXACT expectation must be HUMAN_REVIEWED")
        elif self.expected_value is not None:
            raise ValueError("OPEN generation expectation must not declare expected_value")
        return self


class StatefulMemoryStep(BaseModel):
    """Scenario 中一个 ordered step；对应一次 HTTP execution attempt。"""

    model_config = ConfigDict(extra="forbid")

    step_id: StrictStr
    agent_id: StrictStr
    memory_scope: StrictStr
    query: StrictStr = Field(min_length=1)
    expected_formation: FormationExpectation | None = None
    expected_lifecycle: LifecycleOperation | None = None
    expected_retrieval: RetrievalExpectation | None = None
    expected_injection: InjectionExpectation | None = None
    required: bool = True

    @field_validator("step_id")
    @classmethod
    def _step_id(cls, value: str) -> str:
        return _require_wire_id(value, "step_id")

    @model_validator(mode="after")
    def _coherent(self) -> "StatefulMemoryStep":
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


class StatefulMemoryScenario(BaseModel):
    """一个 evaluation-only stateful Memory scenario（dataset Ground Truth 单元）。"""

    model_config = ConfigDict(extra="forbid")

    scenario_id: StrictStr
    description: StrictStr = Field(min_length=1)
    truthfulness_origin: TruthfulnessOrigin
    tags: list[StrictStr] = Field(default_factory=list)
    regression_tags: list[RegressionTag] = Field(default_factory=list)
    initial_state: InitialMemoryState = Field(default_factory=InitialMemoryState)
    steps: list[StatefulMemoryStep] = Field(min_length=1)
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
    def _coherent(self) -> "StatefulMemoryScenario":
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


class StatefulMemoryDataset(BaseModel):
    """versioned Stateful Memory Scenario 集合（测试资产，非业务数据）。"""

    model_config = ConfigDict(extra="forbid")

    dataset_schema_version: StrictStr
    dataset_id: StrictStr
    version: StrictStr
    name: StrictStr = Field(min_length=1)
    description: StrictStr | None = None
    scenarios: list[StatefulMemoryScenario] = Field(min_length=1)
    content_digest: StrictStr | None = None

    @field_validator("dataset_schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value != EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported stateful dataset schema version: {value}; "
                f"expected {EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION}"
            )
        return value

    @field_validator("dataset_id", "version")
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return _require_wire_id(value, info.field_name)

    @model_validator(mode="after")
    def _coherent(self) -> "StatefulMemoryDataset":
        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("duplicate scenario_id is not allowed")
        return self

    def __len__(self) -> int:
        return len(self.scenarios)


def content_digest(raw_bytes: bytes) -> str:
    """返回 dataset 原始 UTF-8 bytes 的 sha256 digest（不可变身份快照）。"""
    return f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"


def validate_stateful_dataset(payload: object) -> StatefulMemoryDataset:
    """校验 stateful dataset payload；失败抛 pydantic ValidationError。"""
    return StatefulMemoryDataset.model_validate(payload)


def load_stateful_memory_dataset(path: str | Path) -> object:
    """从 UTF-8 JSON 文件加载并严格校验 stateful-memory-scenario dataset（V1/V2 dispatch）。

    按 ``dataset_schema_version`` 严格分派：``stateful-memory-scenario.v1`` bytes 返回
    ``StatefulMemoryDataset``（V1 compatibility path）；``stateful-memory-scenario.v2``
    bytes 返回 ``StatefulMemoryDatasetV2``（严格 V2 schema）。V1 与 V2 的 model 不共享
    可变结构；V2-only 字段出现在 V1 中必须 validation error，V1 legacy seed fallback
    不得流入 V2 path。

    Args:
        path: UTF-8 JSON dataset 文件路径。

    Returns:
        校验通过并携带 ``content_digest`` 的 V1 或 V2 stateful dataset model。

    Raises:
        EvaluationDatasetLoadError: 文件不可读、不是合法 JSON object，或 schema version
            既不是 v1 也不是 v2。
        pydantic.ValidationError: 内容不符合对应 schema version 的 stateful contract。
    """
    from app.core.evaluation.stateful_memory_dataset_v2 import load_stateful_dataset

    return load_stateful_dataset(path)


def stateful_dataset_bytes(dataset: StatefulMemoryDataset) -> bytes:
    """把 dataset 投影为稳定的 canonical UTF-8 JSON bytes（用于 digest/比较）。"""
    data = dataset.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"content_digest"},
    )
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION",
    "FormationDecision",
    "FormationExpectation",
    "GenerationExpectation",
    "GenerationExpectationKind",
    "InitialMemoryState",
    "InitialMemoryStateKind",
    "InjectionExpectation",
    "LifecycleOperation",
    "MemoryRecordExpectation",
    "MemoryStatus",
    "MemoryType",
    "PredicateClassification",
    "PredicateExpectation",
    "REGISTERED_PREDICATES",
    "RegressionTag",
    "RetrievalExpectation",
    "StatefulMemoryDataset",
    "StatefulMemoryScenario",
    "StatefulMemoryStep",
    "TruthfulnessOrigin",
    "content_digest",
    "load_stateful_memory_dataset",
    "stateful_dataset_bytes",
    "validate_stateful_dataset",
]
