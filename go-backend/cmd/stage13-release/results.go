package main

import (
	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
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
	save(out, "release-gate", gate)
	save(out, "aggregate-metrics", gate.Stage13)
	save(out, "per-case-regression", gate.Cases)
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
