package httpapi

import (
	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
	rv "agentevalops/go-backend/internal/review"
	"encoding/json"
	"sort"
	"time"
)

func sortResults(results []resultResponse) {
	sort.Slice(results, func(i, j int) bool { return results[i].ID < results[j].ID })
}

type resultResponse struct {
	ID          string            `json:"id"`
	CaseID      string            `json:"case_id"`
	CaseVersion string            `json:"case_version"`
	Evaluator   asset.Ref         `json:"evaluator"`
	Verdict     string            `json:"evaluation_verdict"`
	Score       *float64          `json:"score"`
	TaskSuccess *taskResponse     `json:"task_success"`
	SourceError string            `json:"source_error"`
	Evidence    []bindingResponse `json:"evidence"`
}
type taskResponse struct {
	Decision     string   `json:"decision"`
	Method       string   `json:"method"`
	Confidence   *float64 `json:"confidence"`
	EvidenceRefs []string `json:"evidence_refs"`
}
type bindingResponse struct {
	Ref          string      `json:"ref"`
	Digest       string      `json:"digest"`
	Schema       string      `json:"schema"`
	Availability string      `json:"availability"`
	Body         *asset.JSON `json:"body,omitempty"`
}

func bindings(v []ev.Binding, body bool) []bindingResponse {
	out := []bindingResponse{}
	for _, b := range v {
		d := bindingResponse{Ref: b.Ref, Digest: b.Digest, Schema: b.Schema, Availability: b.Availability}
		if body && b.Availability == "AVAILABLE" {
			copy := b.Body
			d.Body = &copy
		}
		out = append(out, d)
	}
	return out
}
func resultProjection(v ev.EvaluationResult) resultResponse {
	var p struct {
		Task *agentquality.TaskJudgment `json:"task_success"`
	}
	_ = json.Unmarshal(v.Value.Provenance.Bytes(), &p)
	if p.Task != nil && p.Task.Validate() != nil {
		p.Task = nil
	}
	var task *taskResponse
	if p.Task != nil {
		task = &taskResponse{string(p.Task.Decision), p.Task.Method, p.Task.Confidence, p.Task.EvidenceRefs}
	}
	errorCode := ""
	if v.Value.SourceError != "" {
		errorCode = "SOURCE_ERROR"
	}
	return resultResponse{ID: v.ID, CaseID: v.CaseID, CaseVersion: v.CaseVersion, Evaluator: asset.Ref{EntityID: v.EvaluatorID, Version: v.EvaluatorVersion}, Verdict: v.Value.Verdict, Score: v.Value.Score, TaskSuccess: task, SourceError: errorCode, Evidence: bindings(v.Value.Evidence, false)}
}

type attemptResponse struct {
	ID      string         `json:"id"`
	CaseID  string         `json:"case_id"`
	Version string         `json:"case_version"`
	Number  int            `json:"attempt_no"`
	State   string         `json:"state"`
	Outcome ev.OutcomeKind `json:"execution_outcome"`
}
type runResponse struct {
	ID        string                                                     `json:"id"`
	ProjectID string                                                     `json:"project_id"`
	State     ev.RunStatus                                               `json:"pipeline_state"`
	Attempts  []attemptResponse                                          `json:"attempts"`
	Results   []resultResponse                                           `json:"results"`
	Coverage  struct{ Expected, Completed, Missing, Unknown, Error int } `json:"coverage"`
}

func runProjection(state ev.RunState) runResponse {
	out := runResponse{ID: state.Run.ID, ProjectID: state.Run.ProjectID, State: state.Run.Status, Attempts: []attemptResponse{}, Results: []resultResponse{}}
	out.Coverage.Expected = len(state.Run.Snapshot.Input.Manifest) * len(state.Run.Snapshot.Input.Evaluators)
	latest, _ := ev.Latest(state.Attempts)
	selected := map[string]bool{}
	for _, a := range state.Attempts {
		out.Attempts = append(out.Attempts, attemptResponse{a.ID, a.CaseID, a.CaseVersion, a.Number, a.Status, a.Outcome})
	}
	for _, a := range latest {
		selected[a.ID] = true
		if a.Outcome == ev.Unknown {
			out.Coverage.Unknown++
		}
	}
	for _, v := range state.Results {
		if !selected[v.AttemptID] {
			continue
		}
		out.Results = append(out.Results, resultProjection(v))
		out.Coverage.Completed++
		if v.Value.Verdict == "ERROR" {
			out.Coverage.Error++
		}
	}
	out.Coverage.Missing = out.Coverage.Expected - out.Coverage.Completed
	return out
}
func gateProjection(v decision.Receipt) any {
	// 不序列化 Source.Units 中的完整 provider/evidence/config。
	return map[string]any{"gate_id": v.GateID, "project_id": v.ProjectID, "comparison_version": v.Version, "decision": v.Decision, "reason_codes": v.Reasons, "comparability": v.Compatibility, "baseline": map[string]any{"ref": v.Baseline.Ref, "digest": v.Baseline.Digest}, "candidate": map[string]any{"ref": v.Candidate.Ref, "digest": v.Candidate.Digest}, "policy_ref": v.PolicyRef, "policy_digest": v.PolicyDigest, "snapshot_digest": v.SnapshotDigest, "critical_regressions": v.CriticalRegressions, "coverage": v.Coverage, "task_success": v.TaskSuccess, "metric_decisions": v.Metrics, "issues": v.Issues, "accepted_exceptions": v.Exceptions, "summary": v.Summary, "created_at": v.CreatedAt}
}

type reviewSlotResponse struct {
	Number int        `json:"number"`
	Status string     `json:"status"`
	Token  string     `json:"token,omitempty"`
	Lease  *time.Time `json:"lease,omitempty"`
	Mine   bool       `json:"mine"`
}
type reviewResponse struct {
	ID            string               `json:"id"`
	Status        string               `json:"status"`
	Reason        string               `json:"reason"`
	Schema        asset.Ref            `json:"schema"`
	SchemaDigest  string               `json:"schema_digest"`
	SourceDigest  string               `json:"source_digest"`
	Protocol      rv.Protocol          `json:"protocol"`
	Labels        []string             `json:"labels"`
	Evidence      []bindingResponse    `json:"evidence"`
	Slots         []reviewSlotResponse `json:"slots"`
	Annotations   []annotationResponse `json:"annotations,omitempty"`
	Adjudications []rv.Adjudication    `json:"adjudications,omitempty"`
	Golden        []rv.GoldenLabel     `json:"golden,omitempty"`
	Automatic     *string              `json:"automatic_decision,omitempty"`
}

func reviewProjection(v rv.ReadModel, p identity.Principal, adjudicator bool) reviewResponse {
	item := v.Item
	out := reviewResponse{ID: item.ID, Status: item.Status, Reason: item.Command.Reason, Schema: item.Command.Schema, SchemaDigest: item.SchemaDigest, SourceDigest: item.Source.Digest, Protocol: item.Command.Policy.Protocol, Labels: rv.Labels(item.Command.Kind), Slots: []reviewSlotResponse{}}
	if item.Command.Policy.Protocol.Blind {
		out.Reason = "盲审：请根据呈现的证据独立判断"
	}
	show := adjudicator && p.Has(identity.Adjudicate) && (item.Status == "ADJUDICATION_REQUIRED" || item.Status == "COMPLETED")
	out.Evidence = bindings(item.Source.Evidence, item.Source.BodyAvailability == "AVAILABLE" && (item.Source.Retention == "RETAINED" || item.Source.Retention == "REDACTED" || item.Source.Retention == "EXPLICIT_EVALUATION_RETENTION"))
	for _, slot := range v.Slots {
		mine := slot.Reviewer != nil && slot.Reviewer.ID == p.ID
		d := reviewSlotResponse{Number: slot.Number, Status: slot.Status, Mine: mine}
		if mine {
			d.Token = slot.Token
			d.Lease = slot.Lease
		}
		out.Slots = append(out.Slots, d)
	}
	if show {
		for _, a := range v.Annotations {
			out.Annotations = append(out.Annotations, annotationResponse{a.ID, a.Slot, a.Reviewer.ID, a.Decision, a.SubmittedAt})
		}
		out.Adjudications = v.Adjudications
		out.Golden = v.Golden
	}
	if item.Source.Automatic != nil && (!item.Command.Policy.Protocol.Blind || show) {
		d := item.Source.Automatic.Decision
		out.Automatic = &d
	}
	return out
}

type annotationResponse struct {
	ID          string      `json:"id"`
	Slot        int         `json:"slot"`
	ReviewerID  string      `json:"reviewer_id"`
	Decision    rv.Judgment `json:"decision"`
	SubmittedAt time.Time   `json:"submitted_at"`
}

func goldenProjection(v rv.GoldenLabel) any {
	return map[string]any{"id": v.ID, "item_id": v.ItemID, "schema": v.Schema, "source_digest": v.SourceDigest, "origin": v.Origin, "annotation_ids": v.AnnotationIDs, "adjudication_id": v.AdjudicationID, "case": v.Case, "decision": v.Decision, "protocol": v.Protocol, "created_at": v.CreatedAt}
}
func adjudicationProjection(v rv.Adjudication) any {
	return map[string]any{"id": v.ID, "item_id": v.ItemID, "annotation_ids": v.Inputs, "reviewer_id": v.Reviewer.ID, "decision": v.Decision, "reason": v.Reason, "supersedes": v.Supersedes, "created_at": v.CreatedAt}
}
func draftProjection(v rv.Draft) any {
	return map[string]any{"id": v.ID, "item_id": v.ItemID, "case": v.Case, "body": v.Body, "sanitization": v.Sanitization, "sanitized_digest": v.SanitizedDigest, "source_digest": v.SourceDigest, "golden_id": v.GoldenID, "annotation_ids": v.AnnotationIDs, "adjudication_id": v.AdjudicationID, "created_at": v.CreatedAt}
}
func calibrationProjection(v rv.CalibrationReport) any {
	identities := []map[string]string{}
	for _, pair := range v.Pairs {
		if pair.Judge != nil {
			for _, c := range pair.Judge.Calls {
				identities = append(identities, map[string]string{"provider": c.ActualProvider, "model": c.ActualModel, "revision": c.ActualRevision})
			}
		}
	}
	return map[string]any{"id": v.ID, "snapshot_digest": v.SnapshotDigest, "golden_set_digest": v.GoldenSetDigest, "evaluator": v.Evaluator, "dataset": v.Dataset, "schema": v.Schema, "protocol": v.Protocol, "sampling": v.Sampling, "agreement": v.Agreement, "provider_identities": identities, "requested_model": v.Definition.Model, "created_at": v.CreatedAt}
}
