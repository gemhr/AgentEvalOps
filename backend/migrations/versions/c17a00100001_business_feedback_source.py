"""新增受限业务反馈来源；不改既有 Case、GT 或结果。"""
from alembic import op

revision = 'c17a00100001'
down_revision = 'c13a00300001'
branch_labels = None
depends_on = None

OLD = """(source_type IN ('OFFLINE_RESULT','CALIBRATION_SAMPLE') AND offline_result_id IS NOT NULL AND online_result_id IS NULL AND observation_id IS NULL AND gate_id IS NULL AND controlled_case_id IS NULL AND controlled_case_version IS NULL)
 OR (source_type='ONLINE_RESULT' AND offline_result_id IS NULL AND online_result_id IS NOT NULL AND observation_id IS NOT NULL AND gate_id IS NULL AND controlled_case_id IS NULL AND controlled_case_version IS NULL)
 OR (source_type='FAILURE_CANDIDATE' AND offline_result_id IS NULL AND observation_id IS NOT NULL AND gate_id IS NULL AND controlled_case_id IS NULL AND controlled_case_version IS NULL)
 OR (source_type='GATE_DECISION' AND offline_result_id IS NULL AND online_result_id IS NULL AND observation_id IS NULL AND gate_id IS NOT NULL AND controlled_case_id IS NULL AND controlled_case_version IS NULL)
 OR (source_type='CONTROLLED_CASE' AND offline_result_id IS NULL AND online_result_id IS NULL AND observation_id IS NULL AND gate_id IS NULL AND controlled_case_id IS NOT NULL AND controlled_case_version IS NOT NULL)"""


def upgrade():
    op.execute("""CREATE TABLE evaluation_business_feedback (
      project_id uuid NOT NULL REFERENCES projects(id), id uuid NOT NULL,
      incident_id uuid NOT NULL, digest text NOT NULL CHECK(length(digest)=64),
      source_bytes bytea NOT NULL CHECK(octet_length(source_bytes) BETWEEN 1 AND 32768),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(project_id,id));
      CREATE TRIGGER business_feedback_immutable BEFORE UPDATE OR DELETE ON evaluation_business_feedback
      FOR EACH ROW EXECUTE FUNCTION g1_reject_mutation();
      ALTER TABLE evaluation_review_items DROP CONSTRAINT wp07_review_source;""")
    op.execute("ALTER TABLE evaluation_review_items ADD CONSTRAINT wp07_review_source CHECK(" + OLD + " OR (source_type='BUSINESS_FEEDBACK' AND offline_result_id IS NULL AND online_result_id IS NULL AND observation_id IS NULL AND gate_id IS NULL AND controlled_case_id IS NULL AND controlled_case_version IS NULL))")


def downgrade():
    # 已有审核引用时拒绝降级，不删除历史审核/Case/GT。
    op.execute("ALTER TABLE evaluation_review_items DROP CONSTRAINT wp07_review_source")
    op.execute("ALTER TABLE evaluation_review_items ADD CONSTRAINT wp07_review_source CHECK(" + OLD + ")")
    op.execute("DROP TABLE evaluation_business_feedback")
