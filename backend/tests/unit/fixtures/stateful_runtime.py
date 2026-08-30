"""WP5 Stateful Memory 测试 fixtures：deterministic runtime double + fake persistence。

Layer 1 deterministic harness：``ScriptedMemoryTarget`` 是 LocalAgent runtime 的
受控确定性 double（模拟 INSERT/NO_CHANGE/SUPERSEDE/FORGET 生命周期与 retrieval
event 输出），只写入测试拥有的 scenario-isolated SQLite 文件。它不代表 AgentEvalOps
的 mutation authority——它是被测 runtime 的 stand-in。

``FixtureStatefulProvisioner`` 为每个 scenario 创建 fresh isolated memory/journal DB，
验证绑定，并返回绑定到该环境的目标。
"""

# ruff: noqa: D415, D101, D103, D102, D105

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from app.core.evaluation.execution import ExecutionOutcome, ExecutionRequest, ExecutionTarget, OutcomeKind
from app.core.evaluation.references import ArtifactRef, EvidenceRef
from app.core.evaluation.run_attempts import ExecutionAttempt, RunStatus
from app.core.evaluation.stateful_memory_dataset import StatefulMemoryScenario

MEMORY_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS long_term_memory (
  memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, status TEXT NOT NULL,
  agent_id TEXT NOT NULL, memory_scope TEXT NOT NULL, canonical_text TEXT NOT NULL,
  payload TEXT NOT NULL, logical_key TEXT, origin_type TEXT NOT NULL,
  origin_run_id TEXT NOT NULL, origin_exchange_id TEXT NOT NULL,
  origin_agent_id TEXT NOT NULL, origin_memory_scope TEXT NOT NULL,
  formation_method TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  superseded_by_memory_id TEXT
)
"""

JOURNAL_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_event_journal (
  event_id TEXT NOT NULL, run_id TEXT NOT NULL, trace_id TEXT,
  sequence INTEGER NOT NULL, emitted_at TEXT, journaled_at TEXT,
  event_type TEXT NOT NULL, component TEXT, step_id TEXT, step_sequence INTEGER,
  span_id TEXT, parent_span_id TEXT, safe_payload TEXT, payload_digest TEXT,
  event_digest TEXT, PRIMARY KEY (run_id, sequence)
)
"""

_LATIN_RUN = re.compile(r"[0-9a-z_]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_TOKEN_RUN = re.compile(r"[0-9a-z_]+|[\u4e00-\u9fff]+")


def stateful_lexical_tokens(text: str) -> tuple[str, ...]:
    """LocalAgent v1 retrieval tokenizer 的 test-only deterministic mirror."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    for run in _TOKEN_RUN.findall(normalized):
        if _CJK_RUN.fullmatch(run):
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
        else:
            tokens.append(run)
    return tuple(tokens)


def stateful_lexical_score(
    query: str,
    *,
    canonical_text: str,
    logical_key: str | None,
    payload: object,
) -> int:
    """返回 Layer-1 wildcard 需要的 LocalAgent-v1 lexical relevance score。

    本函数只服务 test fixture，模拟 score-positive selection；不复制 production 的
    ranking、top-K、budget 或 context construction。
    """
    candidate_tokens = set(stateful_lexical_tokens(canonical_text))
    if logical_key is not None:
        candidate_tokens.update(stateful_lexical_tokens(logical_key))
    if isinstance(payload, dict):
        values: list[str] = []
        for key in sorted(payload):
            value = payload[key]
            if isinstance(value, bool):
                values.append("true" if value else "false")
            elif isinstance(value, int):
                values.append(str(value))
            elif isinstance(value, float):
                values.append(repr(value))
            elif isinstance(value, str):
                values.append(value)
        if values:
            candidate_tokens.update(stateful_lexical_tokens(" ".join(values)))
    return sum(1 for token in dict.fromkeys(stateful_lexical_tokens(query)) if token in candidate_tokens)


@dataclass(frozen=True, slots=True)
class ScriptedStep:
    """一个 scripted runtime 行为：按 query 匹配后执行确定性操作。"""

    query: str
    operation: str
    logical_key: str | None = None
    value: Any = None
    canonical_text: str | None = None
    selected_ids: tuple[str, ...] = ()
    lexical_wildcard: bool = False
    context_record_count: int = 0
    planning_injected: bool = False
    direct_entry_supplied: bool = False
    error_category: str | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ScriptedMemoryTarget:
    """确定性 LocalAgent runtime double：写 scenario-owned DB + journal，返回 outcome。"""

    def __init__(
        self,
        memory_db: Path,
        journal_db: Path,
        scripts: dict[str, ScriptedStep],
        *,
        agent_id: str = "core_router",
        memory_scope: str = "direct",
    ) -> None:
        self._memory_db = Path(memory_db)
        self._journal_db = Path(journal_db)
        self._scripts = scripts
        self._agent_id = agent_id
        self._memory_scope = memory_scope
        self._seq = 0

    @property
    def target_ref(self):
        from app.core.evaluation.execution import ExecutionTargetRef

        return ExecutionTargetRef("scripted-memory", "FIXTURE", config_ref=None)

    def _memory_id(self) -> str:
        self._seq += 1
        return f"mem-{self._seq:04d}"

    def _connect(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.executescript(MEMORY_DB_SCHEMA if path == self._memory_db else JOURNAL_DB_SCHEMA)
        return connection

    def _partition(self, connection: sqlite3.Connection, logical_key: str) -> list[tuple]:
        rows = connection.execute(
            "SELECT memory_id, status, payload, canonical_text, created_at, updated_at, "
            "superseded_by_memory_id FROM long_term_memory "
            "WHERE agent_id=? AND memory_scope=? AND logical_key=? ORDER BY created_at",
            (self._agent_id, self._memory_scope, logical_key),
        ).fetchall()
        return [tuple(row) for row in rows]

    def _write_row(
        self,
        connection: sqlite3.Connection,
        *,
        memory_id: str,
        status: str,
        logical_key: str | None,
        value: Any,
        run_id: str,
        superseded_by_memory_id: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        canonical_text: str | None = None,
    ) -> None:
        ts = created_at or _now_iso()
        if status == "FORGOTTEN":
            payload = "{}"
            text = "[FORGOTTEN]"
        else:
            payload = json.dumps({"value": value}, ensure_ascii=False)
            text = canonical_text or f"{logical_key}: {value}"
        connection.execute(
            "INSERT OR REPLACE INTO long_term_memory VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id,
                "SEMANTIC",
                status,
                self._agent_id,
                self._memory_scope,
                text,
                payload,
                logical_key,
                "USER",
                run_id,
                run_id,
                self._agent_id,
                self._memory_scope,
                "llm",
                ts,
                updated_at or ts,
                superseded_by_memory_id,
            ),
        )

    def _journal(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        sequence: int,
    ) -> None:
        connection.execute(
            "INSERT INTO runtime_event_journal (event_id, run_id, sequence, event_type, safe_payload) "
            "VALUES (?,?,?,?,?)",
            (f"ev-{run_id}-{sequence}", run_id, sequence, event_type, json.dumps(payload, ensure_ascii=False)),
        )

    async def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        started = datetime.now(UTC)
        run_id = request.attempt_id
        query = str(request.input_payload["query"])
        script = self._scripts.get(query)
        connection = sqlite3.connect(self._memory_db)
        journal = sqlite3.connect(self._journal_db)
        try:
            connection.executescript(MEMORY_DB_SCHEMA)
            journal.executescript(JOURNAL_DB_SCHEMA)
            if script is None or script.operation == "fail":
                error_category = script.error_category if script else "UNHANDLED_ERROR"
                return ExecutionOutcome(
                    request_id=request.request_id,
                    kind=OutcomeKind.FAILURE,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    error_category=error_category,
                    reason=error_category,
                )
            if script.operation == "ignore":
                self._journal(
                    journal,
                    run_id,
                    "MEMORY_FORMATION_COMPLETED",
                    {
                        "schema_version": "v1",
                        "formation_method": "llm",
                        "status": "OK",
                        "proposed_count": 1,
                        "accepted_count": 0,
                        "ignored_count": 1,
                        "persisted_count": 0,
                        "reused_count": 0,
                        "failed_count": 0,
                        "candidate_outcomes": "1|IGNORED|POLICY",
                    },
                    1,
                )
            elif script.operation == "remember":
                self._remember(connection, journal, run_id, script)
            elif script.operation == "forget":
                self._forget(connection, journal, run_id, script)
            elif script.operation == "retrieve":
                self._retrieve(journal, run_id, script)
            connection.commit()
            journal.commit()
        finally:
            journal.close()
            connection.close()
        return ExecutionOutcome(
            request_id=request.request_id,
            kind=OutcomeKind.SUCCESS,
            started_at=started,
            finished_at=datetime.now(UTC),
            output_artifact_ref=ArtifactRef(artifact_id=f"scripted-run://{run_id}"),
            evidence_refs=(EvidenceRef(kind="localagent_run", identifier=run_id, schema_version="v1"),),
        )

    def _remember(
        self, connection: sqlite3.Connection, journal: sqlite3.Connection, run_id: str, script: ScriptedStep
    ) -> None:
        partition = self._partition(connection, script.logical_key)
        active = [row for row in partition if row[1] == "ACTIVE"]
        if not active:
            memory_id = self._memory_id()
            self._write_row(
                connection,
                memory_id=memory_id,
                status="ACTIVE",
                logical_key=script.logical_key,
                value=script.value,
                run_id=run_id,
                canonical_text=script.canonical_text,
            )
            self._journal(
                journal,
                run_id,
                "MEMORY_FORMATION_COMPLETED",
                {
                    "schema_version": "v1",
                    "formation_method": "llm",
                    "status": "OK",
                    "proposed_count": 1,
                    "accepted_count": 1,
                    "ignored_count": 0,
                    "persisted_count": 1,
                    "reused_count": 0,
                    "failed_count": 0,
                    "candidate_outcomes": f"1|ACCEPTED|{memory_id}",
                },
                1,
            )
            self._journal(
                journal,
                run_id,
                "MEMORY_LIFECYCLE_RESOLVED",
                {
                    "schema_version": "v1",
                    "memory_type": "SEMANTIC",
                    "operation": "INSERT",
                    "outcome": "OK",
                    "affected_count": 1,
                    "winner_memory_id": memory_id,
                    "new_memory_id": memory_id,
                    "candidate_outcome": "ACCEPTED",
                    "affected_transitions": "NONE",
                },
                2,
            )
            return
        winner = active[0]
        winner_payload = json.loads(winner[2])
        if winner_payload.get("value") == script.value:
            self._journal(
                journal,
                run_id,
                "MEMORY_FORMATION_COMPLETED",
                {
                    "schema_version": "v1",
                    "formation_method": "llm",
                    "status": "OK",
                    "proposed_count": 1,
                    "accepted_count": 1,
                    "ignored_count": 0,
                    "persisted_count": 0,
                    "reused_count": 1,
                    "failed_count": 0,
                    "candidate_outcomes": f"1|REUSED|{winner[0]}",
                },
                1,
            )
            self._journal(
                journal,
                run_id,
                "MEMORY_LIFECYCLE_RESOLVED",
                {
                    "schema_version": "v1",
                    "memory_type": "SEMANTIC",
                    "operation": "NO_CHANGE",
                    "outcome": "OK",
                    "affected_count": 0,
                    "winner_memory_id": winner[0],
                    "new_memory_id": None,
                    "candidate_outcome": "REUSED",
                    "affected_transitions": "NONE",
                },
                2,
            )
            return
        new_memory_id = self._memory_id()
        for row in active:
            connection.execute(
                "UPDATE long_term_memory SET status='SUPERSEDED', superseded_by_memory_id=? WHERE memory_id=?",
                (new_memory_id, row[0]),
            )
        self._write_row(
            connection,
            memory_id=new_memory_id,
            status="ACTIVE",
            logical_key=script.logical_key,
            value=script.value,
            run_id=run_id,
            canonical_text=script.canonical_text,
        )
        self._journal(
            journal,
            run_id,
            "MEMORY_FORMATION_COMPLETED",
            {
                "schema_version": "v1",
                "formation_method": "llm",
                "status": "OK",
                "proposed_count": 1,
                "accepted_count": 1,
                "ignored_count": 0,
                "persisted_count": 1,
                "reused_count": 0,
                "failed_count": 0,
                "candidate_outcomes": f"1|ACCEPTED|{new_memory_id}",
            },
            1,
        )
        self._journal(
            journal,
            run_id,
            "MEMORY_LIFECYCLE_RESOLVED",
            {
                "schema_version": "v1",
                "memory_type": "SEMANTIC",
                "operation": "SUPERSEDE",
                "outcome": "OK",
                "affected_count": len(active),
                "winner_memory_id": new_memory_id,
                "new_memory_id": new_memory_id,
                "candidate_outcome": "ACCEPTED",
                "affected_transitions": ";".join(f"{row[0]}|ACTIVE|SUPERSEDED" for row in active),
            },
            2,
        )

    def _forget(
        self, connection: sqlite3.Connection, journal: sqlite3.Connection, run_id: str, script: ScriptedStep
    ) -> None:
        partition = self._partition(connection, script.logical_key)
        if not partition:
            self._journal(
                journal,
                run_id,
                "MEMORY_LIFECYCLE_RESOLVED",
                {
                    "schema_version": "v1",
                    "memory_type": "SEMANTIC",
                    "operation": "FORGET",
                    "outcome": "NOT_FOUND",
                    "affected_count": 0,
                    "winner_memory_id": None,
                    "new_memory_id": None,
                    "candidate_outcome": "FORGET",
                    "affected_transitions": "NONE",
                },
                1,
            )
            return
        active = [row for row in partition if row[1] == "ACTIVE"]
        if not active:
            self._journal(
                journal,
                run_id,
                "MEMORY_LIFECYCLE_RESOLVED",
                {
                    "schema_version": "v1",
                    "memory_type": "SEMANTIC",
                    "operation": "FORGET",
                    "outcome": "ALREADY_FORGOTTEN",
                    "affected_count": 0,
                    "winner_memory_id": None,
                    "new_memory_id": None,
                    "candidate_outcome": "FORGET",
                    "affected_transitions": "NONE",
                },
                1,
            )
            return
        for row in partition:
            connection.execute(
                "UPDATE long_term_memory SET status='FORGOTTEN', canonical_text='[FORGOTTEN]', "
                "payload='{}', superseded_by_memory_id=NULL, updated_at=? WHERE memory_id=?",
                (_now_iso(), row[0]),
            )
        self._journal(
            journal,
            run_id,
            "MEMORY_LIFECYCLE_RESOLVED",
            {
                "schema_version": "v1",
                "memory_type": "SEMANTIC",
                "operation": "FORGET",
                "outcome": "OK",
                "affected_count": len(partition),
                "winner_memory_id": None,
                "new_memory_id": None,
                "candidate_outcome": "FORGET",
                "affected_transitions": ";".join(f"{row[0]}|ACTIVE|FORGOTTEN" for row in partition),
            },
            1,
        )

    def _retrieve(self, journal: sqlite3.Connection, run_id: str, script: ScriptedStep) -> None:
        if script.selected_ids == ("*",):
            connection = sqlite3.connect(self._memory_db)
            try:
                rows = connection.execute(
                    "SELECT memory_id, canonical_text, logical_key, payload FROM long_term_memory "
                    "WHERE memory_type='SEMANTIC' AND status='ACTIVE' AND agent_id=? AND memory_scope=? "
                    "ORDER BY memory_id",
                    (self._agent_id, self._memory_scope),
                ).fetchall()
            finally:
                connection.close()
            if script.lexical_wildcard:
                selected_ids = tuple(
                    row[0]
                    for row in rows
                    if stateful_lexical_score(
                        script.query,
                        canonical_text=row[1],
                        logical_key=row[2],
                        payload=json.loads(row[3]),
                    )
                    > 0
                )
            else:
                selected_ids = tuple(row[0] for row in rows)
        else:
            selected_ids = list(script.selected_ids)
        # identity 级 selection evidence（Layer 1 deterministic harness 的扩展证据；
        # 真实 LocalAgent journal 是 content-minimized，不会写出该文件）
        selection_file = self._memory_db.parent / f"retrieval_selection_{run_id}.json"
        selection_file.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "selected_memory_ids": list(selected_ids),
                    "context_record_count": script.context_record_count,
                    "planning_injected": script.planning_injected,
                    "direct_entry_supplied": script.direct_entry_supplied,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._journal(
            journal,
            run_id,
            "MEMORY_RETRIEVAL_COMPLETED",
            {
                "schema_version": "v1",
                "retrieval_method": "lexical",
                "ranking_method": "deterministic",
                "status": "COMPLETE",
                "candidate_count": len(selected_ids),
                "eligible_count": len(selected_ids),
                "selected_count": len(selected_ids),
                "context_record_count": script.context_record_count,
                "malformed_count": 0,
                "omitted_count": 0,
                "registered_selected_count": len(selected_ids),
                "open_selected_count": 0,
                "planning_injected": script.planning_injected,
                "direct_entry_supplied": script.direct_entry_supplied,
            },
            1,
        )


class FixtureStatefulProvisioner:
    """测试用环境 provisioner：每 scenario 一个 fresh isolated DB/journal 目录。"""

    def __init__(self, base_dir: Path | None = None, scripts: dict[str, ScriptedStep] | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir else Path(".")
        self._scripts = scripts or {}
        self._created: list[dict[str, object]] = []

    def _work_dir(self, scenario_id: str) -> Path:
        path = self._base_dir / f"{scenario_id}-{uuid4().hex[:8]}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def provision(self, scenario: StatefulMemoryScenario):
        from app.services.evaluation.stateful_environment import (
            SCENARIO_JOURNAL_DB_FILE,
            SCENARIO_MEMORY_DB_FILE,
            SCENARIO_TOKEN_FILE,
            ScenarioEnvironmentEvidence,
        )

        work_dir = self._work_dir(scenario.scenario_id)
        memory_db = work_dir / SCENARIO_MEMORY_DB_FILE
        journal_db = work_dir / SCENARIO_JOURNAL_DB_FILE
        connection = sqlite3.connect(memory_db)
        connection.executescript(MEMORY_DB_SCHEMA)
        connection.commit()
        connection.close()
        connection = sqlite3.connect(journal_db)
        connection.executescript(JOURNAL_DB_SCHEMA)
        connection.commit()
        connection.close()
        token = f"scn-{uuid4().hex[:12]}"
        evidence = ScenarioEnvironmentEvidence(
            scenario_id=scenario.scenario_id,
            scenario_environment_id=f"env-{uuid4().hex[:8]}",
            scenario_token=token,
            work_dir=work_dir,
            memory_db_path=memory_db,
            journal_db_path=journal_db,
            target_instance_ref=f"scripted-{token}",
            localagent_base_url=None,
            fixture_seeded=False,
            provisioned_at=datetime.now(UTC),
            evaluation_only_harness=True,
        )
        (work_dir / SCENARIO_TOKEN_FILE).write_text(
            json.dumps(
                {
                    "scenario_id": evidence.scenario_id,
                    "scenario_environment_id": evidence.scenario_environment_id,
                    "scenario_token": evidence.scenario_token,
                    "memory_db": evidence.memory_db_path.name,
                    "journal_db": evidence.journal_db_path.name,
                }
            ),
            encoding="utf-8",
        )
        self._created.append({"scenario_id": scenario.scenario_id, "evidence": evidence})
        return evidence

    async def verify_bound(self, evidence) -> bool:
        from app.services.evaluation.stateful_environment import SCENARIO_TOKEN_FILE

        return (
            evidence.memory_db_path.is_file()
            and evidence.journal_db_path.is_file()
            and (evidence.work_dir / SCENARIO_TOKEN_FILE).is_file()
        )

    def build_target(self, evidence, scripts: dict[str, ScriptedStep] | None = None):
        return ScriptedMemoryTarget(
            evidence.memory_db_path,
            evidence.journal_db_path,
            scripts if scripts is not None else self._scripts,
        )

    async def cleanup(self, evidence, *, preserve: bool) -> None:
        # test fixture：保留文件以便断言检查；tmp 目录由 pytest 清理
        return None


class FakeStatefulUow:
    """内存版 EvaluationPersistenceUnitOfWork（create/claim/start/record/finish）。"""

    def __init__(self) -> None:
        self._runs: dict[str, object] = {}
        self._attempts: dict[str, ExecutionAttempt] = {}
        self._outcomes: dict[str, ExecutionOutcome] = {}
        self.runs = SimpleNamespace(
            add_run_with_attempts=self._add_run,
            get_run=AsyncMock(return_value=None),
            lock_run=AsyncMock(return_value=None),
            set_running_if_pending=AsyncMock(return_value=True),
            finish_run=AsyncMock(return_value=True),
        )
        self.attempts = SimpleNamespace(
            get_attempt=AsyncMock(side_effect=self._get_attempt),
            list_attempts=AsyncMock(side_effect=self._list_attempts),
            list_latest_attempts=AsyncMock(side_effect=self._list_latest),
            claim_attempt=AsyncMock(side_effect=self._claim),
            mark_running=AsyncMock(side_effect=self._mark_running),
            record_outcome=AsyncMock(side_effect=self._record),
            create_retry=AsyncMock(),
            list_stale_candidates=AsyncMock(return_value=()),
            reconcile_stale=AsyncMock(return_value=None),
        )
        self.results = SimpleNamespace(
            get_result=AsyncMock(return_value=None),
            list_results=AsyncMock(return_value=()),
            list_finalized_slots=AsyncMock(return_value=frozenset()),
            insert_final_result=AsyncMock(),
        )
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            await self.rollback()

    async def _add_run(self, run, attempts):
        self._runs[str(run.run_id)] = run
        for attempt in attempts:
            self._attempts[str(attempt.attempt_id)] = attempt
        self.runs.get_run.return_value = run
        self.runs.lock_run.return_value = run

    def _get_attempt(self, project_id, attempt_id):
        return self._attempts.get(str(attempt_id))

    def _list_attempts(self, project_id, run_id):
        return tuple(a for a in self._attempts.values() if str(a.run_id) == str(run_id))

    def _list_latest(self, project_id, run_id):
        return self._list_attempts(project_id, run_id)

    def _claim(self, project_id, attempt_id, token, lease, worker_ref, task_ref):
        from dataclasses import replace

        attempt = self._attempts.get(str(attempt_id))
        if attempt is None:
            return None
        self._attempts[str(attempt_id)] = replace(
            attempt,
            status=attempt.status,
            claim_token=token,
            lease_expires_at=None,
            claimed_at=None,
        )
        return self._attempts[str(attempt_id)]

    def _mark_running(self, project_id, attempt_id, token):
        attempt = self._attempts.get(str(attempt_id))
        if attempt is None or attempt.claim_token != token:
            return None
        return attempt

    def _record(self, project_id, attempt_id, token, outcome):
        from dataclasses import replace

        from app.core.evaluation.run_attempts import AttemptStatus

        attempt = self._attempts.get(str(attempt_id))
        if attempt is None or attempt.claim_token != token:
            return None
        terminal = replace(
            attempt,
            status=AttemptStatus.TERMINAL,
            execution_outcome_kind=outcome.kind,
            output_artifact_ref=outcome.output_artifact_ref,
            outcome_evidence_refs=outcome.evidence_refs,
            outcome_metadata=outcome.metadata,
            error_category=outcome.error_category,
            reason=outcome.reason,
            finished_at=outcome.finished_at,
            lease_expires_at=None,
        )
        self._attempts[str(attempt_id)] = terminal
        return terminal

    async def finish_run(self, project_id, run_id, status, reason):
        run = self._runs.get(str(run_id))
        if run is None:
            return False
        return True


def make_fake_persistence():
    from app.services.evaluation.persistence import EvaluationPersistenceService

    uow = FakeStatefulUow()
    return uow, EvaluationPersistenceService(lambda: uow)


__all__ = [
    "FakeStatefulUow",
    "FixtureStatefulProvisioner",
    "ScriptedMemoryTarget",
    "ScriptedStep",
    "make_fake_persistence",
    "stateful_lexical_score",
    "stateful_lexical_tokens",
]
