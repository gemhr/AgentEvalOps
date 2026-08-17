"""R3 large-integer wire parity integration tests.

Real PostgreSQL + Redis + full middleware stack.  Proves that producer-valid
``NON_NEGATIVE_INT`` attributes with >4300 decimal digits (4301/5000/10000
and a near-16384-byte payload) pass decode -> semantic validation -> digest ->
PostgreSQL JSONB -> completely fresh-session readback exactly, and that
contract-invalid huge duration tokens return a bounded 422 (never 500).
"""

# ruff: noqa: D415

import json
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.models import (
    LocalAgentExternalSpanIdentityModel,
    LocalAgentExternalTraceIdentityModel,
    LocalAgentTraceEnvelopeSidecarModel,
    SpanModel,
    TraceModel,
)
from app.infrastructure.db.repositories.identity_repo import IdentityRepository
from app.registry.security import hash_api_key

from .conftest import TEST_ORG_ID, TEST_PROJECT_ID
from .test_localagent_trace_envelopes import _counts
from ..unit.localagent_producer_wire_fixtures import (
    VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE,
    VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES,
)

pytestmark = pytest.mark.asyncio

FINGERPRINT = "6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab"
URL = "/integrations/localagent/v1/trace-envelopes"


def raw_envelope(attribute_token: str, *, span_id: str, duration_token: str = "5000") -> bytes:
    """Raw envelope bytes with the given UNQUOTED plan_version token."""
    payload: dict[str, object] = {
        "contract_identity": "localagent.runtime.trace_export",
        "contract_version": 1,
        "contract_fingerprint": FINGERPRINT,
        "run_id": "run-large",
        "trace_id": "trace-large",
        "span_id": span_id,
        "parent_span_id": None,
        "step_id": None,
        "operation": "runtime.run",
        "component": "planner",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:05Z",
        "duration_ms": 5000,
        "status": "OK",
        "error_code": None,
        "attributes": {"plan_version": 1},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    encoded = encoded.replace('"plan_version":1', f'"plan_version":{attribute_token}')
    encoded = encoded.replace('"duration_ms":5000', f'"duration_ms":{duration_token}')
    assert f'"plan_version":{attribute_token}' in encoded
    assert f'"duration_ms":{duration_token}' in encoded
    return encoded.encode("utf-8")


@pytest.fixture
async def la_key(db_session: AsyncSession) -> str:
    """Persisted active API key for the seeded test org."""
    await IdentityRepository(db_session).create_api_key(
        org_id=TEST_ORG_ID,
        key_hash=hash_api_key("sk_pp_test_la_key_0000000000000097"),
        key_prefix="sk_pp_test_",
        name="test-localagent-large-int",
    )
    await db_session.commit()
    return "sk_pp_test_la_key_0000000000000097"


@pytest.fixture
def la_headers(la_key: str) -> dict[str, str]:
    return {
        "X-API-Key": la_key,
        "X-Project-ID": str(TEST_PROJECT_ID),
        "Content-Type": "application/json",
    }


async def _sidecar_attributes(session: AsyncSession, span_id: str) -> dict[str, object] | None:
    return (
        await session.execute(
            select(LocalAgentTraceEnvelopeSidecarModel.attributes).where(
                LocalAgentTraceEnvelopeSidecarModel.external_span_id == span_id
            )
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Producer-valid large attribute: 201 + exact JSONB fresh-session readback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["attr_4301", "attr_5000", "attr_10000"])
async def test_large_attribute_201_and_exact_jsonb_readback(
    client: AsyncClient, db_session: AsyncSession, la_headers, name: str
):
    token = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE[name]
    value = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES[name]
    span_id = f"large-{name}"
    resp = await client.post(URL, content=raw_envelope(token, span_id=span_id), headers=la_headers)
    assert resp.status_code == 201, (name, resp.text)
    assert resp.json() == {"status": "PERSISTED", "error_code": None}

    fresh = async_session_factory()
    try:
        attributes = await _sidecar_attributes(fresh, span_id)
        assert attributes is not None
        assert attributes["plan_version"] == value  # exact integer, no loss/stringify
    finally:
        await fresh.close()


async def test_near_max_payload_attribute_201_and_exact_readback(
    client: AsyncClient, db_session: AsyncSession, la_headers
):
    """~15901-digit attribute: the near-max producer payload (16376 bytes)."""
    token = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE["attr_15901_near_max"]
    value = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES["attr_15901_near_max"]
    body = raw_envelope(token, span_id="large-nearmax")
    assert len(body) <= 16384, len(body)
    resp = await client.post(URL, content=body, headers=la_headers)
    assert resp.status_code == 201, resp.text

    fresh = async_session_factory()
    try:
        attributes = await _sidecar_attributes(fresh, "large-nearmax")
        assert attributes["plan_version"] == value
    finally:
        await fresh.close()


# ---------------------------------------------------------------------------
# Large-integer duplicate semantics
# ---------------------------------------------------------------------------


async def test_large_attribute_exact_replay_200(client: AsyncClient, db_session: AsyncSession, la_headers):
    token = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE["attr_5000"]
    value = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES["attr_5000"]
    first = await client.post(URL, content=raw_envelope(token, span_id="large-dup"), headers=la_headers)
    assert first.status_code == 201, first.text

    counts_before = await _counts(db_session)
    replay = await client.post(URL, content=raw_envelope(token, span_id="large-dup"), headers=la_headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "DUPLICATE_ACCEPTED"
    assert await _counts(db_session) == counts_before

    fresh = async_session_factory()
    try:
        attributes = await _sidecar_attributes(fresh, "large-dup")
        assert attributes["plan_version"] == value
    finally:
        await fresh.close()


async def test_large_attribute_n_plus_1_conflict_409(client: AsyncClient, db_session: AsyncSession, la_headers):
    token = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE["attr_5000"]
    value = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES["attr_5000"]
    assert (
        await client.post(URL, content=raw_envelope(token, span_id="large-conf"), headers=la_headers)
    ).status_code == 201

    counts_before = await _counts(db_session)
    conflict = await client.post(
        URL,
        content=raw_envelope(exact_token(value + 1), span_id="large-conf"),
        headers=la_headers,
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error_code"] == "LOCALAGENT_ENVELOPE_CONFLICT"
    assert await _counts(db_session) == counts_before

    fresh = async_session_factory()
    try:
        attributes = await _sidecar_attributes(fresh, "large-conf")
        assert attributes["plan_version"] == value  # original truth unchanged
    finally:
        await fresh.close()


def exact_token(value: int) -> str:
    """Exact decimal token for a huge int (no Python int->str digit limit)."""
    from app.core.localagent.decoder import exact_json_dumps

    return exact_json_dumps(value)


# ---------------------------------------------------------------------------
# Invalid huge duration -> bounded 422 (parser failure boundary)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("digits", [4301, 5000, 10000])
async def test_invalid_huge_duration_bounded_422(
    client: AsyncClient, db_session: AsyncSession, la_headers, digits: int
):
    """A >4300-digit duration is contract-invalid: bounded 422, never 500."""
    resp = await client.post(
        URL,
        content=raw_envelope(VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE["attr_4301"], span_id=f"dur-{digits}", duration_token="9" * digits),
        headers=la_headers,
    )
    assert resp.status_code == 422, (digits, resp.status_code, resp.text)
    assert resp.json() == {"status": "REJECTED", "error_code": "LOCALAGENT_ENVELOPE_INVALID"}
    assert "ValueError" not in resp.text
    assert "OverflowError" not in resp.text
    assert await _counts(db_session) == {
        "sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0,
    }


async def test_near_body_limit_invalid_duration_422(client: AsyncClient, db_session: AsyncSession, la_headers):
    """A near-body-limit token invalid for duration must be a bounded 422."""
    body = raw_envelope("1", span_id="dur-nearmax", duration_token="9" * 15901)
    assert len(body) <= 16384, len(body)
    resp = await client.post(URL, content=body, headers=la_headers)
    assert resp.status_code == 422, (resp.status_code, resp.text)
    assert resp.json() == {"status": "REJECTED", "error_code": "LOCALAGENT_ENVELOPE_INVALID"}
    assert await _counts(db_session) == {
        "sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0,
    }
