package evaluation_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
)

// 只用于 domain snapshot 的 test double；PostgreSQL Gate 独立执行真实存储。
type reader struct {
	cases     map[asset.Ref]catalog.CaseVersion
	dataset   catalog.DatasetVersion
	suite     catalog.SuiteVersion
	metric    metric.DefinitionVersion
	evaluator metric.EvaluatorDefinitionVersion
}

func (r *reader) GetCaseVersion(_ context.Context, _ asset.Scope, ref asset.Ref) (catalog.CaseVersion, error) {
	v, ok := r.cases[ref]
	if !ok {
		return v, asset.ErrNotFound
	}
	return v, nil
}
func (r *reader) GetDatasetVersion(context.Context, asset.Scope, asset.Ref) (catalog.DatasetVersion, error) {
	return r.dataset, nil
}
func (r *reader) GetSuiteVersion(context.Context, asset.Scope, asset.Ref) (catalog.SuiteVersion, error) {
	return r.suite, nil
}
func (r *reader) GetMetricDefinitionVersion(context.Context, asset.Scope, asset.Ref) (metric.DefinitionVersion, error) {
	return r.metric, nil
}
func (r *reader) GetEvaluatorDefinitionVersion(context.Context, asset.Scope, asset.Ref) (metric.EvaluatorDefinitionVersion, error) {
	return r.evaluator, nil
}

type legacyReader struct{ input evaluation.LegacyInput }

func (r legacyReader) ReadLegacyRunInput(context.Context, asset.Scope, string) (evaluation.LegacyInput, error) {
	return r.input, nil
}
func version[T any](t *testing.T, project string, ref asset.Ref, body T) asset.Version[T] {
	t.Helper()
	v, err := asset.NewVersion(project, ref, body, asset.Source{Kind: "TEST", Ref: "fixture.v1", Principal: "test"}, "test", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func TestBuildRunSnapshotPinsPublishedAssets(t *testing.T) {
	ctx := context.Background()
	scope := asset.Scope{ProjectID: asset.NewID(), OrganizationID: asset.NewID(), Principal: "test"}
	ref := func() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "v1"} }
	a, b, d, s, m, e := ref(), ref(), ref(), ref(), ref(), ref()
	app := asset.Applicability{RuleRef: "evidence.v1"}
	body := catalog.CaseContent{TaskGoal: "达成目标", AcceptanceCriteria: []string{"证据完整"}, Type: catalog.AgentTask, Capability: "answer", Criticality: catalog.Critical, BodyPolicy: catalog.Retained, Applicability: app}
	metricBody := metric.Builtins()[0].Definition
	evaluatorBody := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: "case.v1", OutputMetrics: []asset.Ref{m}, Applicability: app, Availability: asset.ContractOnly, ImplementationRef: "future.task-success.v1", SchemaVersion: "result.v1", Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 1024}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}, Normalization: "null-preserving.v1"}
	binding := catalog.EvaluatorBinding{Evaluator: e, Metrics: []asset.Ref{m}, Required: false, Applicability: app}
	r := &reader{cases: map[asset.Ref]catalog.CaseVersion{a: version(t, scope.ProjectID, a, body), b: version(t, scope.ProjectID, b, body)}, dataset: version(t, scope.ProjectID, d, catalog.DatasetContent{Cases: []asset.Ref{b, a}}), metric: version(t, scope.ProjectID, m, metricBody), evaluator: version(t, scope.ProjectID, e, evaluatorBody)}
	r.suite = version(t, scope.ProjectID, s, catalog.SuiteContent{Dataset: &d, Evaluators: []catalog.EvaluatorBinding{binding}, Metrics: []asset.Ref{m}, Applicability: app})
	builder := evaluation.Builder{Assets: r}
	snapshot, err := builder.BuildRunSnapshot(ctx, scope, evaluation.BuildRunSnapshot{Suite: &s})
	if err != nil {
		t.Fatal(err)
	}
	input := snapshot.Input()
	if input.Origin != evaluation.PublishedCatalog || input.Manifest[0].Identity.Ref != b || input.Manifest[0].Case.TaskGoal != "达成目标" || input.Manifest[0].Identity.Algorithm != asset.CatalogAlgorithm || input.Evaluators[0].Required || len(input.Evaluators) != 1 {
		t.Fatal("ordered/optional/task contract not frozen")
	}
	digest := snapshot.Digest()
	input.Manifest[0].Case.AcceptanceCriteria[0] = "changed"
	raw := snapshot.Bytes()
	raw[0] = 'x'
	r.cases[b] = version(t, scope.ProjectID, b, catalog.CaseContent{TaskGoal: "新版本目标"})
	if snapshot.Digest() != digest || snapshot.Input().Manifest[0].Case.AcceptanceCriteria[0] != "证据完整" {
		t.Fatal("snapshot mutated")
	}
	selected, err := builder.BuildRunSnapshot(ctx, scope, evaluation.BuildRunSnapshot{Dataset: &d, SelectedCases: []asset.Ref{a}, Evaluators: []catalog.EvaluatorBinding{binding}})
	if err != nil || len(selected.Input().Manifest) != 1 || selected.Input().Manifest[0].Identity.Ref != a {
		t.Fatal("dataset selected snapshot", err)
	}
	if _, err = builder.BuildRunSnapshot(ctx, scope, evaluation.BuildRunSnapshot{Suite: &s, SelectedCases: []asset.Ref{ref()}}); !errors.Is(err, asset.ErrInvalid) {
		t.Fatal("accepted selection outside published source", err)
	}
	foreign := scope
	foreign.ProjectID = asset.NewID()
	if _, err = builder.BuildRunSnapshot(ctx, foreign, evaluation.BuildRunSnapshot{Suite: &s}); !errors.Is(err, asset.ErrNotFound) {
		t.Fatal("application scope accepted foreign assets", err)
	}
	legacyID := asset.NewID()
	frozen, _ := asset.ParseJSON([]byte(`{"dataset_id":"opaque-historical","version":"old","semantic_digest":"unchanged"}`))
	builder.Legacy = legacyReader{evaluation.LegacyInput{ProjectID: scope.ProjectID, SourceRunID: legacyID, DatasetSnapshot: frozen}}
	legacy, err := builder.BuildLegacyRunSnapshot(ctx, scope, legacyID)
	if err != nil {
		t.Fatal(err)
	}
	if legacy.Input().Origin != evaluation.LegacySnapshot || legacy.Input().Dataset != nil || legacy.Input().Suite != nil || legacy.Input().Legacy.DatasetSnapshot.String() != frozen.String() || legacy.Input().Legacy.DigestVerification != "DIGEST_UNVERIFIABLE" {
		t.Fatal("history fabricated catalog/digest")
	}
}
