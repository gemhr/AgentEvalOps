package identity

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"strings"
	"time"

	"agentevalops/go-backend/internal/asset"
)

// DevAuth 仅供显式 development/test 装配；身份固定，权限仍从数据库读取。
type DevAuth struct {
	Environment, PrincipalID, OrganizationID, Password string
	SigningKey                                         []byte
	Lifetime                                           time.Duration
}

func (d DevAuth) Validate() error {
	if (d.Environment != "development" && d.Environment != "test") || !asset.ValidID(d.PrincipalID) || !asset.ValidID(d.OrganizationID) || len(d.Password) < 16 || len(d.SigningKey) < 32 || d.Lifetime <= 0 || d.Lifetime > time.Hour {
		return asset.ErrInvalid
	}
	return nil
}

type devClaims struct {
	Principal, Org, Nonce string
	Expiry                int64
}

func (d DevAuth) Login(password string) (string, error) {
	if d.Validate() != nil || subtle.ConstantTimeCompare([]byte(password), []byte(d.Password)) != 1 {
		return "", ErrUnauthenticated
	}
	raw, _ := json.Marshal(devClaims{d.PrincipalID, d.OrganizationID, asset.NewID(), time.Now().Add(d.Lifetime).Unix()})
	data := base64.RawURLEncoding.EncodeToString(raw)
	h := hmac.New(sha256.New, d.SigningKey)
	h.Write([]byte(data))
	return data + "." + base64.RawURLEncoding.EncodeToString(h.Sum(nil)), nil
}
func (d DevAuth) AuthenticateBearer(_ context.Context, token string) (Principal, error) {
	if d.Validate() != nil {
		return Principal{}, ErrUnauthenticated
	}
	data, sig, ok := strings.Cut(token, ".")
	if !ok || len(token) > 2048 {
		return Principal{}, ErrUnauthenticated
	}
	b, err := base64.RawURLEncoding.DecodeString(sig)
	if err != nil {
		return Principal{}, ErrUnauthenticated
	}
	h := hmac.New(sha256.New, d.SigningKey)
	h.Write([]byte(data))
	if !hmac.Equal(b, h.Sum(nil)) {
		return Principal{}, ErrUnauthenticated
	}
	raw, err := base64.RawURLEncoding.DecodeString(data)
	var c devClaims
	if err != nil || json.Unmarshal(raw, &c) != nil || c.Principal != d.PrincipalID || c.Org != d.OrganizationID || time.Now().Unix() >= c.Expiry {
		return Principal{}, ErrUnauthenticated
	}
	return Principal{ID: c.Principal, Type: "HUMAN", OrganizationID: c.Org, AuthMethod: "CONTROLLED_DEV_AUTH", CredentialID: c.Nonce}, nil
}
