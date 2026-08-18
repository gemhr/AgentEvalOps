"""Real PostgreSQL integration tests for the LocalAgent compatibility endpoint.

Proves: first-envelope 201 + complete readback, exact replay 200 with
zero mutation, conflicting replay 409 with zero mutation, foreign-project
ownership 409, trace/run identity conflict 409, child-first ordering,
parent resolution/conflicts, transaction rollback (commit failure never
returns 2xx), unknown-field/contract failures, payload bound, rate limit,
concurrency convergence and the historical cross-project overwrite P1.
"""

# ruff: noqa: D415

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import redis.asyncio as aioredis
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.localagent.entities import (
    AUTHENTICATION_FAILED,
    CONTRACT_FINGERPRINT_UNSUPPORTED,
    CONTRACT_IDENTITY_UNSUPPORTED,
    CONTRACT_VERSION_UNSUPPORTED,
    ENVELOPE_CONFLICT,
    ENVELOPE_INVALID,
    ENVELOPE_TOO_LARGE,
    EXTERNAL_ID_OWNERSHIP_CONFLICT,
    INGESTION_RATE_LIMITED,
    PERSISTENCE_UNAVAILABLE,
    TRACE_IDENTITY_CONFLICT,
    LocalAgentTraceEnvelopeInV1,
)
from app.core.localagent.validation import canonical_payload_digest
from app.infrastructure.db.engine import async_session_factory, get_db_session
from app.infrastructure.db.models import (
    LocalAgentExternalSpanIdentityModel,
    LocalAgentExternalTraceIdentityModel,
    LocalAgentTraceEnvelopeSidecarModel,
    SpanModel,
    TraceModel,
)
from app.infrastructure.db.repositories.identity_repo import IdentityRepository
from app.infrastructure.db.repositories.project_repo import ProjectRepository
from app.infrastructure.db.repositories.trace_repo import TraceRepository
from app.registry.constants import SpanKind, SpanStatusCode, TraceStatus
from app.registry.security import hash_api_key

from .conftest import TEST_ORG_ID, TEST_PROJECT_ID

pytestmark = pytest.mark.asyncio

FINGERPRINT = "6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab"
URL = "/integrations/localagent/v1/trace-envelopes"


def envelope_payload(**overrides: object) -> dict[str, object]:
    """Valid frozen envelope payload (runtime.step, OK)."""
    payload: dict[str, object] = {
        "contract_identity": "localagent.runtime.trace_export",
        "contract_version": 1,
        "contract_fingerprint": FINGERPRINT,
        "run_id": "run-123",
        "trace_id": "trace-abc",
        "span_id": "span-xyz",
        "parent_span_id": None,
        "step_id": "step-1",
        "operation": "runtime.step",
        "component": "planner",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:05Z",
        "duration_ms": 5000,
        "status": "OK",
        "error_code": None,
        "attributes": {
            "execution_kind": "AGENT",
            "output_policy": "INTERNAL",
            "state": "SUCCEEDED",
            "result_char_count": 120,
        },
    }
    payload.update(overrides)
    return payload


async def _create_key(session: AsyncSession, org_id, raw_key: str) -> str:
    await IdentityRepository(session).create_api_key(
        org_id=org_id,
        key_hash=hash_api_key(raw_key),
        key_prefix=raw_key[:10],
        name="test-localagent",
    )
    await session.commit()
    return raw_key


async def _create_project(session: AsyncSession, org_id, name: str):
    project = await ProjectRepository(session).create_project(org_id, name)
    await session.commit()
    return project


async def _counts(session: AsyncSession) -> dict[str, int]:
    sidecars = (await session.execute(select(func.count()).select_from(LocalAgentTraceEnvelopeSidecarModel))).scalar_one()
    trace_bindings = (
        await session.execute(select(func.count()).select_from(LocalAgentExternalTraceIdentityModel))
    ).scalar_one()
    span_bindings = (
        await session.execute(select(func.count()).select_from(LocalAgentExternalSpanIdentityModel))
    ).scalar_one()
    traces = (await session.execute(select(func.count()).select_from(TraceModel))).scalar_one()
    spans = (await session.execute(select(func.count()).select_from(SpanModel))).scalar_one()
    return {
        "sidecars": sidecars,
        "trace_bindings": trace_bindings,
        "span_bindings": span_bindings,
        "traces": traces,
        "spans": spans,
    }


async def _sidecar_row(session: AsyncSession, span_id: str):
    stmt = select(LocalAgentTraceEnvelopeSidecarModel).where(
        LocalAgentTraceEnvelopeSidecarModel.external_span_id == span_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@pytest.fixture
async def la_api_key(db_session: AsyncSession) -> str:
    """Persisted active API key for the seeded test org."""
    return await _create_key(db_session, TEST_ORG_ID, "sk_pp_test_la_key_0000000000000001")


@pytest.fixture
def la_headers(la_api_key: str) -> dict[str, str]:
    """Headers for the seeded project using the persisted key."""
    return {
        "X-API-Key": la_api_key,
        "X-Project-ID": str(TEST_PROJECT_ID),
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# First commit / readback
# ---------------------------------------------------------------------------


async def test_first_envelope_201_and_complete_readback(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    payload = envelope_payload()
    resp = await client.post(URL, json=payload, headers=la_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"status": "PERSISTED", "error_code": None}

    counts = await _counts(db_session)
    assert counts == {"sidecars": 1, "trace_bindings": 1, "span_bindings": 1, "traces": 1, "spans": 1}

    sidecar = await _sidecar_row(db_session, "span-xyz")
    assert sidecar is not None
    assert sidecar.project_id == TEST_PROJECT_ID
    assert sidecar.external_run_id == "run-123"
    assert sidecar.external_trace_id == "trace-abc"
    assert sidecar.external_span_id == "span-xyz"
    assert sidecar.external_parent_span_id is None
    assert sidecar.step_id == "step-1"
    assert sidecar.operation == "runtime.step"
    assert sidecar.component == "planner"
    assert sidecar.status == "OK"
    assert sidecar.error_code is None
    assert sidecar.duration_ms == 5000.0
    assert sidecar.contract_identity == "localagent.runtime.trace_export"
    assert sidecar.contract_version == 1
    assert sidecar.contract_fingerprint == FINGERPRINT
    assert sidecar.canonical_payload_digest == canonical_payload_digest(
        LocalAgentTraceEnvelopeInV1.model_validate(payload)
    )
    assert sidecar.attributes == {
        "execution_kind": "AGENT",
        "output_policy": "INTERNAL",
        "state": "SUCCEEDED",
        "result_char_count": 120,
    }
    assert sidecar.started_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert sidecar.completed_at == datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)

    trace_binding = (
        await db_session.execute(
            select(LocalAgentExternalTraceIdentityModel).where(
                LocalAgentExternalTraceIdentityModel.external_trace_id == "trace-abc"
            )
        )
    ).scalar_one()
    assert trace_binding.project_id == TEST_PROJECT_ID
    assert trace_binding.run_id == "run-123"
    assert trace_binding.internal_trace_uuid == sidecar.internal_trace_uuid

    span_binding = (
        await db_session.execute(
            select(LocalAgentExternalSpanIdentityModel).where(
                LocalAgentExternalSpanIdentityModel.external_span_id == "span-xyz"
            )
        )
    ).scalar_one()
    assert span_binding.project_id == TEST_PROJECT_ID
    assert span_binding.external_trace_id == "trace-abc"
    assert span_binding.internal_span_uuid == sidecar.internal_span_uuid

    trace_row = (
        await db_session.execute(select(TraceModel).where(TraceModel.trace_id == sidecar.internal_trace_uuid))
    ).scalar_one()
    assert trace_row.project_id == TEST_PROJECT_ID
    assert trace_row.name == "localagent.trace"
    assert trace_row.status == TraceStatus.COMPLETED.value
    assert trace_row.input is None and trace_row.output is None
    assert trace_row.normalized_source_kind == "localagent"
    assert trace_row.normalized_outcome == "SUCCESS"
    assert trace_row.source_contract_identity == "localagent.runtime.trace_export"
    assert trace_row.source_contract_version == 1
    assert trace_row.subject_version_ref is None

    span_row = (
        await db_session.execute(select(SpanModel).where(SpanModel.span_id == sidecar.internal_span_uuid))
    ).scalar_one()
    assert span_row.trace_id == sidecar.internal_trace_uuid
    assert span_row.name == "localagent:runtime.step"
    assert span_row.kind == SpanKind.OTHER.value
    assert span_row.status == SpanStatusCode.OK.value
    assert span_row.error is None  # raw legacy error must remain NULL
    assert span_row.input is None and span_row.output is None
    assert span_row.metadata_ == {}
    assert span_row.normalized_operation == "runtime.step"
    assert span_row.normalized_component == "planner"
    assert span_row.normalized_outcome == "SUCCESS"
    assert span_row.normalized_error_code is None
    assert span_row.normalized_duration_ms == 5000
    assert span_row.normalized_attributes == {}
    assert span_row.started_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert span_row.ended_at == datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)


async def test_localagent_normalized_projection_is_queryable(
    client: AsyncClient, la_headers: dict[str, str]
):
    resp = await client.post(URL, json=envelope_payload(), headers=la_headers)
    assert resp.status_code == 201

    response = await client.get(
        "/traces",
        params={
            "normalized_source_kind": "localagent",
            "source_contract_version": 1,
            "normalized_operation": "runtime.step",
            "normalized_outcome": "SUCCESS",
            "failing": "false",
        },
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_localagent_projection_failure_rolls_back_entire_ingest(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str], monkeypatch
):
    async def fail_projection(*args, **kwargs):
        raise RuntimeError("projection failure")

    monkeypatch.setattr(TraceRepository, "persist_normalized", fail_projection)
    resp = await client.post(URL, json=envelope_payload(), headers=la_headers)
    assert resp.status_code == 500
    assert await _counts(db_session) == {
        "sidecars": 0,
        "trace_bindings": 0,
        "span_bindings": 0,
        "traces": 0,
        "spans": 0,
    }


async def test_legacy_trace_readback_through_existing_endpoint(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    """The compatibility write is visible through the existing legacy read path."""
    resp = await client.post(URL, json=envelope_payload(), headers=la_headers)
    assert resp.status_code == 201
    sidecar = await _sidecar_row(db_session, "span-xyz")
    detail = await client.get(f"/traces/{sidecar.internal_trace_uuid}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["name"] == "localagent.trace"
    assert len(body["spans"]) == 1
    assert body["spans"][0]["error"] is None


# ---------------------------------------------------------------------------
# Duplicate semantics
# ---------------------------------------------------------------------------


async def test_exact_replay_200_zero_mutation(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    payload = envelope_payload()
    first = await client.post(URL, json=payload, headers=la_headers)
    assert first.status_code == 201

    sidecar_before = await _sidecar_row(db_session, "span-xyz")
    digest_before = sidecar_before.canonical_payload_digest
    counts_before = await _counts(db_session)

    replay = await client.post(URL, json=payload, headers=la_headers)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {"status": "DUPLICATE_ACCEPTED", "error_code": None}

    counts_after = await _counts(db_session)
    assert counts_after == counts_before
    sidecar_after = await _sidecar_row(db_session, "span-xyz")
    assert sidecar_after.canonical_payload_digest == digest_before
    assert sidecar_after.envelope_id == sidecar_before.envelope_id


async def test_replay_with_different_json_order_still_exact_duplicate(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    """Canonical digest makes key order / timezone spelling irrelevant."""
    import json as _json

    payload = envelope_payload()
    assert (await client.post(URL, json=payload, headers=la_headers)).status_code == 201

    reordered = _json.loads(_json.dumps(payload, sort_keys=False))
    replay = await client.post(URL, json=reordered, headers=la_headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "DUPLICATE_ACCEPTED"

    tz_variant = dict(payload, started_at="2026-01-01T00:00:00+00:00", completed_at="2026-01-01T00:00:05+00:00")
    replay2 = await client.post(URL, json=tz_variant, headers=la_headers)
    assert replay2.status_code == 200, replay2.text


async def test_conflicting_replay_409_zero_mutation(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    payload = envelope_payload()
    assert (await client.post(URL, json=payload, headers=la_headers)).status_code == 201
    counts_before = await _counts(db_session)
    digest_before = (await _sidecar_row(db_session, "span-xyz")).canonical_payload_digest

    conflicting = dict(payload, duration_ms=9000)
    resp = await client.post(URL, json=conflicting, headers=la_headers)
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"status": "REJECTED", "error_code": ENVELOPE_CONFLICT}

    counts_after = await _counts(db_session)
    assert counts_after == counts_before
    assert (await _sidecar_row(db_session, "span-xyz")).canonical_payload_digest == digest_before


async def test_foreign_project_collision_409_ownership_p1(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    """Historical P1 reproduction: project B reuses project A's IDs -> 409, A untouched."""
    payload = envelope_payload()
    assert (await client.post(URL, json=payload, headers=la_headers)).status_code == 201

    project_b = await _create_project(db_session, TEST_ORG_ID, "Project B")
    key_b = await _create_key(db_session, TEST_ORG_ID, "sk_pp_test_la_key_0000000000000002")
    headers_b = {
        "X-API-Key": key_b,
        "X-Project-ID": str(project_b.id),
        "Content-Type": "application/json",
    }

    for attempt in (
        dict(payload),
        dict(payload, span_id="span-other"),
        dict(payload, trace_id="trace-other"),
    ):
        resp = await client.post(URL, json=attempt, headers=headers_b)
        assert resp.status_code == 409, (resp.status_code, resp.text)
        assert resp.json()["error_code"] == EXTERNAL_ID_OWNERSHIP_CONFLICT

    # Verify in a FRESH DB session: A owner/digest/sidecar/legacy rows unchanged,
    # B has no residual compatibility row.
    fresh = async_session_factory()
    try:
        counts = await _counts(fresh)
        assert counts == {"sidecars": 1, "trace_bindings": 1, "span_bindings": 1, "traces": 1, "spans": 1}
        sidecar = await _sidecar_row(fresh, "span-xyz")
        assert sidecar.project_id == TEST_PROJECT_ID
        assert sidecar.canonical_payload_digest == canonical_payload_digest(
            LocalAgentTraceEnvelopeInV1.model_validate(payload)
        )
        trace_b = (
            await fresh.execute(
                select(LocalAgentExternalTraceIdentityModel).where(
                    LocalAgentExternalTraceIdentityModel.project_id == project_b.id
                )
            )
        ).all()
        span_b = (
            await fresh.execute(
                select(LocalAgentExternalSpanIdentityModel).where(
                    LocalAgentExternalSpanIdentityModel.project_id == project_b.id
                )
            )
        ).all()
        sidecar_b = (
            await fresh.execute(
                select(LocalAgentTraceEnvelopeSidecarModel).where(
                    LocalAgentTraceEnvelopeSidecarModel.project_id == project_b.id
                )
            )
        ).all()
        assert trace_b == [] and span_b == [] and sidecar_b == []
    finally:
        await fresh.close()


async def test_trace_run_id_conflict_409(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    payload = envelope_payload()
    assert (await client.post(URL, json=payload, headers=la_headers)).status_code == 201

    # Same trace, different run -> 409 LOCALAGENT_TRACE_IDENTITY_CONFLICT.
    conflict = dict(payload, span_id="span-other", run_id="run-other")
    resp = await client.post(URL, json=conflict, headers=la_headers)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == TRACE_IDENTITY_CONFLICT

    counts = await _counts(db_session)
    assert counts["sidecars"] == 1 and counts["span_bindings"] == 1


# ---------------------------------------------------------------------------
# Ordering (completion order accepted)
# ---------------------------------------------------------------------------


async def test_child_first_supported_and_late_parent_resolution(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    child = envelope_payload(
        span_id="span-child",
        trace_id="trace-abc",
        parent_span_id="span-parent",
        step_id="step-2",
    )
    resp = await client.post(URL, json=child, headers=la_headers)
    assert resp.status_code == 201, resp.text

    child_sidecar = await _sidecar_row(db_session, "span-child")
    assert child_sidecar.external_parent_span_id == "span-parent"
    child_span_row = (
        await db_session.execute(select(SpanModel).where(SpanModel.span_id == child_sidecar.internal_span_uuid))
    ).scalar_one()
    assert child_span_row.parent_span_id is None  # unresolved parent not fabricated

    parent = envelope_payload(span_id="span-parent", trace_id="trace-abc", step_id="step-1")
    assert (await client.post(URL, json=parent, headers=la_headers)).status_code == 201

    # Later envelope whose parent now resolves to the same project/trace.
    sibling = envelope_payload(
        span_id="span-sibling",
        trace_id="trace-abc",
        parent_span_id="span-parent",
        step_id="step-3",
    )
    assert (await client.post(URL, json=sibling, headers=la_headers)).status_code == 201
    sibling_sidecar = await _sidecar_row(db_session, "span-sibling")
    parent_sidecar = await _sidecar_row(db_session, "span-parent")
    sibling_span_row = (
        await db_session.execute(select(SpanModel).where(SpanModel.span_id == sibling_sidecar.internal_span_uuid))
    ).scalar_one()
    assert sibling_span_row.parent_span_id == parent_sidecar.internal_span_uuid


async def test_parent_same_project_resolution(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    parent = envelope_payload(span_id="span-parent", trace_id="trace-abc", step_id="step-1")
    assert (await client.post(URL, json=parent, headers=la_headers)).status_code == 201
    child = envelope_payload(
        span_id="span-child",
        trace_id="trace-abc",
        parent_span_id="span-parent",
        step_id="step-2",
    )
    assert (await client.post(URL, json=child, headers=la_headers)).status_code == 201

    child_sidecar = await _sidecar_row(db_session, "span-child")
    parent_sidecar = await _sidecar_row(db_session, "span-parent")
    child_span_row = (
        await db_session.execute(select(SpanModel).where(SpanModel.span_id == child_sidecar.internal_span_uuid))
    ).scalar_one()
    assert child_span_row.parent_span_id == parent_sidecar.internal_span_uuid


async def test_parent_foreign_project_conflict_409(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    parent = envelope_payload(span_id="span-parent", trace_id="trace-abc", step_id="step-1")
    assert (await client.post(URL, json=parent, headers=la_headers)).status_code == 201

    project_b = await _create_project(db_session, TEST_ORG_ID, "Project B Parent")
    key_b = await _create_key(db_session, TEST_ORG_ID, "sk_pp_test_la_key_0000000000000003")
    headers_b = {"X-API-Key": key_b, "X-Project-ID": str(project_b.id), "Content-Type": "application/json"}
    child = envelope_payload(
        span_id="span-child-b",
        trace_id="trace-b",
        parent_span_id="span-parent",
        step_id="step-2",
    )
    resp = await client.post(URL, json=child, headers=headers_b)
    assert resp.status_code == 409
    assert resp.json()["error_code"] == EXTERNAL_ID_OWNERSHIP_CONFLICT


async def test_parent_different_trace_conflict_409(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    parent = envelope_payload(span_id="span-parent", trace_id="trace-abc", step_id="step-1")
    assert (await client.post(URL, json=parent, headers=la_headers)).status_code == 201
    child = envelope_payload(
        span_id="span-child",
        trace_id="trace-different",
        parent_span_id="span-parent",
        step_id="step-2",
    )
    resp = await client.post(URL, json=child, headers=la_headers)
    assert resp.status_code == 409
    assert resp.json()["error_code"] == ENVELOPE_CONFLICT


# ---------------------------------------------------------------------------
# Transaction / ack semantics
# ---------------------------------------------------------------------------


async def test_commit_failure_never_returns_2xx(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    with patch.object(
        db_session,
        "commit",
        AsyncMock(side_effect=OperationalError("SELECT 1", {}, RuntimeError("connection refused"))),
    ):
        resp = await client.post(URL, json=envelope_payload(), headers=la_headers)
    assert resp.status_code == 503, resp.text
    assert resp.json() == {"status": "REJECTED", "error_code": PERSISTENCE_UNAVAILABLE}
    assert await _counts(db_session) == {
        "sidecars": 0,
        "trace_bindings": 0,
        "span_bindings": 0,
        "traces": 0,
        "spans": 0,
    }


# ---------------------------------------------------------------------------
# Fail-closed validation / bounded errors
# ---------------------------------------------------------------------------


async def test_unknown_field_rejected_422(client: AsyncClient, la_headers: dict[str, str]):
    resp = await client.post(URL, json=dict(envelope_payload(), unknown_field="boom"), headers=la_headers)
    assert resp.status_code == 422
    assert resp.json() == {"status": "REJECTED", "error_code": ENVELOPE_INVALID}


async def test_legacy_field_rejected_422(client: AsyncClient, la_headers: dict[str, str]):
    resp = await client.post(URL, json=dict(envelope_payload(), metadata={"x": 1}), headers=la_headers)
    assert resp.status_code == 422
    assert resp.json()["error_code"] == ENVELOPE_INVALID


async def test_malformed_json_rejected_422(client: AsyncClient, la_headers: dict[str, str]):
    resp = await client.post(URL, content=b"{not json", headers=la_headers)
    assert resp.status_code == 422
    assert resp.json()["error_code"] == ENVELOPE_INVALID


async def test_contract_identity_unsupported_422(client: AsyncClient, la_headers: dict[str, str]):
    resp = await client.post(
        URL, json=dict(envelope_payload(), contract_identity="other.contract"), headers=la_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == CONTRACT_IDENTITY_UNSUPPORTED


async def test_contract_version_unsupported_422(client: AsyncClient, la_headers: dict[str, str]):
    resp = await client.post(URL, json=dict(envelope_payload(), contract_version=2), headers=la_headers)
    assert resp.status_code == 422
    assert resp.json()["error_code"] == CONTRACT_VERSION_UNSUPPORTED


async def test_contract_fingerprint_unsupported_422(client: AsyncClient, la_headers: dict[str, str]):
    resp = await client.post(
        URL, json=dict(envelope_payload(), contract_fingerprint="a" * 64), headers=la_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == CONTRACT_FINGERPRINT_UNSUPPORTED


async def test_semantic_invalid_422(client: AsyncClient, la_headers: dict[str, str]):
    resp = await client.post(
        URL,
        json=dict(envelope_payload(), completed_at="2026-01-01T00:00:00Z", duration_ms=-5),
        headers=la_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == ENVELOPE_INVALID


async def test_payload_too_large_413(client: AsyncClient, la_headers: dict[str, str]):
    huge = {"padding": "x" * 20000}
    resp = await client.post(URL, json=huge, headers=la_headers)
    assert resp.status_code == 413
    assert resp.json() == {"status": "REJECTED", "error_code": ENVELOPE_TOO_LARGE}


async def test_rate_limit_429(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str], monkeypatch
):
    import app.api.v1.routes.localagent_integrations as route_module

    monkeypatch.setattr(route_module, "LOCALAGENT_TRACE_ENVELOPE_RATE_LIMIT_PER_MINUTE", 3)
    for index in range(3):
        resp = await client.post(
            URL, json=dict(envelope_payload(), span_id=f"span-{index + 1}"), headers=la_headers
        )
        assert resp.status_code == 201, resp.text
    resp = await client.post(URL, json=envelope_payload(span_id="span-over"), headers=la_headers)
    assert resp.status_code == 429
    assert resp.json() == {"status": "REJECTED", "error_code": INGESTION_RATE_LIMITED}


async def test_error_response_never_echoes_payload(client: AsyncClient, la_headers: dict[str, str]):
    resp = await client.post(URL, json=dict(envelope_payload(), unknown_field="secret-value"), headers=la_headers)
    body = resp.text
    assert "secret-value" not in body
    assert "span-xyz" not in body
    assert resp.json() == {"status": "REJECTED", "error_code": ENVELOPE_INVALID}


# ---------------------------------------------------------------------------
# Concurrency (real PostgreSQL races)
# ---------------------------------------------------------------------------


async def _fresh_session_override():
    """Per-request session override for true concurrent transactions."""

    async def _dependency():
        session = async_session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    return _dependency


def _app_overrides():
    """Access the FastAPI app dependency override dict."""
    from app.main import app

    return app.dependency_overrides


async def test_concurrent_exact_replay_converges(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    app_deps = _app_overrides()
    app_deps[get_db_session] = await _fresh_session_override()
    try:
        payload = envelope_payload()
        responses = await asyncio.gather(
            client.post(URL, json=payload, headers=la_headers),
            client.post(URL, json=payload, headers=la_headers),
        )
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [200, 201], [(r.status_code, r.text) for r in responses]
    finally:
        app_deps.pop(get_db_session, None)

    counts = await _counts(db_session)
    assert counts == {"sidecars": 1, "trace_bindings": 1, "span_bindings": 1, "traces": 1, "spans": 1}


async def test_concurrent_conflicting_replay_no_merge(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    app_deps = _app_overrides()
    app_deps[get_db_session] = await _fresh_session_override()
    try:
        payload = envelope_payload()
        conflicting = dict(payload, duration_ms=9999)
        responses = await asyncio.gather(
            client.post(URL, json=payload, headers=la_headers),
            client.post(URL, json=conflicting, headers=la_headers),
        )
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [201, 409], [(r.status_code, r.text) for r in responses]
        error_code = next(r.json()["error_code"] for r in responses if r.status_code == 409)
        assert error_code == ENVELOPE_CONFLICT
    finally:
        app_deps.pop(get_db_session, None)

    counts = await _counts(db_session)
    assert counts == {"sidecars": 1, "trace_bindings": 1, "span_bindings": 1, "traces": 1, "spans": 1}


async def test_concurrent_foreign_owner_collision(
    client: AsyncClient, db_session: AsyncSession, la_headers: dict[str, str]
):
    payload = envelope_payload()
    assert (await client.post(URL, json=payload, headers=la_headers)).status_code == 201

    project_b = await _create_project(db_session, TEST_ORG_ID, "Project B Race")
    key_b = await _create_key(db_session, TEST_ORG_ID, "sk_pp_test_la_key_0000000000000004")
    headers_b = {"X-API-Key": key_b, "X-Project-ID": str(project_b.id), "Content-Type": "application/json"}

    app_deps = _app_overrides()
    app_deps[get_db_session] = await _fresh_session_override()
    try:
        responses = await asyncio.gather(
            client.post(URL, json=payload, headers=la_headers),
            client.post(URL, json=payload, headers=headers_b),
        )
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [200, 409], [(r.status_code, r.text) for r in responses]
        error_code = next(r.json()["error_code"] for r in responses if r.status_code == 409)
        assert error_code == EXTERNAL_ID_OWNERSHIP_CONFLICT
    finally:
        app_deps.pop(get_db_session, None)

    fresh = async_session_factory()
    try:
        counts = await _counts(fresh)
        assert counts == {"sidecars": 1, "trace_bindings": 1, "span_bindings": 1, "traces": 1, "spans": 1}
    finally:
        await fresh.close()


async def test_auth_failure_responses_bounded(client: AsyncClient):
    resp = await client.post(
        URL,
        json=envelope_payload(),
        headers={"X-Project-ID": str(TEST_PROJECT_ID), "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"status": "REJECTED", "error_code": AUTHENTICATION_FAILED}
