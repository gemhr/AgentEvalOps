package catalog

import (
	"regexp"
	"slices"

	"agentevalops/go-backend/internal/asset"
	gov "agentevalops/go-backend/internal/cigovernance"
)

const CIDiagnosisCapability = "CI_DIAGNOSIS"
const CIDiagnosisGTVersion = "ci-diagnosis-ground-truth.v1"
const CIDiagnosisMetadataVersion = "ci-diagnosis-lineage.v1"

var diagnosisDigest = regexp.MustCompile(`^[0-9a-f]{64}$`)

// CIDiagnosisSource 是独立审核冻结的来源，不是模型报告自行声明的证据。
// ContentDigest 对应现有 QualityTool.content_digest（原工具 output.content 的 UTF-8 摘要）。
type CIDiagnosisSource struct {
	Kind          string `json:"kind"`
	EvidenceID    string `json:"evidence_id"`
	Digest        string `json:"digest"`
	ContentDigest string `json:"tool_content_digest"`
}
type CIDiagnosisGroundTruth struct {
	Version string              `json:"schema_version"`
	Status  string              `json:"diagnosis_status"`
	Stage   string              `json:"failure_stage"`
	Owner   string              `json:"required_owner_role"`
	Sources []CIDiagnosisSource `json:"required_sources"`
}

func ReadCIDiagnosisGroundTruth(j asset.JSON) (CIDiagnosisGroundTruth, error) {
	var g CIDiagnosisGroundTruth
	if j.Decode(&g) != nil || g.Version != CIDiagnosisGTVersion || !slices.Contains([]string{"SUPPORTED", "INSUFFICIENT_EVIDENCE"}, g.Status) || !slices.Contains([]string{"PARSER", "COMPARISON", "AUTOMATION", "UNKNOWN"}, g.Stage) || !slices.Contains([]string{"TOOL_MAINTAINER", "TEST_OWNER", "UNKNOWN"}, g.Owner) || len(g.Sources) == 0 || len(g.Sources) > 6 {
		return g, asset.ErrInvalid
	}
	seen := map[string]bool{}
	for _, s := range g.Sources {
		if !slices.Contains([]string{"PARSER_LOG", "ENVIRONMENT_RECORD", "CASE_REQUIREMENTS", "COMPARISON_LOG", "AUTOMATION_LOG"}, s.Kind) || !asset.Text(s.EvidenceID) || !diagnosisDigest.MatchString(s.Digest) || !diagnosisDigest.MatchString(s.ContentDigest) || seen[s.EvidenceID] {
			return g, asset.ErrInvalid
		}
		seen[s.EvidenceID] = true
	}
	return g, nil
}

// 仅安全元数据与审计摘要；不含题面、GT、家族到答案映射。
// Operator/ACL/AuditDigest 必须由受信发布/运维边界解析，调用方 bool 不是隔离证明。
type CIDiagnosisMetadata struct {
	Version           string `json:"schema_version"`
	Purpose           string `json:"purpose"`
	SourceDigest      string `json:"source_digest"`
	FamilyDigest      string `json:"mechanism_family_digest"`
	InputDigest       string `json:"input_digest"`
	GroundTruthDigest string `json:"ground_truth_digest"`
	ReviewID          string `json:"review_id"`
	Reviewer          string `json:"reviewer"`
	Developer         string `json:"candidate_developer"`
	FeedbackID        string `json:"feedback_id"`
	ProjectionDigest  string `json:"projection_digest"`
	AttributionDigest string `json:"attribution_digest"`
	Operator          string `json:"operator"`
	AuditDigest       string `json:"independence_audit_digest"`
	ACLDigest         string `json:"access_control_audit_digest"`
	Exposure          string `json:"exposure"`
	Retired           bool   `json:"retired"`
	DeveloperReadable bool   `json:"developer_readable"`
}

func ReadCIDiagnosisMetadata(j asset.JSON) (CIDiagnosisMetadata, error) {
	var fields map[string]asset.JSON
	var m CIDiagnosisMetadata
	if j.Decode(&fields) != nil || fields["ci_diagnosis"].Decode(&m) != nil || m.Version != CIDiagnosisMetadataVersion || !slices.Contains([]string{"GOLDEN", "FAILURE", "HOLDOUT"}, m.Purpose) {
		return m, asset.ErrInvalid
	}
	for _, d := range []string{m.SourceDigest, m.FamilyDigest, m.InputDigest, m.GroundTruthDigest} {
		if !diagnosisDigest.MatchString(d) {
			return m, asset.ErrInvalid
		}
	}
	if !asset.ValidID(m.ReviewID) || !asset.Text(m.Reviewer) || !asset.Text(m.Developer) || m.Reviewer == m.Developer {
		return m, asset.ErrInvalid
	}
	if m.Purpose == "FAILURE" && (!asset.ValidID(m.FeedbackID) || !diagnosisDigest.MatchString(m.ProjectionDigest) || !diagnosisDigest.MatchString(m.AttributionDigest)) {
		return m, asset.ErrInvalid
	}
	if m.Purpose == "HOLDOUT" && (!asset.Text(m.Operator) || m.Operator == m.Developer || !diagnosisDigest.MatchString(m.AuditDigest) || !diagnosisDigest.MatchString(m.ACLDigest) || m.Exposure != "UNEXPOSED" || m.Retired || m.DeveloperReadable) {
		return m, asset.ErrForbidden
	}
	return m, nil
}

func ValidateCIDiagnosisCase(c CaseContent) error {
	var header struct {
		Version string `json:"schema_version"`
	}
	var fields map[string]asset.JSON
	_ = c.GroundTruth.Decode(&fields)
	_ = fields["schema_version"].Decode(&header.Version)
	if header.Version == CIDiagnosisGTVersionV2 {
		return ValidateCIDiagnosisCaseV2(c)
	}
	if _, e := ReadCIDiagnosisGroundTruth(c.GroundTruth); e != nil {
		return e
	}
	m, e := ReadCIDiagnosisMetadata(c.Metadata)
	if e != nil {
		return e
	}
	if m.InputDigest != c.Input.Digest() || m.GroundTruthDigest != c.GroundTruth.Digest() || c.ExpectedOutput.String() != "null" {
		return asset.ErrInvalid
	}
	return nil
}

// ValidateCIDiagnosisHoldoutSet 只检查元数据资格，不签署 OS 隔离或独立资产真实存在。
func ValidateCIDiagnosisHoldoutSet(project string, dataset asset.Ref, caseRef asset.Ref, holdout CIDiagnosisMetadata, development []CIDiagnosisMetadata) error {
	wrapped, _ := asset.Freeze(map[string]any{"ci_diagnosis": holdout})
	if _, e := ReadCIDiagnosisMetadata(wrapped); e != nil || holdout.Purpose != "HOLDOUT" || gov.WP09Retired(dataset, []asset.Ref{caseRef}) || gov.WP11Retired(dataset, []asset.Ref{caseRef}) || gov.WP12BRetired(project, dataset) {
		return asset.ErrForbidden
	}
	for _, d := range development {
		if holdout.SourceDigest == d.SourceDigest || holdout.InputDigest == d.InputDigest || d.Purpose == "FAILURE" && holdout.FamilyDigest == d.FamilyDigest {
			return asset.ErrForbidden
		}
	}
	return nil
}

func ValidateCIDiagnosisCaseV2(c CaseContent) error {
	g, e := ReadCIDiagnosisGroundTruthV2(c.GroundTruth)
	if e != nil {
		return e
	}
	_, e = ReadCIPairInput(c.Input)
	if e != nil {
		return e
	}
	m, e := ReadCIDiagnosisMetadata(c.Metadata)
	if e != nil {
		return e
	}
	if c.ExpectedOutput.String() != "null" || g.InputDigest != c.Input.Digest() || m.InputDigest != g.InputDigest || m.GroundTruthDigest != c.GroundTruth.Digest() || g.ReviewID != m.ReviewID || g.Reviewer != m.Reviewer {
		return asset.ErrInvalid
	}
	for _, s := range g.Sources {
		if s.UpstreamRef != "review-item:"+m.ReviewID || s.UpstreamDigest != m.SourceDigest {
			return asset.ErrForbidden
		}
	}
	return nil
}
