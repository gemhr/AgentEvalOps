//go:build integration

package postgres_test

import (
	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/provider"
	rv "agentevalops/go-backend/internal/review"
	"agentevalops/go-backend/internal/worker"
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TEST_SCOPE公开资产，原Review/Publication/Kernel/受限PG角色。无模型调用。
func TestCIPairReviewPublicationPG(t *testing.T) { runCIPairPG(t, false) }
func TestCIPairWorkerSourcePG(t *testing.T)      { runCIPairPG(t, true) }
func runCIPairPG(t *testing.T, unified bool) {
	if os.Getenv("CI_PAIR_PG_APPROVED") != "1" {
		t.Skip("explicit isolated resources required")
	}
	f := g7(t)
	ctx := context.Background()
	must := func(e error) {
		t.Helper()
		if e != nil {
			t.Fatal(e)
		}
	}
	// 收窄原G7 scaffold到单Case、单指标，避免运行旧矩阵。
	readers := g6Readers(f.g6Fixture)
	ds := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	ref := asset.Ref{EntityID: asset.NewID(), Version: "TEST_SCOPE.v1"}
	_, e := ds.CreateDataset(ctx, f.s.Scope, asset.Create{ID: ref.EntityID, Name: "TEST_SCOPE public single case"})
	must(e)
	_, e = ds.PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: ref, Body: catalog.DatasetContent{Cases: []asset.Ref{f.cmd.Snapshot.Input().Manifest[0].Identity.Ref}}, Source: asset.Source{Kind: "TEST", Ref: "SYNTHETIC_PG_ONLY", Principal: f.s.Principal}})
	must(e)
	f.dataset = ref
	f.build(t, f.bindings[:1])
	f.bindings = f.bindings[:1]
	baseline := f.run(t, "SYNTHETIC_SOURCE", g6Plan{Success: 1})
	state, e := f.k.ReadRunState(ctx, f.s.Scope, baseline.Runs[0])
	must(e)
	item := f.enqueue(t, g7Source(t, state, f.bindings[0].Evaluator, 0), f.refs[0], rv.TaskSuccess, 2, true)
	golden := f.double(t, item, "SUCCESS", "SUCCESS")
	raw, e := os.ReadFile("../agentquality/testdata/ci_source_pair.json")
	must(e)
	var vector struct {
		Schema    string     `json:"schema_version"`
		Input     asset.JSON `json:"visible_input"`
		GT        asset.JSON `json:"ground_truth"`
		Metadata  asset.JSON `json:"metadata"`
		Semantics asset.JSON `json:"semantic_digests"`
		Branches  asset.JSON `json:"branches"`
		Samples   asset.JSON `json:"samples"`
	}
	j, e := asset.ParseJSON(raw)
	must(e)
	must(j.Decode(&vector))
	var gt catalog.CIDiagnosisGroundTruthV2
	must(vector.GT.Decode(&gt))
	gt.ReviewID = item.ID
	gt.Reviewer = f.reviewer("publisher").Principal
	for i := range gt.Sources {
		gt.Sources[i].UpstreamRef = "review-item:" + item.ID
		gt.Sources[i].UpstreamDigest = item.Source.Digest
	}
	ground, _ := asset.Freeze(gt)
	metadata, e := catalog.ReadCIDiagnosisMetadata(vector.Metadata)
	must(e)
	metadata.ReviewID = item.ID
	metadata.Reviewer = gt.Reviewer
	metadata.Developer = f.s.Principal
	metadata.SourceDigest = item.Source.Digest
	metadata.GroundTruthDigest = ground.Digest()
	meta, _ := asset.Freeze(map[string]any{"ci_diagnosis": metadata})
	body := catalog.CaseContent{Input: vector.Input, GroundTruth: ground, Metadata: meta, Capability: catalog.CIDiagnosisCapability, Type: catalog.Golden, Criticality: catalog.Normal, BodyPolicy: catalog.Redacted, TaskGoal: "TEST_SCOPE public source contract", AcceptanceCriteria: []string{"SYNTHETIC storage only"}, Applicability: asset.Applicability{RuleRef: "TEST_SCOPE.v1"}}
	caseRef := asset.Ref{EntityID: asset.NewID(), Version: "TEST_SCOPE.v2"}
	publisher := f.reviewer("publisher")
	draft, e := f.reviews.CreateReviewedCaseDraft(ctx, publisher, rv.Draft{ID: asset.NewID(), ItemID: item.ID, GoldenID: &golden.ID, Case: caseRef, Body: body, Sanitization: "REDACTED", Policy: "TEST_SCOPE_PUBLIC.v1", Reason: "synthetic integration only", HumanSupplement: true})
	must(e)
	published, e := f.reviews.PublishReviewedCase(ctx, publisher, draft.ID, "TEST_SCOPE CI GT v2")
	must(e)
	again, e := readers.Cases.GetCaseVersion(ctx, f.s.Scope, caseRef)
	must(e)
	if again.ContentDigest() != published.ContentDigest() {
		t.Fatal("publication changed")
	}
	if _, e = f.reviews.GetReview(ctx, publisher, item.ID); e != nil {
		t.Fatal(e)
	}
	for _, bad := range []catalog.CaseContent{func() catalog.CaseContent { b := body; b.Input = ground; return b }(), func() catalog.CaseContent {
		b := body
		g := gt
		g.ReviewID = asset.NewID()
		b.GroundTruth, _ = asset.Freeze(g)
		return b
	}()} {
		if bad.Validate() == nil {
			t.Fatal("GT/input/review mismatch accepted")
		}
	}
	wrong := f.s.Scope
	wrong.ProjectID = asset.NewID()
	if _, e = readers.Cases.GetCaseVersion(ctx, wrong, caseRef); e == nil {
		t.Fatal("project leak")
	}
	missing := caseRef
	missing.Version = "unpublished"
	if _, e = readers.Cases.GetCaseVersion(ctx, f.s.Scope, missing); e == nil {
		t.Fatal("unpublished visible")
	}
	badSource := rv.SourceRef{Type: "CALIBRATION_SAMPLE", RunID: asset.NewID(), ResultID: asset.NewID()}
	if _, e = f.reviews.EnqueueReview(ctx, publisher, rv.Enqueue{ID: asset.NewID(), Source: badSource, Schema: f.refs[0], Kind: rv.TaskSuccess, Policy: item.Command.Policy, Reason: "TEST_SCOPE missing source", Priority: "NORMAL", Criticality: "CRITICAL"}); e == nil {
		t.Fatal("nonexistent review source accepted")
	}
	// 原Builder冻结已审核Case；不创建该CI Case的模型Attempt。
	frozenDataset := asset.Ref{EntityID: asset.NewID(), Version: "TEST_SCOPE.reviewed"}
	_, e = ds.CreateDataset(ctx, f.s.Scope, asset.Create{ID: frozenDataset.EntityID, Name: "TEST_SCOPE reviewed"})
	must(e)
	_, e = ds.PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: frozenDataset, Body: catalog.DatasetContent{Cases: []asset.Ref{caseRef}}, Source: asset.Source{Kind: "TEST", Ref: "TEST_SCOPE_PUBLIC", Principal: f.s.Principal}})
	must(e)
	snap, e := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &frozenDataset, Evaluators: f.bindings})
	must(e)
	if snap.Input().Manifest[0].Case.GroundTruth.Digest() != ground.Digest() {
		t.Fatal("frozen GT drift")
	}
	if unified {
		runUnifiedPair(t, f, frozenDataset, caseRef, body)
		return
	}
	var role string
	must(f.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&role))
	var super, wide, column bool
	must(f.k.Pool.QueryRow(ctx, "SELECT rolsuper,has_table_privilege(current_user,'projects','UPDATE'),has_column_privilege(current_user,'projects','id','UPDATE') FROM pg_roles WHERE rolname=current_user").Scan(&super, &wide, &column))
	if super || wide || !column {
		t.Fatal("minimum projects privilege violated")
	}
	if _, e = f.k.Pool.Exec(ctx, "UPDATE projects SET name=name WHERE id=$1", f.s.ProjectID); e == nil {
		t.Fatal("wide project update allowed")
	}
	// 原Result/Comparison/Gate持久接口；导入值明确SYNTHETIC，非真实业务PASS。
	candidate := f.run(t, "SYNTHETIC_CANDIDATE", g6Plan{Success: 1})
	policy := f.policy(t, func(p *decision.Policy) {
		p.Coverage.MinimumDecision = 1
		p.Metrics = []decision.MetricRule{{Metric: f.refs[0], Aggregation: "mean", MinimumCoverage: 1, Required: true}}
	})
	receipt, e := f.gate.CreateGate(ctx, f.s, f.gateCommand(baseline, candidate, policy))
	must(e)
	saved, e := f.gate.GetGateReceipt(ctx, f.s.Scope, receipt.GateID)
	must(e)
	if saved.GateID != receipt.GateID {
		t.Fatal("gate re-read")
	}
	_, e = f.gate.GetComparison(ctx, f.s.Scope, receipt.GateID)
	must(e)
	_, e = f.gate.GetGateReceipt(ctx, wrong, receipt.GateID)
	if e == nil {
		t.Fatal("gate project leak")
	}
	var version string
	must(f.db.pool.QueryRow(ctx, "SELECT version_num FROM alembic_version").Scan(&version))
	t.Logf("TEST_SCOPE publication=%s@%s review=%s PG schema=%s restricted_role=%s super=false projects.UPDATE(table)=false UPDATE(id)=true", caseRef.EntityID, caseRef.Version, item.ID, version, role)
	t.Logf("SYNTHETIC original Result/Gate persisted: baseline=%s candidate=%s gate=%s decision=%s; v2 source Worker/independent-summary NOT_VERIFIED", baseline.Runs[0], candidate.Runs[0], receipt.GateID, receipt.Decision)
}

// 无HTTP认证伪装的受信域内target。实际执行与封存只在AC原Owner产生。
type ciExchangeTarget struct {
	t       *testing.T
	pins    map[string]asset.JSON
	project string
	plan    string
	wires   map[string]asset.JSON
	reject  bool
}

func ciExchange(t *testing.T, payload any) asset.JSON {
	t.Helper()
	p, _ := asset.Freeze(payload)
	envelope, _ := asset.Freeze(map[string]any{"payload": p, "digest": p.Digest()})
	ac := os.Getenv("CI_PAIR_AC_ROOT")
	python := filepath.Join(ac, ".venv", "Scripts", "python.exe")
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, python, "-m", "tests._ci_pair_exchange")
	cmd.Dir = ac
	cmd.Stdin = bytes.NewReader(envelope.Bytes())
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	e := cmd.Start()
	if e != nil {
		t.Fatal(e)
	}
	pid := cmd.Process.Pid
	t.Logf("BRIDGE_START pid=%d", pid)
	e = cmd.Wait()
	raw := stdout.Bytes()
	t.Logf("BRIDGE_EXIT pid=%d code=%d", pid, cmd.ProcessState.ExitCode())
	if e != nil {
		t.Fatalf("AC exchange: %v\n%s", e, stderr.String())
	}
	if len(raw) > 2*1024*1024 {
		t.Fatal("output limit")
	}
	j, e := asset.ParseJSON(raw)
	if e != nil {
		t.Fatalf("exchange JSON: %v", e)
	}
	t.Logf("AC subprocess exited=0 bytes=%d phase=%s", len(raw), func() string { var v map[string]asset.JSON; _ = p.Decode(&v); return v["phase"].String() }())
	return j
}
func (x *ciExchangeTarget) Execute(ctx context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	var c provider.CIPairConfig
	if e := q.Target.Config.Decode(&c); e != nil {
		return ev.Outcome{}, e
	}
	var profile aq.QualityProfile
	_ = c.Quality.Profile.Decode(&profile)
	corr := aq.QualityCorrelation{Agent: "ci_incident_investigator", Run: q.AttemptID, Project: s.ProjectID, EvaluationRun: q.RunID, Attempt: q.AttemptID, Request: q.ID, Case: q.Case.Identity.Ref.EntityID, CaseVersion: q.Case.Identity.Ref.Version, CaseDigest: q.Case.Identity.ContentDigest, ManifestDigest: c.Quality.Manifest.Digest(), ProfileID: profile.ID, ProfileVersion: profile.Version, ProfileDigest: c.Quality.Profile.Digest(), Timeout: q.Target.TimeoutMilliseconds}
	visible, _ := asset.Freeze(corr)
	var quality map[string]asset.JSON
	_ = visible.Decode(&quality)
	delete(quality, "query_digest")
	quality["query"], _ = asset.Freeze(aq.CIPairQuery)
	request := map[string]any{"contract_version": aq.CIPairProtocol, "quality": quality, "frozen_ci": c.Frozen, "plan_digest": x.plan, "candidate_manifest_digest": c.Manifest, "mode": c.Mode}
	var wire asset.JSON
	if x.reject {
		for _, saved := range x.wires {
			wire = saved
			break
		}
	} else {
		wire = ciExchange(x.t, map[string]any{"phase": "execute", "mode": c.Mode, "visible_request": request, "pins": x.pins[c.Mode]})
		if x.wires == nil {
			x.wires = map[string]asset.JSON{}
		}
		x.wires[q.AttemptID] = wire
	}
	// 原严格消费器验证实际Owner字节；无手写成功Wire。
	var outer struct {
		Contract string     `json:"contract_version"`
		Quality  asset.JSON `json:"quality"`
		Side     asset.JSON `json:"sidecar"`
		Digest   string     `json:"sidecar_digest"`
	}
	_ = wire.Decode(&outer)
	var qr map[string]asset.JSON
	_ = outer.Quality.Decode(&qr)
	var bundle aq.QualityBundle
	_ = qr["bundle"].Decode(&bundle)
	corr.QueryDigest = bundle.Correlation.QueryDigest
	verified, err := provider.VerifyCIPair(wire.Bytes(), c, corr, q, s, x.plan)
	if err != nil {
		return ev.Outcome{Kind: ev.Unknown, RequestID: q.ID, RemoteID: q.AttemptID, Protocol: aq.CIPairProtocol, Source: "TEST_SCOPE_STDIO", DispatchCertainty: "MAY_HAVE_DISPATCHED", TerminalCertainty: "UNCONFIRMED", ErrorCategory: "SOURCE_VALIDATION_FAILED", Reason: "original sealed source refused"}, nil
	}
	return verified, nil
}
func runUnifiedPair(t *testing.T, f *g7Fixture, dataset, caseRef asset.Ref, body catalog.CaseContent) {
	ctx := context.Background()
	must := func(e error) {
		t.Helper()
		if e != nil {
			t.Fatal(e)
		}
	}
	pins := map[string]asset.JSON{}
	for _, mode := range []string{"BASELINE", "CANDIDATE"} {
		pins[mode] = ciExchange(t, map[string]any{"phase": "pins", "mode": mode})
	}
	readers := g6Readers(f.g6Fixture)
	er := asset.Ref{EntityID: asset.NewID(), Version: "TEST_SCOPE.ci-v2"}
	def := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: "ci-diagnosis-quality-input.v2", OutputMetrics: []asset.Ref{f.refs[0]}, Applicability: body.Applicability, Availability: asset.ContractOnly, ImplementationRef: aq.CIDiagnosisImplementationV2, SchemaVersion: "ci-diagnosis-result.v2", Normalization: "ci-diagnosis-exact-fields.v2", Config: func() asset.JSON { j, _ := asset.Freeze(aq.Config{Metric: "task_success.v1"}); return j }(), Budget: metric.Budget{TotalMilliseconds: 5000, MaxResponseBytes: 65536}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}}
	es := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	_, e := es.CreateEvaluatorDefinition(ctx, f.s.Scope, asset.Create{ID: er.EntityID, Name: "TEST_SCOPE CI v2"})
	must(e)
	_, e = es.PublishEvaluatorDefinitionVersion(ctx, f.s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: er, Body: def, Source: asset.Source{Kind: "TEST", Ref: "SYNTHETIC_ONLY", Principal: f.s.Principal}})
	must(e)
	bindings := []catalog.EvaluatorBinding{{Evaluator: er, Metrics: []asset.Ref{f.refs[0]}, Required: true, Applicability: body.Applicability}}
	policyRef := f.policy(t, nil)
	policyVersion, e := f.gate.GetPolicyVersion(ctx, f.s.Scope, policyRef)
	must(e)
	suites := catalog.SuiteService{Store: readers.Suites, References: readers}
	suiteRef := asset.Ref{EntityID: asset.NewID(), Version: "TEST_SCOPE.unified"}
	_, e = suites.CreateSuite(ctx, f.s.Scope, asset.Create{ID: suiteRef.EntityID, Name: "TEST_SCOPE unified CI"})
	must(e)
	_, e = suites.PublishSuiteVersion(ctx, f.s.Scope, asset.Publish[catalog.SuiteContent]{Ref: suiteRef, Body: catalog.SuiteContent{Dataset: &dataset, Evaluators: bindings, Metrics: []asset.Ref{f.refs[0]}, Policy: &asset.PolicyRef{PolicyID: policyRef.EntityID, Version: policyRef.Version, Digest: policyVersion.ContentDigest(), Algorithm: asset.CatalogAlgorithm}, Applicability: body.Applicability}, Source: asset.Source{Kind: "TEST", Ref: "SYNTHETIC_SUITE", Principal: f.s.Principal}})
	must(e)
	snap, e := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Suite: &suiteRef})
	must(e)
	spec := snap.Input().Evaluators[0]
	f.k.Capabilities = ev.EvaluatorExecutionCapability{Bindings: []ev.Capability{{Evaluator: er, ImplementationRef: def.ImplementationRef, DefinitionDigest: spec.Identity.ContentDigest}}}
	planID := asset.NewID()
	target := &ciExchangeTarget{t: t, pins: pins, project: f.s.ProjectID}
	var runs []string
	var states []ev.RunState
	commands := map[string]ev.CreateRun{}
	for _, mode := range []string{"BASELINE", "CANDIDATE"} {
		var pin map[string]asset.JSON
		_ = pins[mode].Decode(&pin)
		var manifest aq.QualityManifest
		_ = pin["manifest"].Decode(&manifest)
		var candidate, prompt, cm string
		_ = pin["candidate_id"].Decode(&candidate)
		_ = pin["prompt_digest"].Decode(&prompt)
		_ = pin["candidate_manifest_digest"].Decode(&cm)
		var input catalog.CIPairInput
		_ = body.Input.Decode(&input)
		var gt catalog.CIDiagnosisGroundTruthV2
		_ = body.GroundTruth.Decode(&gt)
		frozen := catalog.CIPairFrozen{CaseID: caseRef.EntityID, CaseVersion: caseRef.Version, CaseDigest: snap.Input().Manifest[0].Identity.ContentDigest, InputDigest: body.Input.Digest(), Dataset: dataset.EntityID, DatasetVersion: dataset.Version, PlanID: planID, PlanVersion: "test-v1", Source: "review-item:" + gt.ReviewID, Input: input}
		cfg := provider.CIPairConfig{Protocol: aq.CIPairProtocol, Quality: provider.AgentQualityConfig{BaseURL: "http://127.0.0.1", TokenEnv: "TEST_SCOPE_NO_HTTP", TrustedProjectID: f.s.ProjectID, MaxResponseBytes: 2 * 1024 * 1024, CallMilliseconds: 30000, HTTP: provider.DefaultHTTPConfig(), Manifest: pin["manifest"], Profile: pin["profile"]}, Frozen: frozen, Manifest: cm, Mode: mode, Prompt: prompt, Candidate: candidate}
		config, _ := asset.Freeze(cfg)
		command := f.cmd
		command.CommandID = asset.NewID()
		command.Snapshot = snap
		command.Target = ev.Target{ID: "ci-synthetic-http", Kind: "LOCALAGENT_HTTP", Version: aq.CIPairTargetVersion, Config: config, TimeoutMilliseconds: 30000}
		command.Retry = ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}
		command.Subject, _ = asset.Freeze(map[string]string{"agent": "ci_incident_investigator", "revision": mode, "environment": "test", "run_mode": "SYNTHETIC"})
		commands[mode] = command
	}
	frozenPlan := buildUnifiedPlan(t, f, snap, body, commands, pins, planID, policyRef)
	planPath := filepath.Join(os.Getenv("CI_PAIR_EXCHANGE_DIR"), "frozen-plan.json")
	must(withFrozenUnifiedPlan(planPath, frozenPlan, decision.ReadIndependentPlan, func(planPath, planDigest string) error {
		target.plan = planDigest
		for _, mode := range []string{"BASELINE", "CANDIDATE"} {
			command := commands[mode]
			created, e := f.k.CreateRun(ctx, f.s, command)
			reply(t, created, e, ev.Applied)
			runs = append(runs, created.ID)
			commands[mode] = command
			wc := worker.DefaultConfig()
			wc.ExecutionConcurrency = 1
			wc.EvaluatorConcurrency = 1
			wc.PollInterval = 10 * time.Millisecond
			wc.Reconcile = false
			runtime := &worker.Runtime{Config: wc, Backend: f.k, Target: target, Evaluator: aq.DeterministicEvaluator{}, Epoch: f.s.Epoch}
			running, cancel := context.WithCancel(ctx)
			done := make(chan error, 1)
			go func() { done <- runtime.Run(running) }()
			deadline := time.Now().Add(50 * time.Second)
			var state ev.RunState
			for time.Now().Before(deadline) {
				state, e = f.k.ReadRunState(ctx, f.s.Scope, created.ID)
				must(e)
				if len(state.Results) > 0 {
					break
				}
				time.Sleep(50 * time.Millisecond)
			}
			cancel()
			must(<-done)
			if len(state.Results) != 1 || state.Results[0].Value.Verdict != "PASS" {
				t.Fatalf("v2 result: %+v attempts=%+v", state.Results, state.Attempts)
			}
			r, e := f.k.CoordinateRun(ctx, f.s, created.ID)
			reply(t, r, e, ev.Applied)
			state, e = f.k.ReadRunState(ctx, f.s.Scope, created.ID)
			must(e)
			states = append(states, state)
			t.Logf("UNIFIED mode=%s run=%s attempt=%s request=%s result=%s verdict=%s implementation=%s", mode, created.ID, state.Attempts[0].ID, state.Attempts[0].RequestID, state.Results[0].ID, state.Results[0].Value.Verdict, state.Works[0].Metadata.Spec.Definition.ImplementationRef)
		}
		if runs[0] == runs[1] {
			t.Fatal("side identity reused")
		}

		finishUnifiedEvidence(t, f, states, commands, target, snap, body, planPath, planDigest, policyRef)
		return nil
	}))
}

// TEST_SCOPE：只有正式工件写入、重读且完整身份一致，才允许进入创建阶段。
// readPlan 参数仅供纯文件测试注入重读异常；PG 路径使用原 Reader。
func withFrozenUnifiedPlan(path string, plan decision.FrozenPlan, readPlan func(string, string) (decision.FrozenPlan, error), execute func(string, string) error) error {
	expected, err := asset.Freeze(plan)
	if err != nil {
		return err
	}
	digest, err := decision.FreezeIndependentPlan(path, plan)
	if err != nil {
		return err
	}
	persisted, err := readPlan(path, digest)
	if err != nil {
		return err
	}
	actual, err := asset.Freeze(persisted)
	if err != nil {
		return err
	}
	if digest != expected.Digest() || actual.String() != expected.String() {
		return asset.ErrInvalid
	}
	return execute(path, digest)
}

func TestCIPairPreExecutionPlanFreeze(t *testing.T) {
	hash := func(value string) string {
		j, err := asset.Freeze(value)
		if err != nil {
			t.Fatal(err)
		}
		return j.Digest()
	}
	ref := func(name string) decision.FrozenAssetRef {
		return decision.FrozenAssetRef{Ref: asset.Ref{EntityID: asset.NewID(), Version: "public-v1"}, Digest: hash(name)}
	}
	baseline := decision.ExecutionPin{RuntimeBuild: hash("build"), RuntimeConfig: hash("baseline config"), Prompt: hash("baseline prompt"), Definition: hash("baseline definition"), Manifest: hash("baseline manifest"), CaptureProfile: hash("profile"), TargetVersion: aq.CIPairTargetVersion, TargetDigest: hash("baseline target"), SubjectDigest: hash("baseline subject")}
	candidate := baseline
	candidate.RuntimeConfig, candidate.Prompt, candidate.Definition, candidate.Manifest = hash("candidate config"), hash("candidate prompt"), hash("candidate definition"), hash("candidate manifest")
	candidate.TargetDigest, candidate.SubjectDigest = hash("candidate target"), hash("candidate subject")
	plan := decision.FrozenPlan{Schema: decision.IndependentPlanVersion, ID: asset.NewID(), Version: "public-file-only-v1", ProjectID: asset.NewID(), CandidateArtifactDigest: hash("public candidate"), FrozenAt: time.Now().UTC(), Cases: []decision.PlannedCase{{Purpose: "GOLDEN", Dataset: ref("dataset"), Case: ref("case"), Suite: ref("suite"), Policy: ref("policy"), Evaluators: []decision.FrozenAssetRef{ref("evaluator")}, GroundTruthVersion: catalog.CIDiagnosisGTVersionV2, GroundTruthDigest: hash("public GT"), Criticality: "NORMAL", Baseline: baseline, Candidate: candidate}}}
	expected, err := asset.Freeze(plan)
	if err != nil || plan.Validate() != nil {
		t.Fatal("invalid public file-only plan", err)
	}
	for _, name := range []string{"valid", "conflicting_path", "write_failure", "read_digest_mismatch", "read_identity_mismatch"} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "frozen-plan.json")
			var original []byte
			if name == "conflicting_path" {
				other := plan
				other.Version = "conflicting-public-version"
				if _, err := decision.FreezeIndependentPlan(path, other); err != nil {
					t.Fatal(err)
				}
				original, err = os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
			}
			if name == "write_failure" {
				path = filepath.Join(filepath.Dir(path), "missing-parent", "frozen-plan.json")
			}
			reads, creates := 0, 0
			read := func(path, digest string) (decision.FrozenPlan, error) {
				reads++
				if name == "read_digest_mismatch" {
					return decision.ReadIndependentPlan(path, hash("wrong expected digest"))
				}
				got, err := decision.ReadIndependentPlan(path, digest)
				if err == nil && name == "read_identity_mismatch" {
					got.ID = asset.NewID()
				}
				return got, err
			}
			err := withFrozenUnifiedPlan(path, plan, read, func(gotPath, digest string) error {
				creates++ // 仅标记 CreateRun 所在后续阶段；不调用 Kernel 或 Runtime。
				got, err := decision.ReadIndependentPlan(gotPath, digest)
				if err != nil {
					return err
				}
				actual, err := asset.Freeze(got)
				if err != nil || gotPath != path || digest != expected.Digest() || actual.String() != expected.String() || reads != 1 {
					t.Fatal("execution did not receive the exact reread artifact", err)
				}
				return nil
			})
			if name == "valid" {
				if err != nil || creates != 1 {
					t.Fatal("valid freeze blocked", err, creates)
				}
			} else if err == nil || creates != 0 {
				t.Fatal("failed freeze entered creation stage", err, creates)
			}
			if name == "conflicting_path" {
				after, readErr := os.ReadFile(path)
				if !os.IsExist(err) || readErr != nil || !bytes.Equal(original, after) || reads != 0 {
					t.Fatal("conflicting artifact replaced or reread", err, readErr)
				}
			} else if name == "write_failure" {
				if !os.IsNotExist(err) || reads != 0 {
					t.Fatal("write failure was not closed before reread", err)
				}
			} else if _, readErr := decision.ReadIndependentPlan(path, expected.Digest()); readErr != nil {
				t.Fatal("frozen artifact changed after execution decision", readErr)
			}
			t.Logf("original_writer_reader=true reread_calls=%d creation_stage_calls=%d error=%v", reads, creates, err)
		})
	}
}

func TestCIPairStoredRejection(t *testing.T) {
	path := os.Getenv("CI_PAIR_STORED_WIRE")
	if path == "" {
		t.Skip("requires current exact sealed artifact")
	}
	raw, e := os.ReadFile(path)
	const originalSHA = "f5fd86283a9657a6580736ccb91fe994683bbe6c3baa44201b0b2776a0729951"
	if fmt.Sprintf("%x", sha256.Sum256(raw)) != originalSHA {
		t.Fatal("147 original bytes changed")
	}
	defer func() {
		after, err := os.ReadFile(path)
		if err != nil || !bytes.Equal(after, raw) {
			t.Error("147 modified during offline replay")
		}
	}()
	if e != nil {
		t.Fatal(e)
	}
	wire, e := asset.ParseJSON(raw)
	if e != nil {
		t.Fatal(e)
	}
	var w struct {
		Contract string     `json:"contract_version"`
		Quality  asset.JSON `json:"quality"`
		Side     asset.JSON `json:"sidecar"`
		Digest   string     `json:"sidecar_digest"`
	}
	_ = wire.Decode(&w)
	var qf map[string]asset.JSON
	_ = w.Quality.Decode(&qf)
	var b aq.QualityBundle
	_ = qf["bundle"].Decode(&b)
	_, _, err := aq.DecodeQuality(qf["bundle"], b.Correlation)
	t.Log("DECODE_QUALITY", err)
	var side map[string]asset.JSON
	_ = w.Side.Decode(&side)
	var binding map[string]asset.JSON
	_ = side["binding"].Decode(&binding)
	var f catalog.CIPairFrozen
	_ = binding["frozen_ci"].Decode(&f)
	var mode, cm string
	_ = binding["mode"].Decode(&mode)
	_ = binding["manifest_digest"].Decode(&cm)
	cfg := provider.CIPairConfig{Protocol: aq.CIPairProtocol, Frozen: f, Mode: mode, Manifest: cm, Quality: provider.AgentQualityConfig{TrustedProjectID: b.Correlation.Project, Manifest: b.Manifest, Profile: b.Profile}}
	var models []struct {
		Model string `json:"model_ref"`
		Frame string `json:"frame_ref"`
		Run   string `json:"run_id"`
		State string `json:"model_state"`
		Proof struct {
			Schema    string `json:"schema_version"`
			Candidate string `json:"candidate_id"`
			Manifest  string `json:"manifest_digest"`
			Mode      string `json:"mode"`
			Prompt    string `json:"prompt_digest"`
			Request   string `json:"model_request_digest"`
			Messages  string `json:"messages_digest"`
			System    string `json:"system_digest"`
			Scope     string `json:"evidence_scope"`
		} `json:"proof"`
	}
	_ = side["models"].Decode(&models)
	cfg.Candidate = models[0].Proof.Candidate
	cfg.Prompt = models[0].Proof.Prompt
	config, _ := asset.Freeze(cfg)
	input, _ := asset.Freeze(f.Input)
	q := ev.Request{ID: b.Correlation.Request, RunID: b.Correlation.EvaluationRun, AttemptID: b.Correlation.Attempt, Target: ev.Target{ID: "ci-synthetic-http", Kind: "LOCALAGENT_HTTP", Version: aq.CIPairTargetVersion, Config: config, TimeoutMilliseconds: b.Correlation.Timeout}, Case: ev.CaseInput{Identity: ev.AssetIdentity{Ref: asset.Ref{EntityID: f.CaseID, Version: f.CaseVersion}, ProjectID: b.Correlation.Project, ContentDigest: f.CaseDigest}, Case: catalog.CaseContent{Input: input}}}
	var plan string
	_ = binding["plan_digest"].Decode(&plan)
	out, e := provider.VerifyCIPair(raw, cfg, b.Correlation, q, ev.Scope{Scope: asset.Scope{ProjectID: b.Correlation.Project}}, plan)
	t.Logf("VERIFY kind=%s terminal=%s error=%v", out.Kind, out.TerminalCertainty, e)

	var answer map[string]asset.JSON
	_ = qf["final_answer_evidence"].Decode(&answer)
	artifactBody := answer["content"]
	bound := func(schema, ref string, j asset.JSON) ev.Binding {
		return ev.Binding{ProjectID: b.Correlation.Project, RunID: q.RunID, AttemptID: q.AttemptID, RequestID: q.ID, Schema: schema, Ref: ref, Availability: "AVAILABLE", Body: j, Digest: j.Digest()}
	}
	artifact := bound("artifact.v1", "final-answer://"+q.AttemptID, artifactBody)
	attempt := ev.Attempt{ID: q.AttemptID, RunID: q.RunID, RequestID: q.ID, Outcome: ev.Success, Request: q, Metadata: ev.AttemptMetadata{Observation: ev.Outcome{Artifact: &artifact, Evidence: []ev.Binding{bound(aq.QualitySchema, b.ID, qf["bundle"]), bound(aq.CIPairSidecarVersion, "ci-synthetic://"+q.AttemptID, w.Side)}}}}
	evidence, re := aq.ReadCIDiagnosisEvidence(worker.EvaluationInput{Scope: ev.Scope{Scope: asset.Scope{ProjectID: b.Correlation.Project}}, Attempt: attempt})
	t.Log("READ_CI", re)
	if err != nil || re != nil || e != nil || out.Kind != ev.Success {
		t.Fatalf("offline consumers failed: DecodeQuality=%v ReadCI=%v VerifyCIPair=%v", err, re, e)
	}
	_, se := aq.VerifyCISourceBinding(evidence.SourceBinding, evidence, f, evidence.Execution, evidence.Incident, mode)
	if se != nil {
		t.Fatal("VerifyCISourceBinding", se)
	}
	t.Log("ORIGINAL_147_OFFLINE_SOURCE_VERIFICATION=PASS; no PG, Worker, GT scoring or model execution")
	var source aq.CISourceBinding
	if err := evidence.SourceBinding.Decode(&source); err != nil {
		t.Fatal(err)
	}
	var payload map[string]asset.JSON
	j, err := asset.ParseJSON([]byte(source.Tools[0].ResultBytes))
	if err != nil {
		t.Fatal(err)
	}
	if err = j.Decode(&payload); err != nil {
		t.Fatal(err)
	}
	var originalID string
	_ = payload["attempt_id"].Decode(&originalID)
	source.Tools[0].ResultBytes = strings.Replace(source.Tools[0].ResultBytes, originalID, strings.Repeat("0", 32), 1)
	tampered, _ := asset.Freeze(source)
	// 仅派生副本重算外层摘要，原Tool.Result/Quality/Action事实保持；147绝不改写。
	evidence.SourceBinding = tampered
	evidence.SourceBindingDigest = tampered.Digest()
	if _, err = aq.VerifyCISourceBinding(tampered, evidence, f, evidence.Execution, evidence.Incident, mode); err == nil {
		t.Fatal("tampered Tool Attempt accepted")
	}
	t.Log("TAMPERED_TOOL_ATTEMPT_WITH_RESEALED_OUTER_DIGEST=REJECTED")
}

func frozenAsset(a ev.AssetIdentity) decision.FrozenAssetRef {
	return decision.FrozenAssetRef{Ref: a.Ref, Digest: a.ContentDigest}
}
func buildUnifiedPlan(t *testing.T, f *g7Fixture, snap ev.Snapshot, body catalog.CaseContent, commands map[string]ev.CreateRun, pins map[string]asset.JSON, id string, policy asset.Ref) decision.FrozenPlan {
	t.Helper()
	input := snap.Input()
	pv, e := f.gate.GetPolicyVersion(context.Background(), f.s.Scope, policy)
	if e != nil {
		t.Fatal(e)
	}
	lineage, e := catalog.ReadCIDiagnosisMetadata(body.Metadata)
	if e != nil {
		t.Fatal(e)
	}
	c := decision.PlannedCase{Purpose: "GOLDEN", Dataset: frozenAsset(*input.Dataset), Case: frozenAsset(input.Manifest[0].Identity), Suite: frozenAsset(*input.Suite), Policy: decision.FrozenAssetRef{Ref: policy, Digest: pv.ContentDigest()}, Evaluators: []decision.FrozenAssetRef{frozenAsset(input.Evaluators[0].Identity)}, GroundTruthVersion: catalog.CIDiagnosisGTVersionV2, GroundTruthDigest: body.GroundTruth.Digest(), Criticality: string(body.Criticality), Lineage: lineage}
	artifact := ""
	for _, mode := range []string{"BASELINE", "CANDIDATE"} {
		var raw map[string]asset.JSON
		_ = pins[mode].Decode(&raw)
		var manifest aq.QualityManifest
		_ = raw["manifest"].Decode(&manifest)
		var prompt string
		_ = raw["prompt_digest"].Decode(&prompt)
		_ = raw["candidate_manifest_digest"].Decode(&artifact)
		a := manifest.Agents["ci_incident_investigator"]
		target, _ := asset.Freeze(commands[mode].Target)
		pin := decision.ExecutionPin{RuntimeBuild: manifest.Build, RuntimeConfig: a.ConfigDigest, Definition: a.DefinitionDigest, Manifest: raw["manifest"].Digest(), CaptureProfile: raw["profile"].Digest(), Prompt: prompt, TargetVersion: aq.CIPairTargetVersion, TargetDigest: target.Digest(), SubjectDigest: commands[mode].Subject.Digest()}
		if mode == "BASELINE" {
			c.Baseline = pin
		} else {
			c.Candidate = pin
		}
	}
	plan := decision.FrozenPlan{Schema: decision.IndependentPlanVersion, ID: id, Version: "test-v1", ProjectID: f.s.ProjectID, CandidateArtifactDigest: artifact, FrozenAt: time.Now().UTC(), Cases: []decision.PlannedCase{c}}
	if e = plan.Validate(); e != nil {
		t.Fatal("real FrozenPlan", e)
	}
	return plan
}
func finishUnifiedEvidence(t *testing.T, f *g7Fixture, states []ev.RunState, commands map[string]ev.CreateRun, target *ciExchangeTarget, snap ev.Snapshot, body catalog.CaseContent, planPath, planDigest string, policy asset.Ref) {
	t.Helper()
	ctx := context.Background()
	must := func(e error) {
		t.Helper()
		if e != nil {
			t.Fatal(e)
		}
	}
	plan, e := decision.ReadIndependentPlan(planPath, planDigest)
	must(e)
	receipt, e := f.gate.CreateGate(ctx, f.s, f.gateCommand(decision.SourceRef{Kind: "RUN_SET", Runs: []string{states[0].Run.ID}}, decision.SourceRef{Kind: "RUN_SET", Runs: []string{states[1].Run.ID}}, policy))
	must(e)
	saved, e := f.gate.GetGateReceipt(ctx, f.s.Scope, receipt.GateID)
	must(e)
	comparison, e := f.gate.GetComparison(ctx, f.s.Scope, receipt.GateID)
	must(e)
	original, _ := asset.Freeze(receipt)
	read, _ := asset.Freeze(saved)
	if original.String() != read.String() {
		t.Fatal("receipt changed")
	}
	t.Logf("REAL_V2_GATE gate=%s decision=%s reasons=%v", receipt.GateID, saved.Decision, saved.Reasons)
	refs := decision.BranchReceiptRefs{Case: plan.Cases[0].Case.Ref, GateID: receipt.GateID, ComparisonDigest: func() string { j, _ := asset.Freeze(comparison); return j.Digest() }(), ReceiptDigest: read.Digest()}
	for i, state := range states {
		a := state.Attempts[0]
		evidence, e := aq.ReadCIDiagnosisEvidence(worker.EvaluationInput{Scope: f.s, Run: state.Run, Attempt: a})
		must(e)
		r := decision.RunReceiptRefs{RunID: state.Run.ID, AttemptID: a.ID, RequestID: a.RequestID, Results: []decision.ResultRef{}, QualityDigest: evidence.QualityDigest, SidecarDigest: evidence.SidecarDigest, ActualLoadDigest: evidence.LoadDigest}
		for _, result := range state.Results {
			j, _ := asset.Freeze(result)
			r.Results = append(r.Results, decision.ResultRef{ID: result.ID, Digest: j.Digest()})
		}
		if i == 0 {
			refs.Baseline = r
		} else {
			refs.Candidate = r
		}
	}
	dir := filepath.Dir(planPath)
	binding := decision.ExecutionBinding{Schema: decision.IndependentBindingVersion, PlanDigest: planDigest, Cases: []decision.BranchReceiptRefs{refs}}
	bindingPath := filepath.Join(dir, "execution-binding.json")
	bd, e := decision.WriteIndependentArtifact(bindingPath, binding)
	must(e)
	summary, e := aq.SummarizeIndependent(ctx, f.gate, f.s.Scope, planPath, planDigest, bindingPath, bd, filepath.Join(dir, "summary.json"))
	must(e)
	missingHoldout := false
	for _, reason := range summary.Missing {
		if reason == "HOLDOUT:BRANCH_MISSING" {
			missingHoldout = true
		}
	}
	if summary.Decision != decision.Blocked || !missingHoldout || len(summary.Cases) != 1 || summary.Cases[0].OriginalDecision != saved.Decision {
		t.Fatal("missing audit/Holdout incorrectly passed", summary)
	}
	t.Logf("REAL_FROZEN_PLAN_SUMMARY decision=%s missing=%v case_original_decision=%s", summary.Decision, summary.Missing, summary.Cases[0].OriginalDecision)
	// 同批持久来源的直接拒绝反例：不发起AC或模型。
	a := states[0].Attempts[0]
	in := worker.EvaluationInput{Scope: f.s, Run: states[0].Run, Attempt: a}
	evidence, e := aq.ReadCIDiagnosisEvidence(in)
	must(e)
	for _, field := range []string{"attempt", "request"} {
		bad := in
		if field == "attempt" {
			bad.Attempt.ID = asset.NewID()
		} else {
			bad.Attempt.RequestID = asset.NewID()
		}
		if _, e = aq.ReadCIDiagnosisEvidence(bad); e == nil {
			t.Fatal("identity transplant accepted")
		}
	}
	var config provider.CIPairConfig
	_ = a.Request.Target.Config.Decode(&config)
	var source aq.CISourceBinding
	_ = evidence.SourceBinding.Decode(&source)
	source.InputDigest = body.GroundTruth.Digest()
	badSource, _ := asset.Freeze(source)
	if _, e = aq.VerifyCISourceBinding(badSource, evidence, config.Frozen, evidence.Execution, evidence.Incident, config.Mode); e == nil {
		t.Fatal("wrong input accepted")
	}
	noAction := evidence
	noAction.Bundle.Components = append([]aq.QualityComponent(nil), evidence.Bundle.Components...)
	for i, component := range noAction.Bundle.Components {
		if component.Kind == "execution" {
			var fields map[string]asset.JSON
			_ = component.Body.Decode(&fields)
			fields["actions"], _ = asset.Freeze([]any{})
			component.Body, _ = asset.Freeze(fields)
			digest := component.Body.Digest()
			component.Digest = &digest
			noAction.Bundle.Components[i] = component
		}
	}
	if _, e = aq.VerifyCISourceBinding(evidence.SourceBinding, noAction, config.Frozen, evidence.Execution, evidence.Incident, config.Mode); e == nil {
		t.Fatal("missing action accepted")
	}
	unconsumed := evidence
	unconsumed.Observations.Models = append([]aq.QualityModel(nil), evidence.Observations.Models...)
	for i := range unconsumed.Observations.Models {
		unconsumed.Observations.Models[i].Sources = []aq.QualitySource{}
	}
	if _, e = aq.VerifyCISourceBinding(evidence.SourceBinding, unconsumed, config.Frozen, evidence.Execution, evidence.Incident, config.Mode); e == nil {
		t.Fatal("unconsumed source accepted")
	}
	t.Log("PERSISTED_SOURCE_NEGATIVES: Attempt/Request/Input/Action/RAW rejected")
	// 独立TEST_SCOPE拒绝Attempt，仅投送另一Run旧字节；不刷新或重执行任何AC意图。
	command := commands["BASELINE"]
	command.CommandID = asset.NewID()
	created, e := f.k.CreateRun(ctx, f.s, command)
	reply(t, created, e, ev.Applied)
	target.reject = true
	wc := worker.DefaultConfig()
	wc.ExecutionConcurrency = 1
	wc.EvaluatorConcurrency = 1
	wc.PollInterval = 10 * time.Millisecond
	wc.Reconcile = false
	runtime := &worker.Runtime{Config: wc, Backend: f.k, Target: target, Evaluator: aq.DeterministicEvaluator{}, Epoch: f.s.Epoch}
	running, cancel := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- runtime.Run(running) }()
	deadline := time.Now().Add(15 * time.Second)
	var state ev.RunState
	for time.Now().Before(deadline) {
		state, e = f.k.ReadRunState(ctx, f.s.Scope, created.ID)
		must(e)
		if len(state.Attempts) == 1 && state.Attempts[0].Status == "TERMINAL" {
			break
		}
		time.Sleep(25 * time.Millisecond)
	}
	cancel()
	must(<-done)
	if len(state.Attempts) != 1 || state.Attempts[0].Outcome != ev.Unknown || state.Attempts[0].Status != "TERMINAL" {
		t.Fatal("UNKNOWN not persisted", state)
	}
	must(state.Attempts[0].Metadata.Observation.Validate(f.s.ProjectID, state.Run.ID, state.Attempts[0].ID, state.Attempts[0].RequestID))
	for _, result := range state.Results {
		if result.Value.Verdict == "PASS" {
			t.Fatal("UNKNOWN manufactured success")
		}
	}
	t.Logf("UNKNOWN_PERSISTENCE=PASS run=%s attempt=%s source=foreign_run_no_AC_execution", created.ID, state.Attempts[0].ID)
	var super, wide, column bool
	must(f.k.Pool.QueryRow(ctx, "SELECT rolsuper,has_table_privilege(current_user,'projects','UPDATE'),has_column_privilege(current_user,'projects','id','UPDATE') FROM pg_roles WHERE rolname=current_user").Scan(&super, &wide, &column))
	if super || wide || !column {
		t.Fatal("role contract")
	}
	t.Log("EO_ROLE: NOSUPERUSER projects.UPDATE(id)=true table.UPDATE=false; AC_PRODUCTION_MINIMUM_ROLE=NOT_VERIFIED")
}
