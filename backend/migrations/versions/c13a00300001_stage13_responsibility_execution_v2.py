"""允许责任合同v2执行请求，保留所有旧请求与不可变触发器."""

from alembic import op

revision = "c13a00300001"
down_revision = "c13a00200001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅扩展版本枚举；JSONB/bytes保存原有及新增合同字段."""
    op.execute("""
    ALTER TABLE evaluation_stage13_execution_requests
      DROP CONSTRAINT evaluation_stage13_execution_requests_policy_version_check;
    ALTER TABLE evaluation_stage13_execution_requests
      ADD CONSTRAINT evaluation_stage13_execution_requests_policy_version_check
      CHECK(policy_version IN ('stage13.attempt-execution.v1','stage13.attempt-execution.v2'));
    """)


def downgrade() -> None:
    """沿用既有执行历史禁止降级政策."""
    raise RuntimeError("STAGE13_EXECUTION_HISTORY_DOWNGRADE_FORBIDDEN")
