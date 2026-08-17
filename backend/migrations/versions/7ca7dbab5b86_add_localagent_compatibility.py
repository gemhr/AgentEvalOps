"""add LocalAgent WP4-C compatibility boundary schema

Revision ID: 7ca7dbab5b86
Revises: 8f3c2e1d4a5b
Create Date: 2026-08-15
"""

# ruff: noqa: D415

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7ca7dbab5b86"
down_revision: Union[str, None] = "8f3c2e1d4a5b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _immutable_trigger(table: str, trigger_name: str, function_name: str) -> None:
    """Create a BEFORE UPDATE trigger that rejects any UPDATE on an immutable row."""
    op.execute(
        f"""
        CREATE FUNCTION {function_name}() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION '{table} rows are immutable';
        END $$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {trigger_name}
        BEFORE UPDATE ON {table}
        FOR EACH ROW EXECUTE FUNCTION {function_name}()
        """
    )


def upgrade() -> None:
    """Create the LocalAgent compatibility identity/sidecar schema."""
    op.create_table(
        "localagent_external_trace_identity",
        sa.Column("external_trace_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("internal_trace_uuid", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("external_trace_id", name="pk_localagent_external_trace_identity"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_localagent_trace_identity_project", ondelete="CASCADE"),
    )
    op.create_index(
        "ix_localagent_trace_identity_project", "localagent_external_trace_identity", ["project_id"]
    )

    op.create_table(
        "localagent_external_span_identity",
        sa.Column("external_span_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("internal_span_uuid", sa.UUID(), nullable=False),
        sa.Column("external_trace_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("external_span_id", name="pk_localagent_external_span_identity"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_localagent_span_identity_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["external_trace_id"],
            ["localagent_external_trace_identity.external_trace_id"],
            name="fk_localagent_span_identity_trace",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_localagent_span_identity_project", "localagent_external_span_identity", ["project_id"]
    )
    op.create_index(
        "ix_localagent_span_identity_trace", "localagent_external_span_identity", ["external_trace_id"]
    )

    op.create_table(
        "localagent_trace_envelope_sidecars",
        sa.Column("envelope_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("external_run_id", sa.String(128), nullable=False),
        sa.Column("external_trace_id", sa.String(128), nullable=False),
        sa.Column("external_span_id", sa.String(128), nullable=False),
        sa.Column("external_parent_span_id", sa.String(128), nullable=True),
        sa.Column("step_id", sa.String(128), nullable=True),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("component", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        # Authoritative lossless duration storage (P1-06): PostgreSQL NUMERIC
        # without precision/scale; the migration was still untracked/unreleased
        # when this replaced the original float8, so it is rewritten in place.
        sa.Column("duration_ms", sa.Numeric(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("attributes", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("contract_identity", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("contract_fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_payload_digest", sa.String(64), nullable=False),
        sa.Column("internal_trace_uuid", sa.UUID(), nullable=False),
        sa.Column("internal_span_uuid", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("envelope_id", name="pk_localagent_trace_envelope_sidecars"),
        sa.UniqueConstraint("external_span_id", name="uq_localagent_sidecar_span"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_localagent_sidecar_project", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["external_span_id"],
            ["localagent_external_span_identity.external_span_id"],
            name="fk_localagent_sidecar_span_identity",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('OK','ERROR','CANCELLED','TIMED_OUT')",
            name="ck_localagent_sidecar_status",
        ),
        sa.CheckConstraint(
            "completed_at >= started_at",
            name="ck_localagent_sidecar_time_order",
        ),
        # PostgreSQL NUMERIC can store NaN/±Infinity; NaN compares GREATER than
        # every finite value, so ``>= 0`` alone would accept NaN.  ``< 'Infinity'``
        # rejects +Infinity AND NaN; ``>= 0`` rejects negatives and -Infinity.
        sa.CheckConstraint(
            "duration_ms >= 0 AND duration_ms < 'Infinity'::numeric",
            name="ck_localagent_sidecar_duration",
        ),
        sa.CheckConstraint(
            "(status = 'OK' AND error_code IS NULL) OR (status <> 'OK' AND error_code IS NOT NULL)",
            name="ck_localagent_sidecar_status_error",
        ),
    )
    op.create_index(
        "ix_localagent_sidecar_project_created", "localagent_trace_envelope_sidecars", ["project_id", "created_at"]
    )
    op.create_index(
        "ix_localagent_sidecar_trace", "localagent_trace_envelope_sidecars", ["external_trace_id"]
    )

    _immutable_trigger(
        "localagent_external_trace_identity",
        "trg_localagent_trace_identity_immutable",
        "reject_localagent_trace_identity_update",
    )
    _immutable_trigger(
        "localagent_external_span_identity",
        "trg_localagent_span_identity_immutable",
        "reject_localagent_span_identity_update",
    )
    _immutable_trigger(
        "localagent_trace_envelope_sidecars",
        "trg_localagent_sidecar_immutable",
        "reject_localagent_sidecar_update",
    )


def downgrade() -> None:
    """Remove only the LocalAgent compatibility bounded-context schema."""
    op.execute("DROP TRIGGER IF EXISTS trg_localagent_sidecar_immutable ON localagent_trace_envelope_sidecars")
    op.execute("DROP FUNCTION IF EXISTS reject_localagent_sidecar_update()")
    op.execute("DROP TRIGGER IF EXISTS trg_localagent_span_identity_immutable ON localagent_external_span_identity")
    op.execute("DROP FUNCTION IF EXISTS reject_localagent_span_identity_update()")
    op.execute("DROP TRIGGER IF EXISTS trg_localagent_trace_identity_immutable ON localagent_external_trace_identity")
    op.execute("DROP FUNCTION IF EXISTS reject_localagent_trace_identity_update()")
    op.drop_table("localagent_trace_envelope_sidecars")
    op.drop_table("localagent_external_span_identity")
    op.drop_table("localagent_external_trace_identity")
