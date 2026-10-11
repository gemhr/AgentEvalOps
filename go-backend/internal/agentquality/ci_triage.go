package agentquality

import (
	"context"
	"encoding/json"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
)

func CITriageSupported(d metric.EvaluatorDefinition) bool {
	var c Config
	input, normalization := "stage13.triage-input.v1", "stage13.cause-descriptor-exact.v1"
	if d.ImplementationRef == citriage.ImplementationV2 {
		input, normalization = citriage.InputV2, "stage13.cause-descriptor-exact.v2"
	}
	return d.Validate() == nil && d.Availability != asset.Unsupported && d.Kind == metric.Deterministic && citriage.IsImplementation(d.ImplementationRef) && d.InputContract == input && d.SchemaVersion == "stage13.triage-output.v1" && d.Normalization == normalization && len(d.OutputMetrics) == 1 && d.Config.Decode(&c) == nil && citriage.Has(c.Metric, citriage.MetricsForVersion(d.ImplementationRef)) && c.K == 3
}
func evaluateCITriage(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if err := ctx.Err(); err != nil {
		return worker.EvaluationOutput{}, err
	}
	var cfg Config
	_ = in.Work.Metadata.Spec.Definition.Config.Decode(&cfg)
	v := BaseValue(in, "ERROR", "INVALID_GROUND_TRUTH")
	c := in.Attempt.Request.Case.Case
	gt, gtErr := citriage.ReadGroundTruth(c.GroundTruth)
	expectedImpl := citriage.Implementation
	if gt.Version == citriage.ContractV2 {
		expectedImpl = citriage.ImplementationV2
	}
	metrics := citriage.MetricsForVersion(gt.Version)
	if gtErr != nil || in.Work.Metadata.Spec.Definition.ImplementationRef != expectedImpl || string(c.Criticality) != gt.Criticality || citriage.ValidateInput(c.Input) != nil || citriage.ValidateResponsibility(c.Input, gt) != nil {
		return worker.EvaluationOutput{Value: v}, nil
	}
	o := in.Attempt.Metadata.Observation
	if o.Artifact == nil || !bound(*o.Artifact, in) || o.Artifact.Digest != o.Artifact.Body.Digest() || in.Attempt.Outcome != "SUCCESS" {
		return worker.EvaluationOutput{Value: BaseValue(in, "INCONCLUSIVE", "MISSING_EVIDENCE")}, nil
	}
	var raw string
	if json.Unmarshal(o.Artifact.Body.Bytes(), &raw) != nil {
		return worker.EvaluationOutput{Value: BaseValue(in, "ERROR", "INVALID_ARTIFACT")}, nil
	}
	scores, err := citriage.Score(raw, c.Input, gt)
	if err != nil {
		return worker.EvaluationOutput{Value: v}, nil
	}
	score := scores.Values[cfg.Metric]
	v.Score = &score
	v.Verdict = "PASS"
	v.SourceError = scores.Reason
	if score == 0 {
		v.Verdict = "FAIL"
	}
	if cfg.Metric == metrics[6] && !scores.Decidable {
		v.Verdict = "INCONCLUSIVE"
		v.Score = nil
	}
	applicability := asset.Applicable
	if gt.RankingNA && citriage.Has(cfg.Metric, metrics[3:6]) {
		v.Verdict = "INCONCLUSIVE"
		v.Score = nil
		applicability = asset.NotApplicable
	}
	v.SourceVerdict = v.Verdict
	v.Reason = "WP00 确定性指标；无效输出保留在计划分母"
	v.Provenance, _ = asset.Freeze(map[string]any{"metric": cfg.Metric, "applicability": applicability, "implementation_ref": expectedImpl, "evaluator_version": in.Work.EvaluatorVersion, "input_digest": c.Input.Digest(), "ground_truth_digest": c.GroundTruth.Digest(), "output_digest": o.Artifact.Digest, "triage": scores, "actual_provider": "NOT_APPLICABLE", "actual_model": "NOT_APPLICABLE"})
	return worker.EvaluationOutput{Value: v}, nil
}
