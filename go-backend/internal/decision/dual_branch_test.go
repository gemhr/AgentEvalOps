package decision

import (
	"context"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
)

type publicPair struct {
	snapshot Snapshot
	receipt  Receipt
	states   []ev.RunState
	readErr  error
}

// 此替身仅代表受信 Owner 端口。真实 Quality 重验另外经 agentquality 实际注册路径验证。
type publicPairReader map[string]publicPair

func (r publicPairReader) ReadCanonicalPair(_ context.Context, _ asset.Scope, id string, _ time.Time) (Snapshot, Receipt, []ev.RunState, error) {
	p, ok := r[id]
	if !ok {
		return Snapshot{}, Receipt{}, nil, asset.ErrNotFound
	}
	return p.snapshot, p.receipt, p.states, p.readErr
}
func (publicPairReader) VerifyIndependentEvidence(asset.Scope, FrozenPlan, PlannedCase, ExecutionPin, RunReceiptRefs, string, ev.RunState) error {
	return nil
}
func publicJSON(v any) asset.JSON {
	j, e := asset.Freeze(v)
	if e != nil {
		panic(e)
	}
	return j
}
func publicAsset(body any, ref asset.Ref) ev.AssetIdentity {
	j := publicJSON(asset.Content[any]{Body: body, Source: asset.Source{Kind: "PUBLIC_SYNTHETIC", Ref: "unit-contract", Principal: "public-reviewer"}})
	return ev.AssetIdentity{Ref: ref, Algorithm: asset.CatalogAlgorithm, ContentDigest: j.Digest(), CanonicalContent: j}
}
func publicRef() asset.Ref { return asset.Ref{EntityID: asset.NewID(), Version: "public-v1"} }

func publicIndependentFixture(t *testing.T) (asset.Scope, FrozenPlan, ExecutionBinding, publicPairReader) {
	t.Helper()
	scope := asset.Scope{ProjectID: asset.NewID(), OrganizationID: asset.NewID(), Principal: "public-operator"}
	h := func(s string) string { return publicJSON(s).Digest() }
	p := FrozenPlan{Schema: IndependentPlanVersion, ID: asset.NewID(), Version: "public-v1", ProjectID: scope.ProjectID, CandidateArtifactDigest: h("artifact"), FrozenAt: time.Now().UTC().Add(-time.Hour)}
	bindings := ExecutionBinding{Schema: IndependentBindingVersion}
	reader := publicPairReader{}
	for _, purpose := range []string{"GOLDEN", "FAILURE", "HOLDOUT"} {
		input := publicJSON(map[string]string{"public": purpose})
		gt := publicJSON(catalog.CIDiagnosisGroundTruth{Version: catalog.CIDiagnosisGTVersion, Status: "SUPPORTED", Stage: "PARSER", Owner: "TOOL_MAINTAINER", Sources: []catalog.CIDiagnosisSource{{Kind: "PARSER_LOG", EvidenceID: "public-source", Digest: h("source"), ContentDigest: h("content")}}})
		m := catalog.CIDiagnosisMetadata{Version: catalog.CIDiagnosisMetadataVersion, Purpose: purpose, SourceDigest: h(purpose + " source"), FamilyDigest: h(purpose + " family"), InputDigest: input.Digest(), GroundTruthDigest: gt.Digest(), ReviewID: asset.NewID(), Reviewer: "public-reviewer", Developer: "public-developer", Exposure: "PUBLIC", DeveloperReadable: true}
		if purpose == "FAILURE" {
			m.FeedbackID = asset.NewID()
			m.ProjectionDigest = h("projection")
			m.AttributionDigest = h("attribution")
		}
		if purpose == "HOLDOUT" {
			m.Operator = "public-independent-operator"
			m.Exposure = "UNEXPOSED"
			m.DeveloperReadable = false
			m.AuditDigest = h("audit")
			m.ACLDigest = h("acl")
		}
		body := catalog.CaseContent{Input: input, GroundTruth: gt, Metadata: publicJSON(map[string]any{"ci_diagnosis": m}), Capability: catalog.CIDiagnosisCapability, Criticality: catalog.Critical}
		caseIdentity := publicAsset(body, publicRef())
		dataset := publicAsset(catalog.DatasetContent{Cases: []asset.Ref{caseIdentity.Ref}}, publicRef())
		suite := publicAsset("public suite", publicRef())
		metricRef := publicRef()
		cfg := publicJSON(map[string]string{"metric": "task_success.v1"})
		def := metric.EvaluatorDefinition{Config: cfg, ImplementationRef: "public-task.v1", SchemaVersion: "public.v1", Normalization: "public.v1"}
		spec := ev.EvaluatorSpec{Identity: publicAsset(def, publicRef()), Definition: def, Metrics: []asset.Ref{metricRef}}
		policy := Policy{Offline: true, Criticalities: []string{"CRITICAL"}, CriticalMissing: true, Coverage: CoveragePolicy{MinimumDecision: 1, MinimumEvaluation: 1}, TaskSuccess: &MetricRule{Metric: metricRef, Aggregation: "rate", AbsoluteTolerance: number(0), MinimumCoverage: 1}}
		policyRef := publicRef()
		policyDigest := h("published policy")
		target := ev.Target{ID: "public-target", Version: "v1", Kind: "FIXTURE", Config: publicJSON(map[string]bool{"public": true})}
		subject := publicJSON(map[string]string{"subject": "public-ci", "subject_version": "v1", "environment": "public", "run_mode": "offline"})
		pin := ExecutionPin{RuntimeBuild: h("build"), RuntimeConfig: h("config"), Prompt: h("prompt"), Definition: h("definition"), Manifest: h("manifest"), CaptureProfile: h("capture"), TargetVersion: target.Version, TargetDigest: digest(target), SubjectDigest: subject.Digest()}
		pc := PlannedCase{Purpose: purpose, Dataset: FrozenAssetRef{dataset.Ref, dataset.ContentDigest}, Case: FrozenAssetRef{caseIdentity.Ref, caseIdentity.ContentDigest}, Suite: FrozenAssetRef{suite.Ref, suite.ContentDigest}, Policy: FrozenAssetRef{policyRef, policyDigest}, Evaluators: []FrozenAssetRef{{spec.Identity.Ref, spec.Identity.ContentDigest}}, GroundTruthVersion: catalog.CIDiagnosisGTVersion, GroundTruthDigest: gt.Digest(), Criticality: "CRITICAL", Lineage: m, Baseline: pin, Candidate: pin}
		states := []ev.RunState{}
		refs := []RunReceiptRefs{}
		sources := []Source{}
		for _, mode := range []string{"BASELINE", "CANDIDATE"} {
			run, attempt, request, result, work := asset.NewID(), asset.NewID(), asset.NewID(), asset.NewID(), asset.NewID()
			task := "SUCCESS"
			verdict := "PASS"
			score := 1.0
			if purpose == "FAILURE" && mode == "BASELINE" {
				task = "FAILURE"
				verdict = "FAIL"
				score = 0
			}
			provenance := publicJSON(map[string]any{"metric": "task_success.v1", "applicability": asset.Applicable, "task_success": map[string]string{"version": "task_success.v1", "decision": task, "method": "DETERMINISTIC", "reason": "public fixture", "producer_ref": "public-task.v1"}})
			c := ev.CaseInput{Identity: caseIdentity, Case: body}
			state := ev.RunState{Run: ev.Run{ID: run, ProjectID: scope.ProjectID, Status: ev.RunCompleted, Snapshot: ev.RunSnapshot{Input: ev.SnapshotInput{Origin: ev.PublishedCatalog, Dataset: &dataset, Suite: &suite, Manifest: []ev.CaseInput{c}, Evaluators: []ev.EvaluatorSpec{spec}, Metrics: []ev.MetricInput{{Identity: ev.AssetIdentity{Ref: metricRef}}}}, Target: target, Subject: subject, Retry: ev.RetryPolicy{MaxAttempts: 1}}}, Attempts: []ev.Attempt{{ID: attempt, RunID: run, RequestID: request, CaseID: caseIdentity.Ref.EntityID, CaseVersion: caseIdentity.Ref.Version, Status: "TERMINAL", Outcome: ev.Success, Request: ev.Request{Case: c, Target: target}}}, Works: []ev.Work{{ID: work, Status: "COMPLETED", ResultID: &result}}, Results: []ev.EvaluationResult{{ID: result, WorkID: &work, ProjectID: scope.ProjectID, RunID: run, AttemptID: attempt, CaseID: caseIdentity.Ref.EntityID, CaseVersion: caseIdentity.Ref.Version, EvaluatorID: spec.Identity.Ref.EntityID, EvaluatorVersion: spec.Identity.Ref.Version, Value: ev.ResultValue{Verdict: verdict, Score: &score, SpecDigest: spec.Identity.ContentDigest, InputDigest: caseIdentity.ContentDigest, Provenance: provenance}}}}
			source, e := Offline(SourceRef{Kind: "RUN_SET", Runs: []string{run}}, []ev.RunState{state})
			if e != nil {
				t.Fatal(e)
			}
			states = append(states, state)
			sources = append(sources, source)
			refs = append(refs, RunReceiptRefs{RunID: run, AttemptID: attempt, RequestID: request, Results: []ResultRef{{ID: result, Digest: digest(state.Results[0])}}, QualityDigest: h("quality"), SidecarDigest: h("sidecar"), ActualLoadDigest: h("load")})
		}
		snap := Snapshot{ProjectID: scope.ProjectID, Command: Command{ID: asset.NewID(), Policy: policyRef, Baseline: sources[0].Ref, Candidate: sources[1].Ref}, Policy: policy, PolicyDigest: policyDigest, Baseline: sources[0], Candidate: sources[1], CapturedAt: time.Now().UTC()}
		receipt, e := Compare(snap)
		if e != nil || receipt.Decision != Pass {
			t.Fatalf("public canonical fixture: %s %v %+v", receipt.Decision, e, receipt.Differences)
		}
		pair := BranchReceiptRefs{Case: pc.Case.Ref, GateID: snap.Command.ID, ComparisonDigest: digest(snap), ReceiptDigest: digest(receipt), Baseline: refs[0], Candidate: refs[1]}
		reader[pair.GateID] = publicPair{snapshot: snap, receipt: receipt, states: states}
		p.Cases = append(p.Cases, pc)
		bindings.Cases = append(bindings.Cases, pair)
	}
	bindings.PlanDigest = digest(p)
	return scope, p, bindings, reader
}

func TestIndependentPlanFreezeAndCanonicalReferences(t *testing.T) {
	scope, p, b, reader := publicIndependentFixture(t)
	invalid := p
	invalid.Cases = append([]PlannedCase{}, p.Cases...)
	invalid.Cases[1].Candidate.RuntimeConfig = digest("another runtime")
	if invalid.Validate() == nil {
		t.Fatal("different Candidate runtime accepted")
	}
	path := filepath.Join(t.TempDir(), "plan.json")
	sum, e := FreezeIndependentPlan(path, p)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = FreezeIndependentPlan(path, p); e == nil {
		t.Fatal("overwrote frozen plan")
	}
	loaded, e := ReadIndependentPlan(path, sum)
	if e != nil || digest(loaded) != digest(p) {
		t.Fatal("plan digest", e)
	}
	if _, e = ReadIndependentPlan(path, digest("wrong")); e == nil {
		t.Fatal("wrong digest accepted")
	}
	out, e := AggregateIndependent(context.Background(), scope, loaded, b, reader)
	if e != nil || out.Decision != Pass || len(out.Cases) != 3 {
		t.Fatalf("public fixture %+v %v", out, e)
	}
	receiptPath := filepath.Join(t.TempDir(), "receipt.json")
	if _, e = WriteIndependentArtifact(receiptPath, out); e != nil {
		t.Fatal(e)
	}
	if _, e = WriteIndependentArtifact(receiptPath, out); e == nil {
		t.Fatal("overwrote receipt")
	}
	b.Cases[0].Candidate.Results[0].Digest = digest("caller result")
	out, e = AggregateIndependent(context.Background(), scope, p, b, reader)
	if e != nil || out.Decision != Blocked {
		t.Fatal("unbound Result accepted", out, e)
	}
	b.Cases[0].ReceiptDigest = digest("caller decision")
	out, e = AggregateIndependent(context.Background(), scope, p, b, reader)
	if e != nil || !slices.Contains(out.Cases[0].Reasons, "CANONICAL_BINDING_INVALID") {
		t.Fatal("untrusted receipt accepted")
	}
}

func TestIndependentCriticalMissingAndUnknown(t *testing.T) {
	scope, p, b, reader := publicIndependentFixture(t)
	pair := reader[b.Cases[0].GateID]
	pair.receipt.Decision = Fail
	pair.receipt.CriticalRegressions = []string{"public-critical"}
	reader[b.Cases[0].GateID] = pair
	b.Cases[0].ReceiptDigest = digest(pair.receipt)
	b.Cases = b.Cases[:2] // 必需 Holdout 缺失，Golden 原 FAIL 仍展示。
	out, e := AggregateIndependent(context.Background(), scope, p, b, reader)
	if e != nil || out.Decision != Blocked || out.Cases[0].Decision != Fail || len(out.CriticalFailures) != 1 {
		t.Fatalf("critical fact hidden %+v/%v", out, e)
	}
	pair = reader[b.Cases[1].GateID]
	pair.states[1].Attempts[0].Outcome = ev.Unknown
	reader[b.Cases[1].GateID] = pair
	out, e = AggregateIndependent(context.Background(), scope, p, b, reader)
	if e != nil || out.Decision != Blocked || !slices.Contains(out.Cases[1].Reasons, "CANDIDATE_EVIDENCE_UNVERIFIED") {
		t.Fatal("unknown became success", e)
	}
	pair = reader[b.Cases[0].GateID]
	pair.readErr = asset.ErrNotFound
	reader[b.Cases[0].GateID] = pair
	out, e = AggregateIndependent(context.Background(), scope, p, b, reader)
	if e != nil || len(out.CriticalFailures) != 1 || out.Cases[0].OriginalDecision != Fail {
		t.Fatal("lost canonical critical fact after Run lookup failure", e)
	}

}

func TestIndependentHoldoutMetadataRefusals(t *testing.T) {
	for _, mutation := range []string{"family", "source", "golden-content", "exposed", "retired", "readable", "operator", "historic"} {
		t.Run(mutation, func(t *testing.T) {
			scope, p, b, reader := publicIndependentFixture(t)
			h := &p.Cases[2]
			switch mutation {
			case "family":
				h.Lineage.FamilyDigest = p.Cases[1].Lineage.FamilyDigest
			case "source":
				h.Lineage.SourceDigest = p.Cases[1].Lineage.SourceDigest
			case "golden-content":
				h.Lineage.InputDigest = p.Cases[0].Lineage.InputDigest
			case "exposed":
				h.Lineage.Exposure = "PUBLIC"
			case "retired":
				h.Lineage.Retired = true
			case "readable":
				h.Lineage.DeveloperReadable = true
			case "operator":
				h.Lineage.Operator = ""
			case "historic":
				h.Dataset.Ref.EntityID = "0f94fa11-8194-5afd-8c66-30be45cfec79"
			}
			b.PlanDigest = digest(p)
			out, e := AggregateIndependent(context.Background(), scope, p, b, reader)
			if e != nil || out.Decision != Blocked || !slices.Contains(out.Cases[2].Reasons, "HOLDOUT_INDEPENDENCE_UNVERIFIED") {
				t.Fatalf("metadata accepted %+v %v", out, e)
			}
		})
	}
}

func (publicPairReader) VerifyIndependentPair(asset.Scope, FrozenPlan, PlannedCase, BranchReceiptRefs, Snapshot, []ev.RunState) error {
	return nil
}

func TestIndependentBlockedPrecedence(t *testing.T) {
	scope, p, b, reader := publicIndependentFixture(t)
	pair := reader[b.Cases[0].GateID]
	value := &pair.states[1].Results[0].Value
	value.Verdict = "FAIL"
	value.Score = number(0)
	value.Provenance = publicJSON(map[string]any{"metric": "task_success.v1", "applicability": asset.Applicable, "task_success": map[string]string{"version": "task_success.v1", "decision": "FAILURE", "method": "DETERMINISTIC", "reason": "public failure", "producer_ref": "public-task.v1"}})
	pair.snapshot.Candidate, _ = Offline(pair.snapshot.Candidate.Ref, []ev.RunState{pair.states[1]})
	pair.snapshot.Policy.Metrics = []MetricRule{{Metric: publicRef(), Aggregation: "mean", Required: true, MinimumCoverage: 1}}
	pair.receipt, _ = Compare(pair.snapshot)
	if pair.receipt.Decision != Blocked || len(pair.receipt.CriticalRegressions) == 0 {
		t.Fatal("expected original Compare BLOCKED + critical", pair.receipt)
	}
	b.Cases[0].ComparisonDigest = digest(pair.snapshot)
	b.Cases[0].ReceiptDigest = digest(pair.receipt)
	b.Cases[0].Candidate.Results[0].Digest = digest(pair.states[1].Results[0])
	reader[b.Cases[0].GateID] = pair
	out, e := AggregateIndependent(context.Background(), scope, p, b, reader)
	if e != nil || out.Decision != Blocked || out.Cases[0].Decision != Blocked || out.Cases[0].OriginalDecision != Blocked || len(out.CriticalFailures) == 0 || len(out.Cases[0].GateReasons) == 0 {
		t.Fatal("BLOCKED downgraded", out, e)
	}
}
