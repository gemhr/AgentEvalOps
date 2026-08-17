"""LocalAgent envelope semantic validation and canonical payload digest.

``validate_envelope_semantics`` re-implements the frozen producer
contract invariants (time order, duration, terminal status/error rule,
step correlation, operation domain and category attribute schema/value
domains) as a defense-in-depth consumer validator.  Any violation maps
to the single bounded wire code ``LOCALAGENT_ENVELOPE_INVALID``.

``canonical_payload_digest`` computes a deterministic server-side SHA-256
over the strict validated DTO.  It never trusts a producer-sent digest and
never includes server-local mutable fields (internal UUIDs, project id,
timestamps of persistence).
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.localagent.contract import (
    ATTR_BOOL,
    ATTR_DIGEST,
    ATTR_FINITE_FLOAT,
    ATTR_NON_NEGATIVE_INT,
    ATTR_SAFE_IDENTIFIER,
    CATEGORY_ATTRIBUTE_DOMAINS,
    CATEGORY_ATTRIBUTE_SCHEMAS,
    FINGERPRINT_PATTERN,
    SAFE_IDENTIFIER_PATTERN,
    STABLE_OPERATIONS,
    TRACE_EXPORT_CONTRACT_FINGERPRINT,
    TRACE_EXPORT_CONTRACT_IDENTITY,
    TRACE_EXPORT_CONTRACT_VERSION,
)
from app.core.localagent.entities import (
    LocalAgentContractFingerprintUnsupportedError,
    LocalAgentContractIdentityUnsupportedError,
    LocalAgentContractVersionUnsupportedError,
    LocalAgentEnvelopeInvalidError,
    LocalAgentTraceEnvelopeInV1,
)

# Content-free internal reason codes (fixed vocabulary, never raw values).
_INVALID_IDENTITY = "INVALID_IDENTITY"
_INVALID_OPERATION = "INVALID_OPERATION"
_UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
_STEP_CORRELATION_MISSING = "STEP_CORRELATION_MISSING"
_STEP_CORRELATION_INVALID = "STEP_CORRELATION_INVALID"
_SPAN_TIME_INVALID = "SPAN_TIME_INVALID"
_SPAN_TIME_ORDER_INVALID = "SPAN_TIME_ORDER_INVALID"
_SPAN_DURATION_INVALID = "SPAN_DURATION_INVALID"
_STATUS_INVALID = "STATUS_INVALID"
_ERROR_CODE_ON_OK = "ERROR_CODE_ON_OK"
_ERROR_CODE_MISSING = "ERROR_CODE_MISSING"
_ATTRIBUTES_NOT_MAPPING = "ATTRIBUTES_NOT_MAPPING"
_UNKNOWN_ATTRIBUTE_KEY = "UNKNOWN_ATTRIBUTE_KEY"
_ATTRIBUTE_VALUE_INVALID = "ATTRIBUTE_VALUE_INVALID"
_ATTRIBUTE_TYPE_INVALID = "ATTRIBUTE_TYPE_INVALID"
_ATTRIBUTE_DOMAIN_INVALID = "ATTRIBUTE_DOMAIN_INVALID"

# Maximum canonical fixed-point length for the frozen duration domain (65 §19):
# the exact binary64 minimum positive subnormal renders as at most 1076 chars.
# Every accepted duration — 0..309-digit int, or a finite binary64 >= 0 — is
# bounded by this code-owned cap, so canonical output size is predictable.
CANONICAL_NUMBER_MAX_CHARS = 1076


def _reject(reason: str) -> None:
    """Raise the single wire code for every semantic violation.

    ``reason`` is an internal fixed code recorded on the exception for
    structured logging only; the wire response is always
    ``LOCALAGENT_ENVELOPE_INVALID``.
    """
    raise LocalAgentEnvelopeInvalidError(reason)


def validate_contract(envelope: LocalAgentTraceEnvelopeInV1) -> None:
    """Fail closed on identity / version / fingerprint mismatch.

    No compatibility window, migration or up-conversion is provided.
    Shape-level failures (non-string identity, non-int version, malformed
    fingerprint) were already rejected at DTO parse time and surface as
    ``LOCALAGENT_ENVELOPE_INVALID``.
    """
    if envelope.contract_identity != TRACE_EXPORT_CONTRACT_IDENTITY:
        raise LocalAgentContractIdentityUnsupportedError()
    if envelope.contract_version != TRACE_EXPORT_CONTRACT_VERSION:
        raise LocalAgentContractVersionUnsupportedError()
    if envelope.contract_fingerprint != TRACE_EXPORT_CONTRACT_FINGERPRINT:
        raise LocalAgentContractFingerprintUnsupportedError()


def _validate_attribute_value(value: object, type_: str, domain) -> str | None:
    """Validate one attribute value shape and optional value domain."""
    if type_ == ATTR_BOOL:
        if not isinstance(value, bool):
            return _ATTRIBUTE_TYPE_INVALID
    elif type_ == ATTR_NON_NEGATIVE_INT:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return _ATTRIBUTE_TYPE_INVALID
    elif type_ == ATTR_FINITE_FLOAT:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return _ATTRIBUTE_TYPE_INVALID
    elif type_ == ATTR_SAFE_IDENTIFIER:
        if not isinstance(value, str) or not SAFE_IDENTIFIER_PATTERN.fullmatch(value):
            return _ATTRIBUTE_TYPE_INVALID
    elif type_ == ATTR_DIGEST:
        if not isinstance(value, str) or not FINGERPRINT_PATTERN.fullmatch(value):
            return _ATTRIBUTE_TYPE_INVALID
    else:
        return _ATTRIBUTE_TYPE_INVALID
    if domain is not None:
        kind, bound = domain
        if kind == "vocabulary":
            if value not in bound:
                return _ATTRIBUTE_DOMAIN_INVALID
        else:
            minimum, maximum = bound
            if value < minimum or value > maximum:
                return _ATTRIBUTE_DOMAIN_INVALID
    return None


def validate_envelope_semantics(envelope: LocalAgentTraceEnvelopeInV1) -> None:
    """Validate every frozen envelope invariant; raise on first violation."""
    for value in (envelope.run_id, envelope.trace_id, envelope.span_id):
        if not isinstance(value, str) or not SAFE_IDENTIFIER_PATTERN.fullmatch(value):
            _reject(_INVALID_IDENTITY)
    if envelope.parent_span_id is not None and not SAFE_IDENTIFIER_PATTERN.fullmatch(envelope.parent_span_id):
        _reject(_INVALID_IDENTITY)
    if envelope.step_id is not None and not SAFE_IDENTIFIER_PATTERN.fullmatch(envelope.step_id):
        _reject(_INVALID_IDENTITY)
    if not isinstance(envelope.component, str) or not SAFE_IDENTIFIER_PATTERN.fullmatch(envelope.component):
        _reject(_INVALID_IDENTITY)
    if not isinstance(envelope.error_code, str) and envelope.error_code is not None:
        _reject(_INVALID_IDENTITY)
    if envelope.error_code is not None and not SAFE_IDENTIFIER_PATTERN.fullmatch(envelope.error_code):
        _reject(_INVALID_IDENTITY)

    operation_schema = STABLE_OPERATIONS.get(envelope.operation)
    if operation_schema is None:
        _reject(_UNSUPPORTED_OPERATION)
    category, step_bound = operation_schema
    if step_bound and envelope.step_id is None:
        _reject(_STEP_CORRELATION_MISSING)
    if not step_bound and envelope.step_id is not None:
        _reject(_STEP_CORRELATION_INVALID)

    started_at = envelope.started_at
    completed_at = envelope.completed_at
    if (
        not isinstance(started_at, datetime)
        or started_at.tzinfo is None
        or started_at.utcoffset() != timedelta(0)
        or not isinstance(completed_at, datetime)
        or completed_at.tzinfo is None
        or completed_at.utcoffset() != timedelta(0)
    ):
        _reject(_SPAN_TIME_INVALID)
    if completed_at < started_at:
        _reject(_SPAN_TIME_ORDER_INVALID)

    duration_ms = envelope.duration_ms
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, Decimal)
        or not duration_ms.is_finite()
        or duration_ms < 0
    ):
        _reject(_SPAN_DURATION_INVALID)

    if envelope.status == "OK":
        if envelope.error_code is not None:
            _reject(_ERROR_CODE_ON_OK)
    elif envelope.error_code is None:
        _reject(_ERROR_CODE_MISSING)

    attributes = envelope.attributes
    if not isinstance(attributes, dict):
        _reject(_ATTRIBUTES_NOT_MAPPING)
    schema = CATEGORY_ATTRIBUTE_SCHEMAS[category]
    domains = CATEGORY_ATTRIBUTE_DOMAINS[category]
    for key, value in attributes.items():
        if key not in schema:
            _reject(_UNKNOWN_ATTRIBUTE_KEY)
        type_, _presence = schema[key]
        if value is None:
            _reject(_ATTRIBUTE_VALUE_INVALID)
        code = _validate_attribute_value(value, type_, domains.get(key))
        if code is not None:
            _reject(code)


def _utc_iso(value: datetime) -> str:
    """Canonical UTC ISO-8601 rendering (same instant always same string)."""
    return value.astimezone(UTC).isoformat()


def canonical_number(value: Decimal) -> str:
    """Exact fixed-point canonical string, independent of Decimal context.

    Implements 65 §25 directly from ``Decimal.as_tuple()``: strip trailing
    coefficient zeros (adjusting the exponent) and render the exact fixed
    point without an exponent, without leading meaningless zeros and without
    any rounding.  ``normalize()`` / ``quantize()`` / float conversion are
    never used, so the current Decimal context precision cannot affect the
    output (verified under precisions 6/28/50/100).

    - zero (including ``0.0`` and ``-0.0``) -> ``"0"``
    - ``9007199254740992`` and ``9007199254740993`` stay distinct
    - the exact binary64 subnormal value renders with all significant digits
    """
    if value.is_zero():
        return "0"
    sign, digits, exponent = value.as_tuple()
    coefficient = list(digits)
    # Strip trailing coefficient zeros (this is what normalize() does, but
    # without the current context precision).
    while coefficient and coefficient[-1] == 0:
        coefficient.pop()
        exponent += 1
    if not coefficient:
        return "0"
    coef = "".join(str(d) for d in coefficient)
    if exponent >= 0:
        rendered = coef + "0" * exponent
    else:
        point = len(coef) + exponent
        if point > 0:
            rendered = coef[:point] + "." + coef[point:]
        else:
            rendered = "0." + "0" * (-point) + coef
    if sign:
        rendered = "-" + rendered
    if len(rendered) > CANONICAL_NUMBER_MAX_CHARS:  # pragma: no cover - domain-bounded
        raise ValueError("canonical number exceeds code-owned length bound")
    return rendered


def _canonical_json_scalar(value: object) -> str:
    """Canonical JSON rendering for one non-container digest value.

    ``str`` and ``bool`` are rendered exactly like ``json.dumps``; ``int`` is
    rendered exactly (large ints via ``Decimal`` fixed-point so Python's
    4300-digit int->str limit can never crash the digest — v1 integer
    attributes have no numeric upper bound); floats are canonicalized through
    :func:`canonical_number`.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, int):
        if value.bit_length() <= 14284:  # guaranteed <= 4300 decimal digits
            return str(value)
        return format(Decimal(value), "f")
    if isinstance(value, float):
        return canonical_number(Decimal.from_float(value))
    return json.dumps(value, ensure_ascii=True)


def _canonical_json_object(mapping: dict[str, object]) -> str:
    """Deterministic compact JSON object with safe exact numeric rendering."""
    parts = []
    for key in sorted(mapping):
        value = mapping[key]
        if isinstance(value, dict):
            encoded = _canonical_json_object(value)
        else:
            encoded = _canonical_json_scalar(value)
        parts.append(json.dumps(key, ensure_ascii=True) + ":" + encoded)
    return "{" + ",".join(parts) + "}"


def canonical_payload_digest(envelope: LocalAgentTraceEnvelopeInV1) -> str:
    """Return the deterministic SHA-256 of the canonical envelope payload.

    The digest covers every frozen DTO field (attributes included) and is
    independent of JSON key order, timezone spelling, integer/float number
    spelling and any server-local mutable field.  ``duration_ms`` uses the
    exact context-independent canonical value, so semantically distinct
    accepted envelopes can never collapse through float precision loss or
    Decimal-context rounding (P1-04).
    """
    payload = {
        "contract_identity": envelope.contract_identity,
        "contract_version": envelope.contract_version,
        "contract_fingerprint": envelope.contract_fingerprint,
        "run_id": envelope.run_id,
        "trace_id": envelope.trace_id,
        "span_id": envelope.span_id,
        "parent_span_id": envelope.parent_span_id,
        "step_id": envelope.step_id,
        "operation": envelope.operation,
        "component": envelope.component,
        "started_at": _utc_iso(envelope.started_at),
        "completed_at": _utc_iso(envelope.completed_at),
        "duration_ms": canonical_number(envelope.duration_ms),
        "status": envelope.status,
        "error_code": envelope.error_code,
        "attributes": dict(sorted(envelope.attributes.items())),
    }
    canonical = _canonical_json_object(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
