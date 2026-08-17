"""LocalAgent compatibility DTOs and typed bounded failure errors.

The request DTO mirrors the frozen ``TraceExportEnvelope`` exactly and
rejects every unknown field (``extra=forbid``).  The response DTO is the
tiny ``{status, error_code}`` contract with no payload echo.

Typed errors carry a bounded, content-free stable code and an HTTP status
so the API layer can translate them without leaking raw exception detail.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, Strict, field_validator

from app.core.localagent.contract import (
    FINGERPRINT_PATTERN,
    MAX_V1_DURATION_INT,
    OPERATION_PATTERN,
    SAFE_IDENTIFIER_PATTERN,
    TERMINAL_STATUSES,
)

# -- Frozen stable failure codes ------------------------------------------------

CONTRACT_IDENTITY_UNSUPPORTED = "LOCALAGENT_CONTRACT_IDENTITY_UNSUPPORTED"
CONTRACT_VERSION_UNSUPPORTED = "LOCALAGENT_CONTRACT_VERSION_UNSUPPORTED"
CONTRACT_FINGERPRINT_UNSUPPORTED = "LOCALAGENT_CONTRACT_FINGERPRINT_UNSUPPORTED"
ENVELOPE_INVALID = "LOCALAGENT_ENVELOPE_INVALID"
AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
PROJECT_FORBIDDEN = "PROJECT_FORBIDDEN"
PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
EXTERNAL_ID_OWNERSHIP_CONFLICT = "LOCALAGENT_EXTERNAL_ID_OWNERSHIP_CONFLICT"
ENVELOPE_CONFLICT = "LOCALAGENT_ENVELOPE_CONFLICT"
TRACE_IDENTITY_CONFLICT = "LOCALAGENT_TRACE_IDENTITY_CONFLICT"
ENVELOPE_TOO_LARGE = "LOCALAGENT_ENVELOPE_TOO_LARGE"
INGESTION_RATE_LIMITED = "INGESTION_RATE_LIMITED"
INGESTION_CAPACITY_UNAVAILABLE = "INGESTION_CAPACITY_UNAVAILABLE"
PERSISTENCE_UNAVAILABLE = "PERSISTENCE_UNAVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"


class LocalAgentCompatibilityError(Exception):
    """Base for every compatibility failure; carries only bounded content-free state."""

    status_code: int = 500
    error_code: str = INTERNAL_ERROR

    def __init__(self) -> None:
        """Initialise with the class-level status/code (no dynamic detail)."""
        super().__init__(self.error_code)

    def __repr__(self) -> str:
        """Bounded repr containing only the stable code and status."""
        return f"{self.__class__.__name__}(error_code={self.error_code!r}, status_code={self.status_code})"


class LocalAgentEnvelopeInvalidError(LocalAgentCompatibilityError):
    """Malformed, unknown-field or semantically invalid envelope.

    ``reason`` is an internal fixed code (never raw payload content) that
    may be used for structured logging; it is never part of the wire
    response.
    """

    status_code = 422
    error_code = ENVELOPE_INVALID

    def __init__(self, reason: str | None = None) -> None:
        """Store an internal fixed reason code (content-free)."""
        self.reason = reason
        super().__init__()


class LocalAgentContractIdentityUnsupportedError(LocalAgentCompatibilityError):
    """Unsupported contract identity (fail closed, no migration)."""

    status_code = 422
    error_code = CONTRACT_IDENTITY_UNSUPPORTED


class LocalAgentContractVersionUnsupportedError(LocalAgentCompatibilityError):
    """Unsupported contract version (fail closed, no up-conversion)."""

    status_code = 422
    error_code = CONTRACT_VERSION_UNSUPPORTED


class LocalAgentContractFingerprintUnsupportedError(LocalAgentCompatibilityError):
    """Unsupported contract fingerprint (fail closed, no compatibility window)."""

    status_code = 422
    error_code = CONTRACT_FINGERPRINT_UNSUPPORTED


class LocalAgentEnvelopeTooLargeError(LocalAgentCompatibilityError):
    """Request body exceeds the code-owned payload bound.

    ``bytes_received_before_reject`` / ``chunks_received_before_reject``
    are internal admission evidence (how much of the stream had been
    collected when the bound was crossed); they are never part of the wire
    response.  The receiver stops at the crossing and does not retain the
    crossing chunk, so the collected prefix is always ``<=`` the bound.
    """

    status_code = 413
    error_code = ENVELOPE_TOO_LARGE

    def __init__(
        self,
        *,
        reason: str | None = None,
        bytes_received_before_reject: int | None = None,
        chunks_received_before_reject: int | None = None,
    ) -> None:
        """Store internal fixed admission evidence (content-free wire response)."""
        self.reason = reason
        self.bytes_received_before_reject = bytes_received_before_reject
        self.chunks_received_before_reject = chunks_received_before_reject
        super().__init__()


class LocalAgentAuthenticationFailedError(LocalAgentCompatibilityError):
    """Missing, invalid, revoked or expired API key."""

    status_code = 401
    error_code = AUTHENTICATION_FAILED


class LocalAgentProjectForbiddenError(LocalAgentCompatibilityError):
    """Project exists but belongs to a different organization."""

    status_code = 403
    error_code = PROJECT_FORBIDDEN


class LocalAgentProjectNotFoundError(LocalAgentCompatibilityError):
    """Project does not exist (and must not be auto-created)."""

    status_code = 404
    error_code = PROJECT_NOT_FOUND


class LocalAgentOwnershipConflictError(LocalAgentCompatibilityError):
    """Foreign project reuses an external trace/span identity."""

    status_code = 409
    error_code = EXTERNAL_ID_OWNERSHIP_CONFLICT


class LocalAgentEnvelopeConflictError(LocalAgentCompatibilityError):
    """Conflicting duplicate envelope or span binding association."""

    status_code = 409
    error_code = ENVELOPE_CONFLICT


class LocalAgentTraceIdentityConflictError(LocalAgentCompatibilityError):
    """Same external trace identity bound to a different run_id."""

    status_code = 409
    error_code = TRACE_IDENTITY_CONFLICT


class LocalAgentIngestionRateLimitedError(LocalAgentCompatibilityError):
    """Per-identity ingestion rate limit exceeded."""

    status_code = 429
    error_code = INGESTION_RATE_LIMITED


class LocalAgentCapacityUnavailableError(LocalAgentCompatibilityError):
    """Admission/capacity infrastructure (Redis) unavailable."""

    status_code = 503
    error_code = INGESTION_CAPACITY_UNAVAILABLE


class LocalAgentPersistenceUnavailableError(LocalAgentCompatibilityError):
    """PostgreSQL unavailable or commit failed; no 2xx is ever returned."""

    status_code = 503
    error_code = PERSISTENCE_UNAVAILABLE


class LocalAgentInternalError(LocalAgentCompatibilityError):
    """Unexpected server failure (content-free)."""

    status_code = 500
    error_code = INTERNAL_ERROR


# -- Request DTO ----------------------------------------------------------------


class LocalAgentTraceEnvelopeInV1(BaseModel):
    """Strict request DTO for ``POST /integrations/localagent/v1/trace-envelopes``.

    Top-level fields mirror the frozen ``TraceExportEnvelope`` exactly.
    No legacy fields (``input``/``output``/``metadata``/raw ``error``/
    ``session_id``/``evaluation_run_id``) exist, and unknown fields are
    rejected by ``extra=forbid``.

    Wire typing is strict (P1-01): every identifier is a JSON string,
    ``contract_version`` is a JSON integer and ``duration_ms`` is a JSON
    number.  The DTO validates the producer's actual JSON type before any
    Pydantic conversion; it never relies on semantic checks after
    coercion.  ``duration_ms`` is parsed to an exact ``Decimal`` so that
    distinct large JSON integers can never collapse through a float
    conversion (P1-04).
    """

    model_config = ConfigDict(extra="forbid")

    contract_identity: Annotated[str, Strict()]
    contract_version: Annotated[int, Strict()]
    contract_fingerprint: Annotated[str, Strict()]
    run_id: Annotated[str, Strict()]
    trace_id: Annotated[str, Strict()]
    span_id: Annotated[str, Strict()]
    parent_span_id: Annotated[str | None, Strict()]
    step_id: Annotated[str | None, Strict()]
    operation: Annotated[str, Strict()]
    component: Annotated[str, Strict()]
    started_at: datetime
    completed_at: datetime
    duration_ms: Decimal
    status: Annotated[str, Strict()]
    error_code: Annotated[str | None, Strict()]
    attributes: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @field_validator("duration_ms", mode="before")
    @classmethod
    def _duration_semantic_value(cls, value: object) -> object:
        """Normalize one wire duration value to its exact semantic Decimal.

        Mirrors the frozen producer domain (65 §12, 75 Gate):

        - ``int`` token -> ``0 <= value <= MAX_V1_DURATION_INT`` -> ``Decimal(int)``
          (exact; ``2**53+1`` and the 309-digit MAX are preserved losslessly,
          and values above MAX are rejected before any Decimal/string expansion);
        - ``float`` token (binary64) -> finite and ``>= 0`` ->
          ``Decimal.from_float(value)`` (exact binary64 semantics, never the
          shortest decimal spelling);
        - negative zero (and any zero) -> canonical ``Decimal(0)``;
        - ``bool``/``string``/``null`` are never JSON numbers -> rejected.

        The route's token-aware decoder already guarantees the value is an
        ``int`` or ``float``; this validator is also the defense-in-depth for
        direct construction.
        """
        if isinstance(value, bool) or value is None:
            raise ValueError("duration_ms must be a JSON number")
        if isinstance(value, Decimal):
            if not value.is_finite() or value < 0:
                raise ValueError("duration_ms out of domain")
            return value
        if isinstance(value, int):
            if value < 0 or value > MAX_V1_DURATION_INT:
                raise ValueError("duration_ms out of domain")
            return Decimal(value)
        if isinstance(value, float):
            if not math.isfinite(value) or value < 0:
                raise ValueError("duration_ms out of domain")
            if value == 0:
                return Decimal(0)
            return Decimal.from_float(value)
        raise ValueError("duration_ms must be a JSON number")

    @field_validator("attributes", mode="before")
    @classmethod
    def _attributes_strict_scalars(cls, value: object) -> object:
        """Reject non-scalar wire attribute values (lists/dicts/null).

        JSON booleans, integers, numbers and strings are preserved without
        cross-type coercion by the union type; per-key strict typing
        (BOOL/NON_NEGATIVE_INT/SAFE_IDENTIFIER/DIGEST) is enforced by the
        semantic validator against these raw wire values.
        """
        if not isinstance(value, dict):
            raise ValueError("attributes must be a JSON object")
        for item in value.values():
            if isinstance(item, bool):
                continue
            if not isinstance(item, (str, int, float)):
                raise ValueError("attribute values must be JSON scalars")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("attribute floats must be finite")
        return value

    @field_validator("run_id", "trace_id", "span_id", "parent_span_id", "step_id", "component", "error_code")
    @classmethod
    def _safe_identifier_shape(cls, value: str | None) -> str | None:
        """Reject identifiers that do not match the frozen safe-identifier shape."""
        if value is None:
            return None
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("invalid safe identifier shape")
        return value

    @field_validator("contract_fingerprint")
    @classmethod
    def _fingerprint_shape(cls, value: str) -> str:
        """Reject fingerprints that are not lowercase 64-hex strings."""
        if not FINGERPRINT_PATTERN.fullmatch(value):
            raise ValueError("invalid contract fingerprint shape")
        return value

    @field_validator("operation")
    @classmethod
    def _operation_shape(cls, value: str) -> str:
        """Reject operations that do not match the frozen operation shape."""
        if not OPERATION_PATTERN.fullmatch(value):
            raise ValueError("invalid operation shape")
        return value

    @field_validator("status")
    @classmethod
    def _terminal_status(cls, value: str) -> str:
        """Reject any non-terminal or unknown span status."""
        if value not in TERMINAL_STATUSES:
            raise ValueError("invalid terminal span status")
        return value


# -- Response DTO ---------------------------------------------------------------


class LocalAgentTraceEnvelopeOutV1(BaseModel):
    """Tiny bounded response DTO; no payload echo, no raw IDs, no exception detail."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["PERSISTED", "DUPLICATE_ACCEPTED", "REJECTED"]
    error_code: str | None = None
