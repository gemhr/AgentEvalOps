package asset

import "fmt"

// LegacyDigest 只重现已冻结的 Python 字段投影。输入必须保留原 JSON number 类型；
// 不可把旧 JSONB 重新编码后冒充可验证的原始素材。
func LegacyCaseDigest(snapshot JSON) (JSON, error) {
	return legacyProjection(snapshot, []string{"input_payload", "expected_output", "assertion_specs", "fixture_refs", "evidence_refs", "metadata"})
}
func LegacyEvaluatorDefinitionDigest(snapshot JSON) (JSON, error) {
	return legacyProjection(snapshot, []string{"evaluator_id", "evaluator_version", "evaluator_kind", "config_ref", "config_snapshot", "threshold", "score_direction", "score_range", "comparison_tolerance", "prompt_ref", "required", "result_schema_ref", "comparison_semantics", "required_artifact_kinds", "required_evidence_kinds"})
}
func legacyProjection(snapshot JSON, fields []string) (JSON, error) {
	var all map[string]JSON
	if err := snapshot.Decode(&all); err != nil {
		return JSON{}, err
	}
	projection := map[string]JSON{}
	for _, key := range fields {
		value, ok := all[key]
		if !ok {
			return JSON{}, fmt.Errorf("legacy snapshot 缺少 %s", key)
		}
		projection[key] = value
	}
	return Freeze(projection)
}
