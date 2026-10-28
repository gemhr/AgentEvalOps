package featureimpact

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"

	"agentevalops/go-backend/internal/asset"
)

type agentSelector struct {
	ReportID string `json:"report_id"`
	RunID    string `json:"run_id"`
}

func validAgentSelector(in Input) bool {
	var s agentSelector
	var fields map[string]json.RawMessage
	return in.CandidateVersion == "1.1.0" && in.Report.Decode(&s) == nil &&
		json.Unmarshal(in.Report.Bytes(), &fields) == nil && len(fields) == 2 && s.ReportID != "" && s.RunID != ""
}

// 只接收已绑定 HTTP Target 从 AgentCore 权威库重验的投影；本函数不能代替服务端来源核验。
func validateAgentSource(in Input, out Output) error {
	if !validAgentSelector(in) {
		return asset.ErrInvalid
	}
	var selector agentSelector
	_ = in.Report.Decode(&selector)
	var source struct {
		Protocol      string   `json:"protocol"`
		ReportID      string   `json:"report_id"`
		RunID         string   `json:"run_id"`
		SessionID     string   `json:"session_id"`
		TaskID        string   `json:"task_id"`
		TaskVersion   int      `json:"task_version"`
		InputDigest   string   `json:"input_digest"`
		CorpusDigest  string   `json:"corpus_digest"`
		ReportDigest  string   `json:"report_digest"`
		Owner         string   `json:"owner_user_id"`
		Tenant        string   `json:"tenant_id"`
		Agent         string   `json:"agent_id"`
		Version       string   `json:"agent_version"`
		FinalStatus   string   `json:"final_status"`
		ReceiptDigest string   `json:"final_receipt_digest"`
		Raw           string   `json:"raw_output"`
		RawDigest     string   `json:"raw_output_digest"`
		Reads         []string `json:"read_sources"`
		TaskInputs    []string `json:"task_input_sources"`
	}
	if out.Sources.Decode(&source) != nil {
		return asset.ErrInvalid
	}
	rawHash := sha256.Sum256([]byte(source.Raw))
	receiptHash, err := hex.DecodeString(source.ReceiptDigest)
	if err != nil || len(receiptHash) != 32 || source.Protocol != "feature-impact-agent-source.v1" ||
		source.ReportID != selector.ReportID || source.RunID != selector.RunID || out.Envelope.RunID == nil || *out.Envelope.RunID != selector.RunID ||
		out.Envelope.Kind != "AGENT" || out.Envelope.Version != in.CandidateVersion || source.TaskID != in.TaskID || source.TaskVersion != out.Envelope.TaskVersion ||
		source.InputDigest != out.Envelope.InputDigest || source.CorpusDigest != out.Envelope.CorpusDigest ||
		source.ReportDigest != out.Envelope.ReportDigest || source.Owner == "" || source.Tenant == "" || source.SessionID == "" ||
		source.Agent != "feature_impact_investigator" || source.Version != in.CandidateVersion || source.FinalStatus != "DELIVERED" ||
		source.RawDigest != hex.EncodeToString(rawHash[:]) {
		return asset.ErrInvalid
	}
	var rawReport Report
	if Decode([]byte(source.Raw), &rawReport) != nil {
		return asset.ErrInvalid
	}
	// 与 Python schema 相同的唯一默认值；不能增加业务主张、操作或理由。
	for i := range rawReport.Findings {
		for k := range rawReport.Findings[i].Support {
			if rawReport.Findings[i].Support[k].Basis == "" {
				rawReport.Findings[i].Support[k].Basis = "TASK_INPUT"
			}
		}
		for k := range rawReport.Findings[i].Counter {
			if rawReport.Findings[i].Counter[k].Basis == "" {
				rawReport.Findings[i].Counter[k].Basis = "TASK_INPUT"
			}
		}
	}
	report, err := asset.Freeze(rawReport)
	if err != nil || report.Digest() != out.Envelope.ReportDigest {
		return asset.ErrInvalid
	}
	actual, err := asset.Freeze(out.Report)
	if err != nil || actual.Digest() != report.Digest() {
		return asset.ErrInvalid
	}
	return nil
}
