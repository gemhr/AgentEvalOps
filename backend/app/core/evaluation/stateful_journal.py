"""WP5 隔离 runtime journal 的只读证据采集与 typed event mapping。

从隔离 LocalAgent runtime 的 ``runtime_event_journal`` SQLite 文件只读读取，把
MEMORY_FORMATION_COMPLETED / MEMORY_LIFECYCLE_RESOLVED / MEMORY_RETRIEVAL_COMPLETED
映射为 typed payload。解析必须 fail-closed：missing required event、malformed
payload 或 schema 不匹配都必须是证据失败（BLOCKED），绝不默认 0 当作 PASS。

注意：事件不携带 Memory 正文 / raw query / payload；本模块不发明任何
LocalAgent 未 journal 的字段。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from app.core.evaluation.immutable import require_text

_JOURNAL_TABLE = "runtime_event_journal"

MEMORY_FORMATION_COMPLETED = "MEMORY_FORMATION_COMPLETED"
MEMORY_LIFECYCLE_RESOLVED = "MEMORY_LIFECYCLE_RESOLVED"
MEMORY_RETRIEVAL_COMPLETED = "MEMORY_RETRIEVAL_COMPLETED"

#: Journal ``RuntimeEvent.step_id`` 身份断言消费的 step 事件（canonical step identity）。
STEP_STARTED_EVENT = "STEP_STARTED"
STEP_COMPLETED_EVENT = "STEP_COMPLETED"
_SUPPORTED_STEP_EVENT_TYPES = frozenset({STEP_STARTED_EVENT, STEP_COMPLETED_EVENT})

_REQUIRED_COLUMNS = frozenset({"event_id", "run_id", "sequence", "event_type", "safe_payload"})
_SUPPORTED_MEMORY_EVENT_SCHEMA_VERSIONS = frozenset({1, "v1"})


class JournalEvidenceError(ValueError):
    """journal 读取/解析失败（fail closed）。"""


def _validate_memory_event_schema_version(value: int | str) -> int | str:
    """只接受已审计的 LocalAgent v1 event schema。"""
    if value not in _SUPPORTED_MEMORY_EVENT_SCHEMA_VERSIONS:
        raise ValueError("unsupported memory event schema_version")
    return value


class _FormationPayload(BaseModel):
    """MEMORY_FORMATION_COMPLETED 的最小严格 payload。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt | StrictStr
    formation_method: StrictStr
    status: StrictStr
    safe_error_code: StrictStr | None = None
    proposed_count: int
    accepted_count: int
    ignored_count: int
    persisted_count: int
    reused_count: int
    failed_count: int
    candidate_outcomes: StrictStr = "NONE"
    exchange_id: StrictStr | None = None
    agent_id: StrictStr | None = None
    memory_scope: StrictStr | None = None
    formation_total_duration_ms: StrictInt = 0
    model_extraction_duration_ms: StrictInt = 0
    persistence_duration_ms: StrictInt = 0

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int | str) -> int | str:
        return _validate_memory_event_schema_version(value)


class _LifecyclePayload(BaseModel):
    """MEMORY_LIFECYCLE_RESOLVED 的最小严格 payload。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt | StrictStr
    memory_type: StrictStr
    operation: StrictStr
    outcome: StrictStr
    safe_error_code: StrictStr | None = None
    affected_count: int
    winner_memory_id: StrictStr | None = None
    new_memory_id: StrictStr | None = None
    candidate_outcome: StrictStr | None = None
    affected_transitions: StrictStr = "NONE"
    exchange_id: StrictStr | None = None
    agent_id: StrictStr | None = None
    memory_scope: StrictStr | None = None
    resolution_duration_ms: StrictInt = 0
    mutation_duration_ms: StrictInt = 0
    ids_truncated: bool = False
    omitted_count: StrictInt = 0
    safe_reason: StrictStr = ""

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int | str) -> int | str:
        return _validate_memory_event_schema_version(value)


class _RetrievalPayload(BaseModel):
    """MEMORY_RETRIEVAL_COMPLETED 的最小严格 payload。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt | StrictStr
    retrieval_method: StrictStr
    ranking_method: StrictStr
    status: StrictStr
    safe_error_code: StrictStr | None = None
    candidate_count: int
    eligible_count: int
    selected_count: int
    context_record_count: int
    malformed_count: int
    omitted_count: int
    registered_selected_count: int
    open_selected_count: int
    planning_injected: bool
    direct_entry_supplied: bool
    budget_used_chars: StrictInt = 0
    duration_ms: StrictInt = 0

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: int | str) -> int | str:
        return _validate_memory_event_schema_version(value)


@dataclass(frozen=True, slots=True)
class FormationEvent:
    """一次已完成的 Memory formation 观察。"""

    run_id: str
    event_id: str
    formation_method: str
    status: str
    safe_error_code: str | None
    proposed_count: int
    accepted_count: int
    ignored_count: int
    persisted_count: int
    reused_count: int
    failed_count: int
    candidate_outcomes: str

    def __post_init__(self) -> None:
        require_text(self.run_id, "run_id")
        require_text(self.event_id, "event_id")
        require_text(self.formation_method, "formation_method")
        require_text(self.status, "status")
        counts = (
            self.proposed_count,
            self.accepted_count,
            self.ignored_count,
            self.persisted_count,
            self.reused_count,
            self.failed_count,
        )
        if any(count < 0 for count in counts):
            raise JournalEvidenceError("formation counts must be non-negative")


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """一次已 resolved 的 Memory lifecycle 观察。"""

    run_id: str
    event_id: str
    memory_type: str
    operation: str
    outcome: str
    safe_error_code: str | None
    affected_count: int
    winner_memory_id: str | None
    new_memory_id: str | None
    candidate_outcome: str | None
    affected_transitions: str

    def __post_init__(self) -> None:
        require_text(self.run_id, "run_id")
        require_text(self.event_id, "event_id")
        require_text(self.memory_type, "memory_type")
        require_text(self.operation, "operation")
        require_text(self.outcome, "outcome")
        if self.affected_count < 0:
            raise JournalEvidenceError("lifecycle affected_count must be non-negative")


@dataclass(frozen=True, slots=True)
class RetrievalEvent:
    """一次已完成的 Memory retrieval 观察（content-minimized：只有 counts/flags）。"""

    run_id: str
    event_id: str
    retrieval_method: str
    ranking_method: str
    status: str
    safe_error_code: str | None
    candidate_count: int
    eligible_count: int
    selected_count: int
    context_record_count: int
    malformed_count: int
    omitted_count: int
    registered_selected_count: int
    open_selected_count: int
    planning_injected: bool
    direct_entry_supplied: bool

    def __post_init__(self) -> None:
        require_text(self.run_id, "run_id")
        require_text(self.event_id, "event_id")
        require_text(self.retrieval_method, "retrieval_method")
        require_text(self.ranking_method, "ranking_method")
        require_text(self.status, "status")
        counts = (
            self.candidate_count,
            self.eligible_count,
            self.selected_count,
            self.context_record_count,
            self.malformed_count,
            self.omitted_count,
            self.registered_selected_count,
            self.open_selected_count,
        )
        if any(count < 0 for count in counts):
            raise JournalEvidenceError("retrieval counts must be non-negative")


@dataclass(frozen=True, slots=True)
class JournalEvents:
    """一个 run_id 的全部 memory 相关 journal events（按 type 分组）。"""

    run_id: str
    formation: tuple[FormationEvent, ...]
    lifecycle: tuple[LifecycleEvent, ...]
    retrieval: tuple[RetrievalEvent, ...]

    def __post_init__(self) -> None:
        require_text(self.run_id, "run_id")
        object.__setattr__(self, "formation", tuple(self.formation))
        object.__setattr__(self, "lifecycle", tuple(self.lifecycle))
        object.__setattr__(self, "retrieval", tuple(self.retrieval))


@dataclass(frozen=True, slots=True)
class JournalStepFact:
    """Journal ``RuntimeEvent.step_id`` 的 typed step 事实（canonical step identity）。

    ``step_id`` 是 canonical Runtime ``PlanStep.step_id``（Journal authority）；``status``
    是 STEP_COMPLETED safe_payload 的 step status（STEP_STARTED 只贡献 presence，不给
    status authority）。identity 绝不来自 ``EpisodeObservation.name`` / display name。
    """

    run_id: str
    event_id: str
    event_type: str
    step_id: str
    status: str | None = None

    def __post_init__(self) -> None:
        require_text(self.run_id, "run_id")
        require_text(self.event_id, "event_id")
        require_text(self.event_type, "event_type")
        require_text(self.step_id, "step_id")
        if self.event_type not in _SUPPORTED_STEP_EVENT_TYPES:
            raise JournalEvidenceError(f"unsupported step journal event_type: {self.event_type}")


@dataclass(frozen=True, slots=True)
class JournalStepFacts:
    """一个 run_id 的 canonical step identity 事实（RuntimeEvent.step_id authority）。

    ``step_id`` 必须全部来自 Journal 的 ``step_id`` 列；不做 display-name、canonical-text
    或 created_at 推断。
    """

    run_id: str
    facts: tuple[JournalStepFact, ...]

    def __post_init__(self) -> None:
        require_text(self.run_id, "run_id")
        object.__setattr__(self, "facts", tuple(self.facts))
        for fact in self.facts:
            if fact.run_id != self.run_id:
                raise JournalEvidenceError(f"step fact run_id mismatch: {fact.run_id!r} != {self.run_id!r}")

    def step_ids(self) -> tuple[str, ...]:
        """Return the computed property value."""
        return tuple(sorted({fact.step_id for fact in self.facts}))

    def status_for(self, step_id: str) -> str | None:
        """Return the latest STEP_COMPLETED status for ``step_id`` (None if not terminal)."""
        completed = [
            fact for fact in self.facts if fact.step_id == step_id and fact.event_type == STEP_COMPLETED_EVENT
        ]
        if not completed:
            return None
        return completed[-1].status

    def has_step(self, step_id: str) -> bool:
        """Implement the ``has_step`` contract (typed, fail-closed)."""
        return any(fact.step_id == step_id for fact in self.facts)


def _parse_payload(event_type: str, raw: str) -> object:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise JournalEvidenceError(f"{event_type} safe_payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise JournalEvidenceError(f"{event_type} safe_payload must be a JSON object")
    try:
        if event_type == MEMORY_FORMATION_COMPLETED:
            return _FormationPayload.model_validate(payload)
        if event_type == MEMORY_LIFECYCLE_RESOLVED:
            return _LifecyclePayload.model_validate(payload)
        if event_type == MEMORY_RETRIEVAL_COMPLETED:
            return _RetrievalPayload.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise JournalEvidenceError(f"{event_type} safe_payload is invalid: {exc}") from exc
    raise JournalEvidenceError(f"unsupported journal event_type: {event_type}")


def read_journal_events(db_path: str | Path, run_id: str) -> JournalEvents:
    """从隔离 runtime journal 只读读取指定 run 的 memory 事件（fail closed）。

    Args:
        db_path: scenario-isolated ``runtime_event_journal`` SQLite 文件路径。
        run_id: 要采集的 run identity（一个 scenario step 对应一个 run）。

    Returns:
        该 run 的 typed memory events（可能为空 tuple）。

    Raises:
        JournalEvidenceError: 文件缺失、schema 不匹配或 payload 畸形。
    """
    path = Path(db_path)
    if not path.is_file():
        raise JournalEvidenceError(f"isolated journal db is missing: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise JournalEvidenceError(f"cannot open isolated journal db read-only: {path}") from exc
    try:
        try:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_JOURNAL_TABLE})")}
        except sqlite3.Error as exc:
            raise JournalEvidenceError(f"cannot inspect journal schema: {exc}") from exc
        if not _REQUIRED_COLUMNS.issubset(columns):
            raise JournalEvidenceError(f"isolated journal schema is missing required columns: {path}")
        try:
            rows = connection.execute(
                f"SELECT event_id, event_type, safe_payload FROM {_JOURNAL_TABLE} WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise JournalEvidenceError(f"cannot read journal events: {exc}") from exc
        formation: list[FormationEvent] = []
        lifecycle: list[LifecycleEvent] = []
        retrieval: list[RetrievalEvent] = []
        for event_id, event_type, safe_payload in rows:
            # LocalAgent journal 同一 run 还包含 planning、step、terminal 等事件；
            # WP5 只拥有三类 Memory observation 的消费契约。非目标事件不应让
            # Memory collector 把完整 journal 误判为 malformed。
            if event_type not in {
                MEMORY_FORMATION_COMPLETED,
                MEMORY_LIFECYCLE_RESOLVED,
                MEMORY_RETRIEVAL_COMPLETED,
            }:
                continue
            parsed = _parse_payload(event_type, safe_payload)
            if event_type == MEMORY_FORMATION_COMPLETED:
                payload = parsed
                assert isinstance(payload, _FormationPayload)
                formation.append(
                    FormationEvent(
                        run_id=run_id,
                        event_id=event_id,
                        formation_method=payload.formation_method,
                        status=payload.status,
                        safe_error_code=payload.safe_error_code,
                        proposed_count=payload.proposed_count,
                        accepted_count=payload.accepted_count,
                        ignored_count=payload.ignored_count,
                        persisted_count=payload.persisted_count,
                        reused_count=payload.reused_count,
                        failed_count=payload.failed_count,
                        candidate_outcomes=payload.candidate_outcomes,
                    )
                )
            elif event_type == MEMORY_LIFECYCLE_RESOLVED:
                payload = parsed
                assert isinstance(payload, _LifecyclePayload)
                lifecycle.append(
                    LifecycleEvent(
                        run_id=run_id,
                        event_id=event_id,
                        memory_type=payload.memory_type,
                        operation=payload.operation,
                        outcome=payload.outcome,
                        safe_error_code=payload.safe_error_code,
                        affected_count=payload.affected_count,
                        winner_memory_id=payload.winner_memory_id,
                        new_memory_id=payload.new_memory_id,
                        candidate_outcome=payload.candidate_outcome,
                        affected_transitions=payload.affected_transitions,
                    )
                )
            elif event_type == MEMORY_RETRIEVAL_COMPLETED:
                payload = parsed
                assert isinstance(payload, _RetrievalPayload)
                retrieval.append(
                    RetrievalEvent(
                        run_id=run_id,
                        event_id=event_id,
                        retrieval_method=payload.retrieval_method,
                        ranking_method=payload.ranking_method,
                        status=payload.status,
                        safe_error_code=payload.safe_error_code,
                        candidate_count=payload.candidate_count,
                        eligible_count=payload.eligible_count,
                        selected_count=payload.selected_count,
                        context_record_count=payload.context_record_count,
                        malformed_count=payload.malformed_count,
                        omitted_count=payload.omitted_count,
                        registered_selected_count=payload.registered_selected_count,
                        open_selected_count=payload.open_selected_count,
                        planning_injected=payload.planning_injected,
                        direct_entry_supplied=payload.direct_entry_supplied,
                    )
                )
        return JournalEvents(
            run_id=run_id,
            formation=tuple(formation),
            lifecycle=tuple(lifecycle),
            retrieval=tuple(retrieval),
        )
    finally:
        connection.close()


def read_journal_step_facts(db_path: str | Path, run_id: str) -> JournalStepFacts:
    """从隔离 runtime journal 只读读取指定 run 的 canonical step identity 事实。

    ``RuntimeEvent.step_id`` 是冻结的 canonical step identity authority（Journal 列
    ``step_id``）；本函数只消费该列与 STEP_STARTED / STEP_COMPLETED 事件类型，绝不把
    ``safe_payload`` 中的 display name 或其它内容当作 identity。

    Args:
        db_path: scenario-isolated ``runtime_event_journal`` SQLite 文件路径。
        run_id: 要采集的 run identity（一个 scenario step 对应一个 run）。

    Returns:
        该 run 的 canonical step facts（可能为空 tuple）。

    Raises:
        JournalEvidenceError: 文件缺失、schema 不匹配或 payload 畸形（fail closed）。
    """
    path = Path(db_path)
    if not path.is_file():
        raise JournalEvidenceError(f"isolated journal db is missing: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise JournalEvidenceError(f"cannot open isolated journal db read-only: {path}") from exc
    try:
        try:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_JOURNAL_TABLE})")}
        except sqlite3.Error as exc:
            raise JournalEvidenceError(f"cannot inspect journal schema: {exc}") from exc
        required = _REQUIRED_COLUMNS | {"step_id"}
        if not required.issubset(columns):
            raise JournalEvidenceError(f"isolated journal schema is missing required columns: {path}")
        try:
            rows = connection.execute(
                f"SELECT event_id, event_type, step_id, safe_payload FROM {_JOURNAL_TABLE} "
                f"WHERE run_id = ? AND event_type IN ({', '.join('?' for _ in _SUPPORTED_STEP_EVENT_TYPES)}) "
                f"ORDER BY sequence",
                (run_id, *_SUPPORTED_STEP_EVENT_TYPES),
            ).fetchall()
        except sqlite3.Error as exc:
            raise JournalEvidenceError(f"cannot read journal step events: {exc}") from exc
        facts: list[JournalStepFact] = []
        for event_id, event_type, step_id, safe_payload in rows:
            if step_id is None or not step_id.strip():
                raise JournalEvidenceError(
                    f"{event_type} journal event {event_id} is missing canonical step_id (identity authority)"
                )
            status: str | None = None
            if event_type == STEP_COMPLETED_EVENT:
                try:
                    parsed = json.loads(safe_payload)
                except (TypeError, ValueError) as exc:
                    raise JournalEvidenceError(f"{event_type} safe_payload is not valid JSON") from exc
                if not isinstance(parsed, dict) or not isinstance(parsed.get("status"), str):
                    raise JournalEvidenceError(f"{event_type} safe_payload must contain a status string")
                status = parsed["status"]
            facts.append(
                JournalStepFact(
                    run_id=run_id,
                    event_id=event_id,
                    event_type=event_type,
                    step_id=step_id,
                    status=status,
                )
            )
        return JournalStepFacts(run_id=run_id, facts=tuple(facts))
    finally:
        connection.close()


def journal_sequence_watermark(db_path: str | Path, run_id: str) -> int:
    """返回该 run 在 scenario-owned journal 中的当前最大 sequence（只读）。

    用于 bounded journal settle 的 stability 判断：如果两次读取的 watermark 相同，
    说明 LocalAgent 没有再提交新 events（stable watermark）。
    """
    path = Path(db_path)
    if not path.is_file():
        return 0
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return 0
    try:
        row = connection.execute(
            f"SELECT MAX(sequence) FROM {_JOURNAL_TABLE} WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    except sqlite3.Error:
        return 0
    finally:
        connection.close()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def has_required_memory_events(
    journal: JournalEvents, *, expects_formation: bool, expects_lifecycle: bool, expects_retrieval: bool
) -> bool:
    """该 step 的 expectations 所需的 memory events 是否都已被观察到。

    用于 settle 停止条件：required events 齐备即 stop（EXPECTED_EVIDENCE_OBSERVED）。
    """
    if expects_formation and not journal.formation:
        return False
    if expects_lifecycle and not journal.lifecycle:
        return False
    if expects_retrieval and not journal.retrieval:
        return False
    return True


@dataclass(frozen=True, slots=True)
class JournalSettleEvidence:
    """bounded journal settle 的 watermark 证据（不含任何 Memory 内容）。"""

    initial_sequence_watermark: int
    final_sequence_watermark: int
    poll_attempts: int
    stop_reason: str

    def __post_init__(self) -> None:
        if self.initial_sequence_watermark < 0 or self.final_sequence_watermark < 0:
            raise ValueError("watermarks must be non-negative")
        if self.final_sequence_watermark < self.initial_sequence_watermark:
            raise ValueError("final watermark must not be below initial watermark")
        if self.poll_attempts < 1:
            raise ValueError("poll_attempts must be positive")
        require_text(self.stop_reason, "stop_reason")
        if self.stop_reason not in {"EXPECTED_EVIDENCE_OBSERVED", "STABLE_WATERMARK", "DEADLINE_REACHED"}:
            raise ValueError(f"unknown journal settle stop_reason: {self.stop_reason}")

    def to_dict(self) -> dict[str, object]:
        """JSON-safe settle 证据（无 raw Memory 内容）。"""
        return {
            "initial_sequence_watermark": self.initial_sequence_watermark,
            "final_sequence_watermark": self.final_sequence_watermark,
            "poll_attempts": self.poll_attempts,
            "stop_reason": self.stop_reason,
        }


__all__ = [
    "FormationEvent",
    "JournalEvidenceError",
    "JournalEvents",
    "JournalSettleEvidence",
    "JournalStepFact",
    "JournalStepFacts",
    "LifecycleEvent",
    "MEMORY_FORMATION_COMPLETED",
    "MEMORY_LIFECYCLE_RESOLVED",
    "MEMORY_RETRIEVAL_COMPLETED",
    "RetrievalEvent",
    "STEP_COMPLETED_EVENT",
    "STEP_STARTED_EVENT",
    "has_required_memory_events",
    "journal_sequence_watermark",
    "read_journal_events",
    "read_journal_step_facts",
]
