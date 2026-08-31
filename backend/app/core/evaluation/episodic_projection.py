"""WP6-E 只读 SQLite Episode final-state projection。

只从 scenario-isolated ``long_term_memory`` SQLite 文件读取 EPISODIC rows，使用只读
连接（``mode=ro``），投影 evaluation 所需的 typed 字段：

- 基础字段：memory_id / memory_type / status / agent_id / memory_scope /
  origin_run_id / logical_key / canonical_text（仅 evaluation-only 环境读取，不出
  artifact 默认正文）。
- typed episodic payload：situation.text、goal.text/authority、
  observations（name/status/safe_error_code 等）、result（terminal_status/
  stop_reason/delivery_status）、lesson（schema_version=1）。

SQLite authority 边界（最高 authority：60 Gate）：

- SQLite 只用于 ``Persistence / Final State`` assertions（row count、origin_run_id
  uniqueness、EPISODIC/ACTIVE、logical_key NULL、agent/scope、typed payload validity、
  invariant）。
- SQLite **绝不**是 Retrieval selection oracle；selected/supplied/injected 只能由
  private capture 证明。本模块不提供任何按内容相似度或时间推断 identity 的 API。

fail-closed：DB 文件缺失或 schema/payload 不匹配必须抛 ``EpisodicProjectionError``，
绝不静默返回空投影。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.core.evaluation.immutable import require_text

_LONG_TERM_MEMORY_TABLE = "long_term_memory"
EPISODIC_PAYLOAD_SCHEMA_VERSION = 1


class EpisodicProjectionError(ValueError):
    """只读 Episode 投影读取/解析失败（fail closed）。"""


@dataclass(frozen=True, slots=True)
class EpisodicObservationProjection:
    """Episode payload 中一条 observation 的 evaluation-only 投影。"""

    observation_type: str
    name: str
    status: str
    safe_error_code: str | None = None
    outcome_classification: str | None = None
    result_digest: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Project this object into a JSON-safe dictionary."""
        return {
            "observation_type": self.observation_type,
            "name": self.name,
            "status": self.status,
            "safe_error_code": self.safe_error_code,
            "outcome_classification": self.outcome_classification,
            "result_digest": self.result_digest,
        }


@dataclass(frozen=True, slots=True)
class EpisodicResultProjection:
    """Episode payload 中 result 的 projection。"""

    terminal_status: str
    stop_reason: str
    delivery_status: str
    delivered_result_digest: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Project this object into a JSON-safe dictionary."""
        return {
            "terminal_status": self.terminal_status,
            "stop_reason": self.stop_reason,
            "delivery_status": self.delivery_status,
            "delivered_result_digest": self.delivered_result_digest,
        }


@dataclass(frozen=True, slots=True)
class EpisodicProjectionRecord:
    """一条 persisted EPISODIC row 的 evaluation-only projection。

    注意：本 DTO 持有 canonical_text 与 payload 字段仅供 isolated Layer1
    evaluation 环境中的 Grounding / Privacy / Structure evaluator 检查；experiment
    artifact 不得默认复制这些正文（见 episodic_artifact 的 privacy projection）。
    """

    memory_id: str
    memory_type: str
    status: str
    agent_id: str
    memory_scope: str
    origin_run_id: str
    logical_key: str | None
    canonical_text: str
    payload_schema_version: int
    situation_text: str
    goal_text: str
    goal_authority: str
    observations: tuple[EpisodicObservationProjection, ...]
    result: EpisodicResultProjection
    lesson: str | None
    created_at: str
    formation_method: str | None

    def __post_init__(self) -> None:
        for name in (
            "memory_id",
            "memory_type",
            "status",
            "agent_id",
            "memory_scope",
            "origin_run_id",
            "situation_text",
        ):
            require_text(getattr(self, name), name)
        if self.memory_type != "EPISODIC":
            raise EpisodicProjectionError(f"projection record is not EPISODIC: {self.memory_type}")
        if self.status != "ACTIVE":
            raise EpisodicProjectionError(f"projection record is not ACTIVE: {self.status}")
        if self.logical_key is not None:
            raise EpisodicProjectionError(f"EPISODIC logical_key must be NULL, got {self.logical_key!r}")
        object.__setattr__(self, "observations", tuple(self.observations))

    def to_projection_dict(self, *, include_content: bool = False) -> dict[str, object]:
        """Project this record into a JSON-safe dictionary.

        ``include_content=False`` 时不携带 canonical_text / situation / goal / lesson
        正文（artifact privacy boundary 默认关闭正文）。
        """
        base: dict[str, object] = {
            "memory_id": self.memory_id,
            "memory_type": self.memory_type,
            "status": self.status,
            "agent_id": self.agent_id,
            "memory_scope": self.memory_scope,
            "origin_run_id": self.origin_run_id,
            "logical_key": self.logical_key,
            "payload_schema_version": self.payload_schema_version,
            "goal_authority": self.goal_authority,
            "observations": [item.to_dict() for item in self.observations],
            "result": self.result.to_dict(),
            "created_at": self.created_at,
            "formation_method": self.formation_method,
            "private_evaluation_artifact": True,
        }
        if include_content:
            base["canonical_text"] = self.canonical_text
            base["situation_text"] = self.situation_text
            base["goal_text"] = self.goal_text
            base["lesson"] = self.lesson
        return base


def _require_json_object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise EpisodicProjectionError(f"{where} must be a JSON object")
    return value


def _parse_payload(raw: str, memory_id: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise EpisodicProjectionError(f"episode {memory_id} payload is not valid JSON") from exc
    return _require_json_object(parsed, f"episode {memory_id} payload")


def _parse_episodic_payload(raw: str, memory_id: str) -> dict[str, object]:
    payload = _parse_payload(raw, memory_id)
    schema = payload.get("schema_version")
    if schema != EPISODIC_PAYLOAD_SCHEMA_VERSION:
        raise EpisodicProjectionError(f"episode {memory_id} unsupported payload schema_version: {schema!r}")
    return payload


def _optional_text(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EpisodicProjectionError(f"{where} must be a non-empty string or null")
    return value


def _required_text(value: object, where: str) -> str:
    text = _optional_text(value, where)
    if text is None:
        raise EpisodicProjectionError(f"{where} is required")
    return text


def read_episodic_projection(db_path: str | Path) -> tuple[EpisodicProjectionRecord, ...]:
    """以只读连接读取 isolated ``long_term_memory`` 的全部 EPISODIC rows。

    Args:
        db_path: scenario-isolated SQLite 文件路径。

    Returns:
        按 memory_id 排序的 EPISODIC projection records。

    Raises:
        EpisodicProjectionError: 文件缺失、schema 不匹配或 payload 畸形（fail closed）。
    """
    path = Path(db_path)
    if not path.is_file():
        raise EpisodicProjectionError(f"isolated memory db is missing: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise EpisodicProjectionError(f"cannot open isolated memory db read-only: {path}") from exc
    try:
        try:
            rows = connection.execute(
                f"SELECT memory_id, memory_type, status, agent_id, memory_scope, "
                f"origin_run_id, logical_key, canonical_text, payload, created_at, "
                f"formation_method FROM {_LONG_TERM_MEMORY_TABLE} "
                f"WHERE memory_type = 'EPISODIC' ORDER BY memory_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise EpisodicProjectionError(f"isolated memory db schema mismatch: {exc}") from exc
        records: list[EpisodicProjectionRecord] = []
        for row in rows:
            (
                memory_id,
                memory_type,
                status,
                agent_id,
                memory_scope,
                origin_run_id,
                logical_key,
                canonical_text,
                payload_raw,
                created_at,
                formation_method,
            ) = row
            payload = _parse_episodic_payload(payload_raw, memory_id)
            situation = _require_json_object(payload.get("situation"), f"episode {memory_id} situation")
            goal = _require_json_object(payload.get("goal"), f"episode {memory_id} goal")
            result = _require_json_object(payload.get("result"), f"episode {memory_id} result")
            raw_observations = payload.get("observations")
            if not isinstance(raw_observations, list):
                raise EpisodicProjectionError(f"episode {memory_id} observations must be a list")
            observations: list[EpisodicObservationProjection] = []
            for index, item in enumerate(raw_observations):
                obs = _require_json_object(item, f"episode {memory_id} observation[{index}]")
                observations.append(
                    EpisodicObservationProjection(
                        observation_type=_required_text(
                            obs.get("observation_type"), f"episode {memory_id} observation_type"
                        ),
                        name=_required_text(obs.get("name"), f"episode {memory_id} observation name"),
                        status=_required_text(obs.get("status"), f"episode {memory_id} observation status"),
                        safe_error_code=_optional_text(
                            obs.get("safe_error_code"), f"episode {memory_id} safe_error_code"
                        ),
                        outcome_classification=_optional_text(
                            obs.get("outcome_classification"), f"episode {memory_id} outcome_classification"
                        ),
                        result_digest=_optional_text(obs.get("result_digest"), f"episode {memory_id} result_digest"),
                    )
                )
            records.append(
                EpisodicProjectionRecord(
                    memory_id=memory_id,
                    memory_type=memory_type,
                    status=status,
                    agent_id=agent_id,
                    memory_scope=memory_scope,
                    origin_run_id=origin_run_id,
                    logical_key=logical_key,
                    canonical_text=canonical_text,
                    payload_schema_version=EPISODIC_PAYLOAD_SCHEMA_VERSION,
                    situation_text=_required_text(situation.get("text"), f"episode {memory_id} situation.text"),
                    goal_text=_required_text(goal.get("text"), f"episode {memory_id} goal.text"),
                    goal_authority=_required_text(goal.get("authority"), f"episode {memory_id} goal.authority"),
                    observations=tuple(observations),
                    result=EpisodicResultProjection(
                        terminal_status=_required_text(
                            result.get("terminal_status"), f"episode {memory_id} result.terminal_status"
                        ),
                        stop_reason=_required_text(
                            result.get("stop_reason"), f"episode {memory_id} result.stop_reason"
                        ),
                        delivery_status=_required_text(
                            result.get("delivery_status"), f"episode {memory_id} result.delivery_status"
                        ),
                        delivered_result_digest=_optional_text(
                            result.get("delivered_result_digest"),
                            f"episode {memory_id} result.delivered_result_digest",
                        ),
                    ),
                    lesson=_optional_text(payload.get("lesson"), f"episode {memory_id} lesson"),
                    created_at=created_at,
                    formation_method=formation_method,
                )
            )
        return tuple(records)
    finally:
        connection.close()


def episode_projection_index(
    records: tuple[EpisodicProjectionRecord, ...],
) -> dict[str, EpisodicProjectionRecord]:
    """按 memory_id 建立投影索引（identity correlation 用，不作 selection 推断）。"""
    index: dict[str, EpisodicProjectionRecord] = {}
    for record in records:
        if record.memory_id in index:
            raise EpisodicProjectionError(f"duplicate episode memory_id: {record.memory_id}")
        index[record.memory_id] = record
    return index


__all__ = [
    "EPISODIC_PAYLOAD_SCHEMA_VERSION",
    "EpisodicObservationProjection",
    "EpisodicProjectionError",
    "EpisodicProjectionRecord",
    "EpisodicResultProjection",
    "episode_projection_index",
    "read_episodic_projection",
]
