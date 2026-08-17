"""Alembic disposable PostgreSQL migration verification for the LocalAgent schema."""

# ruff: noqa: D415

import os
import re
import subprocess
from uuid import uuid4

import psycopg2
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

LOCALAGENT_TABLES = [
    "localagent_trace_envelope_sidecars",
    "localagent_external_span_identity",
    "localagent_external_trace_identity",
]


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    """Override integration conftest create_all; only Alembic may build tables here."""
    yield


@pytest.fixture(autouse=True)
def _override_deps():
    """Override API/Redis autouse fixtures; migration tests do not use them."""
    yield


@pytest.fixture
def migration_database():
    """Create a disposable database on the integration PostgreSQL instance."""
    name = f"pandaprobe_la_{uuid4().hex}"
    admin = psycopg2.connect(host="localhost", port=5433, user="postgres", password="postgres", dbname="postgres")
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        with admin.cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,))
            cursor.execute(f'DROP DATABASE "{name}"')
        admin.close()


def alembic(database: str, *args: str) -> None:
    env = os.environ.copy()
    env.update({"POSTGRES_HOST": "localhost", "POSTGRES_PORT": "5433", "POSTGRES_DB": database})
    subprocess.run(["uv", "run", "--frozen", "--no-sync", "alembic", *args], check=True, env=env)


def _assert_columns(inspector, table: str, expected: dict[str, bool]) -> None:
    columns = {item["name"]: item for item in inspector.get_columns(table)}
    assert set(columns) == set(expected)
    assert {name: item["nullable"] for name, item in columns.items()} == expected


def _normalize_sql(value: str) -> str:
    normalized = value.lower().replace('"', "")
    normalized = re.sub(
        r"::(?:character varying|double precision|float8|text|varchar|bpchar)(?:\[\])?",
        "",
        normalized,
    )
    return re.sub(r"\s+", " ", normalized).strip()


def _quoted_values(value: str) -> set[str]:
    return set(re.findall(r"'([^']+)'", value))


def _assert_named_columns(items, expected: dict[str, tuple[str, ...]], *, column_key: str = "column_names") -> None:
    actual = {item["name"]: tuple(item[column_key]) for item in items}
    for name, columns in expected.items():
        assert actual[name] == columns


def _assert_fk(inspector, table: str, name: str, constrained, referred_table, referred, ondelete) -> None:
    foreign_keys = {item["name"]: item for item in inspector.get_foreign_keys(table)}
    actual = foreign_keys[name]
    assert tuple(actual["constrained_columns"]) == constrained
    assert actual["referred_table"] == referred_table
    assert tuple(actual["referred_columns"]) == referred
    assert actual.get("options", {}).get("ondelete") == ondelete


def _get_check_definitions(engine, table: str) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT constraint_row.conname, pg_get_constraintdef(constraint_row.oid, true)
                FROM pg_constraint AS constraint_row
                JOIN pg_class AS table_row ON table_row.oid = constraint_row.conrelid
                JOIN pg_namespace AS schema_row ON schema_row.oid = table_row.relnamespace
                WHERE constraint_row.contype = 'c'
                  AND schema_row.nspname = current_schema()
                  AND table_row.relname = :table
                """
            ),
            {"table": table},
        ).all()
    return {name: definition for name, definition in rows}


def _assert_trigger(engine, table: str, trigger: str, function: str) -> None:
    with engine.connect() as connection:
        trigger_definition, function_definition = connection.execute(
            text(
                """
                SELECT pg_get_triggerdef(t.oid), pg_get_functiondef(p.oid)
                FROM pg_trigger t
                JOIN pg_proc p ON p.oid = t.tgfoid
                JOIN pg_class c ON c.oid = t.tgrelid
                WHERE t.tgname = :trigger AND c.relname = :table AND NOT t.tgisinternal
                """
            ),
            {"trigger": trigger, "table": table},
        ).one()
    assert "BEFORE UPDATE ON" in trigger_definition
    assert "EXECUTE FUNCTION" in trigger_definition
    assert function in trigger_definition
    assert "CREATE OR REPLACE FUNCTION" in function_definition or "CREATE FUNCTION" in function_definition
    assert "rows are immutable" in function_definition


def _assert_schema_parity(engine) -> None:
    inspector = inspect(engine)
    assert set(LOCALAGENT_TABLES) <= set(inspector.get_table_names())

    _assert_columns(inspector, "localagent_external_trace_identity", {
        "external_trace_id": False, "project_id": False, "internal_trace_uuid": False,
        "run_id": False, "created_at": False,
    })
    pk = set(inspector.get_pk_constraint("localagent_external_trace_identity")["constrained_columns"])
    assert pk == {"external_trace_id"}
    _assert_fk(
        inspector, "localagent_external_trace_identity", "fk_localagent_trace_identity_project",
        ("project_id",), "projects", ("id",), "CASCADE",
    )
    _assert_named_columns(inspector.get_indexes("localagent_external_trace_identity"), {
        "ix_localagent_trace_identity_project": ("project_id",),
    })

    _assert_columns(inspector, "localagent_external_span_identity", {
        "external_span_id": False, "project_id": False, "internal_span_uuid": False,
        "external_trace_id": False, "created_at": False,
    })
    pk = set(inspector.get_pk_constraint("localagent_external_span_identity")["constrained_columns"])
    assert pk == {"external_span_id"}
    _assert_fk(
        inspector, "localagent_external_span_identity", "fk_localagent_span_identity_project",
        ("project_id",), "projects", ("id",), "CASCADE",
    )
    _assert_fk(
        inspector, "localagent_external_span_identity", "fk_localagent_span_identity_trace",
        ("external_trace_id",), "localagent_external_trace_identity", ("external_trace_id",), "CASCADE",
    )
    _assert_named_columns(inspector.get_indexes("localagent_external_span_identity"), {
        "ix_localagent_span_identity_project": ("project_id",),
        "ix_localagent_span_identity_trace": ("external_trace_id",),
    })

    _assert_columns(inspector, "localagent_trace_envelope_sidecars", {
        "envelope_id": False, "project_id": False, "external_run_id": False, "external_trace_id": False,
        "external_span_id": False, "external_parent_span_id": True, "step_id": True, "operation": False,
        "component": False, "started_at": False, "completed_at": False, "duration_ms": False,
        "status": False, "error_code": True, "attributes": False, "contract_identity": False,
        "contract_version": False, "contract_fingerprint": False, "canonical_payload_digest": False,
        "internal_trace_uuid": False, "internal_span_uuid": False, "created_at": False,
    })
    # Authoritative duration storage must be lossless PostgreSQL NUMERIC (P1-06).
    duration_type = next(
        c["type"] for c in inspector.get_columns("localagent_trace_envelope_sidecars") if c["name"] == "duration_ms"
    )
    assert str(duration_type).upper().startswith("NUMERIC"), duration_type
    pk = set(inspector.get_pk_constraint("localagent_trace_envelope_sidecars")["constrained_columns"])
    assert pk == {"envelope_id"}
    _assert_named_columns(inspector.get_unique_constraints("localagent_trace_envelope_sidecars"), {
        "uq_localagent_sidecar_span": ("external_span_id",),
    })
    _assert_fk(
        inspector, "localagent_trace_envelope_sidecars", "fk_localagent_sidecar_project",
        ("project_id",), "projects", ("id",), "CASCADE",
    )
    _assert_fk(
        inspector, "localagent_trace_envelope_sidecars", "fk_localagent_sidecar_span_identity",
        ("external_span_id",), "localagent_external_span_identity", ("external_span_id",), "CASCADE",
    )
    _assert_named_columns(inspector.get_indexes("localagent_trace_envelope_sidecars"), {
        "ix_localagent_sidecar_project_created": ("project_id", "created_at"),
        "ix_localagent_sidecar_trace": ("external_trace_id",),
    })

    sidecar_checks = _get_check_definitions(engine, "localagent_trace_envelope_sidecars")
    assert set(sidecar_checks) == {
        "ck_localagent_sidecar_status",
        "ck_localagent_sidecar_time_order",
        "ck_localagent_sidecar_duration",
        "ck_localagent_sidecar_status_error",
    }
    assert _quoted_values(sidecar_checks["ck_localagent_sidecar_status"]) == {
        "OK", "ERROR", "CANCELLED", "TIMED_OUT",
    }
    time_order = _normalize_sql(sidecar_checks["ck_localagent_sidecar_time_order"])
    assert "completed_at" in time_order and "started_at" in time_order and ">=" in time_order
    duration = _normalize_sql(sidecar_checks["ck_localagent_sidecar_duration"])
    assert "duration_ms" in duration and ">=" in duration and "0" in duration
    # NaN/±Infinity are representable in NUMERIC, so the CHECK must explicitly
    # reject them: '< 'Infinity'::numeric' (NaN compares greater than Infinity).
    assert _quoted_values(sidecar_checks["ck_localagent_sidecar_duration"]) == {"Infinity"}
    status_error = _normalize_sql(sidecar_checks["ck_localagent_sidecar_status_error"])
    assert "error_code" in status_error and "is null" in status_error and "is not null" in status_error

    _assert_trigger(engine, "localagent_external_trace_identity",
                    "trg_localagent_trace_identity_immutable", "reject_localagent_trace_identity_update()")
    _assert_trigger(engine, "localagent_external_span_identity",
                    "trg_localagent_span_identity_immutable", "reject_localagent_span_identity_update()")
    _assert_trigger(engine, "localagent_trace_envelope_sidecars",
                    "trg_localagent_sidecar_immutable", "reject_localagent_sidecar_update()")


def _seed_identity_rows(connection, org_id: str, project_id: str) -> None:
    connection.execute(
        text("INSERT INTO organizations (id, name, created_at) VALUES (:id, 'org', CURRENT_TIMESTAMP)"),
        {"id": org_id},
    )
    connection.execute(
        text(
            "INSERT INTO projects (id, org_id, name, description, created_at) "
            "VALUES (:id, :org, 'project', '', CURRENT_TIMESTAMP)"
        ),
        {"id": project_id, "org": org_id},
    )
    connection.execute(
        text(
            "INSERT INTO localagent_external_trace_identity "
            "(external_trace_id, project_id, internal_trace_uuid, run_id) "
            "VALUES ('trace-1', :project, :internal, 'run-1')"
        ),
        {"project": project_id, "internal": str(uuid4())},
    )


def _assert_immutable_rows(engine) -> None:
    with engine.connect() as connection:
        org_id = str(uuid4())
        project_id = str(uuid4())
        _seed_identity_rows(connection, org_id, project_id)
        with pytest.raises(DBAPIError, match="immutable"):
            connection.execute(
                text("UPDATE localagent_external_trace_identity SET run_id = 'run-2' WHERE external_trace_id = 'trace-1'")
            )
        connection.rollback()

        org_id = str(uuid4())
        project_id = str(uuid4())
        _seed_identity_rows(connection, org_id, project_id)
        connection.execute(
            text(
                "INSERT INTO localagent_external_span_identity "
                "(external_span_id, project_id, internal_span_uuid, external_trace_id) "
                "VALUES ('span-1', :project, :internal, 'trace-1')"
            ),
            {"project": project_id, "internal": str(uuid4())},
        )
        with pytest.raises(DBAPIError, match="immutable"):
            connection.execute(
                text(
                    "UPDATE localagent_external_span_identity SET project_id = :other "
                    "WHERE external_span_id = 'span-1'"
                ),
                {"other": str(uuid4())},
            )
        connection.rollback()

        org_id = str(uuid4())
        project_id = str(uuid4())
        _seed_identity_rows(connection, org_id, project_id)
        connection.execute(
            text(
                "INSERT INTO localagent_external_span_identity "
                "(external_span_id, project_id, internal_span_uuid, external_trace_id) "
                "VALUES ('span-1', :project, :internal, 'trace-1')"
            ),
            {"project": project_id, "internal": str(uuid4())},
        )
        connection.execute(
            text(
                "INSERT INTO localagent_trace_envelope_sidecars "
                "(envelope_id, project_id, external_run_id, external_trace_id, external_span_id, "
                "operation, component, started_at, completed_at, duration_ms, status, attributes, "
                "contract_identity, contract_version, contract_fingerprint, canonical_payload_digest, "
                "internal_trace_uuid, internal_span_uuid) "
                "VALUES (:envelope, :project, 'run-1', 'trace-1', 'span-1', 'runtime.step', 'comp', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1.0, 'OK', '{}'::jsonb, 'localagent.runtime.trace_export', "
                "1, :fingerprint, :digest, :internal_trace, :internal_span)"
            ),
            {
                "envelope": str(uuid4()),
                "project": project_id,
                "fingerprint": "6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab",
                "digest": "a" * 64,
                "internal_trace": str(uuid4()),
                "internal_span": str(uuid4()),
            },
        )
        with pytest.raises(DBAPIError, match="immutable"):
            connection.execute(
                text("UPDATE localagent_trace_envelope_sidecars SET status = 'ERROR' WHERE external_span_id = 'span-1'")
            )
        connection.rollback()


def test_empty_database_upgrade_head_and_schema_parity(migration_database):
    alembic(migration_database, "upgrade", "head")
    engine = create_engine(f"postgresql+psycopg2://postgres:postgres@localhost:5433/{migration_database}")
    _assert_schema_parity(engine)
    _assert_immutable_rows(engine)
    engine.dispose()


def test_upgrade_from_wp3_head(migration_database):
    alembic(migration_database, "upgrade", "8f3c2e1d4a5b")
    alembic(migration_database, "upgrade", "head")
    engine = create_engine(f"postgresql+psycopg2://postgres:postgres@localhost:5433/{migration_database}")
    assert "localagent_trace_envelope_sidecars" in inspect(engine).get_table_names()
    engine.dispose()


def test_downgrade_and_reupgrade(migration_database):
    alembic(migration_database, "upgrade", "head")
    engine = create_engine(f"postgresql+psycopg2://postgres:postgres@localhost:5433/{migration_database}")
    assert "localagent_trace_envelope_sidecars" in inspect(engine).get_table_names()

    alembic(migration_database, "downgrade", "8f3c2e1d4a5b")
    names = set(inspect(engine).get_table_names())
    assert not (set(LOCALAGENT_TABLES) & names)

    alembic(migration_database, "upgrade", "head")
    assert "localagent_trace_envelope_sidecars" in inspect(engine).get_table_names()
    engine.dispose()
