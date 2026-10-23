// Package featureimpact 拥有独立覆盖参考和确定性业务评分，不访问模拟器状态。
package featureimpact

import (
	"agentevalops/go-backend/internal/asset"
	"encoding/json"
	"sort"
	"strings"
)

const Implementation = "feature-impact-deterministic.v1"
const InputProtocol = "feature-impact-input.v1"
const OutputProtocol = "feature-impact-output.v1"
const Normalization = "feature-impact-counts.v1"
const Metric = "feature-impact-quality.v1"

type Input struct {
	TaskID           string     `json:"task_id"`
	Snapshot         asset.JSON `json:"snapshot"`
	CandidateKind    string     `json:"candidate_kind"`
	CandidateVersion string     `json:"candidate_version"`
	Report           asset.JSON `json:"report"`
}
type Reference struct {
	Source  string `json:"source_ref"`
	Version string `json:"version"`
	Chunk   string `json:"chunk"`
	Digest  string `json:"content_digest"`
	Basis   string `json:"basis"`
}
type Test struct {
	ID              string       `json:"test_id"`
	Preconditions   string       `json:"preconditions"`
	Operations      []asset.JSON `json:"operations"`
	Observation     string       `json:"observation"`
	Expected        string       `json:"expected"`
	Policy          string       `json:"policy"`
	HistoricalCases []string     `json:"historical_cases"`
	Gap             string       `json:"coverage_gap"`
}
type Finding struct {
	ID            string      `json:"finding_id"`
	Feature       string      `json:"historical_feature_id"`
	Mechanism     string      `json:"impact_mechanism"`
	Conditions    []string    `json:"trigger_conditions"`
	Class         string      `json:"coverage_class"`
	Rationale     string      `json:"rationale"`
	Support       []Reference `json:"supporting_evidence"`
	Counter       []Reference `json:"counter_evidence"`
	Uncertainties []string    `json:"uncertainties"`
	Existing      string      `json:"existing_coverage"`
	Tests         []Test      `json:"recommended_tests"`
	Questions     []string    `json:"clarification_questions"`
}
type Report struct {
	Schema   string    `json:"schema_version"`
	Findings []Finding `json:"findings"`
}
type Envelope struct {
	Kind         string  `json:"candidate_kind"`
	Version      string  `json:"candidate_version"`
	RunID        *string `json:"run_id"`
	TaskID       string  `json:"task_id"`
	TaskVersion  int     `json:"task_version"`
	InputDigest  string  `json:"input_digest"`
	CorpusDigest string  `json:"corpus_digest"`
	ReportDigest string  `json:"report_digest"`
}
type Output struct {
	Protocol    string     `json:"protocol"`
	ProjectID   string     `json:"project_id"`
	RunID       string     `json:"run_id"`
	AttemptID   string     `json:"attempt_id"`
	RequestID   string     `json:"request_id"`
	CaseID      string     `json:"case_id"`
	InputDigest string     `json:"frozen_input_digest"`
	Envelope    Envelope   `json:"envelope"`
	Report      Report     `json:"report"`
	Sources     asset.JSON `json:"source_record"`
}
type Unit struct {
	Feature    string   `json:"feature"`
	Mechanism  string   `json:"mechanism"`
	Conditions []string `json:"conditions"`
	Class      string   `json:"class"`
	Known      bool     `json:"developer_known"`
	Support    []string `json:"support_sources"`
	Counter    []string `json:"counter_sources"`
	Existing   string   `json:"existing_coverage"`
	Aliases    []string `json:"mechanism_aliases"`
}
type Truth struct {
	Version      string `json:"version"`
	InputDigest  string `json:"input_digest"`
	CorpusDigest string `json:"corpus_digest"`
	Units        []Unit `json:"units"`
}
type Detail struct {
	Feature    string `json:"feature"`
	Mechanism  string `json:"mechanism"`
	Matched    bool   `json:"matched"`
	Actionable bool   `json:"actionable"`
	Reason     string `json:"reason"`
}
type Judgment struct {
	MustTotal         int      `json:"must_total"`
	MustFound         int      `json:"must_found"`
	MustActionable    int      `json:"must_actionable"`
	IncrementalTotal  int      `json:"incremental_total"`
	IncrementalFound  int      `json:"incremental_found"`
	Reasonable        int      `json:"reasonable_confirmation"`
	Uncertainty       int      `json:"uncertainty_handled"`
	Invalid           int      `json:"invalid_claims"`
	Duplicates        int      `json:"duplicates"`
	EvidenceAuthentic bool     `json:"evidence_authentic"`
	SemanticReview    string   `json:"semantic_review"`
	Details           []Detail `json:"details"`
	Verdict           string   `json:"verdict"`
	Reason            string   `json:"reason"`
}

func Decode(raw []byte, dst any) error {
	j, e := asset.ParseJSON(raw)
	if e != nil {
		return e
	}
	return j.Decode(dst)
}
func ReadInput(raw []byte) (Input, error) {
	var in Input
	if Decode(raw, &in) != nil || in.TaskID == "" || (in.CandidateKind != "RULE" && in.CandidateKind != "HUMAN") || in.CandidateVersion == "" || in.Snapshot.String() == "null" {
		return in, asset.ErrInvalid
	}
	if in.CandidateKind == "RULE" && (in.CandidateVersion != "dependency-lifecycle.v1" || in.Report.String() != "null") {
		return in, asset.ErrInvalid
	}
	return in, nil
}
func (o Output) Validate(in Input, project, run, attempt, request, caseID, digest string) error {
	var snapshot map[string]json.RawMessage
	if json.Unmarshal(in.Snapshot.Bytes(), &snapshot) != nil {
		return asset.ErrInvalid
	}
	var inputDigest, corpus string
	var version int
	_ = json.Unmarshal(snapshot["input_digest"], &inputDigest)
	_ = json.Unmarshal(snapshot["corpus_digest"], &corpus)
	_ = json.Unmarshal(snapshot["task_version"], &version)
	report, e := asset.Freeze(o.Report)
	if e != nil || o.Protocol != OutputProtocol || o.ProjectID != project || o.RunID != run || o.AttemptID != attempt || o.RequestID != request || o.CaseID != caseID || o.InputDigest != digest || o.Envelope.TaskID != in.TaskID || o.Envelope.Kind != in.CandidateKind || o.Envelope.Version != in.CandidateVersion || o.Envelope.InputDigest != inputDigest || o.Envelope.CorpusDigest != corpus || o.Envelope.TaskVersion != version || o.Envelope.RunID != nil || o.Envelope.ReportDigest != report.Digest() || o.Report.Schema != "feature-impact-report.v1" || len(o.Report.Findings) == 0 {
		return asset.ErrInvalid
	}
	return nil
}
func key(feature, mechanism string, conditions []string) string {
	c := append([]string{}, conditions...)
	sort.Strings(c)
	return feature + "|" + mechanism + "|" + strings.Join(c, ",")
}
func contains(all []string, s string) bool {
	for _, x := range all {
		if x == s {
			return true
		}
	}
	return false
}
func actionable(f Finding) (bool, string) {
	if len(f.Tests) == 0 {
		if f.Existing == "FULL" {
			return false, "EXISTING_CASE_ACTIONABILITY_PENDING"
		}
		return false, "TEST_SUGGESTION_MISSING"
	}
	pending := false
	for _, t := range f.Tests {
		valid, reason := publicSequence(t)
		if !valid && reason != "TEST_RESOURCE_DEPENDENCY_PENDING" {
			return false, reason
		}
		pending = pending || !valid
	}
	if pending {
		return false, "TEST_RESOURCE_DEPENDENCY_PENDING"
	}
	return true, "PUBLIC_TEST_SEQUENCE_VALID"
}

// publicSequence 只核对公开操作的请求依赖，不分配资源、运行模拟器或读取评分答案。
func publicSequence(t Test) (bool, string) {
	invalid := "TEST_SEQUENCE_INVALID"
	if strings.TrimSpace(t.Preconditions) == "" || strings.TrimSpace(t.Observation) == "" || strings.TrimSpace(t.Expected) == "" || len(t.Operations) == 0 || len(t.Operations) > 40 || (t.Policy != "OLD" && t.Policy != "NEW") {
		return false, invalid
	}
	type requestFacts struct {
		delivered, completed, consumed bool
		mode                           string
	}
	requests := map[string]requestFacts{}
	pending, consumed := false, false
	for _, raw := range t.Operations {
		var op struct {
			Action    string          `json:"action"`
			RequestID string          `json:"request_id"`
			Payload   json.RawMessage `json:"payload"`
			Mode      string          `json:"mode"`
		}
		var fields map[string]json.RawMessage
		if raw.Decode(&op) != nil || json.Unmarshal(raw.Bytes(), &fields) != nil || strings.TrimSpace(op.RequestID) == "" {
			return false, invalid
		}
		job, exists := requests[op.RequestID]
		switch op.Action {
		case "SUBMIT":
			if exists || len(fields) != 3 || op.Payload == nil || op.Mode != "" {
				return false, invalid
			}
			// 并行占用涉及分配/等待时，只记录无法静态证实，不推断槽位复用结果。
			for _, other := range requests {
				if !other.completed || (t.Policy == "OLD" && other.delivered && other.mode == "SHARED" && !other.consumed) {
					pending = true
				}
			}
		case "DELIVER":
			if !exists || job.completed || job.delivered || len(fields) != 3 || op.Payload != nil || (op.Mode != "SHARED" && op.Mode != "COPY" && op.Mode != "INDEPENDENT") {
				return false, invalid
			}
			job.delivered = true
			job.mode = op.Mode
		case "COMPLETE":
			if !exists || job.completed || len(fields) != 2 {
				return false, invalid
			}
			job.completed = true
		case "CONSUME":
			if !exists || !job.delivered || job.consumed || len(fields) != 2 {
				return false, invalid
			}
			job.consumed = true
			consumed = true
		default:
			return false, invalid
		}
		requests[op.RequestID] = job
	}
	if !consumed {
		return false, invalid
	}
	for _, job := range requests {
		if job.delivered && !job.consumed {
			return false, invalid
		}
	}
	if pending {
		return false, "TEST_RESOURCE_DEPENDENCY_PENDING"
	}
	return true, "PUBLIC_TEST_SEQUENCE_VALID"
}

// Score 不用执行 PASS/FAIL 反推 Coverage Truth；未明确的语义留待人审。
func Score(in Input, truth Truth, out Output) Judgment {
	j := Judgment{EvidenceAuthentic: true, SemanticReview: "PENDING", Verdict: "INCONCLUSIVE", Reason: "SEMANTIC_SUPPORT_REVIEW_PENDING", Details: []Detail{}}
	if truth.Version != "feature-impact-truth.v1" || truth.InputDigest != out.Envelope.InputDigest || truth.CorpusDigest != out.Envelope.CorpusDigest || len(truth.Units) == 0 {
		j.EvidenceAuthentic = false
		j.Reason = "REFERENCE_BINDING_INVALID"
		return j
	}
	var snapshot struct {
		Documents []struct {
			Source    string `json:"source_ref"`
			Version   string `json:"version"`
			Chunk     string `json:"chunk"`
			Digest    string `json:"content_digest"`
			Published string `json:"published_at"`
		} `json:"documents"`
		Cutoff string `json:"planning_cutoff"`
	}
	if json.Unmarshal(in.Snapshot.Bytes(), &snapshot) != nil {
		j.EvidenceAuthentic = false
		j.Reason = "INPUT_INVALID"
		return j
	}
	docs := map[string]Reference{}
	for _, d := range snapshot.Documents {
		if d.Published <= snapshot.Cutoff {
			docs[d.Source] = Reference{Source: d.Source, Version: d.Version, Chunk: d.Chunk, Digest: d.Digest}
		}
	}
	var proof struct {
		Reads       []string `json:"read_sources"`
		Rule        string   `json:"rule_version"`
		Query       string   `json:"query"`
		InputDigest string   `json:"input_digest"`
	}
	if in.CandidateKind == "RULE" && (out.Sources.Decode(&proof) != nil || proof.Rule != in.CandidateVersion || proof.InputDigest != truth.InputDigest) {
		j.EvidenceAuthentic = false
	}
	seen := map[string]bool{}
	hits := map[string]Finding{}
	pendingMust := 0
	for _, f := range out.Report.Findings {
		mechanism := f.Mechanism
		for _, u := range truth.Units {
			if u.Feature == f.Feature && contains(u.Aliases, mechanism) {
				mechanism = u.Mechanism
				break
			}
		}
		k := key(f.Feature, mechanism, f.Conditions)
		if seen[k] {
			j.Duplicates++
			continue
		}
		seen[k] = true
		authentic := len(f.Support) > 0
		for _, r := range append(append([]Reference{}, f.Support...), f.Counter...) {
			d, ok := docs[r.Source]
			if !ok || r.Version != d.Version || r.Chunk != d.Chunk || r.Digest != d.Digest || (in.CandidateKind == "RULE" && !contains(proof.Reads, r.Source)) || (in.CandidateKind == "HUMAN" && r.Basis != "TASK_INPUT") {
				authentic = false
			}
		}
		if !authentic {
			j.EvidenceAuthentic = false
		}
		matched := false
		for _, u := range truth.Units {
			mechanism := f.Mechanism
			if contains(u.Aliases, mechanism) {
				mechanism = u.Mechanism
			}
			uk := key(u.Feature, u.Mechanism, u.Conditions)
			if key(f.Feature, mechanism, f.Conditions) == uk && f.Class == u.Class {
				supported := authentic
				for _, s := range u.Support {
					found := false
					for _, r := range f.Support {
						if r.Source == s {
							found = true
						}
					}
					supported = supported && found
				}
				for _, s := range u.Counter {
					found := false
					for _, r := range f.Counter {
						if r.Source == s {
							found = true
						}
					}
					supported = supported && found
				}
				if supported {
					hits[uk] = f
					matched = true
				}
				break
			}
		}
		if !matched {
			j.Invalid++
		}
	}
	for _, u := range truth.Units {
		f, ok := hits[key(u.Feature, u.Mechanism, u.Conditions)]
		a, actionReason := false, ""
		if ok {
			a, actionReason = actionable(f)
		}
		reason := "MATCHED_PUBLIC_EVIDENCE"
		if !ok {
			reason = "MISSING_OR_UNSUPPORTED_UNIT"
		} else if u.Class == "MUST_TEST" || u.Class == "REASONABLE_CHECK" {
			reason = actionReason
		}
		switch u.Class {
		case "MUST_TEST":
			j.MustTotal++
			if !u.Known {
				j.IncrementalTotal++
			}
			if ok {
				j.MustFound++
				if !u.Known {
					j.IncrementalFound++
				}
			}
			if a {
				j.MustActionable++
			} else if ok && strings.HasSuffix(actionReason, "_PENDING") {
				pendingMust++
			}
		case "REASONABLE_CHECK":
			if a {
				j.Reasonable++
			}
		case "INSUFFICIENT_EVIDENCE":
			if ok && len(f.Uncertainties) > 0 && len(f.Questions) > 0 {
				j.Uncertainty++
			}
		}
		j.Details = append(j.Details, Detail{u.Feature, u.Mechanism, ok, a, reason})
	}
	if !j.EvidenceAuthentic {
		j.Verdict = "FAIL"
		j.Reason = "EVIDENCE_NOT_AUTHENTIC"
	} else if j.Invalid > 0 || j.Duplicates > 0 || j.MustFound < j.MustTotal || j.MustActionable < j.MustTotal {
		if j.Invalid == 0 && j.Duplicates == 0 && j.MustFound == j.MustTotal && j.MustActionable+pendingMust == j.MustTotal {
			j.Reason = "TEST_ACTIONABILITY_REVIEW_PENDING"
		} else {
			j.Verdict = "FAIL"
			j.Reason = "COVERAGE_OR_ACTIONABILITY_GAP"
		}
	}
	return j
}
