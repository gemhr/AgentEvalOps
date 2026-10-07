package provider

import (
	"agentevalops/go-backend/internal/asset"
	"testing"
)

func TestStage13ModelPolicy(t *testing.T) {
	for _, mode := range []string{"normal", "missing_model", "provider", "profile", "model", "fingerprint", "upgrade", "undeclared_upgrade", "input", "endpoint", "exact"} {
		t.Run(mode, func(t *testing.T) {
			makeInput := func() Stage13PolicyInput {
				manifest := stage13UnitManifest(t)
				wire := stage13UnitWire(manifest, asset.NewID(), `{"input":"same"}`)
				receipt := wire["actual_subject_receipt"].(map[string]any)
				call := receipt["model_call_receipts"].([]any)[0].(map[string]any)
				call["reported_provider"], call["reported_revision"], call["actual_revision"] = nil, nil, nil
				call["system_fingerprint"] = "A"
				call["verification_status"], receipt["model_identity_verification"] = "PROVIDER_MODEL_MATCH", "PROVIDER_MODEL_MATCH"
				sealReceipt(receipt)
				j, _ := asset.Freeze(receipt)
				return Stage13PolicyInput{Manifest: manifest, Receipt: j}
			}
			doc := Stage13PolicyDocument{Mode: "AGENT_REGRESSION", Baseline: makeInput(), Candidate: makeInput()}
			var body map[string]any
			_ = doc.Candidate.Receipt.Decode(&body)
			call := body["model_call_receipts"].([]any)[0].(map[string]any)
			wantLevel, wantComparable := "PROVIDER_MODEL_MATCH", true
			switch mode {
			case "missing_model":
				call["reported_model"] = nil
				wantLevel, wantComparable = "MODEL_IDENTITY_UNKNOWN", false
			case "provider":
				call["resolved_provider"] = "minimax"
				wantLevel, wantComparable = "MODEL_IDENTITY_MISMATCH", false
			case "profile":
				call["resolved_model_config"].(map[string]any)["temperature"] = 0.7
				wantLevel, wantComparable = "MODEL_IDENTITY_MISMATCH", false
			case "model":
				call["reported_model"] = "glm-5.3"
				wantLevel, wantComparable = "MODEL_IDENTITY_MISMATCH", false
			case "endpoint":
				call["resolved_endpoint"] = "https://unexpected.test"
				wantLevel, wantComparable = "MODEL_IDENTITY_MISMATCH", false
			case "input":
				body["actual_input_digest"] = "different"
				wantComparable = false
			case "fingerprint":
				call["system_fingerprint"] = "B"
			case "exact":
				call["verification_status"] = "EXACT_DEPLOYMENT_VERIFIED"
				call["reported_revision"], call["actual_revision"] = "unit-revision", "unit-revision"
				wantLevel = "EXACT_DEPLOYMENT_VERIFIED"
				call["reported_artifact_digest"], call["reported_deployment_id"] = "6666666666666666666666666666666666666666666666666666666666666666", "provider-deployment-1"
			case "upgrade", "undeclared_upgrade":
				doc.Mode = "MODEL_UPGRADE"
				wantComparable = mode == "upgrade"
				if wantComparable {
					doc.Intended = []string{"canonical_model", "model_profile"}
				}
				cfg := call["resolved_model_config"].(map[string]any)
				cfg["model"] = "glm-5.3"
				cj, _ := asset.Freeze(cfg)
				call["requested_model"], call["reported_model"] = "glm-5.3", "glm-5.3"
				call["resolved_profile_digest"], call["model_config_digest"] = cj.Digest(), cj.Digest()
				m := body["actual_subject_manifest"].(map[string]any)
				m["requested_model"], m["model_profile_digest"] = "glm-5.3", cj.Digest()
				delete(m, "subject_manifest_digest")
				mj, _ := asset.Freeze(m)
				m["subject_manifest_digest"] = mj.Digest()
				doc.Candidate.Manifest, _ = asset.Freeze(m)
			}
			sealReceipt(body)
			doc.Candidate.Receipt, _ = asset.Freeze(body)
			result := CompareStage13Models(doc)
			if result.Candidate.Level != wantLevel || result.Comparable != wantComparable {
				t.Fatalf("%+v", result)
			}
			if mode == "fingerprint" && (result.Comparability != "COMPARABLE_WITH_ENVIRONMENT_WARNING" || result.FingerprintStatus != "BACKEND_CONFIGURATION_CHANGED") {
				t.Fatalf("%+v", result)
			}
		})
	}
}
