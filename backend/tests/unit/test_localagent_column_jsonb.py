"""R4 column-local JSONB decorator unit tests.

Covers ``LocalAgentAttributesJSONB`` (120 OPTION_B_COLUMN_LOCAL_CODEC):
- it is a compatibility-specific ``TypeDecorator(JSONB)`` whose bind processor
  renders exact JSON text (never double-serializing through the inner JSONB
  generic serializer) and whose result processor reconstructs exact Python
  objects via the code-owned large-int parser;
- every mapped select / DML RETURNING compiles ``CAST(attributes AS TEXT)`` so
  the connection-level asyncpg JSONB decoder (default ``json.loads``, 4300-digit
  int limit) is bypassed BEFORE the exact decoder runs (driver-decoder bypass);
- exactly ONE mapped column in the whole schema uses the decorator (the sidecar
  ``attributes`` column), while the shared engine has NO global JSON codec
  (pre-R3 default semantics restored).
"""

# ruff: noqa: D415

import ast
import sys
from pathlib import Path

import pytest
from sqlalchemy import insert, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from sqlalchemy.types import TypeDecorator

from app.core.localagent.decoder import exact_json_dumps, exact_json_loads
from app.infrastructure.db.engine import engine as shared_engine
from app.infrastructure.db.models import Base, LocalAgentTraceEnvelopeSidecarModel
from app.infrastructure.db.types.localagent_jsonb import LocalAgentAttributesJSONB

_ENGINE_PATH = Path(__file__).resolve().parents[2] / "app" / "infrastructure" / "db" / "engine.py"

_ATTRS_COLUMN = LocalAgentTraceEnvelopeSidecarModel.attributes.property.columns[0]

HUGE = 10**5000 + 12345
HUGE_TOKEN = "1" + "0" * 4995 + "12345"


# ---------------------------------------------------------------------------
# Type shape
# ---------------------------------------------------------------------------


def test_type_is_compatibility_named_type_decorator():
    assert issubclass(LocalAgentAttributesJSONB, TypeDecorator)
    assert LocalAgentAttributesJSONB.__name__ == "LocalAgentAttributesJSONB"
    # The inner impl must remain JSONB so the physical column stays jsonb.
    assert LocalAgentAttributesJSONB.impl is postgresql.JSONB
    assert LocalAgentAttributesJSONB.cache_ok is True
    # Ownership must be explicit: the type lives in a LocalAgent-named module.
    assert "localagent" in LocalAgentAttributesJSONB.__module__


def test_single_mapped_column_usage_in_entire_schema():
    """Only LocalAgentTraceEnvelopeSidecarModel.attributes uses the decorator."""
    hits: list[tuple[str, str]] = []
    for table in Base.metadata.tables.values():
        for column_ in table.columns:
            if isinstance(column_.type, LocalAgentAttributesJSONB):
                hits.append((table.name, column_.name))
    assert hits == [("localagent_trace_envelope_sidecars", "attributes")]


def test_physical_ddl_remains_jsonb():
    ddl = str(CreateTable(LocalAgentTraceEnvelopeSidecarModel.__table__).compile(dialect=postgresql.dialect()))
    line = next(line.strip() for line in ddl.splitlines() if line.strip().startswith("attributes"))
    assert "JSONB" in line.upper()
    assert "TEXT" not in line.upper()


# ---------------------------------------------------------------------------
# Bind processor: direct exact JSON text, no double serialization
# ---------------------------------------------------------------------------


def test_bind_processor_is_the_exact_codec():
    assert LocalAgentAttributesJSONB().bind_processor(postgresql.dialect()) is exact_json_dumps


def test_bind_produces_unquoted_exact_numeric_token():
    processor = LocalAgentAttributesJSONB().bind_processor(postgresql.dialect())
    bound = processor({"plan_version": HUGE, "flag": True, "label": "x"})
    assert isinstance(bound, str)
    assert bound.startswith("{") and bound.endswith("}")
    # The huge integer must be an UNQUOTED JSON number, never a JSON string.
    assert f'"plan_version":{HUGE_TOKEN}' in bound
    assert '"plan_version":"' not in bound


def test_bind_processor_no_double_serialization():
    """The bound value is JSON text; re-encoding it must NOT produce a string."""
    processor = LocalAgentAttributesJSONB().bind_processor(postgresql.dialect())
    bound = processor({"plan_version": HUGE})
    # If the value had been run through a generic json.dumps again it would be
    # a quoted string; the direct processor output must stay an object.  The
    # exact codec loader is used because stdlib json.loads hits Python's
    # 4300-digit int/string limit on the huge token.
    assert exact_json_loads(bound) == {"plan_version": HUGE}
    assert not bound.startswith('"{')
    assert not bound.endswith('}"')


def test_bind_rejects_non_json_values_through_codec():
    processor = LocalAgentAttributesJSONB().bind_processor(postgresql.dialect())
    with pytest.raises(TypeError):
        processor({"x": object()})


# ---------------------------------------------------------------------------
# Result processor / column expression (driver-decoder bypass)
# ---------------------------------------------------------------------------


def test_result_processor_is_the_exact_codec():
    assert LocalAgentAttributesJSONB().result_processor(postgresql.dialect(), None) is exact_json_loads


def test_result_processor_round_trips_huge_int():
    processor = LocalAgentAttributesJSONB().result_processor(postgresql.dialect(), None)
    loaded = processor(exact_json_dumps({"plan_version": HUGE, "flag": True}))
    assert loaded == {"plan_version": HUGE, "flag": True}
    assert isinstance(loaded["plan_version"], int)


def test_column_expression_casts_to_text():
    sql = str(
        select(LocalAgentTraceEnvelopeSidecarModel.attributes)
        .where(LocalAgentTraceEnvelopeSidecarModel.external_span_id == "x")
        .compile(dialect=postgresql.dialect())
    )
    assert "CAST(localagent_trace_envelope_sidecars.attributes AS TEXT)" in sql


def test_dml_returning_compiles_same_cast():
    """DML RETURNING must go through the same TEXT cast (driver bypass)."""
    now = "2026-01-01T00:00:00"
    stmt = (
        insert(LocalAgentTraceEnvelopeSidecarModel.__table__)
        .values(
            external_run_id="r",
            external_trace_id="t",
            external_span_id="s",
            operation="o",
            component="c",
            started_at=now,
            completed_at=now,
            status="OK",
            contract_identity="i",
            contract_version=1,
            contract_fingerprint="f",
            canonical_payload_digest="d",
            internal_trace_uuid="00000000-0000-4000-a000-000000000001",
            internal_span_uuid="00000000-0000-4000-a000-000000000002",
            attributes={"plan_version": 1},
        )
        .returning(LocalAgentTraceEnvelopeSidecarModel.attributes)
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "CAST(localagent_trace_envelope_sidecars.attributes AS TEXT)" in sql


def test_update_set_clause_keeps_plain_bind_path():
    """UPDATE SET binds through the decorator (no cast in the SET clause)."""
    stmt = (
        LocalAgentTraceEnvelopeSidecarModel.__table__.update()
        .where(LocalAgentTraceEnvelopeSidecarModel.external_span_id == "s")
        .values(attributes={"plan_version": 1})
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "CAST" not in sql
    assert "attributes" in sql


# ---------------------------------------------------------------------------
# Shared engine: pre-R3 default JSON semantics (no global codec)
# ---------------------------------------------------------------------------


def test_engine_has_no_global_json_codec_arguments():
    """Source/AST proof: create_async_engine(...) gets NO json serializer args."""
    source = _ENGINE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "create_async_engine":
            for keyword in node.keywords:
                assert keyword.arg not in {"json_serializer", "json_deserializer"}, keyword.arg


def test_engine_source_has_no_localagent_decoder_imports():
    """engine.py must not import the compatibility exact codec for global use."""
    source = _ENGINE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.name)
    assert "exact_json_dumps" not in imported_names
    assert "exact_json_loads" not in imported_names
    assert "decoder" not in imported_names


def test_shared_engine_dialect_has_default_json_serializers():
    """Runtime proof: the shared async engine uses the default codec config."""
    assert getattr(shared_engine.dialect, "_json_serializer", None) is None
    assert getattr(shared_engine.dialect, "_json_deserializer", None) is None


def test_global_int_digit_limit_still_unchanged():
    assert sys.get_int_max_str_digits() == 4300
    LocalAgentAttributesJSONB().bind_processor(postgresql.dialect())({"plan_version": HUGE})
    assert sys.get_int_max_str_digits() == 4300
