package main

import (
	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/delivery"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"context"
	"fmt"
	"os"
	"path/filepath"
)

func finish(ctx context.Context, out string, decisionStore postgres.Decisions, s ev.Scope, plan provider.Stage13PolicyDocument, states []ev.RunState, ids []string, policyRef asset.Ref) {
	replayed := 0
	for _, state := range states {
		for _, result := range state.Results {
			if result.WorkID == nil {
				panic("RESULT_WORK_MISSING")
			}
			var attempt ev.Attempt
			var work ev.Work
			for _, a := range state.Attempts {
				if a.ID == result.AttemptID {
					attempt = a
				}
			}
			for _, w := range state.Works {
				if w.ID == *result.WorkID {
					work = w
				}
			}
			value, err := (agentquality.DeterministicEvaluator{}).Evaluate(ctx, worker.EvaluationInput{Scope: s, Run: state.Run, Attempt: attempt, Work: work})
			must(err)
			if freeze(value.Value).Digest() != freeze(result.Value).Digest() {
				panic("EVALUATOR_REPLAY_CHANGED")
			}
			replayed++
		}
	}
	save(out, "evaluator-replay", map[string]any{"PASS": true, "replayed_results": replayed, "new_model_calls": 0, "new_results_written": 0})
	// 实际逐 Case 跨侧模型政策验证，包含 tool capability 原始载荷。
	pairResults := []provider.Stage13PairDecision{}
	pairs := map[string]ev.Attempt{}
	for _, a := range states[0].Attempts {
		pairs[a.CaseID] = a
	}
	side := func(a ev.Attempt, tool asset.JSON) provider.Stage13PolicyInput {
		for _, e := range a.Metadata.Observation.Evidence {
			if e.Schema != "stage13-subject-receipt.v1" {
				continue
			}
			var wire map[string]asset.JSON
			_ = e.Body.Decode(&wire)
			receipt := wire["actual_subject_receipt"]
			var selected string
			_ = wire["selected_final_run_id"].Decode(&selected)
			if selected != a.ID {
				var children []asset.JSON
				_ = wire["child_subject_receipts"].Decode(&children)
				if len(children) == 1 {
					receipt = children[0]
				}
			}
			var r map[string]asset.JSON
			_ = receipt.Decode(&r)
			return provider.Stage13PolicyInput{Manifest: r["actual_subject_manifest"], Receipt: receipt, ToolIdentity: tool}
		}
		return provider.Stage13PolicyInput{}
	}
	for _, a := range states[1].Attempts {
		p := provider.CompareStage13Models(provider.Stage13PolicyDocument{Mode: "AGENT_REGRESSION", Baseline: side(pairs[a.CaseID], plan.Baseline.ToolIdentity), Candidate: side(a, plan.Candidate.ToolIdentity)})
		pairResults = append(pairResults, p)
	}
	save(out, "comparability", pairResults)
	cmd := decision.Command{ID: asset.NewID(), Baseline: decision.SourceRef{Kind: "RUN_SET", Runs: []string{ids[0]}}, Candidate: decision.SourceRef{Kind: "RUN_SET", Runs: []string{ids[1]}}, Policy: policyRef}
	if _, err := os.Stat(filepath.Join(out, "gate-command.json")); err == nil {
		load(filepath.Join(out, "gate-command.json"), &cmd)
	}
	save(out, "gate-command", cmd)
	gate, err := decisionStore.CreateGate(ctx, s, cmd)
	must(err)
	save(filepath.Join(out, "operator-only"), "release-gate-full", gate)
	g := gate.Stage13
	save(out, "aggregate-metrics", map[string]any{"baseline": g.Baseline, "candidate": g.Candidate, "thresholds": g.Thresholds})
	save(out, "release-gate", map[string]any{"gate_id": gate.GateID, "policy_ref": gate.PolicyRef, "policy_digest": gate.PolicyDigest, "snapshot_digest": gate.SnapshotDigest, "release_policy": g.Version, "regression_policy": g.RegressionPolicy, "decision": gate.Decision, "comparability": g.ComparabilityGate, "absolute": g.AbsoluteMetricGate, "critical": g.CriticalGate, "regression": g.RegressionGate, "evidence_coverage": g.EvidenceCoverageGate, "baseline_comparator_valid": g.BaselineValid, "baseline_quality_status": g.BaselineQualityStatus, "reason_codes": gate.Reasons})
	critical := map[string]int{"PASS": 0, "FAIL": 0, "BLOCKED": 0}
	regression := map[string]int{"IMPROVED": 0, "UNCHANGED": 0, "REGRESSED": 0, "BLOCKED": 0}
	for _, row := range g.Cases {
		regression[row.Classification]++
		for _, a := range states[1].Attempts {
			if a.CaseID == row.CaseID && a.Request.Case.Case.Criticality == "CRITICAL" {
				critical[row.Candidate.Critical]++
			}
		}
	}
	save(out, "critical-summary", critical)
	save(out, "regression-summary", regression)
	for i, st := range states {
		a := []decision.Stage13Aggregate{g.Baseline, g.Candidate}[i]
		save(out, []string{"baseline-execution-summary", "candidate-execution-summary"}[i], map[string]any{"run_id": st.Run.ID, "intent": st.Run.Snapshot.Intent, "status": st.Run.Status, "attempts": len(st.Attempts), "results": len(st.Results), "summary": a})
	}
	save(filepath.Join(out, "operator-only"), "per-case-regression", gate.Cases)
	before := freeze(gate).Digest()
	replay, err := decisionStore.CreateGate(ctx, s, cmd)
	must(err)
	if freeze(replay).Digest() != before {
		panic("GATE_REPLAY_CHANGED")
	}
	save(out, "gate-idempotency", map[string]any{"PASS": true, "gate_id": gate.GateID, "gate_digest": before, "new_model_calls": 0})
	snapshot, err := decisionStore.GetComparison(ctx, s.Scope, cmd.ID)
	must(err)
	computed, err := decision.Compare(snapshot)
	must(err)
	if freeze(computed).Digest() != before {
		panic("GATE_FROZEN_REPLAY_CHANGED")
	}
	save(out, "gate-frozen-replay", map[string]any{"PASS": true, "gate_digest": before, "snapshot_digest": gate.SnapshotDigest, "new_model_calls": 0, "new_results_written": 0})

	fmt.Printf("controlled evaluation complete; main attempts=%d+%d; candidate release gate=%s\n", len(states[0].Attempts), len(states[1].Attempts), gate.Decision)
}

// projectAuthorization 只使用既有正式投影，不触发 sink 或业务执行。
func projectAuthorization(ctx context.Context, out string, store postgres.Decisions, s ev.Scope, gate decision.Receipt, candidate ev.RunState) {
	authorizations := []delivery.Authorization{}
	counts := map[string]int{"AUTHORIZED": 0, "DENIED": 0}
	for _, a := range candidate.Attempts {
		g, o, results, e := store.Stage13DeliveryBinding(ctx, s.Scope, gate.GateID, a.ID)
		if e != nil {
			if gate.Decision == decision.Pass {
				must(e)
			}
			counts["DENIED"]++
			continue
		}
		q := delivery.Request{Gate: g, Outcome: o, Destination: delivery.Destination{Kind: delivery.SinkKind, Scope: "TEST_SCOPE", ID: "wp11-eligibility-only"}}
		auth, e := store.AuthorizeStage13Delivery(ctx, s.Scope, q)
		must(e)
		if len(results) != 7 {
			panic("AUTHORIZATION_RESULT_BINDING")
		}
		authorizations = append(authorizations, auth)
		counts[auth.Status]++
	}
	save(filepath.Join(out, "operator-only"), "delivery-authorization-full", authorizations)
	status := "DENIED"
	if gate.Decision == decision.Pass && counts["AUTHORIZED"] == 6 {
		status = "ELIGIBLE"
	}
	save(out, "delivery-authorization-projection", map[string]any{"status": status, "counts": counts, "gate_decision": gate.Decision, "read_only": true, "new_delivery_payloads": 0, "enterprise_side_effects": 0})
}
