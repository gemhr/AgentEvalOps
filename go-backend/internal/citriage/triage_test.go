package citriage

import (
	"math"
	"strings"
	"testing"

	"agentevalops/go-backend/internal/asset"
)

func jf(v any) asset.JSON {
	j, e := asset.Freeze(v)
	if e != nil {
		panic(e)
	}
	return j
}
func fixture(category, action, ticket string) (asset.JSON, GroundTruth, Output) {
	d := Descriptor{Component: "checkout", Mechanism: "PRODUCT_BEHAVIOR"}
	ref := EvidenceRef{ID: "evidence:one", Digest: strings.Repeat("a", 64), Type: "FAILURE_DETAIL"}
	input := jf(map[string]any{"schema_version": "stage13.triage-input.v1", "failure_summary": map[string]any{"component_scope": []string{"checkout", "catalog"}}, "visible_change_inventory": []string{"change-a"}, "visible_evidence": []any{map[string]any{"evidence_id": ref.ID, "digest": ref.Digest, "type": ref.Type, "availability": "AVAILABLE", "content": "visible symptom"}}})
	g := GroundTruth{Version: Contract, Category: category, Actions: []string{action}, Ticket: ticket, Criticality: "CRITICAL", Roots: []Root{{ID: "hidden-root-test", Relevance: 3, Descriptors: []Descriptor{d}}}}
	g.EvidencePolicy.Decidable = true
	g.EvidencePolicy.Types = []string{"FAILURE_DETAIL"}
	g.EvidencePolicy.Missing = "BLOCKED"
	o := Output{Category: category, Refs: []EvidenceRef{ref}, Candidates: []Candidate{{ID: "candidate-1", Rank: 1, Summary: "visible cause", Descriptor: d, Refs: []string{ref.ID}, Confidence: 0.8}}, Ticket: ticket, Confidence: 0.8}
	o.Action.Code = action
	return input, g, o
}
func TestBusinessMetricsAndCriticalCanaries(t *testing.T) {
	for _, tc := range []struct{ category, action, ticket, wrong string }{{"ENVIRONMENT", "RETRY_ENVIRONMENT", "IGNORE", "CREATE_PRODUCT_TICKET"}, {"TOOL_CHAIN", "REPAIR_TOOL_CHAIN", "IGNORE", "CREATE_PRODUCT_TICKET"}, {"PRODUCT", "INVESTIGATE_PRODUCT", "CREATE_PRODUCT_TICKET", "IGNORE"}} {
		t.Run(tc.category, func(t *testing.T) {
			input, g, o := fixture(tc.category, tc.action, tc.ticket)
			s, e := Score(jf(o).String(), input, g)
			if e != nil || s.Critical != "PASS" || s.Values[Metrics[0]] != 1 || s.Values[Metrics[1]] != 1 || s.Values[Metrics[2]] != 1 {
				t.Fatal(s, e)
			}
			g.Actions = append(g.Actions, "ESCALATE_HUMAN")
			o.Action.Code = "ESCALATE_HUMAN"
			s, _ = Score(jf(o).String(), input, g)
			if s.Values[Metrics[1]] != 1 {
				t.Fatal("acceptable-set", s)
			}
			o.Ticket = tc.wrong
			s, _ = Score(jf(o).String(), input, g)
			if s.Critical != "FAIL" || s.Values[Metrics[2]] != 0 || !s.Decidable {
				t.Fatal("known critical wrong", s)
			}
			o.Category = "UNKNOWN"
			o.Need = true
			o.Ticket = "REQUEST_MORE_EVIDENCE"
			o.Action.Code = "REQUEST_MORE_EVIDENCE"
			s, _ = Score(jf(o).String(), input, g)
			if s.Critical != "BLOCKED" || s.Decidable {
				t.Fatal("unknown", s)
			}
		})
	}
	input, g, o := fixture("PRODUCT", "INVESTIGATE_PRODUCT", "CREATE_PRODUCT_TICKET")
	o.Category = "TEST_CASE"
	s, _ := Score(jf(o).String(), input, g)
	if s.Values[Metrics[0]] != 0 {
		t.Fatal("category must exact", s)
	}
}
func TestRankingDescriptorsAndAliases(t *testing.T) {
	input, g, o := fixture("PRODUCT", "INVESTIGATE_PRODUCT", "CREATE_PRODUCT_TICKET")
	b := Descriptor{Component: "catalog", Mechanism: "TEST_LOGIC"}
	g.Roots = append(g.Roots, Root{ID: "hidden-root-B", Relevance: 2, Descriptors: []Descriptor{b}})
	unrelated := o.Candidates[0]
	unrelated.Descriptor = b
	unrelated.Descriptor.Mechanism = "TEST_DATA"
	a := o.Candidates[0]
	a.ID = "candidate-2"
	a.Rank = 2
	o.Candidates = []Candidate{unrelated, a}
	s, e := Score(jf(o).String(), input, g)
	expected := (7 / math.Log2(3)) / (7 + 3/math.Log2(3))
	if e != nil || s.Values[Metrics[3]] != .5 || s.Values[Metrics[4]] != .5 || math.Abs(s.Values[Metrics[5]]-expected) > 1e-12 {
		t.Fatal(s, e)
	}
	change := "change-a"
	alias := g.Roots[0].Descriptors[0]
	alias.Change = &change
	g.Roots[0].Descriptors = append(g.Roots[0].Descriptors, alias)
	a.ID = "candidate-1"
	a.Rank = 1
	duplicate := a
	duplicate.ID = "candidate-2"
	duplicate.Rank = 2
	duplicate.Descriptor = alias
	third := a
	third.ID = "candidate-3"
	third.Rank = 3
	third.Descriptor = b
	o.Candidates = []Candidate{a, duplicate, third}
	s, _ = Score(jf(o).String(), input, g)
	want := (7 + 3/math.Log2(4)) / (7 + 3/math.Log2(3))
	if s.Values[Metrics[3]] != 1 || s.Values[Metrics[4]] != 1 || math.Abs(s.Values[Metrics[5]]-want) > 1e-12 {
		t.Fatal("alias must consume slot with zero gain", s)
	}
	g.Roots[0].Descriptors = []Descriptor{alias}
	o.Candidates = []Candidate{a}
	s, _ = Score(jf(o).String(), input, g)
	if s.Values[Metrics[3]] != 0 {
		t.Fatal("null is not wildcard", s)
	}
}
func TestInvalidOutputDenominatorAndIsolation(t *testing.T) {
	input, g, o := fixture("PRODUCT", "INVESTIGATE_PRODUCT", "CREATE_PRODUCT_TICKET")
	good := jf(o).String()
	nullExplanation := strings.Replace(good, `"RecommendedAction":{`, `"RecommendedAction":{"Explanation":null,`, 1)
	bad := []string{"{}", "```json\n" + good + "\n```", good + " trailing", strings.Replace(good, `"Confidence":0.8`, `"Confidence":null`, 1), strings.Replace(good, `"Confidence":0.8`, `"Confidence":NaN`, 1), strings.Replace(good, `"Confidence":0.8`, `"Confidence":0.8,"Confidence":0.8`, 1), strings.Replace(good, `"change_ref":null`, `"unexpected":null`, 1), strings.Replace(good, `"evidence_refs":["evidence:one"]`, `"evidence_refs":[{}]`, 1), strings.Replace(good, `"rank":1`, `"rank":2`, 1), strings.Replace(good, strings.Repeat("a", 64), strings.Repeat("b", 64), 1)}
	bad = append(bad, nullExplanation)
	for i, raw := range bad {
		s, e := Score(raw, input, g)
		if e != nil || s.Reason != "OUTPUT_INVALID" || s.Decidable || s.Critical != "BLOCKED" {
			t.Fatalf("%d: %+v %v", i, s, e)
		}
		for _, v := range s.Values {
			if v != 0 {
				t.Fatalf("invalid must score zero: %+v", s)
			}
		}
	}
	if ValidateInput(input) != nil {
		t.Fatal("valid input")
	}
	var gtFields map[string]asset.JSON
	_ = jf(g).Decode(&gtFields)
	delete(gtFields, "EvidencePolicy")
	if _, err := ReadGroundTruth(jf(gtFields)); err == nil {
		t.Fatal("missing GT policy allowed")
	}
	var m map[string]asset.JSON
	_ = input.Decode(&m)
	m["ground_truth"] = jf(g)
	if ValidateInput(jf(m)) == nil {
		t.Fatal("GT leaked")
	}
	g.Roots = append(g.Roots, Root{ID: "another", Relevance: 1, Descriptors: g.Roots[0].Descriptors})
	if g.Validate() == nil {
		t.Fatal("ambiguous descriptor allowed")
	}
}
