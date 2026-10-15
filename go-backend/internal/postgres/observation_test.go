package postgres

import (
	"testing"

	ob "agentevalops/go-backend/internal/observation"
)

func TestNormalObservationCannotClaimStrictTraceSources(t *testing.T) {
	for _, source := range []string{ob.TraceV1Source, ob.TraceV2Source} {
		if !isReservedTraceSource(source) {
			t.Errorf("strict source %q was not reserved", source)
		}
	}
	if isReservedTraceSource("APPLICATION_OBSERVATION") {
		t.Fatal("ordinary source was reserved")
	}
}
