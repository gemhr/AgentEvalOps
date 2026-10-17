package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"encoding/json"
	"strings"
)

// 旧组件保持各自字段与适用版本；这里只增加新 Bundle 的消费者校验。
func qualityKeys(v any, fields string) bool {
	m, ok := v.(map[string]any)
	if !ok {
		return false
	}
	keys := strings.Fields(fields)
	if len(keys) != len(m) {
		return false
	}
	for _, k := range keys {
		if _, ok := m[k]; !ok {
			return false
		}
	}
	return true
}
func qualityArray(v any, fields string) bool {
	rows, ok := v.([]any)
	if !ok {
		return false
	}
	for _, row := range rows {
		if !qualityKeys(row, fields) {
			return false
		}
	}
	return true
}
func qualityValues(v any, stringsList, integers, booleans string) bool {
	m, ok := v.(map[string]any)
	if !ok {
		return false
	}
	for kind, list := range []string{stringsList, integers, booleans} {
		for _, key := range strings.Fields(list) {
			nullable := strings.HasSuffix(key, "?")
			key = strings.TrimSuffix(key, "?")
			value, exists := m[key]
			if !exists {
				return false
			}
			if value == nil && nullable {
				continue
			}
			switch kind {
			case 0:
				text, ok := value.(string)
				if !ok || len(text) > 512 {
					return false
				}
			case 1:
				if _, ok := value.(float64); !ok {
					return false
				}
			case 2:
				if _, ok := value.(bool); !ok {
					return false
				}
			}
		}
	}
	return true
}
func validateQualityComponent(c QualityComponent, b QualityBundle) error {
	var body map[string]any
	if json.Unmarshal(c.Body.Bytes(), &body) != nil {
		return asset.ErrInvalid
	}
	switch c.Kind {
	case "loop":
		return nil // DecodeLoopEvidence 已严格验证完整类型。
	case "execution":
		var e AgentExecution
		if c.Schema != "agent-execution-evidence.v1" || b.Policy != "durable-agent-loop.v2" || StrictQualityShape(c.Body.Bytes(), &e) != nil {
			return asset.ErrInvalid
		}
	case "context":
		if c.Schema != "context-evidence.v1" || b.Policy != "durable-agent-loop.v1" ||
			!qualityKeys(body, "schema_version policy_version validator_version algorithm_version run_id frame_id step_id snapshots models operations terminal coverage gaps sealed") ||
			body["schema_version"] != "context-evidence.v1" || body["policy_version"] != float64(1) || body["validator_version"] != "context-validator.v1" ||
			body["algorithm_version"] != "context-compaction.v1" || body["run_id"] != b.Correlation.Attempt ||
			!qualityKeys(body["terminal"], "status stop_reason sequence") ||
			!qualityArray(body["snapshots"], "snapshot_id version digest parent_id parent_digest cursor source_digest mandatory_digest mandatory_checks kind validator checks semantic_preservation created_generation accepted_generation fence") ||
			!qualityArray(body["models"], "attempt model_attempt purpose snapshot_id snapshot_digest operation_id request_digest profile_digest mode input_bound counting_strategy_digest assessment_digest output_reserve framing_bound safety_margin window required_window admissible state usage_source reported_usage usage") ||
			!qualityArray(body["operations"], "operation_id parent_id source_digest mandatory_digest prepared_request_digest created_generation target_iteration cursor state attempt_count candidate_digest validation_digest result_digest reason accepted_snapshot_id") {
			return asset.ErrInvalid
		}
		for _, raw := range body["snapshots"].([]any) {
			s := raw.(map[string]any)
			if !qualityKeys(s["checks"], "source mandatory roles pairs budget") || !qualityArray(s["mandatory_checks"], "item_digest preserved") {
				return asset.ErrInvalid
			}
			if !qualityValues(s, "snapshot_id digest parent_id? parent_digest? source_digest mandatory_digest kind validator semantic_preservation", "version cursor created_generation accepted_generation fence", "") || !qualityValues(s["checks"], "", "", "source mandatory roles pairs budget") {
				return asset.ErrInvalid
			}
			for _, check := range s["mandatory_checks"].([]any) {
				if !qualityValues(check, "item_digest", "", "preserved") {
					return asset.ErrInvalid
				}
			}
		}
		if !qualityValues(body, "schema_version validator_version algorithm_version run_id frame_id step_id coverage", "policy_version", "sealed") || !qualityValues(body["terminal"], "status stop_reason", "sequence?", "") {
			return asset.ErrInvalid
		}
		for _, row := range body["models"].([]any) {
			if !qualityValues(row, "purpose snapshot_id? snapshot_digest? operation_id? request_digest profile_digest mode? counting_strategy_digest assessment_digest state usage_source?", "attempt model_attempt input_bound? output_reserve? framing_bound? safety_margin? window? required_window?", "admissible?") {
				return asset.ErrInvalid
			}
		}
		for _, row := range body["operations"].([]any) {
			if !qualityValues(row, "operation_id parent_id? source_digest mandatory_digest prepared_request_digest state candidate_digest? validation_digest? result_digest? reason? accepted_snapshot_id?", "created_generation target_iteration cursor attempt_count", "") {
				return asset.ErrInvalid
			}
		}
	case "memory":
		if c.Schema != "memory-lifecycle-evidence.v1" || b.Policy != "durable-agent-loop.v1" ||
			!qualityKeys(body, "schema policy_version run_ref mode status reason manifest_digest seed_digest earliest_expiry snapshot_refs fence binding_version scopes records operations admissions coverage gaps sealed eligibility_at_seal model_invocations") ||
			body["schema"] != "memory-lifecycle-evidence.v1" || body["policy_version"] != float64(1) ||
			!qualityArray(body["snapshot_refs"], "snapshot_ref digest") ||
			!qualityArray(body["scopes"], "scope_ref kind epoch status bound_epoch authorized_action outcome") ||
			!qualityArray(body["records"], "record_ref scope_ref record_version epistemic_kind lifecycle_status body_state ttl source_ref dependency_digest lifecycle_eligible_at_seal") ||
			!qualityArray(body["operations"], "operation_ref scope_ref kind state source_ref request_digest candidate_digest expected_version result_version dependency_digest outcome source_mode direct_source_digest user_message_digest deletion_scope all_copies_erased historical_payloads backup_erasure") ||
			!qualityArray(body["admissions"], "identity_digest fence manifest_digest outcome state result_digest") ||
			!qualityArray(body["model_invocations"], "attempt_ref purpose state request_digest formation_operation_ref") {
			return asset.ErrInvalid
		}
		if !qualityValues(body, "schema run_ref mode status reason? manifest_digest seed_digest? earliest_expiry? coverage eligibility_at_seal", "policy_version fence binding_version", "sealed") {
			return asset.ErrInvalid
		}
		for _, row := range body["snapshot_refs"].([]any) {
			if !qualityValues(row, "snapshot_ref digest", "", "") {
				return asset.ErrInvalid
			}
		}
		for _, row := range body["scopes"].([]any) {
			if !qualityValues(row, "scope_ref kind status authorized_action outcome", "epoch bound_epoch?", "") {
				return asset.ErrInvalid
			}
		}
		for _, row := range body["records"].([]any) {
			if !qualityValues(row, "record_ref scope_ref epistemic_kind lifecycle_status body_state ttl source_ref dependency_digest", "record_version", "lifecycle_eligible_at_seal") {
				return asset.ErrInvalid
			}
		}
		for _, row := range body["operations"].([]any) {
			if !qualityValues(row, "operation_ref scope_ref kind state source_ref request_digest candidate_digest? dependency_digest outcome source_mode direct_source_digest? user_message_digest? deletion_scope? historical_payloads? backup_erasure?", "expected_version? result_version?", "all_copies_erased?") {
				return asset.ErrInvalid
			}
		}
		for _, row := range body["admissions"].([]any) {
			if !qualityValues(row, "identity_digest manifest_digest outcome state? result_digest?", "fence", "") {
				return asset.ErrInvalid
			}
		}
		for _, row := range body["model_invocations"].([]any) {
			if !qualityValues(row, "attempt_ref purpose state request_digest formation_operation_ref?", "", "") {
				return asset.ErrInvalid
			}
		}
	default:
		return asset.ErrInvalid
	}
	return nil
}
