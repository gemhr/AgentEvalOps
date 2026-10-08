package main

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"context"
	"github.com/jackc/pgx/v5/pgxpool"
	"path/filepath"
)

// 对应 WP05A 的冻结比较维度：execution policy 已单独比较。
func targetProofDigest(cfg provider.Stage13Config) string {
	cfg.ExecutionPolicyVersion = ""
	return freeze(cfg).Digest()
}

// replayGate 只读原 Run/Result；不装配 Target/Worker，不访问 LocalAgent，不重新创建 Run。
func replayGate(ctx context.Context, out string, pool *pgxpool.Pool, k postgres.Evaluation, s ev.Scope) {
	var ids []string
	load(filepath.Join(out, "main-run-ids.json"), &ids)
	if len(ids) != 2 {
		panic("RELEASE_RUN_SET_MISMATCH")
	}
	states := []ev.RunState{}
	sources := []decision.Source{}
	for _, id := range ids {
		st, e := k.ReadRunState(ctx, s.Scope, id)
		must(e)
		if !st.Run.Status.Terminal() {
			panic("REPLAY_REQUIRES_TERMINAL")
		}
		states = append(states, st)
		src, e := decision.Offline(decision.SourceRef{Kind: "RUN_SET", Runs: []string{id}}, []ev.RunState{st})
		must(e)
		if len(src.Reasons) != 0 {
			panic("SOURCE_IDENTITY_BLOCKED")
		}
		sources = append(sources, src)
	}
	var first decision.Receipt
	load(filepath.Join(out, "release-gate.json"), &first)
	var binding struct {
		Ref    asset.Ref       `json:"policy_ref"`
		Policy decision.Policy `json:"policy"`
	}
	load(filepath.Join(out, "policy-binding.json"), &binding)
	var pairs []provider.Stage13PairDecision
	load(filepath.Join(out, "comparability.json"), &pairs)
	if len(pairs) != 6 {
		panic("ACTUAL_PAIRS_MISSING")
	}
	for _, p := range pairs {
		if !p.Comparable {
			panic("ACTUAL_COMPARABILITY_BLOCKED")
		}
	}
	binding.Policy.AcceptedDifferences[0].Baseline = sources[0].Dimensions["target_config"]
	binding.Policy.AcceptedDifferences[0].Candidate = sources[1].Dimensions["target_config"]
	binding.Policy.AcceptedDifferences[0].Proof = "stage13.model-comparability.v2:" + freeze(pairs).Digest()
	oldRef := binding.Ref
	binding.Ref.Version = "v2-binding-repair"
	store := postgres.Decisions{Pool: pool}
	pv, e := store.PublishPolicyVersion(ctx, s.Scope, asset.Publish[decision.Policy]{Ref: binding.Ref, Body: binding.Policy, Source: asset.Source{Kind: "CONTROLLED_RELEASE_BINDING", Ref: "stage13.wp09-target-proof-repair.v1", Principal: s.Principal}})
	must(e)
	save(out, "final-policy-binding", map[string]any{"policy_ref": binding.Ref, "policy_digest": pv.ContentDigest(), "policy": binding.Policy, "gate_rules_changed": false})
	cmd := decision.Command{ID: asset.NewID(), Baseline: decision.SourceRef{Kind: "RUN_SET", Runs: []string{ids[0]}}, Candidate: decision.SourceRef{Kind: "RUN_SET", Runs: []string{ids[1]}}, Policy: binding.Ref}
	save(out, "gate-command", cmd)
	var plan provider.Stage13PolicyDocument
	load(filepath.Join(out, "release-plan.json"), &plan)
	finish(ctx, out, store, s, plan, states, ids, binding.Ref)
	var gate decision.Receipt
	load(filepath.Join(out, "release-gate.json"), &gate)
	saveConsumption(ctx, out, pool, ids[1], gate.GateID, string(gate.Decision))
	projectAuthorization(ctx, out, store, s, gate, states[1])
	preserved, e := store.GetGateReceipt(ctx, s.Scope, first.GateID)
	must(e)
	if freeze(preserved).Digest() != freeze(first).Digest() {
		panic("FIRST_GATE_MUTATED")
	}
	save(out, "gate-binding-repair", map[string]any{"PASS": true, "first_gate_id": first.GateID, "first_policy_ref": oldRef, "first_gate_preserved": true, "final_gate_id": gate.GateID, "final_policy_ref": binding.Ref, "final_policy_digest": pv.ContentDigest(), "changed_binding": "target_config proof uses WP05A dimension without execution_policy_version", "gate_rules_changed": false, "new_attempts": 0, "new_runtime_runs": 0, "new_model_calls": 0, "new_results": 0})
}
