package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"math"
)

type TaskDecision string

const (
	TaskSuccess       TaskDecision = "SUCCESS"
	TaskFailure       TaskDecision = "FAILURE"
	TaskInconclusive  TaskDecision = "INCONCLUSIVE"
	TaskNotApplicable TaskDecision = "NOT_APPLICABLE"
)

type TaskJudgment struct {
	Version      string       `json:"version"`
	Decision     TaskDecision `json:"decision"`
	Method       string       `json:"method"`
	Reason       string       `json:"reason"`
	EvidenceRefs []string     `json:"evidence_refs"`
	Confidence   *float64     `json:"confidence"`
	ProducerRef  string       `json:"producer_ref"`
}

func (j TaskJudgment) Validate() error {
	if j.Version != "task_success.v1" || !oneOf(string(j.Decision), "SUCCESS", "FAILURE", "INCONCLUSIVE", "NOT_APPLICABLE") || !oneOf(j.Method, "DETERMINISTIC", "LLM_JUDGE", "IMPORTED", "HUMAN") || !asset.ContentText(j.Reason) || j.ProducerRef == "" || (j.Confidence != nil && (math.IsNaN(*j.Confidence) || math.IsInf(*j.Confidence, 0) || *j.Confidence < 0 || *j.Confidence > 1)) {
		return asset.ErrInvalid
	}
	return nil
}

type TaskObservation struct {
	Execution     ev.OutcomeKind
	Applicability asset.ApplicabilityState
	Judgment      TaskJudgment
}
type TaskSuccessRate struct {
	EligibleTotal     int      `json:"eligible_total"`
	Success           int      `json:"success"`
	Failure           int      `json:"failure"`
	Inconclusive      int      `json:"inconclusive"`
	MissingEvidence   int      `json:"missing_evidence"`
	UnknownExecution  int      `json:"unknown_execution"`
	NotApplicable     int      `json:"not_applicable"`
	Unsupported       int      `json:"unsupported"`
	EligibleDecidable int      `json:"eligible_decidable_count"`
	Rate              *float64 `json:"task_success_rate"`
	Coverage          *float64 `json:"decision_coverage"`
	Unit              string   `json:"sampling_unit"`
}

// AggregateTaskSuccess 每个元素是一个 case-run；排他的缺失计数不被当作 failure。
func AggregateTaskSuccess(in []TaskObservation) (TaskSuccessRate, error) {
	r := TaskSuccessRate{Unit: "case-run"}
	for _, o := range in {
		if o.Applicability == asset.NotApplicable {
			r.NotApplicable++
			continue
		}
		r.EligibleTotal++
		if o.Execution == ev.Unknown {
			r.UnknownExecution++
			continue
		}
		if o.Execution == ev.Failure || o.Execution == ev.Timeout || o.Execution == ev.Cancelled {
			r.Failure++
			continue
		}
		if o.Execution != ev.Success {
			return r, asset.ErrInvalid
		}
		switch o.Applicability {
		case asset.MissingEvidence:
			r.MissingEvidence++
			continue
		case asset.UnsupportedEvidence:
			r.Unsupported++
			continue
		case asset.Applicable:
		default:
			return r, asset.ErrInvalid
		}
		if o.Judgment.Validate() != nil {
			return r, asset.ErrInvalid
		}
		switch o.Judgment.Decision {
		case TaskSuccess:
			r.Success++
		case TaskFailure:
			r.Failure++
		case TaskInconclusive:
			r.Inconclusive++
		case TaskNotApplicable:
			r.NotApplicable++
			r.EligibleTotal--
		}
	}
	r.EligibleDecidable = r.Success + r.Failure
	if r.EligibleDecidable > 0 {
		v := float64(r.Success) / float64(r.EligibleDecidable)
		r.Rate = &v
	}
	if r.EligibleTotal > 0 {
		v := float64(r.EligibleDecidable) / float64(r.EligibleTotal)
		r.Coverage = &v
	}
	return r, nil
}
