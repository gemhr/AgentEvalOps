"""R4 column-local JSONB real PostgreSQL integration tests.

Real PostgreSQL + Redis + full middleware stack.  Proves that the shared
engine has pre-R3 DEFAULT JSON/JSONB semantics while ONLY the LocalAgent
sidecar ``attributes`` column (``LocalAgentAttributesJSONB``) preserves
producer-valid >4300-digit NON_NEGATIVE_INT attributes:

- physical column stays ``jsonb`` and the huge integer is stored as an
  UNQUOTED JSON number (never a JSON string);
- the driver JSONB decoder is bypassed (CAST(attributes AS TEXT)) so
  fresh-session mapped reads (select column / session.get / select Model)
  return the exact Python int;
- exact replay -> 200, N+1 conflict -> 409, fresh-session truth unchanged;
- generic (unrelated) JSONB columns keep pre-R3 behavior: stdlib key
  conversion (bool/None/numeric keys), mixed keys accepted, tuple keys and
  Decimal rejected at the serializer stage (StatementError), NaN/Infinity
  rejected by PostgreSQL (DBAPIError), ordinary float semantics, and
  >4300-digit ints REJECTED at bind (scope isolation);
- representative legacy and Phase2 JSONB repository paths round-trip
  ordinary types/values unchanged.
"""

# ruff: noqa: D415

import json
import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.localagent.decoder import exact_json_dumps
from app.core.traces.entities import Span, Trace
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.models import (
    EvaluationRunModel,
    LocalAgentTraceEnvelopeSidecarModel,
    ProjectModel,
    SpanModel,
    TraceModel,
)
from app.infrastructure.db.repositories.identity_repo import IdentityRepository
from app.infrastructure.db.repositories.trace_repo import TraceRepository
from app.registry.constants import SpanKind, SpanStatusCode, TraceStatus
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


def raw_envelope(attribute_token: str, *, span_id: str) -> bytes:
    """Raw envelope bytes with the given UNQUOTED plan_version token."""
    payload: dict[str, object] = {
        "contract_identity": "localagent.runtime.trace_export",
        "contract_version": 1,
        "contract_fingerprint": FINGERPRINT,
        "run_id": "run-r4",
        "trace_id": "trace-r4",
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
    assert f'"plan_version":{attribute_token}' in encoded
    return encoded.encode("utf-8")


@pytest.fixture
async def la_key(db_session: AsyncSession) -> str:
    """Persisted active API key for the seeded test org."""
    await IdentityRepository(db_session).create_api_key(
        org_id=TEST_ORG_ID,
        key_hash=hash_api_key("sk_pp_test_la_key_0000000000000099"),
        key_prefix="sk_pp_test_",
        name="test-localagent-column-jsonb",
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _add_trace(db_session: AsyncSession, *, output: object) -> TraceModel:
    """Persist an ordinary legacy TraceModel row with a generic JSONB output."""
    model = TraceModel(
        project_id=TEST_PROJECT_ID,
        name="r4-generic",
        status="COMPLETED",
        started_at=_now(),
        ended_at=_now(),
        metadata_={},
        output=output,
    )
    db_session.add(model)
    await db_session.commit()
    return model


# ---------------------------------------------------------------------------
# Physical JSONB + unquoted huge numeric token (Bind Truth Probe)
# ---------------------------------------------------------------------------


async def test_physical_column_is_jsonb_and_token_unquoted(
    client: AsyncClient, db_session: AsyncSession, la_headers
):
    token = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE["attr_5000"]
    resp = await client.post(URL, content=raw_envelope(token, span_id="r4-physical"), headers=la_headers)
    assert resp.status_code == 201, resp.text

    raw_type, raw_text = (
        await db_session.execute(
            text(
                "SELECT pg_typeof(attributes), attributes::text "
                "FROM localagent_trace_envelope_sidecars WHERE external_span_id='r4-physical'"
            )
        )
    ).one()
    assert raw_type == "jsonb"
    # JSON object, NOT a JSON string wrapping.
    assert raw_text.startswith("{")
    # The huge integer is an UNQUOTED jsonb number (PostgreSQL jsonb::text uses
    # a colon-space separator after keys).
    assert f'"plan_version": {token}' in raw_text
    assert '"plan_version":"' not in raw_text


# ---------------------------------------------------------------------------
# Fresh-session huge integer readback matrix (4301/5000/10000/near-max)
# ---------------------------------------------------------------------------


async def _fresh_read_paths(span_id: str, value: int) -> None:
    """All ORM read paths against a completely fresh SQLAlchemy session."""
    fresh = async_session_factory()
    try:
        # select(Model.attributes) — the mapped-column select path
        attrs = (
            await fresh.execute(
                select(LocalAgentTraceEnvelopeSidecarModel.attributes).where(
                    LocalAgentTraceEnvelopeSidecarModel.external_span_id == span_id
                )
            )
        ).scalar_one()
        assert attrs["plan_version"] == value
        assert isinstance(attrs["plan_version"], int)

        # select(Model) full-row load
        row = (
            await fresh.execute(
                select(LocalAgentTraceEnvelopeSidecarModel).where(
                    LocalAgentTraceEnvelopeSidecarModel.external_span_id == span_id
                )
            )
        ).scalar_one()
        assert row.attributes["plan_version"] == value

        # session.get() by primary key
        obj = await fresh.get(LocalAgentTraceEnvelopeSidecarModel, row.envelope_id)
        assert obj is not None
        assert obj.attributes["plan_version"] == value
        assert isinstance(obj.attributes["plan_version"], int)
    finally:
        await fresh.close()


@pytest.mark.parametrize("name", ["attr_4301", "attr_5000", "attr_10000"])
async def test_large_attribute_fresh_session_read_paths_exact(
    client: AsyncClient, db_session: AsyncSession, la_headers, name: str
):
    token = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE[name]
    value = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES[name]
    resp = await client.post(URL, content=raw_envelope(token, span_id=f"r4-{name}"), headers=la_headers)
    assert resp.status_code == 201, (name, resp.text)
    await _fresh_read_paths(f"r4-{name}", value)


async def test_near_max_payload_fresh_session_exact(
    client: AsyncClient, db_session: AsyncSession, la_headers
):
    token = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE["attr_15901_near_max"]
    value = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES["attr_15901_near_max"]
    body = raw_envelope(token, span_id="r4-nearmax")
    assert len(body) <= 16384, len(body)
    resp = await client.post(URL, content=body, headers=la_headers)
    assert resp.status_code == 201, resp.text
    await _fresh_read_paths("r4-nearmax", value)


# ---------------------------------------------------------------------------
# Exact replay / N+1 conflict with fresh-session original truth
# ---------------------------------------------------------------------------


async def test_exact_replay_and_n_plus_1_conflict_fresh_truth(
    client: AsyncClient, db_session: AsyncSession, la_headers
):
    token = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE["attr_5000"]
    value = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES["attr_5000"]

    first = await client.post(URL, content=raw_envelope(token, span_id="r4-dup"), headers=la_headers)
    assert first.status_code == 201, first.text

    counts_before = await _counts(db_session)
    replay = await client.post(URL, content=raw_envelope(token, span_id="r4-dup"), headers=la_headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "DUPLICATE_ACCEPTED"
    assert await _counts(db_session) == counts_before

    conflict = await client.post(
        URL,
        content=raw_envelope(exact_json_dumps(value + 1), span_id="r4-dup"),
        headers=la_headers,
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error_code"] == "LOCALAGENT_ENVELOPE_CONFLICT"
    assert await _counts(db_session) == counts_before

    fresh = async_session_factory()
    try:
        attrs = (
            await fresh.execute(
                select(LocalAgentTraceEnvelopeSidecarModel.attributes).where(
                    LocalAgentTraceEnvelopeSidecarModel.external_span_id == "r4-dup"
                )
            )
        ).scalar_one()
        assert attrs["plan_version"] == value  # original truth unchanged
    finally:
        await fresh.close()


# ---------------------------------------------------------------------------
# Scope isolation: generic JSONB rejects huge ints (pre-R3 behavior)
# ---------------------------------------------------------------------------


async def test_generic_jsonb_huge_int_rejected_at_bind(db_session: AsyncSession):
    """Unrelated JSONB keeps the default 4300-digit behavior (R4 scope isolation)."""
    huge = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES["attr_5000"]
    with pytest.raises(StatementError):
        await _add_trace(db_session, output={"plan_version": huge})
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Pre-R3 shared-engine baseline matrix on an UNRELATED mapped JSONB column
# ---------------------------------------------------------------------------


async def test_generic_jsonb_ordinary_round_trip(db_session: AsyncSession):
    model = await _add_trace(
        db_session,
        output={
            "a": 1,
            "b": "x",
            "c": None,
            "d": True,
            "e": -5,
            "f": [1, "s", {"n": 2}],
            "g": {"nested": {"k": "v"}},
            "u": "héllo→世界",
            "esc": "quote\"back\\slash\nnewline",
        },
    )
    fresh = async_session_factory()
    try:
        row = (await fresh.execute(select(TraceModel).where(TraceModel.trace_id == model.trace_id))).scalar_one()
        assert row.output == model.output
        assert isinstance(row.output["a"], int)
        assert isinstance(row.output["d"], bool)
        assert isinstance(row.output["f"], list)
        assert isinstance(row.output["g"], dict)
        assert isinstance(row.output["c"], type(None))
    finally:
        await fresh.close()


async def test_generic_jsonb_bool_keys(db_session: AsyncSession):
    model = await _add_trace(db_session, output={True: "x", False: "y"})
    fresh = async_session_factory()
    try:
        row = (await fresh.execute(select(TraceModel).where(TraceModel.trace_id == model.trace_id))).scalar_one()
        assert row.output == {"true": "x", "false": "y"}
    finally:
        await fresh.close()


async def test_generic_jsonb_none_key(db_session: AsyncSession):
    model = await _add_trace(db_session, output={None: "x"})
    fresh = async_session_factory()
    try:
        row = (await fresh.execute(select(TraceModel).where(TraceModel.trace_id == model.trace_id))).scalar_one()
        assert row.output == {"null": "x"}
    finally:
        await fresh.close()


async def test_generic_jsonb_numeric_and_mixed_keys(db_session: AsyncSession):
    model = await _add_trace(db_session, output={1: "x", "a": "y"})
    fresh = async_session_factory()
    try:
        row = (await fresh.execute(select(TraceModel).where(TraceModel.trace_id == model.trace_id))).scalar_one()
        # stdlib key string conversion, no sorting TypeError
        assert row.output == {"1": "x", "a": "y"}
    finally:
        await fresh.close()


async def test_generic_jsonb_tuple_key_rejected(db_session: AsyncSession):
    with pytest.raises(StatementError) as excinfo:
        await _add_trace(db_session, output={(1, 2): "x"})
    assert isinstance(excinfo.value.orig, TypeError) or isinstance(excinfo.value.__cause__, TypeError)
    await db_session.rollback()


async def test_generic_jsonb_nan_rejected_by_postgresql(db_session: AsyncSession):
    with pytest.raises(DBAPIError):
        await _add_trace(db_session, output={"x": float("nan")})
    await db_session.rollback()


async def test_generic_jsonb_infinity_rejected_by_postgresql(db_session: AsyncSession):
    with pytest.raises(DBAPIError):
        await _add_trace(db_session, output={"x": float("inf")})
    await db_session.rollback()


async def test_generic_jsonb_negative_infinity_rejected_by_postgresql(db_session: AsyncSession):
    with pytest.raises(DBAPIError):
        await _add_trace(db_session, output={"x": float("-inf")})
    await db_session.rollback()


async def test_generic_jsonb_decimal_rejected(db_session: AsyncSession):
    with pytest.raises(StatementError) as excinfo:
        await _add_trace(db_session, output={"x": Decimal("1.5")})
    assert isinstance(excinfo.value.orig, TypeError) or isinstance(excinfo.value.__cause__, TypeError)
    await db_session.rollback()


async def test_generic_jsonb_ordinary_float_readback(db_session: AsyncSession):
    model = await _add_trace(db_session, output={"a": 0.1, "b": 1.5, "c": -0.0})
    fresh = async_session_factory()
    try:
        row = (await fresh.execute(select(TraceModel).where(TraceModel.trace_id == model.trace_id))).scalar_one()
        # Existing Python float semantics; no Decimal conversion.
        for key in ("a", "b", "c"):
            assert isinstance(row.output[key], float)
        assert row.output["a"] == 0.1
        assert row.output["b"] == 1.5
        # PostgreSQL jsonb normalizes -0.0 to 0.0 (pre-R3 behavior).
        assert row.output["c"] == 0.0
        assert math.copysign(1.0, row.output["c"]) == 1.0
    finally:
        await fresh.close()


# ---------------------------------------------------------------------------
# Legacy JSONB dynamic probe (representative repository path)
# ---------------------------------------------------------------------------


async def test_legacy_trace_repository_jsonb_round_trip(db_session: AsyncSession):
    """Write ordinary JSONB values through the real TraceRepository path."""
    now = _now()
    trace = Trace(
        trace_id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        name="r4-legacy-jsonb",
        status=TraceStatus.COMPLETED,
        input={"in": [1, 2, {"k": "v"}]},
        output={"ok": True, "n": 7, "f": 1.5, "s": "text", "none": None, "arr": [1, "b", False]},
        metadata={"m": {"deep": [0.1, 2]}},
        started_at=now,
        ended_at=now,
        session_id=None,
        user_id=None,
        tags=[],
        environment=None,
        release=None,
        spans=[],
    )
    persisted = await TraceRepository(db_session).upsert_trace(trace)
    await db_session.commit()

    fresh = async_session_factory()
    try:
        row = (await fresh.execute(select(TraceModel).where(TraceModel.trace_id == persisted.trace_id))).scalar_one()
        assert row.input == trace.input
        assert row.output == trace.output
        assert row.metadata_ == trace.metadata
        assert isinstance(row.output["n"], int)
        assert isinstance(row.output["f"], float)
        assert isinstance(row.output["ok"], bool)
        assert isinstance(row.output["arr"], list)
    finally:
        await fresh.close()


# ---------------------------------------------------------------------------
# Phase2 JSONB dynamic probe (representative persistence path)
# ---------------------------------------------------------------------------


async def test_phase2_evaluation_run_jsonb_round_trip(db_session: AsyncSession):
    """Write ordinary JSONB values through the Phase2 EvaluationRunModel path."""
    run = EvaluationRunModel(
        id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        dataset_id="ds",
        dataset_version="1",
        suite_id="suite",
        suite_version="1",
        execution_target_id="target",
        execution_target_kind="code",
        status="PENDING",
        dataset_snapshot={"rows": [1, 2, {"k": "v"}], "total": 2},
        suite_snapshot={"name": "s"},
        execution_target_snapshot={"repo": "r"},
        metadata_json={"tags": ["a", "b"], "n": 3, "f": 0.5, "ok": True},
    )
    db_session.add(run)
    await db_session.commit()

    fresh = async_session_factory()
    try:
        row = (await fresh.execute(select(EvaluationRunModel).where(EvaluationRunModel.id == run.id))).scalar_one()
        assert row.dataset_snapshot == run.dataset_snapshot
        assert row.suite_snapshot == run.suite_snapshot
        assert row.execution_target_snapshot == run.execution_target_snapshot
        assert row.metadata_json == run.metadata_json
        assert isinstance(row.metadata_json["n"], int)
        assert isinstance(row.metadata_json["f"], float)
        assert isinstance(row.metadata_json["ok"], bool)
        # No LocalAgent-specific behavior leaks into Phase2.
        assert not isinstance(row.metadata_json, dict) or type(row.metadata_json) is dict
    finally:
        await fresh.close()


# ---------------------------------------------------------------------------
# Generic column is unaffected by the LocalAgent decorator (session.get)
# ---------------------------------------------------------------------------


async def test_unrelated_mapped_jsonb_session_get_plain(db_session: AsyncSession):
    """A generic mapped JSONB field loaded via session.get stays ordinary dict."""
    model = await _add_trace(db_session, output={"a": 1, "b": "x"})
    fresh = async_session_factory()
    try:
        obj = await fresh.get(TraceModel, model.trace_id)
        assert obj.output == {"a": 1, "b": "x"}
        assert type(obj.output) is dict
    finally:
        await fresh.close()
