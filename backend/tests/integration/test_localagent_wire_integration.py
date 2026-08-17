"""R1 remediation integration tests for the LocalAgent compatibility endpoint.

Real PostgreSQL + Redis + full FastAPI middleware stack.  Covers the frozen
WP4-C R1 dispositions:

- wire-level type strictness matrix (P1-01)
- Content-Type / Content-Length framing (R1 P2 closures)
- bounded streaming body receive evidence (P1-02)
- exact semantic domains (P1-03, HTTP level)
- large-integer digest non-collision (P1-04)
- global SlowAPI exemption + single admission owner (P1-05)
- Redis outage/recovery, bad-auth quota, oversized-body quota
- transaction fault injection (before-sidecar / legacy write / flush / commit)
- empty-state ownership first-claim race
- legacy delete boundary (ACCEPTED_P2)
- synthetic API-key marker leakage, Phase2 isolation
"""

# ruff: noqa: D415

import asyncio
import json
import logging
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import redis.asyncio as aioredis
import slowapi.middleware as slowapi_middleware
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.routes import localagent_integrations as route_module
from app.core.localagent.entities import (
    AUTHENTICATION_FAILED,
    ENVELOPE_INVALID,
    ENVELOPE_TOO_LARGE,
    EXTERNAL_ID_OWNERSHIP_CONFLICT,
    INGESTION_CAPACITY_UNAVAILABLE,
    INGESTION_RATE_LIMITED,
    PERSISTENCE_UNAVAILABLE,
    LocalAgentEnvelopeTooLargeError,
)
from app.infrastructure.db.engine import async_session_factory, get_db_session
from app.infrastructure.db.models import (
    EvaluationResultModel,
    EvaluationRunModel,
    ExecutionAttemptModel,
    LocalAgentExternalSpanIdentityModel,
    LocalAgentExternalTraceIdentityModel,
    LocalAgentTraceEnvelopeSidecarModel,
    SpanModel,
    TraceModel,
)
from app.infrastructure.db.repositories.identity_repo import IdentityRepository
from app.infrastructure.db.repositories.project_repo import ProjectRepository
from app.infrastructure.redis.client import get_redis
from app.main import app
from app.registry.security import hash_api_key
from app.registry.settings import settings

from .conftest import TEST_ORG_ID, TEST_PROJECT_ID
from .test_localagent_trace_envelopes import _counts, _create_key, _create_project, envelope_payload

pytestmark = pytest.mark.asyncio

URL = "/integrations/localagent/v1/trace-envelopes"


# ---------------------------------------------------------------------------
# Direct ASGI driver (full middleware stack, chunked body control)
# ---------------------------------------------------------------------------


class _ChunkReceive:
    """ASGI receive that yields the given body chunks incrementally."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self._index = 0

    async def __call__(self) -> dict[str, object]:
        if self._index < len(self._chunks):
            body = self._chunks[self._index]
            self._index += 1
            more = self._index < len(self._chunks)
            return {"type": "http.request", "body": body, "more_body": more}
        return {"type": "http.request", "body": b"", "more_body": False}


async def _asgi_post(
    path: str,
    headers: dict[str, str],
    chunks: list[bytes],
    extra_header_pairs: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[str, object] | None, str]:
    """Drive the REAL FastAPI app (all middleware) with raw headers + chunks."""
    header_pairs = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    if extra_header_pairs:
        header_pairs.extend(extra_header_pairs)
    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": header_pairs,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    await app(scope, _ChunkReceive(chunks), send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None
    return status, parsed, body.decode("latin-1", errors="replace")


def _json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload).encode("utf-8")


async def _la_headers_for(client: AsyncClient, db_session: AsyncSession) -> dict[str, str]:
    """Real persisted API key + project headers (like the la_headers fixture)."""
    key = await _create_key(db_session, TEST_ORG_ID, "sk_pp_test_la_key_0000000000000099")
    return {
        "X-API-Key": key,
        "X-Project-ID": str(TEST_PROJECT_ID),
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# P1-01 wire-level type matrix (HTTP)
# ---------------------------------------------------------------------------


async def test_wire_contract_version_matrix(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)
    assert (await client.post(URL, json=envelope_payload(span_id="wv-1"), headers=headers)).status_code == 201
    for bad in ("1", 1.0, True):
        resp = await client.post(
            URL, json=envelope_payload(span_id=f"wv-{bad}", contract_version=bad), headers=headers
        )
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json() == {"status": "REJECTED", "error_code": ENVELOPE_INVALID}


async def test_wire_duration_ms_matrix(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)
    assert (
        await client.post(URL, json=envelope_payload(span_id="wd-1", duration_ms=1), headers=headers)
    ).status_code == 201
    assert (
        await client.post(URL, json=envelope_payload(span_id="wd-2", duration_ms=1.5), headers=headers)
    ).status_code == 201
    for bad in ("1.5", True, None):
        resp = await client.post(
            URL, json=envelope_payload(span_id=f"wd-{bad}", duration_ms=bad), headers=headers
        )
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error_code"] == ENVELOPE_INVALID


async def test_wire_bool_attribute_matrix(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)
    delivery = dict(envelope_payload(), operation="runtime.output_delivery", step_id="step-9")
    assert (
        await client.post(
            URL,
            json=dict(delivery, span_id="wb-1", attributes={"partially_persisted": True}),
            headers=headers,
        )
    ).status_code == 201
    assert (
        await client.post(
            URL,
            json=dict(delivery, span_id="wb-2", attributes={"partially_persisted": False}),
            headers=headers,
        )
    ).status_code == 201
    for bad in (0, 1):
        resp = await client.post(
            URL, json=dict(delivery, span_id=f"wb-{bad}", attributes={"partially_persisted": bad}), headers=headers
        )
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error_code"] == ENVELOPE_INVALID


async def test_wire_integer_attribute_matrix(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)
    assert (
        await client.post(
            URL,
            json=envelope_payload(span_id="wi-1", attributes={"dependency_count": 3}),
            headers=headers,
        )
    ).status_code == 201
    for bad in (3.0, "3", True):
        resp = await client.post(
            URL,
            json=envelope_payload(span_id=f"wi-{bad}", attributes={"dependency_count": bad}),
            headers=headers,
        )
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error_code"] == ENVELOPE_INVALID


async def test_wire_identifier_attribute_matrix(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)
    delivery = dict(envelope_payload(), operation="runtime.output_delivery", step_id="step-9")
    assert (
        await client.post(
            URL,
            json=dict(delivery, span_id="wid-1", attributes={"final_step_id": "abc"}),
            headers=headers,
        )
    ).status_code == 201
    resp = await client.post(
        URL, json=dict(delivery, span_id="wid-2", attributes={"final_step_id": 123}), headers=headers
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == ENVELOPE_INVALID


async def test_wire_identifier_field_matrix(client: AsyncClient, db_session: AsyncSession):
    """Top-level identifier fields accept JSON strings only (P1-01)."""
    headers = await _la_headers_for(client, db_session)
    for field in ("run_id", "trace_id", "span_id", "component", "status", "contract_fingerprint"):
        resp = await client.post(URL, json=envelope_payload(**{field: 123}), headers=headers)
        assert resp.status_code == 422, (field, resp.text)
        assert resp.json()["error_code"] == ENVELOPE_INVALID


# ---------------------------------------------------------------------------
# Content-Type framing (R1 closure)
# ---------------------------------------------------------------------------


async def test_content_type_accepted_representations(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)
    raw = _json_bytes(envelope_payload(span_id="ct-1"))
    resp = await client.post(URL, content=raw, headers={"X-API-Key": headers["X-API-Key"], "X-Project-ID": headers["X-Project-ID"], "Content-Type": "application/json"})
    assert resp.status_code == 201, resp.text
    raw2 = _json_bytes(envelope_payload(span_id="ct-2"))
    resp2 = await client.post(URL, content=raw2, headers={"X-API-Key": headers["X-API-Key"], "X-Project-ID": headers["X-Project-ID"], "Content-Type": "application/json; charset=utf-8"})
    assert resp2.status_code == 201, resp2.text


async def test_content_type_rejected_representations(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)
    raw = _json_bytes(envelope_payload(span_id="ct-bad"))
    base = {"X-API-Key": headers["X-API-Key"], "X-Project-ID": headers["X-Project-ID"]}
    for content_type in ("text/plain", "application/octet-stream", "application/json; charset=iso-8859-1", "application/xml"):
        resp = await client.post(URL, content=raw, headers={**base, "Content-Type": content_type})
        assert resp.status_code == 422, (content_type, resp.text)
        assert resp.json()["error_code"] == ENVELOPE_INVALID
    resp = await client.post(URL, content=raw, headers=base)  # missing Content-Type
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == ENVELOPE_INVALID
    # No business mutation for invalid media type.
    counts = await _counts(db_session)
    assert counts == {"sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0}


# ---------------------------------------------------------------------------
# Content-Length framing (R1 closure)
# ---------------------------------------------------------------------------


async def test_content_length_declared_oversized_413_before_receive(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)
    status, parsed, _ = await _asgi_post(
        URL,
        {**headers, "Content-Length": "20000"},
        [_json_bytes(envelope_payload(span_id="cl-1"))],
    )
    assert status == 413
    assert parsed == {"status": "REJECTED", "error_code": ENVELOPE_TOO_LARGE}
    counts = await _counts(db_session)
    assert counts == {"sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0}


async def test_content_length_negative_and_non_integer_422(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)
    for bad in ("-5", "abc"):
        status, parsed, _ = await _asgi_post(
            URL,
            {**headers, "Content-Length": bad},
            [_json_bytes(envelope_payload(span_id=f"cl-bad-{bad}"))],
        )
        assert status == 422, bad
        assert parsed == {"status": "REJECTED", "error_code": ENVELOPE_INVALID}


async def test_content_length_conflicting_422(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)
    status, parsed, _ = await _asgi_post(
        URL,
        headers,
        [_json_bytes(envelope_payload(span_id="cl-conf"))],
        extra_header_pairs=[(b"content-length", b"100"), (b"content-length", b"101")],
    )
    assert status == 422
    assert parsed == {"status": "REJECTED", "error_code": ENVELOPE_INVALID}


async def test_content_length_huge_integer_413(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)
    status, parsed, _ = await _asgi_post(
        URL,
        {**headers, "Content-Length": "99999999999999999999999999999999"},
        [_json_bytes(envelope_payload(span_id="cl-huge"))],
    )
    assert status == 413
    assert parsed == {"status": "REJECTED", "error_code": ENVELOPE_TOO_LARGE}


# ---------------------------------------------------------------------------
# P1-02 bounded streaming body (HTTP evidence)
# ---------------------------------------------------------------------------


async def test_multi_chunk_8000_8000_385_rejects_at_crossing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    captured: dict[str, int] = {}
    original = route_module.receive_bounded_body

    async def spy(request):
        try:
            return await original(request)
        except LocalAgentEnvelopeTooLargeError as exc:
            captured["bytes"] = exc.bytes_received_before_reject or 0
            captured["chunks"] = exc.chunks_received_before_reject or 0
            raise

    monkeypatch.setattr(route_module, "receive_bounded_body", spy)
    headers = await _la_headers_for(client, db_session)
    status, parsed, _ = await _asgi_post(
        URL, headers, [b"a" * 8000, b"b" * 8000, b"c" * 385]
    )
    assert status == 413
    assert parsed == {"status": "REJECTED", "error_code": ENVELOPE_TOO_LARGE}
    # Only the retained prefix was collected before rejection.
    assert captured == {"bytes": 16000, "chunks": 2}
    # Oversized body never touched auth/Redis/DTO/service/repository.
    counts = await _counts(db_session)
    assert counts == {"sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0}


async def test_1mib_streaming_body_collects_only_bounded_prefix(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    captured: dict[str, int] = {}
    original = route_module.receive_bounded_body

    async def spy(request):
        try:
            return await original(request)
        except LocalAgentEnvelopeTooLargeError as exc:
            captured["bytes"] = exc.bytes_received_before_reject or 0
            captured["chunks"] = exc.chunks_received_before_reject or 0
            raise

    monkeypatch.setattr(route_module, "receive_bounded_body", spy)
    headers = await _la_headers_for(client, db_session)
    status, parsed, _ = await _asgi_post(URL, headers, [b"x" * 8192] * 128)
    assert status == 413
    assert parsed == {"status": "REJECTED", "error_code": ENVELOPE_TOO_LARGE}
    assert captured == {"bytes": 16384, "chunks": 2}
    counts = await _counts(db_session)
    assert counts == {"sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0}


async def test_lying_small_content_length_actual_crossing_rejected(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    captured: dict[str, int] = {}
    original = route_module.receive_bounded_body

    async def spy(request):
        try:
            return await original(request)
        except LocalAgentEnvelopeTooLargeError as exc:
            captured["bytes"] = exc.bytes_received_before_reject or 0
            captured["chunks"] = exc.chunks_received_before_reject or 0
            raise

    monkeypatch.setattr(route_module, "receive_bounded_body", spy)
    headers = await _la_headers_for(client, db_session)
    headers["Content-Length"] = "10"
    status, parsed, _ = await _asgi_post(URL, headers, [b"a" * 8000, b"b" * 8000, b"c" * 385])
    assert status == 413
    assert parsed == {"status": "REJECTED", "error_code": ENVELOPE_TOO_LARGE}
    assert captured == {"bytes": 16000, "chunks": 2}
    counts = await _counts(db_session)
    assert counts == {"sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0}


async def test_oversized_body_never_triggers_auth_redis_or_parse(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """Oversized body: no PostgreSQL auth read, no Redis admission, no DTO parse."""
    boom = AsyncCallableThatFails()
    monkeypatch.setattr(route_module, "authenticate_localagent_project", boom)
    monkeypatch.setattr(route_module, "_enforce_rate_limit", boom)
    monkeypatch.setattr(route_module.LocalAgentTraceEnvelopeInV1, "model_validate_json", boom)
    monkeypatch.setattr(route_module.LocalAgentTraceService, "ingest", boom)
    headers = {"Content-Type": "application/json", "X-API-Key": "sk_pp_whatever", "X-Project-ID": str(TEST_PROJECT_ID)}
    status, parsed, _ = await _asgi_post(URL, headers, [b"z" * 9000, b"y" * 9000])
    assert status == 413
    assert parsed == {"status": "REJECTED", "error_code": ENVELOPE_TOO_LARGE}
    assert boom.calls == 0


class AsyncCallableThatFails:
    """Async spy that fails the test if ever awaited."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *args, **kwargs):  # pragma: no cover - fails when invoked
        """Record the invocation and raise; oversized bodies must never reach it."""
        self.calls += 1
        raise AssertionError("must never be called for an oversized body")


# ---------------------------------------------------------------------------
# P1-04 large-integer digest non-collision (HTTP duplicate semantics)
# ---------------------------------------------------------------------------


async def test_large_integer_duration_never_collapses_to_duplicate(
    client: AsyncClient, db_session: AsyncSession
):
    headers = await _la_headers_for(client, db_session)
    first = await client.post(
        URL, json=envelope_payload(span_id="big-1", duration_ms=9007199254740992), headers=headers
    )
    assert first.status_code == 201, first.text
    different = await client.post(
        URL, json=envelope_payload(span_id="big-1", duration_ms=9007199254740993), headers=headers
    )
    # Semantically distinct accepted envelopes must NOT collapse to a duplicate.
    assert different.status_code == 409, different.text
    exact = await client.post(
        URL, json=envelope_payload(span_id="big-1", duration_ms=9007199254740992), headers=headers
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["status"] == "DUPLICATE_ACCEPTED"


async def test_negative_zero_and_zero_are_exact_duplicates(
    client: AsyncClient, db_session: AsyncSession
):
    """0.0 and -0.0 are numerically equal in the producer's Python semantics."""
    headers = await _la_headers_for(client, db_session)
    assert (
        await client.post(URL, json=envelope_payload(span_id="nz-1", duration_ms=0.0), headers=headers)
    ).status_code == 201
    replay = await client.post(URL, json=envelope_payload(span_id="nz-1", duration_ms=-0.0), headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "DUPLICATE_ACCEPTED"


# ---------------------------------------------------------------------------
# P1-05 global SlowAPI exemption + single admission owner
# ---------------------------------------------------------------------------


async def test_global_slowapi_makes_zero_limiter_calls_for_compatibility(
    client: AsyncClient, db_session: AsyncSession
):
    headers = await _la_headers_for(client, db_session)
    with patch(
        "slowapi.middleware.sync_check_limits",
        side_effect=AssertionError("global SlowAPI limiter must not run for the compatibility route"),
    ):
        resp = await client.post(URL, json=envelope_payload(span_id="exempt-1"), headers=headers)
        assert resp.status_code == 201, resp.text


async def test_global_slowapi_still_active_for_legacy_endpoint(client: AsyncClient):
    calls: list[int] = []
    real = slowapi_middleware.sync_check_limits

    def spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    with patch("slowapi.middleware.sync_check_limits", side_effect=spy):
        resp = await client.get("/health")
        assert resp.status_code == 200, resp.text
    assert calls, "legacy endpoints must remain subject to the global limiter"


async def test_single_admission_owner_429_frozen_dto(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    """Only the compatibility-owned limiter counts; 429 uses the frozen DTO."""
    monkeypatch.setattr(route_module, "LOCALAGENT_TRACE_ENVELOPE_RATE_LIMIT_PER_MINUTE", 2)
    headers = await _la_headers_for(client, db_session)
    for index in range(2):
        resp = await client.post(
            URL, json=envelope_payload(span_id=f"single-{index}"), headers=headers
        )
        assert resp.status_code == 201, resp.text
    resp = await client.post(URL, json=envelope_payload(span_id="single-over"), headers=headers)
    assert resp.status_code == 429
    assert resp.json() == {"status": "REJECTED", "error_code": INGESTION_RATE_LIMITED}


async def test_different_api_keys_same_project_share_quota(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(route_module, "LOCALAGENT_TRACE_ENVELOPE_RATE_LIMIT_PER_MINUTE", 2)
    key_a = await _create_key(db_session, TEST_ORG_ID, "sk_pp_test_la_key_0000000000000091")
    key_b = await _create_key(db_session, TEST_ORG_ID, "sk_pp_test_la_key_0000000000000092")
    headers_a = {"X-API-Key": key_a, "X-Project-ID": str(TEST_PROJECT_ID), "Content-Type": "application/json"}
    headers_b = {"X-API-Key": key_b, "X-Project-ID": str(TEST_PROJECT_ID), "Content-Type": "application/json"}
    assert (await client.post(URL, json=envelope_payload(span_id="share-1"), headers=headers_a)).status_code == 201
    assert (await client.post(URL, json=envelope_payload(span_id="share-2"), headers=headers_b)).status_code == 201
    resp = await client.post(URL, json=envelope_payload(span_id="share-3"), headers=headers_a)
    assert resp.status_code == 429


async def test_different_projects_have_isolated_quota(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(route_module, "LOCALAGENT_TRACE_ENVELOPE_RATE_LIMIT_PER_MINUTE", 2)
    project_b = await _create_project(db_session, TEST_ORG_ID, "Project B Quota")
    key_b = await _create_key(db_session, TEST_ORG_ID, "sk_pp_test_la_key_0000000000000093")
    headers_a = await _la_headers_for(client, db_session)
    headers_b = {"X-API-Key": key_b, "X-Project-ID": str(project_b.id), "Content-Type": "application/json"}
    assert (await client.post(URL, json=envelope_payload(span_id="iso-1"), headers=headers_a)).status_code == 201
    assert (await client.post(URL, json=envelope_payload(span_id="iso-2"), headers=headers_a)).status_code == 201
    assert (await client.post(URL, json=envelope_payload(span_id="iso-a-over"), headers=headers_a)).status_code == 429
    # Project B is untouched.
    assert (
        await client.post(
            URL,
            json=envelope_payload(span_id="iso-b-1", trace_id="trace-b-quota"),
            headers=headers_b,
        )
    ).status_code == 201


# ---------------------------------------------------------------------------
# Redis outage / recovery
# ---------------------------------------------------------------------------


def _dead_redis_client() -> aioredis.Redis:
    """Client pointed at an unbound local port: a REAL TCP availability failure."""
    return aioredis.from_url("redis://127.0.0.1:1", decode_responses=True, socket_connect_timeout=0.5)


async def test_redis_outage_503_and_recovery(client: AsyncClient, db_session: AsyncSession):
    headers = await _la_headers_for(client, db_session)

    async def _dead_redis():
        client = _dead_redis_client()
        try:
            yield client
        finally:
            await client.aclose()

    app.dependency_overrides[get_redis] = _dead_redis
    try:
        resp = await client.post(URL, json=envelope_payload(span_id="outage-1"), headers=headers)
        assert resp.status_code == 503
        assert resp.json() == {"status": "REJECTED", "error_code": INGESTION_CAPACITY_UNAVAILABLE}
        # No PostgreSQL business mutation on Redis failure.
        counts = await _counts(db_session)
        assert counts == {"sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0}
    finally:
        app.dependency_overrides.pop(get_redis, None)

    # Redis "back up" (the real live test Redis): a new request can proceed.
    recovery = await client.post(URL, json=envelope_payload(span_id="outage-2"), headers=headers)
    assert recovery.status_code == 201, recovery.text


# ---------------------------------------------------------------------------
# Bad auth quota behavior
# ---------------------------------------------------------------------------


async def test_bad_auth_does_not_consume_project_quota(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(route_module, "LOCALAGENT_TRACE_ENVELOPE_RATE_LIMIT_PER_MINUTE", 3)
    # Bad auth: missing key (401), invalid key (401), foreign project (403).
    resp = await client.post(URL, json=envelope_payload(span_id="ba-1"), headers={"Content-Type": "application/json"})
    assert resp.status_code == 401
    resp = await client.post(
        URL,
        json=envelope_payload(span_id="ba-2"),
        headers={"X-API-Key": "sk_pp_does_not_exist", "X-Project-ID": str(TEST_PROJECT_ID), "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    other_org = await IdentityRepository(db_session).create_organization("Other Org Quota")
    other_project = await ProjectRepository(db_session).create_project(other_org.id, "Other Project Quota")
    await db_session.commit()
    key = await _create_key(db_session, TEST_ORG_ID, "sk_pp_test_la_key_0000000000000094")
    resp = await client.post(
        URL,
        json=envelope_payload(span_id="ba-3"),
        headers={"X-API-Key": key, "X-Project-ID": str(other_project.id), "Content-Type": "application/json"},
    )
    assert resp.status_code == 403

    # None of the bad-auth requests consumed project quota: 3 valid requests pass.
    for index in range(3):
        resp = await client.post(
            URL, json=envelope_payload(span_id=f"ba-ok-{index}"), headers=headers_for_key(key)
        )
        assert resp.status_code == 201, (index, resp.text)


def headers_for_key(key: str) -> dict[str, str]:
    return {"X-API-Key": key, "X-Project-ID": str(TEST_PROJECT_ID), "Content-Type": "application/json"}


async def test_valid_auth_invalid_envelope_consumes_one_admission(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(route_module, "LOCALAGENT_TRACE_ENVELOPE_RATE_LIMIT_PER_MINUTE", 3)
    headers = await _la_headers_for(client, db_session)
    # Valid authenticated but semantically invalid -> 422 AND consumes one attempt.
    resp = await client.post(
        URL, json=dict(envelope_payload(span_id="vi-1"), duration_ms=-5), headers=headers
    )
    assert resp.status_code == 422
    for index in range(2):
        resp = await client.post(URL, json=envelope_payload(span_id=f"vi-ok-{index}"), headers=headers)
        assert resp.status_code == 201, (index, resp.text)
    resp = await client.post(URL, json=envelope_payload(span_id="vi-over"), headers=headers)
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Transaction fault injection
# ---------------------------------------------------------------------------


async def _failing_execute(db_session: AsyncSession, table_names: set[str], real_execute):
    async def guarded(statement, *args, **kwargs):
        table = getattr(statement, "table", None)
        name = getattr(table, "name", None)
        if name in table_names:
            raise OperationalError("INSERT ...", {}, RuntimeError(f"injected failure at {name}"))
        return await real_execute(statement, *args, **kwargs)

    return guarded


async def test_fault_injection_before_sidecar_legacy_span_write(
    client: AsyncClient, db_session: AsyncSession
):
    """Fault before the sidecar (during the span binding insert) -> 503, zero rows."""
    headers = await _la_headers_for(client, db_session)
    real_execute = db_session.execute
    guarded = await _failing_execute(db_session, {"localagent_external_span_identity"}, real_execute)
    with patch.object(db_session, "execute", side_effect=guarded):
        resp = await client.post(URL, json=envelope_payload(span_id="fi-span"), headers=headers)
        assert resp.status_code == 503, resp.text
        assert resp.json() == {"status": "REJECTED", "error_code": PERSISTENCE_UNAVAILABLE}
    counts = await _counts(db_session)
    assert counts == {"sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0}


async def test_fault_injection_legacy_trace_write(
    client: AsyncClient, db_session: AsyncSession
):
    """Fault during the legacy trace row write -> 503, full rollback, no partial truth."""
    headers = await _la_headers_for(client, db_session)
    real_execute = db_session.execute
    guarded = await _failing_execute(db_session, {"traces", "spans"}, real_execute)
    with patch.object(db_session, "execute", side_effect=guarded):
        resp = await client.post(URL, json=envelope_payload(span_id="fi-trace"), headers=headers)
        assert resp.status_code == 503, resp.text
        assert resp.json() == {"status": "REJECTED", "error_code": PERSISTENCE_UNAVAILABLE}
    counts = await _counts(db_session)
    assert counts == {"sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0}


async def test_fault_injection_flush(
    client: AsyncClient, db_session: AsyncSession
):
    """Flush failure -> 503 PERSISTENCE_UNAVAILABLE, zero residual rows."""
    from unittest.mock import AsyncMock

    headers = await _la_headers_for(client, db_session)
    with patch.object(
        db_session,
        "flush",
        AsyncMock(side_effect=OperationalError("FLUSH", {}, RuntimeError("injected flush failure"))),
    ):
        resp = await client.post(URL, json=envelope_payload(span_id="fi-flush"), headers=headers)
        assert resp.status_code == 503, resp.text
        assert resp.json() == {"status": "REJECTED", "error_code": PERSISTENCE_UNAVAILABLE}
    counts = await _counts(db_session)
    assert counts == {"sidecars": 0, "trace_bindings": 0, "span_bindings": 0, "traces": 0, "spans": 0}


# ---------------------------------------------------------------------------
# Empty-state ownership first-claim race
# ---------------------------------------------------------------------------


async def _fresh_session_override():
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


async def test_empty_state_ownership_first_claim_race(client: AsyncClient, db_session: AsyncSession):
    """Two foreign projects claim the SAME external IDs from an empty state."""
    key_a = await _create_key(db_session, TEST_ORG_ID, "sk_pp_test_la_key_0000000000000095")
    project_b = await _create_project(db_session, TEST_ORG_ID, "Project B First-Claim")
    key_b = await _create_key(db_session, TEST_ORG_ID, "sk_pp_test_la_key_0000000000000096")
    headers_a = {"X-API-Key": key_a, "X-Project-ID": str(TEST_PROJECT_ID), "Content-Type": "application/json"}
    headers_b = {"X-API-Key": key_b, "X-Project-ID": str(project_b.id), "Content-Type": "application/json"}
    payload = envelope_payload(span_id="race-span")

    app_deps = app.dependency_overrides
    app_deps[get_db_session] = await _fresh_session_override()
    try:
        responses = await asyncio.gather(
            client.post(URL, json=payload, headers=headers_a),
            client.post(URL, json=payload, headers=headers_b),
        )
    finally:
        app_deps.pop(get_db_session, None)

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 409], [(r.status_code, r.text) for r in responses]
    winner = next(r for r in responses if r.status_code == 201)
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["error_code"] == EXTERNAL_ID_OWNERSHIP_CONFLICT

    fresh = async_session_factory()
    try:
        counts = await _counts(fresh)
        assert counts == {"sidecars": 1, "trace_bindings": 1, "span_bindings": 1, "traces": 1, "spans": 1}
        # Exactly one authoritative binding; the loser has no residual rows.
        winner_project = str(TEST_PROJECT_ID) if winner.request.headers["x-project-id"] == str(TEST_PROJECT_ID) else str(project_b.id)
        binding = (
            await fresh.execute(
                select(LocalAgentExternalTraceIdentityModel).where(
                    LocalAgentExternalTraceIdentityModel.external_trace_id == payload["trace_id"]
                )
            )
        ).scalar_one()
        assert str(binding.project_id) == winner_project
        loser_project_id = project_b.id if winner_project == str(TEST_PROJECT_ID) else TEST_PROJECT_ID
        loser_trace = (
            await fresh.execute(
                select(LocalAgentExternalTraceIdentityModel).where(
                    LocalAgentExternalTraceIdentityModel.project_id == loser_project_id
                )
            )
        ).all()
        loser_span = (
            await fresh.execute(
                select(LocalAgentExternalSpanIdentityModel).where(
                    LocalAgentExternalSpanIdentityModel.project_id == loser_project_id
                )
            )
        ).all()
        loser_sidecar = (
            await fresh.execute(
                select(LocalAgentTraceEnvelopeSidecarModel).where(
                    LocalAgentTraceEnvelopeSidecarModel.project_id == loser_project_id
                )
            )
        ).all()
        assert loser_trace == [] and loser_span == [] and loser_sidecar == []
    finally:
        await fresh.close()


# ---------------------------------------------------------------------------
# Legacy delete boundary (ACCEPTED_P2)
# ---------------------------------------------------------------------------


async def test_legacy_delete_keeps_sidecar_and_replay_still_200(
    client: AsyncClient, db_session: AsyncSession
):
    headers = await _la_headers_for(client, db_session)
    assert (await client.post(URL, json=envelope_payload(), headers=headers)).status_code == 201
    sidecar = (
        await db_session.execute(
            select(LocalAgentTraceEnvelopeSidecarModel).where(
                LocalAgentTraceEnvelopeSidecarModel.external_span_id == "span-xyz"
            )
        )
    ).scalar_one()

    deleted = await client.delete(f"/traces/{sidecar.internal_trace_uuid}")
    assert deleted.status_code == 204

    # Sidecar / bindings survive (they are not FK-coupled to legacy rows).
    fresh = async_session_factory()
    try:
        assert (await _sidecar_count(fresh, "span-xyz")) == 1
        assert (
            await fresh.execute(
                select(func.count()).select_from(LocalAgentExternalTraceIdentityModel)
            )
        ).scalar_one() == 1
    finally:
        await fresh.close()

    # Exact replay -> 200 DUPLICATE_ACCEPTED; the legacy read model stays absent.
    replay = await client.post(URL, json=envelope_payload(), headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "DUPLICATE_ACCEPTED"
    legacy_trace_count = (
        await db_session.execute(
            select(func.count()).select_from(TraceModel).where(TraceModel.trace_id == sidecar.internal_trace_uuid)
        )
    ).scalar_one()
    assert legacy_trace_count == 0


async def _sidecar_count(session: AsyncSession, span_id: str) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(LocalAgentTraceEnvelopeSidecarModel).where(
                LocalAgentTraceEnvelopeSidecarModel.external_span_id == span_id
            )
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# Synthetic API-key marker safety
# ---------------------------------------------------------------------------


async def test_synthetic_api_key_marker_never_leaks(
    client: AsyncClient, db_session: AsyncSession, caplog
):
    marker = "sk_pp_synthetic_marker_7f3a9c2b00000000000001"
    await _create_key(db_session, TEST_ORG_ID, marker)
    headers = {"X-API-Key": marker, "X-Project-ID": str(TEST_PROJECT_ID), "Content-Type": "application/json"}

    with caplog.at_level(logging.WARNING):
        success = await client.post(URL, json=envelope_payload(span_id="marker-1"), headers=headers)
        assert success.status_code == 201
        rejected = await client.post(
            URL, json=dict(envelope_payload(span_id="marker-2"), duration_ms=-5), headers=headers
        )
        assert rejected.status_code == 422

    assert marker not in success.text
    assert marker not in rejected.text

    # Structured logs carry no raw key.
    for record in caplog.records:
        assert marker not in record.getMessage()

    # DB sidecar / bindings / legacy rows carry no raw key.
    fresh = async_session_factory()
    try:
        for model in (
            LocalAgentTraceEnvelopeSidecarModel,
            LocalAgentExternalTraceIdentityModel,
            LocalAgentExternalSpanIdentityModel,
            TraceModel,
            SpanModel,
        ):
            rows = (await fresh.execute(select(model))).scalars().all()
            for row in rows:
                assert marker not in str(row)
    finally:
        await fresh.close()

    # Redis keys carry no raw key; admission identity is the project digest.
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        keys = await redis_client.keys("*")
        assert all(marker not in key for key in keys)
        assert all("localagent:admission:" in key or not key.startswith("localagent") for key in keys)
    finally:
        await redis_client.aclose()


# ---------------------------------------------------------------------------
# Phase2 isolation
# ---------------------------------------------------------------------------


async def test_phase2_tables_untouched_after_compatibility_ingestion(
    client: AsyncClient, db_session: AsyncSession
):
    headers = await _la_headers_for(client, db_session)
    assert (await client.post(URL, json=envelope_payload(span_id="iso-p2"), headers=headers)).status_code == 201

    fresh = async_session_factory()
    try:
        for model in (EvaluationRunModel, ExecutionAttemptModel, EvaluationResultModel):
            count = (await fresh.execute(select(func.count()).select_from(model))).scalar_one()
            assert count == 0, f"{model.__tablename__} mutated by compatibility ingestion"
    finally:
        await fresh.close()
