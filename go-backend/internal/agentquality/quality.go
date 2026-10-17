package agentquality

import (
	"bytes"
	"encoding/json"
	"reflect"

	"agentevalops/go-backend/internal/asset"
)

const QualitySchema = "agent-quality-evidence.v1"
const QualityObservationsSchema = "agent-quality-observations.v1"
const BehaviorImplementation = "agent-behavior-deterministic.v1"
const BehaviorInput = "agent-quality-input.v2"
const BehaviorResult = "agent-quality-result.v1"
const BehaviorNormalization = "agent-quality-null-preserving.v2"

func StrictQualityShape(raw []byte, dst any) error {
	j, err := asset.ParseJSON(raw)
	if err != nil || j.Decode(dst) != nil {
		return asset.ErrInvalid
	}
	var value any
	if json.Unmarshal(raw, &value) != nil {
		return asset.ErrInvalid
	}
	var shape func(any, reflect.Type) error
	shape = func(v any, t reflect.Type) error {
		if t == reflect.TypeOf(asset.JSON{}) {
			return nil
		}
		if t.Kind() == reflect.Pointer {
			if v == nil {
				return nil
			}
			return shape(v, t.Elem())
		}
		if v == nil {
			return asset.ErrInvalid
		}
		switch t.Kind() {
		case reflect.Struct:
			m, ok := v.(map[string]any)
			if !ok || len(m) != t.NumField() {
				return asset.ErrInvalid
			}
			for i := 0; i < t.NumField(); i++ {
				f := t.Field(i)
				child, ok := m[f.Tag.Get("json")]
				if !ok || shape(child, f.Type) != nil {
					return asset.ErrInvalid
				}
			}
		case reflect.Slice:
			a, ok := v.([]any)
			if !ok {
				return asset.ErrInvalid
			}
			for _, child := range a {
				if shape(child, t.Elem()) != nil {
					return asset.ErrInvalid
				}
			}
		case reflect.Map:
			m, ok := v.(map[string]any)
			if !ok {
				return asset.ErrInvalid
			}
			for _, child := range m {
				if shape(child, t.Elem()) != nil {
					return asset.ErrInvalid
				}
			}
		}
		return nil
	}
	return shape(value, reflect.TypeOf(dst).Elem())
}

type QualityCorrelation struct {
	Agent          string `json:"agent_id"`
	Run            string `json:"run_id"`
	Project        string `json:"evaluation_project_id"`
	EvaluationRun  string `json:"evaluation_run_id"`
	Attempt        string `json:"attempt_id"`
	Request        string `json:"request_id"`
	Case           string `json:"case_id"`
	CaseVersion    string `json:"case_version"`
	CaseDigest     string `json:"case_digest"`
	ManifestDigest string `json:"execution_manifest_digest"`
	ProfileID      string `json:"capture_profile_id"`
	ProfileVersion int    `json:"capture_profile_version"`
	ProfileDigest  string `json:"capture_profile_digest"`
	Timeout        int64  `json:"timeout_milliseconds"`
	QueryDigest    string `json:"query_digest"`
}
type QualityAgentPin struct {
	Version          string `json:"agent_version"`
	Policy           string `json:"execution_policy"`
	DefinitionDigest string `json:"definition_digest"`
	ConfigDigest     string `json:"runtime_config_digest"`
}
type QualityManifest struct {
	Schema       string                     `json:"schema_version"`
	Build        string                     `json:"runtime_build_digest"`
	Fixture      string                     `json:"public_fixture_version"`
	MemoryEffect string                     `json:"memory_effect_policy"`
	Agents       map[string]QualityAgentPin `json:"agents"`
}
type QualityProfile struct {
	ID             string              `json:"id"`
	Version        int                 `json:"version"`
	Policy         string              `json:"policy"`
	Aliases        map[string]string   `json:"tool_aliases"`
	ParameterPaths map[string][]string `json:"parameter_paths"`
	FactKeys       []string            `json:"fact_keys"`
}
type QualityComponent struct {
	Kind         string     `json:"kind"`
	Schema       string     `json:"schema_version"`
	Availability string     `json:"availability"`
	Body         asset.JSON `json:"body"`
	Digest       *string    `json:"body_digest"`
	Reason       *string    `json:"capture_reason"`
}
type QualityFact struct {
	Key        string `json:"key"`
	Digest     string `json:"value_digest"`
	Source     string `json:"source_ref"`
	Extraction string `json:"extraction_version"`
}
type QualityParameter struct {
	Path      string     `json:"path"`
	Supported bool       `json:"supported"`
	Value     asset.JSON `json:"value"`
}
type QualityTool struct {
	Source          string             `json:"source_ref"`
	Frame           string             `json:"frame_ref"`
	Iteration       int                `json:"iteration"`
	Ordinal         int                `json:"ordinal"`
	Action          *string            `json:"action_ref"`
	Call            *string            `json:"call_ref"`
	Invocation      *string            `json:"invocation_ref"`
	Alias           *string            `json:"tool_alias"`
	Arguments       *string            `json:"arguments_digest"`
	Parameters      []QualityParameter `json:"parameters"`
	ReadOnly        bool               `json:"read_only"`
	Resource        *string            `json:"resource_key_digest"`
	State           string             `json:"state"`
	BusinessOutcome *string            `json:"business_outcome"`
	Result          *string            `json:"result_digest"`
	NotExecuted     *string            `json:"not_executed_digest"`
	EffectProof     string             `json:"external_effect_proof"`
	ScanComplete    bool               `json:"fact_scan_complete"`
	Observation     *string            `json:"observation_digest"`
	Spec            *string            `json:"spec_digest"`
	Content         *string            `json:"content_digest"`
	ExecutionStatus *string            `json:"execution_status"`
}
type QualitySource struct {
	Ref            *string `json:"source_ref"`
	Digest         *string `json:"source_digest"`
	Representation *string `json:"representation"`
}
type QualityModel struct {
	Source         string          `json:"source_ref"`
	Frame          *string         `json:"frame_ref"`
	Iteration      *int            `json:"iteration"`
	Purpose        string          `json:"purpose"`
	State          string          `json:"state"`
	Request        string          `json:"request_digest"`
	Snapshot       *string         `json:"snapshot_ref"`
	Messages       *string         `json:"messages_digest"`
	ScanComplete   bool            `json:"fact_scan_complete"`
	Verified       bool            `json:"request_capture_verified"`
	Sources        []QualitySource `json:"sources"`
	Root           bool            `json:"root_frame"`
	AcceptedKind   *string         `json:"accepted_kind"`
	AcceptedDigest *string         `json:"accepted_digest"`
	InputTokens    *int64          `json:"input_tokens"`
	OutputTokens   *int64          `json:"output_tokens"`
	TotalTokens    *int64          `json:"total_tokens"`
	Usage          string          `json:"usage_quality"`
	Profile        string          `json:"profile_digest"`
	Compaction     *string         `json:"compaction_operation_ref"`
	Budget         string          `json:"budget_operation_ref"`
}
type QualityContext struct {
	Source          string                     `json:"source_ref"`
	Frame           string                     `json:"frame_ref"`
	Version         int                        `json:"version"`
	Digest          string                     `json:"snapshot_digest"`
	Parent          *string                    `json:"parent_ref"`
	Cursor          *int                       `json:"cursor"`
	Kind            string                     `json:"kind"`
	SourceDigest    string                     `json:"source_digest"`
	MandatoryDigest string                     `json:"mandatory_digest"`
	Validator       asset.JSON                 `json:"validator"`
	ScanComplete    bool                       `json:"fact_scan_complete"`
	Comparisons     []QualityRequestComparison `json:"request_budget_comparisons"`
	Compaction      *string                    `json:"compaction_operation_ref"`
}
type QualityRequestComparison struct {
	Profile          string `json:"profile_digest"`
	Mode             string `json:"mode"`
	Counting         string `json:"counting_version"`
	Options          string `json:"options_digest"`
	Reserve          int64  `json:"output_reserve"`
	Cursor           int64  `json:"cursor"`
	RawRequest       string `json:"raw_request_digest"`
	CompactedRequest string `json:"compacted_request_digest"`
	Raw              int64  `json:"raw_required_window"`
	Compacted        int64  `json:"compacted_required_window"`
	Delta            int64  `json:"bound_delta"`
}
type QualityChild struct {
	Source            string  `json:"source_ref"`
	Parent            string  `json:"parent_ref"`
	Child             string  `json:"child_ref"`
	Target            *string `json:"target_ref"`
	Permission        *string `json:"permission_digest"`
	Result            *string `json:"result_digest"`
	Verified          bool    `json:"result_verified"`
	Accepted          bool    `json:"parent_accepted"`
	ScanComplete      bool    `json:"fact_scan_complete"`
	Content           *string `json:"content_digest"`
	ParentObservation *string `json:"parent_observation_digest"`
	ParentModel       *string `json:"parent_model_ref"`
}
type QualityMemoryRecord struct {
	Ref       string  `json:"record_ref"`
	Kind      string  `json:"kind"`
	Version   *int    `json:"version"`
	Source    *string `json:"source_digest"`
	Epistemic *string `json:"epistemic_kind"`
}
type QualityMemoryAdmission struct {
	Ref    *string `json:"ref"`
	State  *string `json:"state"`
	Result *string `json:"result_digest"`
}
type QualityMemory struct {
	Mode        *string                  `json:"mode"`
	Status      *string                  `json:"status"`
	Manifest    *string                  `json:"manifest_digest"`
	Seed        *string                  `json:"seed_digest"`
	Selected    []QualityMemoryRecord    `json:"selected"`
	Admissions  []QualityMemoryAdmission `json:"admissions"`
	ActualUse   bool                     `json:"actual_use_proven"`
	Eligibility string                   `json:"current_eligibility"`
	Effects     *QualityMemoryEffects    `json:"effects"`
}
type QualityMemoryEffects struct {
	Policy       string              `json:"policy"`
	ReadMode     string              `json:"read_mode"`
	Conversation QualityMemoryEffect `json:"conversation"`
	Formation    QualityMemoryEffect `json:"formation"`
	Final        *string             `json:"final_receipt_digest"`
	Exchanges    int                 `json:"exchange_count"`
	Operations   int                 `json:"formation_operation_count"`
	Models       int                 `json:"formation_model_attempt_count"`
}
type QualityMemoryEffect struct {
	Status string `json:"status"`
	Reason string `json:"reason"`
}
type QualityObservations struct {
	Schema     string           `json:"schema_version"`
	Extraction string           `json:"extraction_version"`
	Coverage   string           `json:"coverage"`
	Gaps       []string         `json:"gaps"`
	Tools      []QualityTool    `json:"tools"`
	Models     []QualityModel   `json:"models"`
	Contexts   []QualityContext `json:"contexts"`
	Children   []QualityChild   `json:"delegations"`
	Memory     *QualityMemory   `json:"memory"`
	Facts      []QualityFact    `json:"facts"`
	Budget     *QualityBudget   `json:"budget"`
}
type QualityBudgetOperation struct {
	Ref        string            `json:"ref"`
	Kind       string            `json:"kind"`
	State      string            `json:"state"`
	Reserved   map[string]int64  `json:"reserved"`
	Actual     *map[string]int64 `json:"actual"`
	Unspent    *map[string]int64 `json:"unspent"`
	Released   *map[string]int64 `json:"released"`
	Delegation *string           `json:"delegation_ref"`
	Source     *string           `json:"source"`
}
type QualityBudget struct {
	Consumed   map[string]int64         `json:"consumed"`
	Reserved   map[string]int64         `json:"reserved"`
	Operations []QualityBudgetOperation `json:"operations"`
}
type QualityTerminal struct {
	Run      string `json:"run_id"`
	Status   string `json:"status"`
	Stop     string `json:"stop_reason"`
	Sequence *int64 `json:"sequence"`
}
type QualityFinal struct {
	Digest string `json:"content_digest"`
	Event  string `json:"event_id"`
	Cursor int64  `json:"cursor"`
}
type QualityWatermark struct {
	Sequence *int64 `json:"terminal_sequence"`
	Frames   []struct {
		Ref        string `json:"ref"`
		Generation int64  `json:"generation"`
		Cursor     int64  `json:"observation_cursor"`
	} `json:"frames"`
}
type QualityBundle struct {
	Schema             string             `json:"schema_version"`
	Producer           string             `json:"producer_version"`
	Capture            string             `json:"capture_policy_version"`
	Algorithm          string             `json:"algorithm_version"`
	ID                 string             `json:"evidence_id"`
	Correlation        QualityCorrelation `json:"correlation"`
	Fingerprint        string             `json:"request_fingerprint"`
	Manifest           asset.JSON         `json:"execution_manifest"`
	ManifestDigest     string             `json:"execution_manifest_digest"`
	Profile            asset.JSON         `json:"capture_profile"`
	ProfileDigest      string             `json:"capture_profile_digest"`
	Policy             string             `json:"execution_policy"`
	Terminal           QualityTerminal    `json:"terminal"`
	Final              *QualityFinal      `json:"root_final"`
	Watermark          QualityWatermark   `json:"source_watermark"`
	Components         []QualityComponent `json:"components"`
	Observations       asset.JSON         `json:"observations"`
	ObservationsDigest *string            `json:"observations_digest"`
	Coverage           string             `json:"coverage"`
	Gaps               []string           `json:"gaps"`
	Sealed             bool               `json:"sealed"`
}

// ValidateQualityJSON 限制整数子集；asset.ParseJSON 已拒绝重复键和非法 Unicode。
func ValidateQualityJSON(body asset.JSON, limit int) error {
	if len(body.Bytes()) > limit {
		return asset.ErrInvalid
	}
	d := json.NewDecoder(bytes.NewReader(body.Bytes()))
	d.UseNumber()
	var value any
	if d.Decode(&value) != nil {
		return asset.ErrInvalid
	}
	var walk func(any) bool
	walk = func(v any) bool {
		switch x := v.(type) {
		case nil, bool, string:
			return true
		case json.Number:
			_, err := x.Int64()
			return err == nil
		case []any:
			for _, child := range x {
				if !walk(child) {
					return false
				}
			}
			return true
		case map[string]any:
			for _, child := range x {
				if !walk(child) {
					return false
				}
			}
			return true
		}
		return false
	}
	if !walk(value) {
		return asset.ErrInvalid
	}
	return nil
}

func DecodeQuality(body asset.JSON, expected QualityCorrelation) (QualityBundle, QualityObservations, error) {
	var b QualityBundle
	var o QualityObservations
	var manifest QualityManifest
	var profile QualityProfile
	invalid := func() (QualityBundle, QualityObservations, error) { return b, o, asset.ErrInvalid }
	if ValidateQualityJSON(body, 1024*1024) != nil || StrictQualityShape(body.Bytes(), &b) != nil ||
		b.Schema != QualitySchema || b.Producer != "localagent-agent-quality-producer.v1" || b.Capture != "agent-quality-capture.v1" ||
		b.Algorithm != "catalog-json-v1" || !b.Sealed || b.ID != "agent-quality://"+expected.Attempt ||
		!reflect.DeepEqual(b.Correlation, expected) || expected.Run != expected.Attempt || !loopDigest.MatchString(b.Fingerprint) ||
		b.ManifestDigest != b.Manifest.Digest() || b.ProfileDigest != b.Profile.Digest() ||
		b.ManifestDigest != expected.ManifestDigest || b.ProfileDigest != expected.ProfileDigest ||
		StrictQualityShape(b.Manifest.Bytes(), &manifest) != nil || StrictQualityShape(b.Profile.Bytes(), &profile) != nil ||
		manifest.Schema != "agent-quality-execution-manifest.v1" || manifest.MemoryEffect != "SERVICE_PROJECT_READ_ONLY.v1" || !loopDigest.MatchString(manifest.Build) || !asset.Text(manifest.Fixture) ||
		profile.Policy != "agent-quality-capture.v1" || profile.ID != expected.ProfileID || profile.Version != expected.ProfileVersion ||
		b.Terminal.Run != expected.Attempt || !oneOf(b.Terminal.Status, "SUCCEEDED", "FAILED", "CANCELLED") ||
		!oneOf(b.Coverage, "COMPLETE", "PARTIAL") || !reflect.DeepEqual(b.Terminal.Sequence, b.Watermark.Sequence) {
		return invalid()
	}
	pin, ok := manifest.Agents[expected.Agent]
	if !ok || pin.Policy != b.Policy || !oneOf(b.Policy, "durable-agent-loop.v1", "durable-agent-loop.v2") {
		return invalid()
	}
	seen := map[string]bool{}
	previous := ""
	for _, c := range b.Components {
		if seen[c.Kind] || c.Kind <= previous || !oneOf(c.Kind, "loop", "context", "memory", "execution") {
			return invalid()
		}
		seen[c.Kind] = true
		previous = c.Kind
		if b.Policy == "durable-agent-loop.v2" && c.Kind != "execution" && (c.Availability != "NOT_APPLICABLE" || c.Reason == nil || *c.Reason != "UNSUPPORTED_EXECUTION_POLICY" ||
			c.Schema != map[string]string{"loop": LoopEvidenceSchema, "context": "context-evidence.v1", "memory": "memory-lifecycle-evidence.v1"}[c.Kind]) {
			return invalid()
		}
		if c.Availability == "AVAILABLE" {
			if c.Digest == nil || *c.Digest != c.Body.Digest() || ValidateQualityJSON(c.Body, 256*1024) != nil {
				return invalid()
			}
			if validateQualityComponent(c, b) != nil {
				return invalid()
			}
			if c.Kind == "loop" {
				if c.Schema != LoopEvidenceSchema || b.Policy != "durable-agent-loop.v1" {
					return invalid()
				}
				if _, err := DecodeLoopEvidence(c.Body, expected.Attempt, LoopCorrelation{expected.EvaluationRun, expected.Attempt, expected.Request}); err != nil {
					return invalid()
				}
			} else if c.Kind == "execution" {
				if c.Schema != "agent-execution-evidence.v1" || b.Policy != "durable-agent-loop.v2" {
					return invalid()
				}
			}
		} else if !oneOf(c.Availability, "UNAVAILABLE", "NOT_APPLICABLE") || c.Body.String() != "null" || c.Digest != nil || c.Reason == nil {
			return invalid()
		}
	}
	if b.Policy == "durable-agent-loop.v1" && (!seen["loop"] || !seen["context"] || !seen["memory"] || seen["execution"]) {
		return invalid()
	}
	if b.Policy == "durable-agent-loop.v2" && (!seen["execution"] || !seen["loop"] || !seen["context"] || !seen["memory"]) {
		return invalid()
	}
	if b.Observations.String() != "null" {
		if b.ObservationsDigest == nil || *b.ObservationsDigest != b.Observations.Digest() || ValidateQualityJSON(b.Observations, 512*1024) != nil ||
			StrictQualityShape(b.Observations.Bytes(), &o) != nil || o.Schema != QualityObservationsSchema || o.Extraction != "agent-quality-projection.v1" || !oneOf(o.Coverage, "COMPLETE", "PARTIAL") {
			return invalid()
		}
	} else if b.Coverage != "PARTIAL" || b.ObservationsDigest != nil {
		return invalid()
	}
	if b.Terminal.Status == "SUCCEEDED" && (b.Terminal.Stop != "COMPLETED" || b.Final == nil || !loopDigest.MatchString(b.Final.Digest) || b.Final.Cursor < 1) {
		return invalid()
	}
	return b, o, nil
}
