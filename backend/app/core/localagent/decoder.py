"""Code-owned strict JSON decoder for the LocalAgent compatibility envelope.

This is the ONE JSON decoding path used by the compatibility route (frozen
by 65 §21 and required by the R2/R3 tasks).  It parses the bounded raw
body exactly once and preserves the JSON numeric token category:

- integer tokens stay exact Python ``int`` (never converted through float);
- fraction/exponent tokens become Python ``float`` (binary64), exactly like
  the producer's stdlib serializer round-trips them;
- ``NaN`` / ``Infinity`` / ``-Infinity`` literals are rejected as invalid
  strict JSON (the producer never emits them);
- duplicate object keys are rejected (top level AND nested ``attributes``),
  so a document is never silently resolved with last-value-wins;
- integer tokens are converted by the code-owned exact parser
  :func:`_parse_json_integer_token`, which supports every producer-valid
  integer token inside the ``<=16384``-byte body bound (up to ~15901 digits
  in a real producer envelope) without depending on Python's default
  4300-digit int/str safety limit and without disabling it process-globally.

The returned dict is then validated by the strict DTO via
``model_validate(dict)`` — the raw request is never parsed twice with
different numeric semantics.  The DTO validator performs the per-category
domain check and the exact ``Decimal`` conversion:

- integer token -> exact int -> ``0 <= v <= MAX_V1_DURATION_INT`` ->
  ``Decimal(int)``
- fraction/exponent token -> binary64 float -> finite / non-negative ->
  ``Decimal.from_float(float_value)``
- negative zero -> canonical ``Decimal(0)``
"""

from __future__ import annotations

import json
from decimal import Decimal

# Content-free internal decode reason codes (log-only; the wire response is
# always the frozen ``LOCALAGENT_ENVELOPE_INVALID``).
_REASON_DUPLICATE_KEY = "DUPLICATE_JSON_KEY"
_REASON_JSON_CONSTANT = "INVALID_JSON_CONSTANT"
_REASON_NOT_OBJECT = "ENVELOPE_NOT_OBJECT"
_REASON_MALFORMED = "MALFORMED_JSON"
_REASON_NOT_UTF8 = "ENVELOPE_NOT_UTF8"
_REASON_INTEGER_TOKEN_TOO_LONG = "INTEGER_TOKEN_TOO_LONG"
_REASON_INTEGER_CONVERSION = "INTEGER_CONVERSION_FAILED"

# Maximum JSON integer token length accepted by the parser.  Derived from the
# code-owned 16384-byte envelope body bound: a raw integer token cannot be
# longer than the entire body it lives in, so any token longer than 16384
# characters can never appear inside a valid ``<=16384``-byte envelope.  This
# is a bounded-cost guard, NOT a semantic field bound — producer-valid
# attributes (up to ~15901 digits in a real serializer envelope) pass through.
_MAX_INT_TOKEN_DIGITS = 16384

# Python's default int<->str safety digit limit (sys.get_int_max_str_digits()).
# Tokens up to this length convert with the fast stdlib path; longer tokens
# use the exact Decimal path which is not subject to the limit.
_STR_SAFE_DIGITS = 4300


class EnvelopeDecodeError(Exception):
    """Bounded content-free JSON decode failure.

    ``reason`` is an internal fixed code used for structured logging only;
    it never appears on the wire.
    """

    def __init__(self, reason: str) -> None:
        """Store the internal fixed reason code."""
        self.reason = reason
        super().__init__(reason)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``object_pairs_hook`` that fails closed on any duplicate JSON key."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EnvelopeDecodeError(_REASON_DUPLICATE_KEY)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    """``parse_constant`` hook: strict JSON has no NaN/Infinity literals."""
    raise EnvelopeDecodeError(_REASON_JSON_CONSTANT)


def _parse_json_integer_token(token: str) -> int:
    """Exact bounded conversion of one raw JSON integer token to ``int``.

    This is the code-owned ``parse_int`` hook.  It owns ONLY the exact
    bounded decimal token conversion:

    - the token length is bounded by the 16384-byte body contract (any
      longer token cannot fit a valid envelope and is rejected cheaply);
    - tokens up to 4300 digits use the fast stdlib ``int(token)`` path;
    - longer tokens (4301 .. 16384) use ``int(Decimal(token))``, which is
      exact and is NOT subject to Python's default int/string digit limit —
      so producer-valid ``NON_NEGATIVE_INT`` attributes serialize exactly
      without any process-global ``sys.set_int_max_str_digits`` change;
    - never converts through ``float`` and never accepts non-integer JSON.

    Downstream semantic Owners (duration range, attribute domain, canonical
    digest, DB persistence) are deliberately not decided here.
    """
    if len(token) > _MAX_INT_TOKEN_DIGITS:
        raise EnvelopeDecodeError(_REASON_INTEGER_TOKEN_TOO_LONG)
    try:
        if len(token) <= _STR_SAFE_DIGITS:
            return int(token)
        return int(Decimal(token))
    except EnvelopeDecodeError:
        raise
    except Exception as exc:
        raise EnvelopeDecodeError(_REASON_INTEGER_CONVERSION) from exc


def decode_envelope_body(raw: bytes) -> dict[str, object]:
    """Decode the bounded raw envelope body exactly once.

    Numeric token category is preserved (int vs float); integer tokens use
    the exact bounded parser; duplicate keys and non-strict JSON constants
    fail closed.  Malformed JSON, wrong UTF-8, a non-object document, an
    over-length integer token or any unexpected conversion failure all raise
    :class:`EnvelopeDecodeError` — no raw ``ValueError``/``OverflowError``
    ever escapes to a 500.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvelopeDecodeError(_REASON_NOT_UTF8) from exc
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
            parse_int=_parse_json_integer_token,
        )
    except EnvelopeDecodeError:
        raise
    except json.JSONDecodeError as exc:
        raise EnvelopeDecodeError(_REASON_MALFORMED) from exc
    if not isinstance(data, dict):
        raise EnvelopeDecodeError(_REASON_NOT_OBJECT)
    return data


# ---------------------------------------------------------------------------
# Exact JSON rendering for the JSONB sidecar column (P1-07)
# ---------------------------------------------------------------------------
#
# PostgreSQL JSONB stores the exact numeric value of a JSON integer (verified:
# a 5000-digit integer is persisted and returned losslessly), but both the
# default ``json.dumps`` (write) and ``json.loads`` (readback) hit Python's
# 4300-digit int/string limit.  The compatibility sidecar column therefore
# uses these exact serializer/deserializer functions so producer-valid huge
# integer attributes survive the full decode -> validate -> persist -> fresh
# readback pipeline.


def exact_json_dumps(value: object) -> str:
    """Render a JSON-compatible value with exact large-integer support.

    Identical to ``json.dumps`` for every ordinary value; integers with more
    than 4300 digits are rendered exactly through ``Decimal`` fixed point so
    Python's int/string safety limit cannot truncate or crash the output.
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, int):
        if value.bit_length() <= 14284:  # guaranteed <= 4300 decimal digits
            return str(value)
        return format(Decimal(value), "f")
    if isinstance(value, float):
        return json.dumps(value, allow_nan=False)
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            parts.append(json.dumps(str(key), ensure_ascii=True) + ":" + exact_json_dumps(value[key]))
        return "{" + ",".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(exact_json_dumps(item) for item in value) + "]"
    raise TypeError(f"cannot serialize {type(value).__name__} exactly")


def exact_json_loads(text: str) -> object:
    """Parse JSON text with the exact bounded integer token parser."""
    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_json_constant,
        parse_int=_parse_json_integer_token,
    )
