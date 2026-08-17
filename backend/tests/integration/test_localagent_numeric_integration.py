"""R2 numeric-contract PostgreSQL integration tests.

Real PostgreSQL + Redis + full middleware stack.  Proves the frozen numeric
pipeline end-to-end: raw producer wire token -> HTTP -> token-aware decoder ->
exact semantic Decimal -> lossless NUMERIC sidecar -> completely fresh session
readback equal to the frozen semantic value (never via the digest).

Also covers exact/conflicting duplicate semantics, MAX+1 and huge-token
bounded rejection, duplicate JSON keys, and direct NUMERIC special-value
rejection.
"""

# ruff: noqa: D415

import json
import sys
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.localagent.contract import MAX_V1_DURATION_INT
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
    VERIFIED_PRODUCER_SEMANTIC_VALUES,
    VERIFIED_PRODUCER_WIRE_FIXTURE,
)

pytestmark = pytest.mark.asyncio

FINGERPRINT = "6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab"
URL = "/integrations/localagent/v1/trace-envelopes"


def raw_body(duration_token: str, *, span_id: str, attributes: dict[str, object] | None = None) -> bytes:
    """Raw envelope bytes with the given UNQUOTED duration token on the wire."""
    payload: dict[str, object] = {
        "contract_identity": "localagent.runtime.trace_export",
        "contract_version": 1,
        "contract_fingerprint": FINGERPRINT,
        "run_id": "run-num",
        "trace_id": "trace-num",
        "span_id": span_id,
        "parent_span_id": None,
        "step_id": "step-1",
        "operation": "runtime.step",
        "component": "planner",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:05Z",
        "duration_ms": 5000,
        "status": "OK",
        "error_code": None,
        "attributes": attributes or {"execution_kind": "AGENT"},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    encoded = encoded.replace('"duration_ms":5000', f'"duration_ms":{duration_token}')
    assert f'"duration_ms":{duration_token}' in encoded
    return encoded.encode("utf-8")


@pytest.fixture
async def la_key(db_session: AsyncSession) -> str:
    """Persisted active API key for the seeded test org."""
    await IdentityRepository(db_session).create_api_key(
        org_id=TEST_ORG_ID,
        key_hash=hash_api_key("sk_pp_test_la_key_0000000000000099"),
        key_prefix="sk_pp_test_",
        name="test-localagent-numeric",
    )
    await db_session.commit()
    return "sk_pp_test_la_key_0000000000000099"


@pytest.fixture
def la_headers(la_key: str) -> dict[str, str]:
    return {
        "X-API-Key": la_key,
        "X-Project-ID": str(TEST_PROJECT_ID),
        "Content-Type": "application/json",
    }


async def _sidecar_duration(session: AsyncSession, span_id: str) -> Decimal | None:
    return (
        await session.execute(
            select(LocalAgentTraceEnvelopeSidecarModel.duration_ms).where(
                LocalAgentTraceEnvelopeSidecarModel.external_span_id == span_id
            )
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Fresh-session numeric truth matrix (P1-06) — no digest lookups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "zero",
        "negative_zero",
        "one",
        "one_float",
        "one_point_five",
        "two_53",
        "two_53_plus_1",
        "ten_100_plus_1",
        "ten_308_plus_1",
        "max_int",
        "float_max",
        "min_subnormal",
    ],
)
async def test_fresh_session_numeric_truth(client: AsyncClient, db_session: AsyncSession, la_headers, name: str):
    token = VERIFIED_PRODUCER_WIRE_FIXTURE[name]
    expected = VERIFIED_PRODUCER_SEMANTIC_VALUES[name]
    span_id = f"num-{name}"
    resp = await client.post(URL, content=raw_body(token, span_id=span_id), headers=la_headers)
    assert resp.status_code == 201, (name, token, resp.text)

    fresh = async_session_factory()
    try:
        persisted = await _sidecar_duration(fresh, span_id)
        assert persisted is not None
        assert persisted == expected, (name, token, persisted, expected)
    finally:
        await fresh.close()


async def test_max_integer_persistence_exact(client: AsyncClient, db_session: AsyncSession, la_headers):
    """309-digit MAX_V1_DURATION_INT: 201 then exact NUMERIC equality."""
    token = VERIFIED_PRODUCER_WIRE_FIXTURE["max_int"]
    resp = await client.post(URL, content=raw_body(token, span_id="num-max"), headers=la_headers)
    assert resp.status_code == 201, resp.text

    fresh = async_session_factory()
    try:
        persisted = await _sidecar_duration(fresh, "num-max")
        assert persisted == Decimal(MAX_V1_DURATION_INT)
        assert persisted == VERIFIED_PRODUCER_SEMANTIC_VALUES["max_int"]
        assert len(str(persisted)) == 309
    finally:
        await fresh.close()


async def test_minimum_subnormal_persistence_exact(client: AsyncClient, db_session: AsyncSession, la_headers):
    """5e-324 (minimum positive subnormal): exact NUMERIC equality (scale 1074)."""
    resp = await client.post(
        URL,
        content=raw_body(VERIFIED_PRODUCER_WIRE_FIXTURE["min_subnormal"], span_id="num-subnormal"),
        headers=la_headers,
    )
    assert resp.status_code == 201, resp.text

    fresh = async_session_factory()
    try:
        persisted = await _sidecar_duration(fresh, "num-subnormal")
        assert persisted == VERIFIED_PRODUCER_SEMANTIC_VALUES["min_subnormal"]
        assert persisted == Decimal.from_float(float.fromhex("0x0.0000000000001p-1022"))
    finally:
        await fresh.close()


async def test_float_max_persistence_exact(client: AsyncClient, db_session: AsyncSession, la_headers):
    resp = await client.post(
        URL,
        content=raw_body(VERIFIED_PRODUCER_WIRE_FIXTURE["float_max"], span_id="num-floatmax"),
        headers=la_headers,
    )
    assert resp.status_code == 201, resp.text

    fresh = async_session_factory()
    try:
        persisted = await _sidecar_duration(fresh, "num-floatmax")
        assert persisted == VERIFIED_PRODUCER_SEMANTIC_VALUES["float_max"]
        assert persisted == Decimal.from_float(sys.float_info.max)
    finally:
        await fresh.close()


# ---------------------------------------------------------------------------
# Duplicate semantics (P1-04)
# ---------------------------------------------------------------------------


async def test_exact_duplicate_semantic_equivalents(client: AsyncClient, db_session: AsyncSession, la_headers):
    """1 then 1.0, and 0 then -0.0 -> 201 then 200 DUPLICATE_ACCEPTED."""
    assert (
        await client.post(URL, content=raw_body("1", span_id="dup-int"), headers=la_headers)
    ).status_code == 201
    replay = await client.post(URL, content=raw_body("1.0", span_id="dup-int"), headers=la_headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "DUPLICATE_ACCEPTED"

    assert (
        await client.post(URL, content=raw_body("0", span_id="dup-zero"), headers=la_headers)
    ).status_code == 201
    replay2 = await client.post(URL, content=raw_body("-0.0", span_id="dup-zero"), headers=la_headers)
    assert replay2.status_code == 200, replay2.text
    assert replay2.json()["status"] == "DUPLICATE_ACCEPTED"


async def test_conflicting_duplicate_semantic_difference(client: AsyncClient, db_session: AsyncSession, la_headers):
    """Same span: 2**53 (201) then 2**53+1 (409); zero mutation."""
    assert (
        await client.post(
            URL, content=raw_body(VERIFIED_PRODUCER_WIRE_FIXTURE["two_53"], span_id="conf-53"), headers=la_headers
        )
    ).status_code == 201
    counts_before = await _counts(db_session)
    conflict = await client.post(
        URL,
        content=raw_body(VERIFIED_PRODUCER_WIRE_FIXTURE["two_53_plus_1"], span_id="conf-53"),
        headers=la_headers,
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error_code"] == "LOCALAGENT_ENVELOPE_CONFLICT"
    assert await _counts(db_session) == counts_before
    # exact replay of the first still converges.
    replay = await client.post(
        URL, content=raw_body(VERIFIED_PRODUCER_WIRE_FIXTURE["two_53"], span_id="conf-53"), headers=la_headers
    )
    assert replay.status_code == 200


# ---------------------------------------------------------------------------
# Bounded rejection (P1-03)
# ---------------------------------------------------------------------------


async def test_max_plus_one_rejected_422_no_mutation(client: AsyncClient, db_session: AsyncSession, la_headers):
    resp = await client.post(
        URL,
        content=raw_body(str(MAX_V1_DURATION_INT + 1), span_id="rej-max1"),
        headers=la_headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"status": "REJECTED", "error_code": "LOCALAGENT_ENVELOPE_INVALID"}
    assert await _counts(db_session) == {
        "sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0,
    }


@pytest.mark.parametrize(
    "token",
    [str(10**309), str(10**1000), "1e1000000", "1e1000000000"],
)
async def test_huge_numeric_tokens_bounded_422(client: AsyncClient, db_session: AsyncSession, la_headers, token: str):
    resp = await client.post(URL, content=raw_body(token, span_id=f"huge-{len(token)}"), headers=la_headers)
    # Must be a bounded 422, never 500 / OverflowError / MemoryError leakage.
    assert resp.status_code == 422, (token, resp.status_code, resp.text)
    assert "OverflowError" not in resp.text
    assert "MemoryError" not in resp.text
    assert "ValueError" not in resp.text
    assert resp.json() == {"status": "REJECTED", "error_code": "LOCALAGENT_ENVELOPE_INVALID"}


async def test_duplicate_json_keys_422(client: AsyncClient, db_session: AsyncSession, la_headers):
    body = json.dumps(
        {
            "contract_identity": "localagent.runtime.trace_export",
            "contract_version": 1,
            "contract_fingerprint": FINGERPRINT,
            "run_id": "run-dup",
            "trace_id": "trace-dup",
            "span_id": "span-dup",
            "operation": "runtime.step",
            "component": "planner",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:05Z",
            "duration_ms": 100,
            "status": "OK",
            "error_code": None,
            "attributes": {"execution_kind": "AGENT"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).replace('"duration_ms":100', '"duration_ms":100,"duration_ms":200')
    resp = await client.post(URL, content=body.encode("utf-8"), headers=la_headers)
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"status": "REJECTED", "error_code": "LOCALAGENT_ENVELOPE_INVALID"}
    assert await _counts(db_session) == {
        "sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0,
    }


# ---------------------------------------------------------------------------
# Direct PostgreSQL NUMERIC behavior (defense-in-depth)
# ---------------------------------------------------------------------------


async def test_numeric_column_rejects_special_and_negative_values(db_session: AsyncSession):
    """NaN/±Infinity are not representable in NUMERIC; negative violates CHECK."""
    project_id = str(TEST_PROJECT_ID)
    trace_id = str(TEST_PROJECT_ID)
    stmt = text(
        "INSERT INTO localagent_external_trace_identity "
        "(external_trace_id, project_id, internal_trace_uuid, run_id) "
        "VALUES (:trace, :project, :internal, 'run-1')"
    )
    await db_session.execute(stmt, {"trace": trace_id, "project": project_id, "internal": str(TEST_PROJECT_ID)})
    await db_session.execute(
        text(
            "INSERT INTO localagent_external_span_identity "
            "(external_span_id, project_id, internal_span_uuid, external_trace_id) "
            "VALUES ('sp-probe', :project, :internal, :trace)"
        ),
        {"project": project_id, "internal": str(TEST_PROJECT_ID), "trace": trace_id},
    )
    # Persist the identity rows so the rollbacks below never lose them.
    await db_session.commit()
    base = {
        "envelope": str(TEST_PROJECT_ID),
        "project": project_id,
        "trace": trace_id,
        "internal": str(TEST_PROJECT_ID),
    }

    async def insert_with(duration_literal: str) -> None:
        await db_session.execute(
            text(
                "INSERT INTO localagent_trace_envelope_sidecars "
                "(envelope_id, project_id, external_run_id, external_trace_id, external_span_id, "
                "operation, component, started_at, completed_at, duration_ms, status, attributes, "
                "contract_identity, contract_version, contract_fingerprint, canonical_payload_digest, "
                "internal_trace_uuid, internal_span_uuid) "
                "VALUES (:envelope, :project, 'run-1', :trace, 'sp-probe', 'runtime.step', 'comp', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, " + duration_literal + ", 'OK', '{}'::jsonb, "
                "'localagent.runtime.trace_export', 1, :fp, :digest, :internal, :internal)"
            ),
            {**base, "fp": FINGERPRINT, "digest": "a" * 64},
        )

    for special in ("'NaN'::numeric", "'Infinity'::numeric", "'-Infinity'::numeric"):
        with pytest.raises(DBAPIError):
            await insert_with(special)
        await db_session.rollback()
    with pytest.raises(DBAPIError):  # negative violates the CHECK
        await insert_with("-1")
    await db_session.rollback()

    await insert_with("0")  # valid zero accepted
    await db_session.commit()
    count = (
        await db_session.execute(
            select(func.count()).select_from(LocalAgentTraceEnvelopeSidecarModel)
        )
    ).scalar_one()
    assert count == 1
