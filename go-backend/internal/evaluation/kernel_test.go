package evaluation

import (
	"agentevalops/go-backend/internal/asset"
	"math"
	"testing"
)

func TestKernelCoordinationManifestAndLatest(t *testing.T) {
	c := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	e := asset.Ref{EntityID: asset.NewID(), Version: "v1"}
	s := RunSnapshot{Input: SnapshotInput{Manifest: []CaseInput{{Identity: AssetIdentity{Ref: c}}}, Evaluators: []EvaluatorSpec{{Identity: AssetIdentity{Ref: e}, Required: false}}}}
	a := Attempt{ID: asset.NewID(), CaseID: c.EntityID, CaseVersion: "v1", Number: 1, Status: "TERMINAL", Outcome: Success}
	rid := asset.NewID()
	w := Work{AttemptID: a.ID, CaseID: a.CaseID, CaseVersion: a.CaseVersion, EvaluatorID: e.EntityID, EvaluatorVersion: e.Version, Status: "COMPLETED", ResultID: &rid}
	if r := DecideRun(s, []Attempt{a}, []Work{w}); r.Status != "COMPLETED" {
		t.Fatal(r)
	}
	w.Status = "PENDING"
	if r := DecideRun(s, []Attempt{a}, []Work{w}); r.Code != NotReady {
		t.Fatal("optional must wait", r)
	}
	if r := DecideRun(s, []Attempt{a}, nil); r.Code != IntegrityBlocked {
		t.Fatal(r)
	}
	if r := DecideRun(RunSnapshot{}, nil, nil); r.Code != IntegrityBlocked {
		t.Fatal("empty all must not succeed", r)
	}
	a.Outcome = Unknown
	if r := DecideRun(s, []Attempt{a}, nil); r.Status != "OUTCOME_UNKNOWN" {
		t.Fatal(r)
	}
	a.Outcome = Failure
	if r := DecideRun(s, []Attempt{a}, nil); r.Status != "FAILED" {
		t.Fatal(r)
	}
	child := a
	child.ID = asset.NewID()
	child.Number = 2
	child.Status = "PENDING"
	if r := DecideRun(s, []Attempt{a, child}, nil); r.Code != NotReady {
		t.Fatal(r)
	}
	latest, err := Latest([]Attempt{child, a})
	if err != nil || latest[a.CaseID].ID != child.ID {
		t.Fatal(latest, err)
	}
	child.CaseVersion = "different"
	if _, e := Latest([]Attempt{a, child}); e == nil {
		t.Fatal("mixed versions accepted")
	}
}
func TestKernelOutcomeRetryAndUnknown(t *testing.T) {
	p, r, a, q := asset.NewID(), asset.NewID(), asset.NewID(), asset.NewID()
	body, _ := asset.ParseJSON([]byte(`{"answer":"ok"}`))
	b := Binding{ProjectID: p, RunID: r, AttemptID: a, RequestID: q, Ref: "a", Schema: "v1", Availability: "AVAILABLE", Body: body}
	o := Outcome{Kind: Success, RequestID: q, Source: "fixture", RemoteID: a, Protocol: "fixture.v1", DispatchCertainty: "REMOTE_ACCEPTED", TerminalCertainty: "REMOTE_CONFIRMED", Artifact: &b}
	if e := o.Validate(p, r, a, q); e != nil {
		t.Fatal(e)
	}
	o.Kind = Timeout
	o.Artifact = nil
	o.ErrorCategory = "timeout"
	o.Reason = "deadline"
	o.TerminalCertainty = "UNCONFIRMED"
	if e := o.Validate(p, r, a, q); e == nil {
		t.Fatal("transport timeout masquerades as runtime deadline")
	}
	o.Kind = Unknown
	if e := o.Validate(p, r, a, q); e != nil {
		t.Fatal(e)
	}
	attempt := Attempt{Status: "TERMINAL", Number: 1, Outcome: Unknown}
	policy := RetryPolicy{Version: "EXPLICIT_RETRY.v1", MaxAttempts: 2, Allowed: []OutcomeKind{Failure}}
	if policy.Validate() != nil || policy.Allows(attempt, true, true, true) || policy.Allows(attempt, false, true, true) {
		t.Fatal("unknown automatic retry")
	}
	policy.AllowUnknownFork = true
	if !policy.Allows(attempt, false, true, true) || policy.Allows(attempt, true, true, true) {
		t.Fatal("fork authorization")
	}
	attempt.Outcome = Failure
	if !policy.Allows(attempt, false, true, false) || policy.Allows(attempt, true, false, false) {
		t.Fatal("explicit vs auto")
	}
	policy.Version = "SAFE_AUTO_RETRY.v1"
	if policy.Validate() == nil {
		t.Fatal("retry safe source required")
	}
	policy.RetrySafeSource = "authenticated-retry-safe.v1"
	if policy.Validate() != nil || !policy.Allows(attempt, true, false, false) {
		t.Fatal("safe automatic retry")
	}
	attempt.Number = 2
	if policy.Allows(attempt, true, false, false) {
		t.Fatal("budget bypass")
	}
	policy.Allowed = append(policy.Allowed, Unknown)
	if policy.Validate() == nil {
		t.Fatal("ordinary UNKNOWN list")
	}
}
func TestKernelResultAndIntent(t *testing.T) {
	spec := EvaluatorSpec{Identity: AssetIdentity{ContentDigest: "spec"}}
	v := ResultValue{Verdict: "INCONCLUSIVE", SourceVerdict: "INCONCLUSIVE", Reason: "missing evidence", SpecDigest: "spec", InputDigest: "input", Artifact: Binding{Ref: "a"}}
	if e := v.Validate(spec, "input", v.Artifact); e != nil {
		t.Fatal(e)
	}
	score := math.NaN()
	v.Score = &score
	if e := v.Validate(spec, "input", v.Artifact); e == nil {
		t.Fatal("NaN accepted")
	}
	v.Score = nil
	v.SpecDigest = "different"
	if e := v.Validate(spec, "input", v.Artifact); e == nil {
		t.Fatal("binding accepted")
	}
	x, _ := asset.ParseJSON([]byte(`{"b":1.0,"a":"中"}`))
	y, _ := asset.ParseJSON([]byte(`{"a":"中","b":1.0}`))
	dx, _ := Intent(x)
	dy, _ := Intent(y)
	if dx != dy {
		t.Fatal("noncanonical intent")
	}
	z, _ := asset.ParseJSON([]byte(`{"a":"中","b":1}`))
	dz, _ := Intent(z)
	if dz == dx {
		t.Fatal("number types collapsed")
	}
	if !RunCompleted.Terminal() || RunRunning.Terminal() || !RunUnknown.Terminal() {
		t.Fatal("terminal states")
	}
}
