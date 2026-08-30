"""Runtime journal 只读证据映射与 fail-closed 解析测试。"""

# ruff: noqa: D101, D105, D415

import json
import sqlite3
from pathlib import Path

import pytest

from app.core.evaluation.stateful_journal import (
    JournalEvidenceError,
    MEMORY_FORMATION_COMPLETED,
    MEMORY_LIFECYCLE_RESOLVED,
    MEMORY_RETRIEVAL_COMPLETED,
    has_required_memory_events,
    journal_sequence_watermark,
    read_journal_events,
)

SCHEMA = """
CREATE TABLE runtime_event_journal (
  event_id TEXT NOT NULL, run_id TEXT NOT NULL, trace_id TEXT,
  sequence INTEGER NOT NULL, emitted_at TEXT, journaled_at TEXT,
  event_type TEXT NOT NULL, component TEXT, step_id TEXT, step_sequence INTEGER,
  span_id TEXT, parent_span_id TEXT, safe_payload TEXT, payload_digest TEXT,
  event_digest TEXT, PRIMARY KEY (run_id, sequence)
)
"""


def make_journal(path: Path, rows: list[tuple]) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    for row in rows:
        connection.execute(
            "INSERT INTO runtime_event_journal (event_id, run_id, sequence, event_type, safe_payload) "
            "VALUES (?,?,?,?,?)",
            row,
        )
    connection.commit()
    connection.close()
    return path


def payload(event_type: str, **fields) -> str:
    base = {
        MEMORY_FORMATION_COMPLETED: {
            "schema_version": "v1",
            "formation_method": "llm",
            "status": "OK",
            "proposed_count": 1,
            "accepted_count": 1,
            "ignored_count": 0,
            "persisted_count": 1,
            "reused_count": 0,
            "failed_count": 0,
            "candidate_outcomes": "1|ACCEPTED|mem-1",
        },
        MEMORY_LIFECYCLE_RESOLVED: {
            "schema_version": "v1",
            "memory_type": "SEMANTIC",
            "operation": "INSERT",
            "outcome": "OK",
            "affected_count": 1,
            "winner_memory_id": "mem-1",
            "new_memory_id": "mem-1",
            "candidate_outcome": "ACCEPTED",
            "affected_transitions": "NONE",
        },
        MEMORY_RETRIEVAL_COMPLETED: {
            "schema_version": "v1",
            "retrieval_method": "lexical",
            "ranking_method": "deterministic",
            "status": "COMPLETE",
            "candidate_count": 1,
            "eligible_count": 1,
            "selected_count": 1,
            "context_record_count": 1,
            "malformed_count": 0,
            "omitted_count": 0,
            "registered_selected_count": 1,
            "open_selected_count": 0,
            "planning_injected": True,
            "direct_entry_supplied": False,
        },
    }
    merged = {**base[event_type], **fields}
    return json.dumps(merged, ensure_ascii=False)


def test_journal_event_mapping():
    journal = make_journal(
        Path("test-journal.db"),
        [
            ("e1", "run-1", 1, MEMORY_FORMATION_COMPLETED, payload(MEMORY_FORMATION_COMPLETED)),
            ("e2", "run-1", 2, MEMORY_LIFECYCLE_RESOLVED, payload(MEMORY_LIFECYCLE_RESOLVED, operation="INSERT")),
            (
                "e3",
                "run-1",
                3,
                MEMORY_RETRIEVAL_COMPLETED,
                payload(MEMORY_RETRIEVAL_COMPLETED, planning_injected=True, direct_entry_supplied=True),
            ),
        ],
    )
    try:
        events = read_journal_events(journal, "run-1")
        assert len(events.formation) == 1
        assert events.formation[0].persisted_count == 1
        assert len(events.lifecycle) == 1
        assert events.lifecycle[0].operation == "INSERT"
        assert len(events.retrieval) == 1
        assert events.retrieval[0].planning_injected is True
        assert events.retrieval[0].direct_entry_supplied is True
    finally:
        journal.unlink(missing_ok=True)


def test_journal_accepts_current_localagent_integer_event_schema_version():
    journal = make_journal(
        Path("integer-schema-journal.db"),
        [
            (
                "e1",
                "run-1",
                1,
                MEMORY_RETRIEVAL_COMPLETED,
                payload(MEMORY_RETRIEVAL_COMPLETED, schema_version=1),
            )
        ],
    )
    try:
        events = read_journal_events(journal, "run-1")
        assert events.retrieval[0].selected_count == 1
    finally:
        journal.unlink(missing_ok=True)


def test_journal_accepts_current_localagent_safe_memory_payload_shape():
    journal = make_journal(
        Path("current-memory-payload-journal.db"),
        [
            (
                "e1",
                "run-1",
                1,
                MEMORY_FORMATION_COMPLETED,
                payload(
                    MEMORY_FORMATION_COMPLETED,
                    schema_version=1,
                    formation_method="HYBRID",
                    status="SUCCEEDED",
                    exchange_id="exchange-1",
                    agent_id="core_router",
                    memory_scope="direct",
                    formation_total_duration_ms=1,
                    model_extraction_duration_ms=1,
                    persistence_duration_ms=0,
                ),
            ),
            (
                "e2",
                "run-1",
                2,
                MEMORY_LIFECYCLE_RESOLVED,
                payload(
                    MEMORY_LIFECYCLE_RESOLVED,
                    schema_version=1,
                    exchange_id="exchange-1",
                    agent_id="core_router",
                    memory_scope="direct",
                    resolution_duration_ms=1,
                    mutation_duration_ms=0,
                    ids_truncated=False,
                    omitted_count=0,
                    safe_reason="",
                ),
            ),
            (
                "e3",
                "run-1",
                3,
                MEMORY_RETRIEVAL_COMPLETED,
                payload(
                    MEMORY_RETRIEVAL_COMPLETED,
                    schema_version=1,
                    retrieval_method="SQLITE_BOUNDED_LEXICAL_V1",
                    ranking_method="DETERMINISTIC_LEXICAL_V1",
                    status="SUCCEEDED",
                    budget_used_chars=12,
                    duration_ms=3,
                ),
            ),
        ],
    )
    try:
        events = read_journal_events(journal, "run-1")
        assert len(events.formation) == len(events.lifecycle) == len(events.retrieval) == 1
    finally:
        journal.unlink(missing_ok=True)


def test_journal_unknown_event_schema_version_fails_closed():
    journal = make_journal(
        Path("unknown-schema-journal.db"),
        [
            (
                "e1",
                "run-1",
                1,
                MEMORY_RETRIEVAL_COMPLETED,
                payload(MEMORY_RETRIEVAL_COMPLETED, schema_version=2),
            )
        ],
    )
    try:
        with pytest.raises(JournalEvidenceError, match="schema_version"):
            read_journal_events(journal, "run-1")
    finally:
        journal.unlink(missing_ok=True)


def test_journal_missing_db_fails_closed():
    with pytest.raises(JournalEvidenceError, match="missing"):
        read_journal_events(Path("missing-journal.db"), "run-1")


def test_journal_schema_mismatch_fails_closed():
    path = Path("bad-journal.db")
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE runtime_event_journal (nope TEXT)")
    connection.commit()
    connection.close()
    try:
        with pytest.raises(JournalEvidenceError, match="schema"):
            read_journal_events(path, "run-1")
    finally:
        path.unlink(missing_ok=True)


def test_journal_malformed_payload_fails_closed():
    journal = make_journal(
        Path("malformed-journal.db"),
        [("e1", "run-1", 1, MEMORY_FORMATION_COMPLETED, "not-json")],
    )
    try:
        with pytest.raises(JournalEvidenceError, match="not valid JSON"):
            read_journal_events(journal, "run-1")
    finally:
        journal.unlink(missing_ok=True)


def test_journal_non_memory_event_is_not_a_memory_evidence_error():
    journal = make_journal(
        Path("unknown-event.db"),
        [("e1", "run-1", 1, "UNKNOWN_EVENT", "{}")],
    )
    try:
        events = read_journal_events(journal, "run-1")
        assert events.formation == ()
        assert events.lifecycle == ()
        assert events.retrieval == ()
    finally:
        journal.unlink(missing_ok=True)


def test_journal_ignores_non_memory_runtime_events_in_same_run():
    journal = make_journal(
        Path("mixed-runtime-journal.db"),
        [
            ("e1", "run-1", 1, "PLAN_CREATED", "{}"),
            ("e2", "run-1", 2, MEMORY_FORMATION_COMPLETED, payload(MEMORY_FORMATION_COMPLETED)),
        ],
    )
    try:
        events = read_journal_events(journal, "run-1")
        assert len(events.formation) == 1
    finally:
        journal.unlink(missing_ok=True)


def test_journal_extra_fields_forbidden():
    journal = make_journal(
        Path("extra-field.db"),
        [
            (
                "e1",
                "run-1",
                1,
                MEMORY_FORMATION_COMPLETED,
                payload(MEMORY_FORMATION_COMPLETED, canonical_text="raw memory text"),
            )
        ],
    )
    try:
        with pytest.raises(JournalEvidenceError):
            read_journal_events(journal, "run-1")
    finally:
        journal.unlink(missing_ok=True)


def test_journal_run_filtering():
    journal = make_journal(
        Path("filtered-journal.db"),
        [
            ("e1", "run-1", 1, MEMORY_FORMATION_COMPLETED, payload(MEMORY_FORMATION_COMPLETED)),
            ("e2", "run-2", 1, MEMORY_LIFECYCLE_RESOLVED, payload(MEMORY_LIFECYCLE_RESOLVED, operation="SUPERSEDE")),
        ],
    )
    try:
        run1 = read_journal_events(journal, "run-1")
        assert len(run1.formation) == 1 and len(run1.lifecycle) == 0
        run2 = read_journal_events(journal, "run-2")
        assert len(run2.formation) == 0 and run2.lifecycle[0].operation == "SUPERSEDE"
    finally:
        journal.unlink(missing_ok=True)


# ------------------------------------------------------------------ E1-R2 journal settle


def test_journal_sequence_watermark_and_required_events():
    journal = make_journal(
        Path("watermark-journal.db"),
        [
            ("e1", "run-1", 1, MEMORY_FORMATION_COMPLETED, payload(MEMORY_FORMATION_COMPLETED)),
            ("e2", "run-1", 2, MEMORY_LIFECYCLE_RESOLVED, payload(MEMORY_LIFECYCLE_RESOLVED, operation="INSERT")),
        ],
    )
    try:
        assert journal_sequence_watermark(journal, "run-1") == 2
        assert journal_sequence_watermark(journal, "run-2") == 0
        events = read_journal_events(journal, "run-1")
        assert (
            has_required_memory_events(
                events, expects_formation=True, expects_lifecycle=False, expects_retrieval=False
            )
            is True
        )
        assert (
            has_required_memory_events(events, expects_formation=True, expects_lifecycle=True, expects_retrieval=False)
            is True
        )
        # no retrieval event in this fixture -> expects_retrieval=True is not satisfied
        assert (
            has_required_memory_events(events, expects_formation=True, expects_lifecycle=True, expects_retrieval=True)
            is False
        )
    finally:
        journal.unlink(missing_ok=True)


def test_journal_settle_evidence_validation():
    from app.core.evaluation.stateful_journal import JournalSettleEvidence

    with pytest.raises(ValueError, match="positive"):
        JournalSettleEvidence(0, 0, 0, "EXPECTED_EVIDENCE_OBSERVED")
    with pytest.raises(ValueError, match="below"):
        JournalSettleEvidence(2, 1, 1, "EXPECTED_EVIDENCE_OBSERVED")
    with pytest.raises(ValueError, match="stop_reason"):
        JournalSettleEvidence(0, 0, 1, "SOME_OTHER_REASON")
    settle = JournalSettleEvidence(1, 3, 2, "EXPECTED_EVIDENCE_OBSERVED")
    assert settle.to_dict()["initial_sequence_watermark"] == 1
    assert settle.to_dict()["final_sequence_watermark"] == 3
