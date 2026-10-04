"""Stage12 G5 生产观测与独立在线评测事实."""

from alembic import op

revision = "c12a00500001"
down_revision = "c12a00300001"
branch_labels = None
depends_on = None


def upgrade():
    """新增在线 owner，不改变 G2 状态或合同版本."""
    op.execute(r"""
ALTER TABLE localagent_trace_envelope_sidecars ADD CONSTRAINT g5_receipt_scope UNIQUE(project_id,envelope_id);
CREATE TABLE evaluation_observations (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 kind text NOT NULL CHECK(kind IN ('TRACE','SPAN','PLAN','STEP','TOOL_CALL','RETRIEVAL')),
 source text NOT NULL, source_principal text NOT NULL, trust text NOT NULL CHECK(trust IN ('STRICT_AUTHENTICATED','NORMAL_OBSERVATION')),
 source_identity text NOT NULL, trace_id text NOT NULL, runtime_id text NOT NULL,
 schema_version text NOT NULL, digest text NOT NULL, canonical_bytes bytea NOT NULL, envelope_bytes bytea NOT NULL,
 observation_bytes bytea NOT NULL, receipt_id uuid,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), completed_at timestamptz NOT NULL,
 UNIQUE(project_id,id), UNIQUE(source,source_identity),
 FOREIGN KEY(project_id,receipt_id) REFERENCES localagent_trace_envelope_sidecars(project_id,envelope_id) ON DELETE RESTRICT,
 CHECK(trust<>'STRICT_AUTHENTICATED' OR receipt_id IS NOT NULL)
);
CREATE INDEX g5_observation_scan ON evaluation_observations(created_at,id);
CREATE TABLE evaluation_online_rules (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 name text NOT NULL, created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(project_id,id)
);
CREATE TABLE evaluation_online_rule_versions (
 project_id uuid NOT NULL, entity_id uuid NOT NULL, version text NOT NULL,
 canonical_bytes bytea NOT NULL, digest text NOT NULL, published_by text NOT NULL,
 published_at timestamptz NOT NULL DEFAULT clock_timestamp(), enabled boolean NOT NULL,
 PRIMARY KEY(project_id,entity_id,version),
 FOREIGN KEY(project_id,entity_id) REFERENCES evaluation_online_rules(project_id,id) ON DELETE RESTRICT
);
CREATE TABLE evaluation_online_bindings (
 project_id uuid NOT NULL, rule_id uuid NOT NULL, rule_version text NOT NULL,
 evaluator_id uuid NOT NULL, evaluator_version text NOT NULL, metric_id uuid NOT NULL, metric_version text NOT NULL,
 PRIMARY KEY(project_id,rule_id,rule_version,evaluator_id,evaluator_version),
 FOREIGN KEY(project_id,rule_id,rule_version) REFERENCES evaluation_online_rule_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,evaluator_id,evaluator_version) REFERENCES evaluation_evaluator_definition_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,metric_id,metric_version) REFERENCES evaluation_metric_definition_versions(project_id,entity_id,version) ON DELETE RESTRICT
);
CREATE TABLE evaluation_online_project_limits (
 project_id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE RESTRICT, policy_bytes bytea NOT NULL,
 sampled integer NOT NULL DEFAULT 0, works integer NOT NULL DEFAULT 0, calls integer NOT NULL DEFAULT 0,
 estimated_tokens bigint NOT NULL DEFAULT 0, reported_tokens bigint NOT NULL DEFAULT 0
);
CREATE TABLE evaluation_online_rule_usage (
 project_id uuid NOT NULL, rule_id uuid NOT NULL, rule_version text NOT NULL,
 sampled integer NOT NULL DEFAULT 0, works integer NOT NULL DEFAULT 0, calls integer NOT NULL DEFAULT 0,
 estimated_tokens bigint NOT NULL DEFAULT 0, reported_tokens bigint NOT NULL DEFAULT 0,
 PRIMARY KEY(project_id,rule_id,rule_version),
 FOREIGN KEY(project_id,rule_id,rule_version) REFERENCES evaluation_online_rule_versions(project_id,entity_id,version) ON DELETE RESTRICT
);
CREATE TABLE evaluation_online_admissions (
 project_id uuid NOT NULL, rule_id uuid NOT NULL, rule_version text NOT NULL, observation_id uuid NOT NULL,
 selection_bytes bytea NOT NULL, outcome text NOT NULL,
 PRIMARY KEY(project_id,rule_id,rule_version,observation_id),
 FOREIGN KEY(project_id,rule_id,rule_version) REFERENCES evaluation_online_rule_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,observation_id) REFERENCES evaluation_observations(project_id,id) ON DELETE RESTRICT
);
CREATE TABLE evaluation_online_works (
 id uuid PRIMARY KEY, project_id uuid NOT NULL, observation_id uuid NOT NULL, rule_id uuid NOT NULL, rule_version text NOT NULL,
 evaluator_id uuid NOT NULL, evaluator_version text NOT NULL, binding_bytes bytea NOT NULL,
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','CLAIMED','COMPLETED')),
 claim_token uuid UNIQUE, writer_epoch bigint, lease_expires_at timestamptz, owner_ref text,
 claim_count integer NOT NULL DEFAULT 0, call_count integer NOT NULL DEFAULT 0, calls_bytes bytea NOT NULL DEFAULT '\x5b5d',
 result_id uuid UNIQUE, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), completed_at timestamptz,
 UNIQUE(project_id,id), UNIQUE(project_id,observation_id,rule_id,rule_version,evaluator_id,evaluator_version),
 FOREIGN KEY(project_id,rule_id,rule_version,observation_id) REFERENCES evaluation_online_admissions(project_id,rule_id,rule_version,observation_id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,rule_id,rule_version,evaluator_id,evaluator_version) REFERENCES evaluation_online_bindings(project_id,rule_id,rule_version,evaluator_id,evaluator_version) ON DELETE RESTRICT,
 CHECK((status='COMPLETED')=(result_id IS NOT NULL AND completed_at IS NOT NULL)),
 CHECK(status<>'CLAIMED' OR (claim_token IS NOT NULL AND writer_epoch IS NOT NULL AND lease_expires_at IS NOT NULL)),
 CHECK(claim_count>=0 AND call_count>=0)
);
CREATE INDEX g5_online_pending ON evaluation_online_works(created_at,id) WHERE status='PENDING';
CREATE INDEX g5_online_expired ON evaluation_online_works(lease_expires_at,id) WHERE status='CLAIMED';
CREATE TABLE evaluation_online_results (
 id uuid PRIMARY KEY, project_id uuid NOT NULL, work_id uuid NOT NULL UNIQUE,
 result_bytes bytea NOT NULL, command_id uuid NOT NULL, author_token uuid NOT NULL, author_epoch bigint NOT NULL,
 intent_digest text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(project_id,work_id,id), FOREIGN KEY(project_id,work_id) REFERENCES evaluation_online_works(project_id,id) ON DELETE RESTRICT
);
ALTER TABLE evaluation_online_works ADD CONSTRAINT g5_result_link FOREIGN KEY(project_id,id,result_id) REFERENCES evaluation_online_results(project_id,work_id,id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE evaluation_online_backfills (
 id uuid PRIMARY KEY, project_id uuid NOT NULL, rule_id uuid NOT NULL, rule_version text NOT NULL,
 intent_bytes bytea NOT NULL, processed integer NOT NULL DEFAULT 0, cursor_at timestamptz, cursor_id uuid,
 completed boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(project_id,id), FOREIGN KEY(project_id,rule_id,rule_version) REFERENCES evaluation_online_rule_versions(project_id,entity_id,version) ON DELETE RESTRICT
);
CREATE FUNCTION g5_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 RAISE EXCEPTION 'online fact immutable' USING ERRCODE='55000'; END $$;
CREATE FUNCTION g5_work_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF TG_OP='DELETE' OR OLD.status='COMPLETED' THEN RAISE EXCEPTION 'online completed work immutable' USING ERRCODE='55000'; END IF;
 IF (to_jsonb(OLD)-ARRAY['status','claim_token','writer_epoch','lease_expires_at','owner_ref','claim_count','call_count','calls_bytes','result_id','completed_at']) IS DISTINCT FROM
    (to_jsonb(NEW)-ARRAY['status','claim_token','writer_epoch','lease_expires_at','owner_ref','claim_count','call_count','calls_bytes','result_id','completed_at']) THEN
 RAISE EXCEPTION 'online work binding immutable' USING ERRCODE='55000'; END IF; RETURN NEW; END $$;
CREATE TRIGGER g5_work_immutable BEFORE UPDATE OR DELETE ON evaluation_online_works FOR EACH ROW EXECUTE FUNCTION g5_work_guard();
CREATE FUNCTION g5_result_complete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM evaluation_online_works WHERE project_id=NEW.project_id AND id=NEW.work_id AND status='COMPLETED' AND result_id=NEW.id) THEN
 RAISE EXCEPTION 'online result and work must commit atomically' USING ERRCODE='23514'; END IF; RETURN NULL; END $$;
CREATE CONSTRAINT TRIGGER g5_result_complete AFTER INSERT ON evaluation_online_results DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g5_result_complete();
CREATE FUNCTION g5_limits_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF TG_OP='DELETE' OR OLD.policy_bytes IS DISTINCT FROM NEW.policy_bytes THEN RAISE EXCEPTION 'online project policy immutable' USING ERRCODE='55000'; END IF; RETURN NEW; END $$;
CREATE TRIGGER g5_limits_immutable BEFORE UPDATE OR DELETE ON evaluation_online_project_limits FOR EACH ROW EXECUTE FUNCTION g5_limits_guard();
CREATE FUNCTION g5_binding_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM evaluation_online_rule_versions v,
 jsonb_array_elements(convert_from(v.canonical_bytes,'UTF8')::jsonb->'Bindings') b
 WHERE v.project_id=NEW.project_id AND v.entity_id=NEW.rule_id AND v.version=NEW.rule_version
 AND b->'Evaluator'->'identity'->'ref'->>'entity_id'=NEW.evaluator_id::text
 AND b->'Evaluator'->'identity'->'ref'->>'version'=NEW.evaluator_version
 AND b->'Metric'->'identity'->'ref'->>'entity_id'=NEW.metric_id::text
 AND b->'Metric'->'identity'->'ref'->>'version'=NEW.metric_version) THEN
 RAISE EXCEPTION 'online binding outside immutable rule' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE TRIGGER g5_binding_member BEFORE INSERT ON evaluation_online_bindings FOR EACH ROW EXECUTE FUNCTION g5_binding_guard();
CREATE FUNCTION g5_rule_complete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF (SELECT count(*) FROM evaluation_online_bindings WHERE project_id=NEW.project_id AND rule_id=NEW.entity_id AND rule_version=NEW.version)
 <>jsonb_array_length(convert_from(NEW.canonical_bytes,'UTF8')::jsonb->'Bindings') THEN
 RAISE EXCEPTION 'online rule bindings must commit atomically' USING ERRCODE='23514'; END IF; RETURN NULL; END $$;
CREATE CONSTRAINT TRIGGER g5_rule_complete AFTER INSERT ON evaluation_online_rule_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g5_rule_complete();
CREATE FUNCTION g5_backfill_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF TG_OP='DELETE' OR OLD.completed OR NEW.processed<OLD.processed OR
 (to_jsonb(OLD)-ARRAY['processed','cursor_at','cursor_id','completed']) IS DISTINCT FROM
 (to_jsonb(NEW)-ARRAY['processed','cursor_at','cursor_id','completed']) THEN
 RAISE EXCEPTION 'online backfill intent immutable' USING ERRCODE='55000'; END IF; RETURN NEW; END $$;
CREATE TRIGGER g5_backfill_immutable BEFORE UPDATE OR DELETE ON evaluation_online_backfills FOR EACH ROW EXECUTE FUNCTION g5_backfill_guard();
""")
    for table in (
        "evaluation_observations", "evaluation_online_rules", "evaluation_online_rule_versions",
        "evaluation_online_bindings", "evaluation_online_admissions", "evaluation_online_results",
    ):
        op.execute(f"CREATE TRIGGER g5_fact_immutable BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION g5_immutable()")
    for table in (
        "evaluation_observations", "evaluation_online_rules", "evaluation_online_rule_versions",
        "evaluation_online_bindings", "evaluation_online_admissions", "evaluation_online_results",
        "evaluation_online_works", "evaluation_online_project_limits", "evaluation_online_rule_usage",
        "evaluation_online_backfills",
    ):
        op.execute(f"CREATE TRIGGER g5_no_truncate BEFORE TRUNCATE ON {table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION g5_immutable()")


def downgrade():
    """拒绝通过降级丢失不可变在线事实."""
    raise RuntimeError("G5 online history requires an explicit compatible recovery plan")
