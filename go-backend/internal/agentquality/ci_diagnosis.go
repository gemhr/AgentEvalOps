package agentquality

import (
	"context"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"reflect"
	"unicode/utf8"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
)

const CIDiagnosisImplementation = "ci-diagnosis-deterministic.v1"
const CIDiagnosisInput = "ci-diagnosis-quality-input.v1"
const CIDiagnosisResult = "ci-diagnosis-result.v1"
const CIDiagnosisNormalization = "ci-diagnosis-exact-fields.v1"

// 原 agent-quality-session UUIDv5 合同；只重验已有绑定，不创建 Session。
func diagnosisSession(attempt string) string {
	ns, _ := hex.DecodeString("6ba7b8119dad11d180b400c04fd430c8")
	sum := sha1.Sum(append(ns, []byte("agent-quality-session:"+attempt)...))
	b := sum[:16]
	b[6] = (b[6] & 15) | 80
	b[8] = (b[8] & 63) | 128
	s := hex.EncodeToString(b)
	return s[:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:]
}

type diagnosisReference struct {
	ID     string `json:"evidence_id"`
	Digest string `json:"digest"`
}
type diagnosisReport struct {
	Schema        string               `json:"schema_version"`
	Kind          string               `json:"kind"`
	Incident      string               `json:"incident_id"`
	Status        string               `json:"diagnosis_status"`
	Stage         string               `json:"failure_stage"`
	Hypothesis    string               `json:"root_cause_hypothesis"`
	Evidence      []diagnosisReference `json:"supporting_evidence"`
	Uncertainties []string             `json:"uncertainties"`
	Checks        []string             `json:"recommended_checks"`
	Owner         string               `json:"recommended_owner_role"`
}

// 原 v1 Wire 的只读形状；不添加字段、不放宽 Provider 的原校验。
type diagnosisSidecar struct {
	Schema  string `json:"schema_version"`
	Binding struct {
		Contract      string             `json:"contract_version"`
		Source        string             `json:"source_type"`
		Run           string             `json:"run_id"`
		Session       string             `json:"session_id"`
		Agent         string             `json:"agent_id"`
		Version       string             `json:"agent_version"`
		Instance      string             `json:"instance_id"`
		Execution     string             `json:"execution_id"`
		Incident      string             `json:"incident_id"`
		Investigation string             `json:"investigation_id"`
		ReportVersion int                `json:"report_version"`
		Frozen        asset.JSON         `json:"frozen_ci"`
		Manifest      string             `json:"manifest_digest"`
		Mode          string             `json:"mode"`
		Correlation   QualityCorrelation `json:"quality_correlation"`
	} `json:"binding"`
	Models []struct {
		Ref   string `json:"model_ref"`
		Frame string `json:"frame_ref"`
		Run   string `json:"run_id"`
		State string `json:"model_state"`
		Proof struct {
			Schema    string `json:"schema_version"`
			Candidate string `json:"candidate_id"`
			Manifest  string `json:"manifest_digest"`
			Mode      string `json:"mode"`
			Prompt    string `json:"prompt_digest"`
			Request   string `json:"model_request_digest"`
			Messages  string `json:"messages_digest"`
			System    string `json:"system_digest"`
			Scope     string `json:"evidence_scope"`
		} `json:"proof"`
	} `json:"models"`
	Quality string  `json:"quality_digest"`
	Report  *string `json:"report_digest"`
	Status  string  `json:"runtime_status"`
}

func CIDiagnosisSupported(d metric.EvaluatorDefinition) bool {
	var c Config
	return d.Validate() == nil && d.Kind == metric.Deterministic && d.Availability != asset.Unsupported && d.ImplementationRef == CIDiagnosisImplementation && d.InputContract == CIDiagnosisInput && d.SchemaVersion == CIDiagnosisResult && d.Normalization == CIDiagnosisNormalization && d.Model == nil && d.PromptRef == nil && len(d.OutputMetrics) == 1 && d.Config.Decode(&c) == nil && c.Metric == "task_success.v1" && c.K == 0
}

// CIDiagnosisEvidence 由原持久 Observation 重验得到，不接受客户端报告/分数。
type CIDiagnosisEvidence struct {
	Bundle                                             QualityBundle
	Observations                                       QualityObservations
	QualityDigest, SidecarDigest, LoadDigest, Incident string
	SourceBinding                                      asset.JSON
	SourceBindingDigest, Execution, Mode               string
	PlanDigest                                         string
	Answer                                             string
}

func completeCIDiagnosisEvidence(e CIDiagnosisEvidence) bool {
	if e.Bundle.Coverage != "COMPLETE" || len(e.Bundle.Gaps) > 0 || e.Observations.Coverage != "COMPLETE" || len(e.Observations.Gaps) > 0 {
		return false
	}
	for _, c := range e.Bundle.Components {
		if c.Kind == "execution" && c.Availability == "AVAILABLE" {
			verdict, _ := executionIntegrity(c.Body, e.Bundle, e.Observations)
			return verdict == "PASS"
		}
	}
	return false
}

func ReadCIDiagnosisEvidence(in worker.EvaluationInput) (CIDiagnosisEvidence, error) {
	var out CIDiagnosisEvidence
	invalid := func() (CIDiagnosisEvidence, error) { return out, asset.ErrInvalid }
	a := in.Attempt
	artifact := a.Metadata.Observation.Artifact
	if a.Outcome != ev.Success || artifact == nil || artifact.Schema != "artifact.v1" || artifact.Ref != "final-answer://"+a.ID || artifact.Availability != "AVAILABLE" || !bound(*artifact, in) || artifact.Body.Decode(&out.Answer) != nil {
		return invalid()
	}
	var quality, side asset.JSON
	nq, ns := 0, 0
	for _, b := range a.Metadata.Observation.Evidence {
		if !bound(b, in) {
			return invalid()
		}
		if b.Schema == QualitySchema {
			nq++
			if b.Availability == "AVAILABLE" {
				quality = b.Body
			}
		}
		if b.Schema == "ci-synthetic-evidence.v1" || a.Request.Target.Version == CIPairTargetVersion && b.Schema == CIPairSidecarVersion {
			ns++
			if b.Availability == "AVAILABLE" {
				side = b.Body
			}
		}
	}
	var b QualityBundle
	var s diagnosisSidecar
	var config, qualityConfig map[string]asset.JSON
	pair := a.Request.Target.Version == CIPairTargetVersion
	protocol, sideSchema := "ci-synthetic-evaluation-execute.v1", "ci-synthetic-evidence.v1"
	parseSide := side
	if pair {
		protocol, sideSchema = CIPairProtocol, CIPairSidecarVersion
		var fields map[string]asset.JSON
		if side.Decode(&fields) != nil {
			return invalid()
		}
		var sourceDigest string
		if fields["source_binding_digest"].Decode(&sourceDigest) != nil || sourceDigest != fields["source_binding"].Digest() {
			return invalid()
		}
		out.SourceBinding = fields["source_binding"]
		out.SourceBindingDigest = sourceDigest
		var bindingFields map[string]asset.JSON
		if fields["binding"].Decode(&bindingFields) != nil || bindingFields["plan_digest"].Decode(&out.PlanDigest) != nil || !loopDigest.MatchString(out.PlanDigest) {
			return invalid()
		}
		delete(bindingFields, "plan_digest")
		fields["binding"], _ = asset.Freeze(bindingFields)
		delete(fields, "source_binding")
		delete(fields, "source_binding_digest")
		parseSide, _ = asset.Freeze(fields)
	}
	if nq != 1 || ns != 1 || StrictQualityShape(quality.Bytes(), &b) != nil || StrictQualityShape(parseSide.Bytes(), &s) != nil || a.Request.Target.ID != "ci-synthetic-http" || (!pair && a.Request.Target.Version != "ci-synthetic-target.v2") || a.Request.Target.Config.Decode(&config) != nil || config["quality"].Decode(&qualityConfig) != nil {
		return invalid()
	}
	if pair {
		var protocol string
		if len(config) != 7 || config["protocol_version"].Decode(&protocol) != nil || protocol != CIPairProtocol {
			return invalid()
		}
		for _, key := range []string{"protocol_version", "quality", "frozen_ci", "candidate_manifest_digest", "mode", "prompt_digest", "candidate_id"} {
			if _, exists := config[key]; !exists {
				return invalid()
			}
		}
	}
	expected := b.Correlation
	ci := a.Request.Case.Identity
	var profile QualityProfile
	var project string
	if qualityConfig["trusted_project_id"].Decode(&project) != nil || project != in.Scope.ProjectID {
		return invalid()
	}
	if StrictQualityShape(qualityConfig["capture_profile"].Bytes(), &profile) != nil {
		return invalid()
	}
	queryText := "请调查固定的公开合成 CI 失败，使用授权工具取证。"
	if pair {
		queryText = CIPairQuery
	}
	query := fmt.Sprintf("%x", sha256.Sum256([]byte(queryText))) // 原 v1 固定 Query。
	if expected.Agent != "ci_incident_investigator" || expected.Run != a.ID || expected.Attempt != a.ID || expected.Project != in.Scope.ProjectID || expected.EvaluationRun != a.RunID || expected.Request != a.RequestID || expected.Case != ci.Ref.EntityID || expected.CaseVersion != ci.Ref.Version || expected.CaseDigest != ci.ContentDigest || expected.ManifestDigest != qualityConfig["execution_manifest"].Digest() || expected.ProfileDigest != qualityConfig["capture_profile"].Digest() || expected.ProfileID != profile.ID || expected.ProfileVersion != profile.Version || expected.Timeout != a.Request.Target.TimeoutMilliseconds || expected.QueryDigest != query {
		return invalid()
	}
	var e error
	out.Bundle, out.Observations, e = DecodeQuality(quality, expected)
	if e != nil || b.Final == nil || b.Final.Digest != fmt.Sprintf("%x", sha256.Sum256([]byte(out.Answer))) {
		return invalid()
	}
	var manifest QualityManifest
	_ = b.Manifest.Decode(&manifest)
	var candidate, prompt, mode, cm string
	_ = config["candidate_id"].Decode(&candidate)
	_ = config["prompt_digest"].Decode(&prompt)
	_ = config["mode"].Decode(&mode)
	_ = config["candidate_manifest_digest"].Decode(&cm)
	sb := s.Binding
	if sb.Session != diagnosisSession(a.ID) {
		return invalid()
	}
	if s.Schema != sideSchema || sb.Contract != protocol || sb.Source != "SYNTHETIC" || sb.Run != a.ID || sb.Agent != expected.Agent || sb.Version != manifest.Agents[expected.Agent].Version || sb.ReportVersion != 1 || sb.Mode != mode || sb.Manifest != cm || sb.Frozen.String() != config["frozen_ci"].String() || !reflect.DeepEqual(sb.Correlation, expected) || s.Status != b.Terminal.Status || s.Report == nil || *s.Report != b.Final.Digest {
		return invalid()
	}
	for _, id := range []string{sb.Session, sb.Instance, sb.Execution, sb.Incident, sb.Investigation} {
		if !asset.ValidID(id) {
			return invalid()
		}
	}
	var frozen map[string]asset.JSON
	if sb.Frozen.Decode(&frozen) != nil {
		return invalid()
	}
	for k, v := range map[string]string{"case_id": ci.Ref.EntityID, "case_version": ci.Ref.Version, "case_digest": ci.ContentDigest} {
		var actual string
		if frozen[k].Decode(&actual) != nil || actual != v {
			return invalid()
		}
	}
	if pair {
		var f catalog.CIPairFrozen
		if sb.Frozen.Decode(&f) != nil || f.Validate() != nil || f.InputDigest != a.Request.Case.Case.Input.Digest() {
			return invalid()
		}
		actual, _ := asset.Freeze(f.Input)
		if actual.String() != a.Request.Case.Case.Input.String() {
			return invalid()
		}
	} else {
		var visible map[string]asset.JSON
		if a.Request.Case.Case.Input.Decode(&visible) != nil || len(visible) != 3 {
			return invalid()
		}
		for _, k := range []string{"template_id", "template_version", "input_digest"} {
			if visible[k].String() != frozen[k].String() {
				return invalid()
			}
		}
	}
	// Quality response 可由原 Bundle + 最终正文无损重建，验证 Sidecar 原摘要。
	answer, _ := asset.Freeze(map[string]any{"schema_version": "final-answer-evidence.v1", "evidence_id": "final-answer://" + a.ID, "run_id": a.ID, "attempt_id": a.ID, "media_type": "text/plain; charset=utf-8", "content_sha256": b.Final.Digest, "content": out.Answer})
	wire, _ := asset.Freeze(map[string]any{"protocol_version": "localagent-agent-quality-evaluation-execute.v1", "run_id": a.ID, "status": b.Terminal.Status, "stop_reason": b.Terminal.Stop, "capture_status": b.Coverage, "bundle": quality, "bundle_digest": quality.Digest(), "final_answer_evidence": answer})
	if wire.Digest() != s.Quality || len(s.Models) == 0 {
		return invalid()
	}
	requests := map[string]bool{}
	for _, m := range s.Models {
		p := m.Proof
		if requests[p.Request] || m.Run != a.ID || p.Schema != "ci-prompt-selection.v1" || p.Scope != "DURABLE_REQUEST_PREPARED" || p.Candidate != candidate || p.Manifest != cm || p.Mode != mode || p.Prompt != prompt || !loopDigest.MatchString(p.System) || !loopDigest.MatchString(p.Request) || !loopDigest.MatchString(p.Messages) {
			return invalid()
		}
		frame, _ := asset.Freeze(m.Frame)
		found := false
		for _, actual := range out.Observations.Models {
			if actual.Request == p.Request && actual.Messages != nil && *actual.Messages == p.Messages && actual.Frame != nil && *actual.Frame == frame.Digest() && actual.State == m.State && actual.Root && actual.Purpose == "DECISION" && actual.Verified {
				found = true
			}
		}
		if !found {
			return invalid()
		}
		requests[p.Request] = true
	}
	for _, m := range out.Observations.Models {
		if m.Root && m.Purpose == "DECISION" && !requests[m.Request] {
			return invalid()
		}
	}
	load, _ := asset.Freeze(s.Models)
	out.QualityDigest = quality.Digest()
	out.SidecarDigest = side.Digest()
	out.LoadDigest = load.Digest()
	out.Incident = sb.Incident
	out.Execution = sb.Execution
	out.Mode = sb.Mode
	return out, nil
}

func evaluateCIDiagnosis(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if err := ctx.Err(); err != nil {
		return worker.EvaluationOutput{}, err
	}
	missing := func() (worker.EvaluationOutput, error) {
		return NonApplicable(in, asset.MissingEvidence, "task_success.v1", "DETERMINISTIC"), nil
	}
	c := in.Attempt.Request.Case.Case
	gt, e := catalog.ReadCIDiagnosisGroundTruth(c.GroundTruth)
	if e != nil || c.Capability != catalog.CIDiagnosisCapability || catalog.ValidateCIDiagnosisCase(c) != nil {
		return missing()
	}
	evidence, e := ReadCIDiagnosisEvidence(in)
	if e != nil || !completeCIDiagnosisEvidence(evidence) {
		return missing()
	}
	// 必需来源必须有已提交工具结果，并进入实际模型上下文；仅报告列出 ID 不构成证明。
	for _, source := range gt.Sources {
		found := false
		for _, tool := range evidence.Observations.Tools {
			if tool.Content == nil || *tool.Content != source.ContentDigest || tool.State != "COMMITTED" || tool.Observation == nil || tool.Result == nil || !loopDigest.MatchString(*tool.Result) || !tool.ScanComplete || !committedDiagnosisSource(evidence, tool) {
				continue
			}
			for _, model := range evidence.Observations.Models {
				if model.Verified && model.Root && model.State == "COMPLETED" && model.Frame != nil && *model.Frame == tool.Frame && model.AcceptedKind != nil {
					for _, used := range model.Sources {
						if used.Digest != nil && *used.Digest == *tool.Observation {
							found = true
						}
					}
				}
			}
		}
		if !found {
			return missing()
		}
	}
	return scoreCIDiagnosis(in, evidence, gt)
}

func scoreCIDiagnosis(in worker.EvaluationInput, evidence CIDiagnosisEvidence, gt catalog.CIDiagnosisGroundTruth) (worker.EvaluationOutput, error) {
	c := in.Attempt.Request.Case.Case
	var r diagnosisReport
	valid := StrictQualityShape([]byte(evidence.Answer), &r) == nil && r.Schema == "ci-diagnosis.v1" && r.Kind == "REPORT" && r.Incident == evidence.Incident && oneOf(r.Status, "SUPPORTED", "INSUFFICIENT_EVIDENCE") && oneOf(r.Stage, "PARSER", "COMPARISON", "AUTOMATION", "UNKNOWN") && oneOf(r.Owner, "TOOL_MAINTAINER", "TEST_OWNER", "UNKNOWN") && utf8.RuneCountInString(r.Hypothesis) >= 1 && utf8.RuneCountInString(r.Hypothesis) <= 500 && len(r.Evidence) <= 6 && len(r.Uncertainties) <= 4 && len(r.Checks) >= 1 && len(r.Checks) <= 4 && (r.Status != "SUPPORTED" || len(r.Evidence) > 0) && (r.Status != "INSUFFICIENT_EVIDENCE" || len(r.Uncertainties) > 0)
	for _, s := range append(append([]string{}, r.Uncertainties...), r.Checks...) {
		valid = valid && utf8.ValidString(s)
	}
	matched := map[string]bool{}
	for _, ref := range r.Evidence {
		known := false
		for _, s := range gt.Sources {
			if s.EvidenceID == ref.ID && s.Digest == ref.Digest {
				known = true
			}
		}
		valid = valid && known && !matched[ref.ID]
		matched[ref.ID] = true
	}
	if gt.Status == "SUPPORTED" {
		for _, s := range gt.Sources {
			valid = valid && matched[s.EvidenceID]
		}
	}
	verdict, task, score := "FAIL", TaskFailure, 0.0
	if valid && r.Status == gt.Status && r.Stage == gt.Stage && r.Owner == gt.Owner {
		verdict, task, score = "PASS", TaskSuccess, 1
	}
	v := BaseValue(in, verdict, "CI_DIAGNOSIS_EXACT_FIELDS")
	v.Score = &score
	j := TaskJudgment{Version: "task_success.v1", Decision: task, Method: "DETERMINISTIC", Reason: v.Reason, ProducerRef: in.Work.Metadata.Spec.Definition.ImplementationRef}
	v.Provenance, _ = asset.Freeze(map[string]any{"metric": "task_success.v1", "applicability": asset.Applicable, "task_success": j, "implementation_ref": in.Work.Metadata.Spec.Definition.ImplementationRef, "ground_truth_digest": c.GroundTruth.Digest(), "quality_digest": evidence.QualityDigest, "sidecar_digest": evidence.SidecarDigest, "actual_load_digest": evidence.LoadDigest})
	return worker.EvaluationOutput{Value: v}, nil
}

// 仅把必需来源关联到原 Execution Owner 的 action、已观察批次与后继请求。
func committedDiagnosisSource(e CIDiagnosisEvidence, t QualityTool) bool {
	var execution AgentExecution
	for _, c := range e.Bundle.Components {
		if c.Kind == "execution" {
			if StrictQualityShape(c.Body.Bytes(), &execution) != nil {
				return false
			}
		}
	}
	root, _ := asset.Freeze(e.Bundle.Correlation.Attempt)
	if execution.Run != root.Digest() {
		return false
	}
	if t.Invocation == nil || t.Action == nil || t.Call == nil || t.Observation == nil || t.Result == nil || t.Source != "tool:"+*t.Invocation {
		return false
	}
	matches := 0
	for _, a := range execution.Actions {
		if a.Ref != *t.Action {
			continue
		}
		matches++
		if a.Frame != t.Frame || a.Iteration != t.Iteration || a.Ordinal != t.Ordinal || a.Call != *t.Call || a.Invocation == nil || *a.Invocation != *t.Invocation || a.State != "COMMITTED" || a.Source == nil || *a.Source != *t.Result || a.Projection == nil || !loopDigest.MatchString(*a.Projection) || !loopDigest.MatchString(a.Recipe) {
			return false
		}
	}
	if matches != 1 {
		return false
	}
	observed := false
	for _, d := range execution.Decisions {
		if d.Frame == t.Frame && d.Iteration == t.Iteration && d.Kind == "TOOL_BATCH" && d.Observation != nil && *d.Observation == *t.Observation {
			observed = true
		}
	}
	if !observed {
		return false
	}
	for _, m := range e.Observations.Models {
		if m.Frame == nil || *m.Frame != t.Frame || m.Iteration == nil || *m.Iteration <= t.Iteration || !m.Root || !m.Verified || m.State != "COMPLETED" || m.AcceptedKind == nil {
			continue
		}
		accepted := false
		for _, d := range execution.Decisions {
			if "model:"+d.Model == m.Source && d.Frame == t.Frame && d.Iteration == *m.Iteration && d.Kind == *m.AcceptedKind {
				accepted = true
			}
		}
		if !accepted || !m.ScanComplete {
			continue
		}
		expectedRef, _ := asset.Freeze(fmt.Sprintf("source:%d", t.Iteration))
		for _, used := range m.Sources {
			if used.Ref != nil && *used.Ref == expectedRef.Digest() && used.Representation != nil && *used.Representation == "RAW" && used.Digest != nil && *used.Digest == *t.Observation {
				return true
			}
		}
	}
	return false
}
