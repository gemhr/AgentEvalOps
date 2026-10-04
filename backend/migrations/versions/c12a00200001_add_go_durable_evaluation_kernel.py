"""Stage12 G2 durable kernel；保留历史行，仅添加新合同约束."""
from alembic import op

revision = "c12a00200001"
down_revision = "c12a00100001"
branch_labels = None
depends_on = None


def upgrade():
    """添加 G2 持久化合同与保护约束."""
    op.execute(r"""
CREATE TABLE evaluation_writer_control (
 domain text PRIMARY KEY CHECK(domain='evaluation'), contract_version text NOT NULL,
 required_schema_version text NOT NULL, mode text NOT NULL CHECK(mode IN ('PYTHON_ACTIVE','PYTHON_DRAINING','BARRIER','GO_ACTIVE','GO_DRAINING')),
 writer_epoch bigint NOT NULL CHECK(writer_epoch>0), active_writer text NOT NULL CHECK(active_writer IN ('PYTHON','GO','NONE')),
 cutover_id uuid, changed_at timestamptz NOT NULL DEFAULT clock_timestamp(), operator_ref text NOT NULL,
 CHECK((mode IN ('PYTHON_ACTIVE','PYTHON_DRAINING') AND active_writer='PYTHON') OR (mode IN ('GO_ACTIVE','GO_DRAINING') AND active_writer='GO') OR (mode='BARRIER' AND active_writer='NONE'))
);
INSERT INTO evaluation_writer_control VALUES('evaluation','stage12.durable-evaluation.v1','c12a00200001','PYTHON_ACTIVE',1,'PYTHON',NULL,clock_timestamp(),'migration');
ALTER TABLE evaluation_runs ADD COLUMN contract_version text;
ALTER TABLE evaluation_runs ADD COLUMN creation_command_id uuid;
ALTER TABLE evaluation_runs ADD COLUMN creation_intent_digest text;
ALTER TABLE evaluation_runs ADD COLUMN source_run_id uuid;
ALTER TABLE evaluation_runs ADD COLUMN rerun_reason text;
ALTER TABLE evaluation_runs ADD COLUMN kernel_snapshot jsonb;
ALTER TABLE evaluation_runs ADD COLUMN kernel_bytes bytea;
ALTER TABLE evaluation_runs ADD COLUMN writer_epoch bigint;
ALTER TABLE evaluation_runs ADD CONSTRAINT g2_run_contract CHECK(contract_version IS NULL OR COALESCE((contract_version='stage12.durable-evaluation.v1' AND creation_command_id IS NOT NULL AND creation_intent_digest IS NOT NULL AND kernel_snapshot IS NOT NULL AND kernel_bytes IS NOT NULL AND writer_epoch IS NOT NULL AND jsonb_array_length(kernel_snapshot->'Input'->'ordered_manifest')>0 AND jsonb_array_length(kernel_snapshot->'Input'->'evaluator_specs')>0),false));
ALTER TABLE evaluation_runs ADD CONSTRAINT g2_source_run FOREIGN KEY(project_id,source_run_id) REFERENCES evaluation_runs(project_id,id) ON DELETE RESTRICT;
ALTER TABLE evaluation_runs ADD CONSTRAINT g2_source_shape CHECK(source_run_id IS NULL OR (source_run_id<>id AND length(rerun_reason)>0));
CREATE UNIQUE INDEX g2_creation_command ON evaluation_runs(project_id,creation_command_id) WHERE creation_command_id IS NOT NULL;
ALTER TABLE evaluation_attempts ADD COLUMN writer_epoch bigint;
ALTER TABLE evaluation_attempts ADD COLUMN contract_version text;
ALTER TABLE evaluation_attempts ADD COLUMN retry_intent_digest text;
ALTER TABLE evaluation_attempts ADD COLUMN retry_command_id uuid;
ALTER TABLE evaluation_attempts ADD COLUMN catalog_case_id uuid;
ALTER TABLE evaluation_attempts ADD COLUMN input_origin text;
ALTER TABLE evaluation_attempts ADD CONSTRAINT g2_attempt_slot UNIQUE(project_id,run_id,id,case_id,case_version);
ALTER TABLE evaluation_attempts ADD CONSTRAINT g2_retry_parent FOREIGN KEY(project_id,run_id,retry_of_attempt_id,case_id,case_version) REFERENCES evaluation_attempts(project_id,run_id,id,case_id,case_version) ON DELETE RESTRICT NOT VALID;
ALTER TABLE evaluation_attempts ADD CONSTRAINT g2_case_catalog FOREIGN KEY(project_id,catalog_case_id,case_version) REFERENCES evaluation_case_versions(project_id,entity_id,version) ON DELETE RESTRICT;
ALTER TABLE evaluation_attempts ADD CONSTRAINT g2_attempt_contract CHECK(contract_version IS NULL OR COALESCE((contract_version='stage12.durable-evaluation.v1' AND ((input_origin='PUBLISHED_CATALOG' AND catalog_case_id IS NOT NULL AND case_id=catalog_case_id::text) OR (input_origin='LEGACY_SNAPSHOT' AND catalog_case_id IS NULL)) AND (status='PENDING' OR writer_epoch IS NOT NULL)),false));
CREATE TABLE evaluation_evaluator_works (
 id uuid PRIMARY KEY, project_id uuid NOT NULL, run_id uuid NOT NULL, attempt_id uuid NOT NULL,
 case_id varchar(255) NOT NULL, case_version varchar(255) NOT NULL,
 evaluator_id varchar(255) NOT NULL, evaluator_version varchar(255) NOT NULL,
 spec_digest text NOT NULL, spec_version text NOT NULL, required boolean NOT NULL,
 status text NOT NULL CHECK(status IN ('PENDING','CLAIMED','COMPLETED')),
 claim_token uuid UNIQUE, writer_epoch bigint, owner_ref text, lease_expires_at timestamptz,
 claim_count integer NOT NULL DEFAULT 0 CHECK(claim_count>=0), evaluation_attempt_count integer NOT NULL DEFAULT 0 CHECK(evaluation_attempt_count>=0),
 next_available_at timestamptz NOT NULL DEFAULT clock_timestamp(), last_error_category text, last_error_reason text,
 provider_call_count integer NOT NULL DEFAULT 0 CHECK(provider_call_count>=0), bounded_call_provenance jsonb NOT NULL DEFAULT '[]',
 result_id uuid, metadata jsonb NOT NULL, contract_kind text NOT NULL CHECK(contract_kind IN ('STAGE12_CANONICAL','LEGACY')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), claimed_at timestamptz, completed_at timestamptz,
 CONSTRAINT g2_work_slot UNIQUE(project_id,run_id,attempt_id,case_id,case_version,evaluator_id,evaluator_version),
 CONSTRAINT g2_work_full UNIQUE(project_id,run_id,attempt_id,case_id,case_version,evaluator_id,evaluator_version,id),
 CONSTRAINT g2_work_attempt FOREIGN KEY(project_id,run_id,attempt_id,case_id,case_version) REFERENCES evaluation_attempts(project_id,run_id,id,case_id,case_version) ON DELETE RESTRICT,
 CONSTRAINT g2_work_complete CHECK((status='COMPLETED' AND result_id IS NOT NULL AND completed_at IS NOT NULL) OR (status IN ('PENDING','CLAIMED') AND result_id IS NULL AND completed_at IS NULL)),
 CONSTRAINT g2_work_claim CHECK((status='PENDING' AND claim_token IS NULL AND lease_expires_at IS NULL AND owner_ref IS NULL) OR (status='CLAIMED' AND claim_token IS NOT NULL AND writer_epoch IS NOT NULL AND lease_expires_at IS NOT NULL AND owner_ref IS NOT NULL) OR status='COMPLETED'),
 CONSTRAINT g2_call_budget CHECK(jsonb_typeof(bounded_call_provenance)='array' AND provider_call_count=jsonb_array_length(bounded_call_provenance) AND provider_call_count<=COALESCE((metadata->'Spec'->'definition'->'execution_budget'->>'max_provider_calls')::int,0) AND octet_length(bounded_call_provenance::text)<=1048576),
 CONSTRAINT g2_evaluation_budget CHECK(evaluation_attempt_count<=COALESCE((metadata->'Spec'->'definition'->'retry_policy'->>'max_evaluation_attempts')::int,0))
);
ALTER TABLE evaluation_results ADD COLUMN work_id uuid;
ALTER TABLE evaluation_results ADD COLUMN contract_version text;
ALTER TABLE evaluation_results ADD COLUMN kernel_value jsonb;
ALTER TABLE evaluation_results ADD COLUMN kernel_receipt jsonb;
ALTER TABLE evaluation_results ADD CONSTRAINT g2_result_contract CHECK(contract_version IS NULL OR (contract_version='stage12.durable-evaluation.v1' AND work_id IS NOT NULL AND kernel_value IS NOT NULL AND kernel_receipt IS NOT NULL));
ALTER TABLE evaluation_results ADD CONSTRAINT g2_result_full UNIQUE(project_id,run_id,attempt_id,case_id,case_version,evaluator_id,evaluator_version,id);
ALTER TABLE evaluation_results ADD CONSTRAINT g2_result_work FOREIGN KEY(project_id,run_id,attempt_id,case_id,case_version,evaluator_id,evaluator_version,work_id) REFERENCES evaluation_evaluator_works(project_id,run_id,attempt_id,case_id,case_version,evaluator_id,evaluator_version,id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE evaluation_evaluator_works ADD CONSTRAINT g2_work_result FOREIGN KEY(project_id,run_id,attempt_id,case_id,case_version,evaluator_id,evaluator_version,result_id) REFERENCES evaluation_results(project_id,run_id,attempt_id,case_id,case_version,evaluator_id,evaluator_version,id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
CREATE UNIQUE INDEX g2_one_result_per_work ON evaluation_results(work_id) WHERE work_id IS NOT NULL;
CREATE INDEX g2_pending_execution ON evaluation_attempts(created_at,id) WHERE status='PENDING';
CREATE INDEX g2_pending_work ON evaluation_evaluator_works(next_available_at,id) WHERE status='PENDING';
CREATE INDEX g2_expired_work ON evaluation_evaluator_works(lease_expires_at,id) WHERE status='CLAIMED';
CREATE INDEX g2_running_runs ON evaluation_runs(created_at,id) WHERE status='RUNNING';
CREATE TABLE evaluation_legacy_import_receipts(project_id uuid NOT NULL,run_id uuid NOT NULL,cutover_id uuid NOT NULL,intent_digest text NOT NULL,inventory_ref text NOT NULL,receipt jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(project_id,run_id,cutover_id),FOREIGN KEY(project_id,run_id) REFERENCES evaluation_runs(project_id,id) ON DELETE RESTRICT);
CREATE FUNCTION g2_control_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF current_user <> (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=TG_RELID) THEN RAISE EXCEPTION 'operator only writer control' USING ERRCODE='42501'; END IF;
 IF TG_OP<>'UPDATE' THEN RAISE EXCEPTION 'writer control cannot be removed' USING ERRCODE='55000'; END IF;
 IF NEW.writer_epoch<OLD.writer_epoch OR (NEW.mode IN ('BARRIER','GO_ACTIVE') AND NEW.writer_epoch=OLD.writer_epoch) THEN RAISE EXCEPTION 'writer epoch must increase' USING ERRCODE='55000'; END IF;
 IF NOT ((OLD.mode='PYTHON_ACTIVE' AND NEW.mode='PYTHON_DRAINING') OR (OLD.mode IN ('PYTHON_DRAINING','GO_DRAINING') AND NEW.mode='BARRIER') OR (OLD.mode='BARRIER' AND NEW.mode='GO_ACTIVE') OR (OLD.mode='GO_ACTIVE' AND NEW.mode='GO_DRAINING')) THEN RAISE EXCEPTION 'invalid writer transition' USING ERRCODE='55000'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER g2_writer_guard BEFORE UPDATE OR DELETE ON evaluation_writer_control FOR EACH ROW EXECUTE FUNCTION g2_control_guard();
CREATE FUNCTION g2_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE mode_now text; writer_now text; epoch_now bigint;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'evaluation run deletion forbidden' USING ERRCODE='55000'; END IF;
 IF NEW.contract_version IS NOT NULL THEN
  SELECT mode,active_writer,writer_epoch INTO mode_now,writer_now,epoch_now FROM evaluation_writer_control WHERE domain='evaluation' FOR SHARE;
  IF writer_now<>'GO' OR mode_now NOT IN ('GO_ACTIVE','GO_DRAINING') OR (TG_OP='INSERT' AND mode_now<>'GO_ACTIVE') THEN RAISE EXCEPTION 'writer barrier' USING ERRCODE='55000'; END IF;
  IF TG_OP='INSERT' AND NEW.writer_epoch<>epoch_now THEN RAISE EXCEPTION 'writer epoch mismatch' USING ERRCODE='55000'; END IF;
 ELSIF TG_OP='INSERT' AND current_user<>(SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=TG_RELID) THEN
  SELECT mode INTO mode_now FROM evaluation_writer_control WHERE domain='evaluation' FOR SHARE;
  IF mode_now NOT IN ('PYTHON_ACTIVE','PYTHON_DRAINING') THEN RAISE EXCEPTION 'runtime cannot create legacy run after barrier' USING ERRCODE='42501'; END IF;
 END IF;
 IF TG_OP='INSERT' THEN RETURN NEW; END IF;
 IF OLD.status IN ('COMPLETED','FAILED','OUTCOME_UNKNOWN') THEN RAISE EXCEPTION 'terminal run immutable' USING ERRCODE='55000'; END IF;
 IF (to_jsonb(NEW)-ARRAY['status','status_reason','started_at','finished_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','status_reason','started_at','finished_at']) THEN RAISE EXCEPTION 'run snapshot immutable' USING ERRCODE='55000'; END IF;
 IF NOT (NEW.status=OLD.status OR (OLD.status='PENDING' AND NEW.status='RUNNING') OR (OLD.status='RUNNING' AND NEW.status IN ('COMPLETED','FAILED','OUTCOME_UNKNOWN'))) THEN RAISE EXCEPTION 'invalid run transition' USING ERRCODE='55000'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER g2_run_immutable BEFORE INSERT OR UPDATE OR DELETE ON evaluation_runs FOR EACH ROW EXECUTE FUNCTION g2_run_guard();
CREATE FUNCTION g2_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent_status text; mode_now text; epoch_now bigint; writer_now text; canonical boolean;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'evaluation child deletion forbidden' USING ERRCODE='55000'; END IF;
 SELECT status,contract_version IS NOT NULL INTO parent_status,canonical FROM evaluation_runs WHERE project_id=NEW.project_id AND id=NEW.run_id;
 IF parent_status IN ('COMPLETED','FAILED','OUTCOME_UNKNOWN') THEN
  SELECT mode INTO mode_now FROM evaluation_writer_control WHERE domain='evaluation';
  IF NOT (TG_TABLE_NAME='evaluation_evaluator_works' AND TG_OP='INSERT' AND NEW.status='COMPLETED' AND mode_now='BARRIER' AND to_jsonb(NEW)->>'contract_kind'='LEGACY' AND current_user=(SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=TG_RELID)) THEN RAISE EXCEPTION 'terminal run sealed' USING ERRCODE='55000'; END IF;
 END IF;
 IF TG_OP='UPDATE' THEN
  IF OLD.status IN ('TERMINAL','COMPLETED') THEN RAISE EXCEPTION 'terminal child immutable' USING ERRCODE='55000'; END IF;
  IF TG_TABLE_NAME='evaluation_attempts' AND (to_jsonb(NEW)-ARRAY['status','claim_token','worker_ref','task_ref','writer_epoch','claimed_at','started_at','finished_at','lease_expires_at','execution_outcome_kind','output_artifact_ref','outcome_evidence_refs','error_category','reason','outcome_metadata']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','claim_token','worker_ref','task_ref','writer_epoch','claimed_at','started_at','finished_at','lease_expires_at','execution_outcome_kind','output_artifact_ref','outcome_evidence_refs','error_category','reason','outcome_metadata']) THEN RAISE EXCEPTION 'attempt input immutable' USING ERRCODE='55000'; END IF;
  IF TG_TABLE_NAME='evaluation_evaluator_works' THEN
   IF (to_jsonb(NEW)-ARRAY['status','claim_token','writer_epoch','owner_ref','lease_expires_at','claim_count','evaluation_attempt_count','next_available_at','last_error_category','last_error_reason','provider_call_count','bounded_call_provenance','result_id','metadata','claimed_at','completed_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','claim_token','writer_epoch','owner_ref','lease_expires_at','claim_count','evaluation_attempt_count','next_available_at','last_error_category','last_error_reason','provider_call_count','bounded_call_provenance','result_id','metadata','claimed_at','completed_at']) OR to_jsonb(NEW)->'metadata'->'Spec' IS DISTINCT FROM to_jsonb(OLD)->'metadata'->'Spec' THEN RAISE EXCEPTION 'work binding immutable' USING ERRCODE='55000'; END IF;
  END IF;
 END IF;
 IF canonical THEN
  IF TG_TABLE_NAME='evaluation_attempts' AND to_jsonb(NEW)->>'contract_version' IS NULL THEN RAISE EXCEPTION 'canonical attempt contract required' USING ERRCODE='55000'; END IF;
  SELECT mode,writer_epoch,active_writer INTO mode_now,epoch_now,writer_now FROM evaluation_writer_control WHERE domain='evaluation' FOR SHARE;
  IF writer_now<>'GO' OR mode_now NOT IN ('GO_ACTIVE','GO_DRAINING') THEN RAISE EXCEPTION 'writer barrier' USING ERRCODE='55000'; END IF;
  IF NEW.writer_epoch IS NOT NULL AND NEW.writer_epoch<>epoch_now THEN RAISE EXCEPTION 'writer epoch mismatch' USING ERRCODE='55000'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER g2_attempt_guard BEFORE INSERT OR UPDATE OR DELETE ON evaluation_attempts FOR EACH ROW EXECUTE FUNCTION g2_child_guard();
CREATE TRIGGER g2_work_guard BEFORE INSERT OR UPDATE OR DELETE ON evaluation_evaluator_works FOR EACH ROW EXECUTE FUNCTION g2_child_guard();
CREATE FUNCTION g2_result_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s text; canonical boolean; writer_now text; mode_now text;
BEGIN
 SELECT status,contract_version IS NOT NULL INTO s,canonical FROM evaluation_runs WHERE project_id=NEW.project_id AND id=NEW.run_id;
 IF s<>'RUNNING' THEN RAISE EXCEPTION 'result requires running run' USING ERRCODE='55000'; END IF;
 IF canonical AND NEW.contract_version IS NULL THEN RAISE EXCEPTION 'canonical result contract required' USING ERRCODE='55000'; END IF;
 IF NEW.contract_version IS NOT NULL THEN
  SELECT mode,active_writer INTO mode_now,writer_now FROM evaluation_writer_control WHERE domain='evaluation' FOR SHARE;
  IF writer_now<>'GO' OR mode_now NOT IN ('GO_ACTIVE','GO_DRAINING') THEN RAISE EXCEPTION 'writer barrier' USING ERRCODE='55000'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER g2_result_insert BEFORE INSERT ON evaluation_results FOR EACH ROW EXECUTE FUNCTION g2_result_insert_guard();
CREATE FUNCTION g2_result_work_atomic() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.contract_version IS NOT NULL AND NOT EXISTS(SELECT 1 FROM evaluation_evaluator_works WHERE id=NEW.work_id AND result_id=NEW.id AND status='COMPLETED') THEN RAISE EXCEPTION 'result/work atomic relation violated' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER g2_result_work_atomic AFTER INSERT ON evaluation_results DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g2_result_work_atomic();
CREATE FUNCTION g2_protect_project() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF EXISTS(SELECT 1 FROM evaluation_runs WHERE project_id=OLD.id) THEN RAISE EXCEPTION 'PROTECTED_EVALUATION_HISTORY' USING ERRCODE='55000'; END IF; RETURN OLD; END $$;
CREATE TRIGGER g2_project_delete BEFORE DELETE ON projects FOR EACH ROW EXECUTE FUNCTION g2_protect_project();
CREATE TRIGGER g2_import_receipt_immutable BEFORE UPDATE OR DELETE ON evaluation_legacy_import_receipts FOR EACH ROW EXECUTE FUNCTION g1_reject_mutation();
-- 原历史列不重写；仅将 canonical 父关系的级联删除改为明确 RESTRICT。
DO $$ DECLARE c record; definition text; BEGIN
 FOR c IN SELECT conname,conrelid::regclass AS tbl,oid FROM pg_constraint WHERE contype='f' AND confdeltype='c' AND conrelid IN ('evaluation_runs'::regclass,'evaluation_attempts'::regclass,'evaluation_results'::regclass) LOOP
  definition:=replace(pg_get_constraintdef(c.oid),'ON DELETE CASCADE','ON DELETE RESTRICT');
  EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',c.tbl,c.conname);
  EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s',c.tbl,c.conname,definition);
 END LOOP;
END $$;
""")
    for table in ("evaluation_runs", "evaluation_attempts", "evaluation_evaluator_works", "evaluation_results", "evaluation_writer_control", "evaluation_legacy_import_receipts"):
        op.execute(f"CREATE TRIGGER g2_no_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation()")


def downgrade():
    """拒绝将 G2 持久化事实降级给不兼容的 Python writer."""
    raise RuntimeError("G2 durable facts cannot be downgraded to an incompatible Python writer")
