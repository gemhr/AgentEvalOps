package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
	"context"
	"sort"
)

const BasebandImplementation = "baseband-deterministic.v1"
const BasebandInput = "baseband-input.v1"
const BasebandOutput = "baseband-quality-output.v1"
const BasebandMetric = "baseband_diagnosis.v1"
const BasebandNormalization = "baseband-null-preserving.v1"
const BasebandImplementationV2 = "baseband-deterministic.v2"
const BasebandMetricV2 = "baseband_diagnosis_correctness.v2"
const BasebandNormalizationV2 = "baseband-correctness-null-preserving.v2"

type BasebandSelector struct {
	IncidentID      string  `json:"incident_id"`
	InvestigationID *string `json:"investigation_id"`
	RunID           *string `json:"run_id"`
	Kind            string  `json:"candidate_kind"`
}
type BasebandSubject struct {
	Subject     string                      `json:"subject"`
	Version     string                      `json:"subject_version"`
	Environment string                      `json:"environment"`
	Mode        string                      `json:"run_mode"`
	Selectors   map[string]BasebandSelector `json:"selectors"`
}
type BasebandReference struct {
	ID     string `json:"evidence_id"`
	Digest string `json:"digest"`
}
type BasebandFinding struct {
	Category      string              `json:"category"`
	Mechanism     string              `json:"mechanism"`
	Owner         string              `json:"owner_role"`
	Support       []BasebandReference `json:"supporting_evidence"`
	Counter       []BasebandReference `json:"counter_evidence"`
	Uncertainties []string            `json:"uncertainties"`
	Action        string              `json:"recommended_action"`
}
type BasebandReport struct {
	Schema        string            `json:"schema_version"`
	IncidentID    string            `json:"incident_id"`
	ExecutionID   string            `json:"execution_id"`
	InputDigest   string            `json:"input_digest"`
	Summary       string            `json:"evidence_summary"`
	Decidability  string            `json:"decidability"`
	Findings      []BasebandFinding `json:"candidate_findings"`
	Uncertainties []string          `json:"uncertainties"`
}
type BasebandEvidence struct {
	ID           string     `json:"evidence_id"`
	ExecutionID  string     `json:"execution_id"`
	Kind         string     `json:"kind"`
	Captured     string     `json:"captured_at"`
	Availability string     `json:"availability"`
	Media        string     `json:"media_type"`
	Digest       *string    `json:"digest"`
	Content      asset.JSON `json:"content"`
	Missing      *string    `json:"missing_reason"`
}
type BasebandSource struct {
	ModelCalls      int     `json:"model_calls"`
	CommittedTools  int     `json:"committed_tools"`
	ObservedBatches int     `json:"observed_batches"`
	Kind            string  `json:"candidate_kind"`
	IncidentID      string  `json:"incident_id"`
	ExecutionID     string  `json:"execution_id"`
	InputDigest     string  `json:"input_digest"`
	EvidenceDigest  string  `json:"evidence_digest"`
	Agent           *string `json:"agent_id"`
	AgentVersion    *string `json:"agent_version"`
	InvestigationID *string `json:"investigation_id"`
	RunID           *string `json:"run_id"`
	SessionID       *string `json:"session_id"`
	Final           *string `json:"final_status"`
	RawDigest       *string `json:"raw_output_digest"`
	RuleVersion     string  `json:"rule_version,omitempty"`
}
type BasebandReceipt struct {
	Protocol     string             `json:"protocol"`
	ProjectID    string             `json:"project_id"`
	RunID        string             `json:"run_id"`
	AttemptID    string             `json:"attempt_id"`
	RequestID    string             `json:"request_id"`
	CaseID       string             `json:"case_id"`
	InputDigest  string             `json:"frozen_input_digest"`
	Input        asset.JSON         `json:"input"`
	Source       BasebandSource     `json:"source"`
	Report       BasebandReport     `json:"report"`
	ReportDigest string             `json:"report_digest"`
	Evidence     []BasebandEvidence `json:"evidence"`
}

func (v BasebandReceipt) Validate(input asset.JSON, selector BasebandSelector, project, run, attempt, request, caseID string) error {
	r, s := v.Report, v.Source
	report, e := asset.Freeze(r)
	if e != nil || v.Protocol != BasebandOutput || v.ProjectID != project || v.RunID != run || v.AttemptID != attempt || v.RequestID != request || v.CaseID != caseID || v.InputDigest != input.Digest() || v.Input.Digest() != input.Digest() || r.Schema != "baseband-diagnosis.v1" || r.InputDigest != input.Digest() || s.InputDigest != input.Digest() || r.IncidentID != selector.IncidentID || s.IncidentID != selector.IncidentID || r.ExecutionID == "" || s.ExecutionID != r.ExecutionID || s.Kind != selector.Kind || report.Digest() != v.ReportDigest || len(r.Findings) < 1 || len(r.Findings) > 3 || r.Summary == "" {
		return asset.ErrInvalid
	}
	if s.Kind == "AGENT" {
		if s.ModelCalls < 1 || s.CommittedTools < 1 || s.ObservedBatches < 1 {
			return asset.ErrInvalid
		}
		if s.Agent == nil || *s.Agent != "baseband_test_investigator" || s.AgentVersion == nil || *s.AgentVersion != "1.0.0" || s.RunID == nil || selector.RunID == nil || *s.RunID != *selector.RunID || s.InvestigationID == nil || selector.InvestigationID == nil || *s.InvestigationID != *selector.InvestigationID || s.SessionID == nil || *s.SessionID == "" || s.Final == nil || *s.Final != "DELIVERED" || s.RawDigest == nil || len(*s.RawDigest) != 64 || s.RuleVersion != "" {
			return asset.ErrInvalid
		}
	} else if s.Kind != "RULE" || s.RuleVersion != "baseband-rule.v1" || s.RunID != nil || s.Agent != nil || selector.RunID != nil || selector.InvestigationID != nil {
		return asset.ErrInvalid
	}
	if r.Decidability != "SUPPORTED" && r.Decidability != "INSUFFICIENT_EVIDENCE" {
		return asset.ErrInvalid
	}
	if r.Decidability == "INSUFFICIENT_EVIDENCE" && len(r.Uncertainties) == 0 {
		return asset.ErrInvalid
	}
	sources := map[string]BasebandEvidence{}
	kinds := map[string]bool{}
	for _, ev := range v.Evidence {
		if ev.ExecutionID != r.ExecutionID || sources[ev.ID].ID != "" || kinds[ev.Kind] || ev.ID == "" {
			return asset.ErrInvalid
		}
		sources[ev.ID] = ev
		kinds[ev.Kind] = true
		if ev.Kind == "INPUT_MANIFEST" && ev.Content.Digest() != input.Digest() {
			return asset.ErrInvalid
		}
		if ev.Availability == "AVAILABLE" {
			if ev.Digest == nil || *ev.Digest != ev.Content.Digest() || ev.Content.String() == "null" {
				return asset.ErrInvalid
			}
		} else if ev.Availability != "MISSING" || ev.Digest != nil || ev.Content.String() != "null" {
			return asset.ErrInvalid
		}
	}
	if len(sources) != 4 || !kinds["CI_SUMMARY"] || !kinds["INPUT_MANIFEST"] || !kinds["CONSTRAINT_CHECK"] || !kinds["EXECUTION_RECEIPT"] {
		return asset.ErrInvalid
	}
	ordered := append([]BasebandEvidence{}, v.Evidence...)
	sort.Slice(ordered, func(i, j int) bool { return ordered[i].ID < ordered[j].ID })
	frozen, e := asset.Freeze(ordered)
	if e != nil || frozen.Digest() != s.EvidenceDigest {
		return asset.ErrInvalid
	}
	used := map[string]bool{}
	for _, f := range r.Findings {
		if len(f.Support) == 0 || (f.Category != "INPUT_CONFIGURATION" && f.Category != "UNDETERMINED") || (f.Owner != "TEST_OWNER" && f.Owner != "UNKNOWN") || f.Mechanism == "" {
			return asset.ErrInvalid
		}
		for _, ref := range append(append([]BasebandReference{}, f.Support...), f.Counter...) {
			ev, ok := sources[ref.ID]
			if !ok || ev.Availability != "AVAILABLE" || ev.Digest == nil || *ev.Digest != ref.Digest {
				return asset.ErrInvalid
			}
			used[ev.Kind] = true
		}
	}
	if r.Decidability == "SUPPORTED" && (!used["INPUT_MANIFEST"] || !used["CONSTRAINT_CHECK"] || !used["EXECUTION_RECEIPT"]) {
		return asset.ErrInvalid
	}
	return nil
}

type BasebandTruth struct {
	Version      string `json:"version"`
	Visibility   string `json:"visibility"`
	InputDigest  string `json:"input_digest"`
	Decidability string `json:"decidability"`
	Category     string `json:"category"`
	Mechanism    string `json:"mechanism"`
	Owner        string `json:"owner_role"`
	Action       string `json:"recommended_action"`
}

func BasebandScore(r BasebandReport, gt BasebandTruth) map[string]bool {
	scores := map[string]bool{"decidability": r.Decidability == gt.Decidability, "mechanism": true, "category": true, "owner": true, "safe_action": true}
	for _, f := range r.Findings {
		scores["mechanism"] = scores["mechanism"] && f.Mechanism == gt.Mechanism
		scores["category"] = scores["category"] && f.Category == gt.Category
		scores["owner"] = scores["owner"] && f.Owner == gt.Owner
		scores["safe_action"] = scores["safe_action"] && f.Action == gt.Action
	}
	return scores
}
func BasebandSupported(d metric.EvaluatorDefinition) bool {
	var c Config
	if d.Config.Decode(&c) != nil {
		return false
	}
	version := (d.ImplementationRef == BasebandImplementation && c.Metric == BasebandMetric && d.Normalization == BasebandNormalization) ||
		(d.ImplementationRef == BasebandImplementationV2 && c.Metric == BasebandMetricV2 && d.Normalization == BasebandNormalizationV2)
	return version && d.Validate() == nil && d.Kind == metric.Deterministic && d.Availability != asset.Unsupported && d.InputContract == BasebandInput && d.SchemaVersion == BasebandOutput && len(d.OutputMetrics) == 1 && d.Model == nil && d.PromptRef == nil && d.Budget.MaxProviderCalls == 0
}
func evaluateBaseband(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if ctx.Err() != nil {
		return worker.EvaluationOutput{}, ctx.Err()
	}
	value := BaseValue(in, "INCONCLUSIVE", "MISSING_BASEBAND_EVIDENCE")
	c := in.Attempt.Request.Case.Case
	var gt BasebandTruth
	var actual BasebandReceipt
	var subject BasebandSubject
	artifact := in.Attempt.Metadata.Observation.Artifact
	if c.GroundTruth.Decode(&gt) != nil || gt.Version != "baseband-truth.v1" || gt.Visibility != "DEVELOPMENT_VISIBLE" || gt.InputDigest != c.Input.Digest() || in.Run.Snapshot.Subject.Decode(&subject) != nil || artifact == nil || !bound(*artifact, in) || artifact.Availability != "AVAILABLE" || in.Attempt.Outcome != "SUCCESS" || artifact.Body.Decode(&actual) != nil {
		return worker.EvaluationOutput{Value: value}, nil
	}
	selector, ok := subject.Selectors[in.Attempt.Request.Case.Identity.Ref.EntityID]
	if !ok || actual.Validate(c.Input, selector, in.Scope.ProjectID, in.Attempt.RunID, in.Attempt.ID, in.Attempt.RequestID, in.Attempt.Request.Case.Identity.Ref.EntityID) != nil {
		return worker.EvaluationOutput{Value: value}, nil
	}
	scores := BasebandScore(actual.Report, gt)
	verdict := "PASS"
	for _, passed := range scores {
		if !passed {
			verdict = "FAIL"
		}
	}
	value.Verdict = verdict
	value.SourceVerdict = verdict
	value.SourceError = ""
	value.Reason = "BASEBAND_DIAGNOSIS_SCORED"
	metricName := BasebandMetric
	if in.Work.Metadata.Spec.Definition.ImplementationRef == BasebandImplementationV2 {
		metricName = BasebandMetricV2
		score := 0.0
		if verdict == "PASS" {
			score = 1.0
		}
		value.Score = &score
	}
	value.Provenance, _ = asset.Freeze(map[string]any{"applicability": asset.Applicable, "metric": metricName, "transport_trusted": true, "execution_complete": actual.Source.Kind == "AGENT", "source_kind": actual.Source.Kind, "scores": scores, "input_digest": c.Input.Digest(), "ground_truth_digest": c.GroundTruth.Digest(), "report_digest": actual.ReportDigest, "source": actual.Source})
	return worker.EvaluationOutput{Value: value}, nil
}
