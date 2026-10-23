package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	fi "agentevalops/go-backend/internal/featureimpact"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
	"context"
)

func FeatureImpactSupported(d metric.EvaluatorDefinition) bool {
	var c Config
	return d.Validate() == nil && d.Kind == metric.Deterministic && d.Availability != asset.Unsupported && d.ImplementationRef == fi.Implementation && d.InputContract == fi.InputProtocol && d.SchemaVersion == fi.OutputProtocol && d.Normalization == fi.Normalization && len(d.OutputMetrics) == 1 && d.Config.Decode(&c) == nil && c.Metric == fi.Metric && d.Model == nil && d.PromptRef == nil && d.Budget.MaxProviderCalls == 0
}
func evaluateFeatureImpact(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if ctx.Err() != nil {
		return worker.EvaluationOutput{}, ctx.Err()
	}
	value := BaseValue(in, "INCONCLUSIVE", "MISSING_FEATURE_IMPACT_EVIDENCE")
	c := in.Attempt.Request.Case.Case
	input, e := fi.ReadInput(c.Input.Bytes())
	var reference fi.Truth
	if e != nil || c.GroundTruth.Decode(&reference) != nil {
		return worker.EvaluationOutput{Value: value}, nil
	}
	artifact := in.Attempt.Metadata.Observation.Artifact
	var actual fi.Output
	if artifact == nil || !bound(*artifact, in) || artifact.Availability != "AVAILABLE" || in.Attempt.Outcome != "SUCCESS" || artifact.Body.Decode(&actual) != nil || actual.Validate(input, in.Scope.ProjectID, in.Attempt.RunID, in.Attempt.ID, in.Attempt.RequestID, in.Attempt.Request.Case.Identity.Ref.EntityID, c.Input.Digest()) != nil {
		return worker.EvaluationOutput{Value: value}, nil
	}
	judgment := fi.Score(input, reference, actual)
	value.Verdict = judgment.Verdict
	value.SourceVerdict = judgment.Verdict
	value.Reason = judgment.Reason
	value.SourceError = ""
	value.Provenance, _ = asset.Freeze(map[string]any{"metric": fi.Metric, "counts": judgment, "input_digest": c.Input.Digest(), "ground_truth_digest": c.GroundTruth.Digest(), "report_digest": actual.Envelope.ReportDigest, "actual_provider": "NOT_APPLICABLE", "actual_model": "NOT_APPLICABLE"})
	return worker.EvaluationOutput{Value: value}, nil
}
