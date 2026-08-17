"""VERIFIED_PRODUCER_WIRE_FIXTURE — exact LocalAgent serializer wire tokens.

These are the literal JSON number tokens emitted by the LocalAgent producer
serializer (``core/runtime/trace_export_serialization.py``), verified by
``75_codex_trace_export_serializer_gate.md`` and re-captured here from the
serializer's actual UTF-8 bytes for each frozen edge value.

Do NOT hand-edit these token strings — they are producer wire evidence, not
arbitrary test inputs.
"""

from __future__ import annotations

import sys
from decimal import Decimal

VERIFIED_PRODUCER_WIRE_FIXTURE: dict[str, str] = {
    "zero": "0",
    "negative_zero": "-0.0",
    "one": "1",
    "one_float": "1.0",
    "one_point_five": "1.5",
    "two_53": "9007199254740992",
    "two_53_plus_1": "9007199254740993",
    "ten_100_plus_1": "10000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001",
    "ten_308_plus_1": (
        "100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "000000000001"
    ),
    "max_int": (
        "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096"
        "330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730"
        "270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497"
        "791"
    ),
    "float_max": "1.7976931348623157e+308",
    "min_subnormal": "5e-324",
}

# The exact semantic Decimal the consumer must reconstruct for each fixture.
VERIFIED_PRODUCER_SEMANTIC_VALUES: dict[str, Decimal] = {
    "zero": Decimal(0),
    "negative_zero": Decimal(0),
    "one": Decimal(1),
    "one_float": Decimal(1),
    "one_point_five": Decimal("1.5"),
    "two_53": Decimal(2**53),
    "two_53_plus_1": Decimal(2**53 + 1),
    "ten_100_plus_1": Decimal(10**100 + 1),
    "ten_308_plus_1": Decimal(10**308 + 1),
    "max_int": Decimal(2**1024 - 2**970 - 1),
    "float_max": Decimal.from_float(sys.float_info.max),
    "min_subnormal": Decimal.from_float(float.fromhex("0x0.0000000000001p-1022")),
}

# ---------------------------------------------------------------------------
# VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE — exact serializer tokens for
# producer-valid NON_NEGATIVE_INT ``plan_version`` attributes that exceed
# Python's default 4300-digit int/string safety limit.
#
# Captured by executing the real LocalAgent serializer
# (``Local_Agent/core/runtime/trace_export_serialization.py``, Python 3.12.6)
# with ``attributes={"plan_version": 10**digits + 12345}`` and extracting the
# emitted JSON number token from the serializer bytes.  Every token was
# verified byte-for-byte against ``format(Decimal(10**digits + 12345), 'f')``.
#
# Generation command (recorded source evidence):
#   uv run python -c "... serialize_trace_export_envelope(envelope with
#   attributes={'plan_version': 10**digits + 12345}) ..."
#
# Measured total serializer payloads (all <= 16384 bytes):
#   digits=4301  -> token 4302 chars, envelope 4776 bytes
#   digits=5000  -> token 5001 chars, envelope 5475 bytes
#   digits=10000 -> token 10001 chars, envelope 10475 bytes
#   digits=15901 -> token 15902 chars, envelope 16376 bytes (near-max)
# ---------------------------------------------------------------------------
VERIFIED_PRODUCER_LARGE_ATTRIBUTE_DIGITS = {
    "attr_4301": 4301,
    "attr_5000": 5000,
    "attr_10000": 10000,
    "attr_15901_near_max": 15901,
}

VERIFIED_PRODUCER_LARGE_ATTRIBUTE_FIXTURE: dict[str, str] = {
    name: "1" + "0" * (digits - 5) + "12345" for name, digits in VERIFIED_PRODUCER_LARGE_ATTRIBUTE_DIGITS.items()
}

VERIFIED_PRODUCER_LARGE_ATTRIBUTE_VALUES: dict[str, int] = {
    name: 10**digits + 12345 for name, digits in VERIFIED_PRODUCER_LARGE_ATTRIBUTE_DIGITS.items()
}

