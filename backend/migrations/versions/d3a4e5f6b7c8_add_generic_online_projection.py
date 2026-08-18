"""add generic online normalized trace/span projection

Revision ID: d3a4e5f6b7c8
Revises: 7ca7dbab5b86
Create Date: 2026-08-18
"""

# ruff: noqa: D415

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d3a4e5f6b7c8"
down_revision: Union[str, None] = "7ca7dbab5b86"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable normalized projection columns without historical backfill."""
    op.add_column("traces", sa.Column("normalized_source_kind", sa.String(length=64), nullable=True))
    op.add_column("traces", sa.Column("normalized_outcome", sa.String(length=16), nullable=True))
    op.add_column("traces", sa.Column("source_contract_identity", sa.String(length=128), nullable=True))
    op.add_column("traces", sa.Column("source_contract_version", sa.Integer(), nullable=True))
    op.add_column("traces", sa.Column("subject_version_ref", sa.String(length=255), nullable=True))
    op.add_column("spans", sa.Column("normalized_operation", sa.String(length=512), nullable=True))
    op.add_column("spans", sa.Column("normalized_component", sa.String(length=255), nullable=True))
    op.add_column("spans", sa.Column("normalized_outcome", sa.String(length=16), nullable=True))
    op.add_column("spans", sa.Column("normalized_error_code", sa.String(length=128), nullable=True))
    op.add_column("spans", sa.Column("normalized_duration_ms", sa.Numeric(), nullable=True))
    op.add_column(
        "spans",
        sa.Column("normalized_attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_traces_project_normalized_source_outcome_started",
        "traces",
        ["project_id", "normalized_source_kind", "normalized_outcome", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_spans_trace_normalized_operation_outcome",
        "spans",
        ["trace_id", "normalized_operation", "normalized_outcome"],
        unique=False,
    )


def downgrade() -> None:
    """Remove only the generic online projection columns and indexes."""
    op.drop_index("ix_spans_trace_normalized_operation_outcome", table_name="spans")
    op.drop_index("ix_traces_project_normalized_source_outcome_started", table_name="traces")
    op.drop_column("spans", "normalized_attributes")
    op.drop_column("spans", "normalized_duration_ms")
    op.drop_column("spans", "normalized_error_code")
    op.drop_column("spans", "normalized_outcome")
    op.drop_column("spans", "normalized_component")
    op.drop_column("spans", "normalized_operation")
    op.drop_column("traces", "subject_version_ref")
    op.drop_column("traces", "source_contract_version")
    op.drop_column("traces", "source_contract_identity")
    op.drop_column("traces", "normalized_outcome")
    op.drop_column("traces", "normalized_source_kind")
