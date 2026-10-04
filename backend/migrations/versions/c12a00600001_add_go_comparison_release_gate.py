"""G6 不可变比较快照、版本化政策与发布门禁决定."""

from alembic import op

revision = "c12a00600001"
down_revision = "c12a00500001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只新增 decision owner，保留唯一迁移链和已有事实表."""
    op.execute(r"""
CREATE TABLE evaluation_policies (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 name varchar(255) NOT NULL CHECK(length(btrim(name))>0), created_by text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(project_id,id)
);
CREATE TABLE evaluation_policy_versions (
 project_id uuid NOT NULL, entity_id uuid NOT NULL, version varchar(255) NOT NULL,
 algorithm_ref text NOT NULL CHECK(algorithm_ref='catalog-json-v1'),
 content_digest text NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
 semantic_digest text NOT NULL CHECK(semantic_digest ~ '^[0-9a-f]{64}$'),
 canonical_bytes bytea NOT NULL, content jsonb NOT NULL,
 published_by text NOT NULL, published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 sealed boolean NOT NULL DEFAULT false, creation_xid bigint NOT NULL DEFAULT txid_current(),
 PRIMARY KEY(project_id,entity_id,version),
 FOREIGN KEY(project_id,entity_id) REFERENCES evaluation_policies(project_id,id) ON DELETE RESTRICT
);
CREATE TRIGGER g6_policy_guard BEFORE INSERT OR UPDATE OR DELETE ON evaluation_policy_versions FOR EACH ROW EXECUTE FUNCTION g1_version_guard();
CREATE CONSTRAINT TRIGGER g6_policy_sealed AFTER INSERT OR UPDATE ON evaluation_policy_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g1_require_sealed();
CREATE TRIGGER g6_policy_no_truncate BEFORE TRUNCATE ON evaluation_policy_versions FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation();
CREATE TABLE evaluation_comparisons (
 id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 policy_id uuid NOT NULL, policy_version text NOT NULL, intent_digest text NOT NULL,
 snapshot_digest text NOT NULL, snapshot_bytes bytea NOT NULL CHECK(octet_length(snapshot_bytes) BETWEEN 1 AND 67108864),
 created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(project_id,id),
 FOREIGN KEY(project_id,policy_id,policy_version) REFERENCES evaluation_policy_versions(project_id,entity_id,version) ON DELETE RESTRICT
);
CREATE TABLE evaluation_gate_receipts (
 project_id uuid NOT NULL, gate_id uuid NOT NULL,
 decision text NOT NULL CHECK(decision IN ('PASS','FAIL','BLOCKED')),
 receipt_bytes bytea NOT NULL, receipt_digest text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), creation_xid bigint NOT NULL DEFAULT txid_current(),
 PRIMARY KEY(project_id,gate_id),
 FOREIGN KEY(project_id,gate_id) REFERENCES evaluation_comparisons(project_id,id) ON DELETE RESTRICT
);
CREATE TABLE evaluation_case_comparisons (
 project_id uuid NOT NULL, gate_id uuid NOT NULL, unit_key text NOT NULL, comparison_bytes bytea NOT NULL,
 PRIMARY KEY(project_id,gate_id,unit_key),
 FOREIGN KEY(project_id,gate_id) REFERENCES evaluation_gate_receipts(project_id,gate_id) ON DELETE RESTRICT
);
CREATE FUNCTION g6_case_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM evaluation_gate_receipts WHERE project_id=NEW.project_id AND gate_id=NEW.gate_id AND creation_xid=txid_current()) THEN
  RAISE EXCEPTION 'case comparison belongs to receipt publication transaction' USING ERRCODE='55000';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER g6_case_insert BEFORE INSERT ON evaluation_case_comparisons FOR EACH ROW EXECUTE FUNCTION g6_case_insert_guard();
CREATE FUNCTION g6_receipt_complete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE snapshot jsonb; receipt jsonb;
BEGIN
 SELECT convert_from(snapshot_bytes,'UTF8')::jsonb INTO snapshot FROM evaluation_comparisons WHERE project_id=NEW.project_id AND id=NEW.gate_id;
 receipt := convert_from(NEW.receipt_bytes,'UTF8')::jsonb;
 IF receipt->>'gate_id' IS DISTINCT FROM NEW.gate_id::text OR receipt->>'project_id' IS DISTINCT FROM NEW.project_id::text OR receipt->>'decision' IS DISTINCT FROM NEW.decision
 OR receipt->>'snapshot_digest' IS DISTINCT FROM (SELECT snapshot_digest FROM evaluation_comparisons WHERE project_id=NEW.project_id AND id=NEW.gate_id)
 OR receipt->'policy_ref' IS DISTINCT FROM snapshot->'Command'->'Policy'
 OR receipt->>'policy_digest' IS DISTINCT FROM snapshot->>'PolicyDigest'
 OR receipt->>'Actor' IS DISTINCT FROM snapshot->>'Actor'
 OR jsonb_array_length(receipt->'case_comparisons') IS DISTINCT FROM (SELECT count(*) FROM evaluation_case_comparisons WHERE project_id=NEW.project_id AND gate_id=NEW.gate_id)
 OR EXISTS (SELECT 1 FROM evaluation_case_comparisons c WHERE c.project_id=NEW.project_id AND c.gate_id=NEW.gate_id AND NOT EXISTS
   (SELECT 1 FROM jsonb_array_elements(receipt->'case_comparisons') item WHERE item->>'Key'=c.unit_key AND item=convert_from(c.comparison_bytes,'UTF8')::jsonb)) THEN
  RAISE EXCEPTION 'gate receipt binding or case manifest invalid' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER g6_receipt_complete AFTER INSERT ON evaluation_gate_receipts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION g6_receipt_complete();
CREATE INDEX g6_comparison_list ON evaluation_comparisons(project_id,created_at,id);
""")
    for table in ("evaluation_comparisons", "evaluation_gate_receipts", "evaluation_case_comparisons"):
        op.execute(f"CREATE TRIGGER g6_immutable BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION g1_reject_mutation()")
        op.execute(f"CREATE TRIGGER g6_no_truncate BEFORE TRUNCATE ON {table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION g1_reject_mutation()")


def downgrade() -> None:
    """拒绝删除不可变发布决定历史."""
    raise RuntimeError("G6 decision history requires an explicit compatible recovery plan")
