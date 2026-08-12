"""Alembic WP3 disposable PostgreSQL migration verification。"""

# ruff: noqa: D415

import os
import subprocess
from uuid import uuid4

import psycopg2
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def migration_database():
    """Create a disposable database on the integration PostgreSQL instance。"""
    name = f"pandaprobe_wp3_{uuid4().hex}"
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


def test_empty_parent_downgrade_reupgrade_and_schema_parity(migration_database):
    # Path A: empty -> head.
    alembic(migration_database, "upgrade", "head")
    url = f"postgresql+psycopg2://postgres:postgres@localhost:5433/{migration_database}"
    engine = create_engine(url)
    inspector = inspect(engine)
    assert {"evaluation_runs", "evaluation_attempts", "evaluation_results"} <= set(inspector.get_table_names())
    assert {"project_id", "dataset_snapshot", "suite_snapshot", "status"} <= {
        column["name"] for column in inspector.get_columns("evaluation_runs")
    }
    assert "uq_evaluation_results_logical_slot" in {
        item["name"] for item in inspector.get_unique_constraints("evaluation_results")
    }
    attempt_fks = {item["name"] for item in inspector.get_foreign_keys("evaluation_attempts")}
    assert {"fk_evaluation_attempts_project_run", "fk_evaluation_attempts_retry_parent"} <= attempt_fks
    with engine.connect() as connection:
        trigger = connection.execute(text("SELECT tgname FROM pg_trigger WHERE tgname='trg_evaluation_results_immutable' AND NOT tgisinternal")).scalar_one()
        assert trigger == "trg_evaluation_results_immutable"
        ids = [str(uuid4()) for _ in range(5)]
        organization_id, project_id, run_id, attempt_id, result_id = ids
        connection.execute(text("INSERT INTO organizations (id,name,created_at) VALUES (:id,'org',CURRENT_TIMESTAMP)"), {"id": organization_id})
        connection.execute(text("INSERT INTO projects (id,org_id,name,description,created_at) VALUES (:id,:org,'project','',CURRENT_TIMESTAMP)"), {"id": project_id, "org": organization_id})
        connection.execute(text("""
            INSERT INTO evaluation_runs
              (id,project_id,dataset_id,dataset_version,suite_id,suite_version,execution_target_id,
               execution_target_kind,target_version_kind,target_version_value,dataset_snapshot,suite_snapshot,
               execution_target_snapshot,status,metadata,created_at,started_at)
            VALUES (:run,:project,'dataset','d1','suite','s1','target','FIXTURE','git','abc',
                    '{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'RUNNING','{}'::jsonb,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
        """), {"run": run_id, "project": project_id})
        connection.execute(text("""
            INSERT INTO evaluation_attempts
              (id,project_id,run_id,case_id,case_version,attempt_no,execution_target_id,execution_target_kind,
               target_version_kind,target_version_value,execution_request_id,idempotency_key,request_snapshot,status,
               claim_token,created_at,claimed_at,started_at,finished_at,lease_expires_at,execution_outcome_kind,
               output_artifact_ref,outcome_evidence_refs,outcome_metadata)
            VALUES (:attempt,:project,:run,'case','v1',1,'target','FIXTURE','git','abc','request','stable',
                    '{"input_payload":{},"timeout_seconds":1}'::jsonb,'TERMINAL',:token,CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'SUCCESS',
                    '{"artifact_id":"a"}'::jsonb,'[]'::jsonb,'{}'::jsonb)
        """), {"attempt": attempt_id, "project": project_id, "run": run_id, "token": str(uuid4())})
        connection.execute(text("""
            INSERT INTO evaluation_results
              (id,project_id,run_id,attempt_id,dataset_id,dataset_version,case_id,case_version,suite_id,suite_version,
               evaluator_id,evaluator_version,config_ref_kind,config_ref_value,execution_target_id,target_version_kind,
               target_version_value,execution_request_id,verdict,reason,provenance_completeness,evidence_refs,metadata,created_at)
            VALUES (:result,:project,:run,:attempt,'dataset','d1','case','v1','suite','s1','eval','e1','cfg','1',
                    'target','git','abc','request','FAIL','wrong','COMPLETE','[]'::jsonb,'{}'::jsonb,CURRENT_TIMESTAMP)
        """), {"result": result_id, "project": project_id, "run": run_id, "attempt": attempt_id})
        with pytest.raises(DBAPIError, match="immutable"):
            connection.execute(text("UPDATE evaluation_results SET verdict='PASS' WHERE id=:id"), {"id": result_id})
        connection.rollback()

    # Path C: head -> parent -> head. This also proves Path B parent -> head.
    alembic(migration_database, "downgrade", "3ca1d40bddfa")
    assert "evaluation_runs" not in inspect(engine).get_table_names()
    alembic(migration_database, "upgrade", "head")
    assert "evaluation_results" in inspect(engine).get_table_names()
    engine.dispose()
