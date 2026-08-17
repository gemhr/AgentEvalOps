"""Column-local exact JSONB bridge for the LocalAgent sidecar attributes.

Approved by 120 (``OPTION_B_COLUMN_LOCAL_CODEC``): the shared SQLAlchemy
engine keeps SQLAlchemy/asyncpg DEFAULT JSON/JSONB semantics, and ONLY
``LocalAgentTraceEnvelopeSidecarModel.attributes`` uses this decorator.

Why a column-local type is required (R3 P1-07 closure, R4 narrow scope):
producer-valid ``NON_NEGATIVE_INT`` attributes may be >4300 decimal digits,
which the default ``json.dumps`` (write) and the connection-level asyncpg
``json.loads`` (readback) both reject.  PostgreSQL JSONB itself stores the
exact numeric value, so this decorator bridges only that one column:

- bind: the validated attributes dict is rendered by the exact codec
  (``exact_json_dumps``) straight to JSON text and bound as JSONB.  The
  ``bind_processor`` override is deliberate: going through the inner JSONB
  generic serializer would double-serialize (JSON text -> JSON string).
- read: every mapped select compiles ``CAST(attributes AS TEXT)``
  (``column_expression``), so asyncpg returns a plain ``str`` and the
  connection-level JSONB decoder is bypassed before the exact large-int
  parser (``exact_json_loads``) reconstructs the Python ``dict``/``int``.

This is a LocalAgent compatibility owner; it is NOT a generic application
"ExactJSONB" utility and must never be applied to unrelated JSONB columns.
"""

from __future__ import annotations

from sqlalchemy import Text, cast, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

from app.core.localagent.decoder import exact_json_dumps, exact_json_loads


class LocalAgentAttributesJSONB(TypeDecorator):
    """Exact huge-integer JSONB bridge for the LocalAgent sidecar attributes column."""

    impl = JSONB
    cache_ok = True

    def bind_processor(self, dialect):
        """Render the validated attributes dict directly as exact JSON text.

        Returning the codec itself (not ``process_bind_param``) avoids the
        inner JSONB generic ``json.dumps`` re-serializing the JSON text into
        a JSON string; the returned text is bound as a real JSONB value.
        """
        return exact_json_dumps

    def result_processor(self, dialect, coltype):
        """Reconstruct the exact Python object from the CAST(AS TEXT) value."""
        return exact_json_loads

    def column_expression(self, column):
        """Select the column as TEXT so the driver JSONB decoder is bypassed.

        asyncpg's connection-level JSONB decoder runs ``json.loads`` and
        would hit Python's 4300-digit int/string limit before this decorator
        could act; casting to TEXT hands the exact parser the raw JSON text.
        """
        return type_coerce(cast(column, Text), self)
