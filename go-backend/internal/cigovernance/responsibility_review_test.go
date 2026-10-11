package cigovernance

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
	"os"
	"testing"
)

func TestResponsibilityReviewV2ProofAndDigest(t *testing.T) {
	raw, err := os.ReadFile("../citriage/testdata/world-test-only.json")
	if err != nil {
		t.Fatal(err)
	}
	world, _ := asset.ParseJSON(raw)
	input, err := citriage.MaterializeWorldInput(world, []string{})
	if err != nil {
		t.Fatal(err)
	}
	change := "fixture-change"
	gt, err := citriage.DeriveGroundTruth(world, citriage.ControlledFault{ID: "review-test", Target: citriage.EntityRef{Kind: "ARTIFACT", ID: "fixture-helper"}, Mechanism: "TEST_LOGIC", Change: &change, Category: "TEST_CASE", Actions: []string{"FIX_TEST"}, Ticket: "IGNORE", Criticality: "NORMAL"})
	if err != nil {
		t.Fatal(err)
	}
	in, _ := citriage.ReadInputV2(input)
	truth := gt.Roots[0].Responsibility
	ids := citriage.ContractIdentities()
	r := ReviewDecision{DecisionContractVersion: ResponsibilityReviewV2, State: "CONFIRMED", GroundTruth: Freeze(gt), ResponsibilityReview: &ResponsibilityReview{Semantics: citriage.ResponsibilityV2, SemanticsDigest: ids["responsibility_contract_digest"], InputVersion: citriage.InputV2, InputContractDigest: ids["input_contract_digest"], InputDigest: input.Digest(), BusinessDigest: Freeze(in.Business).Digest(), Roots: []ResponsibilityRootReview{{ID: gt.Roots[0].ID, Target: &truth.Target, Relations: truth.Relations, Mechanism: true, Change: true, Owner: true, Resolution: "UNIQUE", Owners: []string{*truth.Owner}, Provenance: "TEST-ONLY visible OWNS/TARGETS source digest/pointers verified", Causal: "TEST-ONLY controlled fault targets fixture-helper"}}}}
	if r.ValidateResponsibilityInput(input) != nil {
		t.Fatal("complete proof")
	}
	r.Digest = Freeze(r).Digest()
	if r.VerifyDigest() != nil {
		t.Fatal("review digest")
	}
	r.ResponsibilityReview.Roots[0].Causal = "changed reason"
	if r.VerifyDigest() == nil {
		t.Fatal("digest omitted reasons")
	}
	r.ResponsibilityReview.Roots[0].Owners = []string{"component-b"}
	if r.ValidateResponsibilityInput(input) == nil {
		t.Fatal("boolean-only ownership accepted")
	}
	r.ResponsibilityReview.Roots[0].Owners = []string{*truth.Owner}
	r.ResponsibilityReview.Roots[0].Relations = []string{}
	if r.ValidateResponsibilityInput(input) == nil {
		t.Fatal("missing support accepted")
	}
	r.State = "AMBIGUOUS"
	r.GroundTruth = asset.JSON{}
	r.GoldenID = ""
	r.ResponsibilityReview.Roots[0].Resolution = "AMBIGUOUS"
	r.ResponsibilityReview.Roots[0].Owner = false
	if r.ValidateResponsibilityInput(input) != nil {
		t.Fatal("non-hard review")
	}
	r.GoldenID = asset.NewID()
	if r.ValidateResponsibilityInput(input) == nil {
		t.Fatal("ambiguous became Golden")
	}
}
