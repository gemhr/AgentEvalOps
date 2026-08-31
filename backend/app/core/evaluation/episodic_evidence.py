"""WP6-E Episodic Layer1 evidence DTO（严格消费 LocalAgent v3 private projection）。

所有 parse 都 fail-closed：缺失/畸形字段抛 ``EpisodicEvidenceError``，绝不宽松
``dict.get("whatever")`` 消费关键 Ground Truth evidence。证据 authority 分离：

- selected = capture selection（``MemoryContextBundle.episodic_evidence`` 投影）；
- supplied = capture supplied（``MemoryContextBundle.episodic_records`` 投影）；
- injected = capture injected（``ContextBuilder.included_items`` 中
  ``EPISODIC_MEMORY_RETRIEVAL``）；
- runtime = ``runtime_receipt``（terminal/delivery/step/formation digest）；
- formation / fixture / replay = 对应 receipt；
- journal = content-minimized count-level 事件；
- final state = 只读 SQLite Episode projection（只做 persistence，绝不作 selection oracle）。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from app.core.evaluation.immutable import require_text
from app.core.evaluation.stateful_assertion import EvaluationLayer
from app.core.evaluation.stateful_journal import JournalEvents, JournalStepFacts

CAPTURE_SCHEMA_VERSION: Final[str] = "episodic-evaluation-capture.v1"
EVALUATION_EXECUTE_V3_PROTOCOL_VERSION: Final[str] = "localagent-episodic-evaluation-execute.v1"


class EpisodicEvidenceError(ValueError):
    """证据 parse / 关联失败（fail closed）。"""


class EpisodicInjectionTarget(StrEnum):
    """LocalAgent ContextBuilder capture 的固定观察目标。"""

    PLANNING = "PLANNING"
    DIRECT_ENTRY = "DIRECT_ENTRY"


class EpisodicContextSourceType(StrEnum):
    """AgentEvalOps 侧规范化后的 Context source 语义枚举。"""

    EPISODIC_MEMORY_RETRIEVAL = "EPISODIC_MEMORY_RETRIEVAL"


class EpisodicContextTrustLevel(StrEnum):
    """AgentEvalOps 侧规范化后的 Context trust 语义枚举。"""

    USER_CONTENT = "USER_CONTENT"
    SYSTEM_CONTENT = "SYSTEM_CONTENT"


def validate_runtime_uuid_binding(record: "EpisodicRunEvidence") -> None:
    """所有 target evidence 必须绑定请求生成的同一 runtime UUID。"""
    expected = record.actual_runtime_run_id
    values = {
        "target_run_id": record.target_run_id,
        "runtime_receipt.run_id": record.runtime_receipt.run_id if record.runtime_receipt else None,
        "formation_receipt.run_id": record.formation_receipt.run_id if record.formation_receipt else None,
        "replay_receipt.run_id": record.replay_receipt.run_id if record.replay_receipt else None,
    }
    if record.capture is not None:
        values["capture.run_id"] = record.capture.run_id
    for source, run_id in values.items():
        if run_id is not None and run_id != expected:
            raise EpisodicEvidenceError(f"EVIDENCE_CAPTURE: {source} does not match actual_runtime_run_id")


def _require_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EpisodicEvidenceError(f"{where} must be a JSON object")
    return value


def _require_text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EpisodicEvidenceError(f"{where} must be a non-empty string")
    return value


def _optional_text(value: object, where: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, where)


def _require_int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EpisodicEvidenceError(f"{where} must be an integer")
    return value


def _require_bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise EpisodicEvidenceError(f"{where} must be a boolean")
    return value


def _require_str_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise EpisodicEvidenceError(f"{where} must be a list")
    return tuple(_require_text(item, f"{where} item") for item in value)


@dataclass(frozen=True, slots=True)
class EpisodicSelectionItemEvidence:
    """capture selection 中一条 selection item。"""

    memory_id: str
    rank: int
    lexical_match_score: int
    selected: bool
    drop_reason: str | None = None

    def __post_init__(self) -> None:
        require_text(self.memory_id, "selection.memory_id")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise EpisodicEvidenceError("selection.rank must be a positive integer")
        if (
            isinstance(self.lexical_match_score, bool)
            or not isinstance(self.lexical_match_score, int)
            or self.lexical_match_score < 0
        ):
            raise EpisodicEvidenceError("selection.lexical_match_score must be a non-negative integer")

    @classmethod
    def from_wire(cls, value: object, where: str) -> "EpisodicSelectionItemEvidence":
        """Strictly parse ``value`` into a typed DTO (fail closed)."""
        item = _require_mapping(value, where)
        return cls(
            memory_id=_require_text(item.get("memory_id"), f"{where}.memory_id"),
            rank=_require_int(item.get("rank"), f"{where}.rank"),
            lexical_match_score=_require_int(item.get("lexical_match_score"), f"{where}.lexical_match_score"),
            selected=_require_bool(item.get("selected"), f"{where}.selected"),
            drop_reason=_optional_text(item.get("drop_reason"), f"{where}.drop_reason"),
        )


@dataclass(frozen=True, slots=True)
class EpisodicSelectionEvidence:
    """capture selection：candidate_count + 全部 scored selection items。"""

    candidate_count: int
    selected: tuple[EpisodicSelectionItemEvidence, ...]

    def __post_init__(self) -> None:
        if isinstance(self.candidate_count, bool) or not isinstance(self.candidate_count, int):
            raise EpisodicEvidenceError("selection.candidate_count must be an integer")
        if self.candidate_count < 0:
            raise EpisodicEvidenceError("selection.candidate_count must be non-negative")
        object.__setattr__(self, "selected", tuple(self.selected))

    @classmethod
    def from_wire(cls, value: object, where: str) -> "EpisodicSelectionEvidence":
        """Strictly parse ``value`` into a typed DTO (fail closed)."""
        item = _require_mapping(value, where)
        raw = item.get("selected")
        if not isinstance(raw, list):
            raise EpisodicEvidenceError(f"{where}.selected must be a list")
        return cls(
            candidate_count=_require_int(item.get("candidate_count"), f"{where}.candidate_count"),
            selected=tuple(
                EpisodicSelectionItemEvidence.from_wire(entry, f"{where}.selected[{index}]")
                for index, entry in enumerate(raw)
            ),
        )

    @property
    def selected_memory_ids(self) -> tuple[str, ...]:
        """Return the computed property value."""
        return tuple(item.memory_id for item in self.selected if item.selected)

    @property
    def selected_count(self) -> int:
        """Return the computed property value."""
        return sum(1 for item in self.selected if item.selected)

    def score_for(self, memory_id: str) -> int | None:
        """Implement the ``score_for`` contract (typed, fail-closed)."""
        for item in self.selected:
            if item.memory_id == memory_id:
                return item.lexical_match_score
        return None

    def selection_item_for(self, memory_id: str) -> EpisodicSelectionItemEvidence | None:
        """Implement the ``selection_item_for`` contract (typed, fail-closed)."""
        for item in self.selected:
            if item.memory_id == memory_id:
                return item
        return None


@dataclass(frozen=True, slots=True)
class EpisodicSuppliedEvidence:
    """capture supplied：supplied episodic memory_ids 与 record_count。"""

    episodic_memory_ids: tuple[str, ...]
    record_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "episodic_memory_ids", tuple(self.episodic_memory_ids))
        if isinstance(self.record_count, bool) or not isinstance(self.record_count, int):
            raise EpisodicEvidenceError("supplied.record_count must be an integer")
        if self.record_count < 0 or self.record_count != len(self.episodic_memory_ids):
            raise EpisodicEvidenceError("supplied.record_count must equal episodic_memory_ids length")

    @classmethod
    def from_wire(cls, value: object, where: str) -> "EpisodicSuppliedEvidence":
        """Strictly parse ``value`` into a typed DTO (fail closed)."""
        item = _require_mapping(value, where)
        ids = _require_str_list(item.get("episodic_memory_ids"), f"{where}.episodic_memory_ids")
        count = _require_int(item.get("record_count"), f"{where}.record_count")
        return cls(episodic_memory_ids=ids, record_count=count)


@dataclass(frozen=True, slots=True)
class EpisodicInjectedEvidence:
    """capture injected：一次 ContextBuilder acceptance 的 episodic-only 投影。"""

    target: EpisodicInjectionTarget
    episodic_memory_ids: tuple[str, ...]
    context_record_count: int
    source_type: EpisodicContextSourceType
    trust_level: EpisodicContextTrustLevel

    def __post_init__(self) -> None:
        object.__setattr__(self, "episodic_memory_ids", tuple(self.episodic_memory_ids))
        if (
            isinstance(self.context_record_count, bool)
            or not isinstance(self.context_record_count, int)
            or self.context_record_count < 0
        ):
            raise EpisodicEvidenceError("injected.context_record_count must be a non-negative integer")
        if self.context_record_count != len(self.episodic_memory_ids):
            raise EpisodicEvidenceError("injected.context_record_count must equal episodic_memory_ids length")

    @classmethod
    def from_wire(cls, value: object, where: str) -> "EpisodicInjectedEvidence":
        """Strictly parse ``value`` into a typed DTO (fail closed)."""
        item = _require_mapping(value, where)
        ids = _require_str_list(item.get("episodic_memory_ids"), f"{where}.episodic_memory_ids")
        source_type = _require_text(item.get("source_type"), f"{where}.source_type")
        trust_level = _require_text(item.get("trust_level"), f"{where}.trust_level")
        source_map = {
            "episodic_memory_retrieval": EpisodicContextSourceType.EPISODIC_MEMORY_RETRIEVAL,
            "EPISODIC_MEMORY_RETRIEVAL": EpisodicContextSourceType.EPISODIC_MEMORY_RETRIEVAL,
        }
        trust_map = {
            "user_content": EpisodicContextTrustLevel.USER_CONTENT,
            "USER_CONTENT": EpisodicContextTrustLevel.USER_CONTENT,
            "system_content": EpisodicContextTrustLevel.SYSTEM_CONTENT,
            "SYSTEM_CONTENT": EpisodicContextTrustLevel.SYSTEM_CONTENT,
        }
        if source_type not in source_map or trust_level not in trust_map:
            raise EpisodicEvidenceError(f"{where} has unsupported LocalAgent semantic enum wire value")
        try:
            target = EpisodicInjectionTarget(_require_text(item.get("target"), f"{where}.target"))
        except ValueError as exc:
            raise EpisodicEvidenceError(f"{where}.target has unsupported ContextBuilder target") from exc
        return cls(
            target=target,
            episodic_memory_ids=ids,
            context_record_count=_require_int(item.get("context_record_count"), f"{where}.context_record_count"),
            source_type=source_map[source_type],
            trust_level=trust_map[trust_level],
        )


@dataclass(frozen=True, slots=True)
class EpisodicCaptureEvidence:
    """capture artifact 的严格 typed 解析（``episodic-evaluation-capture.v1``）。"""

    schema_version: str
    run_id: str
    capture_outcome: str
    selection: EpisodicSelectionEvidence | None
    supplied: EpisodicSuppliedEvidence | None
    injected: tuple[EpisodicInjectedEvidence, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CAPTURE_SCHEMA_VERSION:
            raise EpisodicEvidenceError(f"unsupported episodic capture schema_version: {self.schema_version!r}")
        require_text(self.run_id, "capture.run_id")
        if self.capture_outcome not in {"COMPLETE", "PARTIAL", "FAILED"}:
            raise EpisodicEvidenceError(f"unknown capture_outcome: {self.capture_outcome!r}")
        object.__setattr__(self, "injected", tuple(self.injected))

    @classmethod
    def from_wire(cls, value: object) -> "EpisodicCaptureEvidence":
        """Strictly parse ``value`` into a typed DTO (fail closed)."""
        item = _require_mapping(value, "episodic_capture")
        selection = (
            EpisodicSelectionEvidence.from_wire(item["selection"], "episodic_capture.selection")
            if item.get("selection") is not None
            else None
        )
        supplied = (
            EpisodicSuppliedEvidence.from_wire(item["supplied"], "episodic_capture.supplied")
            if item.get("supplied") is not None
            else None
        )
        raw_injected = item.get("injected")
        if not isinstance(raw_injected, list):
            raise EpisodicEvidenceError("episodic_capture.injected must be a list")
        return cls(
            schema_version=_require_text(item.get("schema_version"), "episodic_capture.schema_version"),
            run_id=_require_text(item.get("run_id"), "episodic_capture.run_id"),
            capture_outcome=_require_text(item.get("capture_outcome"), "episodic_capture.capture_outcome"),
            selection=selection,
            supplied=supplied,
            injected=tuple(
                EpisodicInjectedEvidence.from_wire(entry, f"episodic_capture.injected[{index}]")
                for index, entry in enumerate(raw_injected)
            ),
        )

    @property
    def injected_planning(self) -> tuple[EpisodicInjectedEvidence, ...]:
        """Return the computed property value."""
        return self.injected_for_target(EpisodicInjectionTarget.PLANNING)

    def injected_for_target(self, target: EpisodicInjectionTarget) -> tuple[EpisodicInjectedEvidence, ...]:
        """返回一个明确 ContextBuilder target 的 capture，禁止跨 target 聚合。"""
        return tuple(item for item in self.injected if item.target is target)


@dataclass(frozen=True, slots=True)
class EpisodicFormationReceiptEvidence:
    """formation / replay receipt 的严格解析。"""

    run_id: str
    outcome: str
    memory_id: str | None
    lesson_status: str
    safe_reason: str | None = None

    def __post_init__(self) -> None:
        require_text(self.run_id, "formation_receipt.run_id")
        if self.outcome not in {"CREATED", "REUSED", "SKIPPED", "FAILED"}:
            raise EpisodicEvidenceError(f"unknown formation outcome: {self.outcome!r}")
        if self.memory_id is not None:
            require_text(self.memory_id, "formation_receipt.memory_id")
        require_text(self.lesson_status, "formation_receipt.lesson_status")

    @classmethod
    def from_wire(cls, value: object, where: str) -> "EpisodicFormationReceiptEvidence":
        """Strictly parse ``value`` into a typed DTO (fail closed)."""
        item = _require_mapping(value, where)
        return cls(
            run_id=_require_text(item.get("run_id"), f"{where}.run_id"),
            outcome=_require_text(item.get("outcome"), f"{where}.outcome"),
            memory_id=_optional_text(item.get("memory_id"), f"{where}.memory_id"),
            lesson_status=_require_text(item.get("lesson_status"), f"{where}.lesson_status"),
            safe_reason=_optional_text(item.get("safe_reason"), f"{where}.safe_reason"),
        )


@dataclass(frozen=True, slots=True)
class EpisodicFixtureReceiptEvidence:
    """fixture installation receipt 的严格解析。"""

    fixture_ref: str
    memory_id: str
    origin_run_id: str
    origin_kind: str
    memory_scope: str

    def __post_init__(self) -> None:
        require_text(self.fixture_ref, "fixture_receipt.fixture_ref")
        require_text(self.memory_id, "fixture_receipt.memory_id")
        require_text(self.origin_run_id, "fixture_receipt.origin_run_id")
        require_text(self.origin_kind, "fixture_receipt.origin_kind")
        require_text(self.memory_scope, "fixture_receipt.memory_scope")
        if self.origin_kind != "DATASET_CONTROLLED_INITIAL_FIXTURE":
            raise EpisodicEvidenceError("fixture receipt origin_kind must be DATASET_CONTROLLED_INITIAL_FIXTURE")

    @classmethod
    def from_wire(cls, value: object, where: str) -> "EpisodicFixtureReceiptEvidence":
        """Strictly parse ``value`` into a typed DTO (fail closed)."""
        item = _require_mapping(value, where)
        return cls(
            fixture_ref=_require_text(item.get("fixture_ref"), f"{where}.fixture_ref"),
            memory_id=_require_text(item.get("memory_id"), f"{where}.memory_id"),
            origin_run_id=_require_text(item.get("origin_run_id"), f"{where}.origin_run_id"),
            origin_kind=_require_text(item.get("origin_kind"), f"{where}.origin_kind"),
            memory_scope=_require_text(item.get("memory_scope"), f"{where}.memory_scope"),
        )


@dataclass(frozen=True, slots=True)
class EpisodicRuntimeReceiptEvidence:
    """runtime receipt 的严格解析（deterministic runtime evidence，无正文）。"""

    run_id: str
    plan_goal: str | None
    step_names: tuple[str, ...]
    step_statuses: tuple[str, ...]
    terminal_status: str
    stop_reason: str
    delivery_status: str
    formed_memory_id: str | None
    formation_outcome: str | None
    canonical_text_sha256: str | None

    def __post_init__(self) -> None:
        require_text(self.run_id, "runtime_receipt.run_id")
        require_text(self.terminal_status, "runtime_receipt.terminal_status")
        require_text(self.stop_reason, "runtime_receipt.stop_reason")
        require_text(self.delivery_status, "runtime_receipt.delivery_status")
        object.__setattr__(self, "step_names", tuple(self.step_names))
        object.__setattr__(self, "step_statuses", tuple(self.step_statuses))
        if len(self.step_names) != len(self.step_statuses):
            raise EpisodicEvidenceError("runtime_receipt step_names/step_statuses length mismatch")
        if self.canonical_text_sha256 is not None and len(self.canonical_text_sha256) != 64:
            raise EpisodicEvidenceError("runtime_receipt canonical_text_sha256 must be a SHA-256 digest")

    @classmethod
    def from_wire(cls, value: object, where: str) -> "EpisodicRuntimeReceiptEvidence":
        """Strictly parse ``value`` into a typed DTO (fail closed)."""
        item = _require_mapping(value, where)
        return cls(
            run_id=_require_text(item.get("run_id"), f"{where}.run_id"),
            plan_goal=_optional_text(item.get("plan_goal"), f"{where}.plan_goal"),
            step_names=_require_str_list(item.get("step_names"), f"{where}.step_names"),
            step_statuses=_require_str_list(item.get("step_statuses"), f"{where}.step_statuses"),
            terminal_status=_require_text(item.get("terminal_status"), f"{where}.terminal_status"),
            stop_reason=_require_text(item.get("stop_reason"), f"{where}.stop_reason"),
            delivery_status=_require_text(item.get("delivery_status"), f"{where}.delivery_status"),
            formed_memory_id=_optional_text(item.get("formed_memory_id"), f"{where}.formed_memory_id"),
            formation_outcome=_optional_text(item.get("formation_outcome"), f"{where}.formation_outcome"),
            canonical_text_sha256=_optional_text(item.get("canonical_text_sha256"), f"{where}.canonical_text_sha256"),
        )


class RunExecutionStatus(StrEnum):
    """一次 Run 的 evaluation-side execution status（与 target terminal 分离）。"""

    EXECUTED = "EXECUTED"
    INFRA_FAILURE = "INFRA_FAILURE"
    RUNTIME_FAILURE = "RUNTIME_FAILURE"


@dataclass(frozen=True, slots=True)
class EpisodicRunEvidence:
    """一次 Run 的全部 evaluation evidence（runner 构建的 typed Run artifact 输入）。"""

    scenario_id: str
    case_code: str
    dataset_run_id: str
    actual_runtime_run_id: str
    execution_status: RunExecutionStatus
    terminal_status: str | None
    delivery_status: str | None
    evaluation_controls_sent: tuple[str, ...] = ()
    target_run_id: str | None = None
    target_status: str | None = None
    target_stop_reason: str | None = None
    target_error_code: str | None = None
    target_safe_message: str | None = None
    evaluation_control_status: str | None = None
    evaluation_error_code: str | None = None
    capture_status: str | None = None
    capture_error_code: str | None = None
    formation_receipt: EpisodicFormationReceiptEvidence | None = None
    fixture_receipt: EpisodicFixtureReceiptEvidence | None = None
    replay_receipt: EpisodicFormationReceiptEvidence | None = None
    capture: EpisodicCaptureEvidence | None = None
    runtime_receipt: EpisodicRuntimeReceiptEvidence | None = None
    journal: JournalEvents | None = None
    step_facts: JournalStepFacts | None = None
    journal_error: str | None = None
    infra_status: str | None = None

    def __post_init__(self) -> None:
        require_text(self.scenario_id, "run_evidence.scenario_id")
        require_text(self.case_code, "run_evidence.case_code")
        require_text(self.dataset_run_id, "run_evidence.dataset_run_id")
        require_text(self.actual_runtime_run_id, "run_evidence.actual_runtime_run_id")
        if not isinstance(self.execution_status, RunExecutionStatus):
            raise TypeError("run_evidence.execution_status must be RunExecutionStatus")
        object.__setattr__(self, "evaluation_controls_sent", tuple(self.evaluation_controls_sent))


@dataclass(frozen=True, slots=True)
class EpisodicScenarioEvaluationEvidence:
    """一次 scenario 的全部只读 evidence（由 runner 构建）。"""

    scenario: object
    run_evidence_by_dataset_run_id: dict[str, EpisodicRunEvidence] = field(default_factory=dict, compare=False)
    identity_map: object = None
    final_projection: tuple[object, ...] = field(default_factory=tuple, compare=False)
    evaluation_layer: EvaluationLayer = EvaluationLayer.LAYER_1_DETERMINISTIC

    def __post_init__(self) -> None:
        from app.core.evaluation.episodic_dataset import EpisodicScenario
        from app.core.evaluation.episodic_identity import EpisodicIdentityMap

        if not isinstance(self.scenario, EpisodicScenario):
            raise TypeError("evidence scenario must be EpisodicScenario")
        if not isinstance(self.evaluation_layer, EvaluationLayer):
            raise TypeError("evidence evaluation_layer must be EvaluationLayer")
        if self.identity_map is None:
            object.__setattr__(self, "identity_map", EpisodicIdentityMap())


__all__ = [
    "CAPTURE_SCHEMA_VERSION",
    "EVALUATION_EXECUTE_V3_PROTOCOL_VERSION",
    "EpisodicCaptureEvidence",
    "EpisodicContextSourceType",
    "EpisodicContextTrustLevel",
    "EpisodicEvidenceError",
    "EpisodicFixtureReceiptEvidence",
    "EpisodicFormationReceiptEvidence",
    "EpisodicInjectedEvidence",
    "EpisodicInjectionTarget",
    "EpisodicRunEvidence",
    "EpisodicRuntimeReceiptEvidence",
    "EpisodicScenarioEvaluationEvidence",
    "EpisodicSelectionEvidence",
    "EpisodicSelectionItemEvidence",
    "EpisodicSuppliedEvidence",
    "RunExecutionStatus",
    "validate_runtime_uuid_binding",
]
