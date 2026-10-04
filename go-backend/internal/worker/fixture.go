package worker

import (
	"context"
	"fmt"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
)

const FixtureImplementation = "fixture-evaluator.v1"
const FixtureProviderImplementation = "fixture-provider-evaluator.v1"

// FixturePlan 必须冻结在 Target.Config / EvaluatorDefinition.Config 内。CONTROLLED / TEST ONLY。
type FixturePlan struct {
	Mode                  string `json:"mode"`
	DelayMilliseconds     int64  `json:"delay_milliseconds"`
	RetryBefore           int    `json:"retry_before"`
	ConfirmedCancellation bool   `json:"confirmed_cancellation"`
}

func FixtureDefinitionSupported(d metric.EvaluatorDefinition) bool {
	if d.Availability == asset.Unsupported || d.Validate() != nil {
		return false
	}
	if d.ImplementationRef == FixtureImplementation {
		return d.Kind == metric.Deterministic
	}
	return d.ImplementationRef == FixtureProviderImplementation && d.Kind == metric.LLMJudge && d.Model != nil && d.Model.Provider == "fixture" && d.Model.Model == "fixture"
}
func fixturePlan(raw asset.JSON, fallback string) (FixturePlan, error) {
	p := FixturePlan{Mode: fallback}
	if raw.String() != "null" {
		if err := raw.Decode(&p); err != nil {
			return p, err
		}
	}
	if p.DelayMilliseconds < 0 || p.DelayMilliseconds > 600000 || p.RetryBefore < 0 {
		return p, asset.ErrInvalid
	}
	return p, nil
}
func fixtureWait(ctx context.Context, p FixturePlan) error {
	if p.Mode == "BLOCK" {
		<-ctx.Done()
		return ctx.Err()
	}
	if p.DelayMilliseconds > 0 && !wait(ctx, time.Duration(p.DelayMilliseconds)*time.Millisecond) {
		return ctx.Err()
	}
	return ctx.Err()
}

// FixtureExecutionTarget 只在明确 FIXTURE_ONLY composition 中装配，绝非生产默认 target。
type FixtureExecutionTarget struct{}

func (FixtureExecutionTarget) Execute(ctx context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	if q.Target.Kind != "FIXTURE" || q.Target.Version != "v1" {
		return ev.Outcome{}, asset.ErrUnsupported
	}
	p, err := fixturePlan(q.Target.Config, "SUCCESS")
	if err != nil {
		return ev.Outcome{}, err
	}
	switch p.Mode {
	case "SUCCESS", "FAILURE", "CONFIRMED_TIMEOUT", "CONFIRMED_CANCELLED", "UNKNOWN", "BLOCK", "DELAY", "PANIC":
	default:
		return ev.Outcome{}, asset.ErrUnsupported
	}
	if p.Mode == "PANIC" {
		panic("CONTROLLED fixture target panic")
	}
	mode := p.Mode
	if fixtureWait(ctx, p) != nil {
		if p.ConfirmedCancellation {
			mode = "CONFIRMED_CANCELLED"
		} else {
			mode = "UNKNOWN"
		}
	}
	out := ev.Outcome{RequestID: q.ID, RemoteID: q.AttemptID, Protocol: "fixture.v1", Source: "CONTROLLED_TEST_ONLY", DispatchCertainty: "REMOTE_ACCEPTED", TerminalCertainty: "REMOTE_CONFIRMED"}
	switch mode {
	case "SUCCESS", "DELAY":
		out.Kind = ev.Success
		body := q.Case.Case.Input
		out.Artifact = &ev.Binding{ProjectID: s.ProjectID, RunID: q.RunID, AttemptID: q.AttemptID, RequestID: q.ID, Ref: "fixture:" + q.AttemptID, Digest: body.Digest(), Schema: "fixture-output.v1", Availability: "AVAILABLE", Body: body}
	case "FAILURE":
		out.Kind = ev.Failure
	case "CONFIRMED_TIMEOUT":
		out.Kind = ev.Timeout
	case "CONFIRMED_CANCELLED":
		out.Kind = ev.Cancelled
	default:
		out.Kind = ev.Unknown
		out.TerminalCertainty = "UNCONFIRMED"
	}
	if out.Kind != ev.Success {
		out.ErrorCategory = "CONTROLLED_" + mode
		out.Reason = "受控 fixture observation"
	}
	return out, nil
}

// FixtureEvaluator 不发送网络请求；模拟 provider 时只登记 G2 provenance。
type FixtureEvaluator struct{ DBTimeout time.Duration }

func (f FixtureEvaluator) Evaluate(ctx context.Context, in EvaluationInput) (EvaluationOutput, error) {
	d := in.Work.Metadata.Spec.Definition
	if !FixtureDefinitionSupported(d) {
		return EvaluationOutput{}, asset.ErrUnsupported
	}
	p, err := fixturePlan(d.Config, "PASS")
	if err != nil {
		return EvaluationOutput{}, err
	}
	switch p.Mode {
	case "PASS", "FAIL", "INCONCLUSIVE", "ERROR", "DELAY", "PANIC", "RETRYABLE", "BLOCK":
	default:
		return EvaluationOutput{}, asset.ErrUnsupported
	}
	if p.Mode == "PANIC" {
		panic("CONTROLLED fixture evaluator panic")
	}
	value := errorValue(in.Work, in.Attempt, "CONTROLLED_ERROR")
	value.Provenance, _ = asset.Freeze(map[string]string{"source": "CONTROLLED_TEST_ONLY", "actual_provider": "NOT_APPLICABLE"})
	var call ev.ProviderCall
	if d.ImplementationRef == FixtureProviderImplementation {
		call = ev.ProviderCall{ID: asset.NewID(), Number: in.Work.CallCount + 1, EvaluationNumber: in.Work.Evaluations,
			Suboperation: "fixture", RequestedProvider: d.Model.Provider, RequestedModel: d.Model.Model, PromptRef: d.PromptRef,
			PromptDigest: "fixture-prompt", Schema: d.SchemaVersion, InputDigest: in.Attempt.Request.Case.Identity.ContentDigest}
		call.EvidenceDigest, _ = ev.Intent(in.Attempt.Metadata.Observation.Evidence)
		if err = f.providerCommand(ctx, func(db context.Context) (ev.Reply, error) {
			return in.Persistence.BeginProviderCall(db, in.Scope, ev.BeginCall{Owned: in.Owned, Call: call})
		}); err != nil {
			return EvaluationOutput{}, err
		}
	}
	if fixtureWait(ctx, p) != nil {
		return EvaluationOutput{}, ctx.Err()
	}
	retry := p.Mode == "RETRYABLE" && (p.RetryBefore == 0 || in.Work.Evaluations <= p.RetryBefore)
	mode := p.Mode
	if mode == "DELAY" || (mode == "RETRYABLE" && !retry) {
		mode = "PASS"
	}
	value.Verdict = mode
	value.SourceVerdict = mode
	value.Reason = "受控 fixture evaluation"
	value.SourceError = ""
	if retry {
		value = errorValue(in.Work, in.Attempt, "CONTROLLED_RETRYABLE")
	} else if mode == "PASS" || mode == "FAIL" {
		score := 1.0
		if mode == "FAIL" {
			score = 0
		}
		value.Score = &score
	}
	if call.ID != "" {
		classification := "RESPONSE_RECEIVED"
		if retry {
			classification = "ERROR"
		}
		response, _ := asset.Freeze(map[string]string{"verdict": value.Verdict})
		value.SelectedCallIDs = []string{call.ID}
		value.Provenance, _ = asset.Freeze(map[string]string{"source": "CONTROLLED_TEST_ONLY", "actual_provider": "fixture"})
		finish := ev.FinishCall{Owned: in.Owned, CallID: call.ID, Classification: classification, ActualProvider: "fixture", ActualModel: "fixture", ActualRevision: d.Model.Revision, Response: response, ResponseDigest: response.Digest(), UsageAvailability: "UNKNOWN", CostAvailability: "UNKNOWN"}
		if !retry {
			finish.Draft = &value
		}
		if err = f.providerCommand(ctx, func(db context.Context) (ev.Reply, error) {
			return in.Persistence.FinishProviderCall(db, in.Scope, finish)
		}); err != nil {
			return EvaluationOutput{}, err
		}
	}
	return EvaluationOutput{Value: value, Retryable: retry}, nil
}
func (f FixtureEvaluator) providerCommand(ctx context.Context, fn func(context.Context) (ev.Reply, error)) error {
	timeout := f.DBTimeout
	if timeout == 0 {
		timeout = 3 * time.Second
	}
	query, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	reply, err := fn(query)
	if err != nil {
		return err
	}
	if reply.Code != ev.Applied && reply.Code != ev.AlreadyApplied {
		return fmt.Errorf("fixture provider command: %s/%s", reply.Code, reply.Reason)
	}
	return nil
}
