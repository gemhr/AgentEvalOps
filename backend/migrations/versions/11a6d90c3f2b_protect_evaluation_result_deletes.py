"""Prevent deletion of canonical evaluation results.

Revision ID: 11a6d90c3f2b
Revises: d3a4e5f6b7c8
"""

from typing import Sequence, Union

from alembic import op

revision: str = "11a6d90c3f2b"
down_revision: Union[str, None] = "d3a4e5f6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Keep canonical result rows when callers delete a parent resource."""
    op.execute("""
        CREATE FUNCTION reject_evaluation_result_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'evaluation_results rows cannot be deleted';
        END; $$
    """)
    op.execute("""
        CREATE TRIGGER trg_evaluation_results_no_delete
        BEFORE DELETE ON evaluation_results
        FOR EACH ROW EXECUTE FUNCTION reject_evaluation_result_delete()
    """)


def downgrade() -> None:
    """Remove the delete guard."""
    op.execute("DROP TRIGGER IF EXISTS trg_evaluation_results_no_delete ON evaluation_results")
    op.execute("DROP FUNCTION IF EXISTS reject_evaluation_result_delete()")
