package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
	"context"
	"sort"
)

const JointInput = "baseband-input.v2"
const JointOutput = "baseband-quality-output.v2"
const JointImplementation = "baseband-deterministic.v3"
const JointMetric = "baseband_joint_diagnosis_correctness.v3"
const JointNormalization = "baseband-joint-null-preserving.v3"

type JointFinding struct {
	BasebandFinding
	Status   string `json:"finding_status"`
	Relation string `json:"failure_relation"`
}
type JointReport struct {
	Schema        string         `json:"schema_version"`
	IncidentID    string         `json:"incident_id"`
	ExecutionID   string         `json:"execution_id"`
	InputDigest   string         `json:"input_digest"`
	Summary       string         `json:"evidence_summary"`
	Decidability  string         `json:"decidability"`
	Findings      []JointFinding `json:"candidate_findings"`
	Uncertainties []string       `json:"uncertainties"`
}
type ObservedRead struct {
	ID           string  `json:"evidence_id"`
	Digest       *string `json:"digest"`
	Invocation   string  `json:"invocation_id"`
	Action       string  `json:"action_id"`
	Iteration    int     `json:"iteration_no"`
	Availability string  `json:"availability"`
}
type JointSource struct {
	BasebandSource
	Reads []ObservedRead `json:"observed_reads"`
}
type JointReceipt struct {
	Protocol     string             `json:"protocol"`
	ProjectID    string             `json:"project_id"`
	RunID        string             `json:"run_id"`
	AttemptID    string             `json:"attempt_id"`
	RequestID    string             `json:"request_id"`
	CaseID       string             `json:"case_id"`
	InputDigest  string             `json:"frozen_input_digest"`
	Input        asset.JSON         `json:"input"`
	Source       JointSource        `json:"source"`
	Report       JointReport        `json:"report"`
	ReportDigest string             `json:"report_digest"`
	Evidence     []BasebandEvidence `json:"evidence"`
}

func JointPublicInput(input asset.JSON) asset.JSON {
	var full map[string]any
	if input.Decode(&full) != nil {
		return asset.JSON{}
	}
	projection := map[string]any{}
	for _, k := range []string{"schema_version", "software_version", "environment_condition", "constraint", "ddt", "kdt", "stimulus"} {
		projection[k] = full[k]
	}
	daas, ok := full["daas"].(map[string]any)
	if !ok {
		return asset.JSON{}
	}
	projection["daas_request"] = map[string]any{"request_id": daas["request_id"], "requested": daas["requested"], "sample_tick": daas["sample_tick"]}
	result, _ := asset.Freeze(projection)
	return result
}

func (v JointReceipt) Validate(input asset.JSON, selector BasebandSelector, project, run, attempt, request, caseID string) error {
	r, s := v.Report, v.Source
	report, e := asset.Freeze(r)
	if e != nil || v.Protocol != JointOutput || v.ProjectID != project || v.RunID != run || v.AttemptID != attempt || v.RequestID != request || v.CaseID != caseID || v.InputDigest != input.Digest() || v.Input.Digest() != input.Digest() || r.Schema != "baseband-diagnosis.v2" || r.InputDigest != input.Digest() || s.InputDigest != input.Digest() || r.IncidentID != selector.IncidentID || s.IncidentID != selector.IncidentID || r.ExecutionID == "" || s.ExecutionID != r.ExecutionID || s.Kind != selector.Kind || report.Digest() != v.ReportDigest || len(r.Findings) < 1 || len(r.Findings) > 4 || r.Summary == "" {
		return asset.ErrInvalid
	}
	if s.Kind == "AGENT" {
		if s.ModelCalls < 1 || s.CommittedTools < 1 || s.ObservedBatches < 1 || s.Agent == nil || *s.Agent != "baseband_test_investigator" || s.AgentVersion == nil || *s.AgentVersion != "1.1.0" || s.RunID == nil || selector.RunID == nil || *s.RunID != *selector.RunID || s.InvestigationID == nil || selector.InvestigationID == nil || *s.InvestigationID != *selector.InvestigationID || s.SessionID == nil || *s.SessionID == "" || s.Final == nil || *s.Final != "DELIVERED" || s.RawDigest == nil || len(*s.RawDigest) != 64 || s.RuleVersion != "" || len(s.Reads) < 1 || len(s.Reads) > 8 {
			return asset.ErrInvalid
		}
	} else if s.Kind != "RULE" || s.RuleVersion != "baseband-rule.v2" || s.RunID != nil || s.Agent != nil || selector.RunID != nil || selector.InvestigationID != nil || len(s.Reads) != 0 {
		return asset.ErrInvalid
	}
	if !oneOf(r.Decidability, "SUPPORTED", "INSUFFICIENT_EVIDENCE") || (r.Decidability == "INSUFFICIENT_EVIDENCE" && len(r.Uncertainties) == 0) {
		return asset.ErrInvalid
	}
	sources := map[string]BasebandEvidence{}
	kinds := map[string]bool{}
	for _, ev := range v.Evidence {
		if ev.ExecutionID != r.ExecutionID || ev.ID == "" || sources[ev.ID].ID != "" || kinds[ev.Kind] {
			return asset.ErrInvalid
		}
		sources[ev.ID] = ev
		kinds[ev.Kind] = true
		if ev.Availability == "AVAILABLE" {
			if ev.Digest == nil || *ev.Digest != ev.Content.Digest() || ev.Content.String() == "null" {
				return asset.ErrInvalid
			}
		} else if ev.Availability != "MISSING" || ev.Digest != nil || ev.Content.String() != "null" {
			return asset.ErrInvalid
		}
		if ev.Kind == "INPUT_MANIFEST" && ev.Content.Digest() != JointPublicInput(input).Digest() {
			return asset.ErrInvalid
		}
	}
	if len(kinds) != 6 {
		return asset.ErrInvalid
	}
	for _, k := range []string{"CI_SUMMARY", "INPUT_MANIFEST", "REFERENCE_RECORD", "DAAS_TIMELINE", "APPLIED_STATE", "EXECUTION_RECEIPT"} {
		if !kinds[k] {
			return asset.ErrInvalid
		}
	}
	ordered := append([]BasebandEvidence{}, v.Evidence...)
	sort.Slice(ordered, func(i, j int) bool { return ordered[i].ID < ordered[j].ID })
	frozen, _ := asset.Freeze(ordered)
	if frozen.Digest() != s.EvidenceDigest {
		return asset.ErrInvalid
	}
	reads := map[string]bool{}
	for _, read := range s.Reads {
		ev, ok := sources[read.ID]
		if !ok || read.Invocation == "" || read.Action != read.Invocation || read.Iteration < 1 || ev.Availability != read.Availability || (ev.Digest == nil) != (read.Digest == nil) || (ev.Digest != nil && *ev.Digest != *read.Digest) {
			return asset.ErrInvalid
		}
		reads[read.ID] = true
	}
	for _, f := range r.Findings {
		if len(f.Support) < 1 || !oneOf(f.Category, "REFERENCE", "SIMULATION_CHAIN", "PRODUCT_L1_CANDIDATE", "INPUT_CONFIGURATION", "UNDETERMINED") || !oneOf(f.Status, "CONFIRMED", "CANDIDATE", "REFUTED") || !oneOf(f.Relation, "SUPPORTED", "UNRESOLVED", "NOT_SUPPORTED") {
			return asset.ErrInvalid
		}
		for _, ref := range append(append([]BasebandReference{}, f.Support...), f.Counter...) {
			ev, ok := sources[ref.ID]
			if !ok || ev.Availability != "AVAILABLE" || ev.Digest == nil || *ev.Digest != ref.Digest || (s.Kind == "AGENT" && !reads[ref.ID]) {
				return asset.ErrInvalid
			}
		}
	}
	return nil
}

type JointClaim struct {
	Category  string   `json:"category"`
	Mechanism string   `json:"mechanism"`
	Status    string   `json:"finding_status"`
	Relation  string   `json:"failure_relation"`
	Owner     string   `json:"owner_role"`
	Actions   []string `json:"recommended_actions"`
	Support   []string `json:"support_kinds"`
	Counter   []string `json:"counter_kinds"`
}
type JointTruth struct {
	Version      string       `json:"version"`
	Visibility   string       `json:"visibility"`
	InputDigest  string       `json:"input_digest"`
	Decidability string       `json:"decidability"`
	Required     []JointClaim `json:"required_findings"`
	Allowed      []JointClaim `json:"allowed_findings"`
	Forbidden    []string     `json:"forbidden_confirmed_mechanisms"`
}

func claimKey(category, mechanism, status, relation string) string {
	return category + "/" + mechanism + "/" + status + "/" + relation
}
func JointScore(r JointReport, gt JointTruth, evidence []BasebandEvidence) map[string]bool {
	scores := map[string]bool{"decidability": r.Decidability == gt.Decidability, "required_findings": true, "allowed_claims": true, "evidence_conditions": true, "safe_action": true}
	allowed := map[string]JointClaim{}
	seen := map[string]bool{}
	kinds := map[string]string{}
	for _, ev := range evidence {
		kinds[ev.ID] = ev.Kind
	}
	for _, c := range append(append([]JointClaim{}, gt.Required...), gt.Allowed...) {
		allowed[claimKey(c.Category, c.Mechanism, c.Status, c.Relation)] = c
	}
	for _, f := range r.Findings {
		key := claimKey(f.Category, f.Mechanism, f.Status, f.Relation)
		c, ok := allowed[key]
		if !ok || seen[key] {
			scores["allowed_claims"] = false
		}
		seen[key] = true
		if !ok {
			continue
		}
		scores["allowed_claims"] = scores["allowed_claims"] && c.Owner == f.Owner
		action := false
		for _, a := range c.Actions {
			action = action || a == f.Action
		}
		scores["safe_action"] = scores["safe_action"] && action
		for _, condition := range []struct {
			Required []string
			Refs     []BasebandReference
		}{{c.Support, f.Support}, {c.Counter, f.Counter}} {
			actual := map[string]bool{}
			for _, ref := range condition.Refs {
				actual[kinds[ref.ID]] = true
			}
			for _, kind := range condition.Required {
				scores["evidence_conditions"] = scores["evidence_conditions"] && actual[kind]
			}
		}
		for _, mechanism := range gt.Forbidden {
			if f.Mechanism == mechanism && f.Status == "CONFIRMED" {
				scores["allowed_claims"] = false
			}
		}
	}
	for _, c := range gt.Required {
		scores["required_findings"] = scores["required_findings"] && seen[claimKey(c.Category, c.Mechanism, c.Status, c.Relation)]
	}
	return scores
}
func JointSupported(d metric.EvaluatorDefinition) bool {
	var c Config
	return d.Config.Decode(&c) == nil && c.Metric == JointMetric && d.ImplementationRef == JointImplementation && d.Normalization == JointNormalization && d.Validate() == nil && d.Kind == metric.Deterministic && d.Availability != asset.Unsupported && d.InputContract == JointInput && d.SchemaVersion == JointOutput && len(d.OutputMetrics) == 1 && d.Model == nil && d.PromptRef == nil && d.Budget.MaxProviderCalls == 0
}
func evaluateJoint(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if ctx.Err() != nil {
		return worker.EvaluationOutput{}, ctx.Err()
	}
	value := BaseValue(in, "INCONCLUSIVE", "MISSING_JOINT_EVIDENCE")
	c := in.Attempt.Request.Case.Case
	var gt JointTruth
	var actual JointReceipt
	var subject BasebandSubject
	artifact := in.Attempt.Metadata.Observation.Artifact
	if c.GroundTruth.Decode(&gt) != nil || gt.Version != "baseband-truth.v2" || gt.Visibility != "DEVELOPMENT_VISIBLE" || gt.InputDigest != c.Input.Digest() || len(gt.Required) == 0 || in.Run.Snapshot.Subject.Decode(&subject) != nil || artifact == nil || !bound(*artifact, in) || artifact.Schema != JointOutput || artifact.Availability != "AVAILABLE" || in.Attempt.Outcome != "SUCCESS" || artifact.Body.Decode(&actual) != nil {
		return worker.EvaluationOutput{Value: value}, nil
	}
	selector, ok := subject.Selectors[in.Attempt.Request.Case.Identity.Ref.EntityID]
	if !ok || actual.Validate(c.Input, selector, in.Scope.ProjectID, in.Attempt.RunID, in.Attempt.ID, in.Attempt.RequestID, in.Attempt.Request.Case.Identity.Ref.EntityID) != nil {
		return worker.EvaluationOutput{Value: value}, nil
	}
	scores := JointScore(actual.Report, gt, actual.Evidence)
	score := 1.0
	verdict := "PASS"
	for _, passed := range scores {
		if !passed {
			score = 0
			verdict = "FAIL"
		}
	}
	value.Verdict = verdict
	value.SourceVerdict = verdict
	value.SourceError = ""
	value.Reason = "JOINT_DIAGNOSIS_SCORED"
	value.Score = &score
	value.Provenance, _ = asset.Freeze(map[string]any{"applicability": asset.Applicable, "metric": JointMetric, "transport_trusted": true, "execution_complete": actual.Source.Kind == "AGENT", "source_kind": actual.Source.Kind, "scores": scores, "input_digest": c.Input.Digest(), "ground_truth_digest": c.GroundTruth.Digest(), "report_digest": actual.ReportDigest, "source": actual.Source})
	return worker.EvaluationOutput{Value: value}, nil
}
