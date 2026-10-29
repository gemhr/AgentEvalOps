package bootstrap

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
)

func TestCISyntheticAuthorizationLoadingFailClosed(t *testing.T) {
	config, _ := asset.ParseJSON([]byte(`{"allowed_attempt_id":"obsolete"}`))
	path := filepath.Join(t.TempDir(), "authorization.json")
	if e := os.WriteFile(path, []byte(`{"schema_version":"unknown","attempt_id":"*"}`), 0600); e != nil {
		t.Fatal(e)
	}
	for _, p := range []string{"", path + ".missing", path} {
		if target, e := loadCISyntheticTarget(config, p); e == nil {
			target.Close()
			t.Fatal("invalid configuration accepted")
		}
	}
	// 真实 loader 的正路径及缺文件/未知字段拒绝，不依赖 DB 或模型服务。
	digest := strings.Repeat("a", 64)
	m, _ := asset.Freeze(aq.QualityManifest{Schema: "agent-quality-execution-manifest.v1", Agents: map[string]aq.QualityAgentPin{"ci_incident_investigator": {}}})
	p, _ := asset.Freeze(aq.QualityProfile{ID: "synthetic", Version: 1, Policy: "agent-quality-capture.v1", Aliases: map[string]string{}, ParameterPaths: map[string][]string{}, FactKeys: []string{}})
	c := provider.CISyntheticConfig{Quality: provider.AgentQualityConfig{BaseURL: "http://127.0.0.1:1", TokenEnv: "CI_BOOTSTRAP_UNIT_TOKEN", TrustedProjectID: asset.NewID(), MaxResponseBytes: 8192, CallMilliseconds: 1000, HTTP: provider.DefaultHTTPConfig(), Manifest: m, Profile: p}, Frozen: provider.FrozenCI{CaseID: asset.NewID(), CaseVersion: "1", CaseDigest: digest, Template: "report_pipeline_failure", TemplateVersion: "2", InputDigest: digest}, Mode: "CANDIDATE", Manifest: digest, Prompt: digest, Candidate: "ci-prompt-" + digest}
	config, _ = asset.Freeze(c)
	frozen, _ := asset.Freeze(ev.Target{ID: provider.CISyntheticTargetID, Kind: "LOCALAGENT_HTTP", Version: provider.CISyntheticTargetVersion, Config: config, TimeoutMilliseconds: 1000})
	a := provider.CISyntheticAttemptAuthorization{Schema: provider.CISyntheticAuthorizationVersion, Attempt: asset.NewID(), Run: asset.NewID(), Project: c.Quality.TrustedProjectID, Case: c.Frozen.CaseID, TargetDigest: frozen.Digest(), Principal: asset.NewID(), Tenant: "unit", CredentialDigest: fmt.Sprintf("%x", sha256.Sum256([]byte("unit-token"))), Scopes: []string{"localagent:evaluation:execute", "localagent:ci-synthetic:evaluate"}}
	t.Setenv(c.Quality.TokenEnv, "unit-token")
	for _, badPath := range []string{"", path + ".missing", path} {
		if unexpected, e := loadCISyntheticTarget(config, badPath); e == nil {
			unexpected.Close()
			t.Fatal("missing/invalid authorization accepted")
		}
	}
	authorization, _ := asset.Freeze(a)
	if e := os.WriteFile(path, authorization.Bytes(), 0600); e != nil {
		t.Fatal(e)
	}
	target, e := loadCISyntheticTarget(config, path)
	if e != nil {
		t.Fatal(e)
	}
	target.Close()
	// 未知字段与重复键都必须拒绝，不允许在外置文件中暗藏第二策略。
	for _, raw := range []string{strings.TrimSuffix(authorization.String(), "}") + `,"unknown":true}`, `{"schema_version":"x","schema_version":"y"}`} {
		if e := os.WriteFile(path, []byte(raw), 0600); e != nil {
			t.Fatal(e)
		}
		if unexpected, e := loadCISyntheticTarget(config, path); e == nil {
			unexpected.Close()
			t.Fatal("invalid strict shape accepted")
		}
	}
	t.Setenv("LOCALAGENT_CI_SYNTHETIC_ENABLED", "1")
	if _, _, e := Worker(context.Background(), "", worker.DefaultConfig(), true, nil); e == nil || e.Error() != "CI_SYNTHETIC_EXECUTION_MODE_REQUIRED" {
		t.Fatal("fixture bypass")
	}
}
