"""PostgreSQL-backed EvaluationLoop integration for the LOCALAGENT_HTTP target.

Proves the real chain::

    EvaluationPersistenceService.create_run
        -> real EvaluationRun / ExecutionAttempt
        -> EvaluationLoopService.execute_attempt
        -> production LocalAgentHttpExecutionTargetResolver
        -> LocalAgentHttpExecutionTarget
        -> real HTTP transport (controlled LocalAgent protocol server)
        -> ExecutionOutcome
        -> EvaluationPersistenceService.record_outcome

Uses real PostgreSQL (conftest fixtures) and real HTTP transport to a
controlled in-process LocalAgent protocol server. This is the INTEGRATION
layer; the live LocalAgent E2E is covered separately.
"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from app.adapters.evaluation import (
    LOCALAGENT_HTTP_CONFIG,
    LOCALAGENT_HTTP_TARGET_ID,
    LOCALAGENT_HTTP_TARGET_KIND,
    LOCALAGENT_HTTP_TARGET_VERSION,
    LocalAgentHttpExecutionTarget,
    LocalAgentHttpExecutionTargetResolver,
)
from app.core.evaluation import (
    CaseVersionRef,
    DatasetVersion,
    EvaluationPolicy,
    EvaluationResultDraft,
    EvaluationSuiteVersion,
    EvaluationVerdict,
    EvaluatorKind,
    EvaluatorSpec,
    ExecutionTargetRef,
    OutcomeKind,
    PolicyDisposition,
    ScoreDirection,
    TestCaseVersion as CaseVersion,
    VersionRef,
)
from app.core.evaluation.run_attempts import AttemptStatus, RunStatus
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import PostgresEvaluationPersistenceUnitOfWork
from app.services.evaluation import (
    EvaluationLoopResult,
    EvaluationLoopService,
    EvaluationPersistenceService,
    ResolvedEvaluator,
)

from .conftest import TEST_PROJECT_ID

NOW = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Controlled LocalAgent protocol HTTP server (real HTTP transport)           #
# --------------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        run_id = payload.get("run_id", "")
        terminal = getattr(self.server, "terminal", None) or {}
        response = dict(terminal)
        response.setdefault("run_id", run_id)
        response.setdefault("status", "SUCCEEDED")
        response.setdefault("stop_reason", "COMPLETED")
        response.setdefault("error_code", None)
        response.setdefault("safe_message", None)
        data = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@dataclass
class ControlledServer:
    server: ThreadingHTTPServer
    thread: threading.Thread
    base_url: str
    terminal: dict[str, object]

    def set_terminal(self, terminal: dict[str, object]) -> None:
        self.terminal.clear()
        self.terminal.update(terminal)

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


@pytest.fixture
def controlled_server() -> ControlledServer:
    holder: dict[str, object] = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.terminal = holder  # type: ignore[attr-defined]
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cs = ControlledServer(server, thread, f"http://127.0.0.1:{port}", holder)
    try:
        yield cs
    finally:
        cs.close()


# --------------------------------------------------------------------------- #
# Resolver / evaluator plumbing                                              #
# --------------------------------------------------------------------------- #


class RecordingResolver:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.target: LocalAgentHttpExecutionTarget | None = None

    def resolve(self, target_ref: ExecutionTargetRef) -> LocalAgentHttpExecutionTarget:
        target = LocalAgentHttpExecutionTarget(target_ref, self.base_url)
        self.target = target
        return target


class PassEvaluator:
    def __init__(self, spec: EvaluatorSpec) -> None:
        self.spec = spec

    async def evaluate(self, evaluation_input: object, context: object) -> EvaluationResultDraft:
        return EvaluationResultDraft(
            self.spec.evaluator_id,
            self.spec.evaluator_version,
            self.spec.config_ref,
            EvaluationVerdict.PASS,
            "integration evaluator",
            score=1.0,
            prompt_ref=self.spec.prompt_ref,
        )


class EvaluatorResolver:
    def __init__(self, specs: tuple[EvaluatorSpec, ...]) -> None:
        self.specs = specs

    def resolve(self, spec: EvaluatorSpec) -> ResolvedEvaluator:
        return ResolvedEvaluator(spec.evaluator_id, spec.evaluator_version, PassEvaluator(spec))


def make_spec(evaluator_id: str) -> EvaluatorSpec:
    return EvaluatorSpec(
        evaluator_id,
        "v1",
        EvaluatorKind.DETERMINISTIC,
        VersionRef("config", f"{evaluator_id}-config"),
        ScoreDirection.HIGHER_IS_BETTER,
        score_range=(0.0, 1.0),
        prompt_ref=VersionRef("prompt", f"{evaluator_id}-prompt"),
        required=True,
    )


def localagent_target_ref() -> ExecutionTargetRef:
    return ExecutionTargetRef(
        target_id=LOCALAGENT_HTTP_TARGET_ID,
        target_kind=LOCALAGENT_HTTP_TARGET_KIND,
        target_version_ref=LOCALAGENT_HTTP_TARGET_VERSION,
        config_ref=LOCALAGENT_HTTP_CONFIG,
    )


def persistence() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(
        lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
    )


async def seed(service: EvaluationPersistenceService, specs: tuple[EvaluatorSpec, ...]):
    case_ref = CaseVersionRef("case-la-http", "v1")
    case = CaseVersion(
        "case-la-http",
        "v1",
        "case-la-http",
        {"agent_id": "core_router", "query": "hello"},
        NOW,
        expected_output={"answer": "hi"},
    )
    dataset = DatasetVersion("dataset", "d1", "dataset", NOW, case_version_refs=(case_ref,))
    suite = EvaluationSuiteVersion(
        "suite",
        "s1",
        (case_ref,),
        specs,
        EvaluationPolicy(
            evaluator_error=PolicyDisposition.FAIL,
            evaluator_inconclusive=PolicyDisposition.INCONCLUSIVE,
        ),
        NOW,
        (),
    )
    target_ref = localagent_target_ref()
    run, (attempt,) = await service.create_run(
        project_id=TEST_PROJECT_ID,
        dataset=dataset,
        suite=suite,
        cases={case_ref: case},
        target=target_ref,
        timeout=timedelta(seconds=30),
    )
    return run, attempt, case


# --------------------------------------------------------------------------- #
# Integration scenarios                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_success_persists_outcome_artifact_evidence_and_provenance(db_session, controlled_server):
    specs = (make_spec("eval"),)
    service = persistence()
    run, attempt, case = await seed(service, specs)

    resolver = RecordingResolver(controlled_server.base_url)
    controlled_server.set_terminal(
        {
            "run_id": str(attempt.attempt_id),
            "status": "SUCCEEDED",
            "stop_reason": "COMPLETED",
            "error_code": None,
            "safe_message": None,
        }
    )
    loop = EvaluationLoopService(
        service,
        resolver,
        EvaluatorResolver(specs),
        uuid_factory=uuid4,
        clock=lambda: NOW,
    )
    try:
        result = await loop.execute_attempt(
            TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=5)
        )
    finally:
        if resolver.target is not None:
            await resolver.target.aclose()

    assert result is EvaluationLoopResult.PROGRESSED
    stored_attempt = await service.get_attempt(TEST_PROJECT_ID, attempt.attempt_id)
    stored_run = await service.get_run(TEST_PROJECT_ID, run.run_id)

    assert stored_attempt.status is AttemptStatus.TERMINAL
    assert stored_attempt.execution_outcome_kind is OutcomeKind.SUCCESS
    assert stored_attempt.output_artifact_ref is not None
    assert stored_attempt.output_artifact_ref.artifact_id == f"localagent-run://{attempt.attempt_id}"
    assert any(ref.kind == "localagent_run" and ref.identifier == str(attempt.attempt_id)
               for ref in stored_attempt.outcome_evidence_refs)
    assert stored_attempt.execution_target_ref.target_id == LOCALAGENT_HTTP_TARGET_ID
    assert stored_attempt.execution_target_ref.target_kind == LOCALAGENT_HTTP_TARGET_KIND
    assert stored_attempt.execution_target_ref.target_version_ref == LOCALAGENT_HTTP_TARGET_VERSION
    assert stored_attempt.execution_target_ref.config_ref == LOCALAGENT_HTTP_CONFIG
    assert stored_run.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_structured_failure_persists_non_success_without_results(db_session, controlled_server):
    specs = (make_spec("eval"),)
    service = persistence()
    run, attempt, case = await seed(service, specs)

    resolver = RecordingResolver(controlled_server.base_url)
    controlled_server.set_terminal(
        {
            "run_id": str(attempt.attempt_id),
            "status": "FAILED",
            "stop_reason": "UNHANDLED_ERROR",
            "error_code": "RUNTIME_TEST_ERROR",
            "safe_message": "bounded failure",
        }
    )
    loop = EvaluationLoopService(
        service,
        resolver,
        EvaluatorResolver(specs),
        uuid_factory=uuid4,
        clock=lambda: NOW,
    )
    try:
        result = await loop.execute_attempt(
            TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=5)
        )
    finally:
        if resolver.target is not None:
            await resolver.target.aclose()

    assert result is EvaluationLoopResult.PROGRESSED
    stored_attempt = await service.get_attempt(TEST_PROJECT_ID, attempt.attempt_id)
    stored_run = await service.get_run(TEST_PROJECT_ID, run.run_id)
    results = await service.list_results(TEST_PROJECT_ID, run.run_id, attempt.attempt_id)

    assert stored_attempt.status is AttemptStatus.TERMINAL
    assert stored_attempt.execution_outcome_kind is OutcomeKind.FAILURE
    assert stored_attempt.output_artifact_ref is None
    assert stored_run.status is RunStatus.FAILED
    assert results == ()
