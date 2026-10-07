package provider

import (
	"agentevalops/go-backend/internal/asset"
	"encoding/json"
	"strings"
)

const Stage13ModelPolicy = "stage13.model-comparability.v2"

// Stage13ModelEvidence 仅用于显式绑定的历史取证投影，不回写原 receipt。
type Stage13ModelEvidence struct {
	CallID         string     `json:"call_id"`
	RunID          string     `json:"run_id"`
	ReceiptDigest  string     `json:"receipt_digest"`
	MessagesDigest string     `json:"effective_messages_digest"`
	Provider       string     `json:"resolved_provider"`
	Source         string     `json:"resolved_provider_source"`
	Endpoint       string     `json:"resolved_endpoint"`
	Config         asset.JSON `json:"resolved_model_config"`
	Body           string     `json:"response_body"`
	BodyDigest     string     `json:"response_body_sha256"`
}

type Stage13PolicyInput struct {
	Manifest     asset.JSON             `json:"manifest"`
	Receipt      asset.JSON             `json:"receipt"`
	Evidence     []Stage13ModelEvidence `json:"evidence"`
	ToolIdentity asset.JSON             `json:"tool_identity,omitempty"`
}
type Stage13PolicyDocument struct {
	Mode      string             `json:"evaluation_mode"`
	Intended  []string           `json:"intended_variables"`
	Baseline  Stage13PolicyInput `json:"baseline"`
	Candidate Stage13PolicyInput `json:"candidate"`
}
type Stage13ModelDecision struct {
	Policy            string   `json:"policy_version"`
	EvaluatedPolicy   string   `json:"evaluated_under_policy_version"`
	Mode              string   `json:"evaluation_mode"`
	Level             string   `json:"identity_level"`
	Comparable        bool     `json:"comparable"`
	ExpectedProvider  string   `json:"expected_provider"`
	Provider          *string  `json:"actual_provider"`
	ExpectedModel     string   `json:"expected_model"`
	Model             *string  `json:"actual_model"`
	ProfileMatch      bool     `json:"model_profile_match"`
	Revision          *string  `json:"revision"`
	RevisionStatus    string   `json:"revision_status"`
	RevisionRequired  bool     `json:"revision_required"`
	Fingerprint       *string  `json:"system_fingerprint"`
	FingerprintStatus string   `json:"fingerprint_status"`
	Warnings          []string `json:"warnings"`
	Reasons           []string `json:"reasons"`
}

func strptr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// ProjectStage13ModelIdentity 独立重算配置/receipt/response 身份，不信任声明的 MATCH。
func ProjectStage13ModelIdentity(in Stage13PolicyInput, mode string) Stage13ModelDecision {
	var m triageManifest
	_ = in.Manifest.Decode(&m)
	d := Stage13ModelDecision{Policy: Stage13ModelPolicy, EvaluatedPolicy: Stage13ModelPolicy, Mode: mode, Level: "MODEL_IDENTITY_UNKNOWN", ExpectedProvider: m.Provider, ExpectedModel: m.Model, RevisionStatus: "UNAVAILABLE", FingerprintStatus: "UNAVAILABLE", Warnings: []string{}, Reasons: []string{}}
	reject := func(reason string, mismatch bool) Stage13ModelDecision {
		if mismatch {
			d.Level = "MODEL_IDENTITY_MISMATCH"
		}
		d.Reasons = append(d.Reasons, reason)
		return d
	}
	if mode != "AGENT_REGRESSION" && mode != "MODEL_UPGRADE" {
		return reject("EVALUATION_MODE_UNSUPPORTED", true)
	}
	var r triageReceipt
	if validateManifest(in.Manifest) != nil || in.Receipt.Decode(&r) != nil || r.Manifest.String() != in.Manifest.String() || r.Digest != hashWithout(in.Receipt, "receipt_digest") || r.ToolIdentity != m.ToolDigest {
		return reject("SUBJECT_RECEIPT_MISMATCH", true)
	}
	if len(r.Calls) == 0 {
		return reject("MODEL_CALL_MISSING", false)
	}
	if len(r.Calls) != 1 {
		return reject("MODEL_CALL_BINDING_INVALID", true)
	}
	c := r.Calls[0]
	if c.RunID != r.RunID || c.Role != r.Role {
		return reject("MODEL_CALL_BINDING_INVALID", true)
	}
	d.ProfileMatch = c.Profile == m.ModelProfile && c.ProfileDigest == m.ModelDigest && c.ConfigDigest == m.ModelDigest
	if !d.ProfileMatch {
		return reject("MODEL_PROFILE_MISMATCH", true)
	}
	if c.Provider != m.Provider || c.Model != m.Model || !sameOptional(c.Revision, m.Revision) {
		return reject("REQUESTED_IDENTITY_MISMATCH", true)
	}
	provider, source, endpoint, config := c.ResolvedProvider, c.ProviderSource, c.Endpoint, c.Config
	model, revision, fingerprint := c.ReportedModel, c.ReportedRevision, c.Fingerprint
	artifact, deployment := c.ArtifactDigest, c.DeploymentID
	if len(in.Evidence) > 0 {
		if len(in.Evidence) != 1 {
			return reject("MODEL_EVIDENCE_BINDING_INVALID", true)
		}
		e := in.Evidence[0]
		if e.CallID != c.ID || e.RunID != c.RunID || e.ReceiptDigest != r.Digest || e.MessagesDigest != c.MessagesDigest {
			return reject("MODEL_EVIDENCE_BINDING_INVALID", true)
		}
		if rawDigest(e.Body) != e.BodyDigest {
			return reject("PROVIDER_RESPONSE_DIGEST_INVALID", true)
		}
		models, revisions, fingerprints := map[string]bool{}, map[string]bool{}, map[string]bool{}
		artifacts, deployments := map[string]bool{}, map[string]bool{}
		for _, line := range strings.Split(e.Body, "\n") {
			line = strings.TrimSuffix(line, "\r")
			if !strings.HasPrefix(line, "data: ") || line == "data: [DONE]" {
				continue
			}
			packet, err := asset.ParseJSON([]byte(line[6:]))
			if err != nil {
				return reject("MODEL_EVIDENCE_INVALID", true)
			}
			var fields map[string]json.RawMessage
			if packet.Decode(&fields) != nil {
				return reject("MODEL_EVIDENCE_INVALID", true)
			}
			for key, dst := range map[string]map[string]bool{"model": models, "model_revision": revisions, "system_fingerprint": fingerprints, "model_artifact_sha256": artifacts, "deployment_id": deployments} {
				var s string
				if json.Unmarshal(fields[key], &s) == nil && s != "" {
					dst[s] = true
				}
			}
		}
		if len(models) != 1 || len(revisions) > 1 || len(fingerprints) > 1 || len(artifacts) > 1 || len(deployments) > 1 {
			return reject("PROVIDER_RESPONSE_IDENTITY_UNCONFIRMED", false)
		}
		model, revision, fingerprint = nil, nil, nil
		artifact, deployment = nil, nil
		for s := range artifacts {
			artifact = strptr(s)
		}
		for s := range deployments {
			deployment = strptr(s)
		}
		for s := range models {
			model = strptr(s)
		}
		for s := range revisions {
			revision = strptr(s)
		}
		for s := range fingerprints {
			fingerprint = strptr(s)
		}
		if !sameOptional(model, c.ReportedModel) || !sameOptional(revision, c.ReportedRevision) {
			return reject("PROVIDER_RESPONSE_RECEIPT_MISMATCH", true)
		}
		provider, source, endpoint, config = e.Provider, e.Source, e.Endpoint, e.Config
	}
	if c.State != "COMPLETED" || c.Certainty != "PROVIDER_RESPONDED" {
		return reject("PROVIDER_RESPONSE_UNCONFIRMED", false)
	}
	d.Provider, d.Model = strptr(provider), model
	if provider == "" || source != "CONFIGURED_TRANSPORT" || config.String() == "null" {
		return reject("RESOLVED_TRANSPORT_UNAVAILABLE", false)
	}
	if config.Digest() != m.ModelDigest {
		return reject("RESOLVED_CONFIG_MISMATCH", true)
	}
	var cfg map[string]asset.JSON
	if config.Decode(&cfg) != nil {
		return reject("MODEL_EVIDENCE_INVALID", true)
	}
	read := func(key string) string { var s string; _ = cfg[key].Decode(&s); return s }
	if endpoint == "" || rawDigest(endpoint) != read("endpoint_digest") {
		return reject("ENDPOINT_BINDING_INVALID", true)
	}
	if provider != read("provider") || provider != m.Provider {
		return reject("PROVIDER_MISMATCH", true)
	}
	if read("model") != m.Model {
		return reject("MODEL_CONFIG_MISMATCH", true)
	}
	if c.ReportedProvider != nil && *c.ReportedProvider != provider {
		return reject("REPORTED_PROVIDER_MISMATCH", true)
	}
	if model == nil || *model == "" {
		return reject("REPORTED_MODEL_UNAVAILABLE", false)
	}
	if *model != m.Model {
		return reject("CANONICAL_MODEL_MISMATCH", true)
	}
	if revision != nil && *revision != "" {
		d.Revision = revision
	}
	if revision != nil && strings.TrimSpace(*revision) != "" {
		d.RevisionStatus = "PROVIDER_REPORTED"
	}
	if c.ActualRevision != nil && !sameOptional(c.ActualRevision, revision) {
		return reject("ACTUAL_REVISION_NOT_REPORTED", true)
	}
	if c.ActualRevision != nil && strings.TrimSpace(*c.ActualRevision) == "" {
		return reject("ACTUAL_REVISION_NOT_REPORTED", true)
	}
	if m.Revision != nil && revision != nil && *revision != "" && !sameOptional(m.Revision, revision) {
		return reject("EXPECTED_REVISION_MISMATCH", true)
	}
	d.Fingerprint = fingerprint
	if fingerprint != nil && *fingerprint != "" {
		d.FingerprintStatus = "RECORDED"
	}
	d.Level = "PROVIDER_MODEL_MATCH"
	if artifact != nil && len(*artifact) == 64 && strings.Trim(*artifact, "0123456789abcdef") == "" && deployment != nil && strings.TrimSpace(*deployment) != "" && revision != nil && strings.TrimSpace(*revision) != "" {
		d.Level = "EXACT_DEPLOYMENT_VERIFIED"
		d.RevisionStatus = "VERIFIED_BY_PROVIDER_RESPONSE"
	}
	d.Comparable = true
	d.Reasons = []string{"PROVIDER_CANONICAL_MODEL_PROFILE_MATCH"}
	return d
}

type Stage13PairDecision struct {
	Policy            string               `json:"policy_version"`
	EvaluatedPolicy   string               `json:"evaluated_under_policy_version"`
	Mode              string               `json:"evaluation_mode"`
	Baseline          Stage13ModelDecision `json:"baseline"`
	Candidate         Stage13ModelDecision `json:"candidate"`
	Intended          []string             `json:"intended_variables"`
	Comparable        bool                 `json:"comparable"`
	Comparability     string               `json:"comparability"`
	FingerprintStatus string               `json:"fingerprint_status"`
	Warnings          []string             `json:"warnings"`
	Reasons           []string             `json:"reasons"`
}

// CompareStage13Models 只比较明确冻结的模型变量，不计算业务指标。
func CompareStage13Models(doc Stage13PolicyDocument) Stage13PairDecision {
	left, right := ProjectStage13ModelIdentity(doc.Baseline, doc.Mode), ProjectStage13ModelIdentity(doc.Candidate, doc.Mode)
	intended := doc.Intended
	if intended == nil {
		intended = []string{}
	}
	d := Stage13PairDecision{Policy: Stage13ModelPolicy, EvaluatedPolicy: Stage13ModelPolicy, Mode: doc.Mode, Baseline: left, Candidate: right, Intended: intended, Comparability: "BLOCKED", FingerprintStatus: "UNAVAILABLE", Warnings: []string{}, Reasons: []string{}}
	if !left.Comparable || !right.Comparable {
		d.Reasons = append(d.Reasons, "SUBJECT_IDENTITY_BLOCKED")
		return d
	}
	var lm, rm triageManifest
	_ = doc.Baseline.Manifest.Decode(&lm)
	_ = doc.Candidate.Manifest.Decode(&rm)
	var lr, rr triageReceipt
	_ = doc.Baseline.Receipt.Decode(&lr)
	_ = doc.Candidate.Receipt.Decode(&rr)
	if lm.ToolDigest != rm.ToolDigest {
		capability := func(j asset.JSON, m triageManifest) (asset.JSON, bool) {
			var p map[string]asset.JSON
			if j.Digest() != m.ToolDigest || j.Decode(&p) != nil {
				return asset.JSON{}, false
			}
			var agent, profile string
			_ = p["agent_id"].Decode(&agent)
			_ = p["model_profile"].Decode(&profile)
			if agent != m.AgentID || profile != m.ModelProfile {
				return asset.JSON{}, false
			}
			delete(p, "agent_id")
			delete(p, "model_profile")
			c, err := asset.Freeze(p)
			return c, err == nil
		}
		lc, lok := capability(doc.Baseline.ToolIdentity, lm)
		rc, rok := capability(doc.Candidate.ToolIdentity, rm)
		if !lok || !rok || lc.String() != rc.String() {
			d.Reasons = append(d.Reasons, "TOOL_PROFILE_DIGEST_MISMATCH")
		}
	}
	if lm.SchemaDigest != rm.SchemaDigest {
		d.Reasons = append(d.Reasons, "OUTPUT_SCHEMA_DIGEST_MISMATCH")
	}
	if lr.InputDigest != rr.InputDigest {
		d.Reasons = append(d.Reasons, "INPUT_BINDING_MISMATCH")
	}
	differences := []string{}
	if !sameOptional(left.Provider, right.Provider) {
		differences = append(differences, "provider")
	}
	if !sameOptional(left.Model, right.Model) {
		differences = append(differences, "canonical_model")
	}
	if lm.ModelDigest != rm.ModelDigest {
		differences = append(differences, "model_profile")
	}
	if doc.Mode == "AGENT_REGRESSION" {
		if len(differences) > 0 || len(intended) > 0 {
			d.Reasons = append(d.Reasons, "AGENT_REGRESSION_MODEL_IDENTITY_MISMATCH")
		}
	} else {
		valid := lm.PromptDigest == rm.PromptDigest
		for _, v := range intended {
			if !contains(v, "provider", "canonical_model", "model_profile") {
				valid = false
			}
		}
		for _, v := range differences {
			if !contains(v, intended...) {
				valid = false
			}
		}
		if !valid {
			d.Reasons = append(d.Reasons, "MODEL_UPGRADE_VARIABLES_NOT_FROZEN")
		}
	}
	if left.Fingerprint != nil && right.Fingerprint != nil {
		if *left.Fingerprint == *right.Fingerprint {
			d.FingerprintStatus = "BACKEND_FINGERPRINT_MATCH"
		} else {
			d.FingerprintStatus = "BACKEND_CONFIGURATION_CHANGED"
			d.Warnings = append(d.Warnings, "BACKEND_CONFIGURATION_CHANGED")
		}
	}
	if len(d.Reasons) == 0 {
		d.Comparable = true
		d.Comparability = "COMPARABLE"
		if len(d.Warnings) > 0 {
			d.Comparability = "COMPARABLE_WITH_ENVIRONMENT_WARNING"
		}
	}
	return d
}
