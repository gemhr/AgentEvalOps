package decision

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
	"testing"
)

func TestResponsibilityV2GateMetricBinding(t *testing.T) {
	s := v2Snapshot()
	metrics := citriage.MetricsForVersion(citriage.ContractV2)
	for _, side := range []*Source{&s.Baseline, &s.Candidate} {
		u := &side.Units[0]
		score, _ := stage13Score(*u)
		score.Values = map[string]float64{}
		for _, name := range metrics {
			score.Values[name] = 1
		}
		for i := range u.Metrics {
			m := &u.Metrics[i]
			m.Evaluator.Definition.ImplementationRef = citriage.ImplementationV2
			m.Evaluator.Definition.Config, _ = asset.Freeze(map[string]string{"metric": metrics[i]})
			m.Provenance, _ = asset.Freeze(map[string]any{"triage": score})
			m.Value = number(1)
		}
	}
	if !stage13EvidenceValid(s.Baseline) {
		t.Fatal("complete v2 evidence blocked")
	}
	r, e := Compare(s)
	if e != nil || r.Decision != Pass || r.Stage13.Candidate.Scores[metrics[3]] == nil || r.Stage13.Thresholds[metrics[3]] != 0.8 || r.Stage13.Candidate.Scores[citriage.Metrics[3]] != nil {
		t.Fatal("v2 root metric identity/threshold", r, e)
	}
	s.Baseline.Units[0].Metrics[1].Evaluator.Definition.ImplementationRef = citriage.Implementation
	if stage13EvidenceValid(s.Baseline) {
		t.Fatal("mixed implementations accepted")
	}
}
