"""add evaluation run attempt and append-only result persistence

Revision ID: 8f3c2e1d4a5b
Revises: 3ca1d40bddfa
Create Date: 2026-08-12
"""

# ruff: noqa: D415

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8f3c2e1d4a5b"
down_revision: Union[str, None] = "3ca1d40bddfa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the isolated WP3 persistence schema。"""
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("dataset_id", sa.String(255), nullable=False),
        sa.Column("dataset_version", sa.String(255), nullable=False),
        sa.Column("suite_id", sa.String(255), nullable=False),
        sa.Column("suite_version", sa.String(255), nullable=False),
        sa.Column("execution_target_id", sa.String(255), nullable=False),
        sa.Column("execution_target_kind", sa.String(100), nullable=False),
        sa.Column("target_version_kind", sa.String(100), nullable=True),
        sa.Column("target_version_value", sa.String(255), nullable=True),
        sa.Column("dataset_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("suite_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("execution_target_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("subject_ref", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('PENDING','RUNNING','COMPLETED','FAILED','OUTCOME_UNKNOWN')", name="ck_evaluation_runs_status"),
        sa.CheckConstraint("(target_version_kind IS NULL) = (target_version_value IS NULL)", name="ck_evaluation_runs_target_version_pair"),
        sa.CheckConstraint("((status IN ('COMPLETED','FAILED','OUTCOME_UNKNOWN')) = (finished_at IS NOT NULL))", name="ck_evaluation_runs_terminal_finished"),
        sa.CheckConstraint("finished_at IS NULL OR finished_at >= COALESCE(started_at, created_at)", name="ck_evaluation_runs_finished_order"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_evaluation_runs_project", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_runs"),
        sa.UniqueConstraint("project_id", "id", name="uq_evaluation_runs_project_id_id"),
        sa.UniqueConstraint("project_id", "id", "dataset_id", "dataset_version", "suite_id", "suite_version", name="uq_evaluation_runs_result_provenance"),
    )
    op.create_index("ix_evaluation_runs_project_created", "evaluation_runs", ["project_id", "created_at"])
    op.create_index("ix_evaluation_runs_project_status_created", "evaluation_runs", ["project_id", "status", "created_at"])

    op.create_table(
        "evaluation_attempts",
        sa.Column("id", sa.UUID(), nullable=False), sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False), sa.Column("case_id", sa.String(255), nullable=False),
        sa.Column("case_version", sa.String(255), nullable=False), sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("retry_of_attempt_id", sa.UUID(), nullable=True),
        sa.Column("execution_target_id", sa.String(255), nullable=False), sa.Column("execution_target_kind", sa.String(100), nullable=False),
        sa.Column("target_version_kind", sa.String(100), nullable=True), sa.Column("target_version_value", sa.String(255), nullable=True),
        sa.Column("target_config_kind", sa.String(100), nullable=True), sa.Column("target_config_value", sa.String(255), nullable=True),
        sa.Column("execution_request_id", sa.String(255), nullable=False), sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_snapshot", postgresql.JSONB(), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("claim_token", sa.UUID(), nullable=True), sa.Column("worker_ref", sa.String(255), nullable=True),
        sa.Column("task_ref", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True), sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True), sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_outcome_kind", sa.String(32), nullable=True), sa.Column("output_artifact_ref", postgresql.JSONB(), nullable=True),
        sa.Column("outcome_evidence_refs", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("error_category", sa.String(255), nullable=True), sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("outcome_metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.CheckConstraint("attempt_no > 0", name="ck_evaluation_attempts_number_positive"),
        sa.CheckConstraint("(attempt_no = 1 AND retry_of_attempt_id IS NULL) OR (attempt_no > 1 AND retry_of_attempt_id IS NOT NULL)", name="ck_evaluation_attempts_retry_lineage"),
        sa.CheckConstraint("status IN ('PENDING','CLAIMED','RUNNING','TERMINAL')", name="ck_evaluation_attempts_status"),
        sa.CheckConstraint("(target_version_kind IS NULL) = (target_version_value IS NULL)", name="ck_evaluation_attempts_target_version_pair"),
        sa.CheckConstraint("(target_config_kind IS NULL) = (target_config_value IS NULL)", name="ck_evaluation_attempts_target_config_pair"),
        sa.CheckConstraint("(status = 'PENDING' AND claim_token IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL) OR (status IN ('CLAIMED','RUNNING') AND claim_token IS NOT NULL AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status = 'TERMINAL' AND claim_token IS NOT NULL)", name="ck_evaluation_attempts_claim_state"),
        sa.CheckConstraint("(status = 'TERMINAL') = (execution_outcome_kind IS NOT NULL AND finished_at IS NOT NULL)", name="ck_evaluation_attempts_terminal_outcome"),
        sa.CheckConstraint("execution_outcome_kind IS NULL OR execution_outcome_kind IN ('SUCCESS','FAILURE','TIMEOUT','CANCELLED','OUTCOME_UNKNOWN')", name="ck_evaluation_attempts_outcome_kind"),
        sa.CheckConstraint("execution_outcome_kind IS NULL OR (execution_outcome_kind = 'SUCCESS' AND output_artifact_ref IS NOT NULL AND error_category IS NULL) OR (execution_outcome_kind <> 'SUCCESS' AND output_artifact_ref IS NULL AND error_category IS NOT NULL AND reason IS NOT NULL)", name="ck_evaluation_attempts_outcome_payload"),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["evaluation_runs.project_id", "evaluation_runs.id"], name="fk_evaluation_attempts_project_run", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "run_id", "retry_of_attempt_id"], ["evaluation_attempts.project_id", "evaluation_attempts.run_id", "evaluation_attempts.id"], name="fk_evaluation_attempts_retry_parent"),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_attempts"),
        sa.UniqueConstraint("project_id", "run_id", "id", name="uq_evaluation_attempts_project_run_id"),
        sa.UniqueConstraint("project_id", "run_id", "id", "case_id", "case_version", "execution_target_id", "execution_request_id", name="uq_evaluation_attempts_result_provenance"),
        sa.UniqueConstraint("project_id", "run_id", "case_id", "case_version", "attempt_no", name="uq_evaluation_attempts_case_number"),
        sa.UniqueConstraint("project_id", "run_id", "execution_request_id", name="uq_evaluation_attempts_request"),
        sa.UniqueConstraint("claim_token", name="uq_evaluation_attempts_claim_token"),
    )
    op.create_index("uq_evaluation_attempts_direct_retry", "evaluation_attempts", ["project_id", "run_id", "retry_of_attempt_id"], unique=True, postgresql_where=sa.text("retry_of_attempt_id IS NOT NULL"))
    op.create_index("ix_evaluation_attempts_case_number", "evaluation_attempts", ["project_id", "run_id", "case_id", "case_version", "attempt_no"])
    op.create_index("ix_evaluation_attempts_run_status", "evaluation_attempts", ["project_id", "run_id", "status"])
    op.create_index("ix_evaluation_attempts_stale", "evaluation_attempts", ["lease_expires_at"], postgresql_where=sa.text("status IN ('CLAIMED','RUNNING')"))

    op.create_table(
        "evaluation_results",
        sa.Column("id", sa.UUID(), nullable=False), sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False), sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("dataset_id", sa.String(255), nullable=False), sa.Column("dataset_version", sa.String(255), nullable=False),
        sa.Column("case_id", sa.String(255), nullable=False), sa.Column("case_version", sa.String(255), nullable=False),
        sa.Column("suite_id", sa.String(255), nullable=False), sa.Column("suite_version", sa.String(255), nullable=False),
        sa.Column("evaluator_id", sa.String(255), nullable=False), sa.Column("evaluator_version", sa.String(255), nullable=False),
        sa.Column("config_ref_kind", sa.String(100), nullable=False), sa.Column("config_ref_value", sa.String(255), nullable=False),
        sa.Column("prompt_ref_kind", sa.String(100), nullable=True), sa.Column("prompt_ref_value", sa.String(255), nullable=True),
        sa.Column("execution_target_id", sa.String(255), nullable=False), sa.Column("target_version_kind", sa.String(100), nullable=True),
        sa.Column("target_version_value", sa.String(255), nullable=True), sa.Column("execution_request_id", sa.String(255), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("provenance_completeness", sa.String(16), nullable=False), sa.Column("output_artifact_ref", postgresql.JSONB(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True), sa.Column("evidence_refs", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("verdict IN ('PASS','FAIL','INCONCLUSIVE','ERROR')", name="ck_evaluation_results_verdict"),
        sa.CheckConstraint("provenance_completeness IN ('COMPLETE','PARTIAL')", name="ck_evaluation_results_provenance"),
        sa.CheckConstraint("(prompt_ref_kind IS NULL) = (prompt_ref_value IS NULL)", name="ck_evaluation_results_prompt_pair"),
        sa.CheckConstraint("(target_version_kind IS NULL) = (target_version_value IS NULL)", name="ck_evaluation_results_target_pair"),
        sa.CheckConstraint("score IS NULL OR score NOT IN ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)", name="ck_evaluation_results_finite_score"),
        sa.ForeignKeyConstraint(["project_id", "run_id", "dataset_id", "dataset_version", "suite_id", "suite_version"], ["evaluation_runs.project_id", "evaluation_runs.id", "evaluation_runs.dataset_id", "evaluation_runs.dataset_version", "evaluation_runs.suite_id", "evaluation_runs.suite_version"], name="fk_evaluation_results_run_provenance", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "run_id", "attempt_id", "case_id", "case_version", "execution_target_id", "execution_request_id"], ["evaluation_attempts.project_id", "evaluation_attempts.run_id", "evaluation_attempts.id", "evaluation_attempts.case_id", "evaluation_attempts.case_version", "evaluation_attempts.execution_target_id", "evaluation_attempts.execution_request_id"], name="fk_evaluation_results_attempt_provenance", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_results"),
        sa.UniqueConstraint("run_id", "attempt_id", "case_id", "case_version", "evaluator_id", "evaluator_version", name="uq_evaluation_results_logical_slot"),
    )
    op.create_index("ix_evaluation_results_run_created", "evaluation_results", ["project_id", "run_id", "created_at"])
    op.create_index("ix_evaluation_results_attempt", "evaluation_results", ["project_id", "attempt_id"])
    op.create_index("ix_evaluation_results_case_evaluator", "evaluation_results", ["project_id", "case_id", "case_version", "evaluator_id", "evaluator_version"])
    op.execute("""
        CREATE FUNCTION reject_evaluation_result_update() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'evaluation_results rows are immutable';
        END; $$
    """)
    op.execute("""
        CREATE TRIGGER trg_evaluation_results_immutable
        BEFORE UPDATE ON evaluation_results
        FOR EACH ROW EXECUTE FUNCTION reject_evaluation_result_update()
    """)


def downgrade() -> None:
    """Remove only the WP3 bounded-context schema。"""
    op.execute("DROP TRIGGER IF EXISTS trg_evaluation_results_immutable ON evaluation_results")
    op.execute("DROP FUNCTION IF EXISTS reject_evaluation_result_update()")
    op.drop_table("evaluation_results")
    op.drop_table("evaluation_attempts")
    op.drop_table("evaluation_runs")
