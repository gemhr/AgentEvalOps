"""Integration tests for evaluation monitor endpoints."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio as aioredis
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models import EvalMonitorModel, EvalRunModel, EvaluationRunModel, ExecutionAttemptModel
from app.infrastructure.queue import tasks
from app.services.eval_service import EvalService
from app.services.usage_service import UsageService
from tests.integration.conftest import TEST_PROJECT_ID


async def _assert_monitor_run_linkage(session: AsyncSession, monitor_id):
    monitor = await session.get(EvalMonitorModel, monitor_id)
    assert monitor is not None and monitor.last_run_id is not None
    legacy = await session.get(EvalRunModel, monitor.last_run_id)
    canonical = await session.get(EvaluationRunModel, monitor.last_run_id)
    attempt_count = await session.scalar(
        select(func.count()).select_from(ExecutionAttemptModel).where(
            ExecutionAttemptModel.project_id == TEST_PROJECT_ID,
            ExecutionAttemptModel.run_id == monitor.last_run_id,
        )
    )
    assert legacy is not None and legacy.canonical_run_id == monitor.last_run_id
    assert canonical is not None
    assert attempt_count and attempt_count > 0


# ---------------------------------------------------------------------------
# Monitor creation
# ---------------------------------------------------------------------------


async def test_create_monitor_returns_201(client: AsyncClient, seed_trace):
    await seed_trace(session_id="mon-sess-1")
    resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Daily trace eval",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Daily trace eval"
    assert data["target_type"] == "TRACE"
    assert data["metric_names"] == ["task_completion"]
    assert data["cadence"] == "daily"
    assert data["status"] == "ACTIVE"
    assert data["only_if_changed"] is True
    assert data["next_run_at"] is not None


async def test_create_monitor_session_target(client: AsyncClient, seed_trace):
    await seed_trace(session_id="mon-sess-2")
    resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Weekly session eval",
            "target_type": "SESSION",
            "metrics": ["agent_reliability"],
            "cadence": "weekly",
            "signal_weights": {"confidence": 0.5, "coherence": 0.3},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["target_type"] == "SESSION"
    assert data["filters"]["signal_weights"]["confidence"] == 0.5


async def test_create_monitor_invalid_metric_returns_422(client: AsyncClient):
    resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Bad metric monitor",
            "target_type": "TRACE",
            "metrics": ["nonexistent_metric"],
            "cadence": "daily",
        },
    )
    assert resp.status_code == 422


async def test_create_monitor_invalid_cadence_returns_422(client: AsyncClient):
    resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Bad cadence monitor",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "biweekly",
        },
    )
    assert resp.status_code == 422


async def test_create_monitor_invalid_target_type_returns_422(client: AsyncClient):
    resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Bad type",
            "target_type": "INVALID",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    assert resp.status_code == 422


async def test_create_monitor_signal_weights_rejected_for_trace(client: AsyncClient):
    resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Weights on trace",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
            "signal_weights": {"confidence": 0.5},
        },
    )
    assert resp.status_code == 422


async def test_create_monitor_cron_cadence(client: AsyncClient, seed_trace):
    await seed_trace()
    resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Cron monitor",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "cron:0 9 * * 1-5",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["cadence"] == "cron:0 9 * * 1-5"


# ---------------------------------------------------------------------------
# Monitor listing
# ---------------------------------------------------------------------------


async def test_list_monitors(client: AsyncClient, seed_trace):
    await seed_trace()
    await client.post(
        "/evaluations/monitors",
        json={
            "name": "Monitor A",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    await client.post(
        "/evaluations/monitors",
        json={
            "name": "Monitor B",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "weekly",
        },
    )
    resp = await client.get("/evaluations/monitors")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2
    assert len(data["items"]) >= 2


async def test_list_monitors_filter_by_status(client: AsyncClient, seed_trace):
    await seed_trace()
    create_resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "To Pause",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    monitor_id = create_resp.json()["id"]
    await client.post(f"/evaluations/monitors/{monitor_id}/pause")

    resp = await client.get("/evaluations/monitors?status=PAUSED")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert all(m["status"] == "PAUSED" for m in items)


# ---------------------------------------------------------------------------
# Monitor detail
# ---------------------------------------------------------------------------


async def test_get_monitor_detail(client: AsyncClient, seed_trace):
    await seed_trace()
    create_resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Detail test",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "every_6h",
        },
    )
    monitor_id = create_resp.json()["id"]
    resp = await client.get(f"/evaluations/monitors/{monitor_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == monitor_id


async def test_get_monitor_not_found(client: AsyncClient):
    resp = await client.get("/evaluations/monitors/00000000-0000-4000-a000-000000000099")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Monitor update
# ---------------------------------------------------------------------------


async def test_update_monitor(client: AsyncClient, seed_trace):
    await seed_trace()
    create_resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Before update",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    monitor_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/evaluations/monitors/{monitor_id}",
        json={"name": "After update", "cadence": "weekly"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "After update"
    assert data["cadence"] == "weekly"


# ---------------------------------------------------------------------------
# Monitor delete
# ---------------------------------------------------------------------------


async def test_delete_monitor(client: AsyncClient, seed_trace):
    await seed_trace()
    create_resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "To delete",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    monitor_id = create_resp.json()["id"]

    resp = await client.delete(f"/evaluations/monitors/{monitor_id}")
    assert resp.status_code == 204

    get_resp = await client.get(f"/evaluations/monitors/{monitor_id}")
    assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# Pause / Resume
# ---------------------------------------------------------------------------


async def test_pause_monitor(client: AsyncClient, seed_trace):
    await seed_trace()
    create_resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Pausable",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    monitor_id = create_resp.json()["id"]

    resp = await client.post(f"/evaluations/monitors/{monitor_id}/pause")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "PAUSED"
    assert data["next_run_at"] is None


async def test_pause_already_paused_is_idempotent(client: AsyncClient, seed_trace):
    await seed_trace()
    create_resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Double pause",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    monitor_id = create_resp.json()["id"]
    await client.post(f"/evaluations/monitors/{monitor_id}/pause")

    resp = await client.post(f"/evaluations/monitors/{monitor_id}/pause")
    assert resp.status_code == 200
    assert resp.json()["status"] == "PAUSED"


async def test_resume_monitor(client: AsyncClient, seed_trace):
    await seed_trace()
    create_resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Resumable",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    monitor_id = create_resp.json()["id"]

    await client.post(f"/evaluations/monitors/{monitor_id}/pause")
    resp = await client.post(f"/evaluations/monitors/{monitor_id}/resume")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ACTIVE"
    assert data["next_run_at"] is not None


async def test_resume_already_active_is_idempotent(client: AsyncClient, seed_trace):
    await seed_trace()
    create_resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Already active",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    monitor_id = create_resp.json()["id"]

    resp = await client.post(f"/evaluations/monitors/{monitor_id}/resume")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


async def test_trigger_monitor_creates_run(client: AsyncClient, seed_trace):
    await seed_trace()
    create_resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Trigger test",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    monitor_id = create_resp.json()["id"]

    resp = await client.post(f"/evaluations/monitors/{monitor_id}/trigger")
    assert resp.status_code == 202
    run_data = resp.json()
    assert run_data["monitor_id"] == monitor_id
    assert run_data["target_type"] == "TRACE"
    assert run_data["name"].startswith("[Monitor]")


@pytest.mark.asyncio
async def test_manual_monitor_dispatch_creates_projection_before_fk_linkage(db_session, seed_trace, monkeypatch):
    await seed_trace()
    service = EvalService(db_session)
    monitor = await service.create_monitor(
        TEST_PROJECT_ID, "Manual boundary", "TRACE", ["task_completion"], "daily"
    )
    dispatch = MagicMock()
    monkeypatch.setattr(tasks.execute_evaluation_attempt, "apply_async", dispatch)
    monkeypatch.setattr(tasks.execute_eval_run, "delay", MagicMock(side_effect=AssertionError("legacy task dispatched")))
    monkeypatch.setattr(tasks.execute_session_eval_run, "delay", MagicMock(side_effect=AssertionError("legacy task dispatched")))

    prepared = await service.prepare_trigger_monitor(monitor.id, TEST_PROJECT_ID)
    assert (await db_session.get(EvalMonitorModel, monitor.id)).last_run_id is None
    await service.dispatch_trigger_monitor(prepared)

    await _assert_monitor_run_linkage(db_session, monitor.id)
    dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_scheduled_monitor_dispatch_creates_projection_before_fk_linkage(
    db_session, seed_trace, monkeypatch
):
    await seed_trace()
    service = EvalService(db_session)
    monitor = await service.create_monitor(
        TEST_PROJECT_ID, "Scheduled boundary", "TRACE", ["task_completion"], "daily"
    )
    dispatch = MagicMock()
    monkeypatch.setattr(tasks.execute_evaluation_attempt, "apply_async", dispatch)
    monkeypatch.setattr(tasks.execute_eval_run, "delay", MagicMock(side_effect=AssertionError("legacy task dispatched")))
    monkeypatch.setattr(tasks.execute_session_eval_run, "delay", MagicMock(side_effect=AssertionError("legacy task dispatched")))
    fake_redis = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(aioredis, "from_url", MagicMock(return_value=fake_redis))
    monkeypatch.setattr(UsageService, "check_and_increment", AsyncMock())

    @asynccontextmanager
    async def worker_session():
        yield db_session

    monkeypatch.setattr(tasks, "_worker_session", worker_session)
    result = await tasks._process_single_monitor(str(monitor.id), str(TEST_PROJECT_ID))

    await _assert_monitor_run_linkage(db_session, monitor.id)
    assert result["status"] == "spawned"
    dispatch.assert_called_once()


async def test_trigger_paused_monitor_still_works(client: AsyncClient, seed_trace):
    await seed_trace()
    create_resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Paused trigger",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    monitor_id = create_resp.json()["id"]

    await client.post(f"/evaluations/monitors/{monitor_id}/pause")

    resp = await client.post(f"/evaluations/monitors/{monitor_id}/trigger")
    assert resp.status_code == 202
    assert resp.json()["monitor_id"] == monitor_id


# ---------------------------------------------------------------------------
# Monitor runs
# ---------------------------------------------------------------------------


async def test_list_monitor_runs(client: AsyncClient, seed_trace):
    await seed_trace()
    create_resp = await client.post(
        "/evaluations/monitors",
        json={
            "name": "Runs list test",
            "target_type": "TRACE",
            "metrics": ["task_completion"],
            "cadence": "daily",
        },
    )
    monitor_id = create_resp.json()["id"]

    await client.post(f"/evaluations/monitors/{monitor_id}/trigger")

    resp = await client.get(f"/evaluations/monitors/{monitor_id}/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert data["items"][0]["monitor_id"] == monitor_id
