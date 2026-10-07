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
	return d.Validate() == nil && d.Availability != asset.Unsupported && d.Kind == metric.Deterministic && d.ImplementationRef == citriage.Implementation && d.InputContract == "stage13.triage-input.v1" && d.SchemaVersion == "stage13.triage-output.v1" && d.Normalization == "stage13.cause-descriptor-exact.v1" && len(d.OutputMetrics) == 1 && d.Config.Decode(&c) == nil && citriage.Has(c.Metric, citriage.Metrics) && c.K == 3
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
	if gtErr != nil || string(c.Criticality) != gt.Criticality || citriage.ValidateInput(c.Input) != nil {
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
	if cfg.Metric == citriage.Metrics[6] && !scores.Decidable {
		v.Verdict = "INCONCLUSIVE"
		v.Score = nil
	}
	applicability := asset.Applicable
	if gt.RankingNA && citriage.Has(cfg.Metric, citriage.Metrics[3:6]) {
		v.Verdict = "INCONCLUSIVE"
		v.Score = nil
		applicability = asset.NotApplicable
	}
	v.SourceVerdict = v.Verdict
	v.Reason = "WP00 确定性指标；无效输出保留在计划分母"
	v.Provenance, _ = asset.Freeze(map[string]any{"metric": cfg.Metric, "applicability": applicability, "implementation_ref": citriage.Implementation, "evaluator_version": in.Work.EvaluatorVersion, "input_digest": c.Input.Digest(), "ground_truth_digest": c.GroundTruth.Digest(), "output_digest": o.Artifact.Digest, "triage": scores, "actual_provider": "NOT_APPLICABLE", "actual_model": "NOT_APPLICABLE"})
	return worker.EvaluationOutput{Value: v}, nil
}
