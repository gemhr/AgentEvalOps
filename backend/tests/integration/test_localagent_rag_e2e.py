"""AC10: real LocalAgent TCP endpoint to PostgreSQL evidence persistence."""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import subprocess
import sys
import textwrap
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from app.adapters.evaluation.http_localagent import (
    LOCALAGENT_HTTP_EVALUATION_CONFIG,
    LOCALAGENT_HTTP_EVALUATION_TARGET_VERSION,
    LOCALAGENT_HTTP_EVALUATION_V2_CONFIG,
    LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION,
    LOCALAGENT_HTTP_TARGET_ID,
    LOCALAGENT_HTTP_TARGET_KIND,
    LocalAgentHttpExecutionTarget,
)
from app.core.evaluation import (
    CaseVersionRef,
    DatasetVersion,
    EvaluationPolicy,
    EvaluationSuiteVersion,
    EvaluatorKind,
    EvaluatorSpec,
    ExecutionTargetRef,
    OutcomeKind,
    ScoreDirection,
    TestCaseVersion as CaseVersion,
    VersionRef,
)
from app.core.evaluation.run_attempts import AttemptStatus
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import PostgresEvaluationPersistenceUnitOfWork
from app.services.evaluation import EvaluationPersistenceService
from tests.integration.conftest import TEST_PROJECT_ID


_LOCAL_AGENT_DIR = Path(r"D:\PythonProject\Local_Agent")
_LOCAL_AGENT_PYTHON = _LOCAL_AGENT_DIR / ".venv" / "Scripts" / "python.exe"

_SERVER_PROGRAM = textwrap.dedent(
    """
    import asyncio
    import sys
    from types import SimpleNamespace

    import uvicorn
    import server
    from core.runtime import (
        BudgetLedger,
        ChatRuntimeMode,
        RetrievalExecutionService,
        RetrievalExecutionStatus,
        RetrievalInvocation,
        RunBudget,
        RunCoordinatorResult,
        RunStatus,
        StopReason,
        create_run_context,
    )
    from tests.test_retrieval_execution import FakeRetrievalAdapter


    def _result(run_id, failed):
        ledger = BudgetLedger(RunBudget())
        return RunCoordinatorResult(
            run_id=run_id,
            plan_id="ac10-deterministic-plan",
            status=RunStatus.FAILED if failed else RunStatus.SUCCEEDED,
            stop_reason=StopReason.UNHANDLED_ERROR if failed else StopReason.COMPLETED,
            succeeded_step_ids=() if failed else ("knowledge-expert",),
            failed_step_ids=("knowledge-expert",) if failed else (),
            cancelled_step_ids=(),
            blocked_step_ids=(),
            budget_snapshot=ledger.snapshot(),
            cleanup_error_codes=(),
            error_code="AC10_RUNTIME_FAILURE" if failed else None,
            safe_message="deterministic AC10 runtime failure" if failed else "",
        )


    class DeterministicEvaluationService:
        admission_gate = SimpleNamespace(accepts_new_runs=True)

        def selected_runtime_mode(self):
            return ChatRuntimeMode.COORDINATED

        async def run_coordinated_agent(self, *, query, run_id, **_kwargs):
            modes = {
                "success": (1, False, False),
                "failure": (1, True, False),
                "multi": (2, False, False),
                "capture-failure": (1, False, True),
                "final-overbound": (1, False, False),
            }
            count, failed, capture_failure = modes[query]
            service = RetrievalExecutionService(FakeRetrievalAdapter())
            for index in range(count):
                context, _ = create_run_context(
                    entry_agent_id="knowledge_expert", run_id=run_id
                )
                context.attach_budget_ledger(
                    BudgetLedger(RunBudget(), deadline_remaining=context.remaining_seconds)
                )
                original_query = "q" * 32769 if capture_failure else f"AC10 query {index + 1}"
                result = service.execute(
                    RetrievalInvocation.create(
                        original_query,
                        collection_names=("kb",),
                        top_k=2,
                        rerank_top_k=2,
                        requested_timeout_seconds=5.0,
                        retrieval_id=f"ac10-retrieval-{index + 1}",
                    ),
                    run_context=context,
                )
                assert result.status is RetrievalExecutionStatus.SUCCEEDED
            output = "unexpected failed output" if failed else "answer-v2"
            if query == "final-overbound":
                output = "中" * 21846
            return output, _result(run_id, failed)


    server.chat_service = DeterministicEvaluationService()
    uvicorn.run(server.app, host="127.0.0.1", port=int(sys.argv[1]), lifespan="off", log_level="warning")
    """
)


def _persistence() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory))


def _target_ref(*, v2: bool = False) -> ExecutionTargetRef:
    return ExecutionTargetRef(
        target_id=LOCALAGENT_HTTP_TARGET_ID,
        target_kind=LOCALAGENT_HTTP_TARGET_KIND,
        target_version_ref=(
            LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION
            if v2
            else LOCALAGENT_HTTP_EVALUATION_TARGET_VERSION
        ),
        config_ref=(
            LOCALAGENT_HTTP_EVALUATION_V2_CONFIG
            if v2
            else LOCALAGENT_HTTP_EVALUATION_CONFIG
        ),
    )


async def _wait_for_tcp(port: int, process: subprocess.Popen[bytes]) -> None:
    for _ in range(100):
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise RuntimeError(f"LocalAgent AC10 server exited early: {stderr}")
        with socket.socket() as probe:
            probe.settimeout(0.05)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        await asyncio.sleep(0.05)
    raise TimeoutError("LocalAgent AC10 server did not open its TCP port")


@pytest.fixture
async def localagent_e2e_url(monkeypatch):
    if not _LOCAL_AGENT_PYTHON.is_file():
        raise RuntimeError(f"LocalAgent test Python is unavailable: {_LOCAL_AGENT_PYTHON}")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    environment = {**os.environ, "PYTHONPATH": str(_LOCAL_AGENT_DIR)}
    process = subprocess.Popen(
        [str(_LOCAL_AGENT_PYTHON), "-c", _SERVER_PROGRAM, str(port)],
        cwd=_LOCAL_AGENT_DIR,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    await _wait_for_tcp(port, process)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def _create_running_attempt(mode: str, *, v2: bool = False):
    now = datetime.now(timezone.utc)
    case_ref = CaseVersionRef(f"ac10-{mode}", "v1")
    case = CaseVersion(case_ref.case_id, case_ref.version, case_ref.case_id, {"agent_id": "core_router", "query": mode}, now)
    dataset = DatasetVersion("ac10-dataset", "v1", "AC10", now, case_version_refs=(case_ref,))
    evaluator = EvaluatorSpec(
        "ac10-placeholder",
        "v1",
        EvaluatorKind.DETERMINISTIC,
        VersionRef("ac10_config", "v1"),
        ScoreDirection.HIGHER_IS_BETTER,
        config_snapshot={"purpose": "persistence-only"},
    )
    suite = EvaluationSuiteVersion("ac10-suite", "v1", (case_ref,), (evaluator,), EvaluationPolicy(), now)
    persistence = _persistence()
    _run, (attempt,) = await persistence.create_run(
        project_id=TEST_PROJECT_ID,
        dataset=dataset,
        suite=suite,
        cases={case_ref: case},
        target=_target_ref(v2=v2),
        timeout=timedelta(seconds=30),
    )
    claim = await persistence.claim_attempt(TEST_PROJECT_ID, attempt.attempt_id, lease=timedelta(minutes=5))
    assert claim.claimed and claim.claim_token is not None
    return persistence, await persistence.start_attempt(TEST_PROJECT_ID, attempt.attempt_id, claim.claim_token), claim.claim_token


async def _execute_persist_reload(mode: str, base_url: str, *, v2: bool = False):
    persistence, running, claim_token = await _create_running_attempt(mode, v2=v2)
    target = LocalAgentHttpExecutionTarget(_target_ref(v2=v2), base_url)
    try:
        outcome = await target.execute(running.execution_request)
    finally:
        await target.aclose()
    terminal = await persistence.record_outcome(TEST_PROJECT_ID, running.attempt_id, claim_token, outcome)
    reloaded = await persistence.get_attempt(TEST_PROJECT_ID, running.attempt_id)
    assert reloaded.status is AttemptStatus.TERMINAL
    assert reloaded == terminal
    return outcome, reloaded


def _rag_refs(attempt):
    return tuple(ref for ref in attempt.outcome_evidence_refs if ref.kind == "rag_evaluation_artifact")


def _final_answer_refs(attempt):
    return tuple(ref for ref in attempt.outcome_evidence_refs if ref.kind == "final_answer")


@pytest.mark.asyncio
async def test_success_artifact_round_trips_over_real_http_and_postgres(localagent_e2e_url):
    outcome, reloaded = await _execute_persist_reload("success", localagent_e2e_url)
    assert outcome.kind is OutcomeKind.SUCCESS
    refs = _rag_refs(reloaded)
    assert len(refs) == 1
    payload = refs[0].metadata["payload"]
    assert refs[0].identifier == payload["artifact_id"]
    assert payload["run_id"] == str(reloaded.attempt_id)
    assert payload["attempt_id"] == str(reloaded.attempt_id)
    assert payload["retrieval_id"] == "ac10-retrieval-1"
    assert payload["schema_version"] == "rag-evaluation-artifact.v1"
    assert payload["retrieval_status"] == "SUCCEEDED"
    assert payload["retrieved_items"]
    assert payload["ranked_items"]
    assert payload["selected_items"]
    assert payload["citations"]


@pytest.mark.asyncio
async def test_runtime_failure_keeps_complete_artifact_after_postgres_reload(localagent_e2e_url):
    outcome, reloaded = await _execute_persist_reload("failure", localagent_e2e_url)
    assert outcome.kind is OutcomeKind.FAILURE
    assert reloaded.execution_outcome_kind is OutcomeKind.FAILURE
    refs = _rag_refs(reloaded)
    assert len(refs) == 1
    assert refs[0].metadata["capture_status"] == "COMPLETE"
    assert refs[0].metadata["payload"]["retrieval_status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_two_retrieval_artifacts_do_not_overwrite_after_postgres_reload(localagent_e2e_url):
    outcome, reloaded = await _execute_persist_reload("multi", localagent_e2e_url)
    assert outcome.kind is OutcomeKind.SUCCESS
    refs = _rag_refs(reloaded)
    assert len(refs) == 2
    assert [ref.metadata["retrieval_id"] for ref in refs] == ["ac10-retrieval-1", "ac10-retrieval-2"]
    assert len({ref.identifier for ref in refs}) == 2


@pytest.mark.asyncio
async def test_capture_failure_preserves_runtime_success_and_capture_metadata(localagent_e2e_url):
    outcome, reloaded = await _execute_persist_reload("capture-failure", localagent_e2e_url)
    assert outcome.kind is OutcomeKind.SUCCESS
    assert not _rag_refs(reloaded)
    assert reloaded.outcome_metadata["rag_evaluation_capture_status"] == "FAILED"
    assert reloaded.outcome_metadata["rag_evaluation_capture_error_code"] == "RAG_EVALUATION_QUERY_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_v2_final_answer_round_trips_over_real_http_and_postgres(localagent_e2e_url):
    outcome, reloaded = await _execute_persist_reload(
        "success", localagent_e2e_url, v2=True
    )

    assert outcome.kind is OutcomeKind.SUCCESS
    refs = _final_answer_refs(reloaded)
    assert len(refs) == 1
    payload = refs[0].metadata["payload"]
    assert refs[0].identifier == f"final-answer://{reloaded.attempt_id}"
    assert payload["run_id"] == str(reloaded.attempt_id)
    assert payload["attempt_id"] == str(reloaded.attempt_id)
    assert payload["content"] == "answer-v2"
    assert payload["content_sha256"] == hashlib.sha256(b"answer-v2").hexdigest()


@pytest.mark.asyncio
async def test_v2_runtime_failure_persists_terminal_without_final_answer(localagent_e2e_url):
    outcome, reloaded = await _execute_persist_reload(
        "failure", localagent_e2e_url, v2=True
    )

    assert outcome.kind is OutcomeKind.FAILURE
    assert reloaded.execution_outcome_kind is OutcomeKind.FAILURE
    assert not _final_answer_refs(reloaded)
    assert reloaded.outcome_metadata["final_answer_capture_status"] == "FAILED"


@pytest.mark.asyncio
async def test_v2_final_answer_over_bound_preserves_runtime_and_rag(localagent_e2e_url):
    outcome, reloaded = await _execute_persist_reload(
        "final-overbound", localagent_e2e_url, v2=True
    )

    assert outcome.kind is OutcomeKind.SUCCESS
    assert len(_rag_refs(reloaded)) == 1
    assert not _final_answer_refs(reloaded)
    assert reloaded.outcome_metadata["final_answer_capture_status"] == "FAILED"
    assert reloaded.outcome_metadata["final_answer_capture_error_code"] == "FINAL_ANSWER_CONTENT_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_v2_persists_rag_and_final_answer_evidence_together(localagent_e2e_url):
    _outcome, reloaded = await _execute_persist_reload(
        "success", localagent_e2e_url, v2=True
    )

    assert len(_rag_refs(reloaded)) == 1
    assert len(_final_answer_refs(reloaded)) == 1
