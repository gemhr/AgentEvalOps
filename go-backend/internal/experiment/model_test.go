package experiment

import (
	"context"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
)

type published struct {
	ev.PublishedReader
	caseVersion catalog.CaseVersion
	dataset     catalog.DatasetVersion
	evaluator   metric.EvaluatorDefinitionVersion
	metric      metric.DefinitionVersion
}

func (p published) GetCaseVersion(context.Context, asset.Scope, asset.Ref) (catalog.CaseVersion, error) {
	return p.caseVersion, nil
}
func (p published) GetDatasetVersion(context.Context, asset.Scope, asset.Ref) (catalog.DatasetVersion, error) {
	return p.dataset, nil
}
func (p published) GetEvaluatorDefinitionVersion(context.Context, asset.Scope, asset.Ref) (metric.EvaluatorDefinitionVersion, error) {
	return p.evaluator, nil
}
func (p published) GetMetricDefinitionVersion(context.Context, asset.Scope, asset.Ref) (metric.DefinitionVersion, error) {
	return p.metric, nil
}
func version[T any](t *testing.T, s ev.Scope, r asset.Ref, body T) asset.Version[T] {
	t.Helper()
	v, e := asset.NewVersion(s.ProjectID, r, body, asset.Source{Kind: "TEST", Ref: "v1", Principal: s.Principal}, s.Principal, time.Now())
	if e != nil {
		t.Fatal(e)
	}
	return v
}

func TestExperimentIdentityAndExplicitRepeat(t *testing.T) {
	s := ev.Scope{Scope: asset.Scope{ProjectID: asset.NewID(), OrganizationID: asset.NewID(), Principal: "unit"}, Create: true}
	ref := func() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "v1"} }
	c, d, e, m := ref(), ref(), ref(), ref()
	app := asset.Applicability{RuleRef: "fixture.v1"}
	p := published{caseVersion: version(t, s, c, catalog.CaseContent{TaskGoal: "goal", AcceptanceCriteria: []string{"criterion"}, Type: catalog.AgentTask, Capability: "answer", Criticality: catalog.Critical, BodyPolicy: catalog.Retained, Applicability: app}), dataset: version(t, s, d, catalog.DatasetContent{Cases: []asset.Ref{c}}), metric: version(t, s, m, metric.Builtins()[0].Definition)}
	p.evaluator = version(t, s, e, metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: "v1", OutputMetrics: []asset.Ref{m}, Applicability: app, Availability: asset.ContractOnly, ImplementationRef: "fixture-evaluator.v1", SchemaVersion: "v1", Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 4096}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 2}, Normalization: "v1"})
	snapshot, err := (ev.Builder{Assets: p}).BuildRunSnapshot(context.Background(), s.Scope, ev.BuildRunSnapshot{Dataset: &d, Evaluators: []catalog.EvaluatorBinding{{Evaluator: e, Metrics: []asset.Ref{m}, Applicability: app}}})
	if err != nil {
		t.Fatal(err)
	}
	spec := snapshot.Input().Evaluators[0]
	caps := ev.EvaluatorExecutionCapability{Bindings: []ev.Capability{{Evaluator: e, ImplementationRef: spec.Definition.ImplementationRef, DefinitionDigest: spec.Identity.ContentDigest}}}
	candidate, _ := asset.ParseJSON([]byte(`{"revision":"v1"}`))
	cmd := Create{ID: asset.NewID(), Name: "offline", Repeat: 3, Candidate: candidate, Run: ev.CreateRun{CommandID: asset.NewID(), Snapshot: snapshot, Subject: candidate, Target: ev.Target{ID: "fixture", Kind: "FIXTURE", Version: "v1", TimeoutMilliseconds: 1000}, Retry: ev.RetryPolicy{Version: "EXPLICIT_RETRY.v1", MaxAttempts: 2, Allowed: []ev.OutcomeKind{ev.Failure}}}}
	a, err := Freeze(cmd, s, caps)
	if err != nil {
		t.Fatal(err)
	}
	b, err := Freeze(cmd, s, caps)
	if err != nil || a.Digest != b.Digest || len(a.Slots) != 3 {
		t.Fatal("stable intent/repeat", err)
	}
	seen := map[string]bool{}
	for _, slot := range a.Slots {
		if seen[slot.CommandID] || !asset.ValidID(slot.CommandID) || slot.RunID != nil {
			t.Fatal("repeat shares identity")
		}
		seen[slot.CommandID] = true
	}
	cmd.Repeat = 0
	if _, err = Freeze(cmd, s, caps); err == nil {
		t.Fatal("implicit repeat accepted")
	}
	cmd.Repeat = 4
	changed, _ := Freeze(cmd, s, caps)
	if changed.Digest == a.Digest {
		t.Fatal("repeat not frozen in identity")
	}
	if a.Intent.Snapshot.Retry.MaxAttempts != 2 || a.Intent.Repeat != 3 {
		t.Fatal("repeat conflated with attempt retry")
	}
}
