package provider

import (
	"context"
	"crypto/sha256"
	"fmt"
	"net/http"
	"net/http/httptrace"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
	ev "agentevalops/go-backend/internal/evaluation"
)

const Stage13TargetID = "localagent-ci-triage-http"
const Stage13TargetVersion = "stage13-evaluation-v1"
const Stage13Protocol = "localagent-ci-triage-evaluation-execute.v1"

type Stage13Config struct {
	Transport               LocalAgentConfig       `json:"transport"`
	ExpectedSubjectManifest asset.JSON             `json:"expected_subject_manifest"`
	ModelIdentityEvidence   []Stage13ModelEvidence `json:"model_identity_evidence,omitempty"`
}

type Stage13Target struct {
	config Stage13Config
	client *http.Client
}

func NewStage13Target(c Stage13Config) (*Stage13Target, error) {
	if c.Transport.Validate() != nil || validateManifest(c.ExpectedSubjectManifest) != nil {
		return nil, asset.ErrInvalid
	}
	client, err := newClient(c.Transport.HTTP)
	if err != nil {
		return nil, err
	}
	return &Stage13Target{c, client}, nil
}
func (t *Stage13Target) Close() { t.client.CloseIdleConnections() }

type triageManifest struct {
	SubjectID      string  `json:"subject_id"`
	SubjectVersion string  `json:"subject_version"`
	AgentID        string  `json:"agent_id"`
	AgentVersion   string  `json:"agent_definition_version"`
	AgentDigest    string  `json:"agent_definition_digest"`
	PromptVersion  string  `json:"prompt_version"`
	PromptDigest   string  `json:"prompt_digest"`
	ModelProfile   string  `json:"model_profile_id"`
	ModelDigest    string  `json:"model_profile_digest"`
	Provider       string  `json:"requested_provider"`
	Model          string  `json:"requested_model"`
	Revision       *string `json:"requested_revision"`
	ToolProfile    string  `json:"tool_profile_id"`
	ToolDigest     string  `json:"tool_profile_digest"`
	SchemaVersion  string  `json:"output_schema_version"`
	SchemaDigest   string  `json:"output_schema_digest"`
	Enabled        bool    `json:"execution_enabled"`
	Digest         string  `json:"subject_manifest_digest"`
}

func hashWithout(j asset.JSON, field string) string {
	var body map[string]asset.JSON
	if j.Decode(&body) != nil {
		return ""
	}
	delete(body, field)
	frozen, err := asset.Freeze(body)
	if err != nil {
		return ""
	}
	return frozen.Digest()
}
func validateManifest(j asset.JSON) error {
	var m triageManifest
	if j.Decode(&m) != nil || !m.Enabled || m.SubjectID != m.AgentID || m.SubjectVersion != m.AgentVersion ||
		m.AgentID == "" || m.AgentVersion == "" || m.PromptVersion == "" || m.ModelProfile == "" || m.Provider == "" || m.Model == "" ||
		m.ToolProfile == "" || m.SchemaVersion != "stage13.triage-output.v1" || m.Digest != hashWithout(j, "subject_manifest_digest") {
		return asset.ErrInvalid
	}
	for _, d := range []string{m.AgentDigest, m.PromptDigest, m.ModelDigest, m.ToolDigest, m.SchemaDigest, m.Digest} {
		if len(d) != 64 || strings.Trim(d, "0123456789abcdef") != "" {
			return asset.ErrInvalid
		}
	}
	return nil
}

type triageCall struct {
	ArtifactDigest   *string    `json:"reported_artifact_digest"`
	DeploymentID     *string    `json:"reported_deployment_id"`
	ResolvedProvider string     `json:"resolved_provider"`
	ProviderSource   string     `json:"resolved_provider_source"`
	Endpoint         string     `json:"resolved_endpoint"`
	Config           asset.JSON `json:"resolved_model_config"`
	Fingerprint      *string    `json:"system_fingerprint"`
	MessagesDigest   string     `json:"effective_messages_digest"`
	SystemDigest     string     `json:"effective_system_prompt_digest"`
	ID               string     `json:"call_id"`
	RunID            string     `json:"run_id"`
	Role             string     `json:"role"`
	Profile          string     `json:"resolved_profile_id"`
	ProfileDigest    string     `json:"resolved_profile_digest"`
	ConfigDigest     string     `json:"model_config_digest"`
	Provider         string     `json:"requested_provider"`
	Model            string     `json:"requested_model"`
	Revision         *string    `json:"requested_revision"`
	ReportedProvider *string    `json:"reported_provider"`
	ReportedModel    *string    `json:"reported_model"`
	ReportedRevision *string    `json:"reported_revision"`
	ActualRevision   *string    `json:"actual_revision"`
	Certainty        string     `json:"dispatch_certainty"`
	Verification     string     `json:"verification_status"`
	InputTokens      *int64     `json:"input_tokens"`
	OutputTokens     *int64     `json:"output_tokens"`
	Cost             *float64   `json:"cost"`
	State            string     `json:"state"`
}
type triageReceipt struct {
	Version         string       `json:"receipt_version"`
	RunID           string       `json:"run_id"`
	Anchor          string       `json:"anchor_run_id"`
	JobID           *string      `json:"analysis_job_id"`
	AttemptID       *string      `json:"evaluation_attempt_id"`
	Role            string       `json:"role"`
	Manifest        asset.JSON   `json:"actual_subject_manifest"`
	InputDigest     string       `json:"actual_input_digest"`
	EffectiveDigest string       `json:"effective_payload_digest"`
	TemplateDigest  string       `json:"prompt_template_digest"`
	ToolIdentity    string       `json:"resolved_toolset_identity"`
	AnswerDigest    string       `json:"final_answer_digest"`
	Calls           []triageCall `json:"model_call_receipts"`
	Verification    string       `json:"model_identity_verification"`
	Digest          string       `json:"receipt_digest"`
}
type triageChild struct {
	RunID   string     `json:"run_id"`
	Role    string     `json:"role"`
	Status  string     `json:"status"`
	Stop    string     `json:"stop_reason"`
	Receipt asset.JSON `json:"actual_subject_receipt"`
}
type triageResponse struct {
	ModelDecision asset.JSON    `json:"model_comparability_decision"`
	Anchor        string        `json:"anchor_run_id"`
	ChildReceipts []asset.JSON  `json:"child_subject_receipts"`
	Capture       string        `json:"triage_capture_status"`
	Protocol      string        `json:"protocol_version"`
	RunID         string        `json:"run_id"`
	Status        string        `json:"status"`
	Stop          string        `json:"stop_reason"`
	Receipt       asset.JSON    `json:"actual_subject_receipt"`
	Children      []triageChild `json:"child_runs"`
	Selected      string        `json:"selected_final_run_id"`
	Validation    asset.JSON    `json:"business_output_validation"`
	Answer        asset.JSON    `json:"final_answer_evidence"`
}
type triageAnswer struct {
	Schema    string `json:"schema_version"`
	ID        string `json:"evidence_id"`
	RunID     string `json:"run_id"`
	AttemptID string `json:"attempt_id"`
	Producer  string `json:"producer_run_id"`
	Content   string `json:"content"`
	Digest    string `json:"content_sha256"`
}

func rawDigest(s string) string      { return fmt.Sprintf("%x", sha256.Sum256([]byte(s))) }
func sameOptional(a, b *string) bool { return a == nil && b == nil || a != nil && b != nil && *a == *b }
func terminal(status, stop string) bool {
	if status == "SUCCEEDED" {
		return stop == "COMPLETED"
	}
	return contains(status, "FAILED", "CANCELLED", "BLOCKED") && contains(stop, "UNHANDLED_ERROR", "DEADLINE_EXCEEDED", "BUDGET_EXHAUSTED", "USER_CANCELLED", "CLIENT_DISCONNECTED", "SYSTEM_SHUTDOWN", "PLANNING_FAILED", "MAX_STEPS_REACHED")
}
func (t *Stage13Target) validateReceipt(j asset.JSON, anchor, runID, role, inputDigest string, seen map[string]bool) (triageReceipt, error) {
	var r triageReceipt
	var m triageManifest
	_ = t.config.ExpectedSubjectManifest.Decode(&m)
	if j.Decode(&r) != nil || r.Version != "stage13.actual-subject-receipt.v1" || r.RunID != runID || r.Anchor != anchor || r.Role != role ||
		r.AttemptID == nil || *r.AttemptID != anchor || r.JobID != nil || r.Manifest.String() != t.config.ExpectedSubjectManifest.String() ||
		validateManifest(r.Manifest) != nil || r.InputDigest != inputDigest || r.ToolIdentity != m.ToolDigest || r.Digest != hashWithout(j, "receipt_digest") ||
		r.Calls == nil || len(r.Calls) > 1 {
		return r, &Error{Kind: IdentityMismatch}
	}
	template := "仅分析下面原始授权输入；输出一个严格七字段 JSON 对象。"
	if role == "SCHEMA_REPAIR" {
		template = "仅使用原始授权输入、被拒绝输出和校验错误修复格式及引用；不得补造业务事实。"
	}
	if r.TemplateDigest != rawDigest(template) {
		return r, &Error{Kind: IdentityMismatch}
	}
	if !contains(r.Verification, "UNKNOWN", "VERIFIED_BY_PROVIDER_RESPONSE", "PROVIDER_MODEL_MATCH", "EXACT_DEPLOYMENT_VERIFIED", "MODEL_IDENTITY_UNKNOWN", "MODEL_IDENTITY_MISMATCH") {
		return r, &Error{Kind: IdentityMismatch}
	}
	if len(r.Calls) == 0 && !contains(r.Verification, "UNKNOWN", "MODEL_IDENTITY_UNKNOWN") {
		return r, &Error{Kind: IdentityMismatch}
	}
	for _, c := range r.Calls {
		if !asset.ValidID(c.ID) || seen[c.ID] || c.RunID != runID || c.Role != role || c.Profile != m.ModelProfile || c.ProfileDigest != m.ModelDigest || c.ConfigDigest != m.ModelDigest ||
			c.Provider != m.Provider || c.Model != m.Model || !sameOptional(c.Revision, m.Revision) || c.Verification != r.Verification ||
			!contains(c.Certainty, "MAY_HAVE_DISPATCHED", "PROVIDER_RESPONDED") || !contains(c.State, "STARTED", "COMPLETED", "UNKNOWN") {
			return r, &Error{Kind: IdentityMismatch}
		}
		seen[c.ID] = true
		if contains(c.Verification, "VERIFIED_BY_PROVIDER_RESPONSE", "EXACT_DEPLOYMENT_VERIFIED") {
			if c.ReportedModel == nil || *c.ReportedModel != m.Model ||
				c.ReportedRevision == nil || c.ActualRevision == nil || !sameOptional(c.ReportedRevision, c.ActualRevision) ||
				strings.TrimSpace(*c.ReportedRevision) == "" ||
				(m.Revision != nil && !sameOptional(m.Revision, c.ActualRevision)) || c.State != "COMPLETED" || c.Certainty != "PROVIDER_RESPONDED" {
				return r, &Error{Kind: IdentityMismatch}
			}
		} else if c.ActualRevision != nil && (c.ReportedRevision == nil || !sameOptional(c.ReportedRevision, c.ActualRevision) || strings.TrimSpace(*c.ActualRevision) == "") {
			return r, &Error{Kind: IdentityMismatch}
		}
	}
	return r, nil
}

func (t *Stage13Target) Execute(parent context.Context, s ev.Scope, q ev.Request) (out ev.Outcome, err error) {
	out = ev.Outcome{Kind: ev.Failure, RequestID: q.ID, RemoteID: q.AttemptID, Protocol: Stage13Protocol, Source: "LOCALAGENT_HTTP", DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: string(ProtocolError), Reason: "Stage13 请求校验失败"}
	var input struct {
		Agent string `json:"agent_id"`
		Query string `json:"query"`
	}
	expectedConfig, _ := asset.Freeze(t.config)
	var manifest triageManifest
	_ = t.config.ExpectedSubjectManifest.Decode(&manifest)
	// WP05 的 Case.Input 是两侧共用的可见 episode；subject 由冻结 Target 拥有。
	var fields map[string]asset.JSON
	_ = q.Case.Case.Input.Decode(&fields)
	var inputVersion string
	_ = fields["schema_version"].Decode(&inputVersion)
	if inputVersion == "stage13.triage-input.v1" {
		if citriage.ValidateInput(q.Case.Case.Input) != nil {
			return out, nil
		}
		input.Agent, input.Query = manifest.AgentID, q.Case.Case.Input.String()
	} else if q.Case.Case.Input.Decode(&input) != nil {
		return out, nil
	}
	if q.Target.ID != Stage13TargetID || q.Target.Version != Stage13TargetVersion || q.Target.Kind != "LOCALAGENT_HTTP" ||
		q.Target.Config.String() != expectedConfig.String() || q.Target.TimeoutMilliseconds <= 0 || q.Target.TimeoutMilliseconds > 180000 ||
		!asset.ValidID(q.AttemptID) || !asset.ValidID(q.ID) || !asset.ValidID(q.RunID) || s.ProjectID != q.Case.Identity.ProjectID ||
		input.Agent != manifest.AgentID {
		return out, nil
	}
	parsed, parseErr := asset.ParseJSON([]byte(input.Query))
	if parseErr != nil {
		return out, nil
	}
	token := os.Getenv(t.config.Transport.TokenEnv)
	if strings.TrimSpace(token) == "" || strings.ContainsAny(token, "\r\n") || parent.Err() != nil {
		return out, nil
	}
	body, _ := asset.Freeze(map[string]any{"run_id": q.AttemptID, "agent_id": input.Agent, "query": input.Query, "timeout_seconds": float64(q.Target.TimeoutMilliseconds) / 1000, "expected_subject_manifest": t.config.ExpectedSubjectManifest})
	ctx, cancel := context.WithTimeout(parent, time.Duration(t.config.Transport.CallMilliseconds)*time.Millisecond)
	defer cancel()
	var gotConn, failedConnect atomic.Bool
	trace := &httptrace.ClientTrace{GotConn: func(httptrace.GotConnInfo) { gotConn.Store(true) }, ConnectDone: func(_, _ string, e error) {
		if e != nil {
			failedConnect.Store(true)
		}
	}}
	req, e := http.NewRequestWithContext(httptrace.WithClientTrace(ctx, trace), "POST", strings.TrimRight(t.config.Transport.BaseURL, "/")+"/api/runtime/evaluation-execute/stage13/v1", nil)
	if e != nil {
		return out, nil
	}
	post(req, body.Bytes())
	req.Header.Set("Authorization", "Bearer "+token)
	resp, e := t.client.Do(req)
	out.Kind = ev.Unknown
	out.DispatchCertainty = "MAY_HAVE_DISPATCHED"
	out.TerminalCertainty = "UNCONFIRMED"
	out.ErrorCategory = string(TransportError)
	out.Reason = "远端终态未确认"
	if e != nil {
		if !gotConn.Load() && failedConnect.Load() {
			out.Kind = ev.Failure
			out.DispatchCertainty = "NOT_DISPATCHED"
			out.TerminalCertainty = "NOT_DISPATCHED"
		}
		return out, nil
	}
	defer resp.Body.Close()
	raw, e := boundedBody(resp.Body, t.config.Transport.MaxResponseBytes)
	if e != nil || resp.StatusCode != 200 {
		return out, nil
	}
	out.ErrorCategory = string(ProtocolError)
	var wire triageResponse
	if strict(raw, &wire) != nil || wire.Protocol != Stage13Protocol || wire.RunID != q.AttemptID || wire.Anchor != q.AttemptID || !terminal(wire.Status, wire.Stop) || wire.Children == nil || len(wire.Children) > 1 || wire.ChildReceipts == nil || len(wire.Children) != len(wire.ChildReceipts) {
		return out, nil
	}
	// 合法 terminal envelope 的身份失败是确认的拒绝，不能进入可比较结果。
	out.Kind = ev.Failure
	out.TerminalCertainty = "REMOTE_CONFIRMED"
	out.DispatchCertainty = "REMOTE_ACCEPTED"
	out.ErrorCategory = string(IdentityMismatch)
	seen := map[string]bool{}
	initial, e := t.validateReceipt(wire.Receipt, q.AttemptID, q.AttemptID, "INITIAL", parsed.Digest(), seen)
	if e != nil || initial.EffectiveDigest != rawDigest(input.Query) {
		return out, nil
	}
	selected := initial
	selectedStatus := wire.Status
	if wire.Selected != q.AttemptID {
		if len(wire.Children) != 1 || wire.Children[0].RunID != wire.Selected {
			return out, nil
		}
		child := wire.Children[0]
		if child.Receipt.String() != wire.ChildReceipts[0].String() {
			return out, nil
		}
		if !asset.ValidID(child.RunID) || child.RunID == q.AttemptID || child.Role != "SCHEMA_REPAIR" || !terminal(child.Status, child.Stop) {
			return out, nil
		}
		selected, e = t.validateReceipt(child.Receipt, q.AttemptID, child.RunID, "SCHEMA_REPAIR", parsed.Digest(), seen)
		if e != nil {
			return out, nil
		}
		selectedStatus = child.Status
	} else if len(wire.Children) != 0 {
		return out, nil
	}
	if (selectedStatus == "SUCCEEDED" && wire.Capture != "COMPLETE") || (selectedStatus != "SUCCEEDED" && wire.Capture != "FAILED") {
		return out, nil
	}
	var answer triageAnswer
	if wire.Answer.Decode(&answer) != nil || answer.Schema != "stage13-final-answer.v1" || answer.RunID != q.AttemptID || answer.AttemptID != q.AttemptID ||
		answer.Producer != wire.Selected || answer.ID != "final-answer://"+q.AttemptID || len(answer.Content) > 65536 || answer.Digest != rawDigest(answer.Content) || selected.AnswerDigest != answer.Digest {
		return out, nil
	}
	var validation struct {
		Status   string     `json:"status"`
		Schema   bool       `json:"schema_valid"`
		Semantic bool       `json:"semantic_valid"`
		Errors   []string   `json:"errors"`
		Output   asset.JSON `json:"output"`
	}
	if wire.Validation.Decode(&validation) != nil || !contains(validation.Status, "VALID", "INVALID") ||
		validation.Status == "VALID" && (!validation.Schema || !validation.Semantic || len(validation.Errors) != 0 || validation.Output.String() == "null") {
		return out, nil
	}
	envelope, _ := asset.ParseJSON(raw)
	out.Evidence = []ev.Binding{binding(s, q, "subject-receipt://"+q.AttemptID, "stage13-subject-receipt.v1", envelope), binding(s, q, answer.ID, answer.Schema, wire.Answer)}
	evidenceFor := func(r triageReceipt) []Stage13ModelEvidence {
		var result []Stage13ModelEvidence
		for _, e := range t.config.ModelIdentityEvidence {
			if e.RunID == r.RunID {
				result = append(result, e)
			}
		}
		return result
	}
	initialDecision := ProjectStage13ModelIdentity(Stage13PolicyInput{Manifest: t.config.ExpectedSubjectManifest, Receipt: wire.Receipt, Evidence: evidenceFor(initial)}, "AGENT_REGRESSION")
	selectedReceipt := wire.Receipt
	if wire.Selected != q.AttemptID {
		selectedReceipt = wire.Children[0].Receipt
	}
	modelDecision := ProjectStage13ModelIdentity(Stage13PolicyInput{Manifest: t.config.ExpectedSubjectManifest, Receipt: selectedReceipt, Evidence: evidenceFor(selected)}, "AGENT_REGRESSION")
	out.Cleanup, _ = asset.Freeze(map[string]any{"comparability": "BLOCKED", "runtime_status": wire.Status, "selected_status": selectedStatus, "validation": wire.Validation, "model_identity_verification": modelDecision.Level, "model_comparability_decision": modelDecision, "target_version": Stage13TargetVersion})
	if !initialDecision.Comparable || !modelDecision.Comparable {
		out.ErrorCategory = "MODEL_IDENTITY_UNKNOWN"
		if initialDecision.Level == "MODEL_IDENTITY_MISMATCH" || modelDecision.Level == "MODEL_IDENTITY_MISMATCH" {
			out.ErrorCategory = "MODEL_IDENTITY_MISMATCH"
		}
		out.Reason = "v2 模型身份策略未满足；禁止可比较执行"
		return out, nil
	}
	if selectedStatus != "SUCCEEDED" {
		out.ErrorCategory = string(RemoteTerminal)
		out.Reason = "Runtime 已确认失败"
		return out, nil
	}
	out.Kind = ev.Success
	out.ErrorCategory = ""
	out.Reason = "身份、真实终态和原始输出已验证"
	value, _ := asset.Freeze(answer.Content)
	artifact := binding(s, q, answer.ID, "artifact.v1", value)
	out.Artifact = &artifact
	out.Cleanup, _ = asset.Freeze(map[string]any{"comparability": "COMPARABLE", "runtime_status": wire.Status, "selected_status": selectedStatus, "validation": wire.Validation, "model_identity_verification": modelDecision.Level, "model_comparability_decision": modelDecision, "target_version": Stage13TargetVersion})
	return out, nil
}
