"""新增 Go Product API 身份、项目能力和外部命令记录.

仅由 Alembic offline operator 执行.
"""

from alembic import op

revision = "c12a00800001"
down_revision = "c12a00700001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留历史身份，仅增加 Go owner 的边界."""
    op.execute("""
    ALTER TABLE projects ADD CONSTRAINT uq_g8_project_org UNIQUE(id,org_id);
    CREATE TABLE product_project_memberships (
        project_id uuid NOT NULL, org_id uuid NOT NULL, principal_id uuid NOT NULL REFERENCES users(id),
        capabilities text[] NOT NULL CHECK(cardinality(capabilities)>0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(project_id,principal_id), FOREIGN KEY(project_id,org_id) REFERENCES projects(id,org_id)
    );
    CREATE TABLE product_api_credentials (
        id uuid PRIMARY KEY, project_id uuid NOT NULL, org_id uuid NOT NULL,
        key_digest text NOT NULL UNIQUE CHECK(length(key_digest)=64), prefix text NOT NULL,
        name text NOT NULL, capabilities text[] NOT NULL CHECK(cardinality(capabilities)>0),
        created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        expires_at timestamptz, revoked_at timestamptz,
        FOREIGN KEY(project_id,org_id) REFERENCES projects(id,org_id)
    );
    CREATE INDEX ix_g8_credentials_project ON product_api_credentials(project_id,id);
    CREATE TABLE product_api_commands (
        id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id), principal_id text NOT NULL,
        route text NOT NULL, body_digest text NOT NULL CHECK(length(body_digest)=64),
        response bytea, status integer, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CHECK((response IS NULL)=(status IS NULL)), CHECK(octet_length(response)<=8388608)
    );
    CREATE TABLE product_api_audit (
        id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id), principal_id text NOT NULL,
        request_id uuid NOT NULL, event text NOT NULL, resource_id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE FUNCTION product_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP!='TRUNCATE' AND TG_TABLE_NAME='product_project_memberships' AND current_user=(SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=TG_RELID) THEN
        IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
      END IF;
      IF TG_OP='UPDATE' THEN
        IF TG_TABLE_NAME='product_api_credentials' THEN
          IF OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL AND
            (to_jsonb(NEW)-'revoked_at')=(to_jsonb(OLD)-'revoked_at') THEN RETURN NEW; END IF;
        ELSIF TG_TABLE_NAME='product_api_commands' THEN
          IF OLD.response IS NULL AND NEW.response IS NOT NULL AND
            (to_jsonb(NEW)-'response'-'status')=(to_jsonb(OLD)-'response'-'status') THEN RETURN NEW; END IF;
        END IF;
      END IF;
      RAISE EXCEPTION 'immutable product API identity/history';
    END $$;
    CREATE TRIGGER product_membership_write BEFORE INSERT OR UPDATE OR DELETE ON product_project_memberships
      FOR EACH ROW EXECUTE FUNCTION product_identity_guard();
    """)
    for table in ("product_api_credentials", "product_api_commands", "product_api_audit"):
        op.execute(f"CREATE TRIGGER product_guard BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION product_identity_guard()")
    for table in ("product_project_memberships", "product_api_credentials", "product_api_commands", "product_api_audit"):
        op.execute(f"CREATE TRIGGER product_no_truncate BEFORE TRUNCATE ON {table} "
                   "FOR EACH STATEMENT EXECUTE FUNCTION product_identity_guard()")


def downgrade() -> None:
    """禁止抹除认证与外部命令历史."""
    raise RuntimeError("G8_IDENTITY_HISTORY_DOWNGRADE_FORBIDDEN")
