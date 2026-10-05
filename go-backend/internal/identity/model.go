// Package identity 从认证结果和持久化成员关系建立可信项目权限。
package identity

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"slices"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	rv "agentevalops/go-backend/internal/review"
)

var ErrUnauthenticated = errors.New("UNAUTHENTICATED")

type Capability string

const (
	Read             Capability = "READ"
	Write            Capability = "WRITE"
	Execute          Capability = "EXECUTE"
	Review           Capability = "REVIEW"
	Adjudicate       Capability = "ADJUDICATE"
	PublishGolden    Capability = "PUBLISH_GOLDEN"
	PublishDataset   Capability = "PUBLISH_DATASET"
	ManagePolicy     Capability = "MANAGE_POLICY"
	RunGate          Capability = "RUN_GATE"
	ApproveException Capability = "APPROVE_EXCEPTION"
	ManageAPIKey     Capability = "MANAGE_API_KEY"
)

var All = []Capability{Read, Write, Execute, Review, Adjudicate, PublishGolden, PublishDataset, ManagePolicy, RunGate, ApproveException, ManageAPIKey}

func ValidCapabilities(c []Capability) bool {
	if len(c) == 0 || len(c) > len(All) {
		return false
	}
	seen := map[Capability]bool{}
	for _, v := range c {
		if !slices.Contains(All, v) || seen[v] {
			return false
		}
		seen[v] = true
	}
	return true
}

type Principal struct {
	ID             string       `json:"principal_id"`
	Type           string       `json:"principal_type"`
	OrganizationID string       `json:"organization_id"`
	AuthMethod     string       `json:"auth_method"`
	CredentialID   string       `json:"credential_id"`
	ProjectID      string       `json:"-"`
	Capabilities   []Capability `json:"capabilities"`
}

func (p Principal) Has(c Capability) bool { return slices.Contains(p.Capabilities, c) }

type Access struct {
	Principal Principal
	Scope     asset.Scope
}

func (a Access) Evaluation(epoch int64) ev.Scope {
	return ev.Scope{Scope: a.Scope, Epoch: epoch, Create: a.Principal.Has(Execute)}
}
func (a Access) Review(epoch int64) rv.Scope {
	return rv.Scope{Scope: a.Evaluation(epoch), ReviewerType: a.Principal.Type, IdentitySource: a.Principal.AuthMethod, Queue: a.Principal.Has(Review), Review: a.Principal.Has(Review), Adjudicate: a.Principal.Has(Adjudicate), PublishGolden: a.Principal.Has(PublishGolden), Calibrate: a.Principal.Has(ManagePolicy), Feedback: a.Principal.Has(PublishDataset), ApproveException: a.Principal.Has(ApproveException)}
}

// BearerProvider 是用户认证的边界；machine key 不作为浏览器用户身份。
type BearerProvider interface {
	AuthenticateBearer(context.Context, string) (Principal, error)
}
type Credential struct {
	ID             string       `json:"id"`
	ProjectID      string       `json:"project_id"`
	OrganizationID string       `json:"organization_id"`
	Name           string       `json:"name"`
	Prefix         string       `json:"prefix"`
	Capabilities   []Capability `json:"capabilities"`
	CreatedAt      time.Time    `json:"created_at"`
	ExpiresAt      *time.Time   `json:"expires_at"`
	RevokedAt      *time.Time   `json:"revoked_at"`
}

func NewKey() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return "aep_" + base64.RawURLEncoding.EncodeToString(b), nil
}
func Digest(pepper []byte, key string) string {
	h := hmac.New(sha256.New, pepper)
	h.Write([]byte(key))
	return hex.EncodeToString(h.Sum(nil))
}
func CommandID(principal, project, route, key string) string {
	h := sha256.Sum256([]byte(principal + "\x00" + project + "\x00" + route + "\x00" + key))
	h[6] = (h[6] & 15) | 80
	h[8] = (h[8] & 63) | 128
	v := hex.EncodeToString(h[:16])
	return v[:8] + "-" + v[8:12] + "-" + v[12:16] + "-" + v[16:20] + "-" + v[20:]
}
