"""Unit tests for the LocalAgent compatibility wire layer.

Covers the frozen WP4-C framing checks (Content-Length syntax/cardinality,
Content-Type), the bounded streaming body receiver, the rate-limit identity
(project digest) and the Redis failure mapping.  These exercise the real
route helpers directly with hand-built ASGI receive channels; HTTP-level
behaviour is proven in the integration tests.
"""

# ruff: noqa: D415

import hashlib
from uuid import UUID

import pytest
from starlette.requests import Request

from app.api.v1.routes import localagent_integrations as route_module
from app.core.localagent.entities import (
    ENVELOPE_TOO_LARGE,
    LocalAgentCapacityUnavailableError,
    LocalAgentEnvelopeInvalidError,
    LocalAgentEnvelopeTooLargeError,
)

URL = "/integrations/localagent/v1/trace-envelopes"
PROJECT_ID = UUID("00000000-0000-4000-a000-000000000002")


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


def build_request(
    headers: dict[str, str] | None = None,
    chunks: bytes | list[bytes] = b"",
) -> Request:
    """Build a real Starlette Request over a chunked ASGI receive channel."""
    header_pairs = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": URL,
        "raw_path": URL.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": header_pairs,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    if isinstance(chunks, (bytes, bytearray)):
        chunks = [bytes(chunks)]
    return Request(scope, _ChunkReceive(chunks))


# ---------------------------------------------------------------------------
# Content-Length framing
# ---------------------------------------------------------------------------


def test_content_length_valid_proceeds():
    route_module._check_framing(build_request(headers={"content-length": "100", "content-type": "application/json"}))


def test_content_length_missing_allowed():
    route_module._check_framing(build_request(headers={"content-type": "application/json"}))


def test_content_length_oversized_413_before_receive():
    with pytest.raises(LocalAgentEnvelopeTooLargeError) as exc:
        route_module._check_framing(build_request(headers={"content-length": "16385", "content-type": "application/json"}))
    assert exc.value.status_code == 413
    assert exc.value.error_code == ENVELOPE_TOO_LARGE


def test_content_length_huge_integer_413():
    with pytest.raises(LocalAgentEnvelopeTooLargeError):
        route_module._check_framing(
            build_request(headers={"content-length": "99999999999999999999999999999999", "content-type": "application/json"})
        )


def test_content_length_negative_422():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        route_module._check_framing(build_request(headers={"content-length": "-5", "content-type": "application/json"}))


def test_content_length_non_integer_422():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        route_module._check_framing(build_request(headers={"content-length": "abc", "content-type": "application/json"}))


def test_content_length_duplicate_identical_allowed():
    headers = {
        "content-length": "100",
        "content-type": "application/json",
    }
    request = build_request(headers=headers)
    # Add a second identical header directly into the ASGI scope.
    request.scope["headers"].append((b"content-length", b"100"))
    route_module._check_framing(request)


def test_content_length_conflicting_422():
    headers = {
        "content-length": "100",
        "content-type": "application/json",
    }
    request = build_request(headers=headers)
    request.scope["headers"].append((b"content-length", b"101"))
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        route_module._check_framing(request)


# ---------------------------------------------------------------------------
# Content-Type framing
# ---------------------------------------------------------------------------


def test_content_type_application_json_ok():
    route_module._check_framing(build_request(headers={"content-type": "application/json"}))


def test_content_type_utf8_charset_ok():
    route_module._check_framing(build_request(headers={"content-type": "application/json; charset=utf-8"}))
    route_module._check_framing(build_request(headers={"content-type": "application/json; charset=UTF-8"}))
    route_module._check_framing(build_request(headers={"content-type": "Application/JSON"}))


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "application/octet-stream",
        "application/xml",
        "application/json; charset=iso-8859-1",
        "application/json; charset=utf-16",
        "application/json; charset=utf-8; boundary=x",
        "application/json; foo=bar",
        "application/json;",
    ],
)
def test_content_type_invalid_422(content_type: str):
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        route_module._check_framing(build_request(headers={"content-type": content_type}))


def test_content_type_missing_422():
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        route_module._check_framing(build_request(headers={}))


def test_content_type_multiple_conflicting_422():
    request = build_request(headers={"content-type": "application/json"})
    request.scope["headers"].append((b"content-type", b"text/plain"))
    with pytest.raises(LocalAgentEnvelopeInvalidError):
        route_module._check_framing(request)


# ---------------------------------------------------------------------------
# Bounded streaming body receive
# ---------------------------------------------------------------------------


async def test_bounded_receive_under_limit():
    request = build_request(chunks=[b"a" * 8000, b"b" * 8000, b"c" * 384])
    raw = await route_module.receive_bounded_body(request)
    assert len(raw) == 16384


async def test_bounded_receive_exactly_limit_ok():
    request = build_request(chunks=b"x" * 16384)
    raw = await route_module.receive_bounded_body(request)
    assert len(raw) == 16384


async def test_bounded_receive_8000_8000_385_rejects_at_crossing():
    request = build_request(chunks=[b"a" * 8000, b"b" * 8000, b"c" * 385])
    with pytest.raises(LocalAgentEnvelopeTooLargeError) as exc:
        await route_module.receive_bounded_body(request)
    assert exc.value.status_code == 413
    assert exc.value.bytes_received_before_reject == 16000
    assert exc.value.chunks_received_before_reject == 2


async def test_bounded_receive_1mib_collects_only_prefix():
    request = build_request(chunks=[b"x" * 8192] * 128)
    with pytest.raises(LocalAgentEnvelopeTooLargeError) as exc:
        await route_module.receive_bounded_body(request)
    # Only the retained prefix is collected before rejection; the remaining
    # ~1 MiB is never buffered.
    assert exc.value.bytes_received_before_reject == 16384
    assert exc.value.chunks_received_before_reject == 2


async def test_bounded_receive_single_large_chunk_rejects():
    request = build_request(chunks=b"y" * 16385)
    with pytest.raises(LocalAgentEnvelopeTooLargeError) as exc:
        await route_module.receive_bounded_body(request)
    assert exc.value.bytes_received_before_reject == 0
    assert exc.value.chunks_received_before_reject == 0


# ---------------------------------------------------------------------------
# Redis admission identity and failure mapping
# ---------------------------------------------------------------------------


class _FakePipeline:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def incr(self, key: str) -> "_FakePipeline":
        self.keys.append(key)
        return self

    def expire(self, key: str, ttl: int) -> "_FakePipeline":
        return self

    async def execute(self) -> list[int]:
        return [5]


class _FakeRedis:
    def __init__(self, *, broken: bool = False) -> None:
        self.pipe = _FakePipeline()
        self.broken = broken

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        if self.broken:
            raise ConnectionError("redis down")
        return self.pipe


async def test_rate_limit_identity_is_full_project_digest():
    fake = _FakeRedis()
    await route_module._enforce_rate_limit(fake, PROJECT_ID)
    digest = hashlib.sha256(str(PROJECT_ID).encode("ascii")).hexdigest()
    assert len(digest) == 64
    key = fake.pipe.keys[0]
    assert key.startswith("localagent:admission:")
    # Bucket identity segment is the FULL 64-hex project digest.
    assert key.split(":")[-2] == digest
    assert len(key.split(":")[-2]) == 64
    # No raw API key, no truncated credential hash, no caller project ID.
    assert "sk_" not in key
    assert "00000000-0000-4000-a000-000000000002" not in key


async def test_rate_limit_uses_fixed_window_bucket_and_ttl():
    fake = _FakeRedis()
    await route_module._enforce_rate_limit(fake, PROJECT_ID)
    assert len(fake.pipe.keys) == 1
    assert ":" in fake.pipe.keys[0]


async def test_redis_failure_maps_to_capacity_unavailable():
    fake = _FakeRedis(broken=True)
    with pytest.raises(LocalAgentCapacityUnavailableError):
        await route_module._enforce_rate_limit(fake, PROJECT_ID)


async def test_rate_limit_over_quota_raises_429():
    class _OverPipeline(_FakePipeline):
        async def execute(self) -> list[int]:
            return [101]

    class _OverRedis(_FakeRedis):
        def pipeline(self, transaction: bool = True) -> _FakePipeline:
            return _OverPipeline()

    with pytest.raises(route_module.LocalAgentIngestionRateLimitedError):
        await route_module._enforce_rate_limit(_OverRedis(), PROJECT_ID)
