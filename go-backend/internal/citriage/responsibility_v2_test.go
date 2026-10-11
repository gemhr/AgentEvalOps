package citriage

import (
	"agentevalops/go-backend/internal/asset"
	"os"
	"slices"
	"strings"
	"testing"
	"time"
)

func contractFixture(t *testing.T) (asset.JSON, asset.JSON, GroundTruth) {
	t.Helper()
	raw, err := os.ReadFile("testdata/world-test-only.json")
	if err != nil {
		t.Fatal(err)
	}
	world, err := asset.ParseJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	input, err := MaterializeWorldInput(world, []string{})
	if err != nil {
		t.Fatal(err)
	}
	change := "fixture-change"
	gt, err := DeriveGroundTruth(world, ControlledFault{ID: "test-only", Target: EntityRef{"ARTIFACT", "fixture-helper"}, Mechanism: "TEST_LOGIC", Change: &change, Category: "TEST_CASE", Actions: []string{"FIX_TEST"}, Ticket: "IGNORE", Criticality: "NORMAL"})
	if err != nil {
		t.Fatal(err)
	}
	return world, input, gt
}
func mutateInput(t *testing.T, in asset.JSON, mutate func(map[string]any)) asset.JSON {
	t.Helper()
	var m map[string]any
	if in.Decode(&m) != nil {
		t.Fatal("decode")
	}
	mutate(m)
	return freeze(m)
}
func TestResponsibilityV2WorldProjectionAndScore(t *testing.T) {
	world, input, gt := contractFixture(t)
	raw, _ := os.ReadFile("testdata/input-test-only.json")
	expected, _ := asset.ParseJSON(raw)
	if expected.String() != input.String() || input.Digest() != "13b2ad0f9e43f384eaa29e3ba1cd97d213670801dfcb34b7139e06ee79c5d2e4" {
		t.Fatal("cross-language canonical fixture changed")
	}
	if ValidateInputV2(input) != nil || ValidateResponsibility(input, gt) != nil {
		t.Fatal("valid contract")
	}
	if strings.Contains(input.String(), "hidden-root-") || strings.Contains(input.String(), "owner_component_id") {
		t.Fatal("GT leak")
	}
	frozen, err := FreezeWorld(func() BusinessWorld {
		var w BusinessWorld
		_ = world.Decode(&w)
		slices.Reverse(w.Entities)
		slices.Reverse(w.Facts)
		return w
	}())
	if err != nil || frozen.String() != world.String() {
		t.Fatal("world canonicalization", err)
	}
	// 固定inventory，切换两个目标的owner，Agent inventory保持同一序；排序函数没有GT参数。
	second, err := DeriveGroundTruth(world, ControlledFault{ID: "test-only-b", Target: EntityRef{"CONFIGURATION", "fixture-config"}, Mechanism: "ENVIRONMENT_CONFIG", Category: "ENVIRONMENT", Actions: []string{"RETRY_ENVIRONMENT"}, Ticket: "IGNORE", Criticality: "NORMAL"})
	if err != nil || *second.Roots[0].Responsibility.Owner == *gt.Roots[0].Responsibility.Owner {
		t.Fatal("fixture owners")
	}
	if !slices.Equal(componentOrder([]string{"component-b", "component-a", "component-b"}), []string{"component-a", "component-b"}) {
		t.Fatal("structural sort")
	}
	unchanged, _ := MaterializeWorldInput(world, []string{})
	if unchanged.String() != input.String() {
		t.Fatal("fault changed projection")
	}
	parsed, err := ReadGroundTruth(freeze(gt))
	if err != nil || parsed.Roots[0].Responsibility == nil {
		t.Fatal("GT read", err)
	}
	in, _ := ReadInputV2(input)
	ref := in.Business.Relations[0].Source
	out := Output{Category: gt.Category, Candidates: []Candidate{{ID: "candidate-1", Rank: 1, Summary: "TEST-ONLY contract answer", Descriptor: gt.Roots[0].Descriptors[0], Refs: []string{ref.ID}, Confidence: 0.8}}, Refs: []EvidenceRef{ref}, Confidence: 0.8, Ticket: gt.Ticket}
	out.Action.Code = gt.Actions[0]
	score, err := Score(freeze(out).String(), input, gt)
	if err != nil {
		t.Fatal(err)
	}
	for _, metric := range MetricsForVersion(ContractV2) {
		if score.Values[metric] != 1 {
			t.Fatalf("%s=%v", metric, score.Values[metric])
		}
	}
	out.Candidates[0].Descriptor.Component = "component-b"
	score, err = Score(freeze(out).String(), input, gt)
	if err != nil || score.Values["ci_root_cause_recall_at_3.v2"] != 0 {
		t.Fatal("wrong maintenance owner credited", err)
	}
}

func TestResponsibilityV2RejectsInvalidProvenanceAndLeakage(t *testing.T) {
	_, input, gt := contractFixture(t)
	mutations := map[string]func(map[string]any){
		"version":     func(m map[string]any) { m["schema_version"] = "stage13.triage-input.v3" },
		"unknown-top": func(m map[string]any) { m["unexpected"] = true },
		"GT-nested":   func(m map[string]any) { m["failure_summary"].(map[string]any)["owner_component_id"] = "component-a" },
		"GT-string": func(m map[string]any) {
			m["failure_summary"].(map[string]any)["note"] = `{"ground_truth_owner":"component-a"}`
		},
		"hidden-id": func(m map[string]any) { m["failure_summary"].(map[string]any)["note"] = "hidden-root-test" },
		"pointer": func(m map[string]any) {
			m["business_context"].(map[string]any)["relations"].([]any)[0].(map[string]any)["source_pointer"] = "/relations/999"
		},
		"pointer-type": func(m map[string]any) {
			m["business_context"].(map[string]any)["relations"].([]any)[0].(map[string]any)["relation_type"] = "DEPENDS_ON"
		},
		"missing-source": func(m map[string]any) { m["visible_evidence"].([]any)[0].(map[string]any)["availability"] = "MISSING" },
		"content-digest": func(m map[string]any) {
			m["visible_evidence"].([]any)[0].(map[string]any)["digest"] = strings.Repeat("0", 64)
		},
		"snapshot": func(m map[string]any) {
			m["business_context"].(map[string]any)["snapshot_ref"].(map[string]any)["revision"] = "drift"
		},
		"scope": func(m map[string]any) { m["scope"].(map[string]any)["suite_id"] = "other" },
		"endpoint": func(m map[string]any) {
			m["business_context"].(map[string]any)["relations"].([]any)[0].(map[string]any)["object"].(map[string]any)["id"] = "not-authorized"
		},
		"budget": func(m map[string]any) { m["evidence_policy"].(map[string]any)["max_tool_reads"] = 9 },
	}
	for name, mutation := range mutations {
		t.Run(name, func(t *testing.T) {
			if ValidateInputV2(mutateInput(t, input, mutation)) == nil {
				t.Fatal("accepted")
			}
		})
	}
	noEdges := mutateInput(t, input, func(m map[string]any) { m["business_context"].(map[string]any)["relations"] = []any{} })
	p, err := ProjectInputV2(noEdges)
	if err != nil || ValidateResponsibility(p, gt) == nil {
		t.Fatal("missing edge invented", err)
	}
	legacy := gt
	legacy.Version = Contract
	legacy.ResponsibilitySemantics = ""
	legacy.Roots = append([]Root{}, gt.Roots...)
	legacy.Roots[0].Responsibility = nil
	if ValidateResponsibility(input, legacy) == nil {
		t.Fatal("v1 GT silently rebound")
	}
	sorted := mutateInput(t, input, func(m map[string]any) {
		m["failure_summary"].(map[string]any)["component_scope"] = []string{"component-b", "component-a"}
	})
	if ValidateInputV2(sorted) == nil {
		t.Fatal("noncanonical published input")
	}
	p, err = ProjectInputV2(sorted)
	if err != nil || p.String() != input.String() {
		t.Fatal("canonical projection", err)
	}
}

func TestResponsibilityV2EquivalentOwnersAndConflicts(t *testing.T) {
	world, _, _ := contractFixture(t)
	var w BusinessWorld
	_ = world.Decode(&w)
	mode := "EQUIVALENT"
	for i := range w.Facts {
		if w.Facts[i].ID == "owns-helper" {
			w.Facts[i].Mode = &mode
		}
	}
	w.Facts = append(w.Facts, BusinessFact{ID: "owns-helper-b", Type: "OWNS", Subject: EntityRef{"COMPONENT", "component-b"}, Object: EntityRef{"ARTIFACT", "fixture-helper"}, Mode: &mode})
	world, err := FreezeWorld(w)
	if err != nil {
		t.Fatal(err)
	}
	change := "fixture-change"
	g, err := DeriveGroundTruth(world, ControlledFault{ID: "equivalence-test", Target: EntityRef{"ARTIFACT", "fixture-helper"}, Mechanism: "TEST_LOGIC", Change: &change, Category: "TEST_CASE", Actions: []string{"FIX_TEST"}, Ticket: "IGNORE", Criticality: "NORMAL"})
	if err != nil {
		t.Fatal(err)
	}
	in, _ := MaterializeWorldInput(world, []string{})
	if g.Roots[0].Responsibility.Owner != nil || len(g.Roots[0].Descriptors) != 2 || ValidateResponsibility(in, g) != nil {
		t.Fatal("equivalent complete set")
	}
	input, _ := ReadInputV2(in)
	ref := input.Business.Relations[0].Source
	answer := Output{Category: g.Category, Candidates: []Candidate{{ID: "candidate-1", Rank: 1, Summary: "TEST-ONLY owner alias a", Descriptor: g.Roots[0].Descriptors[0], Refs: []string{ref.ID}, Confidence: 0.8}, {ID: "candidate-2", Rank: 2, Summary: "TEST-ONLY same root alias b", Descriptor: g.Roots[0].Descriptors[1], Refs: []string{ref.ID}, Confidence: 0.8}}, Refs: []EvidenceRef{ref}, Confidence: 0.8, Ticket: g.Ticket}
	answer.Action.Code = g.Actions[0]
	score, err := Score(freeze(answer).String(), in, g)
	if err != nil || score.Values["ci_root_cause_recall_at_3.v2"] != 1 || score.Values["ci_root_cause_mrr_at_3.v2"] != 1 || score.Values["ci_root_cause_ndcg_at_3.v2"] != 1 {
		t.Fatal("same root aliases double-counted", score, err)
	}
	g.Roots[0].Descriptors = g.Roots[0].Descriptors[:1]
	if g.Validate() == nil {
		t.Fatal("partial alternatives")
	}
	conflict := "EXCLUSIVE"
	w.Facts[len(w.Facts)-1].Mode = &conflict
	if _, err = FreezeWorld(w); err == nil {
		t.Fatal("selected first conflicting owner")
	}
}

func TestResponsibilityV2ExecutionReceiptIdentity(t *testing.T) {
	_, input, _ := contractFixture(t)
	m := map[string]any{"agent_id": "test-only-contract-subject"}
	for k, v := range ContractIdentities() {
		m[k] = v
	}
	var domain map[string]any
	_ = ComparisonDomain().Decode(&domain)
	m["output_schema_digest"] = domain["output_schema_digest"]
	manifest := freeze(m)
	start := time.Now().UTC()
	request, err := MaterializeExecution(input, manifest, asset.NewID(), asset.NewID(), 180000, start)
	if err != nil {
		t.Fatal(err)
	}
	var body map[string]asset.JSON
	_ = request.Decode(&body)
	var p ExecutionPolicy
	_ = body["execution_policy"].Decode(&p)
	if p.StartedAt != start || p.DeadlineAt.Sub(start) != 180*time.Second || p.SourceInputDigest != input.Digest() || p.ProjectedDigest != input.Digest() || p.DomainDigest != ComparisonDomain().Digest() {
		t.Fatal(p)
	}
	receipt := map[string]any{"receipt_version": ReceiptV2, "actual_subject_manifest": manifest, "execution_policy": body["execution_policy"], "execution_request_digest": request.Digest()}
	for k, v := range ContractIdentities() {
		receipt[k] = v
	}
	var policy map[string]asset.JSON
	_ = body["execution_policy"].Decode(&policy)
	for _, key := range []string{"source_input_digest", "source_semantic_input_digest", "projected_input_digest", "semantic_input_digest", "comparison_domain_digest"} {
		receipt[key] = policy[key]
	}
	if ValidateReceiptV2(freeze(receipt), manifest, body["execution_policy"], request.Digest()) != nil {
		t.Fatal("valid receipt")
	}
	for _, key := range []string{"receipt_version", "source_input_digest", "projection_digest", "comparison_domain_digest", "execution_request_digest"} {
		t.Run(key, func(t *testing.T) {
			old := receipt[key]
			receipt[key] = "invalid"
			if ValidateReceiptV2(freeze(receipt), manifest, body["execution_policy"], request.Digest()) == nil {
				t.Fatal("receipt drift")
			}
			receipt[key] = old
		})
	}
	delete(m, "projection_digest")
	if _, err = MaterializeExecution(input, freeze(m), asset.NewID(), asset.NewID(), 180000, start); err == nil {
		t.Fatal("partial subject contract")
	}
}

func TestResponsibilityV2RejectsMalformedHardGroundTruthBeforeScoring(t *testing.T) {
	_, input, g := contractFixture(t)
	for _, mode := range []string{"UNKNOWN", "wrong-owner", "missing-support", "owner-null", "mechanism", "semantics", "extra-field", "GT-fragment", "hidden-eval-root"} {
		t.Run(mode, func(t *testing.T) {
			if mode == "GT-fragment" || mode == "hidden-eval-root" {
				bad := mutateInput(t, input, func(m map[string]any) {
					note := `excerpt: {"ground_truth_owner":"component-a"}`
					if mode == "hidden-eval-root" {
						note = "hidden-eval-root-test"
					}
					m["failure_summary"].(map[string]any)["note"] = note
				})
				if ValidateInputV2(bad) == nil {
					t.Fatal("GT text entered input")
				}
				return
			}
			var value map[string]any
			_ = freeze(g).Decode(&value)
			root := value["RelevantRootCauses"].([]any)[0].(map[string]any)
			rr := root["responsibility"].(map[string]any)
			switch mode {
			case "UNKNOWN":
				rr["owner_component_id"] = "UNKNOWN"
			case "wrong-owner":
				rr["owner_component_id"] = "component-b"
			case "missing-support":
				rr["supporting_relation_ids"] = []string{"not-visible"}
			case "owner-null":
				rr["owner_component_id"] = nil
			case "mechanism":
				rr["mechanism_code"] = "TEST_DATA"
			case "semantics":
				rr["responsibility_semantics"] = "stage13.root-responsibility.v1"
			case "extra-field":
				root["inferred_owner"] = "component-a"
			}
			parsed, err := ReadGroundTruth(freeze(value))
			if err == nil {
				_, err = Score(`{}`, input, parsed)
			}
			if err == nil {
				t.Fatal("malformed hard GT scored")
			}
		})
	}
}
