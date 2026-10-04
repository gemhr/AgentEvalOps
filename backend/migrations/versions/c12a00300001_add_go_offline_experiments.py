"""Stage12 G3 最小离线实验意图与 Run 引用."""

from alembic import op

revision = "c12a00300001"
down_revision = "c12a00200001"
branch_labels = None
depends_on = None


def upgrade():
    """添加实验表，不修改 G2 ownership 或 writer contract."""
    op.execute(r"""
CREATE TABLE evaluation_experiments (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 name text NOT NULL CHECK(length(name)>0), repeat_count integer NOT NULL CHECK(repeat_count BETWEEN 1 AND 100),
 created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 intent_digest text NOT NULL, intent_bytes bytea NOT NULL, kernel_bytes bytea NOT NULL,
 baseline_run_id uuid, baseline_experiment_id uuid,
 creation_xid bigint NOT NULL DEFAULT txid_current(),
 UNIQUE(project_id,id),
 CHECK(baseline_run_id IS NULL OR baseline_experiment_id IS NULL),
 CHECK(baseline_experiment_id IS NULL OR baseline_experiment_id<>id),
 FOREIGN KEY(project_id,baseline_run_id) REFERENCES evaluation_runs(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,baseline_experiment_id) REFERENCES evaluation_experiments(project_id,id) ON DELETE RESTRICT
);
CREATE INDEX g3_experiment_list ON evaluation_experiments(project_id,created_at,id);
CREATE TABLE evaluation_experiment_runs (
 project_id uuid NOT NULL, experiment_id uuid NOT NULL, repeat_no integer NOT NULL CHECK(repeat_no BETWEEN 1 AND 100),
 creation_command_id uuid NOT NULL UNIQUE, run_id uuid UNIQUE,
 PRIMARY KEY(project_id,experiment_id,repeat_no),
 FOREIGN KEY(project_id,experiment_id) REFERENCES evaluation_experiments(project_id,id) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,run_id) REFERENCES evaluation_runs(project_id,id) ON DELETE RESTRICT
);
CREATE FUNCTION g3_experiment_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'experiment intent immutable' USING ERRCODE='55000';
END $$;
CREATE TRIGGER g3_experiment_immutable BEFORE UPDATE OR DELETE ON evaluation_experiments FOR EACH ROW EXECUTE FUNCTION g3_experiment_guard();
CREATE TRIGGER g3_experiment_no_truncate BEFORE TRUNCATE ON evaluation_experiments FOR EACH STATEMENT EXECUTE FUNCTION g3_experiment_guard();
CREATE TRIGGER g3_experiment_runs_no_truncate BEFORE TRUNCATE ON evaluation_experiment_runs FOR EACH STATEMENT EXECUTE FUNCTION g3_experiment_guard();
CREATE FUNCTION g3_experiment_slot_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE exp evaluation_experiments; valid boolean;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'experiment run reference immutable' USING ERRCODE='55000'; END IF;
 SELECT * INTO exp FROM evaluation_experiments WHERE project_id=NEW.project_id AND id=NEW.experiment_id;
 IF TG_OP='INSERT' AND (exp.creation_xid<>txid_current() OR NEW.repeat_no>exp.repeat_count) THEN RAISE EXCEPTION 'experiment slots must be frozen at creation' USING ERRCODE='55000'; END IF;
 IF TG_OP='UPDATE' AND (OLD.run_id IS NOT NULL OR (to_jsonb(OLD)-'run_id') IS DISTINCT FROM (to_jsonb(NEW)-'run_id')) THEN RAISE EXCEPTION 'experiment run link immutable' USING ERRCODE='55000'; END IF;
 IF NEW.run_id IS NOT NULL THEN
  SELECT EXISTS(SELECT 1 FROM evaluation_runs r WHERE r.project_id=NEW.project_id AND r.id=NEW.run_id AND r.creation_command_id=NEW.creation_command_id AND r.kernel_bytes=exp.kernel_bytes AND r.metadata->>'Actor'=exp.created_by) INTO valid;
  IF NOT valid THEN RAISE EXCEPTION 'experiment run intent mismatch' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER g3_experiment_slot_immutable BEFORE INSERT OR UPDATE OR DELETE ON evaluation_experiment_runs FOR EACH ROW EXECUTE FUNCTION g3_experiment_slot_guard();
CREATE FUNCTION g3_experiment_complete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (SELECT count(*) FROM evaluation_experiment_runs WHERE project_id=NEW.project_id AND experiment_id=NEW.id)<>NEW.repeat_count THEN RAISE EXCEPTION 'experiment repeat manifest incomplete' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER g3_experiment_complete AFTER INSERT ON evaluation_experiments DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g3_experiment_complete();
""")


def downgrade():
    """拒绝通过降级丢失不可变实验历史."""
    raise RuntimeError("G3 experiment history requires an explicit compatible recovery plan")
