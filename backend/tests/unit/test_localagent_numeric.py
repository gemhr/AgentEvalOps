"""R2 numeric-contract unit tests for the LocalAgent compatibility boundary.

Covers the frozen producer numeric domain (65/75): the token-aware decoder
(single parse owner, duplicate-key rejection, int/float category
preservation), exact int/binary64 float parsing, negative zero, MAX-V1
domain bounds, context-independent canonicalization, collision/convergence
matrices and the lossless NUMERIC ORM type.  The golden producer wire
fixtures are consumed from :mod:`localagent_producer_wire_fixtures`.
"""

# ruff: noqa: D415

import json
import sys
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError
from sqlalchemy import Numeric

from app.core.localagent.contract import MAX_V1_DURATION_INT
from app.core.localagent.decoder import EnvelopeDecodeError, decode_envelope_body
from app.core.localagent.entities import LocalAgentTraceEnvelopeInV1
from app.core.localagent.validation import canonical_number, canonical_payload_digest, validate_envelope_semantics
from app.infrastructure.db.models import LocalAgentTraceEnvelopeSidecarModel

from .localagent_producer_wire_fixtures import (
    VERIFIED_PRODUCER_SEMANTIC_VALUES,
    VERIFIED_PRODUCER_WIRE_FIXTURE,
)

FINGERPRINT = "6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab"


def valid_payload(**overrides: object) -> dict[str, object]:
    """Fully valid frozen envelope payload (runtime.step, OK)."""
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
        "attributes": {"execution_kind": "AGENT"},
    }
    payload.update(overrides)
    return payload


def raw_with_duration(token: str) -> bytes:
    """Serialize a valid envelope with the given RAW (unquoted) duration token."""
    body = json.dumps(valid_payload(), sort_keys=True, separators=(",", ":"))
    body = body.replace('"duration_ms":5000', f'"duration_ms":{token}')
    assert f'"duration_ms":{token}' in body
    return body.encode("utf-8")


def envelope_from_raw(raw: bytes) -> LocalAgentTraceEnvelopeInV1:
    parsed = decode_envelope_body(raw)
    return LocalAgentTraceEnvelopeInV1.model_validate(parsed)


def envelope_with_duration_token(token: str) -> LocalAgentTraceEnvelopeInV1:
    return envelope_from_raw(raw_with_duration(token))


# ---------------------------------------------------------------------------
# Token-aware decoder
# ---------------------------------------------------------------------------


def test_decoder_preserves_int_vs_float_category():
    parsed_int = decode_envelope_body(raw_with_duration("9007199254740993"))
    parsed_float = decode_envelope_body(raw_with_duration("1.5"))
    assert isinstance(parsed_int["duration_ms"], int)
    assert isinstance(parsed_float["duration_ms"], float)
    assert parsed_int["duration_ms"] == 2**53 + 1  # never converted through float


def test_decoder_rejects_duplicate_top_level_key():
    body = json.dumps(valid_payload(), sort_keys=True, separators=(",", ":"))
    body = body.replace('"duration_ms":5000', '"duration_ms":1,"duration_ms":2')
    with pytest.raises(EnvelopeDecodeError):
        decode_envelope_body(body.encode("utf-8"))


def test_decoder_rejects_duplicate_nested_attribute_key():
    body = json.dumps(
        valid_payload(attributes={"execution_kind": "AGENT"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    body = body.replace('"execution_kind":"AGENT"', '"execution_kind":"AGENT","execution_kind":"SYNTHESIS"')
    with pytest.raises(EnvelopeDecodeError):
        decode_envelope_body(body.encode("utf-8"))


def test_decoder_rejects_non_strict_json_constants():
    for token in ("NaN", "Infinity", "-Infinity"):
        raw = raw_with_duration(token)
        with pytest.raises(EnvelopeDecodeError):
            decode_envelope_body(raw)


def test_decoder_rejects_malformed_json():
    with pytest.raises(EnvelopeDecodeError):
        decode_envelope_body(b"{not json")


def test_decoder_rejects_non_object_document():
    with pytest.raises(EnvelopeDecodeError):
        decode_envelope_body(b"[1,2,3]")


# ---------------------------------------------------------------------------
# Frozen domain (P1-03)
# ---------------------------------------------------------------------------


def test_max_v1_duration_int_value():
    assert MAX_V1_DURATION_INT == 2**1024 - 2**970 - 1
    assert len(str(MAX_V1_DURATION_INT)) == 309


def test_two_53_plus_1_accepted_exact():
    envelope = envelope_with_duration_token(VERIFIED_PRODUCER_WIRE_FIXTURE["two_53_plus_1"])
    validate_envelope_semantics(envelope)
    assert envelope.duration_ms == Decimal(2**53 + 1)


def test_max_int_accepted_exact():
    envelope = envelope_with_duration_token(VERIFIED_PRODUCER_WIRE_FIXTURE["max_int"])
    validate_envelope_semantics(envelope)
    assert envelope.duration_ms == Decimal(MAX_V1_DURATION_INT)
    assert envelope.duration_ms == VERIFIED_PRODUCER_SEMANTIC_VALUES["max_int"]


@pytest.mark.parametrize("token", ["179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497792", str(10**309), str(10**1000)])
def test_integer_above_max_rejected(token: str):
    with pytest.raises(ValidationError):
        envelope_with_duration_token(token)


def test_huge_exponent_float_token_rejected_cheaply():
    # 1e1000000 -> binary64 overflow (inf) -> rejected; no giant Decimal.
    with pytest.raises(ValidationError):
        envelope_with_duration_token("1e1000000")
    with pytest.raises(ValidationError):
        envelope_with_duration_token("1e1000000000")


def test_negative_nonzero_rejected():
    with pytest.raises(ValidationError):
        envelope_with_duration_token("-1")
    with pytest.raises(ValidationError):
        envelope_with_duration_token("-0.5")


def test_duration_string_bool_null_rejected():
    for token in ('"1.5"', "true", "false", "null"):
        with pytest.raises((EnvelopeDecodeError, ValidationError)):
            envelope_with_duration_token(token)


def test_float_max_and_subnormal_accepted():
    envelope = envelope_with_duration_token(VERIFIED_PRODUCER_WIRE_FIXTURE["float_max"])
    validate_envelope_semantics(envelope)
    assert envelope.duration_ms == VERIFIED_PRODUCER_SEMANTIC_VALUES["float_max"]

    subnormal = envelope_with_duration_token(VERIFIED_PRODUCER_WIRE_FIXTURE["min_subnormal"])
    validate_envelope_semantics(subnormal)
    assert subnormal.duration_ms == VERIFIED_PRODUCER_SEMANTIC_VALUES["min_subnormal"]


def test_float_semantic_value_is_exact_binary64():
    # 0.1 parses to the exact binary64 value, not the shortest spelling.
    envelope = envelope_with_duration_token("0.1")
    assert envelope.duration_ms == Decimal.from_float(0.1)
    assert envelope.duration_ms != Decimal("0.1")


def test_negative_zero_normalizes_to_zero():
    for token in ("-0.0", "0.0", "0"):
        envelope = envelope_with_duration_token(token)
        assert envelope.duration_ms == Decimal(0)
        assert canonical_number(envelope.duration_ms) == "0"


# ---------------------------------------------------------------------------
# Golden producer wire fixtures (P1-03 parity)
# ---------------------------------------------------------------------------


def test_golden_producer_wire_fixtures_parse_to_exact_semantic_values():
    for name, token in VERIFIED_PRODUCER_WIRE_FIXTURE.items():
        envelope = envelope_with_duration_token(token)
        validate_envelope_semantics(envelope)
        assert envelope.duration_ms == VERIFIED_PRODUCER_SEMANTIC_VALUES[name], name
        assert canonical_number(envelope.duration_ms) == canonical_number(
            VERIFIED_PRODUCER_SEMANTIC_VALUES[name]
        ), name


# ---------------------------------------------------------------------------
# Canonicalization (P1-04)
# ---------------------------------------------------------------------------


def test_canonical_number_context_independent():
    fixtures = [
        Decimal(0),
        Decimal(2**53 + 1),
        Decimal(MAX_V1_DURATION_INT),
        Decimal.from_float(sys.float_info.max),
        Decimal.from_float(float.fromhex("0x0.0000000000001p-1022")),
        Decimal.from_float(0.1),
        Decimal("1.5"),
        Decimal("1E+3"),
    ]
    baseline = [canonical_number(v) for v in fixtures]
    for precision in (6, 28, 50, 100):
        with localcontext() as ctx:
            ctx.prec = precision
            for value, expected in zip(fixtures, baseline, strict=True):
                assert canonical_number(value) == expected


def test_canonical_number_rendering_rules():
    assert canonical_number(Decimal(0)) == "0"
    assert canonical_number(Decimal("0.0")) == "0"
    assert canonical_number(Decimal("-0.0")) == "0"
    assert canonical_number(Decimal("1.50")) == "1.5"
    assert canonical_number(Decimal("1E+3")) == "1000"
    assert canonical_number(Decimal("1000")) == "1000"
    assert canonical_number(Decimal("0.001")) == "0.001"
    assert canonical_number(Decimal("1E-3")) == "0.001"
    assert canonical_number(Decimal(2**53 + 1)) == str(2**53 + 1)
    assert canonical_number(Decimal(MAX_V1_DURATION_INT)) == str(MAX_V1_DURATION_INT)


def test_canonical_length_bound():
    for name, value in VERIFIED_PRODUCER_SEMANTIC_VALUES.items():
        rendered = canonical_number(value)
        assert len(rendered) <= 1076, (name, len(rendered))
    # Worst case: exact minimum positive subnormal (scale 1074).
    assert len(canonical_number(VERIFIED_PRODUCER_SEMANTIC_VALUES["min_subnormal"])) <= 1076


def test_collision_matrix():
    pairs = [
        (Decimal(10**100 + 1), Decimal(10**100 + 2)),
        (Decimal(10**308), Decimal(10**308 + 1)),
        (Decimal(2**53), Decimal(2**53 + 1)),
    ]
    for a, b in pairs:
        assert canonical_number(a) != canonical_number(b)
        assert canonical_payload_digest(_envelope_for(a)) != canonical_payload_digest(_envelope_for(b))


def test_semantic_convergence():
    groups = [
        [Decimal(0), Decimal("0.0"), Decimal("-0.0")],
        [Decimal(1), Decimal("1.0"), Decimal.from_float(1.0)],
        [Decimal("1.5"), Decimal("1.50"), Decimal.from_float(1.5)],
    ]
    for group in groups:
        canonical = {canonical_number(v) for v in group}
        assert len(canonical) == 1
        digests = {canonical_payload_digest(_envelope_for(v)) for v in group}
        assert len(digests) == 1


def _envelope_for(duration: Decimal) -> LocalAgentTraceEnvelopeInV1:
    return LocalAgentTraceEnvelopeInV1.model_validate(valid_payload(duration_ms=duration))


def test_digest_large_attribute_integer_does_not_crash():
    """v1 integer attributes are unbounded; >4300-digit ints must not crash the digest."""
    huge = 10**5000 + 123
    envelope = LocalAgentTraceEnvelopeInV1.model_validate(
        valid_payload(
            operation="runtime.run",
            step_id=None,
            attributes={"plan_version": huge},
        )
    )
    validate_envelope_semantics(envelope)
    digest = canonical_payload_digest(envelope)
    assert len(digest) == 64
    assert canonical_payload_digest(envelope) == digest  # deterministic


# ---------------------------------------------------------------------------
# NUMERIC ORM type (P1-06)
# ---------------------------------------------------------------------------


def test_sidecar_duration_orm_is_numeric():
    column = LocalAgentTraceEnvelopeSidecarModel.__table__.c.duration_ms
    assert isinstance(column.type, Numeric)
    assert column.type.precision is None  # unbounded, no truncating precision
    assert column.type.scale is None
    assert not column.nullable
