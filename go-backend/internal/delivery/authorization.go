// Package delivery 将不可变 Gate 投影为交付权限，不拥有评测或执行状态。
package delivery

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	"reflect"
	"regexp"
	"time"
)

const Version = "stage13.delivery-authorization.v1"
const SinkKind = "CONTROLLED_DELIVERY_SINK"

type GateBinding struct {
	GateID           string            `json:"gate_id"`
	Decision         decision.Decision `json:"gate_decision"`
	ReceiptDigest    string            `json:"gate_receipt_digest"`
	DatasetVersion   string            `json:"dataset_version"`
	BaselineSubject  string            `json:"baseline_subject_digest"`
	CandidateSubject string            `json:"candidate_subject_digest"`
	ComparisonDigest string            `json:"comparison_digest"`
	SnapshotDigest   string            `json:"snapshot_digest"`
	PolicyVersion    string            `json:"policy_version"`
	PolicyDigest     string            `json:"policy_digest"`
}
type OutcomeBinding struct {
	CaseID          string `json:"case_id"`
	CaseVersion     string `json:"case_version"`
	EvaluationRunID string `json:"evaluation_run_id"`
	AnchorRunID     string `json:"anchor_run_id"`
	SelectedRunID   string `json:"selected_final_run_id"`
	SubjectDigest   string `json:"subject_manifest_digest"`
	AnswerDigest    string `json:"final_answer_digest"`
	ReceiptDigest   string `json:"actual_subject_receipt_digest"`
}
type Destination struct {
	Kind  string `json:"kind"`
	Scope string `json:"scope"`
	ID    string `json:"id"`
}
type Request struct {
	Gate        GateBinding    `json:"gate"`
	Outcome     OutcomeBinding `json:"outcome"`
	Destination Destination    `json:"destination"`
}
type Authorization struct {
	Version     string         `json:"authorization_version"`
	ProjectID   string         `json:"project_id"`
	Gate        GateBinding    `json:"gate"`
	Outcome     OutcomeBinding `json:"outcome"`
	Destination Destination    `json:"destination"`
	Status      string         `json:"authorization_status"`
	Reason      string         `json:"reason"`
	IssuedAt    time.Time      `json:"issued_at"`
	ResultIDs   []string       `json:"evaluation_result_refs"`
	Digest      string         `json:"authorization_digest"`
}

// Project 仅消费已由存储 Owner 核验的原始 binding；任何不一致都拒绝。
func Project(project string, issued time.Time, q Request, gate GateBinding, outcome OutcomeBinding, results []string) Authorization {
	a := Authorization{Version: Version, ProjectID: project, Gate: gate, Outcome: outcome, Destination: q.Destination, Status: "DENIED", Reason: "AUTHORIZATION_BINDING_MISMATCH", IssuedAt: issued, ResultIDs: results}
	switch {
	case !asset.ValidID(project) || !validGate(gate) || !validOutcome(outcome, gate):
		a.Reason = "GATE_OR_OUTCOME_MALFORMED"
	case q.Destination.Kind != SinkKind || q.Destination.Scope != "TEST_SCOPE" || !asset.Text(q.Destination.ID):
		a.Reason = "DESTINATION_DENIED"
	case !reflect.DeepEqual(q.Gate, gate) || !reflect.DeepEqual(q.Outcome, outcome):
	case gate.Decision == decision.Blocked:
		a.Reason = "GATE_BLOCKED"
	case gate.Decision == decision.Fail:
		a.Reason = "GATE_FAIL"
	case gate.Decision == decision.Pass && len(results) > 0:
		a.Status, a.Reason = "AUTHORIZED", "GATE_PASS"
	default:
		a.Reason = "GATE_MALFORMED"
	}
	a.Digest = a.ContentDigest()
	return a
}

func validGate(g GateBinding) bool {
	if !asset.ValidID(g.GateID) || !asset.Text(g.DatasetVersion) || !asset.Text(g.PolicyVersion) {
		return false
	}
	for _, d := range []string{g.ReceiptDigest, g.BaselineSubject, g.CandidateSubject, g.ComparisonDigest, g.SnapshotDigest, g.PolicyDigest} {
		if !digestPattern.MatchString(d) {
			return false
		}
	}
	return g.Decision == decision.Pass || g.Decision == decision.Fail || g.Decision == decision.Blocked
}

var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

func validOutcome(o OutcomeBinding, g GateBinding) bool {
	return asset.ValidID(o.CaseID) && asset.Text(o.CaseVersion) && asset.ValidID(o.EvaluationRunID) && asset.ValidID(o.AnchorRunID) && asset.ValidID(o.SelectedRunID) && o.SubjectDigest == g.CandidateSubject && digestPattern.MatchString(o.AnswerDigest) && digestPattern.MatchString(o.ReceiptDigest)
}
func (a Authorization) ContentDigest() string {
	a.Digest = ""
	j, _ := asset.Freeze(a)
	return j.Digest()
}
func Identity(q Request) string {
	j, _ := asset.Freeze(struct {
		Domain  string
		Request Request
	}{"stage13.delivery.v1", q})
	return j.Digest()
}
