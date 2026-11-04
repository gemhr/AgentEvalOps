package evaluation

import (
	"math"
	"sort"
)

type ClientObservation struct {
	ClientRT        *int64 `json:"client_observed_rt_ms"`
	Censored        bool   `json:"censored"`
	RemoteCertainty string `json:"remote_certainty"`
}
type ObservedLatencySummary struct {
	Planned, Known, Censored int
	Outcomes                 map[OutcomeKind]int
	P50, P95, P99            *int64
	Algorithm                string
	SamplingUnit             string
}

func SummarizeObservedLatency(attempts []Attempt) ObservedLatencySummary {
	s := ObservedLatencySummary{Planned: len(attempts), Outcomes: map[OutcomeKind]int{}, Algorithm: "nearest-rank.v1", SamplingUnit: "EXECUTION_ATTEMPT"}
	values := []int64{}
	for _, a := range attempts {
		s.Outcomes[a.Outcome]++
		var c ClientObservation
		if a.Metadata.Observation.Cleanup.Decode(&c) == nil {
			if c.Censored {
				s.Censored++
			}
			if c.ClientRT != nil && *c.ClientRT >= 0 {
				values = append(values, *c.ClientRT)
			}
		}
	}
	sort.Slice(values, func(i, j int) bool { return values[i] < values[j] })
	s.Known = len(values)
	if len(values) > 0 {
		q := func(p float64) *int64 { n := values[int(math.Ceil(p*float64(len(values))))-1]; return &n }
		s.P50, s.P95, s.P99 = q(.5), q(.95), q(.99)
	}
	return s
}
