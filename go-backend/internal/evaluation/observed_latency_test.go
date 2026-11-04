package evaluation

import (
	"agentevalops/go-backend/internal/asset"
	"testing"
)

func TestAdaptiveObservedLatencyIncludesFailuresAndMissing(t *testing.T) {
	attempts := make([]Attempt, 5)
	for i, outcome := range []OutcomeKind{Success, Failure, Timeout, Unknown, Unknown} {
		attempts[i].Outcome = outcome
		if i < 4 {
			ms := int64((i + 1) * 10)
			attempts[i].Metadata.Observation.Cleanup, _ = asset.Freeze(ClientObservation{ClientRT: &ms, Censored: i == 3, RemoteCertainty: "TEST"})
		}
	}
	s := SummarizeObservedLatency(attempts)
	if s.Planned != 5 || s.Known != 4 || s.Censored != 1 || s.Outcomes[Unknown] != 2 || *s.P50 != 20 || *s.P95 != 40 || *s.P99 != 40 {
		t.Fatalf("bad coverage/nearest-rank %+v", s)
	}
	missing := SummarizeObservedLatency(attempts[4:])
	if missing.P50 != nil || missing.Known != 0 || missing.Planned != 1 {
		t.Fatal("missing latency filled with zero")
	}
}
