package observation

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
)

func TestTraceGolden(t *testing.T) {
	raw, e := os.ReadFile("../../testdata/trace_golden.json")
	if e != nil {
		t.Fatal(e)
	}
	var vectors []struct {
		ID       string `json:"id"`
		Raw      string `json:"raw"`
		Accepted bool   `json:"accepted"`
		Codec    bool   `json:"codec_only"`
		Expected string `json:"expected_canonical_bytes"`
		Digest   string `json:"expected_sha256"`
	}
	if e = json.Unmarshal(raw, &vectors); e != nil {
		t.Fatal(e)
	}
	for _, v := range vectors {
		t.Run(v.ID, func(t *testing.T) {
			var canonical []byte
			var digest string
			if v.Codec {
				d := json.NewDecoder(strings.NewReader(v.Raw))
				d.UseNumber()
				var value map[string]any
				if e = d.Decode(&value); e != nil {
					t.Fatal(e)
				}
				canonical, e = TraceCanonical(value)
				digest = fmt.Sprintf("%x", sha256.Sum256(canonical))
			} else {
				var d DecodedTrace
				d, e = DecodeTrace([]byte(v.Raw))
				if (e == nil) != v.Accepted {
					t.Fatalf("accepted=%v err=%v", v.Accepted, e)
				}
				if !v.Accepted {
					return
				}
				canonical = d.Canonical
				digest = d.Digest
			}
			expected, _ := base64.StdEncoding.DecodeString(v.Expected)
			if e != nil || !bytes.Equal(canonical, expected) || digest != v.Digest {
				t.Fatalf("golden byte/digest mismatch: %v", e)
			}
		})
	}
	if _, e = ReadTrace(strings.NewReader(strings.Repeat("x", MaxTraceBytes+1))); e == nil {
		t.Fatal("body limit")
	}
	if _, e = DecodeTrace([]byte{0xff}); e == nil {
		t.Fatal("UTF-8")
	}
}

func TestTraceProfileVectors(t *testing.T) {
	type vector struct {
		Name          string `json:"name"`
		Raw           string `json:"raw_utf8"`
		Fingerprint   string `json:"descriptor_fingerprint"`
		Canonical     string `json:"canonical_payload_sha256"`
		CanonicalUTF8 string `json:"expected_canonical_utf8"`
	}
	type artifact struct {
		Version     int      `json:"contract_version"`
		Fingerprint string   `json:"descriptor_fingerprint"`
		Vectors     []vector `json:"vectors"`
	}
	profiles := []struct {
		name, path, source, schema string
		version                    int
		fingerprint                string
	}{
		{"v1-drift-e2bd", "../../testdata/trace_contract/v1-drift-e2bd/golden.json", TraceV1Source, "trace-v1", 1, TraceDriftFingerprint},
		{"v2", "../../testdata/trace_contract/v2/golden.json", TraceV2Source, "trace-v2", 2, TraceV2Fingerprint},
	}
	artifacts := map[string]artifact{}
	for _, profile := range profiles {
		t.Run(profile.name, func(t *testing.T) {
			raw, err := os.ReadFile(profile.path)
			if err != nil {
				t.Fatal(err)
			}
			var a artifact
			if err = json.Unmarshal(raw, &a); err != nil {
				t.Fatal(err)
			}
			if a.Version != profile.version || a.Fingerprint != profile.fingerprint || len(a.Vectors) != 6 {
				t.Fatalf("unexpected artifact identity: version=%d fingerprint=%s vectors=%d", a.Version, a.Fingerprint, len(a.Vectors))
			}
			artifacts[profile.name] = a
			for _, v := range a.Vectors {
				t.Run(v.Name, func(t *testing.T) {
					decoded, err := DecodeTrace([]byte(v.Raw))
					if err != nil {
						t.Fatal(err)
					}
					if v.Fingerprint != profile.fingerprint || decoded.Source != profile.source || decoded.Schema != profile.schema || decoded.Digest != v.Canonical || string(decoded.Canonical) != v.CanonicalUTF8 || !bytes.Contains(decoded.Canonical, []byte(fmt.Sprintf(`"contract_version":%d`, profile.version))) {
						t.Fatalf("unexpected canonicalization: source=%s schema=%s digest=%s", decoded.Source, decoded.Schema, decoded.Digest)
					}
				})
			}
		})
	}

	e2bdRaw := artifacts["v1-drift-e2bd"].Vectors[0].Raw
	v2Raw := artifacts["v2"].Vectors[0].Raw
	invalidProfiles := map[string]string{
		"v1-frozen-with-e2bd-attribute": strings.Replace(e2bdRaw, TraceDriftFingerprint, TraceFingerprint, 1),
		"v1-with-v2-fingerprint":        strings.Replace(e2bdRaw, TraceDriftFingerprint, TraceV2Fingerprint, 1),
		"v2-with-e2bd-fingerprint":      strings.Replace(v2Raw, TraceV2Fingerprint, TraceDriftFingerprint, 1),
		"unknown-version":               strings.Replace(v2Raw, `"contract_version":2`, `"contract_version":3`, 1),
	}
	for name, raw := range invalidProfiles {
		t.Run(name, func(t *testing.T) {
			if _, err := DecodeTrace([]byte(raw)); err == nil {
				t.Fatal("mixed profile was accepted")
			}
		})
	}
}

func TestSamplingFilteringAndCoverage(t *testing.T) {
	ref := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	p := Sampling{Policy: "HASH_PERCENTAGE", Algorithm: "sha256-trace-basis-points", Version: 1, BasisPoints: 5000}
	a := Sample("project", ref, "trace", p, true)
	if a != Sample("project", ref, "trace", p, true) || a.KeyDigest == "" || a.Unit != "trace" {
		t.Fatal("determinism/provenance")
	}
	p.BasisPoints = 0
	if Sample("project", ref, "trace", p, true).Sampled {
		t.Fatal("zero")
	}
	p.BasisPoints = 10000
	if !Sample("project", ref, "trace", p, true).Sampled || Sample("project", ref, "trace", p, false).Sampled {
		t.Fatal("100/eligible")
	}
	errorOnly := true
	r := Rule{Enabled: true, Scope: Step, Trust: "STRICT_AUTHENTICATED", Source: "LOCALAGENT_TRACE_V1", Filter: Filter{Operation: "runtime.step", Error: &errorOnly, Exact: map[string]string{"environment": "test"}}}
	metadata, _ := asset.Freeze(map[string]string{"environment": "test"})
	o := Observation{Ref: Ref{Kind: Step}, Trust: r.Trust, Source: r.Source, Operation: "runtime.step", Status: "ERROR", Metadata: metadata, Completed: time.Now()}
	if !Match(r, o) {
		t.Fatal("filter")
	}
	o.Status = "OK"
	if Match(r, o) {
		t.Fatal("error filter")
	}
	c := Coverage{Eligible: 10, Sampled: 4, WorkCreated: 8, Completed: 6, Decidable: 2}
	c.Calculate()
	if *c.SamplingCoverage != 0.4 || *c.EvaluationCoverage != 0.75 || *c.DecisionCoverage != 0.25 {
		t.Fatal(c)
	}
	empty := Coverage{}
	empty.Calculate()
	if empty.DecisionCoverage != nil {
		t.Fatal("null")
	}
	if Classify(Value{Verdict: "ERROR"}, "task_success.v1") != "EVALUATOR_ERROR" || Classify(Value{Verdict: "FAIL"}, "task_success.v1") != "TASK_NOT_COMPLETED" || Classify(Value{Applicability: asset.MissingEvidence}, "") != "MISSING_EVIDENCE" {
		t.Fatal("source classification")
	}
	missingBody := asset.Applicability{RuleRef: "context-required.v1", RequiredEvidence: []asset.EvidenceRequirement{{Kind: "retrieved_context", Schema: "context.v1", BodyRequired: true}}}
	var binding Binding
	binding.Evaluator.Definition.Availability = asset.ContractOnly
	binding.Evaluator.Applicability = missingBody
	_, state, err := EvidenceState(o, binding)
	if err != nil || state != asset.MissingEvidence {
		t.Fatal("missing body cannot be supplemented", state, err)
	}
	if Classify(Value{Verdict: "ERROR", Category: "SKIPPED_BUDGET"}, "task_success.v1") != "" {
		t.Fatal("budget skip is not quality failure")
	}
}
