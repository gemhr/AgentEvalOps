"""WP5 Stateful Memory 的只读 SQLite 投影与 canonical state diff。

只从隔离的 ``long_term_memory`` SQLite 文件读取，使用只读连接（``mode=ro``）：
构造 evaluation-only canonical projection，不要求 runtime ``memory_id`` 与 dataset
固定 ID 相等；runtime ``memory_id`` 仅保留用于 relation/supersede binding。

fail-closed：DB 文件缺失或 schema 不匹配必须抛出证据错误，绝不静默返回空投影。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from app.core.evaluation.immutable import require_text

_LONG_TERM_MEMORY_TABLE = "long_term_memory"
FORGET_TOMBSTONE_TEXT = "[FORGOTTEN]"


class StateProjectionError(ValueError):
    """只读投影读取/解析失败（fail closed）。"""


class RedactionState(StrEnum):
    """一条 FORGOTTEN record 的 redaction 完整性。"""

    REDACTED = "REDACTED"
    NOT_REDACTED = "NOT_REDACTED"


@dataclass(frozen=True, slots=True)
class CanonicalMemoryRecord:
    """evaluation-only canonical memory projection（不含 runtime 决策 authority）。"""

    memory_id: str
    agent_id: str
    memory_scope: str
    memory_type: str
    logical_key: str | None
    status: str
    canonical_text: str
    payload: dict[str, object]
    canonical_value: object
    created_at: str
    updated_at: str
    superseded_by_memory_id: str | None
    origin_run_id: str
    formation_method: str | None

    def __post_init__(self) -> None:
        for field_name in (
            "memory_id",
            "agent_id",
            "memory_scope",
            "memory_type",
            "status",
            "canonical_text",
            "created_at",
            "updated_at",
            "origin_run_id",
        ):
            require_text(getattr(self, field_name), field_name)
        if self.logical_key is not None:
            require_text(self.logical_key, "logical_key")
        if self.status not in {"ACTIVE", "SUPERSEDED", "FORGOTTEN"}:
            raise StateProjectionError(f"unknown memory status: {self.status}")

    def provenance_key(self) -> tuple[str, str | None]:
        """NO_CHANGE provenance 不变量使用的不可变来源指纹。"""
        return (self.origin_run_id, self.formation_method)

    def canonical_identity(self) -> tuple[str, str, str, str | None, str, object]:
        """不依赖 runtime memory_id 的 canonical identity（含 expected value）。"""
        return (
            self.agent_id,
            self.memory_scope,
            self.memory_type,
            self.logical_key,
            self.status,
            self.canonical_value,
        )

    def redaction_state(self) -> RedactionState:
        """FORGOTTEN record 的 redaction 完整性（非 FORGOTTEN 一律 NOT_REDACTED 语义）。"""
        if self.status != "FORGOTTEN":
            return RedactionState.NOT_REDACTED
        if (
            self.canonical_text == FORGET_TOMBSTONE_TEXT
            and self.payload == {}
            and self.superseded_by_memory_id is None
        ):
            return RedactionState.REDACTED
        return RedactionState.NOT_REDACTED

    def to_projection_dict(self) -> dict[str, object]:
        """JSON-safe projection；标记为 private evaluation artifact 内容。"""
        return {
            "memory_id": self.memory_id,
            "agent_id": self.agent_id,
            "memory_scope": self.memory_scope,
            "memory_type": self.memory_type,
            "logical_key": self.logical_key,
            "status": self.status,
            "canonical_text": self.canonical_text,
            "payload": self.payload,
            "canonical_value": self.canonical_value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "superseded_by_memory_id": self.superseded_by_memory_id,
            "origin_run_id": self.origin_run_id,
            "formation_method": self.formation_method,
            "private_evaluation_artifact": True,
        }


def _parse_payload(raw: str) -> tuple[dict[str, object], object]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StateProjectionError(f"memory payload is not valid JSON: {raw[:120]!r}") from exc
    if not isinstance(parsed, dict):
        raise StateProjectionError(f"memory payload must be a JSON object: {raw[:120]!r}")
    value = parsed.get("value", parsed)
    return parsed, value


def read_memory_projection(db_path: str | Path) -> tuple[CanonicalMemoryRecord, ...]:
    """以只读连接读取 isolated ``long_term_memory`` 的 canonical projection。

    Args:
        db_path: scenario-isolated SQLite 文件路径。

    Returns:
        按 memory_id 排序的 canonical records。

    Raises:
        StateProjectionError: 文件缺失、schema 不匹配或 payload 畸形（fail closed）。
    """
    path = Path(db_path)
    if not path.is_file():
        raise StateProjectionError(f"isolated memory db is missing: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise StateProjectionError(f"cannot open isolated memory db read-only: {path}") from exc
    try:
        try:
            rows = connection.execute(
                f"SELECT memory_id, agent_id, memory_scope, memory_type, logical_key, status, "
                f"canonical_text, payload, created_at, updated_at, superseded_by_memory_id, "
                f"origin_run_id, formation_method "
                f"FROM {_LONG_TERM_MEMORY_TABLE} ORDER BY memory_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateProjectionError(f"isolated memory db schema mismatch: {exc}") from exc
        records: list[CanonicalMemoryRecord] = []
        for row in rows:
            (
                memory_id,
                agent_id,
                memory_scope,
                memory_type,
                logical_key,
                status,
                canonical_text,
                payload_raw,
                created_at,
                updated_at,
                superseded_by_memory_id,
                origin_run_id,
                formation_method,
            ) = row
            payload, canonical_value = _parse_payload(payload_raw)
            records.append(
                CanonicalMemoryRecord(
                    memory_id=memory_id,
                    agent_id=agent_id,
                    memory_scope=memory_scope,
                    memory_type=memory_type,
                    logical_key=logical_key,
                    status=status,
                    canonical_text=canonical_text,
                    payload=payload,
                    canonical_value=canonical_value,
                    created_at=created_at,
                    updated_at=updated_at,
                    superseded_by_memory_id=superseded_by_memory_id,
                    origin_run_id=origin_run_id,
                    formation_method=formation_method,
                )
            )
        return tuple(records)
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class MemoryStateSnapshot:
    """一个 step 前/后或 scenario 最终的只读 Memory 投影快照。"""

    snapshot_id: str
    db_path: str
    captured_at: datetime
    records: tuple[CanonicalMemoryRecord, ...]

    def __post_init__(self) -> None:
        require_text(self.snapshot_id, "snapshot_id")
        require_text(self.db_path, "db_path")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        object.__setattr__(self, "records", tuple(self.records))


def snapshot_memory_state(
    db_path: str | Path,
    snapshot_id: str,
    *,
    captured_at: datetime | None = None,
) -> MemoryStateSnapshot:
    """创建只读 Memory state snapshot（不写入任何内容）。"""
    records = read_memory_projection(db_path)
    return MemoryStateSnapshot(
        snapshot_id=snapshot_id,
        db_path=str(Path(db_path)),
        captured_at=captured_at or datetime.now(UTC),
        records=records,
    )


@dataclass(frozen=True, slots=True)
class StateDiffEntry:
    """final-state 比较的一个差异条目。"""

    kind: str
    detail: str
    expected: object | None = None
    actual: object | None = None

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON-safe diff 条目。"""
        return {"kind": self.kind, "detail": self.detail, "expected": self.expected, "actual": self.actual}


def _identity_key(record: CanonicalMemoryRecord) -> tuple[str, str, str, str | None]:
    return (record.agent_id, record.memory_scope, record.memory_type, record.logical_key)


def state_diff(
    expected: list[object],
    actual: tuple[CanonicalMemoryRecord, ...],
    alias_binding: dict[str, str] | None = None,
) -> tuple[StateDiffEntry, ...]:
    """Canonical 比较 expected records 与 actual records。

    expected 是 dataset ``MemoryRecordExpectation``（含 alias、value、status、supersede 关系）。
    按 (agent, scope, type, logical_key) 对齐；alias 只用于可读 detail。supersede 关系用
    alias_binding 解析为 runtime memory_id 后精确比较。不能因为 expected rows 都存在就
    忽略额外错误的 ACTIVE row。
    """
    diffs: list[StateDiffEntry] = []
    binding = alias_binding or {}
    actual_index: dict[tuple[str, str, str, str | None], list[CanonicalMemoryRecord]] = {}
    for record in actual:
        actual_index.setdefault(_identity_key(record), []).append(record)

    expected_items = sorted(expected, key=lambda item: item.alias)
    matched_memory_ids: set[str] = set()

    for expectation in expected_items:
        key = (expectation.agent_id, expectation.memory_scope, expectation.memory_type, expectation.logical_key)
        candidates = actual_index.get(key, [])
        if expectation.status == "FORGOTTEN":
            matches = [
                record
                for record in candidates
                if record.status == "FORGOTTEN" and record.memory_id not in matched_memory_ids
            ]
        else:
            matches = [
                record
                for record in candidates
                if record.status == expectation.status
                and record.canonical_value == expectation.value
                and record.memory_id not in matched_memory_ids
            ]
        if not candidates:
            diffs.append(
                StateDiffEntry(
                    kind="MISSING",
                    detail=f"expected record for alias {expectation.alias} not found",
                    expected=expectation.alias,
                )
            )
            continue
        if not matches:
            diffs.append(
                StateDiffEntry(
                    kind="STATUS_OR_VALUE_MISMATCH",
                    detail=f"expected alias {expectation.alias} found but status/value differ",
                    expected={
                        "status": expectation.status,
                        "value": expectation.value,
                    },
                    actual=[
                        {"status": r.status, "value": r.canonical_value, "memory_id": r.memory_id} for r in candidates
                    ],
                )
            )
            continue
        match = matches[0]
        matched_memory_ids.add(match.memory_id)
        if expectation.superseded_by_alias is not None:
            expected_relation = binding.get(expectation.superseded_by_alias)
            relation = match.superseded_by_memory_id
            if relation is None:
                diffs.append(
                    StateDiffEntry(
                        kind="SUPERSEDE_RELATION_MISSING",
                        detail=f"alias {expectation.alias} is missing superseded_by relation",
                        expected=expectation.superseded_by_alias,
                        actual=None,
                    )
                )
            elif expected_relation is not None and relation != expected_relation:
                diffs.append(
                    StateDiffEntry(
                        kind="SUPERSEDE_RELATION_MISMATCH",
                        detail=f"alias {expectation.alias} superseded_by relation differs from expectation",
                        expected=expected_relation,
                        actual=relation,
                    )
                )
        elif match.superseded_by_memory_id is not None:
            diffs.append(
                StateDiffEntry(
                    kind="UNEXPECTED_SUPERSEDE_RELATION",
                    detail=f"alias {expectation.alias} has unexpected superseded_by relation",
                    expected=None,
                    actual=match.superseded_by_memory_id,
                )
            )

    # Expected state may legitimately contain several historical rows under one
    # logical key. Key membership alone must not hide an extra ACTIVE old value.
    extra_active = [
        record for record in actual if record.status == "ACTIVE" and record.memory_id not in matched_memory_ids
    ]
    for record in extra_active:
        diffs.append(
            StateDiffEntry(
                kind="EXTRA_ACTIVE_ROW",
                detail=f"unexpected extra ACTIVE row for memory_id {record.memory_id}",
                actual={
                    "logical_key": record.logical_key,
                    "value": record.canonical_value,
                    "agent_id": record.agent_id,
                    "memory_scope": record.memory_scope,
                },
            )
        )
    return tuple(diffs)


def count_active_by_logical_key(
    records: tuple[CanonicalMemoryRecord, ...],
) -> dict[tuple[str, str, str, str | None], int]:
    """Keyed partition 的 ACTIVE 行计数（invariant 检查用）。"""
    counts: dict[tuple[str, str, str, str | None], int] = {}
    for record in records:
        if record.status == "ACTIVE":
            key = _identity_key(record)
            counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = [
    "CanonicalMemoryRecord",
    "FORGET_TOMBSTONE_TEXT",
    "MemoryStateSnapshot",
    "RedactionState",
    "StateDiffEntry",
    "StateProjectionError",
    "count_active_by_logical_key",
    "read_memory_projection",
    "snapshot_memory_state",
    "state_diff",
]
