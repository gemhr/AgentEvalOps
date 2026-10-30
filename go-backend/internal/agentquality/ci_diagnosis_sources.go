package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"slices"
	"sort"
	"time"
)

const CISourceBindingVersion = "ci-runtime-source-binding.v1"
const CIPairProtocol = "ci-synthetic-evaluation-execute.v2"
const CIPairTargetVersion = "ci-synthetic-target.v3"
const CIPairSidecarVersion = "ci-synthetic-evidence.v2"
const CIPairQuery = "请调查本次冻结的 CI 执行，使用授权工具取证。"

type CISourceEvidence struct {
	ID           string     `json:"evidence_id"`
	Execution    string     `json:"execution_id"`
	Kind         string     `json:"kind"`
	Captured     string     `json:"captured_at"`
	Availability string     `json:"availability"`
	Media        string     `json:"media_type"`
	Digest       *string    `json:"digest"`
	Content      asset.JSON `json:"content"`
	Missing      *string    `json:"missing_reason"`
}
type CISourceTool struct {
	Invocation      string     `json:"invocation_ref"`
	Action          string     `json:"action_ref"`
	Frame           string     `json:"frame_ref"`
	Iteration       int        `json:"iteration"`
	Ordinal         int        `json:"ordinal"`
	Call            string     `json:"call_ref"`
	Name            string     `json:"tool_name"`
	Arguments       asset.JSON `json:"arguments"`
	ArgumentsDigest string     `json:"arguments_digest"`
	ResultBytes     string     `json:"result_canonical_bytes"`
	Result          string     `json:"result_digest"`
}
type CISourceBinding struct {
	Schema         string             `json:"schema_version"`
	Semantics      string             `json:"source_semantics_version"`
	Correlation    QualityCorrelation `json:"quality_correlation"`
	Plan           string             `json:"plan_id"`
	PlanVersion    string             `json:"plan_version"`
	PlanDigest     string             `json:"plan_digest"`
	Mode           string             `json:"mode"`
	InputDigest    string             `json:"input_digest"`
	StateDigest    string             `json:"state_digest"`
	Source         string             `json:"source_ref"`
	Revision       int                `json:"state_revision"`
	Snapshot       asset.JSON         `json:"execution_snapshot"`
	Summary        asset.JSON         `json:"summary"`
	Manifest       []CISourceEvidence `json:"evidence_manifest"`
	ManifestDigest string             `json:"evidence_digest"`
	Tools          []CISourceTool     `json:"tools"`
}

func ciHash(v any) string {
	j, e := asset.Freeze(v)
	if e != nil {
		return ""
	}
	return j.Digest()
}
func ciTextHash(s string) string { return fmt.Sprintf("%x", sha256.Sum256([]byte(s))) }
func ciEvidenceID(execution, kind string) string {
	ns, _ := hex.DecodeString("6ba7b8119dad11d180b400c04fd430c8")
	sum := sha1.Sum(append(ns, []byte(execution+":"+kind)...))
	b := sum[:16]
	b[6] = (b[6] & 15) | 80
	b[8] = (b[8] & 63) | 128
	s := hex.EncodeToString(b)
	return s[:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:]
}
func sourceFields(j asset.JSON, fields string) (map[string]any, bool) {
	var v map[string]any
	if json.Unmarshal(j.Bytes(), &v) != nil || !qualityKeys(v, fields) {
		return nil, false
	}
	return v, true
}

// 精确已知结构；语义摘要保存所有非动态字段，包括null/错误/版本和嵌套同名字段。
func CISourceSemanticDigest(kind string, content asset.JSON, execution string) (string, error) {
	if ValidateQualityJSON(content, 16384) != nil {
		return "", asset.ErrInvalid
	}
	fields := "execution_id artifact_id call_id "
	switch kind {
	case "PARSER_LOG":
		fields += "field_revision parser_supported_field_revision decoder_supported_field_revision parser_version decoder_table_version status skipped_reason error parsed_fields"
	case "ENVIRONMENT_RECORD":
		fields += "environment_id ip software_version parser_version decoder_table_version parser_supported_field_revision decoder_supported_field_revision feature_flags buffer_behavior"
	case "CASE_REQUIREMENTS":
		fields += "case_id feature_id required_field_revision input_revision golden_id golden_version comparison_rule_id"
	case "COMPARISON_LOG":
		fields += "caller callee golden_comparison_performed outcome error skipped_reason"
	case "AUTOMATION_LOG":
		fields += "caller callee outcome stages container_type product_release"
	default:
		return "", asset.ErrInvalid
	}
	var m map[string]asset.JSON
	if content.Decode(&m) != nil {
		return "", asset.ErrInvalid
	}
	if kind == "ENVIRONMENT_RECORD" {
		if _, ok := m["missing_reason"]; ok {
			fields += " missing_reason"
		}
	}
	if kind == "CASE_REQUIREMENTS" {
		for _, k := range []string{"compressed_transport", "input_contract", "buffer_requirement"} {
			if _, ok := m[k]; ok {
				fields += " " + k
			}
		}
	}
	if kind == "COMPARISON_LOG" {
		if _, ok := m["detail"]; ok {
			fields = "execution_id artifact_id call_id caller outcome detail"
		}
	}
	value, ok := sourceFields(content, fields)
	if !ok {
		return "", asset.ErrInvalid
	}
	for _, key := range []string{"field_revision", "parser_supported_field_revision", "decoder_supported_field_revision", "required_field_revision", "input_revision"} {
		if v, exists := value[key]; exists && v != nil {
			n, numeric := v.(float64)
			if !numeric || math.Trunc(n) != n || n < 1 || n > 16 {
				return "", asset.ErrInvalid
			}
		}
	}
	for k, want := range map[string]string{"execution_id": execution, "artifact_id": execution + ":report", "call_id": execution + ":compare"} {
		var actual string
		if m[k].Decode(&actual) != nil || actual != want {
			return "", asset.ErrInvalid
		}
		delete(m, k)
	}
	// 固定字段类型/分支；不接受表达式、递归投影或未知嵌套字段。
	switch kind {
	case "PARSER_LOG":
		if !qualityValues(value, "execution_id artifact_id call_id parser_version decoder_table_version status skipped_reason? error?", "field_revision parser_supported_field_revision decoder_supported_field_revision", "") || !oneOf(value["status"].(string), "EXECUTED", "SKIPPED") {
			return "", asset.ErrInvalid
		}
		if value["parsed_fields"] != nil && (!qualityKeys(value["parsed_fields"], "report") || value["parsed_fields"].(map[string]any)["report"] != "decoded") {
			return "", asset.ErrInvalid
		}
	case "ENVIRONMENT_RECORD":
		optional := ""
		if _, ok := value["missing_reason"]; ok {
			optional = " missing_reason"
		}
		if !qualityValues(value, "execution_id artifact_id call_id environment_id ip software_version parser_version? decoder_table_version? buffer_behavior"+optional, "parser_supported_field_revision? decoder_supported_field_revision?", "") {
			return "", asset.ErrInvalid
		}
		flags, ok := value["feature_flags"].(map[string]any)
		if !ok || len(flags) > 1 {
			return "", asset.ErrInvalid
		}
		for key, v := range flags {
			if _, ok := v.(bool); key != "compressed_transport" || !ok {
				return "", asset.ErrInvalid
			}
		}
	case "CASE_REQUIREMENTS":
		optional := ""
		for _, k := range []string{"input_contract", "buffer_requirement"} {
			if _, ok := value[k]; ok {
				optional += " " + k
			}
		}
		bools := ""
		if _, ok := value["compressed_transport"]; ok {
			bools = "compressed_transport"
		}
		if !qualityValues(value, "execution_id artifact_id call_id case_id feature_id golden_id golden_version comparison_rule_id"+optional, "required_field_revision input_revision", bools) {
			return "", asset.ErrInvalid
		}
	case "COMPARISON_LOG":
		if value["caller"] != "COMPARE" || !oneOf(fmt.Sprint(value["outcome"]), "PASS", "FAIL") {
			return "", asset.ErrInvalid
		}
		if _, ok := value["detail"]; ok {
			if !qualityValues(value, "execution_id artifact_id call_id caller outcome detail", "", "") || value["detail"] != "比较阶段未获得可用结果；调用详细记录未采集" {
				return "", asset.ErrInvalid
			}
		} else if !qualityValues(value, "execution_id artifact_id call_id caller callee outcome error? skipped_reason?", "", "golden_comparison_performed") || value["callee"] != "PARSER" {
			return "", asset.ErrInvalid
		}
	case "AUTOMATION_LOG":
		if !qualityValues(value, "execution_id artifact_id call_id caller callee outcome container_type", "", "") || !qualityArray(value["stages"], "stage status error") || !qualityKeys(value["product_release"], "required_tlv_revision buffer_behavior") || !qualityValues(value["product_release"], "buffer_behavior", "required_tlv_revision", "") {
			return "", asset.ErrInvalid
		}
		stages := value["stages"].([]any)
		if len(stages) != 4 || value["caller"] != "AUTOMATION" || value["callee"] != "COMPARE" || !oneOf(fmt.Sprint(value["outcome"]), "PASS", "FAIL") || value["container_type"] != "simulated-compressed-report" {
			return "", asset.ErrInvalid
		}
		for i, stage := range stages {
			if !qualityValues(stage, "stage status error?", "", "") || stage.(map[string]any)["stage"] != []string{"PRECHECK", "PRODUCT", "PARSER", "COMPARISON"}[i] || !oneOf(fmt.Sprint(stage.(map[string]any)["status"]), "EXECUTED", "SKIPPED") {
				return "", asset.ErrInvalid
			}
		}
		release := value["product_release"].(map[string]any)
		if release["required_tlv_revision"] != float64(2) || !oneOf(fmt.Sprint(release["buffer_behavior"]), "compatible", "compact", "legacy") {
			return "", asset.ErrInvalid
		}
	}
	j, e := asset.Freeze(m)
	if e != nil {
		return "", e
	}
	return j.Digest(), nil
}

type ciRawToolResult struct {
	Invocation string `json:"invocation_id"`
	Attempt    string `json:"attempt_id"`
	Name       string `json:"tool_name"`
	Status     string `json:"status"`
	Output     struct {
		Type      string `json:"content_type"`
		Original  int    `json:"original_size_bytes"`
		Returned  int    `json:"returned_size_bytes"`
		Truncated bool   `json:"truncated"`
		Digest    string `json:"digest"`
		Content   string `json:"content"`
	} `json:"output"`
	Summary    string     `json:"safe_summary"`
	Effects    string     `json:"side_effect_state"`
	Replayed   bool       `json:"idempotency_replayed"`
	Retry      string     `json:"retry_disposition"`
	Resource   *string    `json:"resource_key_digest"`
	Started    string     `json:"started_at"`
	Completed  string     `json:"completed_at"`
	Duration   asset.JSON `json:"duration_ms"`
	RetryIndex int        `json:"retry_index"`
	Terminated bool       `json:"worker_terminated"`
	Detached   bool       `json:"execution_detached"`
	Pending    bool       `json:"resource_release_pending"`
}
type ciEvidencePayload struct {
	ID           string     `json:"evidence_id"`
	Kind         string     `json:"kind"`
	Availability string     `json:"availability"`
	Media        string     `json:"media_type"`
	Digest       *string    `json:"digest"`
	Incident     string     `json:"incident_id"`
	Execution    string     `json:"execution_id"`
	Captured     string     `json:"captured_at"`
	Content      asset.JSON `json:"content"`
	Missing      *string    `json:"missing_reason"`
}

func VerifyCISourceBinding(raw asset.JSON, e CIDiagnosisEvidence, f catalog.CIPairFrozen, execution, incident, mode string) (map[string]catalog.CIDiagnosisSource, error) {
	invalid := func() (map[string]catalog.CIDiagnosisSource, error) { return nil, asset.ErrInvalid }
	var b CISourceBinding
	if len(raw.Bytes()) > 768*1024 || StrictQualityShape(raw.Bytes(), &b) != nil || b.Schema != CISourceBindingVersion || b.Semantics != catalog.CISourceSemanticsVersion || !reflect.DeepEqual(b.Correlation, e.Bundle.Correlation) || b.Plan != f.PlanID || b.PlanVersion != f.PlanVersion || b.PlanDigest != e.PlanDigest || b.InputDigest != f.InputDigest || b.StateDigest != f.Input.StateDigest || b.Source != f.Source || b.Revision != 1 || b.Mode != mode || !oneOf(b.Mode, "BASELINE", "CANDIDATE") || len(b.Manifest) != 6 || len(b.Tools) < 1 || len(b.Tools) > 8 {
		return invalid()
	}
	state := f.Input.State
	state.Report.ID = execution + ":report"
	if b.Snapshot.String() != func() string { j, _ := asset.Freeze(state); return j.String() }() {
		return invalid()
	}
	var summary struct {
		Execution   string `json:"execution_id"`
		Environment string `json:"environment_id"`
		Case        string `json:"case_id"`
		Outcome     string `json:"outcome"`
		Completed   string `json:"completed_at"`
		Stages      []struct {
			Stage  string  `json:"stage"`
			Status string  `json:"status"`
			Error  *string `json:"error"`
		} `json:"stages"`
	}
	if StrictQualityShape(b.Summary.Bytes(), &summary) != nil || summary.Execution != execution || summary.Environment != state.Environment.ID || summary.Case != state.Case.ID || !oneOf(summary.Outcome, "PASS", "FAIL") {
		return invalid()
	}
	if _, err := time.Parse(time.RFC3339Nano, summary.Completed); err != nil {
		return invalid()
	}
	manifest := slices.Clone(b.Manifest)
	sort.Slice(manifest, func(i, j int) bool { return manifest[i].ID < manifest[j].ID })
	if ciHash(manifest) != b.ManifestDigest {
		return invalid()
	}
	ids, kinds := map[string]CISourceEvidence{}, map[string]CISourceEvidence{}
	for _, v := range b.Manifest {
		if _, ok := ids[v.ID]; ok {
			return invalid()
		}
		if _, ok := kinds[v.Kind]; ok {
			return invalid()
		}
		if !slices.Contains([]string{"CI_SUMMARY", "PARSER_LOG", "ENVIRONMENT_RECORD", "CASE_REQUIREMENTS", "COMPARISON_LOG", "AUTOMATION_LOG"}, v.Kind) || v.ID != ciEvidenceID(execution, v.Kind) || v.Execution != execution || v.Captured != summary.Completed || v.Media != "application/json" {
			return invalid()
		}
		if v.Availability == "AVAILABLE" {
			if v.Digest == nil || v.Content.String() == "null" || v.Content.Digest() != *v.Digest || v.Missing != nil {
				return invalid()
			}
			if v.Kind == "CI_SUMMARY" {
				if v.Content.String() != b.Summary.String() {
					return invalid()
				}
			} else if _, err := CISourceSemanticDigest(v.Kind, v.Content, execution); err != nil {
				return invalid()
			}
		} else if v.Availability != "MISSING" || v.Content.String() != "null" || v.Digest != nil || v.Missing == nil {
			return invalid()
		}
		ids[v.ID] = v
		kinds[v.Kind] = v
	}
	result := map[string]catalog.CIDiagnosisSource{}
	seenInv := map[string]bool{}
	for _, tool := range b.Tools {
		var value ciRawToolResult
		// 使用原始canonical字节哈希；从不重新序列化后冒充 Tool Owner 摘要。
		if len([]byte(tool.ResultBytes)) > 131072 || ciTextHash(tool.ResultBytes) != tool.Result || StrictQualityShape([]byte(tool.ResultBytes), &value) != nil || ciHash(value.Invocation) != tool.Invocation || seenInv[tool.Invocation] || value.Name != tool.Name || !oneOf(tool.Name, "ci_get_execution_context", "ci_read_execution_evidence") || value.Status != "SUCCEEDED" || value.Output.Truncated || value.Output.Type != "application/json" || len([]byte(value.Output.Content)) > 16384 || value.Output.Returned != len([]byte(value.Output.Content)) || value.Output.Original != value.Output.Returned || value.Output.Digest != ciTextHash(value.Output.Content) || !value.Terminated || value.Detached || value.Pending {
			return invalid()
		}
		seenInv[tool.Invocation] = true
		started, se := time.Parse(time.RFC3339Nano, value.Started)
		completed, ce := time.Parse(time.RFC3339Nano, value.Completed)
		var duration float64
		if se != nil || ce != nil || completed.Before(started) || value.Duration.Decode(&duration) != nil || duration < 0 || !asset.ValidID(value.Attempt) || value.RetryIndex < 0 || value.Effects != "NOT_STARTED" || !oneOf(value.Retry, "SAFE", "UNSAFE", "SAFE_WITH_IDEMPOTENCY_KEY") || value.Resource != nil {
			return invalid()
		}
		args, _ := asset.Freeze(tool.Arguments)
		if args.Digest() != tool.ArgumentsDigest {
			return invalid()
		}
		var observed *QualityTool
		for i := range e.Observations.Tools {
			t := &e.Observations.Tools[i]
			if t.Invocation != nil && *t.Invocation == tool.Invocation {
				if observed != nil {
					return invalid()
				}
				observed = t
			}
		}
		if observed == nil || observed.Action == nil || *observed.Action != tool.Action || observed.Call == nil || *observed.Call != tool.Call || observed.Frame != tool.Frame || observed.Iteration != tool.Iteration || observed.Ordinal != tool.Ordinal || observed.Arguments == nil || *observed.Arguments != tool.ArgumentsDigest || observed.Result == nil || *observed.Result != tool.Result || observed.Content == nil || *observed.Content != ciTextHash(value.Output.Content) || !committedDiagnosisSource(e, *observed) {
			return invalid()
		}
		if tool.Name == "ci_get_execution_context" {
			var context struct {
				Incident    string `json:"incident_id"`
				Execution   string `json:"execution_id"`
				Environment string `json:"environment_id"`
				Case        string `json:"case_id"`
				Outcome     string `json:"outcome"`
				Completed   string `json:"completed_at"`
				Evidence    []struct {
					ID           string  `json:"evidence_id"`
					Kind         string  `json:"kind"`
					Availability string  `json:"availability"`
					Media        string  `json:"media_type"`
					Digest       *string `json:"digest"`
				} `json:"evidence"`
			}
			if tool.Arguments.String() != "{}" || StrictQualityShape([]byte(value.Output.Content), &context) != nil || context.Incident != incident || context.Execution != execution || context.Environment != state.Environment.ID || context.Case != state.Case.ID || context.Completed != summary.Completed || context.Outcome != summary.Outcome || len(context.Evidence) != 6 {
				return invalid()
			}
			seen := map[string]bool{}
			for _, entry := range context.Evidence {
				v, ok := ids[entry.ID]
				if !ok || seen[entry.ID] || entry.Kind != v.Kind || entry.Availability != v.Availability || entry.Media != v.Media || !reflect.DeepEqual(entry.Digest, v.Digest) {
					return invalid()
				}
				seen[entry.ID] = true
			}
			continue
		}
		var arg struct {
			ID string `json:"evidence_id"`
		}
		var payload ciEvidencePayload
		if tool.Arguments.Decode(&arg) != nil || StrictQualityShape([]byte(value.Output.Content), &payload) != nil || payload.ID != arg.ID || payload.Incident != incident || payload.Execution != execution {
			return invalid()
		}
		original, ok := ids[payload.ID]
		if !ok || original.Availability != "AVAILABLE" || payload.Kind != original.Kind || payload.Availability != original.Availability || payload.Media != original.Media || payload.Captured != original.Captured || payload.Content.String() != original.Content.String() || !reflect.DeepEqual(payload.Digest, original.Digest) || !reflect.DeepEqual(payload.Missing, original.Missing) {
			return invalid()
		}
		if original.Kind == "CI_SUMMARY" {
			continue
		}
		source := catalog.CIDiagnosisSource{Kind: payload.Kind, EvidenceID: payload.ID, Digest: *payload.Digest, ContentDigest: ciTextHash(value.Output.Content)}
		if old, ok := result[source.Kind]; ok && old.EvidenceID != source.EvidenceID {
			return invalid()
		}
		result[source.Kind] = source
	}
	return result, nil
}
