//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/asset"
	gov "agentevalops/go-backend/internal/cigovernance"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/provider"
	"context"
	"strings"
	"testing"
	"time"
)

func wp09Config(t *testing.T) asset.JSON {
	t.Helper()
	d := strings.Repeat("a", 64)
	m := map[string]any{"subject_id": "ci_triage_candidate", "subject_version": "frozen-test-v1", "agent_id": "ci_triage_candidate", "agent_definition_version": "frozen-test-v1", "agent_definition_digest": d, "prompt_version": "frozen-prompt-v1", "prompt_digest": d, "model_profile_id": "remote_advanced", "model_profile_digest": d, "requested_provider": "deepseek", "requested_model": "deepseek-flash", "requested_revision": nil, "tool_profile_id": "read-only-v1", "tool_profile_digest": d, "output_schema_version": "stage13.triage-output.v1", "output_schema_digest": d, "execution_enabled": true}
	m["subject_manifest_digest"] = gov.Freeze(m).Digest()
	return gov.Freeze(provider.Stage13Config{Transport: provider.DefaultLocalAgentConfig("http://127.0.0.1:55438", "WP09_TEST_TOKEN"), ExpectedSubjectManifest: gov.Freeze(m), ExecutionPolicyVersion: provider.Stage13ExecutionPolicyVersion})
}

func TestWP09ReleaseConsumptionAndFrozenIdentity(t *testing.T) {
	f := g8(t)
	wp07Permissions(t, f.g7Fixture)
	ctx := context.Background()
	var cmd ev.CreateRun
	for _, role := range []string{"DEVELOPMENT", "CALIBRATION", "HOLDOUT"} {
		s, _, _, body, item := wp07Case(t, f.g7Fixture, role, false)
		review := wp07Decision(t, f.g7Fixture, s, item, body.GroundTruth, "CONFIRMED")
		c := wp07Publish(t, f.g7Fixture, s, review, body)
		d := wp07Dataset(t, f.g7Fixture, s, c, role)
		snapshot, e := (ev.Builder{Assets: g6Readers(f.g6Fixture)}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: ptrRef(d.Ref()), Evaluators: f.bindings})
		mustG7(t, e)
		q := f.cmd
		q.Snapshot = snapshot
		q.Intent = "RELEASE_EVALUATION"
		q.CommandID = asset.NewID()
		q.Target.ID = provider.Stage13TargetID
		q.Target.Config = wp09Config(t)
		if role != "HOLDOUT" {
			r, e := f.k.CreateRun(ctx, f.s, q)
			mustG7(t, e)
			if r.Code != ev.Rejected {
				t.Fatal("release accepted", role, r)
			}
		} else {
			cmd = q
		}
	}
	r, e := f.k.CreateRun(ctx, f.s, cmd)
	mustG7(t, e)
	if r.Code != ev.Applied {
		t.Fatal(r)
	}
	run := r.ID
	cmd.CommandID = asset.NewID()
	r, e = f.k.CreateRun(ctx, f.s, cmd)
	mustG7(t, e)
	if r.Code != ev.AlreadyApplied || r.ID != run {
		t.Fatal("duplicate release created", r)
	}
	var count int
	var consumed time.Time
	var metadata string
	mustG7(t, f.k.Pool.QueryRow(ctx, "SELECT count(*) FROM evaluation_runs WHERE project_id=$1 AND kernel_snapshot->>'Intent'='RELEASE_EVALUATION'", f.s.ProjectID).Scan(&count))
	mustG7(t, f.k.Pool.QueryRow(ctx, "SELECT created_at,metadata->'holdout_consumption'->>'state' FROM evaluation_runs WHERE id=$1", run).Scan(&consumed, &metadata))
	if count != 1 || consumed.IsZero() || metadata != "CONSUMED" {
		t.Fatal("consumption not atomic", count, metadata)
	}
	original := cmd.Target.Config
	var cfg provider.Stage13Config
	mustG7(t, original.Decode(&cfg))
	var manifest map[string]asset.JSON
	mustG7(t, cfg.ExpectedSubjectManifest.Decode(&manifest))
	manifest["prompt_digest"] = gov.Freeze(strings.Repeat("b", 64))
	cfg.ExpectedSubjectManifest = gov.Freeze(manifest)
	cmd.Target.Config = gov.Freeze(cfg)
	r, e = f.k.CreateRun(ctx, f.s, cmd)
	mustG7(t, e)
	if r.Code != ev.Rejected {
		t.Fatal("manifest digest mismatch accepted", r)
	}
	delete(manifest, "subject_manifest_digest")
	manifest["subject_manifest_digest"] = gov.Freeze(gov.Freeze(manifest).Digest())
	cfg.ExpectedSubjectManifest = gov.Freeze(manifest)
	cmd.Target.Config = gov.Freeze(cfg)
	r, e = f.k.CreateRun(ctx, f.s, cmd)
	mustG7(t, e)
	if r.Code != ev.Conflict {
		t.Fatal("post-freeze mutation accepted", r)
	}
	op := f.s
	op.Operator = true
	r, e = f.k.CreateRunRerun(ctx, op, ev.Rerun{CommandID: asset.NewID(), SourceRunID: run, Reason: "cannot resample holdout", Trigger: "CONTROLLED_TEST"})
	mustG7(t, e)
	if r.Code != ev.Rejected || r.Reason != "HOLDOUT_ALREADY_CONSUMED" {
		t.Fatal("rerun bypass", r)
	}
	// 已创建但未执行也保持消费；终态失败不会删除或重置该事实。
	cmd.Target.Config = original
	cmd.CommandID = asset.NewID()
	r, e = f.k.CreateRun(ctx, f.s, cmd)
	mustG7(t, e)
	if r.Code != ev.AlreadyApplied || r.ID != run {
		t.Fatal("failed checks reset consumption", r)
	}
}
