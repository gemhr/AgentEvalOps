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
