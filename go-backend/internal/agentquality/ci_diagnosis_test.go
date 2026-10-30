package agentquality

import (
	"context"
	"crypto/sha256"
	"fmt"
	"testing"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
)

func diagnosisJSON(v any) asset.JSON {
	j, e := asset.Freeze(v)
	if e != nil {
		panic(e)
	}
	return j
}
func diagnosisHash(s string) string { return fmt.Sprintf("%x", sha256.Sum256([]byte(s))) }
func diagnosisPtr(s string) *string { return &s }

// 公开合成 Owner 记录；不是远程模型、真实业务 GT 或独立 Holdout。
func publicDiagnosisInput(t *testing.T, mutation string) worker.EvaluationInput {
	t.Helper()
	project, run, attempt, request, incident := asset.NewID(), asset.NewID(), asset.NewID(), asset.NewID(), asset.NewID()
	input := diagnosisJSON(map[string]string{"template_id": "report_pipeline_failure", "template_version": "2", "input_digest": diagnosisHash("public input")})
	source := catalog.CIDiagnosisSource{Kind: "PARSER_LOG", EvidenceID: "public-parser", Digest: diagnosisHash("public source"), ContentDigest: diagnosisHash("public tool content")}
	gt := catalog.CIDiagnosisGroundTruth{Version: catalog.CIDiagnosisGTVersion, Status: "SUPPORTED", Stage: "PARSER", Owner: "TOOL_MAINTAINER", Sources: []catalog.CIDiagnosisSource{source}}
	if mutation == "source" {
		gt.Sources[0].ContentDigest = diagnosisHash("unavailable tool content")
	}
	g := diagnosisJSON(gt)
	if mutation == "gt" {
		g = asset.JSON{}
	}
	metadata := catalog.CIDiagnosisMetadata{Version: catalog.CIDiagnosisMetadataVersion, Purpose: "GOLDEN", SourceDigest: diagnosisHash("public source lineage"), FamilyDigest: diagnosisHash("public family"), InputDigest: input.Digest(), GroundTruthDigest: g.Digest(), ReviewID: asset.NewID(), Reviewer: "public-reviewer", Developer: "public-developer", Exposure: "PUBLIC", DeveloperReadable: true}
	c := catalog.CaseContent{Input: input, GroundTruth: g, Metadata: diagnosisJSON(map[string]any{"ci_diagnosis": metadata}), Capability: catalog.CIDiagnosisCapability, Type: catalog.Golden, Criticality: catalog.Critical, BodyPolicy: catalog.Retained, TaskGoal: "公开合成诊断", AcceptanceCriteria: []string{"按已审核字段判定"}, Applicability: asset.Applicability{RuleRef: "ci-public.v1"}}
	ci := ev.CaseInput{Identity: ev.AssetIdentity{Ref: asset.Ref{EntityID: asset.NewID(), Version: "public-v1"}, ProjectID: project, ContentDigest: diagnosisJSON(c).Digest()}, Case: c}
	report := diagnosisReport{Schema: "ci-diagnosis.v1", Kind: "REPORT", Incident: incident, Status: "SUPPORTED", Stage: "PARSER", Owner: "TOOL_MAINTAINER", Hypothesis: "公开合成假设", Evidence: []diagnosisReference{{ID: source.EvidenceID, Digest: source.Digest}}, Uncertainties: []string{}, Checks: []string{"核对已读取证据"}}
	switch mutation {
	case "status":
		report.Status = "INSUFFICIENT_EVIDENCE"
		report.Uncertainties = []string{"公开不确定性"}
	case "stage":
		report.Stage = "AUTOMATION"
	case "owner":
		report.Owner = "TEST_OWNER"
	case "reference":
		report.Evidence[0].Digest = diagnosisHash("wrong")
	}
	raw := diagnosisJSON(report).String()
	manifest := diagnosisJSON(QualityManifest{Schema: "agent-quality-execution-manifest.v1", Build: diagnosisHash("build"), Fixture: "public-ci.v1", MemoryEffect: "SERVICE_PROJECT_READ_ONLY.v1", Agents: map[string]QualityAgentPin{"ci_incident_investigator": {Version: "v1", Policy: "durable-agent-loop.v2", DefinitionDigest: diagnosisHash("definition"), ConfigDigest: diagnosisHash("config")}}})
	profile := diagnosisJSON(QualityProfile{ID: "public-ci", Version: 1, Policy: "agent-quality-capture.v1", Aliases: map[string]string{}, ParameterPaths: map[string][]string{}, FactKeys: []string{}})
	frozen := diagnosisJSON(map[string]string{"case_id": ci.Identity.Ref.EntityID, "case_version": ci.Identity.Ref.Version, "case_digest": ci.Identity.ContentDigest, "template_id": "report_pipeline_failure", "template_version": "2", "input_digest": diagnosisHash("public input")})
	cfg := diagnosisJSON(map[string]any{"quality": map[string]any{"execution_manifest": manifest, "capture_profile": profile, "trusted_project_id": project}, "frozen_ci": frozen, "candidate_manifest_digest": diagnosisHash("artifact"), "mode": "CANDIDATE", "prompt_digest": diagnosisHash("prompt"), "candidate_id": "ci-prompt-" + diagnosisHash("artifact")})
	target := ev.Target{ID: "ci-synthetic-http", Version: "ci-synthetic-target.v2", Kind: "LOCALAGENT_HTTP", Config: cfg, TimeoutMilliseconds: 1000}
	corr := QualityCorrelation{Agent: "ci_incident_investigator", Run: attempt, Project: project, EvaluationRun: run, Attempt: attempt, Request: request, Case: ci.Identity.Ref.EntityID, CaseVersion: ci.Identity.Ref.Version, CaseDigest: ci.Identity.ContentDigest, ManifestDigest: manifest.Digest(), ProfileID: "public-ci", ProfileVersion: 1, ProfileDigest: profile.Digest(), Timeout: 1000, QueryDigest: diagnosisHash("请调查固定的公开合成 CI 失败，使用授权工具取证。")}
	frame := diagnosisJSON("public-frame").Digest()
	snapshot := diagnosisHash("snapshot")
	iteration := 2
	accepted := "FINAL"
	observation := diagnosisHash("public tool observation")
	obs := QualityObservations{Schema: QualityObservationsSchema, Extraction: "agent-quality-projection.v1", Coverage: "COMPLETE", Gaps: []string{}, Tools: []QualityTool{{Source: "tool:" + diagnosisHash("invocation"), Frame: frame, Iteration: 1, Action: diagnosisPtr(diagnosisHash("action")), Invocation: diagnosisPtr(diagnosisHash("invocation")), Call: diagnosisPtr(diagnosisHash("call")), Parameters: []QualityParameter{}, State: "COMMITTED", Content: &source.ContentDigest, Result: diagnosisPtr(diagnosisHash("result")), Observation: &observation, ScanComplete: true}}, Models: []QualityModel{{Source: "model:public", Frame: &frame, Iteration: &iteration, Snapshot: &snapshot, Root: true, Purpose: "DECISION", State: "COMPLETED", Request: diagnosisHash("request"), Messages: diagnosisPtr(diagnosisHash("messages")), Verified: true, ScanComplete: true, AcceptedKind: &accepted, Sources: []QualitySource{{Digest: &observation, Ref: diagnosisPtr(diagnosisJSON("source:1").Digest()), Representation: diagnosisPtr("RAW")}}}}, Contexts: []QualityContext{}, Children: []QualityChild{}, Facts: []QualityFact{}}
	if mutation == "coverage" {
		obs.Coverage = "PARTIAL"
		obs.Gaps = []string{"TOOL_SOURCE_INVALID"}
	}
	if mutation == "consumption" {
		obs.Models[0].Sources = []QualitySource{}
	}
	batch := obs.Models[0]
	batch.Source = "model:batch"
	batch.Iteration = new(int)
	*batch.Iteration = 1
	batch.AcceptedKind = diagnosisPtr("TOOL_BATCH")
	batch.Sources = []QualitySource{}
	batch.Request = diagnosisHash("batch request")
	obs.Models = append(obs.Models, batch)
	oj := diagnosisJSON(obs)
	seq := int64(1)
	execution := diagnosisJSON(map[string]any{"schema_version": "agent-execution-evidence.v1", "execution_policy": "durable-agent-loop.v2", "coverage": "COMPLETE", "gaps": []string{}, "run_ref": diagnosisJSON(attempt).Digest(), "outcome": "SUCCEEDED", "frames": []any{map[string]any{"ref": frame, "role": "ROOT", "agent_ref": diagnosisHash("agent"), "parent_ref": nil, "seed_digest": diagnosisHash("seed"), "config_digest": diagnosisHash("config"), "generation": 0, "fence": 0, "final_delivered": true}}, "decisions": []any{map[string]any{"ref": diagnosisHash("batch decision"), "frame_ref": frame, "iteration": 1, "kind": "TOOL_BATCH", "decision_digest": diagnosisHash("batch payload"), "model_ref": "batch", "snapshot_ref": snapshot, "observation_digest": observation}, map[string]any{"ref": diagnosisHash("decision"), "frame_ref": frame, "iteration": 2, "kind": "FINAL", "decision_digest": diagnosisHash("decision payload"), "model_ref": "public", "snapshot_ref": snapshot, "observation_digest": nil}}, "actions": []any{map[string]any{"ref": diagnosisHash("action"), "frame_ref": frame, "iteration": 1, "ordinal": 0, "call_ref": diagnosisHash("call"), "invocation_ref": diagnosisHash("invocation"), "recipe_digest": diagnosisHash("recipe"), "state": "COMMITTED", "source_digest": diagnosisHash("result"), "projection_digest": diagnosisHash("projection")}}, "delegations": []any{}, "snapshots": []any{map[string]any{"ref": snapshot, "frame_ref": frame, "version": 1, "digest": diagnosisHash("snapshot body"), "source_digest": nil, "mandatory_digest": nil, "validator": "context-validator.v2"}}, "memory_binding": nil, "budget": map[string]any{"consumed": map[string]int{}, "reserved": map[string]int{}}, "final_receipt_digest": diagnosisHash("final")})
	if mutation == "action-root" || mutation == "action-missing" || mutation == "action-invocation" || mutation == "action-source" || mutation == "unobserved" || mutation == "action-frame" {
		var ex map[string]asset.JSON
		_ = execution.Decode(&ex)
		if mutation == "action-root" {
			ex["run_ref"] = diagnosisJSON(diagnosisHash("other root"))
		} else if mutation == "action-missing" {
			ex["actions"] = diagnosisJSON([]any{})
		} else if mutation == "unobserved" {
			var ds []map[string]asset.JSON
			_ = ex["decisions"].Decode(&ds)
			ds[0]["observation_digest"] = diagnosisJSON(diagnosisHash("other observation"))
			ex["decisions"] = diagnosisJSON(ds)
		} else {
			var actions []map[string]asset.JSON
			_ = ex["actions"].Decode(&actions)
			key := map[string]string{"action-invocation": "invocation_ref", "action-source": "source_digest", "action-frame": "frame_ref"}[mutation]
			actions[0][key] = diagnosisJSON(diagnosisHash("transplanted"))
			ex["actions"] = diagnosisJSON(actions)
		}
		execution = diagnosisJSON(ex)
	}
	reason := "UNSUPPORTED_EXECUTION_POLICY"
	components := []QualityComponent{{Kind: "context", Schema: "context-evidence.v1", Availability: "NOT_APPLICABLE", Reason: &reason}, {Kind: "execution", Schema: "agent-execution-evidence.v1", Availability: "AVAILABLE", Body: execution, Digest: diagnosisPtr(execution.Digest())}, {Kind: "loop", Schema: LoopEvidenceSchema, Availability: "NOT_APPLICABLE", Reason: &reason}, {Kind: "memory", Schema: "memory-lifecycle-evidence.v1", Availability: "NOT_APPLICABLE", Reason: &reason}}
	b := QualityBundle{Schema: QualitySchema, Producer: "localagent-agent-quality-producer.v1", Capture: "agent-quality-capture.v1", Algorithm: asset.CatalogAlgorithm, ID: "agent-quality://" + attempt, Correlation: corr, Fingerprint: diagnosisHash("fingerprint"), Manifest: manifest, ManifestDigest: manifest.Digest(), Profile: profile, ProfileDigest: profile.Digest(), Policy: "durable-agent-loop.v2", Terminal: QualityTerminal{Run: attempt, Status: "SUCCEEDED", Stop: "COMPLETED", Sequence: &seq}, Final: &QualityFinal{Digest: diagnosisHash(raw), Event: "public-event", Cursor: 1}, Components: components, Observations: oj, ObservationsDigest: diagnosisPtr(oj.Digest()), Coverage: obs.Coverage, Gaps: obs.Gaps, Sealed: true}
	b.Watermark.Sequence = &seq
	b.Watermark.Frames = make([]struct {
		Ref        string `json:"ref"`
		Generation int64  `json:"generation"`
		Cursor     int64  `json:"observation_cursor"`
	}, 0)
	bj := diagnosisJSON(b)
	answer := diagnosisJSON(map[string]any{"schema_version": "final-answer-evidence.v1", "evidence_id": "final-answer://" + attempt, "run_id": attempt, "attempt_id": attempt, "media_type": "text/plain; charset=utf-8", "content_sha256": b.Final.Digest, "content": raw})
	wire := diagnosisJSON(map[string]any{"protocol_version": "localagent-agent-quality-evaluation-execute.v1", "run_id": attempt, "status": "SUCCEEDED", "stop_reason": "COMPLETED", "capture_status": b.Coverage, "bundle": bj, "bundle_digest": bj.Digest(), "final_answer_evidence": answer})
	var side diagnosisSidecar
	side.Schema = "ci-synthetic-evidence.v1"
	side.Quality = wire.Digest()
	side.Report = &b.Final.Digest
	side.Status = "SUCCEEDED"
	sb := &side.Binding
	sb.Contract = "ci-synthetic-evaluation-execute.v1"
	sb.Source = "SYNTHETIC"
	sb.Run = attempt
	sb.Session = diagnosisSession(attempt)
	sb.Agent = corr.Agent
	sb.Version = "v1"
	sb.Instance = asset.NewID()
	sb.Execution = asset.NewID()
	sb.Incident = incident
	sb.Investigation = asset.NewID()
	sb.ReportVersion = 1
	sb.Frozen = frozen
	sb.Manifest = diagnosisHash("artifact")
	sb.Mode = "CANDIDATE"
	sb.Correlation = corr
	model := diagnosisJSON(map[string]any{"model_ref": "public-model", "frame_ref": "public-frame", "run_id": attempt, "model_state": "COMPLETED", "proof": map[string]any{"schema_version": "ci-prompt-selection.v1", "candidate_id": "ci-prompt-" + diagnosisHash("artifact"), "manifest_digest": sb.Manifest, "mode": "CANDIDATE", "prompt_digest": diagnosisHash("prompt"), "model_request_digest": obs.Models[0].Request, "messages_digest": *obs.Models[0].Messages, "system_digest": diagnosisHash("system"), "evidence_scope": "DURABLE_REQUEST_PREPARED"}})
	var second map[string]asset.JSON
	_ = model.Decode(&second)
	second["model_ref"] = diagnosisJSON("batch-model")
	var proof map[string]asset.JSON
	_ = second["proof"].Decode(&proof)
	proof["model_request_digest"] = diagnosisJSON(batch.Request)
	second["proof"] = diagnosisJSON(proof)
	modelArray := diagnosisJSON([]asset.JSON{model, diagnosisJSON(second)})
	if modelArray.Decode(&side.Models) != nil {
		t.Fatal("model fixture")
	}
	sidej := diagnosisJSON(side)
	binding := func(schema string, j asset.JSON) ev.Binding {
		return ev.Binding{ProjectID: project, RunID: run, AttemptID: attempt, RequestID: request, Ref: "public:" + schema, Schema: schema, Availability: "AVAILABLE", Body: j, Digest: j.Digest()}
	}
	artifact := binding("artifact.v1", diagnosisJSON(raw))
	artifact.Ref = "final-answer://" + attempt
	evidence := []ev.Binding{binding(QualitySchema, bj), binding("ci-synthetic-evidence.v1", sidej)}
	if mutation == "binding" {
		evidence[1].AttemptID = asset.NewID()
	}
	metricRef := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	d := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: CIDiagnosisInput, OutputMetrics: []asset.Ref{metricRef}, Applicability: asset.Applicability{RuleRef: "public-ci.v1"}, Availability: asset.ContractOnly, ImplementationRef: CIDiagnosisImplementation, SchemaVersion: CIDiagnosisResult, Normalization: CIDiagnosisNormalization, Config: diagnosisJSON(Config{Metric: "task_success.v1"}), Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 4096}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}}
	return worker.EvaluationInput{Scope: ev.Scope{Scope: asset.Scope{ProjectID: project, OrganizationID: asset.NewID(), Principal: "public-operator"}}, Run: ev.Run{ID: run, Snapshot: ev.RunSnapshot{Target: target}}, Attempt: ev.Attempt{ID: attempt, RunID: run, RequestID: request, Outcome: ev.Success, Request: ev.Request{Case: ci, Target: target}, Metadata: ev.AttemptMetadata{Observation: ev.Outcome{Artifact: &artifact, Evidence: evidence}}}, Work: ev.Work{SpecDigest: diagnosisHash("spec"), Metadata: ev.WorkMetadata{Spec: ev.EvaluatorSpec{Definition: d}}}}
}

func TestCIDiagnosisDeterministicContract(t *testing.T) {
	for _, tc := range []struct{ mutation, want string }{{"", "PASS"}, {"status", "FAIL"}, {"stage", "FAIL"}, {"owner", "FAIL"}, {"reference", "FAIL"}, {"gt", "INCONCLUSIVE"}, {"source", "INCONCLUSIVE"}, {"coverage", "INCONCLUSIVE"}, {"consumption", "INCONCLUSIVE"}, {"binding", "INCONCLUSIVE"}} {
		t.Run(tc.mutation, func(t *testing.T) {
			in := publicDiagnosisInput(t, tc.mutation)
			if !DeterministicSupported(in.Work.Metadata.Spec.Definition) {
				t.Fatal("not registered")
			}

			out, e := (DeterministicEvaluator{}).Evaluate(context.Background(), in)
			if e != nil || out.Value.Verdict != tc.want {
				t.Fatalf("got %s/%s err=%v", out.Value.Verdict, out.Value.Reason, e)
			}
			var provenance struct {
				Task          TaskJudgment             `json:"task_success"`
				Applicability asset.ApplicabilityState `json:"applicability"`
			}
			var fields map[string]asset.JSON
			_ = out.Value.Provenance.Decode(&fields)
			_ = fields["task_success"].Decode(&provenance.Task)
			_ = fields["applicability"].Decode(&provenance.Applicability)
			if provenance.Task.Validate() != nil {
				t.Fatal("invalid original TaskJudgment")
			}
			if tc.want == "INCONCLUSIVE" && (out.Value.Score != nil || provenance.Task.Decision != TaskInconclusive || provenance.Applicability != asset.MissingEvidence) {
				t.Fatal("missing evidence became success")
			}
			if tc.mutation == "" {
				evidence, e := ReadCIDiagnosisEvidence(in)
				if e != nil {
					t.Fatal(e)
				}
				var manifest QualityManifest
				_ = evidence.Bundle.Manifest.Decode(&manifest)
				agent := manifest.Agents["ci_incident_investigator"]
				pin := decision.ExecutionPin{RuntimeBuild: manifest.Build, RuntimeConfig: agent.ConfigDigest, Definition: agent.DefinitionDigest, Manifest: evidence.Bundle.ManifestDigest, CaptureProfile: evidence.Bundle.ProfileDigest, Prompt: diagnosisHash("prompt")}
				refs := decision.RunReceiptRefs{QualityDigest: evidence.QualityDigest, SidecarDigest: evidence.SidecarDigest, ActualLoadDigest: evidence.LoadDigest}
				state := ev.RunState{Run: in.Run, Attempts: []ev.Attempt{in.Attempt}}
				reader := verifiedIndependentReader{}
				plan := decision.FrozenPlan{CandidateArtifactDigest: diagnosisHash("artifact")}
				if e = reader.VerifyIndependentEvidence(in.Scope.Scope, plan, decision.PlannedCase{}, pin, refs, "CANDIDATE", state); e != nil {
					t.Fatal("actual load rejected", e)
				}
				pin.Prompt = diagnosisHash("wrong prompt")
				if reader.VerifyIndependentEvidence(in.Scope.Scope, plan, decision.PlannedCase{}, pin, refs, "CANDIDATE", state) == nil {
					t.Fatal("wrong actual load accepted")
				}
			}
		})
	}
}

func TestCIDiagnosisCommittedActionConsumption(t *testing.T) {
	for _, mutation := range []string{"", "action-root", "action-missing", "action-invocation", "action-source", "action-frame", "unobserved", "consumption"} {
		t.Run(mutation, func(t *testing.T) {
			in := publicDiagnosisInput(t, mutation)
			out, e := (DeterministicEvaluator{}).Evaluate(context.Background(), in)
			want := "INCONCLUSIVE"
			if mutation == "" {
				want = "PASS"
			}
			if e != nil || out.Value.Verdict != want {
				t.Fatalf("%s got %s err=%v", mutation, out.Value.Verdict, e)
			}
			if want == "INCONCLUSIVE" && (out.Value.Score != nil || out.Value.SourceError != "MISSING_EVIDENCE") {
				t.Fatal("missing action admitted")
			}
		})
	}
}
