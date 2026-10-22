package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/cibusiness"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
	"context"
)

func CIBusinessSupported(d metric.EvaluatorDefinition) bool {
	var c Config
	return d.Validate() == nil && d.Kind == metric.Deterministic && d.Availability != asset.Unsupported && d.ImplementationRef == cibusiness.Implementation && d.InputContract == cibusiness.InputProtocol && d.SchemaVersion == cibusiness.OutputProtocol && d.Normalization == cibusiness.Normalization && len(d.OutputMetrics) == 1 && d.Config.Decode(&c) == nil && cibusiness.HasMetric(c.Metric) && d.Model == nil && d.PromptRef == nil && d.Budget.MaxProviderCalls == 0
}
func evaluateCIBusiness(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if ctx.Err() != nil {
		return worker.EvaluationOutput{}, ctx.Err()
	}
	var config Config
	_ = in.Work.Metadata.Spec.Definition.Config.Decode(&config)
	value := BaseValue(in, "INCONCLUSIVE", "MISSING_EVIDENCE")
	value.Provenance, _ = asset.Freeze(map[string]any{"metric": config.Metric, "applicability": asset.MissingEvidence, "reason": "MISSING_EVIDENCE"})
	c := in.Attempt.Request.Case.Case
	reference, e := cibusiness.ReadReference(c.GroundTruth.Bytes())
	if e != nil {
		return worker.EvaluationOutput{Value: value}, nil
	}
	artifact := in.Attempt.Metadata.Observation.Artifact
	var actual cibusiness.Output
	if artifact == nil || !bound(*artifact, in) || artifact.Availability != "AVAILABLE" || in.Attempt.Outcome != "SUCCESS" || cibusiness.Decode(artifact.Body.Bytes(), &actual) != nil {
		return worker.EvaluationOutput{Value: value}, nil
	}
	var subject struct {
		Version string `json:"subject_version"`
	}
	_ = in.Run.Snapshot.Subject.Decode(&subject)
	var target struct {
		Definitions map[string]string `json:"rule_definitions"`
	}
	_ = in.Run.Snapshot.Target.Config.Decode(&target)
	observations, e := cibusiness.ReadInput(c.Input.Bytes())
	if e != nil {
		return worker.EvaluationOutput{Value: value}, nil
	}
	request := cibusiness.Request{ProjectID: in.Scope.ProjectID, RunID: in.Attempt.RunID, AttemptID: in.Attempt.ID, RequestID: in.Attempt.RequestID, CaseID: in.Attempt.Request.Case.Identity.Ref.EntityID, InputDigest: c.Input.Digest(), Rule: subject.Version, Observations: observations}
	if actual.Validate(request, target.Definitions[subject.Version], c.Input.Digest()) != nil {
		return worker.EvaluationOutput{Value: value}, nil
	}
	for _, b := range in.Attempt.Metadata.Observation.Evidence {
		if !bound(b, in) {
			return worker.EvaluationOutput{Value: value}, nil
		}
	}
	judgment := cibusiness.Score(config.Metric, reference, actual)
	value.Score = judgment.Score
	value.Verdict = judgment.Verdict
	value.SourceVerdict = judgment.Verdict
	value.Reason = judgment.Reason
	value.SourceError = ""
	value.Provenance, _ = asset.Freeze(map[string]any{"metric": config.Metric, "applicability": judgment.Applicability, "reference": reference, "actual": actual, "input_digest": c.Input.Digest(), "ground_truth_digest": c.GroundTruth.Digest(), "definition_digest": actual.DefinitionDigest, "actual_provider": "NOT_APPLICABLE", "actual_model": "NOT_APPLICABLE"})
	return worker.EvaluationOutput{Value: value}, nil
}
