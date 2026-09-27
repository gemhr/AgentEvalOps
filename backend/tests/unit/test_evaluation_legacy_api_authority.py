"""Legacy HTTP service mutations cannot rewrite canonical evaluation facts."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.evals.entities import EvalRun, TraceScore
from app.registry.constants import ScoreSource, ScoreStatus
from app.registry.exceptions import ValidationError
from app.services.eval_service import EvalService


@pytest.mark.asyncio
async def test_canonical_trace_score_cannot_be_edited_or_deleted():
    project_id = uuid4()
    score_id = uuid4()
    score = TraceScore(
        id=score_id,
        trace_id=uuid4(),
        project_id=project_id,
        name="quality",
        value="0.9",
        source=ScoreSource.AUTOMATED,
        status=ScoreStatus.SUCCESS,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session = MagicMock()
    session.scalar = AsyncMock(return_value=uuid4())
    session.commit = AsyncMock()
    service = EvalService(session)
    service._repo = SimpleNamespace(get_score_by_id=AsyncMock(return_value=score))

    with pytest.raises(ValidationError, match="read-only"):
        await service.update_score(score_id, project_id, value="0.1")
    with pytest.raises(ValidationError, match="read-only"):
        await service.delete_score(score_id, project_id)
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_canonical_run_cannot_be_deleted_through_legacy_api():
    project_id = uuid4()
    run = EvalRun(
        id=uuid4(),
        project_id=project_id,
        target_type="TRACE",
        metric_names=["quality"],
        created_at=datetime.now(timezone.utc),
    )
    session = MagicMock()
    session.scalar = AsyncMock(return_value=run.id)
    session.commit = AsyncMock()
    service = EvalService(session)
    service._repo = SimpleNamespace(get_eval_run=AsyncMock(return_value=run))

    with pytest.raises(ValidationError, match="history cannot be deleted"):
        await service.delete_eval_run(run.id, project_id, delete_scores=True)
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [ScoreSource.ANNOTATION, ScoreSource.PROGRAMMATIC])
async def test_non_automated_score_sources_remain_writable(source):
    project_id = uuid4()
    trace_id = uuid4()
    session = MagicMock()
    session.commit = AsyncMock()
    service = EvalService(session)
    service._repo = SimpleNamespace(create_score=AsyncMock())

    score = await service.create_score(project_id, trace_id, "quality", "0.9", source=source)

    assert score.source is source
    service._repo.create_score.assert_awaited_once_with(score)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_rejects_automated_score_before_database_write():
    project_id = uuid4()
    session = MagicMock()
    session.commit = AsyncMock()
    service = EvalService(session)
    service._repo = SimpleNamespace(create_score=AsyncMock())

    with pytest.raises(ValidationError, match="canonical evaluation projection"):
        await service.create_score(project_id, uuid4(), "quality", "0.9", source=ScoreSource.AUTOMATED)

    service._repo.create_score.assert_not_awaited()
    session.commit.assert_not_awaited()
