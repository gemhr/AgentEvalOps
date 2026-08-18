"""Integration coverage for WP2 online metrics and evaluation candidates.

Verifies in real PostgreSQL that ``failure_count`` reuses the frozen WP1
failing rule (child span failures count, CANCELLED/TIMEOUT count, UNKNOWN
does not), latency comes from ``ended_at - started_at`` with in-flight rows
excluded, the optional source filter restricts the same Trace set, and
failing-trace candidates resolve through project-scoped evidence references.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.online.entities import GenericOutcome
from app.core.traces.entities import Span, Trace
from app.infrastructure.db.models import OrganizationModel, ProjectModel, SpanModel, TraceModel
from app.infrastructure.db.repositories.trace_repo import TraceRepository
from app.registry.constants import AnalyticsGranularity, SpanKind, SpanStatusCode, TraceStatus
from app.registry.exceptions import NotFoundError
from app.services.trace_service import TraceService

from .conftest import TEST_PROJECT_ID

pytestmark = pytest.mark.asyncio

PROJECT_B_ORG_ID = UUID("00000000-0000-4000-a000-00000000000b")
PROJECT_B_ID = UUID("00000000-0000-4000-a000-00000000000c")

_WINDOW_START = datetime(2026, 2, 1, tzinfo=timezone.utc)
_WINDOW_END = _WINDOW_START + timedelta(days=1)


def _trace(
    *,
    project_id: UUID,
    trace_id: UUID,
    status: TraceStatus = TraceStatus.COMPLETED,
    span_status: SpanStatusCode = SpanStatusCode.OK,
    span_name: str = "operation",
    duration_ms: int = 1000,
) -> Trace:
    """Build a legacy trace; the worker/repository derives normalized facts."""
    started_at = _WINDOW_START + timedelta(milliseconds=duration_ms)
    ended_at = started_at + timedelta(milliseconds=duration_ms)
    return Trace(
        trace_id=trace_id,
        project_id=project_id,
        name="trace",
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        spans=[
            Span(
                span_id=uuid4(),
                trace_id=trace_id,
                parent_span_id=None,
                name=span_name,
                kind=SpanKind.OTHER,
                status=span_status,
                started_at=started_at,
                ended_at=ended_at,
            )
        ],
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


async def test_analytics_failure_count_reuses_wp1_failing_rule(db_session: AsyncSession) -> None:
    # A: SUCCESS.  B: trace-level FAILURE.  C: child span TIMEOUT (trace summary SUCCESS).
    # D: UNKNOWN and no ended_at (in-flight) — counted as request, excluded from latency.
    trace_a = uuid4()
    trace_b = uuid4()
    trace_c = uuid4()
    trace_d = uuid4()
    repo = TraceRepository(db_session)
    await repo.upsert_trace(
        _trace(project_id=TEST_PROJECT_ID, trace_id=trace_a, duration_ms=1000)
    )
    await repo.upsert_trace(
        _trace(
            project_id=TEST_PROJECT_ID,
            trace_id=trace_b,
            status=TraceStatus.ERROR,
            span_status=SpanStatusCode.ERROR,
            duration_ms=2000,
        )
    )
    await repo.upsert_trace(
        _trace(project_id=TEST_PROJECT_ID, trace_id=trace_c, span_name="timeout.operation", duration_ms=500)
    )
    # Trace D: in-flight — counted as a request but has no ended_at, so its
    # latency must be excluded from the SQL aggregation.
    d_started = _WINDOW_START + timedelta(milliseconds=4000)
    await repo.upsert_trace(
        Trace(
            trace_id=trace_d,
            project_id=TEST_PROJECT_ID,
            name="trace",
            status=TraceStatus.PENDING,
            started_at=d_started,
            ended_at=None,
            spans=[
                Span(
                    span_id=uuid4(),
                    trace_id=trace_d,
                    parent_span_id=None,
                    name="operation",
                    kind=SpanKind.OTHER,
                    status=SpanStatusCode.UNSET,
                    started_at=d_started,
                    ended_at=None,
                )
            ],
        )
    )
    # Trace C: simulate a producer that mapped a child span to TIMEOUT while the
    # trace summary stayed SUCCESS — the child EXISTS must still count it.
    await db_session.execute(
        update(SpanModel)
        .where(SpanModel.trace_id == trace_c)
        .values(normalized_outcome=GenericOutcome.TIMEOUT.value)
    )
    await db_session.commit()

    rows = await repo.get_trace_analytics(
        TEST_PROJECT_ID,
        AnalyticsGranularity.DAY,
        _WINDOW_START - timedelta(hours=1),
        _WINDOW_END,
    )
    assert len(rows) == 1
    bucket = rows[0]
    assert bucket.trace_count == 4
    assert bucket.failure_count == 2  # B + C
    assert bucket.error_count == 1  # legacy semantics unchanged (only B is TraceStatus.ERROR)
    # Latency = (1000 + 2000 + 500) / 3; D's NULL ended_at must be excluded.
    assert bucket.avg_latency_ms is not None
    assert abs(float(bucket.avg_latency_ms) - 3500 / 3) < 1.0
    assert bucket.p50_latency_ms is not None
    assert bucket.p90_latency_ms is not None and bucket.p90_latency_ms >= bucket.p50_latency_ms
    assert bucket.p99_latency_ms is not None and bucket.p99_latency_ms >= bucket.p90_latency_ms


async def test_analytics_source_filter_restricts_same_trace_set(db_session: AsyncSession) -> None:
    repo = TraceRepository(db_session)
    await repo.upsert_trace(
        _trace(project_id=TEST_PROJECT_ID, trace_id=uuid4(), span_name="legacy.ok", duration_ms=100)
    )
    await repo.upsert_trace(
        _trace(project_id=TEST_PROJECT_ID, trace_id=uuid4(), span_name="legacy.fail", duration_ms=200)
    )
    localagent_id = uuid4()
    await repo.upsert_trace(
        _trace(project_id=TEST_PROJECT_ID, trace_id=localagent_id, span_name="localagent.ok", duration_ms=300)
    )
    # Simulate the LocalAgent producer projection: source localagent + CANCELLED trace outcome.
    await db_session.execute(
        update(TraceModel)
        .where(TraceModel.trace_id == localagent_id)
        .values(normalized_source_kind="localagent", normalized_outcome=GenericOutcome.CANCELLED.value)
    )
    await db_session.commit()

    localagent_rows = await repo.get_trace_analytics(
        TEST_PROJECT_ID,
        AnalyticsGranularity.DAY,
        _WINDOW_START - timedelta(hours=1),
        _WINDOW_END,
        normalized_source_kind="localagent",
    )
    assert len(localagent_rows) == 1
    assert localagent_rows[0].trace_count == 1
    assert localagent_rows[0].failure_count == 1  # CANCELLED counts as failure
    assert localagent_rows[0].error_count == 0

    all_rows = await repo.get_trace_analytics(
        TEST_PROJECT_ID,
        AnalyticsGranularity.DAY,
        _WINDOW_START - timedelta(hours=1),
        _WINDOW_END,
    )
    assert all_rows[0].trace_count == 3
    assert all_rows[0].failure_count == 1  # only the CANCELLED localagent trace


async def test_evaluation_candidates_are_failing_only_and_project_scoped(db_session: AsyncSession) -> None:
    repo = TraceRepository(db_session)
    failing_a = uuid4()
    success_a = uuid4()
    await repo.upsert_trace(
        _trace(
            project_id=TEST_PROJECT_ID,
            trace_id=failing_a,
            status=TraceStatus.ERROR,
            span_status=SpanStatusCode.ERROR,
            span_name="failed.operation",
            duration_ms=100,
        )
    )
    await repo.upsert_trace(
        _trace(project_id=TEST_PROJECT_ID, trace_id=success_a, span_name="success.operation", duration_ms=100)
    )
    await db_session.commit()

    await _create_project_b(db_session)
    failing_b = uuid4()
    await repo.upsert_trace(
        _trace(
            project_id=PROJECT_B_ID,
            trace_id=failing_b,
            status=TraceStatus.ERROR,
            span_status=SpanStatusCode.ERROR,
            span_name="failed.b",
            duration_ms=100,
        )
    )
    await db_session.commit()

    svc = TraceService(db_session)
    candidates = await svc.list_evaluation_candidates(TEST_PROJECT_ID)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.trace_id == failing_a
    assert candidate.project_id == TEST_PROJECT_ID
    assert candidate.source_kind == "legacy"
    assert candidate.normalized_outcome == GenericOutcome.FAILURE
    assert candidate.evidence_ref.kind == "trace"
    assert candidate.evidence_ref.identifier == str(failing_a)
    assert candidate.evidence_ref.schema_version is None

    # Same-project resolution succeeds; other-project resolution fails closed.
    detail = await svc.get_trace(UUID(candidate.evidence_ref.identifier), TEST_PROJECT_ID)
    assert detail.trace.trace_id == failing_a
    with pytest.raises(NotFoundError):
        await svc.get_trace(UUID(candidate.evidence_ref.identifier), PROJECT_B_ID)


async def test_analytics_endpoint_exposes_failure_count(client: AsyncClient, db_session: AsyncSession) -> None:
    repo = TraceRepository(db_session)
    await repo.upsert_trace(
        _trace(project_id=TEST_PROJECT_ID, trace_id=uuid4(), span_name="ok.operation", duration_ms=100)
    )
    await repo.upsert_trace(
        _trace(
            project_id=TEST_PROJECT_ID,
            trace_id=uuid4(),
            status=TraceStatus.ERROR,
            span_status=SpanStatusCode.ERROR,
            span_name="fail.operation",
            duration_ms=200,
        )
    )
    await db_session.commit()

    resp = await client.get(
        "/traces/analytics",
        params={
            "metric": "volume",
            "granularity": "day",
            "started_after": (_WINDOW_START - timedelta(hours=1)).isoformat(),
            "started_before": _WINDOW_END.isoformat(),
            "normalized_source_kind": "legacy",
        },
    )
    assert resp.status_code == 200
    buckets = resp.json()
    assert len(buckets) == 1
    assert buckets[0]["trace_count"] == 2
    assert buckets[0]["failure_count"] == 1
    assert buckets[0]["error_count"] == 1  # legacy semantics preserved


async def test_trace_detail_exposes_normalized_fields(client: AsyncClient, db_session: AsyncSession) -> None:
    trace_id = uuid4()
    repo = TraceRepository(db_session)
    await repo.upsert_trace(
        _trace(
            project_id=TEST_PROJECT_ID,
            trace_id=trace_id,
            status=TraceStatus.ERROR,
            span_status=SpanStatusCode.ERROR,
            span_name="failed.operation",
            duration_ms=100,
        )
    )
    await db_session.commit()

    resp = await client.get(f"/traces/{trace_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["normalized_source_kind"] == "legacy"
    assert body["normalized_outcome"] == "FAILURE"
    assert body["spans"][0]["normalized_operation"] == "failed.operation"
    assert body["spans"][0]["normalized_outcome"] == "FAILURE"
