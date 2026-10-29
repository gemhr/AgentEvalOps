package provider

import (
	"crypto/sha256"
	"fmt"
	"strings"

	"agentevalops/go-backend/internal/asset"
)

const CISyntheticAuthorizationVersion = "ci-synthetic-attempt-authorization.v1"

// 单进程只允许一个精确 Attempt；不是执行 Claim，也不写入 RunSnapshot。
// 身份声明由运维批准，CredentialDigest 绑定实际发送的凭据；身份签名/DB scope 校验仍归 AC。
type CISyntheticAttemptAuthorization struct {
	Schema           string   `json:"schema_version"`
	Attempt          string   `json:"attempt_id"`
	Run              string   `json:"evaluation_run_id"`
	Project          string   `json:"project_id"`
	Case             string   `json:"case_id"`
	TargetDigest     string   `json:"target_digest"`
	Principal        string   `json:"service_principal_id"`
	Tenant           string   `json:"tenant_id"`
	CredentialDigest string   `json:"credential_digest"`
	Scopes           []string `json:"scopes"`
}

func (a CISyntheticAttemptAuthorization) validate(c CISyntheticConfig, token string) error {
	if a.Schema != CISyntheticAuthorizationVersion || !asset.ValidID(a.Attempt) || !asset.ValidID(a.Run) ||
		a.Project != c.Quality.TrustedProjectID || a.Case != c.Frozen.CaseID || !ciDigest(a.TargetDigest) ||
		!asset.ValidID(a.Principal) || !asset.Text(a.Tenant) || len(a.Tenant) > 64 ||
		!ciDigest(a.CredentialDigest) || token == "" || strings.ContainsAny(token, "\r\n") ||
		fmt.Sprintf("%x", sha256.Sum256([]byte(token))) != a.CredentialDigest || len(a.Scopes) != 2 {
		return asset.ErrInvalid
	}
	if !((a.Scopes[0] == "localagent:evaluation:execute" && a.Scopes[1] == "localagent:ci-synthetic:evaluate") ||
		(a.Scopes[1] == "localagent:evaluation:execute" && a.Scopes[0] == "localagent:ci-synthetic:evaluate")) {
		return asset.ErrInvalid
	}
	return nil
}
