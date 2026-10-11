package citriage

import (
	"bytes"
	"crypto/sha256"
	"embed"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"agentevalops/go-backend/internal/asset"
)

const InputV2 = "stage13.triage-input.v2"
const ContractV2 = "stage13.triage-ground-truth.v2"
const ImplementationV2 = "stage13.ci-triage-deterministic.v2"
const ResponsibilityV2 = "stage13.root-responsibility.v2"
const RelationsV1 = "stage13.business-relations.v1"
const ProjectionV1 = "stage13.triage-relation-projection.v1"
const ComparisonDomainDigest = "1093a3aaf0636a18b7d0604751b2546230289d9eaf39e11702a18f367a56c92f"
const ResponsibilityDefinition = "对消除该根因所必须修改的代码、配置、数据资产、测试逻辑或基础设施资源具有维护责任的 component。"

var businessDate = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}$`)
var gtFieldText = regexp.MustCompile(`(?i)"(?:expected_component|root_component|ground_truth_owner|ground_truth|RelevantRootCauses|ExpectedFailureCategory|AcceptableActions|ExpectedTicketDecision|Criticality|owner_component_id|acceptable_owner_alternatives|root_asset_or_operation|review_decision|responsibility_review|hidden_root_id|expected_descriptor|selected_root|selected_root_target|faulty)"\s*:`)

//go:embed contracts_v2/*.json
var contractFiles embed.FS

func contractJSON(name string) asset.JSON {
	raw, err := contractFiles.ReadFile("contracts_v2/" + name)
	if err != nil {
		panic(err)
	}
	j, err := asset.ParseJSON(raw)
	if err != nil {
		panic(err)
	}
	return j
}
func ContractIdentities() map[string]string {
	var result map[string]string
	if contractJSON("identities.json").Decode(&result) != nil {
		panic("INVALID_FROZEN_IDENTITIES")
	}
	return result
}
func init() {
	// 固定定义的摘要来自已冻结决策，资产漂移必须失败而非继续声明旧身份。
	expected := map[string]string{
		"responsibility-semantics.json":   "2a458f1ee3038f2c807fb514568e877eee63d32436032717dffb6323c03f7be7",
		"business-relation-contract.json": "10267ebd263e40f1da4bb9e2417c0239e29dc9bdf3c94a75ed5e61fdbd924455",
		"input-contract-v2.json":          "3d880f9a6d3ffcf35ee6d84143f704161a516a22591fc3d94d950e5dd3ba682c",
		"projection-contract.json":        "0caf84bffd1cbdffa48a0bc7facb84375a2ace6a16b164316422f4a87555dd4e",
		"gt-contract-v2.json":             "1706cf1c02ed8408ff2cd3b8520137191b397b8ad8d57300735cc42bb5d19863",
	}
	for name, digest := range expected {
		if contractJSON(name).Digest() != digest {
			panic("FROZEN_CONTRACT_DRIFT: " + name)
		}
	}
	for definition, schema := range map[string]string{"input-contract-v2.json": "triage-input-v2.schema.json", "business-relation-contract.json": "business-relations.schema.json", "gt-contract-v2.json": "triage-ground-truth-v2.schema.json"} {
		var d map[string]asset.JSON
		_ = contractJSON(definition).Decode(&d)
		if d["schema"].String() != contractJSON(schema).String() {
			panic("FROZEN_SCHEMA_DRIFT")
		}
	}
	var gt map[string]asset.JSON
	_ = contractJSON("gt-contract-v2.json").Decode(&gt)
	var rootSchema map[string]asset.JSON
	_ = contractJSON("root-responsibility-v2.schema.json").Decode(&rootSchema)
	delete(rootSchema, "$id")
	delete(rootSchema, "$schema")
	if gt["responsibility_schema"].String() != freeze(rootSchema).String() {
		panic("FROZEN_SCHEMA_DRIFT")
	}
	ids := ContractIdentities()
	if ids["input_contract_version"] != InputV2 || ids["input_contract_digest"] != expected["input-contract-v2.json"] || ids["responsibility_contract_version"] != ResponsibilityV2 || ids["responsibility_contract_digest"] != expected["responsibility-semantics.json"] || ids["projection_version"] != ProjectionV1 || ids["projection_digest"] != expected["projection-contract.json"] {
		panic("FROZEN_IDENTITY_DRIFT")
	}
	if ValidateContractIdentity(ComparisonDomain()) != nil || ComparisonDomain().Digest() != ComparisonDomainDigest {
		panic("FROZEN_DOMAIN_DRIFT")
	}
}
func ComparisonDomain() asset.JSON { return contractJSON("comparison-domain.json") }
func ValidateContractIdentity(value asset.JSON) error {
	var actual map[string]asset.JSON
	if value.Decode(&actual) != nil {
		return asset.ErrInvalid
	}
	for key, expected := range ContractIdentities() {
		var s string
		if actual[key].Decode(&s) != nil || s != expected {
			return asset.ErrInvalid
		}
	}
	return nil
}
func InputVersion(input asset.JSON) string {
	var header struct {
		Version string `json:"schema_version"`
	}
	_ = input.Decode(&header)
	return header.Version
}
func IsImplementation(s string) bool { return s == Implementation || s == ImplementationV2 }
func MetricsForVersion(version string) []string {
	if version == ContractV2 || version == ImplementationV2 {
		return []string{Metrics[0], Metrics[1], Metrics[2], "ci_root_cause_recall_at_3.v2", "ci_root_cause_mrr_at_3.v2", "ci_root_cause_ndcg_at_3.v2", Metrics[6]}
	}
	return slices.Clone(Metrics)
}

type EntityRef struct {
	Kind string `json:"kind"`
	ID   string `json:"id"`
}
type BusinessFact struct {
	ID      string    `json:"relation_id"`
	Type    string    `json:"relation_type"`
	Subject EntityRef `json:"subject"`
	Object  EntityRef `json:"object"`
	Mode    *string   `json:"ownership_mode,omitempty"`
}
type BusinessRelation struct {
	ID      string      `json:"relation_id"`
	Type    string      `json:"relation_type"`
	Subject EntityRef   `json:"subject"`
	Object  EntityRef   `json:"object"`
	Source  EvidenceRef `json:"source_evidence_ref"`
	Pointer string      `json:"source_pointer"`
	Mode    *string     `json:"ownership_mode,omitempty"`
}
type BusinessContext struct {
	Version  string `json:"schema_version"`
	Snapshot struct {
		ID       string `json:"source_id"`
		Revision string `json:"revision"`
		Digest   string `json:"digest"`
	} `json:"snapshot_ref"`
	Entities  []EntityRef        `json:"entities"`
	Relations []BusinessRelation `json:"relations"`
}
type PublicResponsibility struct {
	Version    string `json:"version"`
	Role       string `json:"role"`
	Definition string `json:"definition"`
}
type BusinessScope struct {
	Project string `json:"automation_project_id"`
	Suite   string `json:"suite_id"`
	Date    string `json:"business_date"`
}
type Input2 struct {
	Version  string `json:"schema_version"`
	Incident struct {
		ID       string `json:"incident_id"`
		Revision int    `json:"evidence_revision"`
		Digest   string `json:"manifest_digest"`
	} `json:"incident_ref"`
	Scope        BusinessScope `json:"scope"`
	Environments []struct {
		ID      string `json:"environment_id"`
		Channel string `json:"channel_group"`
	} `json:"environment_samples"`
	Versions []string                `json:"version_samples"`
	Summary  map[string]asset.JSON   `json:"failure_summary"`
	Evidence []map[string]asset.JSON `json:"visible_evidence"`
	Changes  []string                `json:"visible_change_inventory"`
	Policy   struct {
		Types     []string `json:"allowed_types"`
		Reads     int      `json:"max_tool_reads"`
		Artifacts int      `json:"max_artifact_reads"`
		Bytes     int      `json:"max_bytes"`
		Seconds   int      `json:"job_deadline_seconds"`
		Deadline  *string  `json:"deadline_at,omitempty"`
	} `json:"evidence_policy"`
	Refs           []EvidenceRef        `json:"EvidenceRefs,omitempty"`
	Responsibility PublicResponsibility `json:"responsibility_semantics"`
	Business       BusinessContext      `json:"business_context"`
}

func strictDecode(j asset.JSON, dst any) error {
	d := json.NewDecoder(bytes.NewReader(j.Bytes()))
	d.DisallowUnknownFields()
	if err := d.Decode(dst); err != nil {
		return asset.ErrInvalid
	}
	if d.Decode(new(any)) != io.EOF {
		return asset.ErrInvalid
	}
	return nil
}

// requiredObject 区分缺失与零值；nullable 必须存在但允许null。
func requiredObject(j asset.JSON, required, optional, nullable []string) bool {
	var m map[string]asset.JSON
	if j.Decode(&m) != nil || m == nil {
		return false
	}
	for _, key := range required {
		v, ok := m[key]
		if !ok || v.String() == "null" && !Has(key, nullable) {
			return false
		}
	}
	for key := range m {
		if m[key].String() == "null" && !Has(key, nullable) {
			return false
		}
		if !Has(key, required) && !Has(key, optional) {
			return false
		}
	}
	return true
}
func freeze(v any) asset.JSON {
	j, err := asset.Freeze(v)
	if err != nil {
		panic(err)
	}
	return j
}
func validEntity(e EntityRef) bool {
	return safe(e.ID) && Has(e.Kind, []string{"COMPONENT", "ARTIFACT", "TEST_LOGIC", "OPERATION", "RESOURCE", "CONFIGURATION", "CHANGE", "INTERVENTION"})
}
func material(kind string) bool {
	return Has(kind, []string{"ARTIFACT", "TEST_LOGIC", "OPERATION", "RESOURCE", "CONFIGURATION"})
}
func entityKey(e EntityRef) string { return e.Kind + "\x00" + e.ID }
func relationKey(r BusinessRelation) string {
	return r.Type + "\x00" + entityKey(r.Subject) + "\x00" + entityKey(r.Object) + "\x00" + r.ID
}
func componentOrder(ids []string) []string {
	result := slices.Clone(ids)
	slices.Sort(result)
	return slices.Compact(result)
}

func rejectGT(value any) bool {
	switch v := value.(type) {
	case map[string]any:
		for key, child := range v {
			if Has(strings.ToLower(key), []string{"expected_component", "root_component", "ground_truth_owner", "ground_truth", "relevantrootcauses", "expectedfailurecategory", "acceptableactions", "expectedticketdecision", "criticality", "owner_component_id", "acceptable_owner_alternatives", "root_asset_or_operation", "review_decision", "responsibility_review", "hidden_root_id", "expected_descriptor", "selected_root", "selected_root_target", "faulty"}) || rejectGT(child) {
				return true
			}
		}
	case []any:
		for _, child := range v {
			if rejectGT(child) {
				return true
			}
		}
	case string:
		if strings.Contains(v, "hidden-root-") || strings.Contains(v, "hidden-eval-root-") || gtFieldText.MatchString(v) {
			return true
		}
		// JSON形式的evidence字符串也必须递归检查。
		var decoded any
		if json.Unmarshal([]byte(v), &decoded) == nil {
			switch decoded.(type) {
			case map[string]any, []any:
				return rejectGT(decoded)
			}
		}
	}
	return false
}
func canonicalSourceSets(value any) bool {
	switch v := value.(type) {
	case map[string]any:
		for key, child := range v {
			if Has(key, []string{"component_ids", "component_scope", "visible_component_inventory"}) {
				raw, ok := child.([]any)
				if !ok {
					return false
				}
				ids := []string{}
				for _, item := range raw {
					id, ok := item.(string)
					if !ok || !safe(id) {
						return false
					}
					ids = append(ids, id)
				}
				if !slices.Equal(ids, componentOrder(ids)) {
					return false
				}
			}
			if !canonicalSourceSets(child) {
				return false
			}
		}
	case []any:
		for _, child := range v {
			if !canonicalSourceSets(child) {
				return false
			}
		}
	}
	return true
}
func pointerValue(value any, pointer string) (any, bool) {
	if !strings.HasPrefix(pointer, "/") || len(pointer) > 1024 {
		return nil, false
	}
	for _, token := range strings.Split(pointer[1:], "/") {
		for i := 0; i < len(token); i++ {
			if token[i] == '~' {
				if i+1 >= len(token) || (token[i+1] != '0' && token[i+1] != '1') {
					return nil, false
				}
				i++
			}
		}
		token = strings.ReplaceAll(strings.ReplaceAll(token, "~1", "/"), "~0", "~")
		switch v := value.(type) {
		case map[string]any:
			var ok bool
			value, ok = v[token]
			if !ok {
				return nil, false
			}
		case []any:
			n, err := strconv.Atoi(token)
			if err != nil || n < 0 || n >= len(v) || strconv.Itoa(n) != token {
				return nil, false
			}
			value = v[n]
		default:
			return nil, false
		}
	}
	return value, true
}
func validEndpoints(r BusinessRelation) bool {
	switch r.Type {
	case "OWNS":
		return r.Subject.Kind == "COMPONENT" && material(r.Object.Kind) && r.Mode != nil && Has(*r.Mode, []string{"EXCLUSIVE", "EQUIVALENT"})
	case "PRODUCES_FOR":
		return r.Mode == nil && Has(r.Subject.Kind, []string{"ARTIFACT", "OPERATION"}) && r.Object.Kind == "OPERATION"
	case "CALLS":
		return r.Mode == nil && r.Subject.Kind == "OPERATION" && r.Object.Kind == "OPERATION"
	case "DEPENDS_ON":
		return r.Mode == nil && r.Subject.Kind == "OPERATION" && Has(r.Object.Kind, []string{"OPERATION", "RESOURCE", "CONFIGURATION"})
	case "TARGETS":
		return r.Mode == nil && Has(r.Subject.Kind, []string{"CHANGE", "INTERVENTION"}) && material(r.Object.Kind)
	}
	return false
}

// ReadInputV2 校验真实来源和显式端点；不选择或推断根因。
func ReadInputV2(input asset.JSON) (Input2, error) {
	var in Input2
	required := []string{"schema_version", "incident_ref", "scope", "environment_samples", "version_samples", "failure_summary", "visible_evidence", "visible_change_inventory", "evidence_policy", "responsibility_semantics", "business_context"}
	if len(input.Bytes()) > 131072 || !requiredObject(input, required, []string{"EvidenceRefs"}, nil) || strictDecode(input, &in) != nil || in.Version != InputV2 {
		return in, asset.ErrInvalid
	}
	var raw map[string]asset.JSON
	_ = input.Decode(&raw)
	for field, keys := range map[string][]string{"incident_ref": {"incident_id", "evidence_revision", "manifest_digest"}, "scope": {"automation_project_id", "suite_id", "business_date"}, "responsibility_semantics": {"version", "role", "definition"}, "evidence_policy": {"allowed_types", "max_tool_reads", "max_artifact_reads", "max_bytes", "job_deadline_seconds"}, "business_context": {"schema_version", "snapshot_ref", "entities", "relations"}} {
		optional := []string{}
		if field == "evidence_policy" {
			optional = []string{"deadline_at"}
		}
		if !requiredObject(raw[field], keys, optional, nil) {
			return in, asset.ErrInvalid
		}
	}
	var value any
	_ = json.Unmarshal(input.Bytes(), &value)
	if rejectGT(value) || !safe(in.Incident.ID) || in.Incident.Revision < 1 || !digestID.MatchString(in.Incident.Digest) || !safe(in.Scope.Project) || !safe(in.Scope.Suite) || !businessDate.MatchString(in.Scope.Date) || in.Responsibility != (PublicResponsibility{ResponsibilityV2, "REMEDIATION_OWNER", ResponsibilityDefinition}) || len(in.Environments) > 8 || len(in.Evidence) > 32 || in.Policy.Reads < 0 || in.Policy.Reads > 8 || in.Policy.Artifacts < 0 || in.Policy.Artifacts > 4 || in.Policy.Bytes < 0 || in.Policy.Bytes > 1048576 || in.Policy.Seconds != 180 {
		return in, asset.ErrInvalid
	}
	for _, c := range in.Scope.Date {
		if (c < '0' || c > '9') && c != '-' {
			return in, asset.ErrInvalid
		}
	}
	for _, e := range in.Environments {
		if !safe(e.ID) || !safe(e.Channel) {
			return in, asset.ErrInvalid
		}
	}
	for _, v := range in.Versions {
		if !safe(v) {
			return in, asset.ErrInvalid
		}
	}
	for _, kind := range in.Policy.Types {
		if !Has(kind, evidenceTypes) {
			return in, asset.ErrInvalid
		}
	}
	if len(componentOrder(in.Policy.Types)) != len(in.Policy.Types) || len(componentOrder(in.Changes)) != len(in.Changes) || len(in.Refs) > 32 {
		return in, asset.ErrInvalid
	}
	var components []string
	if v, ok := in.Summary["component_scope"]; !ok || v.String() == "null" || v.Decode(&components) != nil {
		return in, asset.ErrInvalid
	}
	if len(componentOrder(components)) != len(components) {
		return in, asset.ErrInvalid
	}
	for _, id := range append(slices.Clone(components), in.Changes...) {
		if !safe(id) {
			return in, asset.ErrInvalid
		}
	}
	evidence := map[string]struct {
		Ref     EvidenceRef
		Content any
	}{}
	allEvidence := map[string]bool{}
	for _, e := range in.Evidence {
		var id, kind, digest, availability string
		if e["evidence_id"].Decode(&id) != nil || e["type"].Decode(&kind) != nil || e["digest"].Decode(&digest) != nil || e["availability"].Decode(&availability) != nil || !safe(id) || !Has(kind, evidenceTypes) || !digestID.MatchString(digest) || allEvidence[id] || !Has(availability, []string{"AVAILABLE", "MISSING", "TRUNCATED"}) {
			return in, asset.ErrInvalid
		}
		allEvidence[id] = true
		if availability != "AVAILABLE" {
			continue
		}
		content, ok := e["content"]
		if !ok || content.String() == "null" {
			return in, asset.ErrInvalid
		}
		var text string
		var decoded any
		if content.Decode(&text) == nil {
			if rawDigestInput(text) != digest {
				return in, asset.ErrInvalid
			}
			if json.Unmarshal([]byte(text), &decoded) != nil {
				decoded = text
			}
		} else {
			if content.Digest() != digest || json.Unmarshal(content.Bytes(), &decoded) != nil {
				return in, asset.ErrInvalid
			}
		}
		if !canonicalSourceSets(decoded) {
			return in, asset.ErrInvalid
		}
		if record, ok := decoded.(map[string]any); ok {
			if scope, exists := record["scope"]; exists && freeze(scope).String() != freeze(in.Scope).String() {
				return in, asset.ErrInvalid
			}
			if record["schema_version"] == WorldV1 && (record["source_id"] != in.Business.Snapshot.ID || record["revision"] != in.Business.Snapshot.Revision || digest != in.Business.Snapshot.Digest) {
				return in, asset.ErrInvalid
			}
		}
		evidence[id] = struct {
			Ref     EvidenceRef
			Content any
		}{EvidenceRef{id, digest, kind}, decoded}
	}
	seenRefs := map[string]bool{}
	for _, r := range in.Refs {
		if !safe(r.ID) || seenRefs[r.ID] || evidence[r.ID].Ref != r {
			return in, asset.ErrInvalid
		}
		seenRefs[r.ID] = true
	}
	b := in.Business
	if b.Version != RelationsV1 || !safe(b.Snapshot.ID) || !safe(b.Snapshot.Revision) || !digestID.MatchString(b.Snapshot.Digest) {
		return in, asset.ErrInvalid
	}
	entities := map[string]bool{}
	for _, e := range b.Entities {
		if !validEntity(e) || entities[entityKey(e)] || e.Kind == "COMPONENT" && !Has(e.ID, components) || e.Kind == "CHANGE" && !Has(e.ID, in.Changes) {
			return in, asset.ErrInvalid
		}
		entities[entityKey(e)] = true
	}
	ids, edges := map[string]bool{}, map[string]bool{}
	var rawBusiness map[string]asset.JSON
	_ = raw["business_context"].Decode(&rawBusiness)
	var rawRelations []map[string]asset.JSON
	_ = rawBusiness["relations"].Decode(&rawRelations)
	ownership := map[string][]BusinessRelation{}
	for i, r := range b.Relations {
		edge := r.Type + "\x00" + entityKey(r.Subject) + "\x00" + entityKey(r.Object)
		if !safe(r.ID) || ids[r.ID] || edges[edge] || !validEndpoints(r) || !entities[entityKey(r.Subject)] || !entities[entityKey(r.Object)] {
			return in, asset.ErrInvalid
		}
		ids[r.ID], edges[edge] = true, true
		if _, provided := rawRelations[i]["ownership_mode"]; provided && r.Mode == nil {
			return in, asset.ErrInvalid
		}
		if r.Type == "OWNS" {
			ownership[entityKey(r.Object)] = append(ownership[entityKey(r.Object)], r)
		}
		source, ok := evidence[r.Source.ID]
		if !ok || source.Ref != r.Source {
			return in, asset.ErrInvalid
		}
		target, ok := pointerValue(source.Content, r.Pointer)
		if !ok {
			return in, asset.ErrInvalid
		}
		var fact BusinessFact
		if strictDecode(freeze(target), &fact) != nil || fact.ID != "" && fact.ID != r.ID || fact.Type != r.Type || fact.Subject != r.Subject || fact.Object != r.Object || (fact.Mode == nil) != (r.Mode == nil) || fact.Mode != nil && *fact.Mode != *r.Mode {
			return in, asset.ErrInvalid
		}
	}
	for _, owners := range ownership {
		if len(owners) > 1 {
			for _, r := range owners {
				if *r.Mode != "EQUIVALENT" {
					return in, asset.ErrInvalid
				}
			}
		}
	}
	return in, nil
}

func rawDigestInput(s string) string { return fmt.Sprintf("%x", sha256.Sum256([]byte(s))) }

func ProjectInputV2(input asset.JSON) (asset.JSON, error) {
	in, err := ReadInputV2(input)
	if err != nil {
		return asset.JSON{}, err
	}
	var components []string
	_ = in.Summary["component_scope"].Decode(&components)
	in.Summary["component_scope"] = freeze(componentOrder(components))
	slices.Sort(in.Changes)
	slices.SortFunc(in.Business.Entities, func(a, b EntityRef) int { return strings.Compare(entityKey(a), entityKey(b)) })
	slices.SortFunc(in.Business.Relations, func(a, b BusinessRelation) int { return strings.Compare(relationKey(a), relationKey(b)) })
	// 保留EvidenceRefs的显式空数组与原正文，不用omitempty改变输入形状。
	var result map[string]asset.JSON
	_ = input.Decode(&result)
	result["failure_summary"], result["visible_change_inventory"], result["business_context"] = freeze(in.Summary), freeze(in.Changes), freeze(in.Business)
	return asset.Freeze(result)
}
func ValidateInputV2(input asset.JSON) error {
	projected, err := ProjectInputV2(input)
	if err != nil || projected.String() != input.String() {
		return asset.ErrInvalid
	}
	return nil
}
func ValidateInputScope(input asset.JSON, projectID string) error {
	if ValidateInput(input) != nil {
		return asset.ErrInvalid
	}
	if InputVersion(input) != InputV2 {
		return nil
	}
	var in Input2
	_ = input.Decode(&in)
	for _, e := range in.Evidence {
		if owner, ok := e["owner_scope_id"]; ok {
			var s string
			if owner.Decode(&s) != nil || s != projectID {
				return asset.ErrForbidden
			}
		}
	}
	return nil
}
