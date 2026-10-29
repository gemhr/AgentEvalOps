package provider

import (
	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

// Python 定向用例提供生产 Runtime+测试 Provider 的真实 HTTP Producer。
func TestCISyntheticPythonHTTP(t *testing.T) {
	path := os.Getenv("CI_SYNTHETIC_LIVE_CONFIG")
	if path == "" {
		t.Skip("requires actual Python CI producer")
	}
	raw, e := os.ReadFile(path)
	if e != nil {
		t.Fatal(e)
	}
	j, e := asset.ParseJSON(raw)
	if e != nil {
		t.Fatal(e)
	}
	var c CISyntheticConfig
	if j.Decode(&c) != nil {
		t.Fatal("config")
	}
	c.Quality.HTTP = DefaultHTTPConfig()
	config, _ := asset.Freeze(c)
	input, _ := asset.Freeze(CISyntheticCaseInput{c.Frozen.Template, c.Frozen.TemplateVersion, c.Frozen.InputDigest})
	q := ev.Request{ID: asset.NewID(), RunID: asset.NewID(), AttemptID: os.Getenv("CI_SYNTHETIC_LIVE_ATTEMPT"), Target: ev.Target{ID: CISyntheticTargetID, Kind: "LOCALAGENT_HTTP", Version: CISyntheticTargetVersion, TimeoutMilliseconds: 30000, Config: config}}
	q.Case.Identity = ev.AssetIdentity{ProjectID: c.Quality.TrustedProjectID, Ref: asset.Ref{EntityID: c.Frozen.CaseID, Version: c.Frozen.CaseVersion}, ContentDigest: c.Frozen.CaseDigest}
	q.Case.Case.Input = input
	scope := ev.Scope{}
	scope.ProjectID = c.Quality.TrustedProjectID
	a := testCIAuthorization(c, q, os.Getenv(c.Quality.TokenEnv))
	target, e := NewCISyntheticTarget(c, a)
	if e != nil {
		t.Fatal(e)
	}
	defer target.Close()
	out, e := target.Execute(context.Background(), scope, q)
	if e != nil || out.Kind != ev.Success || len(out.Evidence) != 2 {
		t.Fatalf("CI target kind=%s category=%s reason=%s evidence=%d", out.Kind, out.ErrorCategory, out.Reason, len(out.Evidence))
	}
	again, e := target.Execute(context.Background(), scope, q)
	if e != nil || again.Kind != out.Kind || again.Evidence[1].Body.String() != out.Evidence[1].Body.String() {
		t.Fatal("duplicate did not reuse receipt")
	}
	req, _ := http.NewRequest("GET", c.Quality.BaseURL+"/api/runtime/runs/"+q.AttemptID+"/ci-synthetic-evidence/v1", nil)
	req.Header.Set("Authorization", "Bearer "+os.Getenv(c.Quality.TokenEnv))
	res, e := http.DefaultClient.Do(req)
	if e != nil {
		t.Fatal(e)
	}
	defer res.Body.Close()
	wireRaw, e := io.ReadAll(res.Body)
	if e != nil {
		t.Fatal(e)
	}
	var wire ciWire
	j, e = asset.ParseJSON(wireRaw)
	if e != nil || j.Decode(&wire) != nil {
		t.Fatal("wire")
	}
	var quality agentQualityResponse
	_ = wire.Quality.Decode(&quality)
	var bundle aq.QualityBundle
	if quality.Bundle.Decode(&bundle) != nil {
		t.Fatal("bundle")
	}
	if _, e = VerifyCISynthetic(wireRaw, c, bundle.Correlation); e != nil {
		t.Fatal(e)
	}
	bad := c
	bad.Prompt = strings.Repeat("0", 64)
	if _, e = VerifyCISynthetic(wireRaw, bad, bundle.Correlation); e == nil {
		t.Fatal("wrong prompt accepted")
	}
	bad = c
	bad.Frozen.InputDigest = strings.Repeat("0", 64)
	if _, e = VerifyCISynthetic(wireRaw, bad, bundle.Correlation); e == nil {
		t.Fatal("wrong input accepted")
	}
	bad = c
	bad.Manifest = strings.Repeat("0", 64)
	if _, e = VerifyCISynthetic(wireRaw, bad, bundle.Correlation); e == nil {
		t.Fatal("wrong manifest accepted")
	}
	var side ciSidecar
	_ = wire.Sidecar.Decode(&side)
	side.Models[0].Proof.Scope = "MODEL_REQUEST_SENT"
	altered := wire
	altered.Sidecar, _ = asset.Freeze(side)
	altered.Digest = altered.Sidecar.Digest()
	sent, _ := asset.Freeze(altered)
	if _, e = VerifyCISynthetic(sent.Bytes(), c, bundle.Correlation); e == nil {
		t.Fatal("prepared promoted to sent")
	}
	wrong := bundle.Correlation
	wrong.Attempt = asset.NewID()
	if _, e = VerifyCISynthetic(wireRaw, c, wrong); e == nil {
		t.Fatal("wrong attempt accepted")
	}
	wire.Digest = strings.Repeat("0", 64)
	changed, _ := asset.Freeze(wire)
	if _, e = VerifyCISynthetic(changed.Bytes(), c, bundle.Correlation); e == nil {
		t.Fatal("wrong sidecar digest accepted")
	}
	pending := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(202) }))
	defer pending.Close()
	pc := c
	pc.Quality.BaseURL = pending.URL
	pq := q
	pq.Target.Config, _ = asset.Freeze(pc)
	pt, e := NewCISyntheticTarget(pc, testCIAuthorization(pc, pq, os.Getenv(pc.Quality.TokenEnv)))
	if e != nil {
		t.Fatal(e)
	}
	defer pt.Close()
	unknown, e := pt.Execute(context.Background(), scope, pq)
	if e != nil || unknown.Kind != ev.Unknown {
		t.Fatal("pending became success")
	}
	t.Log("CI Runtime/Tool + durable prompt binding + original quality decoder + sidecar: PASS")
}

func testCIAuthorization(c CISyntheticConfig, q ev.Request, token string) CISyntheticAttemptAuthorization {
	frozen, _ := asset.Freeze(q.Target)
	return CISyntheticAttemptAuthorization{Schema: CISyntheticAuthorizationVersion, Attempt: q.AttemptID, Run: q.RunID,
		Project: c.Quality.TrustedProjectID, Case: c.Frozen.CaseID, TargetDigest: frozen.Digest(),
		Principal: asset.NewID(), Tenant: "synthetic-test", CredentialDigest: fmt.Sprintf("%x", sha256.Sum256([]byte(token))),
		Scopes: []string{"localagent:evaluation:execute", "localagent:ci-synthetic:evaluate"}}
}
