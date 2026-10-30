package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
	"context"
)

const CIDiagnosisImplementationV2 = "ci-diagnosis-deterministic.v2"

func CIDiagnosisV2Supported(d metric.EvaluatorDefinition) bool {
	var c Config
	return d.Validate() == nil && d.Availability != asset.Unsupported && d.Kind == metric.Deterministic && d.ImplementationRef == CIDiagnosisImplementationV2 && d.InputContract == "ci-diagnosis-quality-input.v2" && d.SchemaVersion == "ci-diagnosis-result.v2" && d.Normalization == "ci-diagnosis-exact-fields.v2" && d.Model == nil && d.PromptRef == nil && len(d.OutputMetrics) == 1 && d.Config.Decode(&c) == nil && c.Metric == "task_success.v1" && c.K == 0
}
func evaluateCIDiagnosisV2(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if err := ctx.Err(); err != nil {
		return worker.EvaluationOutput{}, err
	}
	missing := func() (worker.EvaluationOutput, error) {
		return NonApplicable(in, asset.MissingEvidence, "task_success.v1", "DETERMINISTIC"), nil
	}
	c := in.Attempt.Request.Case.Case
	g, err := catalog.ReadCIDiagnosisGroundTruthV2(c.GroundTruth)
	if err != nil || c.Capability != catalog.CIDiagnosisCapability || catalog.ValidateCIDiagnosisCaseV2(c) != nil || in.Attempt.Request.Target.Version != CIPairTargetVersion {
		return missing()
	}
	e, err := ReadCIDiagnosisEvidence(in)
	if err != nil || !completeCIDiagnosisEvidence(e) {
		return missing()
	}
	var persisted map[string]asset.JSON
	var planDigest string
	if in.Attempt.Metadata.Observation.Cleanup.Decode(&persisted) != nil || persisted["plan_digest"].Decode(&planDigest) != nil || planDigest != e.PlanDigest {
		return missing()
	}
	var cfg struct {
		Frozen catalog.CIPairFrozen `json:"frozen_ci"`
	}
	var fields map[string]asset.JSON
	_ = in.Attempt.Request.Target.Config.Decode(&fields)
	if fields["frozen_ci"].Decode(&cfg.Frozen) != nil {
		return missing()
	}
	resolved, err := VerifyCISourceBinding(e.SourceBinding, e, cfg.Frozen, e.Execution, e.Incident, e.Mode)
	if err != nil {
		return missing()
	}
	var source CISourceBinding
	_ = e.SourceBinding.Decode(&source)
	expected := catalog.CIDiagnosisGroundTruth{Version: catalog.CIDiagnosisGTVersion, Status: g.Status, Stage: g.Stage, Owner: g.Owner}
	for _, required := range g.Sources {
		actual, ok := resolved[required.Kind]
		if !ok || cfg.Frozen.Source != required.UpstreamRef {
			return missing()
		}
		found := false
		for _, entry := range source.Manifest {
			if entry.ID == actual.EvidenceID {
				semantic, err := CISourceSemanticDigest(entry.Kind, entry.Content, e.Execution)
				if err != nil || semantic != required.Expected {
					return missing()
				}
				found = true
			}
		}
		if !found {
			return missing()
		}
		expected.Sources = append(expected.Sources, actual)
	}
	out, err := scoreCIDiagnosis(in, e, expected)
	var provenance map[string]asset.JSON
	_ = out.Value.Provenance.Decode(&provenance)
	provenance["source_binding_digest"], _ = asset.Freeze(e.SourceBindingDigest)
	provenance["source_semantics_version"], _ = asset.Freeze(catalog.CISourceSemanticsVersion)
	out.Value.Provenance, _ = asset.Freeze(provenance)
	return out, err
}
