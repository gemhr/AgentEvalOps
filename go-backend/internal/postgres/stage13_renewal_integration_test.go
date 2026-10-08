//go:build integration

package postgres_test

import (
	"context"
	"net/http"
	"testing"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
)

func TestWP10AHoldoutMetadataAndAccess(t *testing.T) {
	f := g8(t)
	wp07Permissions(t, f.g7Fixture)
	s, _, _, body, item := wp07Case(t, f.g7Fixture, "HOLDOUT", false)
	r := wp07Decision(t, f.g7Fixture, s, item, body.GroundTruth, "CONFIRMED")
	c := wp07Publish(t, f.g7Fixture, s, r, body)
	d := wp07Dataset(t, f.g7Fixture, s, c, "HOLDOUT")
	path := "/datasets/" + d.Ref().EntityID + "/versions/" + d.Ref().Version + "/holdout-verification"
	response := f.call(t, "reader", "GET", f.url(path), "", nil, 200)
	data := response
	allowed := map[string]bool{"dataset_version": true, "case_count": true, "manifest_digest": true, "role": true, "critical_count": true, "normal_count": true, "integrity_status": true}
	if len(data) != len(allowed) || data["integrity_status"] != "PASS" || data["case_count"] != float64(1) {
		t.Fatal("metadata contract drift")
	}
	for key := range data {
		if !allowed[key] {
			t.Fatal("holdout content escaped whitelist")
		}
	}
	ctx := context.Background()
	dev := seedProject(t, f.db.pool, asset.NewID())
	admin := identity.Access{Scope: dev, Principal: identity.Principal{ID: "CONTROLLED_SETUP", Capabilities: []identity.Capability{identity.ManageAPIKey, identity.Read}}}
	_, key, err := f.api.Identity.CreateCredential(ctx, admin, asset.NewID(), "WP10A development-only", []identity.Capability{identity.Read}, nil)
	mustG7(t, err)
	for _, route := range []string{path, "/cases/" + c.Ref().EntityID + "/versions/" + c.Ref().Version, "/datasets/" + d.Ref().EntityID + "/versions/" + d.Ref().Version} {
		req, err := http.NewRequest("GET", f.url(route), nil)
		mustG7(t, err)
		req.Header.Set("X-API-Key", key)
		res, err := http.DefaultClient.Do(req)
		mustG7(t, err)
		res.Body.Close()
		if res.StatusCode != 404 {
			t.Fatal("development credential accessed holdout", res.StatusCode)
		}
	}
	ds := catalog.DatasetService{Store: postgres.Datasets{Pool: f.k.Pool}, Cases: postgres.Cases{Pool: f.k.Pool}}
	// 固定版本的 canonical bytes 和底层 UPDATE 均不可变。
	changed := d.Content().Body
	changed.Cases = nil
	_, err = ds.PublishDatasetVersion(ctx, s.Scope.Scope, asset.Publish[catalog.DatasetContent]{Ref: d.Ref(), Body: changed, Source: d.Content().Source})
	if err == nil {
		t.Fatal("frozen version mutation accepted")
	}
	_, err = f.db.pool.Exec(ctx, `UPDATE evaluation_dataset_versions SET content_digest='tampered' WHERE project_id=$1 AND entity_id=$2`, s.ProjectID, d.Ref().EntityID)
	if err == nil {
		t.Fatal("holdout manifest mutable")
	}
	var raw []byte
	err = f.db.pool.QueryRow(ctx, `SELECT canonical_bytes FROM evaluation_dataset_versions WHERE project_id=$1 AND entity_id=$2 AND version=$3`, s.ProjectID, d.Ref().EntityID, d.Ref().Version).Scan(&raw)
	mustG7(t, err)
	if string(raw) != string(d.Bytes()) {
		t.Fatal("frozen manifest changed")
	}
	// 退役检查放在原消费重放之后；新主体不能再消费已曝光资产。
	retired := asset.Ref{EntityID: "6fb67037-6512-562a-8227-e842851e6f0f", Version: "golden-v1"}
	_, err = ds.CreateDataset(ctx, s.Scope.Scope, asset.Create{ID: retired.EntityID, Name: "retired holdout fixture"})
	mustG7(t, err)
	_, err = ds.PublishDatasetVersion(ctx, s.Scope.Scope, asset.Publish[catalog.DatasetContent]{Ref: retired, Body: d.Content().Body, Source: d.Content().Source})
	mustG7(t, err)
	snapshot, err := (ev.Builder{Assets: g6Readers(f.g6Fixture)}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &retired, Evaluators: f.bindings})
	mustG7(t, err)
	cmd := f.cmd
	cmd.Snapshot, cmd.Intent, cmd.CommandID, cmd.Target.ID, cmd.Target.Config = snapshot, "RELEASE_EVALUATION", asset.NewID(), provider.Stage13TargetID, wp09Config(t)
	reply, err := f.k.CreateRun(ctx, f.s, cmd)
	mustG7(t, err)
	if reply.Code != ev.Rejected || reply.Reason != "HOLDOUT_CONSUMED_EXPOSED" {
		t.Fatal("retired holdout accepted new release", reply)
	}
}
