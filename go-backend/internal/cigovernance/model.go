// Package cigovernance 实现 Stage13 的受控数据治理，不调用 Subject 或 Judge。
package cigovernance

import (
	"crypto/sha256"
	"fmt"
	"slices"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
)

const Profile = "stage13-evaluation-data-generation-v1"
const GTMapping = "stage13.evaluation-gt-mapping.v1"
const SplitVersion = "stage13.family-split.v1"
const Contract = "stage13.dataset-governance.v1"
const Schema = "c13a00200001"

var Roles = []string{"DEVELOPMENT", "CALIBRATION", "HOLDOUT"}
var ReviewStates = []string{"PENDING_REVIEW", "CONFIRMED", "CORRECTED", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE", "REJECTED"}
var Taxonomy = []string{"OUTPUT_SCHEMA_INVALID", "OUTPUT_SEMANTIC_INVALID", "CATEGORY_WRONG", "ACTION_WRONG", "TICKET_WRONG", "ROOT_NOT_FOUND", "ROOT_RANKING_WEAK", "EVIDENCE_UNGROUNDED", "INSUFFICIENT_EVIDENCE_HANDLING_WRONG", "CRITICAL_DECISION_WRONG"}
var FeedbackSources = []string{"MODEL_OUTPUT_INVALID", "MODEL_BUSINESS_WRONG", "CRITICAL_WRONG", "GT_DISPUTE", "EVIDENCE_INSUFFICIENT", "HUMAN_CORRECTION"}

func Freeze(v any) asset.JSON {
	j, err := asset.Freeze(v)
	if err != nil {
		panic(err)
	}
	return j
}

// ID 仅用来幂等发布；family 隔离依据独立机制与模板，绝不依据 UUID。
func ID(parts ...string) string {
	h := sha256.Sum256(Freeze(parts).Bytes())
	h[6] = h[6]&15 | 80
	h[8] = h[8]&63 | 128
	return fmt.Sprintf("%x-%x-%x-%x-%x", h[:4], h[4:6], h[6:8], h[8:10], h[10:16])
}

type Policy struct {
	Version    string     `json:"version"`
	Role       string     `json:"role"`
	Family     string     `json:"family_id,omitempty"`
	State      string     `json:"review_state,omitempty"`
	GTMapping  string     `json:"gt_mapping_version"`
	Split      string     `json:"split_version"`
	Profile    string     `json:"source_profile"`
	ReviewID   string     `json:"review_id,omitempty"`
	Supersedes *asset.Ref `json:"supersedes,omitempty"`
	Families   []string   `json:"families,omitempty"`
	Frozen     bool       `json:"frozen"`
}

func (p Policy) Validate() error {
	profileValid := p.Profile == Profile && p.GTMapping == GTMapping && p.Split == SplitVersion
	profileValid = profileValid || p.Profile == ProfileV2 && p.GTMapping == GTMappingV2 && p.Split == SplitVersionV2 && p.Role == "HOLDOUT"
	profileValid = profileValid || p.Profile == ProfileV3 && p.GTMapping == GTMappingV3 && p.Split == SplitVersionV3 && p.Role == "HOLDOUT"
	if p.Version != Contract || !slices.Contains(Roles, p.Role) || !profileValid || !p.Frozen {
		return asset.ErrInvalid
	}
	if p.Family != "" && (!asset.Text(p.Family) || !slices.Contains(ReviewStates, p.State)) {
		return asset.ErrInvalid
	}
	return nil
}
func Metadata(p Policy) asset.JSON { return Freeze(map[string]any{"stage13_governance": p}) }
func ReadPolicy(metadata asset.JSON) (*Policy, error) {
	var fields map[string]asset.JSON
	if metadata.String() == "null" {
		return nil, nil
	}
	if metadata.Decode(&fields) != nil {
		return nil, nil
	}
	j, ok := fields["stage13_governance"]
	if !ok {
		return nil, nil
	}
	var p Policy
	if j.Decode(&p) != nil || p.Validate() != nil {
		return nil, asset.ErrInvalid
	}
	return &p, nil
}
func HardGolden(state string) bool { return state == "CONFIRMED" || state == "CORRECTED" }
func CheckIntent(role, intent string) error {
	if role == "DEVELOPMENT" && intent == "DEV_EVALUATION" || role == "CALIBRATION" && intent == "CALIBRATION_EVALUATION" || role == "HOLDOUT" && intent == "RELEASE_EVALUATION" {
		return nil
	}
	return asset.ErrForbidden
}

type ReviewDecision struct {
	ID               string     `json:"review_id"`
	ItemID           string     `json:"review_item_id"`
	Case             asset.Ref  `json:"case_version"`
	PreviousGTDigest string     `json:"previous_gt_digest"`
	State            string     `json:"review_state"`
	GroundTruth      asset.JSON `json:"ground_truth"`
	Reason           string     `json:"reason_code"`
	Reviewer         string     `json:"reviewer"`
	ActorType        string     `json:"actor_type"`
	AnnotationID     string     `json:"annotation_id"`
	GoldenID         string     `json:"golden_id,omitempty"`
	CreatedAt        time.Time  `json:"created_at"`
	Digest           string     `json:"review_digest"`
}

func (r ReviewDecision) Validate() error {
	if !asset.ValidID(r.ID) || !asset.ValidID(r.ItemID) || r.Case.Validate() != nil || !slices.Contains(ReviewStates, r.State) || r.State == "PENDING_REVIEW" || !asset.Text(r.Reason) || len(r.PreviousGTDigest) != 64 {
		return asset.ErrInvalid
	}
	if HardGolden(r.State) {
		if _, err := citriage.ReadGroundTruth(r.GroundTruth); err != nil {
			return err
		}
	}
	return nil
}

type Feedback struct {
	ID               string     `json:"feedback_id"`
	Case             asset.Ref  `json:"case_version"`
	RunID            string     `json:"evaluation_run_id"`
	ResultID         string     `json:"evaluation_result_id"`
	SubjectDigest    string     `json:"subject_manifest_digest"`
	Evaluator        asset.Ref  `json:"evaluator_version"`
	GateID           string     `json:"gate_id"`
	ComparisonDigest string     `json:"comparison_digest"`
	Source           string     `json:"source"`
	Failure          string     `json:"failure_taxonomy"`
	Details          asset.JSON `json:"details"`
	CreatedAt        time.Time  `json:"created_at"`
	Digest           string     `json:"feedback_digest"`
}

func FeedbackID(f Feedback) string {
	return ID("stage13.feedback.v1", f.Case.EntityID, f.Case.Version, f.SubjectDigest, f.ResultID, f.Source, f.Failure)
}
func (f Feedback) Validate() error {
	if f.Case.Validate() != nil || !asset.ValidID(f.RunID) || !asset.ValidID(f.ResultID) || !asset.ValidID(f.GateID) || len(f.SubjectDigest) != 64 || f.Evaluator.Validate() != nil || !slices.Contains(FeedbackSources, f.Source) || !slices.Contains(Taxonomy, f.Failure) || len(f.ComparisonDigest) != 64 {
		return asset.ErrInvalid
	}
	return nil
}

// Failures 只分析历史已冻结的判分事实，不影响 GT、Result 或 Gate。
func Failures(s citriage.Scores, g citriage.GroundTruth) map[string]string {
	m := map[string]string{}
	if !s.SchemaValid {
		m["OUTPUT_SCHEMA_INVALID"] = "MODEL_OUTPUT_INVALID"
		return m
	}
	if !s.SemanticValid {
		m["OUTPUT_SEMANTIC_INVALID"] = "MODEL_OUTPUT_INVALID"
		return m
	}
	for i, reason := range []string{"CATEGORY_WRONG", "ACTION_WRONG", "TICKET_WRONG"} {
		if s.Values[citriage.Metrics[i]] == 0 {
			m[reason] = "MODEL_BUSINESS_WRONG"
		}
	}
	if s.Values[citriage.Metrics[3]] == 0 {
		m["ROOT_NOT_FOUND"] = "MODEL_BUSINESS_WRONG"
	} else if s.Values[citriage.Metrics[4]] < 1 || s.Values[citriage.Metrics[5]] < 1 {
		m["ROOT_RANKING_WEAK"] = "MODEL_BUSINESS_WRONG"
	}
	if g.Criticality == "CRITICAL" && s.Critical == "FAIL" {
		m["CRITICAL_DECISION_WRONG"] = "CRITICAL_WRONG"
	}
	if s.Reason == "MISSING_EVIDENCE" {
		m["EVIDENCE_UNGROUNDED"] = "EVIDENCE_INSUFFICIENT"
	}
	if s.Reason == "ABSTAINED" || s.Reason == "INSUFFICIENT_CERTAINTY" {
		m["INSUFFICIENT_EVIDENCE_HANDLING_WRONG"] = "MODEL_BUSINESS_WRONG"
	}
	return m
}
