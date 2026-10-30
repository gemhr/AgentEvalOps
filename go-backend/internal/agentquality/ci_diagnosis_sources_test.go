package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"testing"
)

type publicSourceSample struct {
	Binding asset.JSON      `json:"binding"`
	Source  CISourceBinding `json:"source_binding"`
	Digest  string          `json:"source_binding_digest"`
	Frame   string          `json:"frame_id"`
}
type publicSourceVector struct {
	Schema    string            `json:"schema_version"`
	Input     asset.JSON        `json:"visible_input"`
	GT        asset.JSON        `json:"ground_truth"`
	Metadata  asset.JSON        `json:"metadata"`
	Semantics map[string]string `json:"semantic_digests"`
	Branches  []struct {
		Kind      string     `json:"kind"`
		Execution string     `json:"execution_id"`
		Content   asset.JSON `json:"content"`
		Digest    string     `json:"semantic_digest"`
	} `json:"branches"`
	Samples []publicSourceSample `json:"samples"`
}

func readPublicSources(t *testing.T) publicSourceVector {
	t.Helper()
	raw, e := os.ReadFile("testdata/ci_source_pair.json")
	if e != nil {
		t.Fatal(e)
	}
	j, e := asset.ParseJSON(raw)
	var v publicSourceVector
	if e != nil || j.Decode(&v) != nil || len(v.Samples) != 2 {
		t.Fatal("Python source vector", e)
	}
	return v
}
func pairDiagnosisInput(t *testing.T, index int, mutation string) worker.EvaluationInput {
	t.Helper()
	v := readPublicSources(t)
	sample := v.Samples[index]
	source := sample.Source
	var base map[string]asset.JSON
	_ = sample.Binding.Decode(&base)
	var frozen catalog.CIPairFrozen
	if base["frozen_ci"].Decode(&frozen) != nil || frozen.Validate() != nil {
		t.Fatal("frozen Python input")
	}
	corr := source.Correlation
	run := corr.Attempt
	manifest := diagnosisJSON(QualityManifest{Schema: "agent-quality-execution-manifest.v1", Build: diagnosisHash("build"), Fixture: "public-ci.v2", MemoryEffect: "SERVICE_PROJECT_READ_ONLY.v1", Agents: map[string]QualityAgentPin{corr.Agent: {Version: "public-v2", Policy: "durable-agent-loop.v2", DefinitionDigest: diagnosisHash("definition"), ConfigDigest: diagnosisHash("runtime")}}})
	profile := diagnosisJSON(QualityProfile{ID: "public-ci", Version: 1, Policy: "agent-quality-capture.v1", Aliases: map[string]string{"ci_get_execution_context": "ci_get_execution_context", "ci_read_execution_evidence": "ci_read_execution_evidence"}, ParameterPaths: map[string][]string{}, FactKeys: []string{}})
	corr.ManifestDigest = manifest.Digest()
	corr.ProfileDigest = profile.Digest()
	source.Correlation = corr
	if ciHash(sample.Source) != sample.Digest {
		t.Fatal("source binding Python digest")
	}
	observations := QualityObservations{Schema: QualityObservationsSchema, Extraction: "agent-quality-projection.v1", Coverage: "COMPLETE", Gaps: []string{}, Tools: []QualityTool{}, Models: []QualityModel{}, Contexts: []QualityContext{}, Children: []QualityChild{}, Facts: []QualityFact{}}
	actions := []any{}
	decisions := []any{}
	snapshots := []any{}
	sideModels := []any{}
	observationsByIteration := map[int]string{1: diagnosisHash(run + "observed1"), 2: diagnosisHash(run + "observed2"), 3: diagnosisHash(run + "observed3")}
	for _, tool := range source.Tools {
		var raw ciRawToolResult
		if StrictQualityShape([]byte(tool.ResultBytes), &raw) != nil {
			t.Fatal("Owner result shape")
		}
		observation := observationsByIteration[tool.Iteration]
		contentHash := ciTextHash(raw.Output.Content)
		alias := tool.Name
		observations.Tools = append(observations.Tools, QualityTool{Source: "tool:" + tool.Invocation, Frame: tool.Frame, Iteration: tool.Iteration, Ordinal: tool.Ordinal, Action: diagnosisPtr(tool.Action), Call: diagnosisPtr(tool.Call), Invocation: diagnosisPtr(tool.Invocation), Alias: &alias, Arguments: diagnosisPtr(tool.ArgumentsDigest), Parameters: []QualityParameter{}, ReadOnly: true, State: "COMMITTED", Result: diagnosisPtr(tool.Result), EffectProof: "NOT_APPLICABLE", ScanComplete: true, Observation: &observation, Content: &contentHash, ExecutionStatus: diagnosisPtr("SUCCEEDED")})
		if mutation != "no-action" || tool.Iteration != 3 {
			actions = append(actions, map[string]any{"ref": tool.Action, "frame_ref": tool.Frame, "iteration": tool.Iteration, "ordinal": tool.Ordinal, "call_ref": tool.Call, "invocation_ref": tool.Invocation, "recipe_digest": ciHash(map[string]any{"tool_name": tool.Name, "arguments": tool.Arguments}), "state": "COMMITTED", "source_digest": tool.Result, "projection_digest": ciHash(map[string]any{"outcome": "SUCCEEDED", "content": raw.Output.Content})})
		}
	}
	candidate := "ci-prompt-" + diagnosisHash("candidate")
	candidateManifest := diagnosisHash("candidate manifest")
	prompt := diagnosisHash("prompt-" + source.Mode)
	frame := ciHash(sample.Frame)
	for iteration := 1; iteration <= 4; iteration++ {
		kind := "TOOL_BATCH"
		if iteration == 4 {
			kind = "FINAL"
		}
		suffix := diagnosisHash(fmt.Sprintf("%s-model-%d", run, iteration))
		snapshot := diagnosisHash(fmt.Sprintf("%s-snapshot-%d", run, iteration))
		request := diagnosisHash(suffix + "request")
		messages := diagnosisHash(suffix + "messages")
		used := []QualitySource{}
		for prev := 1; prev < iteration; prev++ {
			if mutation == "unconsumed" && prev == 3 {
				continue
			}
			used = append(used, QualitySource{Ref: diagnosisPtr(ciHash(fmt.Sprintf("source:%d", prev))), Digest: diagnosisPtr(observationsByIteration[prev]), Representation: diagnosisPtr("RAW")})
		}
		i := iteration
		observations.Models = append(observations.Models, QualityModel{Source: "model:" + suffix, Frame: &frame, Iteration: &i, Purpose: "DECISION", State: "COMPLETED", Request: request, Snapshot: &snapshot, Messages: &messages, ScanComplete: true, Verified: true, Sources: used, Root: true, AcceptedKind: &kind, AcceptedDigest: diagnosisPtr(diagnosisHash(suffix + "decision"))})
		var observation any
		if iteration < 4 {
			observation = observationsByIteration[iteration]
		}
		decisions = append(decisions, map[string]any{"ref": diagnosisHash(suffix + "decision"), "frame_ref": frame, "iteration": iteration, "kind": kind, "decision_digest": diagnosisHash(suffix + "decision body"), "model_ref": suffix, "snapshot_ref": snapshot, "observation_digest": observation})
		snapshots = append(snapshots, map[string]any{"ref": snapshot, "frame_ref": frame, "version": 1, "digest": diagnosisHash(snapshot + "body"), "source_digest": nil, "mandatory_digest": nil, "validator": "context-validator.v2"})
		sideModels = append(sideModels, map[string]any{"model_ref": suffix, "frame_ref": sample.Frame, "run_id": run, "model_state": "COMPLETED", "proof": map[string]any{"schema_version": "ci-prompt-selection.v1", "candidate_id": candidate, "manifest_digest": candidateManifest, "mode": source.Mode, "prompt_digest": prompt, "model_request_digest": request, "messages_digest": messages, "system_digest": diagnosisHash(prompt + "system"), "evidence_scope": "DURABLE_REQUEST_PREPARED"}})
	}
	report := diagnosisReport{Schema: "ci-diagnosis.v1", Kind: "REPORT", Status: "SUPPORTED", Stage: "PARSER", Owner: "TOOL_MAINTAINER", Hypothesis: "公开合成来源核验", Uncertainties: []string{}, Checks: []string{"核对已读工具版本"}, Evidence: []diagnosisReference{}}
	_ = base["incident_id"].Decode(&report.Incident)
	for _, kind := range []string{"PARSER_LOG", "ENVIRONMENT_RECORD", "CASE_REQUIREMENTS"} {
		for _, entry := range source.Manifest {
			if entry.Kind == kind {
				report.Evidence = append(report.Evidence, diagnosisReference{ID: entry.ID, Digest: *entry.Digest})
			}
		}
	}
	if mutation == "wrong-label" {
		report.Stage = "AUTOMATION"
	}
	if mutation == "report-foreign" {
		report.Evidence[0].ID = v.Samples[1-index].Source.Manifest[0].ID
	}
	rawReport := diagnosisJSON(report).String()
	reportHash := ciTextHash(rawReport)
	execution := diagnosisJSON(map[string]any{"schema_version": "agent-execution-evidence.v1", "execution_policy": "durable-agent-loop.v2", "coverage": "COMPLETE", "gaps": []string{}, "run_ref": ciHash(run), "outcome": "SUCCEEDED", "frames": []any{map[string]any{"ref": frame, "role": "ROOT", "agent_ref": diagnosisHash("agent"), "parent_ref": nil, "seed_digest": diagnosisHash("seed"), "config_digest": diagnosisHash("runtime"), "generation": 0, "fence": 0, "final_delivered": true}}, "decisions": decisions, "actions": actions, "delegations": []any{}, "snapshots": snapshots, "memory_binding": nil, "budget": map[string]any{"consumed": map[string]int{}, "reserved": map[string]int{}}, "final_receipt_digest": diagnosisHash("final")})
	reason := "UNSUPPORTED_EXECUTION_POLICY"
	components := []QualityComponent{{Kind: "context", Schema: "context-evidence.v1", Availability: "NOT_APPLICABLE", Reason: &reason}, {Kind: "execution", Schema: "agent-execution-evidence.v1", Availability: "AVAILABLE", Body: execution, Digest: diagnosisPtr(execution.Digest())}, {Kind: "loop", Schema: LoopEvidenceSchema, Availability: "NOT_APPLICABLE", Reason: &reason}, {Kind: "memory", Schema: "memory-lifecycle-evidence.v1", Availability: "NOT_APPLICABLE", Reason: &reason}}
	observed := diagnosisJSON(observations)
	seq := int64(1)
	bundle := QualityBundle{Schema: QualitySchema, Producer: "localagent-agent-quality-producer.v1", Capture: "agent-quality-capture.v1", Algorithm: asset.CatalogAlgorithm, ID: "agent-quality://" + run, Correlation: corr, Fingerprint: diagnosisHash("fingerprint"), Manifest: manifest, ManifestDigest: manifest.Digest(), Profile: profile, ProfileDigest: profile.Digest(), Policy: "durable-agent-loop.v2", Terminal: QualityTerminal{Run: run, Status: "SUCCEEDED", Stop: "COMPLETED", Sequence: &seq}, Final: &QualityFinal{Digest: reportHash, Event: "public-final", Cursor: 1}, Components: components, Observations: observed, ObservationsDigest: diagnosisPtr(observed.Digest()), Coverage: "COMPLETE", Gaps: []string{}, Sealed: true}
	bundle.Watermark.Sequence = &seq
	bundle.Watermark.Frames = make([]struct {
		Ref        string `json:"ref"`
		Generation int64  `json:"generation"`
		Cursor     int64  `json:"observation_cursor"`
	}, 0)
	bundleJSON := diagnosisJSON(bundle)
	answer := diagnosisJSON(map[string]any{"schema_version": "final-answer-evidence.v1", "evidence_id": "final-answer://" + run, "run_id": run, "attempt_id": run, "media_type": "text/plain; charset=utf-8", "content_sha256": reportHash, "content": rawReport})
	qualityResponse := diagnosisJSON(map[string]any{"protocol_version": "localagent-agent-quality-evaluation-execute.v1", "run_id": run, "status": "SUCCEEDED", "stop_reason": "COMPLETED", "capture_status": "COMPLETE", "bundle": bundleJSON, "bundle_digest": bundleJSON.Digest(), "final_answer_evidence": answer})
	if mutation == "foreign-source" {
		source.Correlation.Attempt = v.Samples[1-index].Source.Correlation.Attempt
	}
	if mutation == "missing-raw" {
		source.Tools = source.Tools[:3]
	}
	if mutation == "tampered-raw" {
		source.Tools[1].ResultBytes += " "
	}
	if mutation == "duplicate" {
		source.Manifest[1] = source.Manifest[0]
	}
	if mutation == "unknown-source" {
		source.Schema = "ci-runtime-source-binding.unknown"
	}
	// 所有外层摘要重新封装；原Tool Owner digest故意保留，不能重写持久执行真相。
	sourceJSON := diagnosisJSON(source)
	var executionID string
	_ = base["execution_id"].Decode(&executionID)
	side := diagnosisJSON(map[string]any{"schema_version": CIPairSidecarVersion, "binding": map[string]any{"contract_version": CIPairProtocol, "source_type": "SYNTHETIC", "run_id": run, "session_id": diagnosisSession(run), "agent_id": corr.Agent, "agent_version": "public-v2", "instance_id": asset.NewID(), "execution_id": executionID, "incident_id": report.Incident, "investigation_id": asset.NewID(), "report_version": 1, "frozen_ci": frozen, "manifest_digest": candidateManifest, "mode": source.Mode, "quality_correlation": corr, "plan_digest": source.PlanDigest}, "models": sideModels, "quality_digest": qualityResponse.Digest(), "report_digest": reportHash, "runtime_status": "SUCCEEDED", "source_binding": sourceJSON, "source_binding_digest": sourceJSON.Digest()})
	target := ev.Target{ID: "ci-synthetic-http", Kind: "LOCALAGENT_HTTP", Version: CIPairTargetVersion, Config: diagnosisJSON(map[string]any{"protocol_version": CIPairProtocol, "quality": map[string]any{"trusted_project_id": corr.Project, "execution_manifest": manifest, "capture_profile": profile}, "frozen_ci": frozen, "candidate_manifest_digest": candidateManifest, "mode": source.Mode, "prompt_digest": prompt, "candidate_id": candidate}), TimeoutMilliseconds: 1000}
	if mutation == "mixed-target" {
		target.Version = "ci-synthetic-target.v2"
	}
	if mutation == "mixed-protocol" {
		var fields map[string]asset.JSON
		_ = target.Config.Decode(&fields)
		fields["protocol_version"] = diagnosisJSON("ci-synthetic-evaluation-execute.v1")
		target.Config = diagnosisJSON(fields)
	}
	metricRef := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	definition := metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: "ci-diagnosis-quality-input.v2", OutputMetrics: []asset.Ref{metricRef}, Applicability: asset.Applicability{RuleRef: "public-ci.v2"}, Availability: asset.ContractOnly, ImplementationRef: CIDiagnosisImplementationV2, SchemaVersion: "ci-diagnosis-result.v2", Normalization: "ci-diagnosis-exact-fields.v2", Config: diagnosisJSON(Config{Metric: "task_success.v1"}), Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 4096}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}}
	c := catalog.CaseContent{Input: v.Input, GroundTruth: v.GT, Metadata: v.Metadata, Capability: catalog.CIDiagnosisCapability, Type: catalog.Golden, Criticality: catalog.Critical, BodyPolicy: catalog.Retained, TaskGoal: "公开合成诊断", AcceptanceCriteria: []string{"按独立冻结GT核验"}, Applicability: asset.Applicability{RuleRef: "public-ci.v2"}}
	if mutation == "wrong-semantics" {
		var g catalog.CIDiagnosisGroundTruthV2
		_ = c.GroundTruth.Decode(&g)
		g.Sources[0].Expected = diagnosisHash("unmatched independent material")
		c.GroundTruth = diagnosisJSON(g)
		var metadata map[string]asset.JSON
		_ = c.Metadata.Decode(&metadata)
		var lineage catalog.CIDiagnosisMetadata
		_ = metadata["ci_diagnosis"].Decode(&lineage)
		lineage.GroundTruthDigest = c.GroundTruth.Digest()
		metadata["ci_diagnosis"] = diagnosisJSON(lineage)
		c.Metadata = diagnosisJSON(metadata)
	}
	if mutation == "mixed-gt" {
		c.GroundTruth = diagnosisJSON(map[string]string{"schema_version": catalog.CIDiagnosisGTVersion})
	}
	binding := func(schema string, j asset.JSON) ev.Binding {
		return ev.Binding{ProjectID: corr.Project, RunID: corr.EvaluationRun, AttemptID: run, RequestID: corr.Request, Ref: "public:" + schema, Schema: schema, Availability: "AVAILABLE", Body: j, Digest: j.Digest()}
	}
	artifact := binding("artifact.v1", diagnosisJSON(rawReport))
	artifact.Ref = "final-answer://" + run
	// 显式再生公开跨包 transport 向量；不含真实凭据、正式Run或Holdout。
	if path := os.Getenv("CI_PAIR_PUBLIC_VECTOR_OUT"); path != "" && mutation == "" {
		q := ev.Request{RunID: corr.EvaluationRun, AttemptID: run, ID: corr.Request, Target: target, Case: ev.CaseInput{Identity: ev.AssetIdentity{Ref: asset.Ref{EntityID: corr.Case, Version: corr.CaseVersion}, ProjectID: corr.Project, ContentDigest: corr.CaseDigest}, Case: c}}
		vector := diagnosisJSON(map[string]any{"request": q, "scope": ev.Scope{Scope: asset.Scope{ProjectID: corr.Project, OrganizationID: asset.NewID(), Principal: "public-operator"}}, "plan_digest": source.PlanDigest, "wire": map[string]any{"contract_version": CIPairProtocol, "quality": qualityResponse, "sidecar": side, "sidecar_digest": side.Digest()}})
		if e := os.WriteFile(path, vector.Bytes(), 0600); e != nil {
			t.Fatal(e)
		}
	}
	return worker.EvaluationInput{Scope: ev.Scope{Scope: asset.Scope{ProjectID: corr.Project, OrganizationID: asset.NewID(), Principal: "public-operator"}}, Run: ev.Run{ID: corr.EvaluationRun, Snapshot: ev.RunSnapshot{Target: target}}, Attempt: ev.Attempt{ID: run, RunID: corr.EvaluationRun, RequestID: corr.Request, Outcome: ev.Success, Request: ev.Request{RunID: corr.EvaluationRun, AttemptID: run, ID: corr.Request, Target: target, Case: ev.CaseInput{Identity: ev.AssetIdentity{Ref: asset.Ref{EntityID: corr.Case, Version: corr.CaseVersion}, ProjectID: corr.Project, ContentDigest: corr.CaseDigest}, Case: c}}, Metadata: ev.AttemptMetadata{Observation: ev.Outcome{Kind: ev.Success, Artifact: &artifact, Evidence: []ev.Binding{binding(QualitySchema, bundleJSON), binding(CIPairSidecarVersion, side)}, Cleanup: diagnosisJSON(map[string]string{"plan_digest": source.PlanDigest})}}}, Work: ev.Work{SpecDigest: diagnosisHash("spec"), Metadata: ev.WorkMetadata{Spec: ev.EvaluatorSpec{Definition: definition}}}}
}
func TestCIDiagnosisV2IndependentSources(t *testing.T) {
	for _, tc := range []struct{ name, want string }{{"", "PASS"}, {"candidate", "PASS"}, {"wrong-label", "FAIL"}, {"report-foreign", "FAIL"}, {"foreign-source", "INCONCLUSIVE"}, {"no-action", "INCONCLUSIVE"}, {"unconsumed", "INCONCLUSIVE"}, {"missing-raw", "INCONCLUSIVE"}, {"tampered-raw", "INCONCLUSIVE"}, {"duplicate", "INCONCLUSIVE"}, {"unknown-source", "INCONCLUSIVE"}, {"mixed-target", "INCONCLUSIVE"}, {"mixed-gt", "INCONCLUSIVE"}, {"mixed-protocol", "INCONCLUSIVE"}, {"wrong-semantics", "INCONCLUSIVE"}} {
		t.Run(tc.name, func(t *testing.T) {
			i := 0
			if tc.name == "candidate" {
				i = 1
			}
			in := pairDiagnosisInput(t, i, tc.name)
			if !DeterministicSupported(in.Work.Metadata.Spec.Definition) {
				t.Fatal("v2 registration")
			}
			out, e := (DeterministicEvaluator{}).Evaluate(context.Background(), in)
			if e != nil || out.Value.Verdict != tc.want {
				t.Fatalf("%s got %s reason=%s err=%v", tc.name, out.Value.Verdict, out.Value.Reason, e)
			}
			if tc.want == "INCONCLUSIVE" && (out.Value.Score != nil || out.Value.SourceError != "MISSING_EVIDENCE") {
				t.Fatal("missing source manufactured success")
			}
		})
	}
}
func TestCISourceSemanticCrossLanguage(t *testing.T) {
	v := readPublicSources(t)
	for _, b := range v.Branches {
		got, e := CISourceSemanticDigest(b.Kind, b.Content, b.Execution)
		if e != nil || got != b.Digest {
			t.Fatal("limited Producer branch", b.Kind, e)
		}
	}
	for _, sample := range v.Samples {
		for _, entry := range sample.Source.Manifest {
			if entry.Kind == "CI_SUMMARY" {
				continue
			}
			got, e := CISourceSemanticDigest(entry.Kind, entry.Content, entry.Execution)
			if e != nil {
				t.Fatal(entry.Kind, e)
			}
			if got != v.Semantics[entry.Kind] {
				t.Fatal("five-kind Python semantic digest differs", entry.Kind)
			}
			var gt catalog.CIDiagnosisGroundTruthV2
			_ = v.GT.Decode(&gt)
			for _, req := range gt.Sources {
				if req.Kind == entry.Kind && got != req.Expected {
					t.Fatal("semantic differs from independent Python material")
				}
			}
			if entry.Content.Digest() != *entry.Digest {
				t.Fatal("original Owner content digest")
			}
		}
		if ciHash(sample.Source) != sample.Digest {
			t.Fatal("binding digest")
		}
		for _, tool := range sample.Source.Tools {
			if ciTextHash(tool.ResultBytes) != tool.Result {
				t.Fatal("original Tool canonical bytes")
			}
			var raw map[string]any
			if json.Unmarshal([]byte(tool.ResultBytes), &raw) != nil {
				t.Fatal("Owner bytes")
			}
		}
	}
}
