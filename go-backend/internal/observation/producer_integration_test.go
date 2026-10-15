//go:build integration

package observation

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
)

// 该跨仓检查必须供应真实 checkout；不以冻结副本代替当前 producer。
func TestCurrentProducerTraceGolden(t *testing.T) {
	root := os.Getenv("LOCALAGENT_TRACE_PRODUCER_ROOT")
	wantProducer := os.Getenv("LOCALAGENT_TRACE_PRODUCER_REVISION")
	wantConsumer := os.Getenv("AGENTEVALOPS_TRACE_CONSUMER_REVISION")
	if root == "" || len(wantProducer) != 40 || len(wantConsumer) != 40 {
		t.Fatal("explicit producer root and both repository revisions are required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	for dir, want := range map[string]string{root: wantProducer, ".": wantConsumer} {
		cmd := exec.CommandContext(ctx, "git", "rev-parse", "HEAD")
		cmd.Dir = dir
		got, err := cmd.Output()
		if err != nil || string(bytes.TrimSpace(got)) != want {
			t.Fatalf("checkout revision mismatch for %s: %v", dir, err)
		}
	}
	directory := t.TempDir()
	cmd := exec.CommandContext(ctx, "uv", "run", "--frozen", "--no-sync", "python", "-m", "scripts.export_trace_contract_vectors", "--output", directory)
	cmd.Dir = root
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("current producer vector generation failed: %v\n%s", err, output)
	}
	type vector struct {
		Name        string `json:"name"`
		Raw         string `json:"raw_utf8"`
		Wire        string `json:"wire_sha256"`
		Canonical   string `json:"expected_canonical_utf8"`
		Digest      string `json:"canonical_payload_sha256"`
		Fingerprint string `json:"descriptor_fingerprint"`
	}
	type manifest struct {
		Identity    string   `json:"contract_identity"`
		Version     int      `json:"contract_version"`
		Fingerprint string   `json:"descriptor_fingerprint"`
		Revision    string   `json:"producer_git_sha"`
		Vectors     []vector `json:"vectors"`
	}
	read := func(path string) []byte {
		t.Helper()
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	var current, frozen manifest
	if err := json.Unmarshal(read(filepath.Join(directory, "golden.json")), &current); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(read("../../testdata/trace_contract/v2/golden.json"), &frozen); err != nil {
		t.Fatal(err)
	}
	if current.Revision != wantProducer || current.Identity != TraceIdentity || current.Version != 2 || current.Fingerprint != TraceV2Fingerprint || len(current.Vectors) != 6 || !reflect.DeepEqual(current.Vectors, frozen.Vectors) {
		t.Fatal("current producer differs from the frozen v2 wire/canonical vectors")
	}
	descriptor, err := asset.ParseJSON(read(filepath.Join(directory, "descriptor.json")))
	if err != nil {
		t.Fatal(err)
	}
	frozenDescriptor, err := asset.ParseJSON(read("../../testdata/trace_contract/v2/descriptor.json"))
	if err != nil || descriptor.String() != frozenDescriptor.String() || descriptor.Digest() != TraceV2Fingerprint {
		t.Fatalf("current descriptor/fingerprint mismatch: %v", err)
	}
	operations := map[string]bool{}
	for _, v := range current.Vectors {
		d, err := DecodeTrace([]byte(v.Raw))
		if err != nil || fmt.Sprintf("%x", sha256.Sum256([]byte(v.Raw))) != v.Wire || d.Digest != v.Digest || string(d.Canonical) != v.Canonical || v.Fingerprint != TraceV2Fingerprint || d.Source != TraceV2Source || d.Schema != "trace-v2" || d.Trace.Operation != v.Name {
			t.Fatalf("current producer vector %s failed strict wire/digest verification: %v", v.Name, err)
		}
		operations[v.Name] = true
	}
	for operation := range traceCategories {
		if !operations[operation] {
			t.Fatalf("current producer omitted %s", operation)
		}
	}
	t.Logf("current producer=%s consumer=%s fingerprint=%s operations=%d", wantProducer, wantConsumer, descriptor.Digest(), len(operations))
}
