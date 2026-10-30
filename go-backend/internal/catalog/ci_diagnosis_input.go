package catalog

import (
	"agentevalops/go-backend/internal/asset"
	"regexp"
	"slices"
)

const CIPairInputVersion = "ci-synthetic-case-input.v2"
const CISourceSemanticsVersion = "ci-diagnosis-source-semantics.v1"
const CIDiagnosisGTVersionV2 = "ci-diagnosis-ground-truth.v2"

var ciBusinessID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$`)
var ciBusinessIP = regexp.MustCompile(`^192\.0\.2\.[0-9]{1,3}$`)

type CIEnvironment struct {
	ID              string          `json:"environment_id"`
	IP              string          `json:"ip"`
	Software        string          `json:"software_version"`
	Parser          string          `json:"parser_version"`
	Decoder         string          `json:"decoder_table_version"`
	ParserRevision  int             `json:"parser_supported_field_revision"`
	DecoderRevision int             `json:"decoder_supported_field_revision"`
	Flags           map[string]bool `json:"feature_flags"`
	Buffer          string          `json:"buffer_behavior"`
}
type CIBusinessCase struct {
	ID            string  `json:"case_id"`
	Feature       string  `json:"feature_id"`
	Required      int     `json:"required_field_revision"`
	InputRevision int     `json:"input_revision"`
	Golden        string  `json:"golden_id"`
	GoldenVersion string  `json:"golden_version"`
	Rule          string  `json:"comparison_rule_id"`
	Compressed    *bool   `json:"compressed_transport,omitempty"`
	InputContract *string `json:"input_contract,omitempty"`
	Buffer        *string `json:"buffer_requirement,omitempty"`
}
type CIBusinessReport struct {
	ID       string `json:"report_artifact_id"`
	Revision int    `json:"field_revision"`
	Matches  bool   `json:"payload_matches_golden"`
}
type CIInitialState struct {
	Environment CIEnvironment    `json:"environment"`
	Case        CIBusinessCase   `json:"test_case"`
	Report      CIBusinessReport `json:"report"`
	Complete    bool             `json:"diagnostics_complete"`
}
type CIPairInput struct {
	Schema      string         `json:"schema_version"`
	Template    string         `json:"template_id"`
	Producer    string         `json:"producer_version"`
	State       CIInitialState `json:"initial_state"`
	StateDigest string         `json:"state_digest"`
}
type CIPairFrozen struct {
	CaseID         string      `json:"case_id"`
	CaseVersion    string      `json:"case_version"`
	CaseDigest     string      `json:"case_digest"`
	InputDigest    string      `json:"input_digest"`
	Dataset        string      `json:"dataset_id"`
	DatasetVersion string      `json:"dataset_version"`
	PlanID         string      `json:"plan_id"`
	PlanVersion    string      `json:"plan_version"`
	Source         string      `json:"source_ref"`
	Input          CIPairInput `json:"visible_input"`
}

func ReadCIPairInput(j asset.JSON) (CIPairInput, error) {
	var in CIPairInput
	if j.Decode(&in) != nil || in.Schema != CIPairInputVersion || in.Template != "operator-frozen-ci" || in.Producer != "ci-simulation.v2" {
		return in, asset.ErrInvalid
	}
	state, _ := asset.Freeze(in.State)
	norm, _ := asset.Freeze(in)
	if state.Digest() != in.StateDigest || norm.String() != j.String() {
		return in, asset.ErrInvalid
	}
	e, c, r := in.State.Environment, in.State.Case, in.State.Report
	for _, id := range []string{e.ID, e.Parser, e.Decoder, c.ID, c.Feature, c.Golden, c.GoldenVersion, r.ID} {
		if !ciBusinessID.MatchString(id) {
			return in, asset.ErrInvalid
		}
	}
	for _, revision := range []int{e.ParserRevision, e.DecoderRevision, c.Required, c.InputRevision, r.Revision} {
		if revision < 1 || revision > 16 {
			return in, asset.ErrInvalid
		}
	}
	if !ciBusinessIP.MatchString(e.IP) || !slices.Contains([]string{"product-2.0", "product-2.1", "product-2.1.1"}, e.Software) || !slices.Contains([]string{"compatible", "compact", "legacy"}, e.Buffer) || c.Rule != "semantic-fields-equal/1" || e.Flags == nil {
		return in, asset.ErrInvalid
	}
	for k := range e.Flags {
		if k != "compressed_transport" {
			return in, asset.ErrInvalid
		}
	}
	if c.InputContract != nil && *c.InputContract != "tlv" || c.Buffer != nil && !slices.Contains([]string{"legacy", "modern"}, *c.Buffer) {
		return in, asset.ErrInvalid
	}
	return in, nil
}
func (f CIPairFrozen) Validate() error {
	for _, id := range []string{f.CaseID, f.Dataset, f.PlanID} {
		if !asset.ValidID(id) {
			return asset.ErrInvalid
		}
	}
	for _, id := range []string{f.CaseVersion, f.DatasetVersion, f.PlanVersion, f.Source} {
		if !ciBusinessID.MatchString(id) {
			return asset.ErrInvalid
		}
	}
	for _, d := range []string{f.CaseDigest, f.InputDigest} {
		if !diagnosisDigest.MatchString(d) {
			return asset.ErrInvalid
		}
	}
	input, e := asset.Freeze(f.Input)
	if e != nil || input.Digest() != f.InputDigest {
		return asset.ErrInvalid
	}
	_, e = ReadCIPairInput(input)
	return e
}

type CIDiagnosisLogicalSource struct {
	Key            string `json:"logical_source_key"`
	Kind           string `json:"kind"`
	Semantics      string `json:"source_semantics_version"`
	Expected       string `json:"expected_semantic_digest"`
	UpstreamRef    string `json:"upstream_source_ref"`
	UpstreamDigest string `json:"upstream_source_digest"`
}
type CIDiagnosisGroundTruthV2 struct {
	Version     string                     `json:"schema_version"`
	Status      string                     `json:"diagnosis_status"`
	Stage       string                     `json:"failure_stage"`
	Owner       string                     `json:"required_owner_role"`
	InputDigest string                     `json:"input_digest"`
	ReviewID    string                     `json:"review_id"`
	Reviewer    string                     `json:"reviewer"`
	Sources     []CIDiagnosisLogicalSource `json:"required_sources"`
}

func ReadCIDiagnosisGroundTruthV2(j asset.JSON) (CIDiagnosisGroundTruthV2, error) {
	var g CIDiagnosisGroundTruthV2
	if j.Decode(&g) != nil || g.Version != CIDiagnosisGTVersionV2 || !slices.Contains([]string{"SUPPORTED", "INSUFFICIENT_EVIDENCE"}, g.Status) || !slices.Contains([]string{"PARSER", "COMPARISON", "AUTOMATION", "UNKNOWN"}, g.Stage) || !slices.Contains([]string{"TOOL_MAINTAINER", "TEST_OWNER", "UNKNOWN"}, g.Owner) || !diagnosisDigest.MatchString(g.InputDigest) || !asset.ValidID(g.ReviewID) || !asset.Text(g.Reviewer) || len(g.Sources) < 1 || len(g.Sources) > 5 {
		return g, asset.ErrInvalid
	}
	kinds, keys := map[string]bool{}, map[string]bool{}
	for _, s := range g.Sources {
		if !slices.Contains([]string{"PARSER_LOG", "ENVIRONMENT_RECORD", "CASE_REQUIREMENTS", "COMPARISON_LOG", "AUTOMATION_LOG"}, s.Kind) || kinds[s.Kind] || keys[s.Key] || !asset.Text(s.Key) || s.Semantics != CISourceSemanticsVersion || !diagnosisDigest.MatchString(s.Expected) || !asset.Text(s.UpstreamRef) || !diagnosisDigest.MatchString(s.UpstreamDigest) {
			return g, asset.ErrInvalid
		}
		kinds[s.Kind] = true
		keys[s.Key] = true
	}
	return g, nil
}
