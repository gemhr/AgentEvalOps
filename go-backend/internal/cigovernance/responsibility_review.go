package cigovernance

import (
	"slices"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
)

const ResponsibilityReviewV2 = "stage13.responsibility-review.v2"
const GovernanceV2 = "stage13.dataset-governance.v2"
const RemediationDevelopmentProfile = "stage13-remediation-development.v1"
const RemediationGTMapping = "stage13.remediation-gt-mapping.v1"
const RemediationSplit = "stage13.remediation-development-split.v1"

type ResponsibilityRootReview struct {
	ID          string              `json:"root_cause_id"`
	Target      *citriage.EntityRef `json:"root_asset_or_operation"`
	Relations   []string            `json:"supporting_relation_ids"`
	Mechanism   bool                `json:"mechanism_decidable"`
	Change      bool                `json:"change_decidable"`
	Owner       bool                `json:"owner_decidable"`
	Resolution  string              `json:"ownership_resolution"`
	Owners      []string            `json:"owner_component_ids"`
	Provenance  string              `json:"provenance_reason"`
	Causal      string              `json:"causal_reason"`
	Equivalence *string             `json:"equivalence_reason"`
}
type ResponsibilityReview struct {
	Semantics           string                     `json:"responsibility_semantics"`
	SemanticsDigest     string                     `json:"responsibility_contract_digest"`
	InputVersion        string                     `json:"input_contract_version"`
	InputContractDigest string                     `json:"input_contract_digest"`
	InputDigest         string                     `json:"input_digest"`
	BusinessDigest      string                     `json:"business_context_digest"`
	Roots               []ResponsibilityRootReview `json:"roots"`
}

// ValidateResponsibilityInput 复用既有review生命周期，对事实而非boolean复算。
func (r ReviewDecision) ValidateResponsibilityInput(input asset.JSON) error {
	if citriage.InputVersion(input) != citriage.InputV2 {
		if r.DecisionContractVersion != "" || r.ResponsibilityReview != nil {
			return asset.ErrInvalid
		}
		if HardGolden(r.State) {
			gt, err := citriage.ReadGroundTruth(r.GroundTruth)
			if err != nil || gt.Version != citriage.Contract || citriage.ValidateResponsibility(input, gt) != nil {
				return asset.ErrInvalid
			}
		}
		return nil
	}
	proof := r.ResponsibilityReview
	ids := citriage.ContractIdentities()
	if r.DecisionContractVersion != ResponsibilityReviewV2 || proof == nil || proof.Semantics != citriage.ResponsibilityV2 || proof.SemanticsDigest != ids["responsibility_contract_digest"] || proof.InputVersion != citriage.InputV2 || proof.InputContractDigest != ids["input_contract_digest"] || proof.InputDigest != input.Digest() || citriage.ValidateInputV2(input) != nil || proof.Roots == nil {
		return asset.ErrInvalid
	}
	in, err := citriage.ReadInputV2(input)
	if err != nil || proof.BusinessDigest != Freeze(in.Business).Digest() {
		return asset.ErrInvalid
	}
	seen := map[string]bool{}
	for _, root := range proof.Roots {
		if !asset.Text(root.ID) || seen[root.ID] || root.Owners == nil || root.Relations == nil || !slices.Contains([]string{"UNIQUE", "EQUIVALENT", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE"}, root.Resolution) || !asset.ContentText(root.Provenance) || !asset.ContentText(root.Causal) || root.Resolution == "EQUIVALENT" && (root.Equivalence == nil || !asset.ContentText(*root.Equivalence)) || root.Resolution != "EQUIVALENT" && root.Equivalence != nil {
			return asset.ErrInvalid
		}
		seen[root.ID] = true
	}
	if !HardGolden(r.State) {
		if r.GroundTruth.String() != "null" || r.GoldenID != "" {
			return asset.ErrInvalid
		}
		return nil
	}
	gt, err := citriage.ReadGroundTruth(r.GroundTruth)
	if err != nil || gt.Version != citriage.ContractV2 || citriage.ValidateResponsibility(input, gt) != nil || len(proof.Roots) != len(gt.Roots) {
		return asset.ErrInvalid
	}
	byID := map[string]ResponsibilityRootReview{}
	for _, rr := range proof.Roots {
		byID[rr.ID] = rr
	}
	for _, root := range gt.Roots {
		rr, ok := byID[root.ID]
		truth := root.Responsibility
		owners := truth.Alternatives
		resolution := "EQUIVALENT"
		if truth.Owner != nil {
			owners = []string{*truth.Owner}
			resolution = "UNIQUE"
		}
		if !ok || !rr.Mechanism || !rr.Change || !rr.Owner || rr.Resolution != resolution || rr.Target == nil || *rr.Target != truth.Target || !slices.Equal(rr.Owners, owners) || !slices.Equal(rr.Relations, truth.Relations) {
			return asset.ErrInvalid
		}
	}
	return nil
}

func (r ReviewDecision) VerifyDigest() error {
	if r.DecisionContractVersion != ResponsibilityReviewV2 {
		return nil
	}
	expected := r.Digest
	r.Digest = ""
	if expected == "" || Freeze(r).Digest() != expected {
		return asset.ErrInvalid
	}
	return nil
}
