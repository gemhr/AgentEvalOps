"""LocalAgent compatibility ingestion endpoint.

``POST /integrations/localagent/v1/trace-envelopes`` is the strict,
fail-closed boundary approved by the WP4-C Architecture Decision and the
45 Redis-admission re-entry.  It is NOT the legacy ``POST /traces``
contract: it requires a versioned DTO with ``extra=forbid``, strict wire
typing, exact contract identity/version/fingerprint, frozen envelope
semantics, a server-computed canonical digest and a single PostgreSQL
transaction committed before any 2xx is returned.

Frozen WP4-C request order (explicitly owned by this boundary, not by
incidental FastAPI dependency ordering):

1. framing/header checks (Content-Length syntax/cardinality/gross limit,
   Content-Type)
2. bounded streaming body receive (stop immediately at 16384 crossing)
3. existing-project authentication/project resolution (PostgreSQL reads)
4. compatibility-owned Redis admission (project digest identity)
5. strict raw JSON/DTO parse + contract semantic validation
6. one PostgreSQL compatibility transaction
7. PostgreSQL commit
8. 201/200

Stable bounded failure responses: ``{status: "REJECTED", error_code}``.
No payload echo, no raw IDs, no exception detail.  Redis is an ADMISSION
dependency only (fail closed); PostgreSQL is the persistence + ack
dependency; Celery never participates.  The global SlowAPI limiter is
exempt for this route — this compatibility-owned limiter is the single
admission owner.
"""

from __future__ import annotations

import hashlib
import time
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request, Response
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import authenticate_localagent_project
from app.api.rate_limit import limiter
from app.core.localagent.decoder import EnvelopeDecodeError, decode_envelope_body
from app.core.localagent.entities import (
    LocalAgentCapacityUnavailableError,
    LocalAgentCompatibilityError,
    LocalAgentEnvelopeInvalidError,
    LocalAgentEnvelopeTooLargeError,
    LocalAgentIngestionRateLimitedError,
    LocalAgentInternalError,
    LocalAgentPersistenceUnavailableError,
    LocalAgentTraceEnvelopeInV1,
    LocalAgentTraceEnvelopeOutV1,
)
from app.infrastructure.db.engine import get_db_session
from app.infrastructure.redis.client import get_redis
from app.services.localagent_trace_service import (
    INGEST_OUTCOME_DUPLICATE,
    LocalAgentTraceService,
)

router = APIRouter(prefix="/integrations/localagent", tags=["localagent"])

# -- Code-owned transport bounds ------------------------------------------------
# 16 KiB: the frozen DTO worst-case serialization is ~4 KiB (verified by a unit
# test), so 16 KiB is a small, derived, non-arbitrary bound with headroom and is
# far below typical deployment body limits.  Not configurable — never unbounded.
LOCALAGENT_TRACE_ENVELOPE_MAX_BYTES = 16 * 1024
# Per-authenticated-project admission (fixed window), matching the frozen 45
# operational contract.  The identity is the full SHA-256 of the project UUID.
LOCALAGENT_TRACE_ENVELOPE_RATE_LIMIT_PER_MINUTE = 100
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_KEY_TTL_SECONDS = 120
_RATE_LIMIT_NAMESPACE = "localagent:admission"

# Internal fixed reason codes for framing violations (content-free, log-only).
_FRAMING_REASON_INVALID_CONTENT_LENGTH = "INVALID_CONTENT_LENGTH"
_FRAMING_REASON_CONFLICTING_CONTENT_LENGTH = "CONFLICTING_CONTENT_LENGTH"
_FRAMING_REASON_MISSING_CONTENT_TYPE = "MISSING_CONTENT_TYPE"
_FRAMING_REASON_INVALID_CONTENT_TYPE = "INVALID_CONTENT_TYPE"


# -- Stage 1: framing / header checks ------------------------------------------


def _header_values(request: Request, name: bytes) -> list[str]:
    """Return all raw header values for ``name`` from the ASGI scope."""
    return [
        value.decode("latin-1")
        for raw_name, value in request.scope.get("headers", [])
        if raw_name == name
    ]


def _check_framing(request: Request) -> None:
    """Fail closed on Content-Length syntax/cardinality and Content-Type.

    - ``Content-Length > 16384`` -> 413 ``LOCALAGENT_ENVELOPE_TOO_LARGE``
      (fail before body receive; never allocate from the declared length).
    - malformed / negative / conflicting Content-Length -> 422
      ``LOCALAGENT_ENVELOPE_INVALID``.
    - missing Content-Length -> allowed (the stream is still bounded by the
      bounded receiver); multiple identical values -> allowed (RFC 7230).
    - only ``application/json`` or ``application/json; charset=utf-8``
      (charset/media type case-insensitive) is accepted; anything else,
      including missing Content-Type, -> 422 ``LOCALAGENT_ENVELOPE_INVALID``.
    """
    lengths = _header_values(request, b"content-length")
    if len(lengths) == 1:
        raw = lengths[0]
        if not raw.isdigit():
            raise LocalAgentEnvelopeInvalidError(_FRAMING_REASON_INVALID_CONTENT_LENGTH)
        declared = int(raw)
    elif len(lengths) > 1:
        seen: set[int] = set()
        for raw in lengths:
            if not raw.isdigit():
                raise LocalAgentEnvelopeInvalidError(_FRAMING_REASON_INVALID_CONTENT_LENGTH)
            seen.add(int(raw))
        if len(seen) > 1:
            raise LocalAgentEnvelopeInvalidError(_FRAMING_REASON_CONFLICTING_CONTENT_LENGTH)
        declared = seen.pop()
    else:
        declared = None
    if declared is not None and declared > LOCALAGENT_TRACE_ENVELOPE_MAX_BYTES:
        raise LocalAgentEnvelopeTooLargeError(reason="declared_content_length_exceeded")

    content_types = _header_values(request, b"content-type")
    if len(content_types) != 1:
        reason = (
            _FRAMING_REASON_MISSING_CONTENT_TYPE if not content_types else _FRAMING_REASON_INVALID_CONTENT_TYPE
        )
        raise LocalAgentEnvelopeInvalidError(reason)
    raw_content_type = content_types[0]
    media_type, _, params = raw_content_type.partition(";")
    if media_type.strip().lower() != "application/json":
        raise LocalAgentEnvelopeInvalidError(_FRAMING_REASON_INVALID_CONTENT_TYPE)
    # A bare trailing ';' is neither of the two accepted representations.
    if ";" in raw_content_type and not params.strip():
        raise LocalAgentEnvelopeInvalidError(_FRAMING_REASON_INVALID_CONTENT_TYPE)
    if params.strip():
        for param in params.split(";"):
            name, sep, value = param.strip().partition("=")
            if not sep or name.strip().lower() != "charset" or value.strip().lower() != "utf-8":
                raise LocalAgentEnvelopeInvalidError(_FRAMING_REASON_INVALID_CONTENT_TYPE)


# -- Stage 2: bounded streaming body receive ------------------------------------


async def receive_bounded_body(request: Request) -> bytes:
    """Read the ASGI request body incrementally, bounded at 16 KiB.

    Chunks are accumulated only while the total stays ``<= 16384``.  As soon
    as the next chunk would cross the bound the receiver stops collecting,
    raises 413 ``LOCALAGENT_ENVELOPE_TOO_LARGE`` and never buffers the
    remaining stream.  ``request.body()`` is never used for this endpoint.

    The crossing chunk is observed but NOT retained; the exception carries
    internal evidence (``bytes_received_before_reject`` = the retained
    prefix size, ``chunks_received_before_reject`` = retained chunk count)
    for admission instrumentation.  Memory never exceeds the bound.
    """
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        if chunk:
            received += len(chunk)
            if received > LOCALAGENT_TRACE_ENVELOPE_MAX_BYTES:
                raise LocalAgentEnvelopeTooLargeError(
                    reason="streaming_crossing",
                    bytes_received_before_reject=received - len(chunk),
                    chunks_received_before_reject=len(chunks),
                )
            chunks.append(chunk)
    return b"".join(chunks)


# -- Stage 4: compatibility-owned Redis admission -------------------------------


async def _enforce_rate_limit(redis_client: aioredis.Redis, project_id: UUID) -> None:
    """Per-authenticated-project bounded admission (single rate-limit owner).

    The identity is the FULL SHA-256 digest of the authenticated project
    UUID (fixed namespace + 64-hex digest + time bucket).  Raw API keys and
    truncated credential hashes are never used as bucket identity.  Any
    Redis failure (connection/timeout/pipeline/decode/unexpected state)
    fails closed to ``503 INGESTION_CAPACITY_UNAVAILABLE`` — content-free,
    with no PostgreSQL business mutation.
    """
    identity = hashlib.sha256(str(project_id).encode("ascii")).hexdigest()
    bucket = int(time.time() // _RATE_LIMIT_WINDOW_SECONDS)
    key = f"{_RATE_LIMIT_NAMESPACE}:{identity}:{bucket}"
    try:
        pipe = redis_client.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, _RATE_LIMIT_KEY_TTL_SECONDS)
        results = await pipe.execute()
        count = int(results[0])
    except Exception as exc:
        raise LocalAgentCapacityUnavailableError() from exc
    if count > LOCALAGENT_TRACE_ENVELOPE_RATE_LIMIT_PER_MINUTE:
        raise LocalAgentIngestionRateLimitedError()


# -- Endpoint -------------------------------------------------------------------


@router.post(
    "/v1/trace-envelopes",
    status_code=201,
    response_model=LocalAgentTraceEnvelopeOutV1,
)
async def ingest_trace_envelope(
    request: Request,
    response: Response,
    redis_client: aioredis.Redis = Depends(get_redis),
    session: AsyncSession = Depends(get_db_session),
) -> LocalAgentTraceEnvelopeOutV1:
    """Ingest exactly one frozen LocalAgent envelope.

    ``201`` first commit, ``200`` exact replay (``DUPLICATE_ACCEPTED``),
    otherwise a bounded ``REJECTED`` response.  The 2xx is returned only
    after the PostgreSQL transaction commits.  The frozen stage order is
    enforced inline (framing -> bounded receive -> auth -> Redis admission
    -> strict parse/validation -> transaction -> commit -> 2xx); oversized
    bodies never touch auth, Redis, DTO parse, service or repository.
    """
    try:
        _check_framing(request)
        raw = await receive_bounded_body(request)
        ctx = await authenticate_localagent_project(request, session)
        await _enforce_rate_limit(redis_client, ctx.project.id)

        try:
            # ONE code-owned JSON decoding path (token-aware, duplicate-key
            # rejecting, no NaN/Infinity); the DTO then validates the already
            # decoded dict — the raw body is never parsed twice with different
            # numeric semantics.
            parsed = decode_envelope_body(raw)
            envelope = LocalAgentTraceEnvelopeInV1.model_validate(parsed)
        except EnvelopeDecodeError as exc:
            raise LocalAgentEnvelopeInvalidError(reason=exc.reason) from exc
        except ValidationError as exc:
            raise LocalAgentEnvelopeInvalidError() from exc

        outcome = await LocalAgentTraceService(session).ingest(envelope, ctx.project.id)
        # Explicit flush makes the flush stage a testable, deterministic
        # failure point (frozen order stage 7) without weakening the
        # commit-before-2xx guarantee: any flush failure rolls back and maps
        # to a bounded 503 PERSISTENCE_UNAVAILABLE.
        await session.flush()
        await session.commit()
    except LocalAgentCompatibilityError:
        raise
    except DBAPIError as exc:
        raise LocalAgentPersistenceUnavailableError() from exc
    except Exception as exc:
        raise LocalAgentInternalError() from exc

    if outcome == INGEST_OUTCOME_DUPLICATE:
        response.status_code = 200
        return LocalAgentTraceEnvelopeOutV1(status="DUPLICATE_ACCEPTED", error_code=None)
    return LocalAgentTraceEnvelopeOutV1(status="PERSISTED", error_code=None)


# The global SlowAPI limiter must make ZERO limiter calls for this endpoint
# (45 §9/§10: single rate-limit owner).  ``limiter.exempt`` registers the
# route name by module/function identity, so the real SlowAPIMiddleware
# bypasses the global default limiter here while legacy routes stay active.
limiter.exempt(ingest_trace_envelope)
