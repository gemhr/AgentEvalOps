package agentquality

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
)

const (
	RepairWire           = "ci-repair-evidence.v1"
	RepairInput          = "ci-repair-case.v1"
	RepairImplementation = "ci-repair-oracle.v1"
	RepairNormalization  = "ci-repair-numeric.v1"
	RepairCorrectness    = "ci_repair.oracle_correctness.v1"
	RepairValidity       = "ci_repair.contract_validity.v1"
	RepairTargetVersion  = "ci-repair-sealed-http.v1"
	RepairPrimary        = "case-tlv-report"
	RepairControl        = "case-report-health"
	repairPath           = "cases/tlv-report.json"
	repairField          = "request_tlv_hex"
	staleTLV             = "TLV_INPUT_REVISION_STALE"
	interfaceDigest      = "07c546452af2ab277dedd74df9f9ed91e43161a8459b0ffafa985fec2579acd0"
	baselineDigest       = "848463116a4e06c2d2386f3c350ecc1bf9f2218fd4d24bb8317499a1ac1a5d6f"
	controlDigest        = "53a0b4014d334606e24406b4f97c847525fbd3b76f78ff1430961f34637db4d3"
)

type RepairLocator struct {
	WorkflowID     string `json:"workflow_run_id"`
	EvidenceDigest string `json:"evidence_digest"`
}

func (v RepairLocator) Valid() bool {
	return asset.ValidID(v.WorkflowID) && repairHex(v.EvidenceDigest, sha256.Size)
}

func repairHex(value string, size int) bool {
	raw, err := hex.DecodeString(value)
	return err == nil && len(raw) == size && value == strings.ToLower(value)
}

type RepairSubject struct {
	Subject     string `json:"subject"`
	Version     string `json:"subject_version"`
	Environment string `json:"environment"`
	Mode        string `json:"run_mode"`
	Role        string `json:"role"`
	RepairLocator
}

func NewRepairSubject(role string, loc RepairLocator) RepairSubject {
	return RepairSubject{"ci-repair-tlv", "1", "development-visible", "SEALED_RUN_REPLAY", role, loc}
}

func (s RepairSubject) Valid() bool {
	return (s.Role == "BASELINE" || s.Role == "CANDIDATE") && s.RepairLocator.Valid() && s == NewRepairSubject(s.Role, s.RepairLocator)
}

type RepairCase struct {
	Schema string `json:"schema_version"`
	CaseID string `json:"case_id"`
}

type RepairTruth struct {
	Version        string `json:"version"`
	Expected       int    `json:"expected_value"`
	Revision       int    `json:"revision"`
	Type           int    `json:"type"`
	Length         int    `json:"length"`
	BaselineReason string `json:"baseline_reason"`
}

func RepairCatalogCase(c, groundTruth asset.JSON) (RepairCase, RepairTruth, bool) {
	var cas RepairCase
	var gt RepairTruth
	if c.Decode(&cas) != nil || groundTruth.Decode(&gt) != nil || cas.Schema != RepairInput ||
		gt.Version != "tlv-ground-truth.v1" || gt.Revision != 2 || gt.Type != 0x11 || gt.Length != 2 {
		return cas, gt, false
	}
	ok := cas.CaseID == RepairPrimary && gt.Expected == 7 && gt.BaselineReason == staleTLV ||
		cas.CaseID == RepairControl && gt.Expected == 9 && gt.BaselineReason == ""
	return cas, gt, ok
}

type RepairTransport struct {
	ProjectID string `json:"project_id"`
	Raw       string `json:"raw"`
	Digest    string `json:"sha256"`
}

type repairResult struct {
	CaseID      string  `json:"case_id"`
	InputDigest string  `json:"input_digest"`
	Expected    int     `json:"expected_value"`
	Actual      *int    `json:"actual_value"`
	Revision    *int    `json:"revision"`
	Oracle      string  `json:"oracle_status"`
	Reason      *string `json:"reason"`
}

type repairEvidence struct {
	Schema   string `json:"schema_version"`
	Workflow struct {
		ID         string `json:"workflow_run_id"`
		Definition string `json:"workflow_id"`
		Version    string `json:"workflow_version"`
		Tenant     string `json:"tenant_id"`
		Space      string `json:"space_id"`
		Owner      string `json:"owner_user_id"`
	} `json:"workflow"`
	Incident struct {
		ID        string `json:"incident_id"`
		Execution string `json:"execution_id"`
		Status    string `json:"status"`
	} `json:"incident"`
	Execution struct {
		ID       string `json:"execution_id"`
		CaseID   string `json:"case_id"`
		Outcome  string `json:"outcome"`
		Scenario string `json:"scenario"`
		Version  string `json:"scenario_version"`
		Digest   string `json:"evidence_digest"`
	} `json:"execution"`
	Native struct {
		InvestigationID  string          `json:"investigation_id"`
		RunID            string          `json:"run_id"`
		SessionID        string          `json:"session_id"`
		Agent            string          `json:"agent_id"`
		Version          string          `json:"agent_version"`
		Report           json.RawMessage `json:"report"`
		ReportDigest     string          `json:"report_digest"`
		ProjectionDigest string          `json:"projection_digest"`
		Digest           string          `json:"result_digest"`
	} `json:"native"`
	RepairInput string `json:"repair_input"`
	Approval    struct {
		ID       string `json:"approval_id"`
		Digest   string `json:"binding_digest"`
		Status   string `json:"status"`
		Actor    string `json:"decided_by"`
		Decided  string `json:"decided_at"`
		Consumed string `json:"consumed_at"`
		ReplayID string `json:"replay_id"`
		Binding  string `json:"binding"`
	} `json:"approval"`
	Artifact  json.RawMessage `json:"artifact"`
	Interface string          `json:"interface"`
	Control   string          `json:"control"`
	Receipt   json.RawMessage `json:"receipt"`
}

type repairArtifact struct {
	Schema       string   `json:"schema_version"`
	ID           string   `json:"artifact_id"`
	Workflow     string   `json:"workflow_run_id"`
	Step         string   `json:"step_run_id"`
	Attempt      int      `json:"attempt"`
	OpenCode     string   `json:"opencode_run_id"`
	Instance     string   `json:"runtime_instance_id"`
	Session      string   `json:"external_session_id"`
	User         string   `json:"user_message_id"`
	Assistant    string   `json:"assistant_message_id"`
	Revision     string   `json:"baseline_revision"`
	Tree         string   `json:"baseline_tree"`
	Allowed      []string `json:"allowed_paths"`
	Changed      []string `json:"changed_paths"`
	Before       string   `json:"before"`
	After        string   `json:"after"`
	Diff         string   `json:"diff"`
	BeforeDigest string   `json:"before_sha256"`
	AfterDigest  string   `json:"after_sha256"`
	DiffDigest   string   `json:"diff_digest"`
	Digest       string   `json:"artifact_digest"`
	Explanation  string   `json:"explanation"`
}

type repairReceipt struct {
	Schema          string                    `json:"schema_version"`
	ID              string                    `json:"replay_id"`
	Workflow        string                    `json:"workflow_run_id"`
	Step            string                    `json:"step_run_id"`
	Attempt         int                       `json:"attempt"`
	Incident        string                    `json:"incident_id"`
	Execution       string                    `json:"execution_id"`
	Investigation   string                    `json:"investigation_id"`
	Native          string                    `json:"native_run_id"`
	OpenCode        string                    `json:"opencode_run_id"`
	Approval        string                    `json:"approval_id"`
	ApprovalDigest  string                    `json:"approval_binding_digest"`
	InputDigest     string                    `json:"repair_input_digest"`
	Revision        string                    `json:"baseline_revision"`
	ArtifactDigest  string                    `json:"artifact_digest"`
	DiffDigest      string                    `json:"diff_digest"`
	Oracle          string                    `json:"oracle_version"`
	InterfaceDigest string                    `json:"interface_definition_digest"`
	BeforeDigest    string                    `json:"before_sha256"`
	AfterDigest     string                    `json:"after_sha256"`
	ControlDigest   string                    `json:"control_sha256"`
	Results         map[string][]repairResult `json:"results"`
	Technical       string                    `json:"technical_status"`
	Digest          string                    `json:"receipt_digest"`
}

func RepairSHA256(raw string) string { return fmt.Sprintf("%x", sha256.Sum256([]byte(raw))) }

// 保留生产端规范 JSON 中每个 value 的原始字节，避免跨语言的转义差异。
func repairObjectDigest(raw json.RawMessage, omit string) string {
	var parts map[string]json.RawMessage
	if json.Unmarshal(raw, &parts) != nil || parts[omit] == nil {
		return ""
	}
	delete(parts, omit)
	keys := make([]string, 0, len(parts))
	for key := range parts {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	var b strings.Builder
	b.WriteByte('{')
	for i, key := range keys {
		if i > 0 {
			b.WriteByte(',')
		}
		name, _ := json.Marshal(key)
		b.Write(name)
		b.WriteByte(':')
		b.Write(parts[key])
	}
	b.WriteByte('}')
	return RepairSHA256(b.String())
}

func repairDecode(raw []byte, dst any) error {
	// ParseJSON 拒绝重复字段/错误 UTF-8，严格解码保留嵌套原始字节。
	if _, err := asset.ParseJSON(raw); err != nil {
		return err
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	return d.Decode(dst)
}

func RepairTransportValid(raw string, locator RepairLocator) bool {
	var evidence repairEvidence
	return locator.Valid() && RepairSHA256(raw) == locator.EvidenceDigest &&
		repairDecode([]byte(raw), &evidence) == nil && evidence.Schema == RepairWire &&
		evidence.Workflow.ID == locator.WorkflowID
}

func RepairSupported(d metric.EvaluatorDefinition) bool {
	var cfg Config
	return d.Validate() == nil && d.Availability != asset.Unsupported && d.Kind == metric.Deterministic &&
		d.ImplementationRef == RepairImplementation && d.InputContract == RepairInput && d.SchemaVersion == RepairWire &&
		d.Normalization == RepairNormalization && len(d.OutputMetrics) == 1 && d.Model == nil && d.PromptRef == nil &&
		d.Budget.MaxProviderCalls == 0 && d.Config.Decode(&cfg) == nil &&
		(cfg.Metric == RepairCorrectness || cfg.Metric == RepairValidity)
}

type repairScript struct {
	CaseID  string `json:"case_id"`
	Request string `json:"request_tlv_hex"`
}

func parseRepairResult(raw string, caseID string, gt RepairTruth) repairResult {
	out := repairResult{CaseID: caseID, InputDigest: RepairSHA256(raw), Expected: gt.Expected, Oracle: "FAIL"}
	reason := "TLV_INPUT_INVALID"
	out.Reason = &reason
	var script repairScript
	if strings.ContainsRune(raw, '\r') || repairDecode([]byte(raw), &script) != nil || script.CaseID != caseID {
		return out
	}
	b, err := hex.DecodeString(script.Request)
	if err != nil || len(b) < 2 || int(b[1]) != len(b)-2 {
		return out
	}
	var revision, value int
	switch {
	case len(b) == gt.Length+2 && int(b[0]) == gt.Type:
		revision, value = gt.Revision, int(b[2])<<8|int(b[3])
	case len(b) == 3 && b[0] == 0x10:
		revision, value = 1, int(b[2])
	default:
		return out
	}
	out.Revision, out.Actual = &revision, &value
	if revision != gt.Revision {
		reason = staleTLV
	} else if value != gt.Expected {
		reason = "TLV_BUSINESS_VALUE_MISMATCH"
	} else {
		out.Oracle, out.Reason = "PASS", nil
	}
	return out
}

func equalRepairResult(a, b repairResult) bool {
	x, _ := asset.Freeze(a)
	y, _ := asset.Freeze(b)
	return x.Digest() == y.Digest()
}

func evaluateRepair(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	var cfg Config
	_ = in.Work.Metadata.Spec.Definition.Config.Decode(&cfg)
	failure := func(state, code string) (worker.EvaluationOutput, error) {
		if state == "ERROR" {
			return worker.EvaluationOutput{Value: BaseValue(in, state, code)}, nil
		}
		return NonApplicable(in, asset.MissingEvidence, cfg.Metric, "DETERMINISTIC"), nil
	}
	c := in.Attempt.Request.Case.Case
	cas, gt, valid := RepairCatalogCase(c.Input, c.GroundTruth)
	var subject RepairSubject
	var transport RepairTransport
	b := in.Attempt.Metadata.Observation.Artifact
	if !valid || in.Run.Snapshot.Subject.Decode(&subject) != nil || !subject.Valid() {
		return failure("ERROR", "CI_REPAIR_SUBJECT_INVALID")
	}
	if b == nil || b.Schema != RepairWire || !bound(*b, in) || b.Availability != "AVAILABLE" || in.Attempt.Outcome != "SUCCESS" {
		return failure("INCONCLUSIVE", "CI_REPAIR_EVIDENCE_MISSING")
	}
	if b.Body.Decode(&transport) != nil || transport.ProjectID != in.Scope.ProjectID || transport.Digest != subject.EvidenceDigest {
		return failure("ERROR", "CI_REPAIR_EVIDENCE_INVALID")
	}
	artifact, receipt, control, err := verifyRepairEvidence(ctx, transport.Raw, subject.RepairLocator)
	if err != nil {
		return failure("ERROR", "CI_REPAIR_EVIDENCE_INVALID")
	}
	verdict := "PASS"
	metricPass := false
	before, after := artifact.Before, artifact.After
	if cas.CaseID == RepairControl {
		before, after = control, control
	}
	baseline, candidate := parseRepairResult(before, cas.CaseID, gt), parseRepairResult(after, cas.CaseID, gt)
	for _, pair := range []struct {
		role     string
		expected repairResult
	}{{"baseline", baseline}, {"candidate", candidate}} {
		index := 0
		if cas.CaseID == RepairControl {
			index = 1
		}
		if !equalRepairResult(pair.expected, receipt.Results[pair.role][index]) {
			return failure("ERROR", "CI_REPAIR_ORACLE_MISMATCH")
		}
	}
	actual := candidate
	if subject.Role == "BASELINE" {
		actual = baseline
	}
	if cfg.Metric == RepairCorrectness {
		metricPass = actual.Oracle == "PASS"
	} else if subject.Role == "BASELINE" && gt.BaselineReason != "" {
		metricPass = actual.Reason != nil && *actual.Reason == gt.BaselineReason && actual.Actual != nil && *actual.Actual == gt.Expected
	} else {
		metricPass = actual.Oracle == "PASS"
	}
	score := float64(1)
	if !metricPass {
		verdict, score = "FAIL", 0
	}
	v := BaseValue(in, verdict, "CI_REPAIR_ORACLE_SCORED")
	v.Score, v.SourceError = &score, ""
	v.Provenance, _ = asset.Freeze(map[string]any{"applicability": asset.Applicable, "metric": cfg.Metric,
		"workflow_run_id": receipt.Workflow, "evidence_digest": subject.EvidenceDigest, "replay_id": receipt.ID,
		"role": subject.Role, "case": actual, "ground_truth_digest": c.GroundTruth.Digest()})
	return worker.EvaluationOutput{Value: v}, nil
}
