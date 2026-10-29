package provider

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
)

func syntheticUnitConfig(url string) CISyntheticConfig {
	digest := strings.Repeat("a", 64)
	m, _ := asset.Freeze(aq.QualityManifest{Schema: "agent-quality-execution-manifest.v1", Agents: map[string]aq.QualityAgentPin{"ci_incident_investigator": {Version: "1", Policy: "durable-agent-loop.v2"}}})
	p, _ := asset.Freeze(aq.QualityProfile{ID: "synthetic", Version: 1, Policy: "agent-quality-capture.v1", Aliases: map[string]string{}, ParameterPaths: map[string][]string{}, FactKeys: []string{}})
	return CISyntheticConfig{Quality: AgentQualityConfig{BaseURL: url, TokenEnv: "CI_AUTH_UNIT_TOKEN", TrustedProjectID: asset.NewID(), MaxResponseBytes: 8192, CallMilliseconds: 1000, HTTP: DefaultHTTPConfig(), Manifest: m, Profile: p},
		Frozen: FrozenCI{asset.NewID(), "1", digest, "report_pipeline_failure", "2", digest}, Manifest: digest, Mode: "CANDIDATE", Prompt: digest, Candidate: "ci-prompt-" + digest}
}

// 仅测适配准入，不替代真实 Kernel 生成 Attempt 的 PG 集成测试。
func TestCISyntheticAttemptAuthorization(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer synthetic-token" {
			t.Error("credential snapshot changed")
		}
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()
	t.Setenv("CI_AUTH_UNIT_TOKEN", "synthetic-token")
	c := syntheticUnitConfig(server.URL)
	config, _ := asset.Freeze(c)
	input, _ := asset.Freeze(CISyntheticCaseInput{c.Frozen.Template, c.Frozen.TemplateVersion, c.Frozen.InputDigest})
	q := ev.Request{ID: asset.NewID(), RunID: asset.NewID(), AttemptID: asset.NewID(), Target: ev.Target{ID: CISyntheticTargetID, Kind: "LOCALAGENT_HTTP", Version: CISyntheticTargetVersion, Config: config, TimeoutMilliseconds: 1000}, Case: ev.CaseInput{Identity: ev.AssetIdentity{ProjectID: c.Quality.TrustedProjectID, Ref: asset.Ref{EntityID: c.Frozen.CaseID, Version: c.Frozen.CaseVersion}, ContentDigest: c.Frozen.CaseDigest}}}
	q.Case.Case.Input = input
	a := testCIAuthorization(c, q, "synthetic-token")
	target, e := NewCISyntheticTarget(c, a)
	if e != nil {
		t.Fatal(e)
	}
	defer target.Close()
	scope := ev.Scope{}
	scope.ProjectID = c.Quality.TrustedProjectID
	for _, mutate := range []func(*ev.Request){
		func(q *ev.Request) { q.AttemptID = asset.NewID() },
		func(q *ev.Request) { q.RunID = asset.NewID() },
		func(q *ev.Request) { q.Case.Identity.ProjectID = asset.NewID() },
		func(q *ev.Request) { q.Case.Identity.Ref.EntityID = asset.NewID() },
		func(q *ev.Request) { q.Target.Version = "ci-synthetic-target.v1" },
		func(q *ev.Request) {
			changed := c
			changed.Mode = "BASELINE"
			q.Target.Config, _ = asset.Freeze(changed)
		},
		func(q *ev.Request) {
			changed := c
			changed.Manifest = strings.Repeat("b", 64)
			q.Target.Config, _ = asset.Freeze(changed)
		},
	} {
		bad := q
		mutate(&bad)
		out, e := target.Execute(context.Background(), scope, bad)
		if e != nil || out.DispatchCertainty != "NOT_DISPATCHED" || calls.Load() != 0 {
			t.Fatal("unauthorized dispatch")
		}
	}
	for _, mutate := range []func(*CISyntheticAttemptAuthorization){
		func(a *CISyntheticAttemptAuthorization) { *a = CISyntheticAttemptAuthorization{} },
		func(a *CISyntheticAttemptAuthorization) { a.Project = asset.NewID() },
		func(a *CISyntheticAttemptAuthorization) { a.Case = asset.NewID() },
		func(a *CISyntheticAttemptAuthorization) { a.CredentialDigest = strings.Repeat("b", 64) },
		func(a *CISyntheticAttemptAuthorization) { a.Scopes = []string{"localagent:evaluation:execute"} },
	} {
		bad := a
		mutate(&bad)
		if unexpected, e := NewCISyntheticTarget(c, bad); e == nil {
			unexpected.Close()
			t.Fatal("invalid runtime authorization accepted")
		}
	}
	t.Setenv("CI_AUTH_UNIT_TOKEN", "changed-after-start")
	out, e := target.Execute(context.Background(), scope, q)
	if e != nil || out.Kind != ev.Unknown || calls.Load() != 1 {
		t.Fatal("authorized Pending dispatch must stay UNKNOWN")
	}
}
