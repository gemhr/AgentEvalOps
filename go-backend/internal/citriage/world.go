package citriage

import (
	"fmt"
	"slices"
	"strings"

	"agentevalops/go-backend/internal/asset"
)

const WorldV1 = "stage13.business-world.v1"

// BusinessWorld 是先于fault/GT冻结的受控Development事实，无答案字段。
type BusinessWorld struct {
	Version  string         `json:"schema_version"`
	SourceID string         `json:"source_id"`
	Revision string         `json:"revision"`
	Scope    BusinessScope  `json:"scope"`
	Entities []EntityRef    `json:"entities"`
	Facts    []BusinessFact `json:"relations"`
}

// ControlledFault 只供EvalOps受控派生使用，不进入LocalAgent或visible projection。
type ControlledFault struct {
	ID          string
	Target      EntityRef
	Mechanism   string
	Change      *string
	Category    string
	Actions     []string
	Ticket      string
	Criticality string
}

func FreezeWorld(world BusinessWorld) (asset.JSON, error) {
	if world.Version != WorldV1 || !safe(world.SourceID) || !safe(world.Revision) || world.Entities == nil || world.Facts == nil {
		return asset.JSON{}, asset.ErrInvalid
	}
	// 深复制至不可变JSON；后续调用者修改DTO不会改已冻结世界。
	frozen := freeze(world)
	var owned BusinessWorld
	_ = frozen.Decode(&owned)
	slices.SortFunc(owned.Entities, func(a, b EntityRef) int { return strings.Compare(entityKey(a), entityKey(b)) })
	slices.SortFunc(owned.Facts, func(a, b BusinessFact) int {
		return strings.Compare(relationKey(BusinessRelation{ID: a.ID, Type: a.Type, Subject: a.Subject, Object: a.Object}), relationKey(BusinessRelation{ID: b.ID, Type: b.Type, Subject: b.Subject, Object: b.Object}))
	})
	frozen = freeze(owned)
	if _, err := MaterializeWorldInput(frozen, []string{}); err != nil {
		return asset.JSON{}, err
	}
	return frozen, nil
}

// MaterializeWorldInput 仅接收先冻结的世界和实际可见观察，不接收GT/owner/fault target。
func MaterializeWorldInput(world asset.JSON, observations []string) (asset.JSON, error) {
	var w BusinessWorld
	if strictDecode(world, &w) != nil || w.Version != WorldV1 || !safe(w.SourceID) || !safe(w.Revision) || w.Entities == nil || w.Facts == nil {
		return asset.JSON{}, asset.ErrInvalid
	}
	components, changes := []string{}, []string{}
	for _, e := range w.Entities {
		if e.Kind == "COMPONENT" {
			components = append(components, e.ID)
		}
		if e.Kind == "CHANGE" {
			changes = append(changes, e.ID)
		}
	}
	components = componentOrder(components)
	changes = componentOrder(changes)
	source := EvidenceRef{ID: "world:" + w.SourceID, Digest: world.Digest(), Type: "AUTHORIZED_ARTIFACT"}
	relations := []BusinessRelation{}
	for i, f := range w.Facts {
		relations = append(relations, BusinessRelation{ID: f.ID, Type: f.Type, Subject: f.Subject, Object: f.Object, Source: source, Pointer: fmt.Sprintf("/relations/%d", i), Mode: f.Mode})
	}
	evidence := []any{map[string]any{"evidence_id": source.ID, "digest": source.Digest, "type": source.Type, "availability": "AVAILABLE", "content": world}}
	input := freeze(map[string]any{
		"schema_version": InputV2, "incident_ref": map[string]any{"incident_id": "incident:" + w.SourceID, "evidence_revision": 1, "manifest_digest": freeze(evidence).Digest()}, "scope": w.Scope,
		"environment_samples": []any{map[string]string{"environment_id": "controlled-context", "channel_group": "contract-validation"}}, "version_samples": []string{w.Revision},
		"failure_summary": map[string]any{"component_scope": components, "observations": observations}, "visible_evidence": evidence, "visible_change_inventory": changes,
		"evidence_policy":          map[string]any{"allowed_types": []string{source.Type}, "max_tool_reads": 8, "max_artifact_reads": 4, "max_bytes": 1048576, "job_deadline_seconds": 180},
		"responsibility_semantics": PublicResponsibility{ResponsibilityV2, "REMEDIATION_OWNER", ResponsibilityDefinition},
		"business_context":         map[string]any{"schema_version": RelationsV1, "snapshot_ref": map[string]string{"source_id": w.SourceID, "revision": w.Revision, "digest": world.Digest()}, "entities": w.Entities, "relations": relations},
	})
	return ProjectInputV2(input)
}

// DeriveGroundTruth 世界+fault+责任合同→GT；该函数没有返回或修改Agent输入。
func DeriveGroundTruth(world asset.JSON, fault ControlledFault) (GroundTruth, error) {
	g := GroundTruth{Version: ContractV2, ResponsibilitySemantics: ResponsibilityV2, Category: fault.Category, Actions: slices.Clone(fault.Actions), Ticket: fault.Ticket, Criticality: fault.Criticality}
	input, err := MaterializeWorldInput(world, []string{})
	if err != nil || !safe(fault.ID) {
		return g, asset.ErrInvalid
	}
	in, err := ReadInputV2(input)
	if err != nil {
		return g, err
	}
	owners, support := []string{}, []string{}
	for _, r := range in.Business.Relations {
		if r.Type == "OWNS" && r.Object == fault.Target {
			owners = append(owners, r.Subject.ID)
			support = append(support, r.ID)
		}
		if fault.Change != nil && r.Type == "TARGETS" && r.Subject == (EntityRef{"CHANGE", *fault.Change}) && r.Object == fault.Target {
			support = append(support, r.ID)
		}
	}
	if len(owners) == 0 {
		return g, asset.ErrInvalid
	} // UNKNOWN；禁止按inventory补owner。
	owners = componentOrder(owners)
	slices.Sort(support)
	rr := RootResponsibility{Semantics: ResponsibilityV2, Role: "REMEDIATION_OWNER", Target: fault.Target, Mechanism: fault.Mechanism, Change: fault.Change, Alternatives: []string{}, Relations: support}
	if len(owners) == 1 {
		rr.Owner = &owners[0]
	} else {
		rr.Alternatives = owners
	}
	root := Root{ID: "hidden-root-" + fault.ID, Relevance: 3, Descriptors: []Descriptor{}, Responsibility: &rr}
	for _, owner := range owners {
		root.Descriptors = append(root.Descriptors, Descriptor{owner, fault.Mechanism, fault.Change})
	}
	g.Roots = []Root{root}
	g.EvidencePolicy.Decidable = true
	g.EvidencePolicy.Types = []string{"AUTHORIZED_ARTIFACT"}
	g.EvidencePolicy.Missing = "BLOCKED"
	if ValidateResponsibility(input, g) != nil {
		return g, asset.ErrInvalid
	}
	return g, nil
}
