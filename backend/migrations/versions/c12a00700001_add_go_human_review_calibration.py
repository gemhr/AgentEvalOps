"""G7 独立人工标注、校准与受审查反馈事实."""

from alembic import op

revision = "c12a00700001"
down_revision = "c12a00600001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增 review owner，不改写历史自动结果或门禁."""
    op.execute(r"""
ALTER TABLE evaluation_results ADD CONSTRAINT g7_result_project_id UNIQUE(project_id,id);
ALTER TABLE evaluation_online_results ADD CONSTRAINT g7_online_result_project_id UNIQUE(project_id,id);
CREATE TABLE evaluation_review_items (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 schema_id uuid NOT NULL, schema_version text NOT NULL,
 source_type text NOT NULL CHECK(source_type IN ('OFFLINE_RESULT','ONLINE_RESULT','FAILURE_CANDIDATE','GATE_DECISION','CALIBRATION_SAMPLE')),
 source_key text NOT NULL, policy_digest text NOT NULL, intent_digest text NOT NULL,
 source_digest text NOT NULL, item_bytes bytea NOT NULL CHECK(octet_length(item_bytes) BETWEEN 1 AND 8388608),
 offline_result_id uuid, online_result_id uuid, observation_id uuid, gate_id uuid,
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','IN_REVIEW','COMPLETED','ADJUDICATION_REQUIRED')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), creation_xid bigint NOT NULL DEFAULT txid_current(),
 CHECK((source_type IN ('OFFLINE_RESULT','CALIBRATION_SAMPLE') AND offline_result_id IS NOT NULL AND online_result_id IS NULL AND observation_id IS NULL AND gate_id IS NULL)
 OR (source_type='ONLINE_RESULT' AND offline_result_id IS NULL AND online_result_id IS NOT NULL AND observation_id IS NOT NULL AND gate_id IS NULL)
 OR (source_type='FAILURE_CANDIDATE' AND offline_result_id IS NULL AND observation_id IS NOT NULL AND gate_id IS NULL)
 OR (source_type='GATE_DECISION' AND offline_result_id IS NULL AND online_result_id IS NULL AND observation_id IS NULL AND gate_id IS NOT NULL)),
 UNIQUE(project_id,id), UNIQUE(project_id,source_key,policy_digest),
 FOREIGN KEY(project_id,schema_id,schema_version) REFERENCES evaluation_metric_definition_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,offline_result_id) REFERENCES evaluation_results(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,online_result_id) REFERENCES evaluation_online_results(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,observation_id) REFERENCES evaluation_observations(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,gate_id) REFERENCES evaluation_gate_receipts(project_id,gate_id) ON DELETE RESTRICT
);
CREATE TABLE evaluation_review_slots (
 project_id uuid NOT NULL, item_id uuid NOT NULL, slot integer NOT NULL CHECK(slot IN (1,2)),
 reviewer_id text, reviewer_bytes bytea, token uuid, writer_epoch bigint, lease_expires_at timestamptz,
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','CLAIMED','COMPLETED')),
 annotation_id uuid, claims integer NOT NULL DEFAULT 0, expired integer NOT NULL DEFAULT 0, released integer NOT NULL DEFAULT 0,
 PRIMARY KEY(project_id,item_id,slot),
 UNIQUE(project_id,item_id,reviewer_id),
 FOREIGN KEY(project_id,item_id) REFERENCES evaluation_review_items(project_id,id) ON DELETE RESTRICT,
 CHECK(status!='CLAIMED' OR (reviewer_id IS NOT NULL AND token IS NOT NULL AND writer_epoch IS NOT NULL AND lease_expires_at IS NOT NULL)),
 CHECK((status='COMPLETED')=(annotation_id IS NOT NULL))
);
CREATE TABLE evaluation_human_annotations (
 id uuid PRIMARY KEY, project_id uuid NOT NULL, item_id uuid NOT NULL, slot integer NOT NULL,
 reviewer_id text NOT NULL, token uuid NOT NULL, intent_digest text NOT NULL,
 annotation_bytes bytea NOT NULL, supersedes uuid,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(project_id,id), UNIQUE(project_id,item_id,slot,id),
 FOREIGN KEY(project_id,item_id,slot) REFERENCES evaluation_review_slots(project_id,item_id,slot) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,supersedes) REFERENCES evaluation_human_annotations(project_id,id) ON DELETE RESTRICT,
 UNIQUE(project_id,supersedes)
);
ALTER TABLE evaluation_review_slots ADD CONSTRAINT g7_slot_annotation FOREIGN KEY(project_id,item_id,slot,annotation_id)
 REFERENCES evaluation_human_annotations(project_id,item_id,slot,id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE evaluation_adjudications (
 id uuid PRIMARY KEY, project_id uuid NOT NULL, item_id uuid NOT NULL, intent_digest text NOT NULL,
 adjudication_bytes bytea NOT NULL, supersedes uuid, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 creation_xid bigint NOT NULL DEFAULT txid_current(),
 UNIQUE(project_id,id), UNIQUE(project_id,supersedes),
 FOREIGN KEY(project_id,item_id) REFERENCES evaluation_review_items(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,supersedes) REFERENCES evaluation_adjudications(project_id,id) ON DELETE RESTRICT
);
CREATE TABLE evaluation_adjudication_inputs (
 project_id uuid NOT NULL, adjudication_id uuid NOT NULL, annotation_id uuid NOT NULL,
 PRIMARY KEY(project_id,adjudication_id,annotation_id),
 FOREIGN KEY(project_id,adjudication_id) REFERENCES evaluation_adjudications(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,annotation_id) REFERENCES evaluation_human_annotations(project_id,id) ON DELETE RESTRICT
);
CREATE TABLE evaluation_golden_labels (
 id uuid PRIMARY KEY, project_id uuid NOT NULL, item_id uuid NOT NULL, intent_digest text NOT NULL,
 golden_bytes bytea NOT NULL, adjudication_id uuid,
 creation_xid bigint NOT NULL DEFAULT txid_current(),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(project_id,id), UNIQUE(project_id,item_id,intent_digest),
 FOREIGN KEY(project_id,item_id) REFERENCES evaluation_review_items(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,adjudication_id) REFERENCES evaluation_adjudications(project_id,id) ON DELETE RESTRICT
);
CREATE TABLE evaluation_golden_annotation_inputs (
 project_id uuid NOT NULL, golden_id uuid NOT NULL, annotation_id uuid NOT NULL,
 PRIMARY KEY(project_id,golden_id,annotation_id),
 FOREIGN KEY(project_id,golden_id) REFERENCES evaluation_golden_labels(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,annotation_id) REFERENCES evaluation_human_annotations(project_id,id) ON DELETE RESTRICT
);
CREATE TABLE evaluation_calibration_snapshots (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 dataset_id uuid NOT NULL, dataset_version text NOT NULL, evaluator_id uuid NOT NULL, evaluator_version text NOT NULL,
 schema_id uuid NOT NULL, schema_version text NOT NULL, intent_digest text NOT NULL,
 snapshot_bytes bytea NOT NULL CHECK(octet_length(snapshot_bytes) BETWEEN 1 AND 67108864),
 creation_xid bigint NOT NULL DEFAULT txid_current(),
 UNIQUE(project_id,id),
 FOREIGN KEY(project_id,dataset_id,dataset_version) REFERENCES evaluation_dataset_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,evaluator_id,evaluator_version) REFERENCES evaluation_evaluator_definition_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,schema_id,schema_version) REFERENCES evaluation_metric_definition_versions(project_id,entity_id,version) ON DELETE RESTRICT
);
CREATE TABLE evaluation_calibration_samples (
 project_id uuid NOT NULL, calibration_id uuid NOT NULL, case_id uuid NOT NULL, case_version text NOT NULL, golden_id uuid,
 PRIMARY KEY(project_id,calibration_id,case_id),
 FOREIGN KEY(project_id,calibration_id) REFERENCES evaluation_calibration_snapshots(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,case_id,case_version) REFERENCES evaluation_case_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,golden_id) REFERENCES evaluation_golden_labels(project_id,id) ON DELETE RESTRICT
);
CREATE TABLE evaluation_calibration_reports (
 project_id uuid NOT NULL, id uuid NOT NULL, report_bytes bytea NOT NULL, report_digest text NOT NULL,
 PRIMARY KEY(project_id,id), created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(project_id,id) REFERENCES evaluation_calibration_snapshots(project_id,id) ON DELETE RESTRICT
);
CREATE TABLE evaluation_reviewed_case_drafts (
 id uuid PRIMARY KEY, project_id uuid NOT NULL, item_id uuid NOT NULL, golden_id uuid, supersedes uuid,
 draft_bytes bytea NOT NULL CHECK(octet_length(draft_bytes) BETWEEN 1 AND 8388608), intent_digest text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(project_id,id), UNIQUE(project_id,supersedes),
 FOREIGN KEY(project_id,item_id) REFERENCES evaluation_review_items(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,golden_id) REFERENCES evaluation_golden_labels(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,supersedes) REFERENCES evaluation_reviewed_case_drafts(project_id,id) ON DELETE RESTRICT
);
CREATE TABLE evaluation_review_case_publications (
 project_id uuid NOT NULL, draft_id uuid NOT NULL, case_id uuid NOT NULL, case_version text NOT NULL,
 PRIMARY KEY(project_id,draft_id),
 FOREIGN KEY(project_id,draft_id) REFERENCES evaluation_reviewed_case_drafts(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,case_id,case_version) REFERENCES evaluation_case_versions(project_id,entity_id,version) ON DELETE RESTRICT
);
CREATE TABLE evaluation_gate_exception_reviews (
 id uuid PRIMARY KEY, project_id uuid NOT NULL, item_id uuid NOT NULL, gate_id uuid NOT NULL,
 review_bytes bytea NOT NULL, intent_digest text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(project_id,id),
 FOREIGN KEY(project_id,item_id) REFERENCES evaluation_review_items(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,gate_id) REFERENCES evaluation_gate_receipts(project_id,gate_id) ON DELETE RESTRICT
);
CREATE FUNCTION g7_item_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR (to_jsonb(OLD)-'status') IS DISTINCT FROM (to_jsonb(NEW)-'status') THEN
  RAISE EXCEPTION 'review source immutable' USING ERRCODE='55000';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER g7_item_guard BEFORE UPDATE OR DELETE ON evaluation_review_items FOR EACH ROW EXECUTE FUNCTION g7_item_guard();
CREATE FUNCTION g7_slot_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR (OLD.status='COMPLETED' AND NEW IS DISTINCT FROM OLD) THEN
  RAISE EXCEPTION 'submitted slot immutable' USING ERRCODE='55000';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER g7_slot_guard BEFORE UPDATE OR DELETE ON evaluation_review_slots FOR EACH ROW EXECUTE FUNCTION g7_slot_guard();
CREATE FUNCTION g7_annotation_atomic() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a jsonb; old_a jsonb; item jsonb;
BEGIN
 a:=convert_from(NEW.annotation_bytes,'UTF8')::jsonb;
 SELECT convert_from(item_bytes,'UTF8')::jsonb INTO item FROM evaluation_review_items WHERE project_id=NEW.project_id AND id=NEW.item_id;
 IF a->>'ProjectID' IS DISTINCT FROM NEW.project_id::text OR a->>'ItemID' IS DISTINCT FROM NEW.item_id::text
 OR a->>'ID' IS DISTINCT FROM NEW.id::text OR (a->'Reviewer'->>'ID') IS DISTINCT FROM NEW.reviewer_id
 OR a->>'SourceDigest' IS DISTINCT FROM (SELECT source_digest FROM evaluation_review_items WHERE project_id=NEW.project_id AND id=NEW.item_id)
 OR a->'Schema' IS DISTINCT FROM item->'Command'->'Schema'
 OR a->>'SchemaDigest' IS DISTINCT FROM item->>'SchemaDigest'
 OR a->>'EvidenceDigest' IS DISTINCT FROM item->'Source'->>'EvidenceDigest'
 OR a->'EvidencePresented' IS DISTINCT FROM item->'Source'->'Evidence'
 OR a->'Protocol' IS DISTINCT FROM item->'Command'->'Policy'->'Protocol'
 THEN RAISE EXCEPTION 'annotation source binding invalid' USING ERRCODE='23514'; END IF;
 IF NEW.supersedes IS NULL THEN
  IF NOT EXISTS(SELECT 1 FROM evaluation_review_slots WHERE project_id=NEW.project_id AND item_id=NEW.item_id AND slot=NEW.slot AND status='COMPLETED' AND annotation_id=NEW.id AND token=NEW.token AND reviewer_id=NEW.reviewer_id) THEN
   RAISE EXCEPTION 'annotation and slot must complete atomically' USING ERRCODE='23514';
  END IF;
 ELSE
  SELECT convert_from(annotation_bytes,'UTF8')::jsonb INTO old_a FROM evaluation_human_annotations WHERE project_id=NEW.project_id AND id=NEW.supersedes AND item_id=NEW.item_id AND slot=NEW.slot AND reviewer_id=NEW.reviewer_id;
  IF old_a IS NULL OR a->>'EvidenceDigest' IS DISTINCT FROM old_a->>'EvidenceDigest' THEN
   RAISE EXCEPTION 'annotation correction lineage invalid' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER g7_annotation_atomic AFTER INSERT ON evaluation_human_annotations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g7_annotation_atomic();
CREATE FUNCTION g7_input_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE body jsonb; xid bigint; item uuid; valid boolean;
BEGIN
 CASE TG_TABLE_NAME
 WHEN 'evaluation_review_slots' THEN
  SELECT creation_xid,convert_from(item_bytes,'UTF8')::jsonb INTO xid,body FROM evaluation_review_items WHERE project_id=NEW.project_id AND id=NEW.item_id;
  valid:=NEW.slot<=(body->'Command'->'Policy'->>'Reviews')::integer;
 WHEN 'evaluation_adjudication_inputs' THEN
  SELECT creation_xid,item_id,convert_from(adjudication_bytes,'UTF8')::jsonb INTO xid,item,body FROM evaluation_adjudications WHERE project_id=NEW.project_id AND id=NEW.adjudication_id;
  valid:=(body->'Inputs' ? NEW.annotation_id::text) AND EXISTS(SELECT 1 FROM evaluation_human_annotations WHERE project_id=NEW.project_id AND id=NEW.annotation_id AND item_id=item);
 WHEN 'evaluation_golden_annotation_inputs' THEN
  SELECT creation_xid,item_id,convert_from(golden_bytes,'UTF8')::jsonb INTO xid,item,body FROM evaluation_golden_labels WHERE project_id=NEW.project_id AND id=NEW.golden_id;
  valid:=(body->'AnnotationIDs' ? NEW.annotation_id::text) AND EXISTS(SELECT 1 FROM evaluation_human_annotations WHERE project_id=NEW.project_id AND id=NEW.annotation_id AND item_id=item);
 WHEN 'evaluation_calibration_samples' THEN
  SELECT creation_xid,convert_from(snapshot_bytes,'UTF8')::jsonb INTO xid,body FROM evaluation_calibration_snapshots WHERE project_id=NEW.project_id AND id=NEW.calibration_id;
  valid:=EXISTS(SELECT 1 FROM jsonb_array_elements(body->'Pairs') p WHERE p->'Case'->>'entity_id'=NEW.case_id::text AND p->'Case'->>'version'=NEW.case_version AND (p->'Golden'->>'ID') IS NOT DISTINCT FROM NEW.golden_id::text);
 END CASE;
 IF xid IS DISTINCT FROM txid_current() OR valid IS DISTINCT FROM true THEN
  RAISE EXCEPTION 'review frozen input binding invalid or late insert' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION g7_input_complete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE expected integer; actual integer;
BEGIN
 CASE TG_TABLE_NAME
 WHEN 'evaluation_review_items' THEN
  expected:=(convert_from(NEW.item_bytes,'UTF8')::jsonb->'Command'->'Policy'->>'Reviews')::integer;
  SELECT count(*) INTO actual FROM evaluation_review_slots WHERE project_id=NEW.project_id AND item_id=NEW.id;
 WHEN 'evaluation_adjudications' THEN
  expected:=jsonb_array_length(convert_from(NEW.adjudication_bytes,'UTF8')::jsonb->'Inputs');
  SELECT count(*) INTO actual FROM evaluation_adjudication_inputs WHERE project_id=NEW.project_id AND adjudication_id=NEW.id;
 WHEN 'evaluation_golden_labels' THEN
  expected:=jsonb_array_length(convert_from(NEW.golden_bytes,'UTF8')::jsonb->'AnnotationIDs');
  SELECT count(*) INTO actual FROM evaluation_golden_annotation_inputs WHERE project_id=NEW.project_id AND golden_id=NEW.id;
 WHEN 'evaluation_calibration_snapshots' THEN
  expected:=jsonb_array_length(convert_from(NEW.snapshot_bytes,'UTF8')::jsonb->'Pairs');
  SELECT count(*) INTO actual FROM evaluation_calibration_samples WHERE project_id=NEW.project_id AND calibration_id=NEW.id;
 END CASE;
 IF expected IS NULL OR expected<1 OR expected IS DISTINCT FROM actual THEN
  RAISE EXCEPTION 'review frozen input manifest incomplete' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
CREATE INDEX g7_review_queue ON evaluation_review_items(project_id,status,created_at,id);
""")
    for table in (
        "evaluation_review_slots", "evaluation_adjudication_inputs", "evaluation_golden_annotation_inputs",
        "evaluation_calibration_samples",
    ):
        op.execute(f"CREATE TRIGGER g7_input_guard BEFORE INSERT ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION g7_input_guard()")
    for table in (
        "evaluation_review_items", "evaluation_adjudications", "evaluation_golden_labels",
        "evaluation_calibration_snapshots",
    ):
        op.execute(f"CREATE CONSTRAINT TRIGGER g7_input_complete AFTER INSERT ON {table} "
                   "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g7_input_complete()")
    for table in ("evaluation_review_items", "evaluation_review_slots"):
        op.execute(f"CREATE TRIGGER g7_no_truncate BEFORE TRUNCATE ON {table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation()")
    for table in (
        "evaluation_human_annotations", "evaluation_adjudications", "evaluation_adjudication_inputs",
        "evaluation_golden_labels", "evaluation_golden_annotation_inputs", "evaluation_calibration_snapshots",
        "evaluation_calibration_samples", "evaluation_calibration_reports", "evaluation_reviewed_case_drafts",
        "evaluation_review_case_publications", "evaluation_gate_exception_reviews",
    ):
        op.execute(f"CREATE TRIGGER g7_immutable BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION g1_reject_mutation()")
        op.execute(f"CREATE TRIGGER g7_no_truncate BEFORE TRUNCATE ON {table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation()")


def downgrade() -> None:
    """人工判断与校准历史需要显式兼容恢复方案."""
    raise RuntimeError("G7 review history requires an explicit compatible recovery plan")
