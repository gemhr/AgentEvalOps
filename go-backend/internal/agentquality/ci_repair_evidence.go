package agentquality

import (
	"bytes"
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"agentevalops/go-backend/internal/asset"
)

type repairRef struct {
	ID     string `json:"evidence_id"`
	Digest string `json:"digest"`
}

type repairInputBody struct {
	Schema            string               `json:"schema_version"`
	Incident          string               `json:"incident_id"`
	Execution         string               `json:"execution_id"`
	Investigation     string               `json:"investigation_id"`
	Native            string               `json:"run_id"`
	Case              string               `json:"case_id"`
	ReportDigest      string               `json:"diagnosis_report_digest"`
	NativeDigest      string               `json:"native_result_digest"`
	ExecutionDigest   string               `json:"execution_evidence_digest"`
	Software          string               `json:"software_version"`
	InterfaceVersion  string               `json:"interface_definition_version"`
	InterfaceRevision int                  `json:"interface_revision"`
	InterfacePath     string               `json:"interface_relative_path"`
	InterfaceDigest   string               `json:"interface_definition_digest"`
	ScriptPath        string               `json:"script_relative_path"`
	ScriptDigest      string               `json:"script_baseline_digest"`
	Paths             []string             `json:"allowed_paths"`
	Fields            []string             `json:"allowed_change_fields"`
	Refs              map[string]repairRef `json:"evidence_refs"`
	Mechanism         string               `json:"failure_mechanism"`
	Instruction       string               `json:"repair_instruction"`
	Admission         string               `json:"admission_result"`
}

type repairReport struct {
	Schema    string      `json:"schema_version"`
	Kind      string      `json:"kind"`
	Incident  string      `json:"incident_id"`
	Status    string      `json:"diagnosis_status"`
	Stage     string      `json:"failure_stage"`
	Mechanism string      `json:"failure_mechanism"`
	Refs      []repairRef `json:"supporting_evidence"`
	Owner     string      `json:"recommended_owner_role"`
}

func repairEvidenceID(key string) string {
	ns, _ := hex.DecodeString("6ba7b8119dad11d180b400c04fd430c8")
	hash := sha1.Sum(append(ns, key...))
	hash[6] = hash[6]&0x0f | 0x50
	hash[8] = hash[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", hash[:4], hash[4:6], hash[6:8], hash[8:10], hash[10:16])
}

func verifyRepairEvidence(ctx context.Context, raw string, loc RepairLocator) (repairArtifact, repairReceipt, string, error) {
	var w repairEvidence
	var a repairArtifact
	var r repairReceipt
	var input repairInputBody
	var report repairReport
	if !loc.Valid() || RepairSHA256(raw) != loc.EvidenceDigest || repairDecode([]byte(raw), &w) != nil ||
		repairDecode(w.Artifact, &a) != nil || repairDecode(w.Receipt, &r) != nil ||
		repairDecode([]byte(w.RepairInput), &input) != nil || repairDecode(w.Native.Report, &report) != nil {
		return a, r, "", asset.ErrInvalid
	}
	if w.Schema != RepairWire || w.Workflow.ID != loc.WorkflowID || !asset.ValidID(w.Workflow.Space) ||
		!asset.ValidID(w.Workflow.Owner) || w.Workflow.Tenant == "" || w.Workflow.Definition == "" || w.Workflow.Version == "" ||
		w.Incident.ID != r.Incident || w.Incident.Status != "OPEN" || w.Incident.Execution != w.Execution.ID ||
		w.Execution.ID != r.Execution || w.Execution.CaseID != RepairPrimary || w.Execution.Outcome != "FAIL" ||
		w.Execution.Scenario != "stale_case_input_file" || w.Execution.Version != "1" ||
		w.Native.InvestigationID != r.Investigation || w.Native.RunID != r.Native ||
		w.Native.SessionID != r.Workflow+":diagnose" || w.Native.Agent != "ci_incident_investigator" ||
		w.Native.Version != "1.0.0" || !repairHex(w.Native.Digest, 32) || !repairHex(w.Native.ReportDigest, 32) ||
		!repairHex(w.Execution.Digest, 32) || report.Schema != "ci-diagnosis.v1" || report.Kind != "REPORT" ||
		report.Incident != r.Incident || report.Status != "SUPPORTED" || report.Stage != "AUTOMATION" ||
		report.Mechanism != staleTLV || report.Owner != "TEST_OWNER" {
		return a, r, "", asset.ErrInvalid
	}
	if r.Schema != "ci-repair-replay.v1" || r.Step != "replay" || r.Attempt != 1 || r.Workflow != w.Workflow.ID ||
		r.ID != repairEvidenceID(r.Workflow+":replay:1") || r.Technical != "COMPLETED" || r.Oracle != "tlv-oracle.v1" ||
		!repairHex(r.Digest, 32) || r.Digest != repairObjectDigest(w.Receipt, "receipt_digest") || len(r.Results) != 2 ||
		len(r.Results["baseline"]) != 2 || len(r.Results["candidate"]) != 2 || w.Approval.ReplayID != r.ID ||
		w.Approval.ID != r.Approval || w.Approval.Digest != r.ApprovalDigest || w.Approval.Status != "APPROVED" ||
		!asset.ValidID(w.Approval.Actor) || RepairSHA256(w.Approval.Binding) != r.ApprovalDigest {
		return a, r, "", asset.ErrInvalid
	}
	decided, dErr := time.Parse(time.RFC3339Nano, w.Approval.Decided)
	consumed, cErr := time.Parse(time.RFC3339Nano, w.Approval.Consumed)
	if dErr != nil || cErr != nil || consumed.Before(decided) {
		return a, r, "", asset.ErrInvalid
	}
	if a.Schema != "ci-repair-artifact.v1" || a.Step != "repair" || a.Attempt != 1 || a.Workflow != r.Workflow ||
		a.ID != r.Workflow+":repair:1" || a.OpenCode != r.OpenCode || a.Revision != r.Revision ||
		(!repairHex(a.Revision, 20) && !repairHex(a.Revision, 32)) || !repairHex(a.Tree, len(a.Revision)/2) ||
		a.Instance == "" || a.Session == "" || a.User == "" || a.Assistant == "" || a.User == a.Assistant ||
		!repairHex(a.Digest, 32) || a.Digest != r.ArtifactDigest || a.Digest != repairObjectDigest(w.Artifact, "artifact_digest") ||
		a.DiffDigest != r.DiffDigest || a.DiffDigest != RepairSHA256(a.Diff) ||
		a.BeforeDigest != r.BeforeDigest || a.BeforeDigest != baselineDigest || a.BeforeDigest != RepairSHA256(a.Before) ||
		a.AfterDigest != r.AfterDigest || a.AfterDigest != RepairSHA256(a.After) ||
		r.InterfaceDigest != interfaceDigest || r.InterfaceDigest != RepairSHA256(w.Interface) ||
		r.ControlDigest != controlDigest || r.ControlDigest != RepairSHA256(w.Control) ||
		!slices.Equal(a.Allowed, []string{repairPath}) || !slices.Equal(a.Changed, []string{repairPath}) {
		return a, r, "", asset.ErrInvalid
	}
	if input.Schema != "repair-input.v1" || input.Execution != r.Execution || input.Incident != r.Incident ||
		input.Investigation != r.Investigation || input.Native != r.Native || input.NativeDigest != w.Native.Digest ||
		input.ReportDigest != w.Native.ReportDigest || w.Native.ProjectionDigest != RepairSHA256(string(w.Native.Report)) ||
		input.ExecutionDigest != w.Execution.Digest ||
		input.InterfaceDigest != r.InterfaceDigest || input.ScriptDigest != r.BeforeDigest ||
		input.InterfacePath != "tlv-interface.json" || input.ScriptPath != repairPath || input.Case != RepairPrimary ||
		input.InterfaceVersion != "tlv-interface.v1" || input.InterfaceRevision != 2 || input.Software != "product-2.0" ||
		input.Mechanism != staleTLV || input.Admission != "ADMITTED" || input.Instruction == "" ||
		!slices.Equal(input.Fields, []string{repairField}) || !slices.Equal(input.Paths, a.Allowed) ||
		RepairSHA256(w.RepairInput) != r.InputDigest || len(input.Refs) != 4 {
		return a, r, "", asset.ErrInvalid
	}
	for _, kind := range []string{"PARSER_LOG", "ENVIRONMENT_RECORD", "TLV_INTERFACE_DEFINITION", "SCRIPT_BASELINE"} {
		ref, ok := input.Refs[kind]
		hash, hashErr := hex.DecodeString(ref.Digest)
		if !ok || ref.ID != repairEvidenceID(r.Execution+":"+kind) || hashErr != nil || len(hash) != 32 ||
			!slices.Contains(report.Refs, ref) {
			return a, r, "", asset.ErrInvalid
		}
	}
	for _, id := range []string{r.Incident, r.Execution, r.Investigation, r.Native, r.OpenCode, r.Approval} {
		if !asset.ValidID(id) {
			return a, r, "", asset.ErrInvalid
		}
	}
	var before, after repairScript
	if repairDecode([]byte(a.Before), &before) != nil || repairDecode([]byte(a.After), &after) != nil ||
		before.CaseID != RepairPrimary || after.CaseID != before.CaseID || !validRepairApproval(w, a, r) ||
		!applyRepairPatch(ctx, a) {
		return a, r, "", asset.ErrInvalid
	}
	return a, r, w.Control, nil
}

type repairApprovalBinding struct {
	Workflow         string `json:"workflow_run_id"`
	Definition       string `json:"workflow_id"`
	Version          string `json:"workflow_version"`
	DefinitionDigest string `json:"definition_digest"`
	Tenant           string `json:"tenant_id"`
	Owner            string `json:"owner_user_id"`
	Phase            string `json:"approval_phase"`
	Producer         struct {
		Step    string `json:"step_id"`
		Attempt int    `json:"attempt"`
		Run     string `json:"platform_run_id"`
		Result  struct {
			Kind     string `json:"kind"`
			Instance string `json:"instance_id"`
			Session  string `json:"session_id"`
			User     string `json:"user_message_id"`
			Message  string `json:"message_id"`
		} `json:"result_ref"`
		Artifact  json.RawMessage `json:"artifact_ref"`
		Directory json.RawMessage `json:"artifact_directory"`
	} `json:"producer"`
	Replay struct {
		Step    string          `json:"step_id"`
		Attempt int             `json:"attempt"`
		Agent   string          `json:"agent_id"`
		Version string          `json:"agent_version"`
		Kind    string          `json:"runtime_kind"`
		Bound   json.RawMessage `json:"bound_input"`
		Digest  string          `json:"input_digest"`
	} `json:"replay"`
}

func validRepairApproval(w repairEvidence, a repairArtifact, r repairReceipt) bool {
	var bind repairApprovalBinding
	var locator struct {
		Schema   string `json:"schema_version"`
		Workflow string `json:"workflow_run_id"`
		Artifact string `json:"artifact_digest"`
		Diff     string `json:"diff_digest"`
		Revision string `json:"baseline_revision"`
	}
	var raw string
	if repairDecode([]byte(w.Approval.Binding), &bind) != nil || json.Unmarshal(bind.Replay.Bound, &raw) != nil ||
		repairDecode([]byte(raw), &locator) != nil || RepairSHA256(string(bind.Replay.Bound)) != bind.Replay.Digest {
		return false
	}
	p := bind.Producer
	return bind.Workflow == r.Workflow && bind.Definition == w.Workflow.Definition && bind.Version == w.Workflow.Version &&
		bind.Tenant == w.Workflow.Tenant && bind.Owner == w.Workflow.Owner && bind.DefinitionDigest != "" && bind.Phase == "AFTER_STEP_OUTPUT" &&
		p.Step == a.Step && p.Attempt == a.Attempt && p.Run == a.OpenCode && string(p.Directory) == "null" &&
		p.Result.Kind == "OPENCODE_MESSAGE" && p.Result.Instance == a.Instance && p.Result.Session == a.Session &&
		p.Result.User == a.User && p.Result.Message == a.Assistant && bytes.Equal(p.Artifact, w.Artifact) &&
		bind.Replay.Step == r.Step && bind.Replay.Attempt == r.Attempt && bind.Replay.Kind == "REPLAY" &&
		locator.Schema == a.Schema && locator.Workflow == r.Workflow && locator.Artifact == a.Digest &&
		locator.Diff == a.DiffDigest && locator.Revision == a.Revision
}

func applyRepairPatch(ctx context.Context, a repairArtifact) bool {
	const limit = 16384
	for _, raw := range []string{a.Before, a.After, a.Diff} {
		if len(raw) == 0 || len(raw) > limit || strings.ContainsRune(raw, '\r') {
			return false
		}
	}
	dir, err := os.MkdirTemp("", "ci-repair-eval-")
	if err != nil {
		return false
	}
	defer os.RemoveAll(dir)
	target := filepath.Join(dir, filepath.FromSlash(repairPath))
	if os.Mkdir(filepath.Dir(target), 0700) != nil || os.WriteFile(target, []byte(a.Before), 0600) != nil {
		return false
	}
	ctx, cancel := context.WithTimeout(ctx, 4*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", "-c", "core.autocrlf=false", "-c", "core.eol=lf",
		"-c", "core.attributesFile="+os.DevNull, "apply", "--numstat", "--summary", "-z", "--apply", "--whitespace=nowarn", "-")
	cmd.Dir = dir
	for _, key := range []string{"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"} {
		if value := os.Getenv(key); value != "" {
			cmd.Env = append(cmd.Env, key+"="+value)
		}
	}
	cmd.Env = append(cmd.Env, "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL="+os.DevNull, "GIT_ATTR_NOSYSTEM=1",
		"GIT_CEILING_DIRECTORIES="+filepath.Dir(dir), "GIT_TERMINAL_PROMPT=0", "LC_ALL=C")
	cmd.Stdin = strings.NewReader(a.Diff)
	out, err := cmd.Output()
	stats := strings.SplitN(string(out), "\t", 3)
	if err != nil || len(stats) != 3 || stats[2] != repairPath+"\x00" || stats[0] == "-" || stats[1] == "-" {
		return false
	}
	b, err := os.ReadFile(target)
	return err == nil && bytes.Equal(b, []byte(a.After))
}
