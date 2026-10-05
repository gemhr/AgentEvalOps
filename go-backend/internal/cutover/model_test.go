package cutover

import (
	"strings"
	"testing"
	"time"
)

func completeEvidence() Evidence {
	e := Evidence{Epoch: 1, Operator: "controlled-operator", LegacyRoles: []string{"retired_fixture"}, BackupRef: "controlled-backup", BackupDigest: strings.Repeat("a", 64), Checks: map[string]string{}, Drain: map[string]*int64{}, Consumers: []Consumer{{Name: "controlled-client", Owner: "fixture", Current: "legacy", Target: "go", Status: "VERIFIED", Rollback: "restore", VerifiedAt: time.Now().UTC().Format(time.RFC3339), Kind: "CONTROLLED"}}}
	for _, key := range MandatoryChecks {
		e.Checks[key] = "VERIFIED"
	}
	e.CutoverID = "00000000-0000-4000-8000-000000000001"
	for _, key := range DrainMetrics {
		e.Drain[key] = new(int64)
	}
	e.ConsumerDigest = ConsumerDigest(e.Consumers)
	return e
}

func TestWriterTransitionsAndRecoveryBoundary(t *testing.T) {
	s := State{Mode: "PYTHON_ACTIVE", Writer: "PYTHON", Epoch: 1}
	for _, cmd := range []string{"enter-drain", "enter-barrier", "activate-go"} {
		var err error
		s, err = Next(s, cmd)
		if err != nil {
			t.Fatal(err)
		}
	}
	if s.Mode != "GO_ACTIVE" || s.Writer != "GO" || s.Epoch != 3 {
		t.Fatal(s)
	}
	for _, phase := range []State{{Mode: "BARRIER", Writer: "NONE", Epoch: 2}, s} {
		n, err := Next(phase, "abort-before-go-write")
		if err == nil || err.Error() != "ROLLBACK_REQUIRES_RESTORE_OR_FORWARD_FIX" || n != phase {
			t.Fatal("unapproved Python recovery", n, err)
		}
	}
	if _, err := Next(s, "activate-go"); err == nil {
		t.Fatal("duplicate activation accepted")
	}
}

func TestPreflightDoesNotPromoteDeclarationsOrUnknownEvidence(t *testing.T) {
	s := State{Mode: "BARRIER", Writer: "NONE", Epoch: 1, Schema: Schema}
	e := completeEvidence()
	if Evaluate(s, e, true).Status != "PASS" || Evaluate(s, e, false).Status != "BLOCKED" {
		t.Fatal("controlled proof escaped scope")
	}
	for _, change := range []func(*Evidence){
		func(e *Evidence) { e.Consumers[0].Status = "DECLARED"; e.ConsumerDigest = ConsumerDigest(e.Consumers) },
		func(e *Evidence) {
			e.Consumers[0].Name = "UNKNOWN_EXTERNAL_CONSUMER"
			e.ConsumerDigest = ConsumerDigest(e.Consumers)
		},
		func(e *Evidence) { delete(e.Drain, "inflight_transactions") },
		func(e *Evidence) { e.BackupDigest = strings.Repeat("z", 64) },
		func(e *Evidence) { e.ConsumerDigest = "unbound" },
		func(e *Evidence) { e.Epoch++ },
	} {
		e := completeEvidence()
		change(&e)
		if Evaluate(s, e, true).Status != "BLOCKED" {
			t.Fatal("missing proof accepted")
		}
	}
}
