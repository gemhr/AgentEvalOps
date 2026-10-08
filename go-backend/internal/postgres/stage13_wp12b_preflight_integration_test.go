//go:build integration

package postgres_test

import (
	"context"
	"testing"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
)

func TestWP12BSourceExposedHoldoutRejectsRelease(t *testing.T) {
	// 使用临时数据库中的已有合成 fixture；不连接真实 golden-v3 项目或读取其正文。
	f := g8(t)
	wp07Permissions(t, f.g7Fixture)
	ctx := context.Background()
	s, _, _, body, item := wp07Case(t, f.g7Fixture, "HOLDOUT", false)
	r := wp07Decision(t, f.g7Fixture, s, item, body.GroundTruth, "CONFIRMED")
	c := wp07Publish(t, f.g7Fixture, s, r, body)
	d := wp07Dataset(t, f.g7Fixture, s, c, "HOLDOUT")
	ds := catalog.DatasetService{Store: postgres.Datasets{Pool: f.k.Pool}, Cases: postgres.Cases{Pool: f.k.Pool}}
	id := "0f94fa11-8194-5afd-8c66-30be45cfec79"
	_, err := ds.CreateDataset(ctx, s.Scope.Scope, asset.Create{ID: id, Name: "WP12B source-exposure synthetic fixture"})
	mustG7(t, err)
	for _, version := range []string{"source-exposure-fixture", "repackaged-fixture"} {
		ref := asset.Ref{EntityID: id, Version: version}
		_, err = ds.PublishDatasetVersion(ctx, s.Scope.Scope, asset.Publish[catalog.DatasetContent]{Ref: ref, Body: d.Content().Body, Source: d.Content().Source})
		mustG7(t, err)
		snapshot, err := (ev.Builder{Assets: g6Readers(f.g6Fixture)}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &ref, Evaluators: f.bindings})
		mustG7(t, err)
		cmd := f.cmd
		cmd.Snapshot, cmd.Intent, cmd.CommandID = snapshot, "RELEASE_EVALUATION", asset.NewID()
		cmd.Target.ID, cmd.Target.Config = provider.Stage13TargetID, wp09Config(t)
		reply, err := f.k.CreateRun(ctx, f.s, cmd)
		mustG7(t, err)
		if reply.Code != ev.Rejected || reply.Reason != "HOLDOUT_EXPOSED" {
			t.Fatal("source-exposed holdout accepted release", reply)
		}
	}
	var runs int
	mustG7(t, f.k.Pool.QueryRow(ctx, `SELECT count(*) FROM evaluation_runs WHERE dataset_id=$1`, id).Scan(&runs))
	if runs != 0 {
		t.Fatal("source-exposure rejection created a run")
	}
}
