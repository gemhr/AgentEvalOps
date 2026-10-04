package agentquality

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log/slog"
	"strings"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
)

const DeterministicImplementation = "agentquality-deterministic.v1"
const InputContract = "agent-quality-input.v1"
const JudgeSchema = "agent-quality-judge.v1"
const Normalization = "agent-quality-null-preserving.v1"

type Config struct {
	Metric string `json:"metric"`
	K      int    `json:"k"`
}
type Input struct {
	Goal         string         `json:"goal"`
	Criteria     []string       `json:"acceptance_criteria"`
	CaseInput    asset.JSON     `json:"case_input"`
	Reference    asset.JSON     `json:"reference"`
	Answer       asset.JSON     `json:"candidate_answer"`
	Execution    ev.OutcomeKind `json:"execution_outcome"`
	Contexts     []SelectedItem `json:"retrieved_context"`
	EvidenceRefs []string       `json:"evidence_refs"`
}

func BaseValue(in worker.EvaluationInput, verdict, category string) ev.ResultValue {
	v := ev.ResultValue{Verdict: verdict, SourceVerdict: verdict, SourceError: category, Reason: category, SpecDigest: in.Work.SpecDigest, InputDigest: in.Attempt.Request.Case.Identity.ContentDigest, Evidence: in.Attempt.Metadata.Observation.Evidence}
	if in.Attempt.Metadata.Observation.Artifact != nil {
		v.Artifact = *in.Attempt.Metadata.Observation.Artifact
	}
	return v
}
func bound(b ev.Binding, in worker.EvaluationInput) bool {
	return b.Matches(in.Scope.ProjectID, in.Attempt.RunID, in.Attempt.ID, in.Attempt.RequestID) && (b.Availability != "AVAILABLE" || b.Digest == b.Body.Digest())
}

// Prepare 只从匹配且可验证的正文导出 evidence；不 fetch opaque ref。
func Prepare(in worker.EvaluationInput, metricName string) (Input, asset.ApplicabilityState, error) {
	c := in.Attempt.Request.Case.Case
	d := in.Work.Metadata.Spec.Definition
	o := in.Attempt.Metadata.Observation
	p := Input{Goal: c.TaskGoal, Criteria: c.AcceptanceCriteria, CaseInput: c.Input, Reference: c.ExpectedOutput, Execution: in.Attempt.Outcome, Contexts: []SelectedItem{}, EvidenceRefs: []string{}}
	evidence := []asset.Evidence{{Kind: "task_goal", Schema: "task-goal.v1", BodyAvailable: c.TaskGoal != ""}, {Kind: "acceptance_criteria", Schema: "acceptance.v1", BodyAvailable: len(c.AcceptanceCriteria) > 0}, {Kind: "execution_outcome", Schema: "execution-outcome.v1"}, {Kind: "reference_or_rubric", Schema: "answer-reference.v1", BodyAvailable: c.ExpectedOutput.String() != "null" || d.Rubric.String() != "null"}}
	if o.Artifact != nil {
		if !bound(*o.Artifact, in) {
			return p, asset.MissingEvidence, asset.ErrInvalid
		}
		p.EvidenceRefs = append(p.EvidenceRefs, o.Artifact.Ref)
		if o.Artifact.Availability == "AVAILABLE" {
			p.Answer = o.Artifact.Body
			evidence = append(evidence, asset.Evidence{Kind: "actual_output", Schema: "artifact.v1", BodyAvailable: true})
		}
	}
	for _, b := range o.Evidence {
		if !bound(b, in) {
			return p, asset.MissingEvidence, asset.ErrInvalid
		}
		p.EvidenceRefs = append(p.EvidenceRefs, b.Ref)
		if b.Availability != "AVAILABLE" {
			continue
		}
		if strings.HasPrefix(b.Schema, "rag-evaluation-artifact.") {
			r, parseErr := DecodeRAG(b.Body, in.Attempt.ID)
			if parseErr != nil {
				return p, asset.MissingEvidence, asset.ErrInvalid
			}
			evidence = append(evidence, asset.Evidence{Kind: "ranked_retrieval", Schema: "ranked-retrieval.v1", BodyAvailable: true})
			for _, s := range r.Selected {
				if s.Text != "" && s.Hash == fmt.Sprintf("%x", sha256.Sum256([]byte(s.Text))) {
					p.Contexts = append(p.Contexts, s)
				}
			}
		}
	}
	if len(p.Contexts) > 0 {
		evidence = append(evidence, asset.Evidence{Kind: "retrieved_context", Schema: "context-body.v1", BodyAvailable: true})
	}
	if c.GroundTruth.String() != "null" {
		evidence = append(evidence, asset.Evidence{Kind: "relevance_ground_truth", Schema: "relevance.v1", BodyAvailable: true})
	}
	seenRefs := map[string]bool{}
	refs := []string{}
	for _, ref := range p.EvidenceRefs {
		if !seenRefs[ref] {
			refs = append(refs, ref)
			seenRefs[ref] = true
		}
	}
	p.EvidenceRefs = refs
	for _, a := range []asset.Applicability{c.Applicability, in.Work.Metadata.Spec.Applicability, d.Applicability} {
		s := asset.CheckApplicability(a, d.Availability, string(c.Type), evidence)
		if s != asset.Applicable {
			return p, s, nil
		}
	}
	for _, ref := range in.Work.Metadata.Spec.Metrics {
		found := false
		for _, m := range in.Run.Snapshot.Input.Metrics {
			if m.Identity.Ref == ref {
				found = true
				s := asset.CheckApplicability(m.Definition.Applicability, m.Definition.Availability, string(c.Type), evidence)
				if s != asset.Applicable {
					return p, s, nil
				}
			}
		}
		if !found {
			return p, asset.UnsupportedEvidence, asset.ErrInvalid
		}
	}
	if in.Attempt.Outcome != ev.Success {
		return p, asset.MissingEvidence, nil
	}
	if oneOf(metricName, "task_success.v1", "answer_correctness.v1", "answer_groundedness.v1") && p.Answer.String() == "null" {
		return p, asset.MissingEvidence, nil
	}
	if metricName == "answer_groundedness.v1" && len(p.Contexts) == 0 {
		return p, asset.MissingEvidence, nil
	}
	return p, asset.Applicable, nil
}
func NonApplicable(in worker.EvaluationInput, state asset.ApplicabilityState, metricName, method string) worker.EvaluationOutput {
	v := BaseValue(in, "INCONCLUSIVE", string(state))
	j := TaskJudgment{Version: "task_success.v1", Decision: TaskInconclusive, Method: method, Reason: string(state), ProducerRef: in.Work.Metadata.Spec.Definition.ImplementationRef}
	if state == asset.NotApplicable {
		j.Decision = TaskNotApplicable
	}
	v.Provenance, _ = asset.Freeze(map[string]any{"metric": metricName, "applicability": state, "task_success": j, "actual_provider": nil, "actual_model": nil, "comparability": "NO_DECIDABLE_RESULT"})
	if state == asset.MissingEvidence {
		slog.Info("evaluator_missing_evidence", "event", "evaluator_missing_evidence", "run_id", in.Attempt.RunID, "attempt_id", in.Attempt.ID, "work_id", in.Work.ID)
	}
	return worker.EvaluationOutput{Value: v}
}
func DeterministicSupported(d metric.EvaluatorDefinition) bool {
	var c Config
	return d.Validate() == nil && d.Availability != asset.Unsupported && d.Kind == metric.Deterministic && d.ImplementationRef == DeterministicImplementation && d.InputContract == InputContract && d.SchemaVersion == JudgeSchema && d.Normalization == Normalization && len(d.OutputMetrics) == 1 && d.Config.Decode(&c) == nil && oneOf(c.Metric, "task_success.v1", "answer_correctness.v1", "recall@k.v1", "mrr.v1", "ndcg.v1") && (oneOf(c.Metric, "task_success.v1", "answer_correctness.v1") || c.K > 0)
}

type DeterministicEvaluator struct{}

func (DeterministicEvaluator) Evaluate(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if e := ctx.Err(); e != nil {
		return worker.EvaluationOutput{}, e
	}
	d := in.Work.Metadata.Spec.Definition
	var cfg Config
	if !DeterministicSupported(d) {
		return NonApplicable(in, asset.UnsupportedEvidence, "unknown", "DETERMINISTIC"), nil
	}
	_ = d.Config.Decode(&cfg)
	p, state, e := Prepare(in, cfg.Metric)
	if e != nil {
		return worker.EvaluationOutput{Value: BaseValue(in, "ERROR", "INVALID_EVIDENCE")}, nil
	}
	if state != asset.Applicable {
		return NonApplicable(in, state, cfg.Metric, "DETERMINISTIC"), nil
	}
	v := BaseValue(in, "INCONCLUSIVE", "MISSING_EVIDENCE")
	var score *float64
	if cfg.Metric == "task_success.v1" || cfg.Metric == "answer_correctness.v1" {
		c := in.Attempt.Request.Case.Case
		hasRequired := false
		for _, a := range c.Assertions {
			hasRequired = hasRequired || a.Required
		}
		if c.ExpectedOutput.String() == "null" && !hasRequired {
			return NonApplicable(in, asset.MissingEvidence, cfg.Metric, "DETERMINISTIC"), nil
		}
		pass := true
		if c.ExpectedOutput.String() != "null" {
			pass = p.Answer.String() == c.ExpectedOutput.String()
		}
		for _, a := range c.Assertions {
			var spec struct {
				Path   []string   `json:"path"`
				Equals asset.JSON `json:"equals"`
				Type   string     `json:"type"`
			}
			if a.Config.Decode(&spec) != nil || !oneOf(a.Kind, "json_equals.v1", "json_type.v1") {
				return NonApplicable(in, asset.UnsupportedEvidence, cfg.Metric, "DETERMINISTIC"), nil
			}
			var fields map[string]asset.JSON
			_ = a.Config.Decode(&fields)
			if a.Kind == "json_equals.v1" {
				if _, exists := fields["equals"]; !exists {
					return worker.EvaluationOutput{Value: BaseValue(in, "ERROR", "INVALID_ASSERTION")}, nil
				}
			}
			value := p.Answer
			present := true
			for _, key := range spec.Path {
				var m map[string]asset.JSON
				if value.Decode(&m) != nil {
					value = asset.JSON{}
					present = false
					break
				}
				var exists bool
				value, exists = m[key]
				present = present && exists
			}
			ok := false
			if a.Kind == "json_equals.v1" {
				ok = present && value.String() == spec.Equals.String()
			} else {
				var x any
				_ = value.Decode(&x)
				switch spec.Type {
				case "object":
					_, ok = x.(map[string]any)
				case "array":
					_, ok = x.([]any)
				case "string":
					_, ok = x.(string)
				case "boolean":
					_, ok = x.(bool)
				default:
					return NonApplicable(in, asset.UnsupportedEvidence, cfg.Metric, "DETERMINISTIC"), nil
				}
			}
			if a.Required {
				pass = pass && ok
			}
		}
		n := 0.0
		if pass {
			n = 1
		}
		score = &n
	} else {
		var gt *GroundTruth
		if in.Attempt.Request.Case.Case.GroundTruth.String() != "null" {
			gt = &GroundTruth{}
			if in.Attempt.Request.Case.Case.GroundTruth.Decode(gt) != nil {
				return worker.EvaluationOutput{Value: BaseValue(in, "ERROR", "INVALID_GROUND_TRUTH")}, nil
			}
		}
		var r RAGArtifact
		count := 0
		for _, b := range in.Attempt.Metadata.Observation.Evidence {
			if strings.HasPrefix(b.Schema, "rag-evaluation-artifact.") {
				count++
				_ = b.Body.Decode(&r)
			}
		}
		if count != 1 {
			return NonApplicable(in, asset.MissingEvidence, cfg.Metric, "DETERMINISTIC"), nil
		}
		result, err := Ranking(cfg.Metric, cfg.K, gt, r)
		if err != nil {
			return worker.EvaluationOutput{Value: BaseValue(in, "ERROR", "INVALID_RANKING_EVIDENCE")}, nil
		}
		if result.State != asset.Applicable {
			return NonApplicable(in, result.State, cfg.Metric, "DETERMINISTIC"), nil
		}
		score = result.Score
	}
	v.Score = score
	v.SourceError = ""
	v.Verdict = "PASS"
	if score != nil && *score == 0 {
		v.Verdict = "FAIL"
	}
	v.SourceVerdict = v.Verdict
	v.Reason = "确定性版本化指标计算完成"
	metadata := map[string]any{"metric": cfg.Metric, "applicability": asset.Applicable, "implementation_ref": d.ImplementationRef, "actual_provider": "NOT_APPLICABLE", "actual_model": "NOT_APPLICABLE"}
	if cfg.Metric == "task_success.v1" {
		decision := TaskFailure
		if v.Verdict == "PASS" {
			decision = TaskSuccess
		}
		metadata["task_success"] = TaskJudgment{Version: cfg.Metric, Decision: decision, Method: "DETERMINISTIC", Reason: v.Reason, EvidenceRefs: p.EvidenceRefs, ProducerRef: d.ImplementationRef}
		slog.Info("task_success_decided", "event", "task_success_decided", "run_id", in.Attempt.RunID, "attempt_id", in.Attempt.ID, "work_id", in.Work.ID, "decision", decision)
	}
	v.Provenance, _ = asset.Freeze(metadata)
	return worker.EvaluationOutput{Value: v}, nil
}
