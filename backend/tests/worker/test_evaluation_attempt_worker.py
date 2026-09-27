"""真实 Redis broker 和独立 Celery worker 的 canonical attempt 集成测试."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import redis
from sqlalchemy import create_engine, func, select, text

from app.core.evals.entities import PreparedRun
from app.core.traces.entities import Trace
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.models import (
    Base,
    EvaluationResultModel,
    EvaluationRunModel,
    ExecutionAttemptModel,
    OrganizationModel,
    ProjectModel,
)
from app.infrastructure.db.repositories.trace_repo import TraceRepository
from app.registry.constants import TraceStatus
from app.registry.settings import settings
from app.services.eval_service import EvalService


def _worker_log_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as log_file:
            return log_file.read()
    except OSError:
        return ""


def _wait_for_worker(process: subprocess.Popen, log_path: str, timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        log = _worker_log_text(log_path)
        if "ready." in log.lower():
            return log
        time.sleep(0.2)
    log = _worker_log_text(log_path)
    raise RuntimeError(f"Celery worker did not become ready (exit={process.poll()}):\n{log}")


def _stop_worker(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


async def _seed_canonical_attempt(project_id, trace_id):
    now = datetime.now(timezone.utc)
    async with async_session_factory() as session:
        session.add(OrganizationModel(id=project_id, name=f"Worker org {project_id}"))
        session.add(
            ProjectModel(
                id=project_id,
                org_id=project_id,
                name=f"Worker project {project_id}",
                description="",
                created_at=now,
            )
        )
        await session.commit()

        trace = Trace(
            trace_id=trace_id,
            project_id=project_id,
            name="worker-broker-test",
            status=TraceStatus.COMPLETED,
            input={},
            output={},
            metadata={},
            started_at=now,
            ended_at=now,
            session_id=None,
            user_id=None,
            tags=[],
            environment=None,
            release=None,
            spans=[],
        )
        await TraceRepository(session).upsert_trace(trace)
        await session.commit()

        prepared: PreparedRun = await EvalService(session).prepare_batch_eval_run(
            project_id,
            [trace_id],
            ["coherence"],
            name="real-worker-broker-test",
        )
        return prepared


async def _read_canonical_state(project_id, run_id, attempt_id):
    async with async_session_factory() as session:
        run = await session.get(EvaluationRunModel, run_id)
        attempt = await session.get(ExecutionAttemptModel, attempt_id)
        result_count = await session.scalar(
            select(func.count()).select_from(EvaluationResultModel).where(
                EvaluationResultModel.project_id == project_id,
                EvaluationResultModel.run_id == run_id,
                EvaluationResultModel.attempt_id == attempt_id,
            )
        )
        return run, attempt, result_count


def _assert_disposable_test_database() -> None:
    if (
        settings.POSTGRES_HOST not in {"localhost", "127.0.0.1", "::1"}
        or settings.POSTGRES_PORT != 5433
        or settings.POSTGRES_DB != "pandaprobe_test_db"
    ):
        raise RuntimeError(
            "Worker integration test cleanup requires the disposable PostgreSQL "
            "database localhost:5433/pandaprobe_test_db."
        )


async def _cleanup_test_database() -> None:
    _assert_disposable_test_database()
    async with async_session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE localagent_trace_envelope_sidecars, localagent_external_span_identity, "
                "localagent_external_trace_identity, evaluation_results, evaluation_attempts, "
                "evaluation_runs, eval_monitors, session_scores, trace_scores, eval_runs, spans, "
                "traces, api_keys, memberships, users, projects, subscriptions, usage_records, "
                "organizations CASCADE"
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_execute_evaluation_attempt_runs_through_real_redis_worker():
    """Worker execution is proved by canonical PostgreSQL terminal rows/results."""
    try:
        redis_client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1)
        redis_client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis broker unavailable at {settings.REDIS_URL}: {exc}")
    finally:
        if "redis_client" in locals():
            redis_client.close()

    _assert_disposable_test_database()
    sync_engine = create_engine(settings.DATABASE_URL_SYNC)
    Base.metadata.create_all(bind=sync_engine)
    sync_engine.dispose()

    project_id = uuid4()
    trace_id = uuid4()
    run_id = None
    worker = None
    log_handle = tempfile.NamedTemporaryFile(prefix="eval-worker-", suffix=".log", delete=False)
    log_path = log_handle.name
    log_handle.close()
    queue_name = f"eval-worker-{uuid4().hex}"

    try:
        prepared = await _seed_canonical_attempt(project_id, trace_id)
        run_id = prepared.run.id
        worker_env = os.environ.copy()
        worker_env["CELERY_TASK_ALWAYS_EAGER"] = "false"
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.infrastructure.queue.celery_app:celery",
                "worker",
                "--pool=solo",
                "--concurrency=1",
                f"--queues={queue_name}",
                f"--hostname={queue_name}@%h",
                "--loglevel=INFO",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            env=worker_env,
            stdout=open(log_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        _wait_for_worker(worker, log_path)

        from app.infrastructure.queue.celery_app import celery
        celery.conf.task_always_eager = False
        celery.conf.task_default_queue = queue_name
        async with async_session_factory() as session:
            await EvalService(session).dispatch_run(prepared)

        async with async_session_factory() as session:
            attempt_id = await session.scalar(
                select(ExecutionAttemptModel.id)
                .where(ExecutionAttemptModel.run_id == run_id)
                .order_by(ExecutionAttemptModel.created_at)
            )
        assert attempt_id is not None

        deadline = time.monotonic() + 45
        observed = None
        while time.monotonic() < deadline:
            observed = await _read_canonical_state(project_id, run_id, attempt_id)
            run, attempt, result_count = observed
            if run is not None and run.status in {"COMPLETED", "FAILED", "OUTCOME_UNKNOWN"}:
                break
            await asyncio.sleep(0.25)

        assert observed is not None
        run, attempt, result_count = observed
        if run is None or run.status not in {"COMPLETED", "FAILED", "OUTCOME_UNKNOWN"}:
            raise AssertionError(
                "worker did not terminalize canonical run before timeout; "
                f"run={None if run is None else run.status}, "
                f"attempt={None if attempt is None else attempt.status}\n{_worker_log_text(log_path)}"
            )
        assert run.status == "COMPLETED"
        assert attempt is not None and attempt.status == "TERMINAL"
        assert attempt.execution_outcome_kind == "SUCCESS"
        assert result_count == 1
    finally:
        if worker is not None:
            _stop_worker(worker)
        await _cleanup_test_database()
        try:
            os.unlink(log_path)
        except OSError:
            pass
