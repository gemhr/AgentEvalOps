package provider

import (
	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/worker"
	"context"
	"crypto/sha256"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"
)

type CIPairConfig struct {
	Protocol  string               `json:"protocol_version"`
	Quality   AgentQualityConfig   `json:"quality"`
	Frozen    catalog.CIPairFrozen `json:"frozen_ci"`
	Manifest  string               `json:"candidate_manifest_digest"`
	Mode      string               `json:"mode"`
	Prompt    string               `json:"prompt_digest"`
	Candidate string               `json:"candidate_id"`
}
type CIPairAuthorization struct {
	Schema      string    `json:"schema_version"`
	Attempt     string    `json:"attempt_id"`
	Run         string    `json:"evaluation_run_id"`
	Request     string    `json:"request_id"`
	Project     string    `json:"project_id"`
	Principal   string    `json:"service_principal_id"`
	Tenant      string    `json:"tenant_id"`
	Credential  string    `json:"credential_digest"`
	Scopes      []string  `json:"scopes"`
	Target      string    `json:"target_digest"`
	Plan        string    `json:"plan_id"`
	PlanVersion string    `json:"plan_version"`
	PlanDigest  string    `json:"plan_digest"`
	Dataset     asset.Ref `json:"dataset"`
	Case        asset.Ref `json:"case"`
	InputDigest string    `json:"input_digest"`
	Operator    string    `json:"operator_id"`
	Source      string    `json:"execution_source"`
	Mode        string    `json:"mode"`
	Prompt      string    `json:"prompt_digest"`
	Manifest    string    `json:"execution_manifest_digest"`
	Runtime     string    `json:"runtime_config_digest"`
}
type ciPairTarget struct {
	config        CIPairConfig
	authorization CIPairAuthorization
	reader        ev.Reader
}

func NewCIPairTarget(c CIPairConfig, a CIPairAuthorization, reader ev.Reader) (*CISyntheticTarget, error) {
	token := os.Getenv(c.Quality.TokenEnv)
	base := CISyntheticAttemptAuthorization{Schema: CISyntheticAuthorizationVersion, Attempt: a.Attempt, Run: a.Run, Project: a.Project, Case: a.Case.EntityID, TargetDigest: a.Target, Principal: a.Principal, Tenant: a.Tenant, CredentialDigest: a.Credential, Scopes: a.Scopes}
	legacy := CISyntheticConfig{Quality: c.Quality, Frozen: FrozenCI{CaseID: c.Frozen.CaseID}}
	if c.Protocol != aq.CIPairProtocol || c.Quality.Validate() != nil || c.Frozen.Validate() != nil || base.validate(legacy, token) != nil || a.Schema != "ci-synthetic-attempt-authorization.v2" || !asset.ValidID(a.Request) || a.Plan != c.Frozen.PlanID || a.PlanVersion != c.Frozen.PlanVersion || !ciDigest(a.PlanDigest) || reader == nil || a.Dataset != (asset.Ref{EntityID: c.Frozen.Dataset, Version: c.Frozen.DatasetVersion}) || a.Case != (asset.Ref{EntityID: c.Frozen.CaseID, Version: c.Frozen.CaseVersion}) || a.InputDigest != c.Frozen.InputDigest || !asset.Text(a.Operator) || a.Source != "OPERATOR_FROZEN_CI" || a.Mode != c.Mode || !onePairMode(c.Mode) || a.Prompt != c.Prompt || !ciDigest(c.Prompt) || !ciDigest(c.Manifest) || !strings.HasPrefix(c.Candidate, "ci-prompt-") || !ciDigest(strings.TrimPrefix(c.Candidate, "ci-prompt-")) || a.Manifest != c.Quality.Manifest.Digest() {
		return nil, asset.ErrInvalid
	}
	var manifest aq.QualityManifest
	_ = c.Quality.Manifest.Decode(&manifest)
	if a.Runtime != manifest.Agents["ci_incident_investigator"].ConfigDigest {
		return nil, asset.ErrInvalid
	}
	client, e := newClient(c.Quality.HTTP)
	if e != nil {
		return nil, e
	}
	return &CISyntheticTarget{client: client, token: token, pair: &ciPairTarget{config: c, authorization: a, reader: reader}}, nil
}
func onePairMode(mode string) bool { return mode == "BASELINE" || mode == "CANDIDATE" }

func VerifyCIPair(raw []byte, c CIPairConfig, correlation aq.QualityCorrelation, q ev.Request, s ev.Scope, planDigest string) (ev.Outcome, error) {
	out := ev.Outcome{Kind: ev.Unknown, Source: "LOCALAGENT_HTTP", Protocol: aq.CIPairProtocol, RequestID: q.ID, RemoteID: q.AttemptID, DispatchCertainty: "MAY_HAVE_DISPATCHED", TerminalCertainty: "UNCONFIRMED", ErrorCategory: string(ProtocolError)}
	j, e := asset.ParseJSON(raw)
	var w ciWire
	if e != nil || j.String() != string(raw) || aq.StrictQualityShape(raw, &w) != nil || w.Contract != aq.CIPairProtocol || w.Sidecar.Digest() != w.Digest {
		return out, asset.ErrInvalid
	}
	out, e = verifyAgentQualityResponse(s, q, correlation, out, w.Quality.Bytes(), 200)
	if e != nil || out.TerminalCertainty != "REMOTE_CONFIRMED" {
		return out, asset.ErrInvalid
	}
	out.Evidence = append(out.Evidence, binding(s, q, "ci-synthetic://"+q.AttemptID, aq.CIPairSidecarVersion, w.Sidecar))
	attempt := ev.Attempt{ID: q.AttemptID, RunID: q.RunID, RequestID: q.ID, Outcome: out.Kind, Request: q, Metadata: ev.AttemptMetadata{Observation: out}}
	evidence, e := aq.ReadCIDiagnosisEvidence(worker.EvaluationInput{Scope: s, Attempt: attempt})
	if e != nil || evidence.PlanDigest != planDigest {
		return ev.Outcome{}, asset.ErrInvalid
	}
	if _, e = aq.VerifyCISourceBinding(evidence.SourceBinding, evidence, c.Frozen, evidence.Execution, evidence.Incident, c.Mode); e != nil {
		return ev.Outcome{}, e
	}
	out.Cleanup, _ = asset.Freeze(map[string]any{"target_version": aq.CIPairTargetVersion, "quality_contract": AgentQualityProtocol, "sidecar_digest": w.Digest, "source_binding_digest": evidence.SourceBindingDigest, "plan_digest": planDigest})
	return out, nil
}
func (t *CISyntheticTarget) executePair(parent context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	c, a := t.pair.config, t.pair.authorization
	out := ev.Outcome{Kind: ev.Failure, Source: "LOCALAGENT_HTTP", Protocol: aq.CIPairProtocol, RequestID: q.ID, RemoteID: q.AttemptID, DispatchCertainty: "NOT_DISPATCHED", TerminalCertainty: "NOT_DISPATCHED", ErrorCategory: string(ProtocolError)}
	target, _ := asset.Freeze(q.Target)
	config, _ := asset.Freeze(c)
	input, e := catalog.ReadCIPairInput(q.Case.Case.Input)
	frozenInput, _ := asset.Freeze(c.Frozen.Input)
	actualInput, _ := asset.Freeze(input)
	if e != nil || catalog.ValidateCIDiagnosisCaseV2(q.Case.Case) != nil || frozenInput.String() != actualInput.String() || q.RunID != a.Run || q.AttemptID != a.Attempt || q.ID != a.Request || target.Digest() != a.Target || q.Target.ID != CISyntheticTargetID || q.Target.Version != aq.CIPairTargetVersion || q.Target.Kind != "LOCALAGENT_HTTP" || q.Target.Config.String() != config.String() || s.ProjectID != a.Project || q.Case.Identity.ProjectID != s.ProjectID || q.Case.Identity.Ref != a.Case || q.Case.Identity.ContentDigest != c.Frozen.CaseDigest {
		return out, nil
	}
	state, err := t.pair.reader.ReadRunState(parent, s.Scope, q.RunID)
	if err != nil || state.Run.ProjectID != s.ProjectID || state.Run.Snapshot.Input.Dataset == nil || state.Run.Snapshot.Input.Dataset.Ref != a.Dataset || state.Run.Snapshot.Retry.Version != "NO_RETRY.v1" || state.Run.Snapshot.Retry.MaxAttempts != 1 || state.Run.Snapshot.Retry.AllowUnknownFork || len(state.Attempts) != 1 || state.Attempts[0].ID != q.AttemptID || state.Attempts[0].RequestID != q.ID {
		return out, nil
	}
	persistedTarget, _ := asset.Freeze(state.Run.Snapshot.Target)
	persistedCase := asset.JSON{}
	if len(state.Run.Snapshot.Input.Manifest) == 1 {
		persistedCase, _ = asset.Freeze(state.Run.Snapshot.Input.Manifest[0])
	}
	requestCase, _ := asset.Freeze(q.Case)
	if state.Run.ID != a.Run || state.Run.Snapshot.Input.Origin != ev.PublishedCatalog || state.Run.Snapshot.Input.ProjectID != s.ProjectID || state.Run.Snapshot.Input.Dataset.ProjectID != s.ProjectID || persistedTarget.Digest() != a.Target || persistedCase.String() != requestCase.String() {
		return out, nil
	}
	var profile aq.QualityProfile
	_ = c.Quality.Profile.Decode(&profile)
	correlation := aq.QualityCorrelation{Agent: "ci_incident_investigator", Run: q.AttemptID, Project: s.ProjectID, EvaluationRun: q.RunID, Attempt: q.AttemptID, Request: q.ID, Case: c.Frozen.CaseID, CaseVersion: c.Frozen.CaseVersion, CaseDigest: c.Frozen.CaseDigest, ManifestDigest: c.Quality.Manifest.Digest(), ProfileID: profile.ID, ProfileVersion: profile.Version, ProfileDigest: c.Quality.Profile.Digest(), Timeout: q.Target.TimeoutMilliseconds, QueryDigest: fmt.Sprintf("%x", sha256.Sum256([]byte(aq.CIPairQuery)))}
	quality, _ := asset.Freeze(correlation)
	var payload map[string]any
	_ = quality.Decode(&payload)
	delete(payload, "query_digest")
	payload["query"] = aq.CIPairQuery
	// 只序列化可见 DTO，从不发送 Case/GroundTruth/acceptance/evaluator。
	body, _ := asset.Freeze(map[string]any{"contract_version": aq.CIPairProtocol, "quality": payload, "frozen_ci": c.Frozen, "candidate_manifest_digest": c.Manifest, "mode": c.Mode, "plan_digest": a.PlanDigest})
	ctx, cancel := context.WithTimeout(parent, time.Duration(c.Quality.CallMilliseconds)*time.Millisecond)
	defer cancel()
	req, e := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(c.Quality.BaseURL, "/")+"/api/runtime/evaluation-execute/ci-synthetic/v2", nil)
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
	if response.StatusCode != 200 {
		return out, nil
	}
	if _, e = boundedBody(response.Body, c.Quality.MaxResponseBytes); e != nil {
		return out, nil
	}
	read, e := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(c.Quality.BaseURL, "/")+"/api/runtime/runs/"+q.AttemptID+"/ci-synthetic-evidence/v2", nil)
	if e != nil {
		return out, e
	}
	read.Header.Set("Authorization", "Bearer "+t.token)
	sealed, e := t.client.Do(read)
	if e != nil {
		return out, nil
	}
	defer sealed.Body.Close()
	if sealed.StatusCode != 200 {
		return out, nil
	}
	raw, e := boundedBody(sealed.Body, c.Quality.MaxResponseBytes)
	if e != nil {
		return out, nil
	}
	verified, e := VerifyCIPair(raw, c, correlation, q, s, a.PlanDigest)
	if e != nil {
		out.Reason = "CI_PAIR_SOURCE_INVALID"
		return out, nil
	}
	return verified, nil
}
