package citriage

import (
	"slices"

	"agentevalops/go-backend/internal/asset"
)

type RootResponsibility struct {
	Semantics    string    `json:"responsibility_semantics"`
	Role         string    `json:"responsibility_role"`
	Owner        *string   `json:"owner_component_id"`
	Target       EntityRef `json:"root_asset_or_operation"`
	Mechanism    string    `json:"mechanism_code"`
	Change       *string   `json:"change_ref"`
	Alternatives []string  `json:"acceptable_owner_alternatives"`
	Relations    []string  `json:"supporting_relation_ids"`
}

func readGroundTruthV2(j asset.JSON) (GroundTruth, error) {
	var g GroundTruth
	if !requiredObject(j, []string{"schema_version", "responsibility_semantics", "ExpectedFailureCategory", "RelevantRootCauses", "AcceptableActions", "ExpectedTicketDecision", "Criticality", "EvidencePolicy"}, []string{"ranking_not_applicable"}, nil) || strictDecode(j, &g) != nil {
		return g, asset.ErrInvalid
	}
	var raw map[string]asset.JSON
	_ = j.Decode(&raw)
	if !requiredObject(raw["EvidencePolicy"], []string{"expected_decidable", "required_evidence_types", "missing_behavior"}, nil, nil) {
		return g, asset.ErrInvalid
	}
	var roots []map[string]asset.JSON
	_ = raw["RelevantRootCauses"].Decode(&roots)
	for _, root := range roots {
		if _, exists := root["responsibility"]; !exists || !requiredObject(freeze(root), []string{"root_cause_id", "relevance", "acceptable_descriptors", "responsibility"}, nil, nil) || !requiredObject(root["responsibility"], []string{"responsibility_semantics", "responsibility_role", "owner_component_id", "root_asset_or_operation", "mechanism_code", "change_ref", "acceptable_owner_alternatives", "supporting_relation_ids"}, nil, []string{"owner_component_id", "change_ref"}) {
			return g, asset.ErrInvalid
		}
		var ds []asset.JSON
		_ = root["acceptable_descriptors"].Decode(&ds)
		for _, d := range ds {
			if !fields(d, []string{"component_id", "mechanism_code", "change_ref"}) {
				return g, asset.ErrInvalid
			}
		}
	}
	if g.Version != ContractV2 || g.Validate() != nil {
		return g, asset.ErrInvalid
	}
	return g, nil
}

func validateRootResponsibility(root Root) error {
	r := root.Responsibility
	if r == nil || r.Semantics != ResponsibilityV2 || r.Role != "REMEDIATION_OWNER" || !validEntity(r.Target) || !material(r.Target.Kind) || !Has(r.Mechanism, mechanisms[:len(mechanisms)-1]) || r.Change != nil && !safe(*r.Change) || r.Alternatives == nil || len(r.Relations) == 0 || !slices.Equal(r.Relations, componentOrder(r.Relations)) {
		return asset.ErrInvalid
	}
	owners := r.Alternatives
	if r.Owner != nil {
		if !safe(*r.Owner) || len(owners) != 0 {
			return asset.ErrInvalid
		}
		owners = []string{*r.Owner}
	} else if len(owners) < 2 || !slices.Equal(owners, componentOrder(owners)) {
		return asset.ErrInvalid
	}
	for _, id := range append(slices.Clone(owners), r.Relations...) {
		if !safe(id) || id == "UNKNOWN" || id == "UNKNOWN_COMPONENT" {
			return asset.ErrInvalid
		}
	}
	descriptors := map[string]bool{}
	for _, owner := range owners {
		descriptors[(Descriptor{owner, r.Mechanism, r.Change}).Key()] = true
	}
	if len(descriptors) != len(root.Descriptors) {
		return asset.ErrInvalid
	}
	for _, d := range root.Descriptors {
		if !descriptors[d.Key()] {
			return asset.ErrInvalid
		}
	}
	return nil
}

// ValidateResponsibility 在发布与评分前复算可见责任证明，不接受缺边或GT自证。
func ValidateResponsibility(input asset.JSON, g GroundTruth) error {
	if g.Validate() != nil {
		return asset.ErrInvalid
	}
	if g.Version == Contract {
		if InputVersion(input) != "stage13.triage-input.v1" {
			return asset.ErrInvalid
		}
		return nil
	}
	if InputVersion(input) != InputV2 || ValidateInputV2(input) != nil {
		return asset.ErrInvalid
	}
	in, err := ReadInputV2(input)
	if err != nil {
		return err
	}
	relations := map[string]BusinessRelation{}
	for _, r := range in.Business.Relations {
		relations[r.ID] = r
	}
	for _, root := range g.Roots {
		rr := root.Responsibility
		if !slices.Contains(in.Business.Entities, rr.Target) || rr.Change != nil && !Has(*rr.Change, in.Changes) {
			return asset.ErrInvalid
		}
		owners := []string{}
		for _, id := range rr.Relations {
			if _, ok := relations[id]; !ok {
				return asset.ErrInvalid
			}
		}
		for _, r := range in.Business.Relations {
			if r.Type != "OWNS" || r.Object != rr.Target {
				continue
			}
			if !Has(r.ID, rr.Relations) || rr.Owner != nil && *r.Mode != "EXCLUSIVE" || rr.Owner == nil && *r.Mode != "EQUIVALENT" {
				return asset.ErrInvalid
			}
			owners = append(owners, r.Subject.ID)
		}
		expected := rr.Alternatives
		if rr.Owner != nil {
			expected = []string{*rr.Owner}
		}
		if !slices.Equal(componentOrder(owners), expected) {
			return asset.ErrInvalid
		}
		if rr.Change != nil {
			supported := false
			for _, id := range rr.Relations {
				r := relations[id]
				supported = supported || r.Type == "TARGETS" && r.Subject == (EntityRef{"CHANGE", *rr.Change}) && r.Object == rr.Target
			}
			if !supported {
				return asset.ErrInvalid
			}
		}
	}
	return nil
}
