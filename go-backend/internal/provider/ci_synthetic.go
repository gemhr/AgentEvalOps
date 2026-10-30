package provider

import (
	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"context"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"
)

const CISyntheticProtocol = "ci-synthetic-evaluation-execute.v1"
const CISyntheticTargetID = "ci-synthetic-http"
const CISyntheticTargetVersion = "ci-synthetic-target.v2"
const CISyntheticQuery = "请调查固定的公开合成 CI 失败，使用授权工具取证。"

type FrozenCI struct {
	CaseID          string `json:"case_id"`
	CaseVersion     string `json:"case_version"`
	CaseDigest      string `json:"case_digest"`
	Template        string `json:"template_id"`
	TemplateVersion string `json:"template_version"`
	InputDigest     string `json:"input_digest"`
}

// Case input 不包含自身 Case content digest，避免不可构造的摘要自引用。
type CISyntheticCaseInput struct {
	Template        string `json:"template_id"`
	TemplateVersion string `json:"template_version"`
	InputDigest     string `json:"input_digest"`
}
type CISyntheticConfig struct {
	Quality   AgentQualityConfig `json:"quality"`
	Frozen    FrozenCI           `json:"frozen_ci"`
	Manifest  string             `json:"candidate_manifest_digest"`
	Mode      string             `json:"mode"`
	Prompt    string             `json:"prompt_digest"`
	Candidate string             `json:"candidate_id"`
}
type CISyntheticTarget struct {
	pair          *ciPairTarget
	config        CISyntheticConfig
	client        *http.Client
	authorization CISyntheticAttemptAuthorization
	token         string
}

func NewCISyntheticTarget(c CISyntheticConfig, a CISyntheticAttemptAuthorization) (*CISyntheticTarget, error) {
	if !strings.HasPrefix(c.Candidate, "ci-prompt-") || !ciDigest(strings.TrimPrefix(c.Candidate, "ci-prompt-")) || c.Quality.Validate() != nil || !asset.ValidID(c.Frozen.CaseID) || c.Frozen.CaseVersion == "" || c.Frozen.Template != "report_pipeline_failure" || c.Frozen.TemplateVersion != "2" || !ciDigest(c.Frozen.InputDigest) || !ciDigest(c.Frozen.CaseDigest) || !ciDigest(c.Manifest) || !ciDigest(c.Prompt) || c.Mode != "BASELINE" && c.Mode != "CANDIDATE" {
		return nil, asset.ErrInvalid
	}
	token := os.Getenv(c.Quality.TokenEnv)
	if a.validate(c, token) != nil {
		return nil, asset.ErrInvalid
	}
	client, e := newClient(c.Quality.HTTP)
	return &CISyntheticTarget{config: c, client: client, authorization: a, token: token}, e
}
func (t *CISyntheticTarget) Close() { t.client.CloseIdleConnections() }
func ciDigest(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, r := range s {
		if !(r >= '0' && r <= '9' || r >= 'a' && r <= 'f') {
			return false
		}
	}
	return true
}

func ciSession(run string) string {
	ns, _ := hex.DecodeString("6ba7b8119dad11d180b400c04fd430c8")
	sum := sha1.Sum(append(ns, []byte("agent-quality-session:"+run)...))
	b := sum[:16]
	b[6] = (b[6] & 15) | 80
	b[8] = (b[8] & 63) | 128
	s := hex.EncodeToString(b)
	return s[:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:]
}

type ciBinding struct {
	Contract      string                `json:"contract_version"`
	Source        string                `json:"source_type"`
	Run           string                `json:"run_id"`
	Session       string                `json:"session_id"`
	Agent         string                `json:"agent_id"`
	Version       string                `json:"agent_version"`
	Instance      string                `json:"instance_id"`
	Execution     string                `json:"execution_id"`
	Incident      string                `json:"incident_id"`
	Investigation string                `json:"investigation_id"`
	ReportVersion int                   `json:"report_version"`
	Frozen        FrozenCI              `json:"frozen_ci"`
	Manifest      string                `json:"manifest_digest"`
	Mode          string                `json:"mode"`
	Correlation   aq.QualityCorrelation `json:"quality_correlation"`
}
type ciProof struct {
	Schema    string `json:"schema_version"`
	Candidate string `json:"candidate_id"`
	Manifest  string `json:"manifest_digest"`
	Mode      string `json:"mode"`
	Prompt    string `json:"prompt_digest"`
	Request   string `json:"model_request_digest"`
	Messages  string `json:"messages_digest"`
	System    string `json:"system_digest"`
	Scope     string `json:"evidence_scope"`
}
type ciModel struct {
	Ref   string  `json:"model_ref"`
	Frame string  `json:"frame_ref"`
	Run   string  `json:"run_id"`
	State string  `json:"model_state"`
	Proof ciProof `json:"proof"`
}
type ciSidecar struct {
	Schema  string    `json:"schema_version"`
	Binding ciBinding `json:"binding"`
	Models  []ciModel `json:"models"`
	Quality string    `json:"quality_digest"`
	Report  *string   `json:"report_digest"`
	Status  string    `json:"runtime_status"`
}
type ciWire struct {
	Contract string     `json:"contract_version"`
	Quality  asset.JSON `json:"quality"`
	Sidecar  asset.JSON `json:"sidecar"`
	Digest   string     `json:"sidecar_digest"`
}

func VerifyCISynthetic(raw []byte, c CISyntheticConfig, correlation aq.QualityCorrelation) (asset.JSON, error) {
	j, e := asset.ParseJSON(raw)
	var w ciWire
	var side ciSidecar
	var quality agentQualityResponse
	if e != nil || aq.StrictQualityShape(raw, &w) != nil || w.Contract != CISyntheticProtocol || w.Sidecar.Digest() != w.Digest || aq.StrictQualityShape(w.Sidecar.Bytes(), &side) != nil || aq.StrictQualityShape(w.Quality.Bytes(), &quality) != nil {
		return j, asset.ErrInvalid
	}
	qj, _ := asset.Freeze(correlation)
	sj, _ := asset.Freeze(side.Binding.Correlation)
	var manifest aq.QualityManifest
	_ = c.Quality.Manifest.Decode(&manifest)
	if side.Schema != "ci-synthetic-evidence.v1" || side.Binding.Contract != CISyntheticProtocol || side.Binding.Source != "SYNTHETIC" || side.Binding.Run != correlation.Attempt || side.Binding.Frozen != c.Frozen || side.Binding.Manifest != c.Manifest || side.Binding.Mode != c.Mode || side.Binding.Agent != correlation.Agent || side.Binding.Version != manifest.Agents[correlation.Agent].Version || sj.String() != qj.String() || side.Status != quality.Status || quality.Run != correlation.Attempt || side.Quality != w.Quality.Digest() || quality.Digest != quality.Bundle.Digest() {
		return j, asset.ErrInvalid
	}
	if side.Binding.Session != ciSession(correlation.Attempt) || side.Binding.ReportVersion != 1 {
		return j, asset.ErrInvalid
	}
	for _, id := range []string{side.Binding.Session, side.Binding.Instance, side.Binding.Execution, side.Binding.Incident, side.Binding.Investigation} {
		if !asset.ValidID(id) {
			return j, asset.ErrInvalid
		}
	}
	bundle, _, e := aq.DecodeQuality(quality.Bundle, correlation)
	if e != nil {
		return j, e
	}
	var observations aq.QualityObservations
	if bundle.Observations.Decode(&observations) != nil || len(side.Models) == 0 {
		return j, asset.ErrInvalid
	}
	seen := map[string]bool{}
	for _, model := range side.Models {
		p := model.Proof
		if model.Run != correlation.Attempt || p.Schema != "ci-prompt-selection.v1" || p.Scope != "DURABLE_REQUEST_PREPARED" || p.Manifest != c.Manifest || p.Mode != c.Mode || p.Prompt != c.Prompt || !ciDigest(p.Request) || !ciDigest(p.System) || !ciDigest(p.Messages) || p.Candidate != c.Candidate || seen[model.Ref] {
			return j, asset.ErrInvalid
		}
		seen[model.Ref] = true
		frame, _ := asset.Freeze(model.Frame)
		found := false
		for _, actual := range observations.Models {
			if actual.Request == p.Request && actual.Messages != nil && *actual.Messages == p.Messages && actual.Verified && actual.State == model.State && actual.Frame != nil && *actual.Frame == frame.Digest() && actual.Root && actual.Purpose == "DECISION" {
				found = true
			}
		}
		if !found {
			return j, asset.ErrInvalid
		}
	}
	return j, nil
}

func (t *CISyntheticTarget) Execute(parent context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	if t.pair != nil {
		return t.executePair(parent, s, q)
	}
	out := ev.Outcome{Kind: ev.Failure, Source: "LOCALAGENT_HTTP", RequestID: q.ID, RemoteID: q.AttemptID, Protocol: CISyntheticProtocol, DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: string(ProtocolError)}
	config, _ := asset.Freeze(t.config)
	var caseInput CISyntheticCaseInput
	input := t.config.Frozen
	target, err := asset.Freeze(q.Target)
	if err != nil || target.Digest() != t.authorization.TargetDigest || q.RunID != t.authorization.Run || q.AttemptID != t.authorization.Attempt || q.Target.ID != CISyntheticTargetID || q.Target.Version != CISyntheticTargetVersion || q.Target.Kind != "LOCALAGENT_HTTP" || q.Target.Config.String() != config.String() || s.ProjectID != t.config.Quality.TrustedProjectID || q.Case.Case.Input.Decode(&caseInput) != nil || caseInput != (CISyntheticCaseInput{input.Template, input.TemplateVersion, input.InputDigest}) || q.Case.Identity.Ref.EntityID != input.CaseID || q.Case.Identity.Ref.Version != input.CaseVersion || q.Case.Identity.ContentDigest != input.CaseDigest || q.Case.Identity.ProjectID != s.ProjectID {
		return out, nil
	}
	var profile aq.QualityProfile
	_ = t.config.Quality.Profile.Decode(&profile)
	correlation := aq.QualityCorrelation{Agent: "ci_incident_investigator", Run: q.AttemptID, Project: s.ProjectID, EvaluationRun: q.RunID, Attempt: q.AttemptID, Request: q.ID, Case: input.CaseID, CaseVersion: input.CaseVersion, CaseDigest: input.CaseDigest, ManifestDigest: t.config.Quality.Manifest.Digest(), ProfileID: profile.ID, ProfileVersion: profile.Version, ProfileDigest: t.config.Quality.Profile.Digest(), Timeout: q.Target.TimeoutMilliseconds, QueryDigest: fmt.Sprintf("%x", sha256.Sum256([]byte(CISyntheticQuery)))}
	quality, _ := asset.Freeze(correlation)
	var payload map[string]any
	_ = quality.Decode(&payload)
	delete(payload, "query_digest")
	payload["query"] = CISyntheticQuery
	body, _ := asset.Freeze(map[string]any{"contract_version": CISyntheticProtocol, "quality": payload, "frozen_ci": input, "candidate_manifest_digest": t.config.Manifest, "mode": t.config.Mode})
	ctx, cancel := context.WithTimeout(parent, time.Duration(t.config.Quality.CallMilliseconds)*time.Millisecond)
	defer cancel()
	req, e := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(t.config.Quality.BaseURL, "/")+"/api/runtime/evaluation-execute/ci-synthetic/v1", nil)
	if e != nil {
		return out, e
	}
	post(req, body.Bytes())
	req.Header.Set("Authorization", "Bearer "+t.token)
	out.Kind = ev.Unknown
	out.DispatchCertainty = "MAY_HAVE_DISPATCHED"
	out.TerminalCertainty = "UNCONFIRMED"
	response, e := t.client.Do(req)
	if e != nil {
		return out, nil
	}
	defer response.Body.Close()
	raw, e := boundedBody(response.Body, t.config.Quality.MaxResponseBytes)
	if e != nil || response.StatusCode != 200 {
		out.Reason = fmt.Sprintf("CI_HTTP_STATUS_%d", response.StatusCode)
		return out, nil
	}
	// 从同 Root 的只读 GET 取得原封存 Quality；绝不再 POST 旧入口启动执行。
	read, e := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(t.config.Quality.BaseURL, "/")+"/api/runtime/runs/"+q.AttemptID+"/ci-synthetic-evidence/v1", nil)
	if e != nil {
		return out, e
	}
	read.Header.Set("Authorization", "Bearer "+t.token)
	sealed, e := t.client.Do(read)
	if e != nil {
		return out, nil
	}
	defer sealed.Body.Close()
	raw, e = boundedBody(sealed.Body, t.config.Quality.MaxResponseBytes)
	if e != nil || sealed.StatusCode != 200 {
		return out, nil
	}
	verified, e := VerifyCISynthetic(raw, t.config, correlation)
	if e != nil {
		out.Reason = "CI_SIDECAR_INVALID"
		return out, nil
	}
	var w ciWire
	_ = verified.Decode(&w)
	out, e = verifyAgentQualityResponse(s, q, correlation, out, w.Quality.Bytes(), 200)
	if e == nil && out.TerminalCertainty == "REMOTE_CONFIRMED" {
		out.Evidence = append(out.Evidence, binding(s, q, "ci-synthetic://"+q.AttemptID, "ci-synthetic-evidence.v1", w.Sidecar))
		out.Cleanup, _ = asset.Freeze(map[string]any{"target_version": CISyntheticTargetVersion, "quality_contract": AgentQualityProtocol, "sidecar_digest": w.Digest})
	}
	return out, e
}
