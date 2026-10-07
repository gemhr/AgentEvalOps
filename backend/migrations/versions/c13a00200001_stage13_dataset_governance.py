"""Stage13 typed GT 审核和反馈；复用既有 Review claim 与 catalog owner."""

from alembic import op

revision = "c13a00200001"
down_revision = "c13a00100001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增受控 Case 审核来源，不回填历史评测或审核事实."""
    op.execute("""
    ALTER TABLE evaluation_review_items DROP CONSTRAINT evaluation_review_items_source_type_check;
    ALTER TABLE evaluation_review_items DROP CONSTRAINT evaluation_review_items_check;
    ALTER TABLE evaluation_review_items ADD COLUMN controlled_case_id uuid;
    ALTER TABLE evaluation_review_items ADD COLUMN controlled_case_version text;
    ALTER TABLE evaluation_review_items ADD CONSTRAINT wp07_review_source CHECK(
      (source_type IN ('OFFLINE_RESULT','CALIBRATION_SAMPLE') AND offline_result_id IS NOT NULL AND online_result_id IS NULL AND observation_id IS NULL AND gate_id IS NULL AND controlled_case_id IS NULL AND controlled_case_version IS NULL)
      OR (source_type='ONLINE_RESULT' AND offline_result_id IS NULL AND online_result_id IS NOT NULL AND observation_id IS NOT NULL AND gate_id IS NULL AND controlled_case_id IS NULL AND controlled_case_version IS NULL)
      OR (source_type='FAILURE_CANDIDATE' AND offline_result_id IS NULL AND observation_id IS NOT NULL AND gate_id IS NULL AND controlled_case_id IS NULL AND controlled_case_version IS NULL)
      OR (source_type='GATE_DECISION' AND offline_result_id IS NULL AND online_result_id IS NULL AND observation_id IS NULL AND gate_id IS NOT NULL AND controlled_case_id IS NULL AND controlled_case_version IS NULL)
      OR (source_type='CONTROLLED_CASE' AND offline_result_id IS NULL AND online_result_id IS NULL AND observation_id IS NULL AND gate_id IS NULL AND controlled_case_id IS NOT NULL AND controlled_case_version IS NOT NULL));
    ALTER TABLE evaluation_review_items ADD FOREIGN KEY(project_id,controlled_case_id,controlled_case_version)
      REFERENCES evaluation_case_versions(project_id,entity_id,version);
    CREATE TABLE evaluation_stage13_gt_reviews (
      id uuid PRIMARY KEY, project_id uuid NOT NULL, item_id uuid NOT NULL,
      case_id uuid NOT NULL, case_version text NOT NULL, annotation_id uuid NOT NULL,
      golden_id uuid, state text NOT NULL CHECK(state IN ('CONFIRMED','CORRECTED','AMBIGUOUS','INSUFFICIENT_EVIDENCE','REJECTED')),
      decision_bytes bytea NOT NULL, intent_digest text NOT NULL, decision_digest text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(project_id,id), UNIQUE(project_id,item_id,annotation_id),
      FOREIGN KEY(project_id,item_id) REFERENCES evaluation_review_items(project_id,id),
      FOREIGN KEY(project_id,case_id,case_version) REFERENCES evaluation_case_versions(project_id,entity_id,version),
      FOREIGN KEY(project_id,annotation_id) REFERENCES evaluation_human_annotations(project_id,id),
      FOREIGN KEY(project_id,golden_id) REFERENCES evaluation_golden_labels(project_id,id)
    );
    CREATE TABLE evaluation_stage13_feedback (
      id uuid PRIMARY KEY, project_id uuid NOT NULL, case_id uuid NOT NULL, case_version text NOT NULL,
      result_id uuid NOT NULL, gate_id uuid NOT NULL, feedback_bytes bytea NOT NULL,
      intent_digest text NOT NULL, feedback_digest text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY(project_id,case_id,case_version) REFERENCES evaluation_case_versions(project_id,entity_id,version),
      FOREIGN KEY(project_id,result_id) REFERENCES evaluation_results(project_id,id),
      FOREIGN KEY(project_id,gate_id) REFERENCES evaluation_gate_receipts(project_id,gate_id)
    );
    CREATE FUNCTION wp07_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'WP07_FACT_IMMUTABLE' USING ERRCODE='55000'; END $$;
    CREATE TRIGGER wp07_review_immutable BEFORE UPDATE OR DELETE ON evaluation_stage13_gt_reviews
      FOR EACH ROW EXECUTE FUNCTION wp07_immutable();
    CREATE TRIGGER wp07_review_no_truncate BEFORE TRUNCATE ON evaluation_stage13_gt_reviews
      FOR EACH STATEMENT EXECUTE FUNCTION wp07_immutable();
    CREATE TRIGGER wp07_feedback_immutable BEFORE UPDATE OR DELETE ON evaluation_stage13_feedback
      FOR EACH ROW EXECUTE FUNCTION wp07_immutable();
    CREATE TRIGGER wp07_feedback_no_truncate BEFORE TRUNCATE ON evaluation_stage13_feedback
      FOR EACH STATEMENT EXECUTE FUNCTION wp07_immutable();
    """)


def downgrade() -> None:
    """保留已发布 Golden/反馈和审核 lineage."""
    raise RuntimeError("STAGE13_GOVERNANCE_HISTORY_DOWNGRADE_FORBIDDEN")
