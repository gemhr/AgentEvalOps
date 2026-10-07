// Package citriage 实现 WP00 冻结的 CI episode 确定性评测，不调用模型。
package citriage

import (
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"

	"agentevalops/go-backend/internal/asset"
)

const Implementation = "stage13.ci-triage-deterministic.v1"
const Contract = "stage13.triage-ground-truth.v1"

var Metrics = []string{"ci_failure_category_accuracy.v1", "ci_recommended_action_accuracy.v1", "ci_ticket_decision_accuracy.v1", "ci_root_cause_recall_at_3.v1", "ci_root_cause_mrr_at_3.v1", "ci_root_cause_ndcg_at_3.v1", "ci_triage_decidability.v1"}
var categories = []string{"PRODUCT", "TEST_CASE", "TEST_DATA", "ENVIRONMENT", "TOOL_CHAIN", "INFRASTRUCTURE", "UNKNOWN"}
var actions = []string{"INVESTIGATE_PRODUCT", "FIX_TEST", "FIX_TEST_DATA", "RETRY_ENVIRONMENT", "REPAIR_TOOL_CHAIN", "RESTORE_INFRASTRUCTURE", "REQUEST_MORE_EVIDENCE", "ESCALATE_HUMAN", "NO_ACTION"}
var tickets = []string{"CREATE_PRODUCT_TICKET", "IGNORE", "REQUEST_MORE_EVIDENCE", "ESCALATE_HUMAN"}
var mechanisms = []string{"PRODUCT_BEHAVIOR", "TEST_LOGIC", "TEST_DATA", "ENVIRONMENT_CONFIG", "TOOL_EXECUTION", "INFRASTRUCTURE_SERVICE", "UNKNOWN"}
var evidenceTypes = []string{"CI_SUMMARY", "FAILURE_DETAIL", "ERROR_EXCERPT", "ENVIRONMENT_METADATA", "VERSION_METADATA", "CHANGE_METADATA", "HISTORICAL_KNOWN_FAILURE", "AUTHORIZED_ARTIFACT"}
var safeID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$`)
var digestID = regexp.MustCompile(`^[0-9a-f]{64}$`)

func Has(s string, all []string) bool {
	for _, a := range all {
		if a == s {
			return true
		}
	}
	return false
}
func safe(s string) bool { return len(s) > 0 && len(s) <= 128 && safeID.MatchString(s) }

type Descriptor struct {
	Component string  `json:"component_id"`
	Mechanism string  `json:"mechanism_code"`
	Change    *string `json:"change_ref"`
}

func (d Descriptor) Key() string { j, _ := asset.Freeze(d); return j.String() }
func (d Descriptor) Valid() bool {
	return safe(d.Component) && Has(d.Mechanism, mechanisms) && (d.Change == nil || safe(*d.Change))
}

type Root struct {
	ID          string       `json:"root_cause_id"`
	Relevance   int          `json:"relevance"`
	Descriptors []Descriptor `json:"acceptable_descriptors"`
}
type GroundTruth struct {
	Version        string   `json:"schema_version"`
	Category       string   `json:"ExpectedFailureCategory"`
	Roots          []Root   `json:"RelevantRootCauses"`
	Actions        []string `json:"AcceptableActions"`
	Ticket         string   `json:"ExpectedTicketDecision"`
	Criticality    string   `json:"Criticality"`
	RankingNA      bool     `json:"ranking_not_applicable,omitempty"`
	EvidencePolicy struct {
		Decidable bool     `json:"expected_decidable"`
		Types     []string `json:"required_evidence_types"`
		Missing   string   `json:"missing_behavior"`
	} `json:"EvidencePolicy"`
}

// ReadGroundTruth 在发布和评测边界拒绝缺失字段，避免零值替代冻结标签。
func ReadGroundTruth(j asset.JSON) (GroundTruth, error) {
	var g GroundTruth
	if !fields(j, []string{"schema_version", "ExpectedFailureCategory", "RelevantRootCauses", "AcceptableActions", "ExpectedTicketDecision", "Criticality", "EvidencePolicy"}, "ranking_not_applicable") || j.Decode(&g) != nil || g.Validate() != nil {
		return g, asset.ErrInvalid
	}
	var m map[string]asset.JSON
	_ = j.Decode(&m)
	if !fields(m["EvidencePolicy"], []string{"expected_decidable", "required_evidence_types", "missing_behavior"}) {
		return g, asset.ErrInvalid
	}
	var roots []asset.JSON
	_ = m["RelevantRootCauses"].Decode(&roots)
	for _, root := range roots {
		if !fields(root, []string{"root_cause_id", "relevance", "acceptable_descriptors"}) {
			return g, asset.ErrInvalid
		}
		var r map[string]asset.JSON
		_ = root.Decode(&r)
		var ds []asset.JSON
		_ = r["acceptable_descriptors"].Decode(&ds)
		for _, d := range ds {
			if !fields(d, []string{"component_id", "mechanism_code", "change_ref"}) {
				return g, asset.ErrInvalid
			}
		}
	}
	return g, nil
}

func (g GroundTruth) Validate() error {
	if g.Version != Contract || !Has(g.Category, categories) || !Has(g.Ticket, tickets) || len(g.Actions) == 0 || !Has(g.Criticality, []string{"NORMAL", "CRITICAL"}) || g.EvidencePolicy.Missing != "BLOCKED" || (len(g.Roots) == 0) != g.RankingNA {
		return asset.ErrInvalid
	}
	for _, a := range g.Actions {
		if !Has(a, actions) {
			return asset.ErrInvalid
		}
	}
	for _, a := range g.EvidencePolicy.Types {
		if !Has(a, evidenceTypes) {
			return asset.ErrInvalid
		}
	}
	ids, aliases := map[string]bool{}, map[string]bool{}
	for _, r := range g.Roots {
		if !safe(r.ID) || ids[r.ID] || r.Relevance < 1 || r.Relevance > 3 || len(r.Descriptors) == 0 {
			return asset.ErrInvalid
		}
		ids[r.ID] = true
		for _, d := range r.Descriptors {
			if !d.Valid() || aliases[d.Key()] {
				return asset.ErrInvalid
			}
			aliases[d.Key()] = true
		}
	}
	return nil
}

type EvidenceRef struct {
	ID     string `json:"evidence_id"`
	Digest string `json:"digest"`
	Type   string `json:"type"`
}
type Candidate struct {
	ID         string     `json:"candidate_id"`
	Rank       int        `json:"rank"`
	Summary    string     `json:"summary"`
	Descriptor Descriptor `json:"cause_descriptor"`
	Refs       []string   `json:"evidence_refs"`
	Confidence float64    `json:"confidence"`
}
type Output struct {
	Category   string        `json:"FailureCategory"`
	Candidates []Candidate   `json:"RootCauseCandidates"`
	Refs       []EvidenceRef `json:"EvidenceRefs"`
	Action     struct {
		Code        string  `json:"ActionCode"`
		Explanation *string `json:"Explanation,omitempty"`
	} `json:"RecommendedAction"`
	Confidence float64 `json:"Confidence"`
	Need       bool    `json:"NeedMoreEvidence"`
	Ticket     string  `json:"TicketDecision"`
}

func fields(j asset.JSON, required []string, optional ...string) bool {
	var m map[string]asset.JSON
	if j.Decode(&m) != nil || m == nil {
		return false
	}
	for _, k := range required {
		if v, ok := m[k]; !ok || v.String() == "null" && k != "change_ref" {
			return false
		}
	}
	for k := range m {
		if !Has(k, required) && !Has(k, optional) {
			return false
		}
		if m[k].String() == "null" && k != "change_ref" {
			return false
		}
	}
	return true
}
func unit(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) && v >= 0 && v <= 1 }

// Parse 逐项核对冻结 schema，再核对 rank、可见 descriptor 和已提供证据。
func Parse(raw string, input asset.JSON) (Output, bool, bool) {
	var o Output
	j, e := asset.ParseJSON([]byte(raw))
	if e != nil || len(raw) > 65536 || !fields(j, []string{"FailureCategory", "RootCauseCandidates", "EvidenceRefs", "RecommendedAction", "Confidence", "NeedMoreEvidence", "TicketDecision"}) || j.Decode(&o) != nil {
		return o, false, false
	}
	var m map[string]asset.JSON
	_ = j.Decode(&m)
	if !Has(o.Category, categories) || !Has(o.Ticket, tickets) || !Has(o.Action.Code, actions) || !unit(o.Confidence) || len(o.Candidates) > 3 || len(o.Refs) > 32 || !fields(m["RecommendedAction"], []string{"ActionCode"}, "Explanation") || (o.Action.Explanation != nil && utf8.RuneCountInString(*o.Action.Explanation) > 1024) {
		return o, false, false
	}
	if o.Need != (o.Ticket == "REQUEST_MORE_EVIDENCE") || o.Need != (o.Action.Code == "REQUEST_MORE_EVIDENCE") || o.Category == "UNKNOWN" && (!Has(o.Ticket, []string{"REQUEST_MORE_EVIDENCE", "ESCALATE_HUMAN"}) || !Has(o.Action.Code, []string{"REQUEST_MORE_EVIDENCE", "ESCALATE_HUMAN"})) || o.Ticket == "CREATE_PRODUCT_TICKET" && (o.Need || len(o.Candidates) == 0 || len(o.Refs) == 0) {
		return o, false, false
	}
	refs := map[string]EvidenceRef{}
	var refJSON []asset.JSON
	_ = m["EvidenceRefs"].Decode(&refJSON)
	for i, r := range o.Refs {
		if !fields(refJSON[i], []string{"evidence_id", "digest", "type"}) || !safe(r.ID) || !digestID.MatchString(r.Digest) || !Has(r.Type, evidenceTypes) {
			return o, false, false
		}
		if prior, ok := refs[r.ID]; ok && prior == r {
			return o, false, false
		}
		refs[r.ID] = r
	}
	var cs []asset.JSON
	_ = m["RootCauseCandidates"].Decode(&cs)
	for i, c := range o.Candidates {
		var cm map[string]asset.JSON
		_ = cs[i].Decode(&cm)
		if !fields(cs[i], []string{"candidate_id", "rank", "summary", "cause_descriptor", "evidence_refs", "confidence"}) || !fields(cm["cause_descriptor"], []string{"component_id", "mechanism_code", "change_ref"}) || !c.Descriptor.Valid() || !Has(c.ID, []string{"candidate-1", "candidate-2", "candidate-3"}) || c.Rank < 1 || c.Rank > 3 || utf8.RuneCountInString(c.Summary) < 1 || utf8.RuneCountInString(c.Summary) > 1024 || !unit(c.Confidence) || len(c.Refs) < 1 || len(c.Refs) > 8 {
			return o, false, false
		}
		seen := map[string]bool{}
		for _, r := range c.Refs {
			if !safe(r) || seen[r] {
				return o, false, false
			}
			seen[r] = true
		}
	}
	var in map[string]json.RawMessage
	_ = json.Unmarshal(input.Bytes(), &in)
	var supplied []map[string]json.RawMessage
	_ = json.Unmarshal(in["visible_evidence"], &supplied)
	components, changes := map[string]bool{"UNKNOWN_COMPONENT": true}, map[string]bool{}
	addBody := func(body json.RawMessage) {
		var b map[string]json.RawMessage
		if json.Unmarshal(body, &b) != nil {
			var s string
			if json.Unmarshal(body, &s) != nil || json.Unmarshal([]byte(s), &b) != nil {
				return
			}
		}
		for _, k := range []string{"component_scope", "component_ids"} {
			var a []string
			_ = json.Unmarshal(b[k], &a)
			for _, v := range a {
				components[v] = true
			}
		}
		for _, k := range []string{"component", "component_id"} {
			var s string
			_ = json.Unmarshal(b[k], &s)
			if s != "" {
				components[s] = true
			}
		}
		for _, k := range []string{"change_refs", "visible_change_refs"} {
			var a []string
			_ = json.Unmarshal(b[k], &a)
			for _, v := range a {
				changes[v] = true
			}
		}
		for _, k := range []string{"change_ref", "change_id"} {
			var s string
			_ = json.Unmarshal(b[k], &s)
			if s != "" {
				changes[s] = true
			}
		}
	}
	addBody(in["failure_summary"])
	var inventory []json.RawMessage
	_ = json.Unmarshal(in["visible_change_inventory"], &inventory)
	for _, v := range inventory {
		var s string
		if json.Unmarshal(v, &s) == nil {
			changes[s] = true
		} else {
			addBody(v)
		}
	}
	allowed := map[string]EvidenceRef{}
	for _, b := range supplied {
		var r EvidenceRef
		data, _ := json.Marshal(b)
		_ = json.Unmarshal(data, &r)
		var availability string
		_ = json.Unmarshal(b["availability"], &availability)
		if (availability == "" || availability == "AVAILABLE") && len(b["content"]) > 0 && string(b["content"]) != "null" {
			allowed[r.ID] = r
			addBody(b["content"])
		}
	}
	semantic := true
	seenRefs := map[string]bool{}
	for _, r := range o.Refs {
		if seenRefs[r.ID] || allowed[r.ID] != r {
			semantic = false
		}
		seenRefs[r.ID] = true
	}
	seenDescriptors := map[string]bool{}
	for i, c := range o.Candidates {
		if c.Rank != i+1 || c.ID != fmt.Sprintf("candidate-%d", i+1) || seenDescriptors[c.Descriptor.Key()] || !components[c.Descriptor.Component] || c.Descriptor.Change != nil && !changes[*c.Descriptor.Change] {
			semantic = false
		}
		seenDescriptors[c.Descriptor.Key()] = true
		for _, r := range c.Refs {
			if !seenRefs[r] {
				semantic = false
			}
		}
	}
	return o, true, semantic
}

type Scores struct {
	Values        map[string]float64 `json:"scores"`
	SchemaValid   bool               `json:"schema_valid"`
	SemanticValid bool               `json:"semantic_valid"`
	Decidable     bool               `json:"decidable"`
	Critical      string             `json:"critical_status"`
	Reason        string             `json:"failure_reason"`
	Output        Output             `json:"output"`
}

func Score(raw string, input asset.JSON, g GroundTruth) (Scores, error) {
	s := Scores{Values: map[string]float64{}, Critical: "PASS"}
	for _, m := range Metrics {
		s.Values[m] = 0
	}
	if g.Validate() != nil {
		return s, asset.ErrInvalid
	}
	s.Output, s.SchemaValid, s.SemanticValid = Parse(raw, input)
	if !s.SchemaValid || !s.SemanticValid {
		s.Reason = "OUTPUT_INVALID"
		s.Critical = "BLOCKED"
		return s, nil
	}
	var in struct {
		Evidence []struct {
			Type         string     `json:"type"`
			Availability string     `json:"availability"`
			Content      asset.JSON `json:"content"`
		} `json:"visible_evidence"`
	}
	_ = json.Unmarshal(input.Bytes(), &in)
	available := map[string]bool{}
	for _, e := range in.Evidence {
		if (e.Availability == "" || e.Availability == "AVAILABLE") && e.Content.String() != "null" {
			available[e.Type] = true
		}
	}
	for _, t := range g.EvidencePolicy.Types {
		if !available[t] {
			s.Reason = "MISSING_EVIDENCE"
			s.Critical = "BLOCKED"
			return s, nil
		}
	}
	o := s.Output
	if o.Need && g.EvidencePolicy.Decidable {
		s.Reason = "ABSTAINED"
		s.Critical = "BLOCKED"
		return s, nil
	}
	if o.Category == g.Category {
		s.Values[Metrics[0]] = 1
	}
	if Has(o.Action.Code, g.Actions) {
		s.Values[Metrics[1]] = 1
	}
	if o.Ticket == g.Ticket {
		s.Values[Metrics[2]] = 1
	}
	roots := map[string]Root{}
	grades := []int{}
	for _, r := range g.Roots {
		grades = append(grades, r.Relevance)
		for _, d := range r.Descriptors {
			roots[d.Key()] = r
		}
	}
	seen := map[string]bool{}
	dcg := 0.0
	for i, c := range o.Candidates {
		r, ok := roots[c.Descriptor.Key()]
		if !ok || seen[r.ID] {
			continue
		}
		seen[r.ID] = true
		if s.Values[Metrics[4]] == 0 {
			s.Values[Metrics[4]] = 1 / float64(i+1)
		}
		dcg += (math.Pow(2, float64(r.Relevance)) - 1) / math.Log2(float64(i+2))
	}
	if len(g.Roots) > 0 {
		s.Values[Metrics[3]] = float64(len(seen)) / float64(len(g.Roots))
		sort.Sort(sort.Reverse(sort.IntSlice(grades)))
		idcg := 0.0
		for i, r := range grades[:min(3, len(grades))] {
			idcg += (math.Pow(2, float64(r)) - 1) / math.Log2(float64(i+2))
		}
		s.Values[Metrics[5]] = dcg / idcg
	}
	s.Decidable = g.EvidencePolicy.Decidable && o.Category != "UNKNOWN" && !o.Need
	if s.Decidable {
		s.Values[Metrics[6]] = 1
	} else {
		s.Reason = "INSUFFICIENT_CERTAINTY"
		s.Critical = "BLOCKED"
	}
	if g.Criticality == "CRITICAL" && s.Decidable && (s.Values[Metrics[0]] != 1 || s.Values[Metrics[1]] != 1 || s.Values[Metrics[2]] != 1) {
		s.Critical = "FAIL"
		s.Reason = "CRITICAL_WRONG"
	}
	return s, nil
}

// ValidateInput 只允许 WP04 已有可见输入字段；GT 不能经嵌套 evidence 进入 wire。
func ValidateInput(input asset.JSON) error {
	var m map[string]asset.JSON
	if input.Decode(&m) != nil {
		return asset.ErrInvalid
	}
	allowed := []string{"schema_version", "incident_ref", "scope", "environment_samples", "version_samples", "failure_summary", "visible_evidence", "visible_change_inventory", "evidence_policy", "EvidenceRefs"}
	for k := range m {
		if !Has(k, allowed) {
			return asset.ErrInvalid
		}
	}
	var version string
	_ = m["schema_version"].Decode(&version)
	if version != "stage13.triage-input.v1" || len(input.Bytes()) > 131072 {
		return asset.ErrInvalid
	}
	raw := input.String()
	for _, key := range []string{"hidden-root-", "ExpectedFailureCategory", "ExpectedTicketDecision", "AcceptableActions", "RelevantRootCauses", "Criticality", "ground_truth"} {
		if strings.Contains(raw, key) {
			return asset.ErrInvalid
		}
	}
	return nil
}
