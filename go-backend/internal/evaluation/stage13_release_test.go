package evaluation_test

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	gov "agentevalops/go-backend/internal/cigovernance"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"context"
	"errors"
	"testing"
)

func TestWP09FullHoldoutManifestRequired(t *testing.T) {
	scope := asset.Scope{ProjectID: asset.NewID(), OrganizationID: asset.NewID(), Principal: "release-test"}
	ref := func() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "v1"} }
	a, b, d, m, e := ref(), ref(), ref(), ref(), ref()
	app := asset.Applicability{RuleRef: "test.v1"}
	policy := gov.Policy{Version: gov.Contract, Role: "HOLDOUT", Family: "a", State: "CONFIRMED", ReviewID: asset.NewID(), GTMapping: gov.GTMapping, Split: gov.SplitVersion, Profile: gov.Profile, Frozen: true}
	body := catalog.CaseContent{Metadata: gov.Metadata(policy), TaskGoal: "frozen", AcceptanceCriteria: []string{"frozen"}, Type: catalog.Golden, Criticality: catalog.Normal, BodyPolicy: catalog.Retained, Applicability: app}
	second := body
	policy.Family = "b"
	second.Metadata = gov.Metadata(policy)
	datasetPolicy := policy
	datasetPolicy.Family = ""
	datasetPolicy.Families = []string{"a", "b"}
	def := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: "case.v1", OutputMetrics: []asset.Ref{m}, Applicability: app, Availability: asset.ContractOnly, ImplementationRef: "future.task-success.v1", SchemaVersion: "result.v1", Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 1024}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}, Normalization: "test.v1"}
	r := &reader{cases: map[asset.Ref]catalog.CaseVersion{a: version(t, scope.ProjectID, a, body), b: version(t, scope.ProjectID, b, second)}, dataset: version(t, scope.ProjectID, d, catalog.DatasetContent{Cases: []asset.Ref{a, b}, Metadata: gov.Metadata(datasetPolicy)}), metric: version(t, scope.ProjectID, m, metric.Builtins()[0].Definition), evaluator: version(t, scope.ProjectID, e, def)}
	binding := catalog.EvaluatorBinding{Evaluator: e, Metrics: []asset.Ref{m}, Required: true, Applicability: app}
	caps := ev.EvaluatorExecutionCapability{Bindings: []ev.Capability{{Evaluator: e, ImplementationRef: def.ImplementationRef, DefinitionDigest: r.evaluator.ContentDigest()}}}
	builder := ev.Builder{Assets: r}
	full, err := builder.BuildRunSnapshot(context.Background(), scope, ev.BuildRunSnapshot{Dataset: &d, Evaluators: []catalog.EvaluatorBinding{binding}})
	if err != nil {
		t.Fatal(err)
	}
	cmd := ev.CreateRun{Intent: "RELEASE_EVALUATION", CommandID: asset.NewID(), Snapshot: full, Target: ev.Target{ID: "test", Kind: "test", Version: "v1", TimeoutMilliseconds: 180000}, Subject: gov.Freeze(map[string]any{"subject": "frozen"}), Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}
	if _, err = ev.FreezeRun(cmd, scope.ProjectID, caps); err != nil {
		t.Fatal("full rejected", err)
	}
	subset, err := builder.BuildRunSnapshot(context.Background(), scope, ev.BuildRunSnapshot{Dataset: &d, Evaluators: []catalog.EvaluatorBinding{binding}, SelectedCases: []asset.Ref{a}})
	if err != nil {
		t.Fatal(err)
	}
	cmd.Snapshot = subset
	if _, err = ev.FreezeRun(cmd, scope.ProjectID, caps); !errors.Is(err, asset.ErrForbidden) {
		t.Fatal("cherry-picked holdout accepted", err)
	}
}
