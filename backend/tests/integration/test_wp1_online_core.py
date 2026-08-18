"""Integration coverage for WP1 normalized queries and legacy ownership."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.online.entities import GenericOutcome, NormalizedOnlineTrace
from app.core.traces.entities import Span, Trace
from app.infrastructure.db.models import OrganizationModel, ProjectModel, SpanModel, TraceModel
from app.infrastructure.db.repositories.trace_repo import (
    NormalizedSourceConflictError,
    SpanOwnershipConflictError,
    TraceOwnershipConflictError,
    TraceRepository,
)
from app.registry.constants import SpanKind, SpanStatusCode, TraceStatus

from .conftest import TEST_PROJECT_ID

pytestmark = pytest.mark.asyncio


PROJECT_B_ORG_ID = UUID("00000000-0000-4000-a000-00000000000b")
PROJECT_B_ID = UUID("00000000-0000-4000-a000-00000000000c")


def _trace(
    *,
    project_id: UUID,
    trace_id: UUID,
    name: str,
    span_id: UUID | None = None,
    span_trace_id: UUID | None = None,
    span_status: SpanStatusCode = SpanStatusCode.OK,
    span_name: str = "operation",
) -> Trace:
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    span = (
        Span(
            span_id=span_id or uuid4(),
            trace_id=span_trace_id or trace_id,
            name=span_name,
            kind=SpanKind.OTHER,
            status=span_status,
            started_at=started_at,
            ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        )
        if span_id is not None or span_trace_id is not None or span_name != "operation"
        else None
    )
    return Trace(
        trace_id=trace_id,
        project_id=project_id,
        name=name,
        status=TraceStatus.ERROR if span_status == SpanStatusCode.ERROR else TraceStatus.COMPLETED,
        started_at=started_at,
        ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        spans=[span] if span is not None else [],
    )


async def _create_project_b(session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    session.add(OrganizationModel(id=PROJECT_B_ORG_ID, name="Project B Org", created_at=now))
    session.add(
        ProjectModel(
            id=PROJECT_B_ID,
            org_id=PROJECT_B_ORG_ID,
            name="Project B",
            description="",
            created_at=now,
        )
    )
    await session.commit()


async def test_trace_collision_fails_closed_and_preserves_owner(db_session: AsyncSession) -> None:
    trace_id = uuid4()
    await TraceRepository(db_session).upsert_trace(
        _trace(project_id=TEST_PROJECT_ID, trace_id=trace_id, name="owner-a")
    )
    await db_session.commit()
    await _create_project_b(db_session)

    with pytest.raises(TraceOwnershipConflictError):
        await TraceRepository(db_session).upsert_trace(
            _trace(project_id=PROJECT_B_ID, trace_id=trace_id, name="attacker-b")
        )
    await db_session.rollback()

    row = (await db_session.execute(select(TraceModel).where(TraceModel.trace_id == trace_id))).scalar_one()
    assert row.project_id == TEST_PROJECT_ID
    assert row.name == "owner-a"


async def test_span_collision_fails_closed_and_rolls_back_new_trace(db_session: AsyncSession) -> None:
    span_id = uuid4()
    trace_a_id = uuid4()
    await TraceRepository(db_session).upsert_trace(
        _trace(project_id=TEST_PROJECT_ID, trace_id=trace_a_id, name="trace-a", span_id=span_id)
    )
    await db_session.commit()
    await _create_project_b(db_session)
    trace_b_id = uuid4()

    with pytest.raises(SpanOwnershipConflictError):
        await TraceRepository(db_session).upsert_trace(
            _trace(
                project_id=PROJECT_B_ID,
                trace_id=trace_b_id,
                name="trace-b",
                span_id=span_id,
                span_trace_id=trace_b_id,
            )
        )
    await db_session.rollback()

    span = (await db_session.execute(select(SpanModel).where(SpanModel.span_id == span_id))).scalar_one()
    assert span.trace_id == trace_a_id
    assert (await db_session.execute(select(TraceModel).where(TraceModel.trace_id == trace_b_id))).scalar_one_or_none() is None


async def test_same_span_different_trace_fails_closed(db_session: AsyncSession) -> None:
    span_id = uuid4()
    trace_a_id = uuid4()
    await TraceRepository(db_session).upsert_trace(
        _trace(project_id=TEST_PROJECT_ID, trace_id=trace_a_id, name="trace-a", span_id=span_id)
    )
    await db_session.commit()
    trace_b_id = uuid4()

    with pytest.raises(SpanOwnershipConflictError):
        await TraceRepository(db_session).upsert_trace(
            _trace(
                project_id=TEST_PROJECT_ID,
                trace_id=trace_b_id,
                name="trace-b",
                span_id=span_id,
                span_trace_id=trace_b_id,
            )
        )
    await db_session.rollback()

    span = (await db_session.execute(select(SpanModel).where(SpanModel.span_id == span_id))).scalar_one()
    assert span.trace_id == trace_a_id


async def test_normalized_source_owner_cannot_be_silently_replaced(db_session: AsyncSession) -> None:
    trace_id = uuid4()
    await TraceRepository(db_session).upsert_trace(
        _trace(project_id=TEST_PROJECT_ID, trace_id=trace_id, name="legacy-owner")
    )
    await db_session.commit()

    with pytest.raises(NormalizedSourceConflictError):
        await TraceRepository(db_session).persist_normalized(
            NormalizedOnlineTrace(
                project_id=TEST_PROJECT_ID,
                trace_id=trace_id,
                source_kind="localagent",
                outcome=GenericOutcome.SUCCESS,
            )
        )
    await db_session.rollback()

    row = (await db_session.execute(select(TraceModel).where(TraceModel.trace_id == trace_id))).scalar_one()
    assert row.normalized_source_kind == "legacy"


async def test_trace_summary_does_not_recover_after_failure_child(db_session: AsyncSession) -> None:
    trace_id = uuid4()
    await TraceRepository(db_session).upsert_trace(
        _trace(
            project_id=TEST_PROJECT_ID,
            trace_id=trace_id,
            name="monotonic-trace",
            span_id=uuid4(),
            span_name="failure.operation",
            span_status=SpanStatusCode.ERROR,
        )
    )
    await db_session.commit()

    await TraceRepository(db_session).upsert_trace(
        _trace(
            project_id=TEST_PROJECT_ID,
            trace_id=trace_id,
            name="monotonic-trace-success-update",
            span_id=uuid4(),
            span_name="success.operation",
            span_status=SpanStatusCode.OK,
        )
    )
    await db_session.commit()

    row = (await db_session.execute(select(TraceModel).where(TraceModel.trace_id == trace_id))).scalar_one()
    assert row.normalized_outcome == GenericOutcome.FAILURE.value


async def test_normalized_query_filters_are_project_scoped(client: AsyncClient, db_session: AsyncSession) -> None:
    await TraceRepository(db_session).upsert_trace(
        _trace(
            project_id=TEST_PROJECT_ID,
            trace_id=uuid4(),
            name="failed-trace",
            span_id=uuid4(),
            span_name="failed.operation",
            span_status=SpanStatusCode.ERROR,
        )
    )
    await TraceRepository(db_session).upsert_trace(
        _trace(
            project_id=TEST_PROJECT_ID,
            trace_id=uuid4(),
            name="success-trace",
            span_id=uuid4(),
            span_name="success.operation",
        )
    )
    await db_session.commit()

    response = await client.get(
        "/traces",
        params={
            "failing": "true",
            "normalized_operation": "failed.operation",
            "normalized_source_kind": "legacy",
            "normalized_outcome": "FAILURE",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "failed-trace"
