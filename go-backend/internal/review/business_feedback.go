package review

// 只从受部署控制的 AC 地址读取投影；调用方不能提供 URL、凭据或原始人工正文。
import (
	"agentevalops/go-backend/internal/asset"
	"context"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

const FeedbackContract = "ci-feedback-projection.v1"

type FeedbackConnection struct {
	BaseURL  string `json:"base_url"`
	TokenEnv string `json:"token_env"`
	Tenant   string `json:"tenant_id"`
	Owner    string `json:"owner_user_id"`
}
type FeedbackObservation struct {
	Tool     string `json:"tool_and_final_binding"`
	Context  string `json:"context_capture"`
	Required string `json:"required_evidence"`
}
type FeedbackAttribution struct {
	Observation   []string `json:"observation"`
	Supporting    []string `json:"supporting_evidence_refs"`
	Contradicting []string `json:"contradicting_evidence_refs"`
	Causes        []string `json:"candidate_causes"`
	Component     string   `json:"affected_component"`
	Uncertainty   []string `json:"uncertainty"`
	Verification  string   `json:"verification_status"`
	Action        string   `json:"recommended_review_action"`
}
type FeedbackProjection struct {
	Contract        string              `json:"contract_version"`
	FeedbackID      string              `json:"source_feedback_id"`
	RevisionID      string              `json:"feedback_revision_id"`
	IncidentID      string              `json:"incident_id"`
	Tenant          string              `json:"tenant_id"`
	Owner           string              `json:"owner_user_id"`
	InvestigationID string              `json:"investigation_id"`
	RunID           string              `json:"root_run_id"`
	ReportVersion   int                 `json:"report_version"`
	ReportDigest    string              `json:"report_digest"`
	RuntimeDigest   string              `json:"runtime_result_digest"`
	EvidenceDigest  string              `json:"evidence_bundle_digest"`
	Decision        string              `json:"feedback_decision"`
	Status          string              `json:"feedback_current_status"`
	Source          string              `json:"source_type"`
	Trust           string              `json:"source_trust"`
	ReviewedAt      string              `json:"reviewed_at"`
	Observations    FeedbackObservation `json:"observations"`
	Attribution     FeedbackAttribution `json:"attribution"`
}
type FeedbackWire struct {
	Projection asset.JSON `json:"projection"`
	Digest     string     `json:"projection_digest"`
}

func DecodeFeedback(raw []byte) (FeedbackWire, FeedbackProjection, error) {
	var w FeedbackWire
	var p FeedbackProjection
	j, e := asset.ParseJSON(raw)
	if e != nil || j.Decode(&w) != nil || w.Projection.Decode(&p) != nil || w.Digest != w.Projection.Digest() {
		return w, p, asset.ErrInvalid
	}
	if p.Contract != FeedbackContract || !asset.ValidID(p.FeedbackID) || p.RevisionID != p.FeedbackID || !asset.ValidID(p.IncidentID) || !asset.ValidID(p.InvestigationID) || !asset.ValidID(p.RunID) || !asset.ValidID(p.Owner) || !asset.Text(p.Tenant) || p.ReportVersion < 1 || !feedbackDigest(p.ReportDigest) || !feedbackDigest(p.RuntimeDigest) || !feedbackDigest(p.EvidenceDigest) || p.Source != "SYNTHETIC" || p.Trust != "HUMAN" || !oneFeedback(p.Status, "ACTIVE", "SUPERSEDED", "WITHDRAWN") || !oneFeedback(p.Decision, "ACCEPT", "REJECT", "NEEDS_CLARIFICATION", "WITHDRAW") {
		return w, p, asset.ErrInvalid
	}
	if _, e = time.Parse(time.RFC3339Nano, p.ReviewedAt); e != nil {
		return w, p, asset.ErrInvalid
	}
	a := p.Attribution
	if a.Verification != "INCONCLUSIVE" || a.Component != "UNKNOWN" || a.Action != "INDEPENDENT_HUMAN_REVIEW" || len(a.Causes) != 0 || len(a.Contradicting) != 0 || len(a.Uncertainty) == 0 || len(a.Uncertainty) > 3 || len(a.Observation) < 1 || len(a.Observation) > 3 || len(a.Supporting) > 1 {
		return w, p, asset.ErrInvalid
	}
	for _, v := range a.Observation {
		if !oneFeedback(v, "OBSERVED_OUTCOME_DISAGREEMENT", "STRONG_CONCLUSION_WITHOUT_REQUIRED_EVIDENCE", "INCONCLUSIVE") {
			return w, p, asset.ErrInvalid
		}
	}
	for _, v := range a.Uncertainty {
		if !oneFeedback(v, "ACTUAL_TOOL_AND_FINAL_BINDING_UNAVAILABLE", "ACTUAL_CONTEXT_CAPTURE_UNAVAILABLE", "HUMAN_BUSINESS_REVIEW_REQUIRED") {
			return w, p, asset.ErrInvalid
		}
	}
	for _, v := range a.Supporting {
		if v != "feedback-decision" {
			return w, p, asset.ErrInvalid
		}
	}
	for _, v := range []string{p.Observations.Tool, p.Observations.Context, p.Observations.Required} {
		if !oneFeedback(v, "AVAILABLE", "UNAVAILABLE") {
			return w, p, asset.ErrInvalid
		}
	}
	obs, _ := asset.Freeze(p.Observations)
	if obs.Digest() != p.EvidenceDigest {
		return w, p, asset.ErrInvalid
	}
	return w, p, nil
}
func feedbackDigest(s string) bool {
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
func oneFeedback(s string, all ...string) bool {
	for _, v := range all {
		if s == v {
			return true
		}
	}
	return false
}

func FetchFeedback(ctx context.Context, project, incident, feedback string) (FeedbackWire, FeedbackProjection, error) {
	var empty FeedbackWire
	var p FeedbackProjection
	var connections map[string]FeedbackConnection
	j, e := asset.ParseJSON([]byte(os.Getenv("AC_FEEDBACK_CONNECTIONS")))
	if e != nil || j.Decode(&connections) != nil {
		return empty, p, asset.ErrForbidden
	}
	c, ok := connections[project]
	u, e := url.Parse(c.BaseURL)
	if !ok || e != nil || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Path != "" && u.Path != "/" || !(u.Scheme == "https" || u.Scheme == "http" && oneFeedback(u.Hostname(), "127.0.0.1", "localhost", "::1")) || !asset.ValidID(incident) || !asset.ValidID(feedback) {
		return empty, p, asset.ErrForbidden
	}
	token := os.Getenv(c.TokenEnv)
	if token == "" || strings.ContainsAny(token, "\r\n") {
		return empty, p, asset.ErrForbidden
	}
	req, e := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(c.BaseURL, "/")+"/api/ci/incidents/"+incident+"/feedback/"+feedback+"/projection", nil)
	if e != nil {
		return empty, p, asset.ErrInvalid
	}
	req.Header.Set("Authorization", "Bearer "+token)
	client := http.Client{Timeout: 2 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	res, e := client.Do(req)
	if e != nil {
		return empty, p, asset.ErrConflict
	} // 未确认来源，禁止继续发布，不宣告成功。
	defer res.Body.Close()
	raw, e := io.ReadAll(io.LimitReader(res.Body, 32769))
	if e != nil || len(raw) > 32768 || res.StatusCode != 200 {
		return empty, p, asset.ErrConflict
	}
	w, p, e := DecodeFeedback(raw)
	if e != nil {
		return w, p, e
	}
	if p.Tenant != c.Tenant || p.Owner != c.Owner || p.IncidentID != incident || p.FeedbackID != feedback {
		return w, p, asset.ErrForbidden
	}
	return w, p, nil
}
