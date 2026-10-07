package provider

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func stage13UnitManifest(t *testing.T) asset.JSON {
	t.Helper()
	m := map[string]any{"subject_id": "ci_triage_candidate", "subject_version": "wp04-candidate-1", "agent_id": "ci_triage_candidate", "agent_definition_version": "wp04-candidate-1",
		"agent_definition_digest": strings.Repeat("a", 64), "prompt_version": "stage13.triage-prompt.v1", "prompt_digest": strings.Repeat("b", 64),
		"model_profile_id": "remote_advanced", "model_profile_digest": strings.Repeat("c", 64), "requested_provider": "deepseek", "requested_model": "unit-model", "requested_revision": "unit-revision",
		"tool_profile_id": "stage13.read-only-evidence.v1", "tool_profile_digest": strings.Repeat("d", 64), "output_schema_version": "stage13.triage-output.v1", "output_schema_digest": "9fb6df7454482257b9358a887e0b5c41871a6e66db9b462f6e7f3c1810f0dca4", "execution_enabled": true}
	cfg, _ := asset.Freeze(map[string]any{"provider": "deepseek", "model": "unit-model", "revision": "unit-revision", "endpoint_digest": rawDigest("http://controlled-unit")})
	m["model_profile_digest"] = cfg.Digest()
	body, _ := asset.Freeze(m)
	m["subject_manifest_digest"] = body.Digest()
	result, _ := asset.Freeze(m)
	return result
}
func sealReceipt(m map[string]any) {
	delete(m, "receipt_digest")
	j, _ := asset.Freeze(m)
	m["receipt_digest"] = j.Digest()
}
func stage13UnitWire(m asset.JSON, runID, query string) map[string]any {
	input, _ := asset.ParseJSON([]byte(query))
	raw := `{"FailureCategory":"UNKNOWN","RootCauseCandidates":[],"EvidenceRefs":[],"RecommendedAction":{"ActionCode":"REQUEST_MORE_EVIDENCE"},"Confidence":0,"NeedMoreEvidence":true,"TicketDecision":"REQUEST_MORE_EVIDENCE"}`
	var output map[string]any
	_ = json.Unmarshal([]byte(raw), &output)
	call := map[string]any{"call_id": asset.NewID(), "run_id": runID, "role": "INITIAL", "resolved_profile_id": "remote_advanced", "resolved_profile_digest": strings.Repeat("c", 64), "model_config_digest": strings.Repeat("c", 64),
		"requested_provider": "deepseek", "requested_model": "unit-model", "requested_revision": "unit-revision", "reported_provider": "deepseek", "reported_model": "unit-model", "reported_revision": "unit-revision", "actual_revision": "unit-revision",
		"dispatch_certainty": "PROVIDER_RESPONDED", "verification_status": "VERIFIED_BY_PROVIDER_RESPONSE", "input_tokens": nil, "output_tokens": nil, "cost": nil, "state": "COMPLETED",
		"effective_messages_digest": strings.Repeat("e", 64), "effective_system_prompt_digest": strings.Repeat("f", 64)}
	var manifest triageManifest
	_ = m.Decode(&manifest)
	cfg := map[string]any{"provider": "deepseek", "model": "unit-model", "revision": manifest.Revision, "endpoint_digest": rawDigest("http://controlled-unit")}
	call["resolved_profile_digest"], call["model_config_digest"] = manifest.ModelDigest, manifest.ModelDigest
	call["resolved_provider"], call["resolved_provider_source"], call["resolved_endpoint"], call["resolved_model_config"] = "deepseek", "CONFIGURED_TRANSPORT", "http://controlled-unit", cfg
	receipt := map[string]any{"receipt_version": "stage13.actual-subject-receipt.v1", "run_id": runID, "anchor_run_id": runID, "analysis_job_id": nil, "evaluation_attempt_id": runID, "role": "INITIAL",
		"actual_subject_manifest": m, "actual_input_digest": input.Digest(), "effective_payload_digest": rawDigest(query), "prompt_template_digest": rawDigest("仅分析下面原始授权输入；输出一个严格七字段 JSON 对象。"), "resolved_toolset_identity": strings.Repeat("d", 64), "final_answer_digest": rawDigest(raw), "model_call_receipts": []any{call}, "model_identity_verification": "VERIFIED_BY_PROVIDER_RESPONSE"}
	sealReceipt(receipt)
	return map[string]any{"anchor_run_id": runID, "child_subject_receipts": []any{}, "triage_capture_status": "COMPLETE", "protocol_version": Stage13Protocol, "run_id": runID, "status": "SUCCEEDED", "stop_reason": "COMPLETED", "actual_subject_receipt": receipt, "child_runs": []any{}, "selected_final_run_id": runID,
		"business_output_validation": map[string]any{"status": "VALID", "schema_valid": true, "semantic_valid": true, "errors": []string{}, "output": output},
		"final_answer_evidence":      map[string]any{"schema_version": "stage13-final-answer.v1", "evidence_id": "final-answer://" + runID, "run_id": runID, "attempt_id": runID, "producer_run_id": runID, "content": raw, "content_sha256": rawDigest(raw)}}
}
func TestStage13ReceiptIdentityAndComparability(t *testing.T) {
	for _, mode := range []string{"valid", "baseline", "provider_model_match", "candidate_actual_baseline", "manifest", "tool", "schema", "missing_receipt", "profile", "actual_model", "echoed_revision", "run", "attempt", "producer", "answer_digest", "raw_answer", "protocol", "unknown_revision", "receipt_digest", "extra_child", "empty_revision", "alias_only", "requested_as_actual", "proof_model_mismatch", "proof_deployment_mismatch", "proof_digest_mismatch", "proof_outside_validity", "proof_wrong_call_binding"} {
		t.Run(mode, func(t *testing.T) {
			manifest := stage13UnitManifest(t)
			baselineManifest := func() asset.JSON {
				var mm map[string]asset.JSON
				_ = manifest.Decode(&mm)
				mm["agent_id"], _ = asset.Freeze("ci_triage_baseline")
				mm["subject_id"], _ = asset.Freeze("ci_triage_baseline")
				mm["agent_definition_version"], _ = asset.Freeze("wp04-baseline-1")
				mm["subject_version"], _ = asset.Freeze("wp04-baseline-1")
				delete(mm, "subject_manifest_digest")
				j, _ := asset.Freeze(mm)
				mm["subject_manifest_digest"], _ = asset.Freeze(j.Digest())
				j, _ = asset.Freeze(mm)
				return j
			}
			if mode == "baseline" {
				manifest = baselineManifest()
			}
			if mode == "empty_revision" {
				var mm map[string]asset.JSON
				_ = manifest.Decode(&mm)
				mm["requested_revision"], _ = asset.Freeze(nil)
				delete(mm, "subject_manifest_digest")
				j, _ := asset.Freeze(mm)
				mm["subject_manifest_digest"], _ = asset.Freeze(j.Digest())
				manifest, _ = asset.Freeze(mm)
			}
			runID, project := asset.NewID(), asset.NewID()
			query := `{"schema_version":"stage13.triage-input.v1"}`
			wire := stage13UnitWire(manifest, runID, query)
			receipt := wire["actual_subject_receipt"].(map[string]any)
			call := receipt["model_call_receipts"].([]any)[0].(map[string]any)
			answer := wire["final_answer_evidence"].(map[string]any)
			switch mode {
			case "provider_model_match":
				call["reported_provider"], call["reported_revision"], call["actual_revision"] = nil, nil, nil
				call["verification_status"], receipt["model_identity_verification"] = "PROVIDER_MODEL_MATCH", "PROVIDER_MODEL_MATCH"
			case "candidate_actual_baseline":
				receipt["actual_subject_manifest"] = baselineManifest()
			case "tool", "schema":
				var mm map[string]asset.JSON
				_ = manifest.Decode(&mm)
				field := "tool_profile_digest"
				if mode == "schema" {
					field = "output_schema_digest"
				}
				mm[field], _ = asset.Freeze(strings.Repeat("0", 64))
				delete(mm, "subject_manifest_digest")
				j, _ := asset.Freeze(mm)
				mm["subject_manifest_digest"], _ = asset.Freeze(j.Digest())
				receipt["actual_subject_manifest"], _ = asset.Freeze(mm)
			case "manifest":
				receipt["actual_subject_manifest"] = stage13UnitManifest(t)
				var mm map[string]asset.JSON
				_ = manifest.Decode(&mm)
				mm["prompt_digest"], _ = asset.Freeze(strings.Repeat("0", 64))
				receipt["actual_subject_manifest"], _ = asset.Freeze(mm)
			case "profile":
				call["resolved_profile_digest"] = strings.Repeat("0", 64)
			case "actual_model":
				call["reported_model"] = "different"
			case "echoed_revision":
				call["reported_revision"] = nil
			case "requested_as_actual":
				call["reported_model"] = call["requested_model"]
				call["reported_revision"] = nil
				call["actual_revision"] = call["requested_revision"]
			case "empty_revision":
				call["requested_revision"] = nil
				call["reported_revision"], call["actual_revision"] = "", ""
			case "alias_only":
				call["reported_model"] = "provider-canonical-alias"
				call["reported_revision"], call["actual_revision"] = nil, nil
			case "proof_model_mismatch", "proof_deployment_mismatch", "proof_digest_mismatch", "proof_outside_validity", "proof_wrong_call_binding":
				// 本轮没有受控部署 proof 合同；拒绝未知 proof，不能信任自报 VERIFIED。
				call["verification_status"] = "VERIFIED_BY_CONTROLLED_DEPLOYMENT_PROOF"
				receipt["model_identity_verification"] = call["verification_status"]
				call["controlled_deployment_proof"] = map[string]any{"model": "wrong-model", "deployment": "wrong-deployment", "proof_digest": "invalid", "valid_until": "2000-01-01T00:00:00Z", "call_id": asset.NewID(), "negative_case": mode}
			case "run":
				receipt["run_id"] = asset.NewID()
			case "attempt":
				receipt["evaluation_attempt_id"] = asset.NewID()
			case "producer":
				answer["producer_run_id"] = asset.NewID()
			case "answer_digest":
				answer["content_sha256"] = strings.Repeat("0", 64)
			case "raw_answer":
				answer["content"] = "tampered"
			case "protocol":
				wire["protocol_version"] = LocalAgentProtocol
			case "unknown_revision":
				call["reported_revision"] = nil
				call["actual_revision"] = nil
				call["verification_status"] = "UNKNOWN"
				receipt["model_identity_verification"] = "UNKNOWN"
			case "extra_child":
				wire["child_runs"] = []any{map[string]any{"run_id": asset.NewID(), "role": "SCHEMA_REPAIR", "status": "SUCCEEDED", "stop_reason": "COMPLETED", "actual_subject_receipt": receipt}}
			}
			sealReceipt(receipt)
			if mode == "missing_receipt" {
				wire["actual_subject_receipt"] = nil
			}
			if mode == "receipt_digest" {
				receipt["receipt_digest"] = strings.Repeat("0", 64)
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/api/runtime/evaluation-execute/stage13/v1" {
					t.Error("wrong route")
				}
				var body map[string]any
				_ = json.NewDecoder(r.Body).Decode(&body)
				if body["run_id"] != runID {
					t.Error("anchor must be AttemptID")
				}
				_ = json.NewEncoder(w).Encode(wire)
			}))
			defer server.Close()
			t.Setenv("STAGE13_UNIT_TOKEN", "unit")
			c := Stage13Config{Transport: DefaultLocalAgentConfig(server.URL, "STAGE13_UNIT_TOKEN"), ExpectedSubjectManifest: manifest}
			target, err := NewStage13Target(c)
			if err != nil {
				t.Fatal(err)
			}
			defer target.Close()
			config, _ := asset.Freeze(c)
			agent := "ci_triage_candidate"
			if mode == "baseline" {
				agent = "ci_triage_baseline"
			}
			input, _ := asset.Freeze(map[string]any{"agent_id": agent, "query": query})
			q := ev.Request{ID: asset.NewID(), RunID: asset.NewID(), AttemptID: runID, Target: ev.Target{ID: Stage13TargetID, Version: Stage13TargetVersion, Kind: "LOCALAGENT_HTTP", Config: config, TimeoutMilliseconds: 1000}, Case: ev.CaseInput{Identity: ev.AssetIdentity{ProjectID: project}}}
			q.Case.Case.Input = input
			out, err := target.Execute(context.Background(), ev.Scope{Scope: asset.Scope{ProjectID: project}}, q)
			if err != nil {
				t.Fatal(err)
			}
			if mode == "valid" || mode == "baseline" || mode == "unknown_revision" || mode == "provider_model_match" {
				if out.Kind != ev.Success || out.Artifact == nil {
					t.Fatalf("%+v", out)
				}
			} else if out.Kind == ev.Success {
				t.Fatalf("accepted %s", mode)
			}
		})
	}
}
