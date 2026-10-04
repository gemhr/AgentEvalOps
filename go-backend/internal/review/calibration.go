package review

import (
	"slices"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"
)

type CalibrationSample struct {
	Case     asset.Ref
	GoldenID string
	Result   SourceRef
}
type CalibrationCommand struct {
	ID                         string
	Dataset, Evaluator, Schema asset.Ref
	Samples                    []CalibrationSample
	Protocol                   Protocol
	Sampling                   Sampling
	PositiveClass              string
}

func (c CalibrationCommand) Validate() error {
	if !asset.ValidID(c.ID) || c.Dataset.Validate() != nil || c.Evaluator.Validate() != nil || c.Schema.Validate() != nil || c.Protocol.Validate() != nil || c.Sampling.Validate() != nil || len(c.Samples) == 0 || len(c.Samples) > 1000 || c.PositiveClass != "" && !Decidable(c.PositiveClass) {
		return asset.ErrInvalid
	}
	seen := map[string]bool{}
	for _, s := range c.Samples {
		if s.Case.Validate() != nil || seen[s.Case.EntityID] || s.GoldenID != "" && !asset.ValidID(s.GoldenID) {
			return asset.ErrInvalid
		}
		seen[s.Case.EntityID] = true
		if s.Result.Type != "" && s.Result.Validate() != nil {
			return asset.ErrInvalid
		}
	}
	return nil
}

type CalibrationPair struct {
	Case   asset.Ref
	Golden *GoldenLabel
	Judge  *Automatic
}
type CalibrationSnapshot struct {
	ID, ProjectID, Actor, Intent, GoldenSetDigest string
	Command                                       CalibrationCommand
	DatasetDigest, EvaluatorDigest, SchemaDigest  string
	Definition                                    metric.EvaluatorDefinition
	Pairs                                         []CalibrationPair
	CreatedAt                                     time.Time
}
type Agreement struct {
	N, HumanDecidable, JudgeDecidable, PairedDecidable, Matches, Disagreements, MissingHuman, MissingJudge, HumanUndecidable, JudgeUndecidable int
	AgreementRate, HumanCoverage, JudgeCoverage, PairedCoverage                                                                                *float64
	// key 为 human→judge；不可判类别另计，不能进入二分类矩阵。
	Confusion                                                map[string]int
	Undecidable                                              map[string]int
	FalsePositive, FalseNegative, TruePositive, TrueNegative int
	Precision, Recall, F1                                    *float64
	ProviderErrors, InvalidOutput, IdentityDrift             int
}
type CalibrationReport struct {
	ID, ProjectID, SnapshotDigest, GoldenSetDigest string
	Evaluator, Dataset, Schema                     asset.Ref
	EvaluatorDigest, DatasetDigest, SchemaDigest   string
	Definition                                     metric.EvaluatorDefinition
	Protocol                                       Protocol
	Sampling                                       Sampling
	PositiveClass                                  string
	Agreement                                      Agreement
	Pairs                                          []CalibrationPair
	CreatedAt                                      time.Time
}

func Compute(s CalibrationSnapshot) (CalibrationReport, error) {
	if s.Command.Validate() != nil || len(s.Pairs) != len(s.Command.Samples) {
		return CalibrationReport{}, asset.ErrInvalid
	}
	r := CalibrationReport{ID: s.ID, ProjectID: s.ProjectID, GoldenSetDigest: s.GoldenSetDigest, Evaluator: s.Command.Evaluator, Dataset: s.Command.Dataset, Schema: s.Command.Schema, EvaluatorDigest: s.EvaluatorDigest, DatasetDigest: s.DatasetDigest, SchemaDigest: s.SchemaDigest, Definition: s.Definition, Protocol: s.Command.Protocol, Sampling: s.Command.Sampling, PositiveClass: s.Command.PositiveClass, Pairs: s.Pairs, CreatedAt: s.CreatedAt}
	j, e := asset.Freeze(s)
	if e != nil {
		return r, e
	}
	r.SnapshotDigest = j.Digest()
	a := Agreement{N: len(s.Pairs), Confusion: map[string]int{}, Undecidable: map[string]int{}}
	for _, p := range s.Pairs {
		h, v := "", ""
		if p.Golden != nil {
			h = p.Golden.Decision.Value
			if p.Golden.Protocol != s.Command.Protocol || p.Golden.Schema != s.Command.Schema {
				return r, asset.ErrConflict
			}
		}
		if p.Judge != nil {
			v = p.Judge.Decision
			if p.Judge.Evaluator != s.Command.Evaluator || p.Judge.EvaluatorDigest != s.EvaluatorDigest {
				return r, asset.ErrConflict
			}
			if p.Judge.ProviderError {
				a.ProviderErrors++
			}
			if p.Judge.InvalidOutput {
				a.InvalidOutput++
			}
			if p.Judge.IdentityDrift {
				a.IdentityDrift++
			}
			if p.Golden != nil && p.Golden.EvidenceDigest != p.Judge.EvidenceDigest {
				return r, asset.ErrConflict
			}
		}
		if h == "" {
			a.MissingHuman++
		} else if !Decidable(h) {
			a.HumanUndecidable++
			a.Undecidable["human:"+h]++
		}
		if v == "" {
			a.MissingJudge++
		} else if !Decidable(v) {
			a.JudgeUndecidable++
			a.Undecidable["judge:"+v]++
		}
		if Decidable(h) {
			a.HumanDecidable++
		}
		if Decidable(v) {
			a.JudgeDecidable++
		}
		if !Decidable(h) || !Decidable(v) {
			continue
		}
		a.PairedDecidable++
		a.Confusion[h+"→"+v]++
		if h == v {
			a.Matches++
		} else {
			a.Disagreements++
		}
		if r.PositiveClass != "" {
			hp, jp := h == r.PositiveClass, v == r.PositiveClass
			switch {
			case hp && jp:
				a.TruePositive++
			case !hp && jp:
				a.FalsePositive++
			case hp && !jp:
				a.FalseNegative++
			default:
				a.TrueNegative++
			}
		}
	}
	a.AgreementRate = Ratio(a.Matches, a.PairedDecidable)
	a.HumanCoverage = Ratio(a.HumanDecidable, a.N)
	a.JudgeCoverage = Ratio(a.JudgeDecidable, a.N)
	a.PairedCoverage = Ratio(a.PairedDecidable, a.N)
	if r.PositiveClass != "" {
		a.Precision = Ratio(a.TruePositive, a.TruePositive+a.FalsePositive)
		a.Recall = Ratio(a.TruePositive, a.TruePositive+a.FalseNegative)
		a.F1 = Ratio(2*a.TruePositive, 2*a.TruePositive+a.FalsePositive+a.FalseNegative)
	}
	r.Agreement = a
	return r, nil
}

type EvaluatorRegression struct {
	Status, Reason                                                                                     string
	Baseline, Candidate                                                                                string
	AgreementDelta, CoverageDelta                                                                      *float64
	FalsePositiveDelta, FalseNegativeDelta, ProviderErrorDelta, InvalidOutputDelta, IdentityDriftDelta int
}

func CompareEvaluators(a, b CalibrationReport) EvaluatorRegression {
	r := EvaluatorRegression{Status: "INCOMPARABLE", Baseline: a.ID, Candidate: b.ID}
	if a.ProjectID != b.ProjectID || a.GoldenSetDigest != b.GoldenSetDigest || a.DatasetDigest != b.DatasetDigest || a.SchemaDigest != b.SchemaDigest || a.Protocol != b.Protocol || a.Sampling != b.Sampling || a.PositiveClass != b.PositiveClass {
		r.Reason = "GOLDEN_EVIDENCE_OR_PROTOCOL_MISMATCH"
		return r
	}
	if a.Agreement.IdentityDrift > 0 || b.Agreement.IdentityDrift > 0 {
		r.Reason = "PROVIDER_IDENTITY_UNVERIFIABLE"
		return r
	}
	if len(a.Pairs) != len(b.Pairs) {
		r.Reason = "SAMPLE_EVIDENCE_MISMATCH"
		return r
	}
	for i, p := range a.Pairs {
		q := b.Pairs[i]
		if p.Case != q.Case || p.Judge != nil && q.Judge != nil && p.Judge.EvidenceDigest != q.Judge.EvidenceDigest {
			r.Reason = "SAMPLE_EVIDENCE_MISMATCH"
			return r
		}
	}
	// 两个版本可以有不同 requested model；每份报告都需保留实际 provider 证据。
	for _, report := range []CalibrationReport{a, b} {
		for _, p := range report.Pairs {
			if p.Judge == nil {
				continue
			}
			if p.Judge.Definition.Kind == metric.LLMJudge && len(p.Judge.Calls) == 0 {
				r.Reason = "PROVIDER_CALL_MISSING"
				return r
			}
			for _, c := range p.Judge.Calls {
				if slices.Contains([]string{"", "UNKNOWN"}, c.ActualProvider) || slices.Contains([]string{"", "UNKNOWN"}, c.ActualModel) || slices.Contains([]string{"", "UNKNOWN"}, c.ActualRevision) {
					r.Reason = "PROVIDER_IDENTITY_UNVERIFIABLE"
					return r
				}
			}
		}
	}
	r.Status = "COMPARABLE"
	if a.Agreement.AgreementRate != nil && b.Agreement.AgreementRate != nil {
		v := *b.Agreement.AgreementRate - *a.Agreement.AgreementRate
		r.AgreementDelta = &v
	}
	if a.Agreement.PairedCoverage != nil && b.Agreement.PairedCoverage != nil {
		v := *b.Agreement.PairedCoverage - *a.Agreement.PairedCoverage
		r.CoverageDelta = &v
	}
	r.FalsePositiveDelta = b.Agreement.FalsePositive - a.Agreement.FalsePositive
	r.FalseNegativeDelta = b.Agreement.FalseNegative - a.Agreement.FalseNegative
	r.ProviderErrorDelta = b.Agreement.ProviderErrors - a.Agreement.ProviderErrors
	r.InvalidOutputDelta = b.Agreement.InvalidOutput - a.Agreement.InvalidOutput
	r.IdentityDriftDelta = b.Agreement.IdentityDrift - a.Agreement.IdentityDrift
	return r
}
