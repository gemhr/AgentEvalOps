package postgres

import (
	"context"
	"crypto/subtle"
	"errors"
	"time"

	"agentevalops/go-backend/internal/asset"
	ident "agentevalops/go-backend/internal/identity"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const ProductSchema = "c12a00800001"

type ProductIdentity struct {
	Pool   *pgxpool.Pool
	Pepper []byte
}

func (k ProductIdentity) AuthenticateKey(ctx context.Context, key string) (ident.Principal, error) {
	var p ident.Principal
	var digest string
	var caps []string
	if len(k.Pepper) < 32 || len(key) != 47 {
		return p, ident.ErrUnauthenticated
	}
	want := ident.Digest(k.Pepper, key)
	err := k.Pool.QueryRow(ctx, `SELECT id::text,org_id::text,project_id::text,key_digest,capabilities FROM product_api_credentials WHERE key_digest=$1 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>clock_timestamp())`, want).Scan(&p.CredentialID, &p.OrganizationID, &p.ProjectID, &digest, &caps)
	if errors.Is(err, pgx.ErrNoRows) {
		return p, ident.ErrUnauthenticated
	}
	if err != nil {
		return p, err
	}
	if subtle.ConstantTimeCompare([]byte(want), []byte(digest)) != 1 {
		return p, ident.ErrUnauthenticated
	}
	p.ID = "key:" + p.CredentialID
	p.Type = "MACHINE"
	p.AuthMethod = "API_KEY"
	for _, c := range caps {
		p.Capabilities = append(p.Capabilities, ident.Capability(c))
	}
	return p, nil
}
func (k ProductIdentity) Authorize(ctx context.Context, p ident.Principal, project string, cap ident.Capability) (ident.Access, error) {
	a := ident.Access{}
	if !asset.ValidID(project) || !asset.ValidID(p.OrganizationID) {
		return a, asset.ErrNotFound
	}
	if p.Type != "HUMAN" && p.Type != "MACHINE" || p.Type == "HUMAN" && !asset.ValidID(p.ID) {
		return a, ident.ErrUnauthenticated
	}
	if p.Type == "MACHINE" {
		if p.ProjectID != project {
			return a, asset.ErrNotFound
		}
		var exists bool
		if err := k.Pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM projects WHERE id=$1 AND org_id=$2)`, project, p.OrganizationID).Scan(&exists); err != nil {
			return a, err
		}
		if !exists {
			return a, asset.ErrNotFound
		}
	} else {
		var caps []string
		err := k.Pool.QueryRow(ctx, `SELECT pm.capabilities FROM product_project_memberships pm JOIN memberships m ON m.user_id=pm.principal_id AND m.org_id=pm.org_id WHERE pm.project_id=$1 AND pm.org_id=$2 AND pm.principal_id=$3`, project, p.OrganizationID, p.ID).Scan(&caps)
		if err != nil {
			return a, dbError(err)
		}
		p.Capabilities = nil
		for _, c := range caps {
			p.Capabilities = append(p.Capabilities, ident.Capability(c))
		}
	}
	if !p.Has(cap) {
		return a, asset.ErrForbidden
	}
	a.Principal = p
	a.Scope = asset.Scope{ProjectID: project, OrganizationID: p.OrganizationID, Principal: p.ID, CanPublish: p.Has(ident.Write) || p.Has(ident.PublishDataset) || p.Has(ident.ManagePolicy)}
	return a, nil
}
func (k ProductIdentity) CreateCredential(ctx context.Context, a ident.Access, id, name string, caps []ident.Capability, expiry *time.Time) (ident.Credential, string, error) {
	var c ident.Credential
	if !a.Principal.Has(ident.ManageAPIKey) {
		return c, "", asset.ErrForbidden
	}
	if !asset.ValidID(id) || !asset.Text(name) || !ident.ValidCapabilities(caps) || (expiry != nil && !expiry.After(time.Now())) {
		return c, "", asset.ErrInvalid
	}
	values := []string{}
	for _, v := range caps {
		if !a.Principal.Has(v) {
			return c, "", asset.ErrForbidden
		}
		values = append(values, string(v))
	}
	raw, err := ident.NewKey()
	if err != nil {
		return c, "", err
	}
	tag, err := k.Pool.Exec(ctx, `INSERT INTO product_api_credentials(id,project_id,org_id,key_digest,prefix,name,capabilities,created_by,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(id) DO NOTHING`, id, a.Scope.ProjectID, a.Scope.OrganizationID, ident.Digest(k.Pepper, raw), raw[:12], name, values, a.Principal.ID, expiry)
	if err != nil {
		return c, "", dbError(err)
	}
	c, err = k.GetCredential(ctx, a.Scope, id)
	if err != nil {
		return c, "", err
	}
	if tag.RowsAffected() == 0 {
		raw = ""
	}
	return c, raw, nil
}
func (k ProductIdentity) GetCredential(ctx context.Context, s asset.Scope, id string) (ident.Credential, error) {
	var c ident.Credential
	if !asset.ValidID(id) {
		return c, asset.ErrInvalid
	}
	var caps []string
	err := k.Pool.QueryRow(ctx, `SELECT id::text,project_id::text,org_id::text,name,prefix,capabilities,created_at,expires_at,revoked_at FROM product_api_credentials WHERE id=$1 AND project_id=$2 AND org_id=$3`, id, s.ProjectID, s.OrganizationID).Scan(&c.ID, &c.ProjectID, &c.OrganizationID, &c.Name, &c.Prefix, &caps, &c.CreatedAt, &c.ExpiresAt, &c.RevokedAt)
	for _, v := range caps {
		c.Capabilities = append(c.Capabilities, ident.Capability(v))
	}
	return c, dbError(err)
}
func (k ProductIdentity) Revoke(ctx context.Context, a ident.Access, id string) (ident.Credential, error) {
	if !asset.ValidID(id) {
		return ident.Credential{}, asset.ErrInvalid
	}
	if !a.Principal.Has(ident.ManageAPIKey) {
		return ident.Credential{}, asset.ErrForbidden
	}
	_, err := k.Pool.Exec(ctx, `UPDATE product_api_credentials SET revoked_at=clock_timestamp() WHERE id=$1 AND project_id=$2 AND org_id=$3 AND revoked_at IS NULL`, id, a.Scope.ProjectID, a.Scope.OrganizationID)
	if err != nil {
		return ident.Credential{}, err
	}
	return k.GetCredential(ctx, a.Scope, id)
}

type ProductProject struct {
	ID             string    `json:"id"`
	OrganizationID string    `json:"organization_id"`
	Name           string    `json:"name"`
	CreatedAt      time.Time `json:"created_at"`
}

func (k ProductIdentity) Projects(ctx context.Context, p ident.Principal) ([]ProductProject, error) {
	return k.ProjectPage(ctx, p, "", 100)
}
func (k ProductIdentity) ProjectPage(ctx context.Context, p ident.Principal, after string, limit int) ([]ProductProject, error) {
	if !asset.ValidID(p.OrganizationID) || limit < 1 || limit > 101 || after != "" && !asset.ValidID(after) {
		return nil, asset.ErrInvalid
	}
	if p.Type == "MACHINE" && !p.Has(ident.Read) {
		return nil, asset.ErrForbidden
	}
	rows, err := k.Pool.Query(ctx, `SELECT p.id::text,p.org_id::text,p.name,p.created_at FROM projects p WHERE p.org_id=$1 AND (($2='MACHINE' AND p.id=$3::uuid) OR ($2='HUMAN' AND EXISTS(SELECT 1 FROM product_project_memberships pm JOIN memberships m ON m.user_id=pm.principal_id AND m.org_id=pm.org_id WHERE pm.project_id=p.id AND pm.principal_id=$4::uuid AND 'READ'=ANY(pm.capabilities)))) AND ($5::uuid IS NULL OR p.id>$5) ORDER BY p.id LIMIT $6`, p.OrganizationID, p.Type, nullableID(p.ProjectID), humanID(p), nullableID(after), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ProductProject{}
	for rows.Next() {
		var v ProductProject
		if err = rows.Scan(&v.ID, &v.OrganizationID, &v.Name, &v.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
func humanID(p ident.Principal) any {
	if p.Type == "HUMAN" {
		return p.ID
	}
	return nil
}
func (k ProductIdentity) Audit(ctx context.Context, a ident.Access, request, event, resource string) error {
	_, err := k.Pool.Exec(ctx, `INSERT INTO product_api_audit(id,project_id,principal_id,request_id,event,resource_id) VALUES($1,$2,$3,$4,$5,$6)`, asset.NewID(), a.Scope.ProjectID, a.Principal.ID, request, event, resource)
	return err
}
