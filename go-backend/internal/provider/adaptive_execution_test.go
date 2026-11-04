package provider

import (
	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestAdaptiveLocalTimeoutPreservesUnknownObservation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(100 * time.Millisecond):
		}
	}))
	defer server.Close()
	project := asset.NewID()
	cfg := AdaptiveConfig{Transport: AgentLoopConfig{BaseURL: server.URL, TokenEnv: "PUBLIC_ADAPTIVE_TIMEOUT_TOKEN", TrustedProjectID: project, MaxResponseBytes: 1024 * 1024, CallMilliseconds: 25, HTTP: DefaultHTTPConfig()}, Group: "CANDIDATE_A", PolicyDigest: agentquality.AdaptivePolicyDigest("CANDIDATE_A"), DatasetDigest: strings.Repeat("a", 64)}
	target, e := NewAdaptiveTarget(cfg)
	if e != nil {
		t.Fatal(e)
	}
	defer target.Close()
	invalid := cfg
	invalid.Group = "UNKNOWN"
	invalid.PolicyDigest = ""
	if _, e := NewAdaptiveTarget(invalid); e == nil {
		t.Fatal("unknown frozen strategy accepted")
	}
	t.Setenv(cfg.Transport.TokenEnv, "public-test")
	q := ev.Request{ID: asset.NewID(), RunID: asset.NewID(), AttemptID: asset.NewID(), Target: ev.Target{ID: AdaptiveTargetID, Version: AdaptiveProtocol, Kind: "LOCALAGENT_HTTP", TimeoutMilliseconds: 1000}}
	q.Target.Config, _ = asset.Freeze(cfg)
	q.Case.Identity.ProjectID = project
	q.Case.Identity.Ref = asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	q.Case.Identity.ContentDigest = strings.Repeat("b", 64)
	q.Case.Case.Input, _ = asset.Freeze(AdaptiveInput{Slice: "STATUS", IncidentID: asset.NewID(), ExecutionID: asset.NewID(), ExecutionDigest: strings.Repeat("c", 64)})
	s := ev.Scope{}
	s.ProjectID = project
	before := time.Now()
	out, e := target.Execute(context.Background(), s, q)
	if e != nil || out.Kind != ev.Unknown || len(out.Evidence) != 0 || out.TerminalCertainty != "UNCONFIRMED" {
		t.Fatalf("timeout invented remote terminal: %+v %v", out, e)
	}
	var observed ev.ClientObservation
	if out.Cleanup.Decode(&observed) != nil || observed.ClientRT == nil || *observed.ClientRT < 20 || !observed.Censored || time.Since(before) > time.Second {
		t.Fatalf("bad local RT %+v", observed)
	}
	q.Target.TimeoutMilliseconds = 300001
	rejected, e := target.Execute(context.Background(), s, q)
	if e != nil || rejected.DispatchCertainty != "NOT_DISPATCHED" || rejected.Kind != ev.Failure {
		t.Fatal("invalid config dispatched")
	}
}
