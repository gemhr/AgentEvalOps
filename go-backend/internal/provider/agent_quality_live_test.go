package provider

import (
	"context"
	"os"
	"testing"

	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
)

// 由 Python 验收节点启动真实 Producer；未配置时不冒充跨仓 E2E。
func TestAgentQualityLiveHTTP(t *testing.T) {
	path := os.Getenv("AGENT_QUALITY_LIVE_CONFIG")
	if path == "" {
		t.Skip("requires actual Python producer")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	j, err := asset.ParseJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	var c AgentQualityConfig
	if j.Decode(&c) != nil {
		t.Fatal("config")
	}
	c.HTTP = DefaultHTTPConfig()
	j, _ = asset.Freeze(c)
	target, err := NewAgentQualityTarget(c, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer target.Close()
	query, _ := asset.Freeze(map[string]any{"agent_id": "core_router", "query": "public loop_a loop_b synthetic quality"})
	q := ev.Request{ID: asset.NewID(), RunID: asset.NewID(), AttemptID: asset.NewID(), Target: ev.Target{ID: AgentQualityTargetID, Kind: "LOCALAGENT_HTTP", Version: AgentQualityTargetVersion, TimeoutMilliseconds: 30000, Config: j}}
	q.Case.Identity = ev.AssetIdentity{ProjectID: c.TrustedProjectID, Ref: asset.Ref{EntityID: asset.NewID(), Version: "v1"}, ContentDigest: query.Digest()}
	q.Case.Case.Input = query
	scope := ev.Scope{}
	scope.ProjectID = c.TrustedProjectID
	out, err := target.Execute(context.Background(), scope, q)
	if err != nil || out.Kind != ev.Success || len(out.Evidence) != 1 || out.Artifact == nil || out.Artifact.Availability != "AVAILABLE" {
		t.Fatalf("actual producer outcome: kind=%s category=%s evidence=%d", out.Kind, out.ErrorCategory, len(out.Evidence))
	}
	again, err := target.Execute(context.Background(), scope, q)
	if err != nil || again.Kind != out.Kind || len(again.Evidence) != 1 || again.Evidence[0].Body.String() != out.Evidence[0].Body.String() {
		t.Fatal("duplicate must reuse immutable bundle")
	}
}

// 只对已由真实 Producer/Target 验证过的封存产物派生 decoder 反例，不替代 HTTP E2E。
func TestAgentQualityCapturedBundleRejectsTampering(t *testing.T) {
	path := os.Getenv("AGENT_QUALITY_CAPTURED_WIRE")
	if path == "" {
		t.Skip("requires actual sealed producer artifact")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	wire, err := asset.ParseJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	var response agentQualityResponse
	if agentquality.StrictQualityShape(wire.Bytes(), &response) != nil {
		t.Fatal("original wire")
	}
	var original agentquality.QualityBundle
	if response.Bundle.Decode(&original) != nil {
		t.Fatal("original bundle")
	}
	if _, _, err := agentquality.DecodeQuality(response.Bundle, original.Correlation); err != nil {
		t.Fatal("original producer artifact invalid")
	}
	for _, field := range []string{"schema_version", "observations", "execution_manifest_digest"} {
		var altered map[string]asset.JSON
		if response.Bundle.Decode(&altered) != nil {
			t.Fatal("map")
		}
		if field == "observations" {
			delete(altered, field)
		} else {
			altered[field], _ = asset.Freeze("unsupported-or-wrong-digest")
		}
		changed, _ := asset.Freeze(altered)
		if _, _, err := agentquality.DecodeQuality(changed, original.Correlation); err == nil {
			t.Fatal("tampered producer evidence accepted", field)
		}
	}
}
