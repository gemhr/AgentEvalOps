"""冻结 Stage13 Attempt 执行请求，保留历史 Kernel 与 Dataset."""

from alembic import op

revision = "c13a00100001"
down_revision = "c12a00800001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加独立不可变执行 envelope，不回填旧 Attempt."""
    op.execute("""
    CREATE TABLE evaluation_stage13_execution_requests (
      project_id uuid NOT NULL, run_id uuid NOT NULL, attempt_id uuid PRIMARY KEY,
      case_id varchar NOT NULL, case_version text NOT NULL,
      policy_version text NOT NULL CHECK(policy_version='stage13.attempt-execution.v1'),
      request_bytes bytea NOT NULL CHECK(octet_length(request_bytes)<=262144),
      request_digest text NOT NULL CHECK(length(request_digest)=64),
      FOREIGN KEY(project_id,run_id,attempt_id,case_id,case_version)
        REFERENCES evaluation_attempts(project_id,run_id,id,case_id,case_version)
    );
    CREATE FUNCTION stage13_execution_request_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'STAGE13_EXECUTION_REQUEST_IMMUTABLE'; END $$;
    CREATE TRIGGER stage13_execution_request_guard BEFORE UPDATE OR DELETE
      ON evaluation_stage13_execution_requests FOR EACH ROW EXECUTE FUNCTION stage13_execution_request_guard();
    CREATE TRIGGER stage13_execution_request_no_truncate BEFORE TRUNCATE
      ON evaluation_stage13_execution_requests FOR EACH STATEMENT EXECUTE FUNCTION stage13_execution_request_guard();
    """)


def downgrade() -> None:
    """不抹除已冻结执行证据."""
    raise RuntimeError("STAGE13_EXECUTION_HISTORY_DOWNGRADE_FORBIDDEN")
