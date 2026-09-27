"""Link legacy evaluation rows to canonical run and result facts.

Revision ID: a2c9f84e1b73
Revises: 11a6d90c3f2b
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a2c9f84e1b73"
down_revision: Union[str, None] = "11a6d90c3f2b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable projection links without synthesizing legacy history."""
    op.create_unique_constraint(
        "uq_evaluation_results_project_id_id", "evaluation_results", ["project_id", "id"]
    )

    op.add_column("eval_runs", sa.Column("canonical_run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_unique_constraint("uq_eval_runs_canonical_run_id", "eval_runs", ["canonical_run_id"])
    op.create_foreign_key(
        "fk_eval_runs_canonical_run",
        "eval_runs",
        "evaluation_runs",
        ["project_id", "canonical_run_id"],
        ["project_id", "id"],
        ondelete="RESTRICT",
    )

    op.add_column("trace_scores", sa.Column("canonical_result_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("trace_scores", sa.Column("canonical_verdict", sa.String(length=32), nullable=True))
    op.create_unique_constraint("uq_trace_scores_canonical_result_id", "trace_scores", ["canonical_result_id"])
    op.create_foreign_key(
        "fk_trace_scores_canonical_result",
        "trace_scores",
        "evaluation_results",
        ["project_id", "canonical_result_id"],
        ["project_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_trace_scores_canonical_verdict",
        "trace_scores",
        "canonical_verdict IS NULL OR canonical_verdict IN ('PASS','FAIL','INCONCLUSIVE','ERROR')",
    )
    op.create_check_constraint(
        "ck_trace_scores_canonical_result_verdict_pair",
        "trace_scores",
        "(canonical_result_id IS NULL) = (canonical_verdict IS NULL)",
    )

    op.add_column("session_scores", sa.Column("canonical_result_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("session_scores", sa.Column("canonical_verdict", sa.String(length=32), nullable=True))
    op.create_unique_constraint("uq_session_scores_canonical_result_id", "session_scores", ["canonical_result_id"])
    op.create_foreign_key(
        "fk_session_scores_canonical_result",
        "session_scores",
        "evaluation_results",
        ["project_id", "canonical_result_id"],
        ["project_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_session_scores_canonical_verdict",
        "session_scores",
        "canonical_verdict IS NULL OR canonical_verdict IN ('PASS','FAIL','INCONCLUSIVE','ERROR')",
    )
    op.create_check_constraint(
        "ck_session_scores_canonical_result_verdict_pair",
        "session_scores",
        "(canonical_result_id IS NULL) = (canonical_verdict IS NULL)",
    )

def downgrade() -> None:
    """Remove projection links and constraints."""
    op.drop_constraint("ck_session_scores_canonical_result_verdict_pair", "session_scores", type_="check")
    op.drop_constraint("ck_session_scores_canonical_verdict", "session_scores", type_="check")
    op.drop_constraint("fk_session_scores_canonical_result", "session_scores", type_="foreignkey")
    op.drop_constraint("uq_session_scores_canonical_result_id", "session_scores", type_="unique")
    op.drop_column("session_scores", "canonical_verdict")
    op.drop_column("session_scores", "canonical_result_id")

    op.drop_constraint("ck_trace_scores_canonical_result_verdict_pair", "trace_scores", type_="check")
    op.drop_constraint("ck_trace_scores_canonical_verdict", "trace_scores", type_="check")
    op.drop_constraint("fk_trace_scores_canonical_result", "trace_scores", type_="foreignkey")
    op.drop_constraint("uq_trace_scores_canonical_result_id", "trace_scores", type_="unique")
    op.drop_column("trace_scores", "canonical_verdict")
    op.drop_column("trace_scores", "canonical_result_id")

    op.drop_constraint("fk_eval_runs_canonical_run", "eval_runs", type_="foreignkey")
    op.drop_constraint("uq_eval_runs_canonical_run_id", "eval_runs", type_="unique")
    op.drop_column("eval_runs", "canonical_run_id")
    op.drop_constraint("uq_evaluation_results_project_id_id", "evaluation_results", type_="unique")
