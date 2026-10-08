package main

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/provider"
	"testing"
)

func TestWP09TargetProofUsesFrozenDimension(t *testing.T) {
	cfg := provider.Stage13Config{ExecutionPolicyVersion: provider.Stage13ExecutionPolicyVersion, Transport: provider.DefaultLocalAgentConfig("http://127.0.0.1:55438", "TOKEN")}
	var fields map[string]asset.JSON
	if freeze(cfg).Decode(&fields) != nil {
		t.Fatal("config decode")
	}
	delete(fields, "execution_policy_version")
	if targetProofDigest(cfg) != freeze(fields).Digest() {
		t.Fatal("proof differs from decision.Offline frozen dimension")
	}
}
