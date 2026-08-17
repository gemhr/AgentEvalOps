"""R3 large-integer decoder unit tests.

Covers the code-owned exact integer token parser (no reliance on Python's
default 4300-digit int/string limit, no process-global limit change), the
decoder failure boundary (every parser-level failure maps to
``EnvelopeDecodeError``), large-integer digest totality and the exact
JSONB serializer/deserializer round-trip used by the sidecar column.
"""

# ruff: noqa: D415

import json
import sys
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.localagent.decoder import (
    EnvelopeDecodeError,
    _parse_json_integer_token,
    decode_envelope_body,
    exact_json_dumps,
    exact_json_loads,
)
from app.core.localagent.entities import LocalAgentTraceEnvelopeInV1
from app.core.localagent.validation import canonical_payload_digest, validate_envelope_semantics

from .localagent_producer_wire_fixtures import (
    VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE,
    VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES,
)

FINGERPRINT = "6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab"


def valid_payload(**overrides: object) -> dict[str, object]:
    """Valid frozen envelope payload (runtime.run category for plan_version)."""
    payload: dict[str, object] = {
        "contract_identity": "localagent.runtime.trace_export",
        "contract_version": 1,
        "contract_fingerprint": FINGERPRINT,
        "run_id": "run-123",
        "trace_id": "trace-abc",
        "span_id": "span-xyz",
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
    payload.update(overrides)
    return payload


def raw_envelope(attribute_token: str) -> bytes:
    """Raw envelope bytes with the given UNQUOTED plan_version token."""
    encoded = json.dumps(valid_payload(), sort_keys=True, separators=(",", ":"))
    encoded = encoded.replace('"plan_version":1', f'"plan_version":{attribute_token}')
    assert f'"plan_version":{attribute_token}' in encoded
    return encoded.encode("utf-8")


# ---------------------------------------------------------------------------
# Code-owned exact integer token parser
# ---------------------------------------------------------------------------


def test_parse_int_exact_boundary_and_beyond():
    assert _parse_json_integer_token("9" * 4300) == 10**4300 - 1
    assert _parse_json_integer_token("9" * 4301) == 10**4301 - 1
    assert _parse_json_integer_token("9" * 10000) == 10**10000 - 1
    assert _parse_json_integer_token("0") == 0
    assert _parse_json_integer_token("1") == 1
    assert _parse_json_integer_token("-123") == -123


def test_parse_int_never_converts_through_float():
    token = _parse_json_integer_token("9" * 5000)
    assert isinstance(token, int)
    assert token == 10**5000 - 1


def test_parse_int_rejects_out_of_body_contract_token():
    with pytest.raises(EnvelopeDecodeError):
        _parse_json_integer_token("9" * 20000)


def test_global_int_digit_limit_unchanged():
    before = sys.get_int_max_str_digits()
    assert before == 4300
    _parse_json_integer_token("9" * 10000)
    assert sys.get_int_max_str_digits() == 4300  # no process-global relaxation


# ---------------------------------------------------------------------------
# Decoder failure boundary
# ---------------------------------------------------------------------------


def test_decoder_rejects_leading_zero_invalid_json():
    with pytest.raises(EnvelopeDecodeError):
        decode_envelope_body(b'{"a":01}')
    with pytest.raises(EnvelopeDecodeError):
        decode_envelope_body(b'{"a":00}')


def test_decoder_translates_value_errors_to_envelope_decode_error():
    # A >4300-digit integer token previously escaped as raw ValueError -> 500;
    # now it must decode exactly (producer-valid) with no raw error.
    parsed = decode_envelope_body(raw_envelope(VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE["attr_5000"]))
    assert parsed["attributes"]["plan_version"] == VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES["attr_5000"]


@pytest.mark.parametrize("name", ["attr_4301", "attr_5000", "attr_10000", "attr_15901_near_max"])
def test_decoder_accepts_producer_valid_large_attributes(name: str):
    parsed = decode_envelope_body(raw_envelope(VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE[name]))
    value = parsed["attributes"]["plan_version"]
    assert isinstance(value, int)
    assert value == VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES[name]


def test_large_attribute_full_pipeline_digest():
    """A producer-valid >4300-digit attribute reaches the canonical digest cleanly."""
    envelope = LocalAgentTraceEnvelopeInV1.model_validate(
        decode_envelope_body(raw_envelope(VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE["attr_5000"]))
    )
    validate_envelope_semantics(envelope)
    digest = canonical_payload_digest(envelope)
    assert len(digest) == 64
    assert canonical_payload_digest(envelope) == digest


def test_large_attribute_digest_n_and_n_plus_1_distinct():
    base = VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES["attr_5000"]
    a = LocalAgentTraceEnvelopeInV1.model_validate(
        decode_envelope_body(raw_envelope(exact_json_dumps(base)))
    )
    b = LocalAgentTraceEnvelopeInV1.model_validate(
        decode_envelope_body(raw_envelope(exact_json_dumps(base + 1)))
    )
    assert a.attributes["plan_version"] != b.attributes["plan_version"]
    assert canonical_payload_digest(a) != canonical_payload_digest(b)


def test_large_duration_token_rejected_by_duration_owner_not_parser():
    """A 4301-digit duration is contract-invalid; the semantic Owner rejects it."""
    encoded = json.dumps(valid_payload(), sort_keys=True, separators=(",", ":"))
    encoded = encoded.replace('"duration_ms":5000', f'"duration_ms":{"9" * 4301}')
    with pytest.raises(ValidationError):
        LocalAgentTraceEnvelopeInV1.model_validate(decode_envelope_body(encoded.encode("utf-8")))


# ---------------------------------------------------------------------------
# Exact JSONB serializer/deserializer round-trip
# ---------------------------------------------------------------------------


def test_exact_json_round_trip_large_integer():
    value = 10**5000 + 7
    rendered = exact_json_dumps({"k": value, "s": "x", "b": True})
    parsed = exact_json_loads(rendered)
    assert parsed == {"k": value, "s": "x", "b": True}


def test_exact_json_matches_json_dumps_for_ordinary_values():
    obj = {"a": 123, "b": "x", "c": None, "d": True, "e": -5}
    assert exact_json_loads(exact_json_dumps(obj)) == obj
    # Compact form identical to stdlib for ordinary values.
    assert exact_json_dumps({"a": 123}) == '{"a":123}'


def test_exact_json_rejects_nan():
    with pytest.raises(ValueError):
        exact_json_dumps(float("nan"))
