"""Live LocalAgent HTTP E2E for WP1.

Proves the real cross-repository chain over real HTTP:

    AgentEvalOps EvaluationLoopService
        -> LocalAgentHttpExecutionTarget
        -> real HTTP transport
        -> Local_Agent POST /api/runtime/execute
        -> COORDINATED admission
        -> RunCoordinatorResult projection
        -> ExecutionOutcome
        -> EvaluationPersistenceService.record_outcome (real PostgreSQL)

The Local_Agent server is launched as a subprocess using its own test-only
runtime assembly fixtures (deterministic model), without modifying Local_Agent
source. The real endpoint, admission gate and structured response projection
are production code.
"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.adapters.evaluation import LocalAgentHttpExecutionTarget, LocalAgentHttpExecutionTargetResolver
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
LOCAL_AGENT_REPO = os.environ.get("LOCAL_AGENT_REPO", r"D:\PythonProject\Local_Agent")

LAUNCHER = r"""
import os
import sys
from contextlib import asynccontextmanager

LOCAL_AGENT = os.environ["LOCAL_AGENT_REPO"]
sys.path.insert(0, LOCAL_AGENT)

import uvicorn
import server
from core.chat_service import ChatService
from core.runtime import CoordinatedRuntimeFactory, RunRegistry, RunStatus, StopReason
from tests._runtime_assembly_fixtures import FakeRouter, make_services
from tests.test_runtime_execute_endpoint import _result, _StubScope


class _DynamicFactory(CoordinatedRuntimeFactory):
    def __init__(self, router, services, **kwargs):
        super().__init__(router, services, **kwargs)

    async def create_run_scope(self, *args, **kwargs):
        run_id = kwargs.get("run_id")
        result = _result(RunStatus.SUCCEEDED, StopReason.COMPLETED, run_id=run_id, safe_message="live e2e ok")
        return _StubScope(result)


@asynccontextmanager
async def _noop_lifespan(app):
    yield


def main():
    router = FakeRouter()
    registry = RunRegistry()
    services = make_services(run_registry=registry, snapshot_enabled=False)
    factory = _DynamicFactory(router, services)
    service = ChatService(router, coordinated_runtime_factory=factory, run_registry=registry)
    server.chat_service = service
    server.application_runtime_services = service
    server.app.router.lifespan_context = _noop_lifespan
    port = int(os.environ["LIVE_E2E_PORT"])
    uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class LiveServer:
    proc: subprocess.Popen
    base_url: str

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


@pytest.fixture
def live_localagent(tmp_path: Path) -> LiveServer:
    if not Path(LOCAL_AGENT_REPO).joinpath("server.py").exists():
        pytest.skip(f"Local_Agent repo not found at {LOCAL_AGENT_REPO}")
    port = _free_port()
    launcher = tmp_path / "live_launcher.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    env = dict(os.environ)
    env["LOCAL_AGENT_REPO"] = LOCAL_AGENT_REPO
    env["LIVE_E2E_PORT"] = str(port)
    env["CHAT_RUNTIME_MODE"] = "COORDINATED"
    proc = subprocess.Popen(
        ["uv", "run", "python", str(launcher)],
        cwd=LOCAL_AGENT_REPO,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    server = LiveServer(proc, base_url)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"LocalAgent launcher exited early with code {proc.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("LocalAgent live server did not become ready in time")
        yield server
    finally:
        server.close()


def _make_spec(evaluator_id: str) -> EvaluatorSpec:
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


class PassEvaluator:
    def __init__(self, spec: EvaluatorSpec) -> None:
        self.spec = spec

    async def evaluate(self, evaluation_input: object, context: object) -> EvaluationResultDraft:
        return EvaluationResultDraft(
            self.spec.evaluator_id,
            self.spec.evaluator_version,
            self.spec.config_ref,
            EvaluationVerdict.PASS,
            "live e2e evaluator",
            score=1.0,
            prompt_ref=self.spec.prompt_ref,
        )


class EvaluatorResolver:
    def __init__(self, specs: tuple[EvaluatorSpec, ...]) -> None:
        self.specs = specs

    def resolve(self, spec: EvaluatorSpec) -> ResolvedEvaluator:
        return ResolvedEvaluator(spec.evaluator_id, spec.evaluator_version, PassEvaluator(spec))


def _persistence() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(
        lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
    )


async def _seed(service: EvaluationPersistenceService, specs: tuple[EvaluatorSpec, ...]):
    case_ref = CaseVersionRef("case-live", "v1")
    case = CaseVersion(
        "case-live", "v1", "case-live", {"agent_id": "core_router", "query": "hello"}, NOW, expected_output={"answer": "hi"}
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
    target_ref = ExecutionTargetRef("localagent-coordinated-http", "LOCALAGENT_HTTP", VersionRef("localagent_http_execution_target", "v1"), config_ref=VersionRef("localagent_http_config", "localagent-coordinated-v1"))
    run, (attempt,) = await service.create_run(
        project_id=TEST_PROJECT_ID,
        dataset=dataset,
        suite=suite,
        cases={case_ref: case},
        target=target_ref,
        timeout=timedelta(seconds=30),
    )
    return run, attempt, case, target_ref


@pytest.mark.asyncio
async def test_live_localagent_full_chain_success(db_session, live_localagent):
    specs = (_make_spec("eval"),)
    service = _persistence()
    run, attempt, case, target_ref = await _seed(service, specs)

    resolver = LocalAgentHttpExecutionTargetResolver(base_url=live_localagent.base_url)
    loop = EvaluationLoopService(
        service,
        resolver,
        EvaluatorResolver(specs),
        uuid_factory=uuid4,
        clock=lambda: NOW,
    )
    result = await loop.execute_attempt(
        TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=5)
    )

    assert result is EvaluationLoopResult.PROGRESSED
    stored_attempt = await service.get_attempt(TEST_PROJECT_ID, attempt.attempt_id)
    stored_run = await service.get_run(TEST_PROJECT_ID, run.run_id)

    assert stored_attempt.status is AttemptStatus.TERMINAL
    assert stored_attempt.execution_outcome_kind is OutcomeKind.SUCCESS
    assert stored_attempt.output_artifact_ref is not None
    assert stored_attempt.output_artifact_ref.artifact_id == f"localagent-run://{attempt.attempt_id}"
    assert any(ref.kind == "localagent_run" and ref.identifier == str(attempt.attempt_id)
               for ref in stored_attempt.outcome_evidence_refs)
    assert stored_run.status is RunStatus.COMPLETED

    # Prove LocalAgent run_id == Evaluation attempt_id.
    assert stored_attempt.outcome_metadata.get("localagent_run_id") == str(attempt.attempt_id)
    results = await service.list_results(TEST_PROJECT_ID, run.run_id, attempt.attempt_id)
    assert len(results) == 1 and results[0].verdict is EvaluationVerdict.PASS
