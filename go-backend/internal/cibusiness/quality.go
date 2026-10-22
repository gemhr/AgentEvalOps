// Package cibusiness 只定义冻结业务观察、实际检查回执和确定性判分。
package cibusiness

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"reflect"
	"sort"
	"strings"
)

const InputProtocol = "ci-business-observations.v1"
const RequestProtocol = "ci-business-quality-request.v1"
const OutputProtocol = "ci-business-quality-output.v1"
const Implementation = "ci-business-deterministic.v1"
const Normalization = "ci-business-null-preserving.v1"
const V1 = "parser-compatibility.v1"
const V2 = "parser-compatibility.v2"

var Metrics = []string{"ci_record_consistency.v1", "ci_candidate_relation_agreement.v1", "ci_unsafe_association.v1", "ci_repair_state_consistency.v1"}

func Digest(raw []byte) string { return fmt.Sprintf("%x", sha256.Sum256(raw)) }
func ValidRule(v string) bool  { return v == V1 || v == V2 }
func HasMetric(v string) bool {
	for _, m := range Metrics {
		if m == v {
			return true
		}
	}
	return false
}
func Decode(raw []byte, value any) error {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(value); err != nil {
		return err
	}
	if d.Decode(new(any)) != io.EOF {
		return fmt.Errorf("CI_BUSINESS_INVALID_JSON")
	}
	return nil
}

type Fact struct {
	Algorithm string `json:"algorithm"`
	Bytes     string `json:"canonical_bytes_b64"`
	SHA256    string `json:"sha256"`
}

func (f Fact) Body() (map[string]json.RawMessage, error) {
	raw, err := base64.StdEncoding.DecodeString(f.Bytes)
	if err != nil || len(raw) > 1024*1024 || f.Algorithm != "localagent-canonical-json.v1" || Digest(raw) != f.SHA256 {
		return nil, fmt.Errorf("CI_BUSINESS_SOURCE_DIGEST")
	}
	var body map[string]json.RawMessage
	if err = json.Unmarshal(raw, &body); err != nil || body == nil {
		return nil, fmt.Errorf("CI_BUSINESS_SOURCE_OBJECT")
	}
	return body, nil
}

type Observations struct {
	Protocol string `json:"protocol"`
	Kind     string `json:"kind"`
	Records  []Fact `json:"records"`
	Repair   *Fact  `json:"repair"`
}

func ReadInput(raw []byte) (Observations, error) {
	var in Observations
	if Decode(raw, &in) != nil || in.Protocol != InputProtocol || len(in.Records) < 1 || len(in.Records) > 36 || (in.Kind != "PAIR" && in.Kind != "REPAIR") || (in.Kind == "PAIR" && (len(in.Records) != 2 || in.Repair != nil)) || (in.Kind == "REPAIR" && in.Repair == nil) {
		return in, fmt.Errorf("CI_BUSINESS_INPUT")
	}
	recordFields := []string{"execution_id", "instance_id", "owner_user_id", "tenant_id", "environment_id", "case_id", "feature_id", "state_revision", "outcome", "started_at", "completed_at", "summary", "evidence_manifest", "evidence_digest", "nightly_version_execution_id", "business_completed_at", "incident_id"}
	repairFields := []string{"incident_id", "execution_id", "instance_id", "owner_user_id", "status", "state_revision", "required_case_ids", "actions", "verifications", "closed_at", "closed_by_user_id", "closure"}
	for _, f := range in.Records {
		if validateFact(f, recordFields) != nil {
			return in, fmt.Errorf("CI_BUSINESS_RECORD")
		}
	}
	if in.Repair != nil && validateFact(*in.Repair, repairFields) != nil {
		return in, fmt.Errorf("CI_BUSINESS_REPAIR")
	}
	return in, nil
}
func validateFact(f Fact, allowed []string) error {
	body, e := f.Body()
	if e != nil {
		return e
	}
	if len(body) != len(allowed) {
		return fmt.Errorf("CI_BUSINESS_FIELDS")
	}
	var decoded any
	raw, _ := base64.StdEncoding.DecodeString(f.Bytes)
	if json.Unmarshal(raw, &decoded) != nil || privateFields(decoded) {
		return fmt.Errorf("CI_BUSINESS_PRIVATE_FIELDS")
	}
	for k := range body {
		found := false
		for _, a := range allowed {
			found = found || a == k
		}
		if !found {
			return fmt.Errorf("CI_BUSINESS_FIELDS")
		}
	}
	return nil
}

func privateFields(v any) bool {
	forbidden := map[string]bool{"groundtruth": true, "ground_truth": true, "expectedoutput": true, "expected_output": true, "assertions": true, "reference": true, "reference_label": true, "scenario_key": true, "profile_key": true, "current_state": true, "expected_root_cause": true, "jwt": true, "token": true, "api_key": true, "model_parameters": true}
	switch node := v.(type) {
	case map[string]any:
		for k, item := range node {
			if forbidden[strings.ToLower(k)] || privateFields(item) {
				return true
			}
		}
	case []any:
		for _, item := range node {
			if privateFields(item) {
				return true
			}
		}
	}
	return false
}

type Request struct {
	Protocol     string       `json:"protocol"`
	ProjectID    string       `json:"project_id"`
	RunID        string       `json:"run_id"`
	AttemptID    string       `json:"attempt_id"`
	RequestID    string       `json:"request_id"`
	CaseID       string       `json:"case_id"`
	InputDigest  string       `json:"frozen_input_digest"`
	Rule         string       `json:"rule_version"`
	Observations Observations `json:"observations"`
}
type Check struct {
	Status         string            `json:"status"`
	Violations     []string          `json:"violations"`
	EvidenceRefs   []json.RawMessage `json:"evidence_refs,omitempty"`
	Eligible       *bool             `json:"eligible,omitempty"`
	IncidentStatus string            `json:"incident_status,omitempty"`
}
type Pair struct {
	Candidate    *bool             `json:"candidate"`
	Reason       string            `json:"reason"`
	EvidenceRefs []json.RawMessage `json:"evidence_refs"`
}
type Output struct {
	Protocol           string `json:"protocol"`
	ProjectID          string `json:"project_id"`
	RunID              string `json:"run_id"`
	AttemptID          string `json:"attempt_id"`
	RequestID          string `json:"request_id"`
	CaseID             string `json:"case_id"`
	InputDigest        string `json:"frozen_input_digest"`
	Rule               string `json:"rule_version"`
	DefinitionDigest   string `json:"definition_digest"`
	ObservationsDigest string `json:"observations_digest"`
	Records            Check  `json:"record_consistency"`
	Pair               Pair   `json:"pair_candidate"`
	Repair             Check  `json:"repair_consistency"`
}

func (o Output) Validate(q Request, definition string, observationsDigest string) error {
	if o.Protocol != OutputProtocol || o.ProjectID != q.ProjectID || o.RunID != q.RunID || o.AttemptID != q.AttemptID || o.RequestID != q.RequestID || o.CaseID != q.CaseID || o.InputDigest != q.InputDigest || o.Rule != q.Rule || o.DefinitionDigest != definition || len(definition) != 64 || o.ObservationsDigest != observationsDigest || o.Records.Status != "CHECKED" || o.Pair.Reason == "" {
		return fmt.Errorf("CI_BUSINESS_RECEIPT_BINDING")
	}
	return nil
}

type Reference struct {
	Protocol         string            `json:"protocol"`
	LabelStatus      string            `json:"label_status"`
	TruthScope       string            `json:"truth_scope"`
	Reviewer         string            `json:"reviewer"`
	ReviewedAt       string            `json:"reviewed_at"`
	Sources          []string          `json:"reference_sources"`
	Applicability    map[string]string `json:"applicability"`
	RecordViolations []string          `json:"expected_record_violations"`
	Association      *bool             `json:"association_expectation"`
	Conservative     bool              `json:"conservative_required"`
	ClosureEligible  *bool             `json:"closure_expectation"`
	IncidentStatus   string            `json:"incident_status"`
}

func ReadReference(raw []byte) (Reference, error) {
	var r Reference
	if Decode(raw, &r) != nil || r.Protocol != "ci-business-reference.v1" || r.TruthScope == "" {
		return r, fmt.Errorf("CI_BUSINESS_REFERENCE")
	}
	switch r.LabelStatus {
	case "VERIFIED_SIMULATION", "REVIEWED_OBSERVATION", "UNDETERMINED", "UNREVIEWED":
	default:
		return r, fmt.Errorf("CI_BUSINESS_LABEL_STATUS")
	}
	for _, m := range Metrics {
		if r.Applicability[m] != "APPLICABLE" && r.Applicability[m] != "NOT_APPLICABLE" {
			return r, fmt.Errorf("CI_BUSINESS_APPLICABILITY")
		}
	}
	return r, nil
}

type Judgment struct {
	Score         *float64 `json:"score"`
	Verdict       string   `json:"verdict"`
	Applicability string   `json:"applicability"`
	Reason        string   `json:"reason"`
}

func Score(metric string, r Reference, o Output) Judgment {
	j := Judgment{Verdict: "INCONCLUSIVE", Applicability: "MISSING_EVIDENCE", Reason: "MISSING_EVIDENCE"}
	if r.Applicability[metric] == "NOT_APPLICABLE" {
		j.Applicability = "NOT_APPLICABLE"
		j.Reason = "NOT_APPLICABLE"
		return j
	}
	if (r.LabelStatus != "VERIFIED_SIMULATION" && r.LabelStatus != "REVIEWED_OBSERVATION") || r.Reviewer == "" || r.ReviewedAt == "" || len(r.Sources) == 0 {
		j.Reason = "UNREVIEWED_REFERENCE"
		return j
	}
	score := float64(0)
	pass := false
	switch metric {
	case Metrics[0]:
		if o.Records.Status != "CHECKED" {
			return j
		}
		a, b := append([]string{}, r.RecordViolations...), append([]string{}, o.Records.Violations...)
		sort.Strings(a)
		sort.Strings(b)
		pass = reflect.DeepEqual(a, b)
	case Metrics[1]:
		if r.Association == nil || o.Pair.Candidate == nil {
			return j
		}
		pass = *r.Association == *o.Pair.Candidate
	case Metrics[2]:
		if o.Pair.Candidate == nil || (!r.Conservative && (r.Association == nil || *r.Association)) {
			return j
		}
		pass = !*o.Pair.Candidate
	case Metrics[3]:
		if o.Repair.Status != "CHECKED" || r.ClosureEligible == nil || o.Repair.Eligible == nil {
			return j
		}
		pass = len(o.Repair.Violations) == 0 && *r.ClosureEligible == *o.Repair.Eligible && r.IncidentStatus == o.Repair.IncidentStatus
	default:
		return j
	}
	if pass {
		score = 1
	}
	if metric == Metrics[2] {
		score = 1 - score
	}
	j = Judgment{Score: &score, Verdict: "FAIL", Applicability: "APPLICABLE", Reason: "DETERMINISTIC_BUSINESS_REFERENCE_COMPARISON"}
	if pass {
		j.Verdict = "PASS"
	}
	return j
}
