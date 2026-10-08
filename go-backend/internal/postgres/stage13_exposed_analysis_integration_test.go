//go:build integration

package postgres_test

import (
	"context"
	"fmt"
	"testing"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	gov "agentevalops/go-backend/internal/cigovernance"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
)

func TestWP10BExposedExportOnlyRetiredOriginalAssets(t *testing.T) {
	f := g8(t)
	wp07Permissions(t, f.g7Fixture)
	ctx := context.Background()
	s, _, _, body, _ := wp07Case(t, f.g7Fixture, "HOLDOUT", false)
	s.Scope.Scope.CanPublish = true
	cs := catalog.CaseService{Store: postgres.Cases{Pool: f.k.Pool}}
	ds := catalog.DatasetService{Store: postgres.Datasets{Pool: f.k.Pool}, Cases: postgres.Cases{Pool: f.k.Pool}}
	ids := []string{"ccebd448-f92f-52e5-b421-a685e01bc92e", "ae9f9c6a-a812-5214-a5a3-ca6a4d7f99c6", "e8f57538-ea2e-536b-8a6d-4e7b58f0693d", "1a98685a-3c1d-5a9e-a5a8-ec72c6c0d773", "880ee15e-dd86-5dda-8541-e8520e6a49c4", "4e1a74c2-05c0-5189-b5f6-96ced4d5487d"}
	refs, families := []asset.Ref{}, []string{}
	for i, id := range ids {
		p, err := gov.ReadPolicy(body.Metadata)
		mustG7(t, err)
		p.Family, p.State, p.ReviewID = fmt.Sprintf("exposed-fixture-%d", i), "PENDING_REVIEW", ""
		body.Metadata = gov.Metadata(*p)
		ref := asset.Ref{EntityID: id, Version: "generated-v1"}
		_, err = cs.CreateCase(ctx, s.Scope.Scope, asset.Create{ID: id, Name: id})
		mustG7(t, err)
		_, err = cs.PublishCaseVersion(ctx, s.Scope.Scope, asset.Publish[catalog.CaseContent]{Ref: ref, Body: body, Source: asset.Source{Kind: "EVALUATION_DATASET_GENERATION", Ref: gov.Profile, Principal: s.Principal}})
		mustG7(t, err)
		item, err := f.reviews.EnqueueReview(ctx, s, rv.Enqueue{ID: asset.NewID(), Source: rv.SourceRef{Type: "CONTROLLED_CASE", Case: &ref}, Schema: f.quality, Kind: rv.Quality, Policy: rv.Policy{Ref: "wp10b-controlled.v1", Reviews: 1, Protocol: rv.Protocol{Blind: true}, Sampling: rv.Sampling{Version: "review-hash.v1", Seed: "wp10b", BasisPoints: 10000}}, Reason: "曝光导出 fixture 审核", Priority: "CRITICAL", Criticality: "CRITICAL"})
		mustG7(t, err)
		r := wp07Decision(t, f.g7Fixture, s, item, body.GroundTruth, "CONFIRMED")
		c := wp07Publish(t, f.g7Fixture, s, r, body)
		refs, families = append(refs, c.Ref()), append(families, p.Family)
	}
	ref := asset.Ref{EntityID: "6fb67037-6512-562a-8227-e842851e6f0f", Version: "golden-v1"}
	_, err := ds.CreateDataset(ctx, s.Scope.Scope, asset.Create{ID: ref.EntityID, Name: "retired fixture"})
	mustG7(t, err)
	d, err := ds.PublishDatasetVersion(ctx, s.Scope.Scope, asset.Publish[catalog.DatasetContent]{Ref: ref, Body: catalog.DatasetContent{Cases: refs, Metadata: gov.Metadata(gov.Policy{Version: gov.Contract, Role: "HOLDOUT", GTMapping: gov.GTMapping, Split: gov.SplitVersion, Profile: gov.Profile, Frozen: true, Families: families})}, Source: asset.Source{Kind: "EVALUATION_DATASET_GENERATION", Ref: gov.Profile, Principal: s.Principal}})
	mustG7(t, err)
	out, err := ds.ExportExposedAnalysis(ctx, s.Scope.Scope, ref)
	mustG7(t, err)
	if len(out.Cases) != 6 || out.SourceRole != "CONSUMED_EXPOSED" || out.Usage != "EXPOSED_DEVELOPMENT_ANALYSIS" || out.Cases[0].GroundTruth.Digest() != body.GroundTruth.Digest() {
		t.Fatal("exposed export contract drift")
	}
	f.call(t, "reader", "GET", f.url("/datasets/"+ref.EntityID+"/versions/golden-v1/exposed-analysis-export"), "", nil, 200)
	// 真新 Holdout 的 identity 即使具同项目 READ 也不能使用专用曝光导出。
	for _, denied := range []asset.Ref{{EntityID: "e7a70b04-0712-58bc-a0c8-655eac0aa463", Version: "golden-v2"}, {EntityID: ref.EntityID, Version: "golden-v2"}} {
		if _, err := ds.ExportExposedAnalysis(ctx, s.Scope.Scope, denied); err != asset.ErrForbidden {
			t.Fatal("unexposed asset export", err)
		}
	}
	after, err := ds.GetDatasetVersion(ctx, s.Scope.Scope, ref)
	mustG7(t, err)
	if after.ContentDigest() != d.ContentDigest() {
		t.Fatal("source dataset mutated")
	}
}
