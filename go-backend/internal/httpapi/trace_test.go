package httpapi

import (
	"testing"

	ev "agentevalops/go-backend/internal/evaluation"
)

func TestTraceValidationRepliesMapToInvalidArgument(t *testing.T) {
	for _, reason := range []string{"LOCALAGENT_ENVELOPE_INVALID", "TRACE_BODY_TOO_LARGE", "SELF_PARENT"} {
		err := traceValidationError(ev.Reply{Code: ev.Rejected, Reason: reason}, nil)
		status, code := mapError(err)
		if status != 400 || code != "INVALID_ARGUMENT" {
			t.Errorf("reason %q mapped to %d %s", reason, status, code)
		}
	}
	if err := traceValidationError(ev.Reply{Code: ev.Rejected, Reason: "AUTHENTICATED_SOURCE_REQUIRED"}, nil); err != nil {
		t.Fatalf("source authorization failure was treated as a payload error: %v", err)
	}
}
