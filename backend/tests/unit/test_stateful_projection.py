"""Read-only SQLite memory projection + canonical diff tests。"""

# ruff: noqa: D101, D105, D415

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.evaluation.stateful_projection import (
    CanonicalMemoryRecord,
    RedactionState,
    StateProjectionError,
    count_active_by_logical_key,
    read_memory_projection,
    snapshot_memory_state,
    state_diff,
)

SCHEMA = """
CREATE TABLE long_term_memory (
  memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, status TEXT NOT NULL,
  agent_id TEXT NOT NULL, memory_scope TEXT NOT NULL, canonical_text TEXT NOT NULL,
  payload TEXT NOT NULL, logical_key TEXT, origin_type TEXT NOT NULL,
  origin_run_id TEXT NOT NULL, origin_exchange_id TEXT NOT NULL,
  origin_agent_id TEXT NOT NULL, origin_memory_scope TEXT NOT NULL,
  formation_method TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  superseded_by_memory_id TEXT
)
"""


def make_db(rows: list[tuple]) -> Path:
    handle, raw = tempfile.mkstemp(suffix=".db")
    path = Path(raw)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    for row in rows:
        connection.execute("INSERT INTO long_term_memory VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    connection.commit()
    connection.close()
    return path


def row(
    memory_id="mem-1",
    status="ACTIVE",
    logical_key="project.database",
    payload='{"value":"SQLite"}',
    agent_id="core_router",
    scope="direct",
    canonical_text="db: SQLite",
    superseded_by=None,
):
    return (
        memory_id,
        "SEMANTIC",
        status,
        agent_id,
        scope,
        canonical_text,
        payload,
        logical_key,
        "USER",
        "run-1",
        "run-1",
        agent_id,
        scope,
        "llm",
        "2026-08-01T00:00:00+00:00",
        "2026-08-01T00:00:00+00:00",
        superseded_by,
    )


def test_read_memory_projection_readonly():
    db = make_db([row(), row("mem-2", logical_key="project.package_manager", payload='{"value":"uv"}')])
    records = read_memory_projection(db)
    assert len(records) == 2
    assert records[0].logical_key == "project.database"
    assert records[0].canonical_value == "SQLite"
    assert records[1].canonical_value == "uv"
    connection = sqlite3.connect(db)
    count = connection.execute("SELECT COUNT(*) FROM long_term_memory").fetchone()[0]
    connection.close()
    assert count == 2


def test_read_memory_projection_missing_db_fails_closed():
    with pytest.raises(StateProjectionError, match="missing"):
        read_memory_projection(Path("definitely-missing-memory.db"))


def test_read_memory_projection_schema_mismatch_fails_closed():
    path = Path("schema-mismatch.db")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE long_term_memory (bad TEXT)")
    connection.commit()
    connection.close()
    try:
        with pytest.raises(StateProjectionError):
            read_memory_projection(path)
    finally:
        path.unlink(missing_ok=True)


def test_read_memory_projection_malformed_payload_fails_closed():
    path = Path("malformed-payload.db")
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO long_term_memory VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        row(payload="not-json"),
    )
    connection.commit()
    connection.close()
    try:
        with pytest.raises(StateProjectionError, match="not valid JSON"):
            read_memory_projection(path)
    finally:
        path.unlink(missing_ok=True)


def test_forgotten_redaction_state():
    redacted = CanonicalMemoryRecord(
        memory_id="m",
        agent_id="a",
        memory_scope="direct",
        memory_type="SEMANTIC",
        logical_key="project.database",
        status="FORGOTTEN",
        canonical_text="[FORGOTTEN]",
        payload={},
        canonical_value={},
        created_at="t",
        updated_at="t",
        superseded_by_memory_id=None,
        origin_run_id="r",
        formation_method="llm",
    )
    assert redacted.redaction_state() is RedactionState.REDACTED

    leaking = CanonicalMemoryRecord(
        memory_id="m",
        agent_id="a",
        memory_scope="direct",
        memory_type="SEMANTIC",
        logical_key="project.database",
        status="FORGOTTEN",
        canonical_text="db: SQLite",
        payload={"value": "SQLite"},
        canonical_value="SQLite",
        created_at="t",
        updated_at="t",
        superseded_by_memory_id=None,
        origin_run_id="r",
        formation_method="llm",
    )
    assert leaking.redaction_state() is RedactionState.NOT_REDACTED


def test_snapshot_is_readonly_and_binds_projection():
    db = make_db([row()])
    snapshot = snapshot_memory_state(db, "snap-1", captured_at=datetime.now(UTC))
    assert snapshot.snapshot_id == "snap-1"
    assert len(snapshot.records) == 1
    assert snapshot.db_path == str(db)


def test_count_active_by_logical_key():
    db = make_db(
        [
            row("mem-1", logical_key="project.database"),
            row("mem-2", logical_key="project.database"),
            row("mem-3", logical_key="project.package_manager", payload='{"value":"uv"}'),
            row("mem-4", status="SUPERSEDED", logical_key="project.database", superseded_by="mem-2"),
        ]
    )
    counts = count_active_by_logical_key(read_memory_projection(db))
    assert counts[("core_router", "direct", "SEMANTIC", "project.database")] == 2
    assert counts[("core_router", "direct", "SEMANTIC", "project.package_manager")] == 1


def test_state_diff_extra_active_row_detected():
    db = make_db([row("mem-1", logical_key="project.database")])
    from app.core.evaluation.stateful_memory_dataset import MemoryRecordExpectation

    expected = [
        MemoryRecordExpectation.model_validate(
            {
                "alias": "db",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "SQLite",
            }
        )
    ]
    diffs = state_diff(expected, read_memory_projection(db))
    assert diffs == ()

    db2 = make_db(
        [
            row("mem-1", logical_key="project.database"),
            row("mem-2", logical_key="project.package_manager", payload='{"value":"uv"}'),
        ]
    )
    diffs = state_diff(expected, read_memory_projection(db2))
    assert any(diff.kind == "EXTRA_ACTIVE_ROW" for diff in diffs)


def test_state_diff_extra_active_row_in_expected_keyed_partition_fails():
    """同 key 的旧 ACTIVE 值也必须是 final-state mismatch。"""
    from app.core.evaluation.stateful_memory_dataset import MemoryRecordExpectation

    expected = [
        MemoryRecordExpectation.model_validate(
            {
                "alias": "db_postgres",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "PostgreSQL",
            }
        )
    ]
    db = make_db(
        [
            row("mem-postgres", payload='{"value":"PostgreSQL"}'),
            row("mem-sqlite", payload='{"value":"SQLite"}'),
        ]
    )

    diffs = state_diff(expected, read_memory_projection(db))

    assert [entry.kind for entry in diffs] == ["EXTRA_ACTIVE_ROW"]


def test_state_diff_supersede_relation_precise_with_alias_binding():
    from app.core.evaluation.stateful_memory_dataset import MemoryRecordExpectation

    expected = [
        MemoryRecordExpectation.model_validate(
            {
                "alias": "db_old",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "SUPERSEDED",
                "value": "SQLite",
                "superseded_by_alias": "db_new",
            }
        ),
        MemoryRecordExpectation.model_validate(
            {
                "alias": "db_new",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "PostgreSQL",
            }
        ),
    ]
    db = make_db(
        [
            row("mem-old", status="SUPERSEDED", payload='{"value":"SQLite"}', superseded_by="mem-new"),
            row("mem-new", status="ACTIVE", payload='{"value":"PostgreSQL"}'),
        ]
    )
    good = state_diff(expected, read_memory_projection(db), alias_binding={"db_old": "mem-old", "db_new": "mem-new"})
    assert good == ()

    db2 = make_db(
        [
            row("mem-old", status="SUPERSEDED", payload='{"value":"SQLite"}', superseded_by="mem-other"),
            row("mem-new", status="ACTIVE", payload='{"value":"PostgreSQL"}'),
            row("mem-other", status="ACTIVE", logical_key="project.package_manager", payload='{"value":"uv"}'),
        ]
    )
    bad = state_diff(expected, read_memory_projection(db2), alias_binding={"db_old": "mem-old", "db_new": "mem-new"})
    assert any(diff.kind == "SUPERSEDE_RELATION_MISMATCH" for diff in bad)
