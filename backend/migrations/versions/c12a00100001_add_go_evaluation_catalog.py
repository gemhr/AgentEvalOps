"""G1 项目隔离的不可变评测目录，沿用唯一 Alembic ledger。

Revision ID: c12a00100001
Revises: a2c9f84e1b73
"""

from alembic import op

revision = "c12a00100001"
down_revision = "a2c9f84e1b73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只新增目录与发布约束，不重写历史 Run/Attempt/Result。"""
    op.execute(r"""

CREATE FUNCTION g1_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'published catalog is immutable' USING ERRCODE='55000'; END; $$;
CREATE FUNCTION g1_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'INSERT' THEN
  IF NEW.sealed OR NEW.creation_xid <> txid_current() THEN RAISE EXCEPTION 'invalid publication transaction' USING ERRCODE='55000'; END IF;
  RETURN NEW;
 END IF;
 IF TG_OP = 'UPDATE' AND NOT OLD.sealed AND NEW.sealed
  AND OLD.creation_xid = txid_current()
  AND (to_jsonb(NEW) - 'sealed') = (to_jsonb(OLD) - 'sealed') THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'published catalog version is immutable' USING ERRCODE='55000';
END; $$;
CREATE FUNCTION g1_require_sealed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE done boolean;
BEGIN
 EXECUTE format('SELECT sealed FROM %I WHERE project_id=$1 AND entity_id=$2 AND version=$3', TG_TABLE_NAME)
 INTO done USING NEW.project_id, NEW.entity_id, NEW.version;
 IF done IS DISTINCT FROM true THEN RAISE EXCEPTION 'incomplete catalog publication' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END; $$;
CREATE FUNCTION g1_relation_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE open boolean; parent jsonb;
BEGIN
 parent := to_jsonb(NEW);
 EXECUTE format('SELECT NOT sealed AND creation_xid=txid_current() FROM %I WHERE project_id=$1 AND entity_id=$2 AND version=$3', TG_ARGV[0])
 INTO open USING NEW.project_id, (parent->>TG_ARGV[1])::uuid, parent->>TG_ARGV[2];
 IF open IS DISTINCT FROM true THEN RAISE EXCEPTION 'published catalog references are immutable' USING ERRCODE='55000'; END IF;
 RETURN NEW;
END; $$;

CREATE TABLE evaluation_cases (
 id uuid PRIMARY KEY,
 project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 name varchar(255) NOT NULL CHECK (length(btrim(name))>0),
 created_by varchar(255) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(project_id,id)
);
CREATE TABLE evaluation_case_versions (
 project_id uuid NOT NULL,
 entity_id uuid NOT NULL,
 version varchar(255) NOT NULL CHECK (length(btrim(version))>0),
 algorithm_ref varchar(64) NOT NULL CHECK(algorithm_ref='catalog-json-v1'),
 content_digest varchar(64) NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
 semantic_digest varchar(64) NOT NULL CHECK(semantic_digest ~ '^[0-9a-f]{64}$'),
 canonical_bytes bytea NOT NULL CHECK(octet_length(canonical_bytes)>0),
 content jsonb NOT NULL CHECK(jsonb_typeof(content)='object'),
 published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 published_by varchar(255) NOT NULL CHECK(length(btrim(published_by))>0),
 sealed boolean NOT NULL DEFAULT false,
 creation_xid bigint NOT NULL DEFAULT txid_current(),
 PRIMARY KEY(project_id,entity_id,version),
 FOREIGN KEY(project_id,entity_id) REFERENCES evaluation_cases(project_id,id) ON DELETE RESTRICT
);
CREATE INDEX ix_evaluation_cases_project_created ON evaluation_cases(project_id,created_at,id);
CREATE TRIGGER trg_evaluation_case_versions_guard BEFORE INSERT OR UPDATE OR DELETE ON evaluation_case_versions FOR EACH ROW EXECUTE FUNCTION g1_version_guard();
CREATE CONSTRAINT TRIGGER trg_evaluation_case_versions_sealed AFTER INSERT OR UPDATE ON evaluation_case_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g1_require_sealed();
CREATE TRIGGER trg_evaluation_case_versions_truncate BEFORE TRUNCATE ON evaluation_case_versions FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();

CREATE TABLE evaluation_datasets (
 id uuid PRIMARY KEY,
 project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 name varchar(255) NOT NULL CHECK (length(btrim(name))>0),
 created_by varchar(255) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(project_id,id)
);
CREATE TABLE evaluation_dataset_versions (
 project_id uuid NOT NULL,
 entity_id uuid NOT NULL,
 version varchar(255) NOT NULL CHECK (length(btrim(version))>0),
 algorithm_ref varchar(64) NOT NULL CHECK(algorithm_ref='catalog-json-v1'),
 content_digest varchar(64) NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
 semantic_digest varchar(64) NOT NULL CHECK(semantic_digest ~ '^[0-9a-f]{64}$'),
 canonical_bytes bytea NOT NULL CHECK(octet_length(canonical_bytes)>0),
 content jsonb NOT NULL CHECK(jsonb_typeof(content)='object'),
 published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 published_by varchar(255) NOT NULL CHECK(length(btrim(published_by))>0),
 sealed boolean NOT NULL DEFAULT false,
 creation_xid bigint NOT NULL DEFAULT txid_current(),
 PRIMARY KEY(project_id,entity_id,version),
 FOREIGN KEY(project_id,entity_id) REFERENCES evaluation_datasets(project_id,id) ON DELETE RESTRICT
);
CREATE INDEX ix_evaluation_datasets_project_created ON evaluation_datasets(project_id,created_at,id);
CREATE TRIGGER trg_evaluation_dataset_versions_guard BEFORE INSERT OR UPDATE OR DELETE ON evaluation_dataset_versions FOR EACH ROW EXECUTE FUNCTION g1_version_guard();
CREATE CONSTRAINT TRIGGER trg_evaluation_dataset_versions_sealed AFTER INSERT OR UPDATE ON evaluation_dataset_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g1_require_sealed();
CREATE TRIGGER trg_evaluation_dataset_versions_truncate BEFORE TRUNCATE ON evaluation_dataset_versions FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();

CREATE TABLE evaluation_suites (
 id uuid PRIMARY KEY,
 project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 name varchar(255) NOT NULL CHECK (length(btrim(name))>0),
 created_by varchar(255) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(project_id,id)
);
CREATE TABLE evaluation_suite_versions (
 project_id uuid NOT NULL,
 entity_id uuid NOT NULL,
 version varchar(255) NOT NULL CHECK (length(btrim(version))>0),
 algorithm_ref varchar(64) NOT NULL CHECK(algorithm_ref='catalog-json-v1'),
 content_digest varchar(64) NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
 semantic_digest varchar(64) NOT NULL CHECK(semantic_digest ~ '^[0-9a-f]{64}$'),
 canonical_bytes bytea NOT NULL CHECK(octet_length(canonical_bytes)>0),
 content jsonb NOT NULL CHECK(jsonb_typeof(content)='object'),
 published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 published_by varchar(255) NOT NULL CHECK(length(btrim(published_by))>0),
 sealed boolean NOT NULL DEFAULT false,
 creation_xid bigint NOT NULL DEFAULT txid_current(),
 PRIMARY KEY(project_id,entity_id,version),
 FOREIGN KEY(project_id,entity_id) REFERENCES evaluation_suites(project_id,id) ON DELETE RESTRICT
);
CREATE INDEX ix_evaluation_suites_project_created ON evaluation_suites(project_id,created_at,id);
CREATE TRIGGER trg_evaluation_suite_versions_guard BEFORE INSERT OR UPDATE OR DELETE ON evaluation_suite_versions FOR EACH ROW EXECUTE FUNCTION g1_version_guard();
CREATE CONSTRAINT TRIGGER trg_evaluation_suite_versions_sealed AFTER INSERT OR UPDATE ON evaluation_suite_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g1_require_sealed();
CREATE TRIGGER trg_evaluation_suite_versions_truncate BEFORE TRUNCATE ON evaluation_suite_versions FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();

CREATE TABLE evaluation_metric_definitions (
 id uuid PRIMARY KEY,
 project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 name varchar(255) NOT NULL CHECK (length(btrim(name))>0),
 created_by varchar(255) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(project_id,id)
);
CREATE TABLE evaluation_metric_definition_versions (
 project_id uuid NOT NULL,
 entity_id uuid NOT NULL,
 version varchar(255) NOT NULL CHECK (length(btrim(version))>0),
 algorithm_ref varchar(64) NOT NULL CHECK(algorithm_ref='catalog-json-v1'),
 content_digest varchar(64) NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
 semantic_digest varchar(64) NOT NULL CHECK(semantic_digest ~ '^[0-9a-f]{64}$'),
 canonical_bytes bytea NOT NULL CHECK(octet_length(canonical_bytes)>0),
 content jsonb NOT NULL CHECK(jsonb_typeof(content)='object'),
 published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 published_by varchar(255) NOT NULL CHECK(length(btrim(published_by))>0),
 sealed boolean NOT NULL DEFAULT false,
 creation_xid bigint NOT NULL DEFAULT txid_current(),
 PRIMARY KEY(project_id,entity_id,version),
 FOREIGN KEY(project_id,entity_id) REFERENCES evaluation_metric_definitions(project_id,id) ON DELETE RESTRICT
);
CREATE INDEX ix_evaluation_metric_definitions_project_created ON evaluation_metric_definitions(project_id,created_at,id);
CREATE TRIGGER trg_evaluation_metric_definition_versions_guard BEFORE INSERT OR UPDATE OR DELETE ON evaluation_metric_definition_versions FOR EACH ROW EXECUTE FUNCTION g1_version_guard();
CREATE CONSTRAINT TRIGGER trg_evaluation_metric_definition_versions_sealed AFTER INSERT OR UPDATE ON evaluation_metric_definition_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g1_require_sealed();
CREATE TRIGGER trg_evaluation_metric_definition_versions_truncate BEFORE TRUNCATE ON evaluation_metric_definition_versions FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();

CREATE TABLE evaluation_evaluator_definitions (
 id uuid PRIMARY KEY,
 project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 name varchar(255) NOT NULL CHECK (length(btrim(name))>0),
 created_by varchar(255) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(project_id,id)
);
CREATE TABLE evaluation_evaluator_definition_versions (
 project_id uuid NOT NULL,
 entity_id uuid NOT NULL,
 version varchar(255) NOT NULL CHECK (length(btrim(version))>0),
 algorithm_ref varchar(64) NOT NULL CHECK(algorithm_ref='catalog-json-v1'),
 content_digest varchar(64) NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
 semantic_digest varchar(64) NOT NULL CHECK(semantic_digest ~ '^[0-9a-f]{64}$'),
 canonical_bytes bytea NOT NULL CHECK(octet_length(canonical_bytes)>0),
 content jsonb NOT NULL CHECK(jsonb_typeof(content)='object'),
 published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 published_by varchar(255) NOT NULL CHECK(length(btrim(published_by))>0),
 sealed boolean NOT NULL DEFAULT false,
 creation_xid bigint NOT NULL DEFAULT txid_current(),
 PRIMARY KEY(project_id,entity_id,version),
 FOREIGN KEY(project_id,entity_id) REFERENCES evaluation_evaluator_definitions(project_id,id) ON DELETE RESTRICT
);
CREATE INDEX ix_evaluation_evaluator_definitions_project_created ON evaluation_evaluator_definitions(project_id,created_at,id);
CREATE TRIGGER trg_evaluation_evaluator_definition_versions_guard BEFORE INSERT OR UPDATE OR DELETE ON evaluation_evaluator_definition_versions FOR EACH ROW EXECUTE FUNCTION g1_version_guard();
CREATE CONSTRAINT TRIGGER trg_evaluation_evaluator_definition_versions_sealed AFTER INSERT OR UPDATE ON evaluation_evaluator_definition_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g1_require_sealed();
CREATE TRIGGER trg_evaluation_evaluator_definition_versions_truncate BEFORE TRUNCATE ON evaluation_evaluator_definition_versions FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();

ALTER TABLE evaluation_suite_versions ADD COLUMN dataset_id uuid;
ALTER TABLE evaluation_suite_versions ADD COLUMN dataset_version varchar(255);
ALTER TABLE evaluation_suite_versions ADD CONSTRAINT ck_g1_suite_dataset_pair CHECK ((dataset_id IS NULL) = (dataset_version IS NULL));
ALTER TABLE evaluation_suite_versions ADD CONSTRAINT fk_g1_suite_dataset FOREIGN KEY(project_id,dataset_id,dataset_version) REFERENCES evaluation_dataset_versions(project_id,entity_id,version) ON DELETE RESTRICT;

CREATE TABLE evaluation_dataset_cases (
 project_id uuid NOT NULL, dataset_id uuid NOT NULL, dataset_version varchar(255) NOT NULL,
 position integer NOT NULL CHECK(position>=0), case_id uuid NOT NULL, case_version varchar(255) NOT NULL,
 PRIMARY KEY(project_id,dataset_id,dataset_version,position),
 UNIQUE(project_id,dataset_id,dataset_version,case_id),
 FOREIGN KEY(project_id,dataset_id,dataset_version) REFERENCES evaluation_dataset_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,case_id,case_version) REFERENCES evaluation_case_versions(project_id,entity_id,version) ON DELETE RESTRICT
);
CREATE TABLE evaluation_suite_cases (
 project_id uuid NOT NULL, suite_id uuid NOT NULL, suite_version varchar(255) NOT NULL,
 position integer NOT NULL CHECK(position>=0), case_id uuid NOT NULL, case_version varchar(255) NOT NULL,
 PRIMARY KEY(project_id,suite_id,suite_version,position),
 UNIQUE(project_id,suite_id,suite_version,case_id),
 FOREIGN KEY(project_id,suite_id,suite_version) REFERENCES evaluation_suite_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,case_id,case_version) REFERENCES evaluation_case_versions(project_id,entity_id,version) ON DELETE RESTRICT
);
CREATE TABLE evaluation_evaluator_metrics (
 project_id uuid NOT NULL, evaluator_id uuid NOT NULL, evaluator_version varchar(255) NOT NULL,
 position integer NOT NULL CHECK(position>=0), metric_id uuid NOT NULL, metric_version varchar(255) NOT NULL,
 PRIMARY KEY(project_id,evaluator_id,evaluator_version,position),
 UNIQUE(project_id,evaluator_id,evaluator_version,metric_id,metric_version),
 FOREIGN KEY(project_id,evaluator_id,evaluator_version) REFERENCES evaluation_evaluator_definition_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,metric_id,metric_version) REFERENCES evaluation_metric_definition_versions(project_id,entity_id,version) ON DELETE RESTRICT
);
CREATE TABLE evaluation_suite_metrics (
 project_id uuid NOT NULL, suite_id uuid NOT NULL, suite_version varchar(255) NOT NULL,
 position integer NOT NULL CHECK(position>=0), metric_id uuid NOT NULL, metric_version varchar(255) NOT NULL,
 PRIMARY KEY(project_id,suite_id,suite_version,position),
 UNIQUE(project_id,suite_id,suite_version,metric_id,metric_version),
 FOREIGN KEY(project_id,suite_id,suite_version) REFERENCES evaluation_suite_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,metric_id,metric_version) REFERENCES evaluation_metric_definition_versions(project_id,entity_id,version) ON DELETE RESTRICT
);
CREATE TABLE evaluation_suite_evaluators (
 project_id uuid NOT NULL, suite_id uuid NOT NULL, suite_version varchar(255) NOT NULL,
 position integer NOT NULL CHECK(position>=0), evaluator_id uuid NOT NULL, evaluator_version varchar(255) NOT NULL,
 required boolean NOT NULL, applicability jsonb NOT NULL,
 PRIMARY KEY(project_id,suite_id,suite_version,position),
 UNIQUE(project_id,suite_id,suite_version,evaluator_id,evaluator_version),
 FOREIGN KEY(project_id,suite_id,suite_version) REFERENCES evaluation_suite_versions(project_id,entity_id,version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,evaluator_id,evaluator_version) REFERENCES evaluation_evaluator_definition_versions(project_id,entity_id,version) ON DELETE RESTRICT
);
CREATE TABLE evaluation_suite_evaluator_metrics (
 project_id uuid NOT NULL, suite_id uuid NOT NULL, suite_version varchar(255) NOT NULL,
 evaluator_id uuid NOT NULL, evaluator_version varchar(255) NOT NULL,
 position integer NOT NULL CHECK(position>=0), metric_id uuid NOT NULL, metric_version varchar(255) NOT NULL,
 PRIMARY KEY(project_id,suite_id,suite_version,evaluator_id,evaluator_version,position),
 UNIQUE(project_id,suite_id,suite_version,evaluator_id,evaluator_version,metric_id,metric_version),
 FOREIGN KEY(project_id,suite_id,suite_version,evaluator_id,evaluator_version) REFERENCES evaluation_suite_evaluators(project_id,suite_id,suite_version,evaluator_id,evaluator_version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,suite_id,suite_version,metric_id,metric_version) REFERENCES evaluation_suite_metrics(project_id,suite_id,suite_version,metric_id,metric_version) ON DELETE RESTRICT,
 FOREIGN KEY(project_id,evaluator_id,evaluator_version,metric_id,metric_version) REFERENCES evaluation_evaluator_metrics(project_id,evaluator_id,evaluator_version,metric_id,metric_version) ON DELETE RESTRICT
);
CREATE TRIGGER trg_evaluation_dataset_cases_insert BEFORE INSERT ON evaluation_dataset_cases FOR EACH ROW EXECUTE FUNCTION g1_relation_insert_guard('evaluation_dataset_versions','dataset_id','dataset_version');
CREATE TRIGGER trg_evaluation_dataset_cases_immutable BEFORE UPDATE OR DELETE ON evaluation_dataset_cases FOR EACH ROW EXECUTE FUNCTION g1_reject_mutation();
CREATE TRIGGER trg_evaluation_dataset_cases_truncate BEFORE TRUNCATE ON evaluation_dataset_cases FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();
CREATE TRIGGER trg_evaluation_suite_cases_insert BEFORE INSERT ON evaluation_suite_cases FOR EACH ROW EXECUTE FUNCTION g1_relation_insert_guard('evaluation_suite_versions','suite_id','suite_version');
CREATE TRIGGER trg_evaluation_suite_cases_immutable BEFORE UPDATE OR DELETE ON evaluation_suite_cases FOR EACH ROW EXECUTE FUNCTION g1_reject_mutation();
CREATE TRIGGER trg_evaluation_suite_cases_truncate BEFORE TRUNCATE ON evaluation_suite_cases FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();
CREATE TRIGGER trg_evaluation_evaluator_metrics_insert BEFORE INSERT ON evaluation_evaluator_metrics FOR EACH ROW EXECUTE FUNCTION g1_relation_insert_guard('evaluation_evaluator_definition_versions','evaluator_id','evaluator_version');
CREATE TRIGGER trg_evaluation_evaluator_metrics_immutable BEFORE UPDATE OR DELETE ON evaluation_evaluator_metrics FOR EACH ROW EXECUTE FUNCTION g1_reject_mutation();
CREATE TRIGGER trg_evaluation_evaluator_metrics_truncate BEFORE TRUNCATE ON evaluation_evaluator_metrics FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();
CREATE TRIGGER trg_evaluation_suite_metrics_insert BEFORE INSERT ON evaluation_suite_metrics FOR EACH ROW EXECUTE FUNCTION g1_relation_insert_guard('evaluation_suite_versions','suite_id','suite_version');
CREATE TRIGGER trg_evaluation_suite_metrics_immutable BEFORE UPDATE OR DELETE ON evaluation_suite_metrics FOR EACH ROW EXECUTE FUNCTION g1_reject_mutation();
CREATE TRIGGER trg_evaluation_suite_metrics_truncate BEFORE TRUNCATE ON evaluation_suite_metrics FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();
CREATE TRIGGER trg_evaluation_suite_evaluators_insert BEFORE INSERT ON evaluation_suite_evaluators FOR EACH ROW EXECUTE FUNCTION g1_relation_insert_guard('evaluation_suite_versions','suite_id','suite_version');
CREATE TRIGGER trg_evaluation_suite_evaluators_immutable BEFORE UPDATE OR DELETE ON evaluation_suite_evaluators FOR EACH ROW EXECUTE FUNCTION g1_reject_mutation();
CREATE TRIGGER trg_evaluation_suite_evaluators_truncate BEFORE TRUNCATE ON evaluation_suite_evaluators FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();
CREATE TRIGGER trg_evaluation_suite_evaluator_metrics_insert BEFORE INSERT ON evaluation_suite_evaluator_metrics FOR EACH ROW EXECUTE FUNCTION g1_relation_insert_guard('evaluation_suite_versions','suite_id','suite_version');
CREATE TRIGGER trg_evaluation_suite_evaluator_metrics_immutable BEFORE UPDATE OR DELETE ON evaluation_suite_evaluator_metrics FOR EACH ROW EXECUTE FUNCTION g1_reject_mutation();
CREATE TRIGGER trg_evaluation_suite_evaluator_metrics_truncate BEFORE TRUNCATE ON evaluation_suite_evaluator_metrics FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();
    """)


def downgrade() -> None:
    """仅移除 G1 schema；不用于生产数据回退。"""
    op.execute("DROP TABLE evaluation_suite_evaluator_metrics")
    op.execute("DROP TABLE evaluation_suite_evaluators")
    op.execute("DROP TABLE evaluation_suite_metrics")
    op.execute("DROP TABLE evaluation_evaluator_metrics")
    op.execute("DROP TABLE evaluation_suite_cases")
    op.execute("DROP TABLE evaluation_dataset_cases")
    op.execute("DROP TABLE evaluation_evaluator_definition_versions")
    op.execute("DROP TABLE evaluation_evaluator_definitions")
    op.execute("DROP TABLE evaluation_metric_definition_versions")
    op.execute("DROP TABLE evaluation_metric_definitions")
    op.execute("DROP TABLE evaluation_suite_versions")
    op.execute("DROP TABLE evaluation_suites")
    op.execute("DROP TABLE evaluation_dataset_versions")
    op.execute("DROP TABLE evaluation_datasets")
    op.execute("DROP TABLE evaluation_case_versions")
    op.execute("DROP TABLE evaluation_cases")
    op.execute("DROP FUNCTION g1_relation_insert_guard()")
    op.execute("DROP FUNCTION g1_require_sealed()")
    op.execute("DROP FUNCTION g1_version_guard()")
    op.execute("DROP FUNCTION g1_reject_mutation()")

