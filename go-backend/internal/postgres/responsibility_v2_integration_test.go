//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	gov "agentevalops/go-backend/internal/cigovernance"
	"agentevalops/go-backend/internal/citriage"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"context"
	"github.com/jackc/pgx/v5"
	"os"
	"slices"
	"testing"
	"time"
)

func responsibilityFixture(t *testing.T) (asset.JSON, citriage.GroundTruth) {
	t.Helper()
	raw, e := os.ReadFile("../citriage/testdata/world-test-only.json")
	mustG7(t, e)
	world, e := asset.ParseJSON(raw)
	mustG7(t, e)
	input, e := citriage.MaterializeWorldInput(world, []string{})
	mustG7(t, e)
	change := "fixture-change"
	gt, e := citriage.DeriveGroundTruth(world, citriage.ControlledFault{ID: "integration-test", Target: citriage.EntityRef{Kind: "ARTIFACT", ID: "fixture-helper"}, Mechanism: "TEST_LOGIC", Change: &change, Category: "TEST_CASE", Actions: []string{"FIX_TEST"}, Ticket: "IGNORE", Criticality: "NORMAL"})
	mustG7(t, e)
	return input, gt
}
func responsibilityPolicy() gov.Policy {
	return gov.Policy{Version: gov.GovernanceV2, Role: "DEVELOPMENT", Family: "TEST-ONLY-contract-fixture", State: "PENDING_REVIEW", GTMapping: gov.RemediationGTMapping, Split: gov.RemediationSplit, Profile: gov.RemediationDevelopmentProfile, Frozen: true}
}
func responsibilityProof(t *testing.T, input asset.JSON, gt citriage.GroundTruth) *gov.ResponsibilityReview {
	t.Helper()
	in, e := citriage.ReadInputV2(input)
	mustG7(t, e)
	ids := citriage.ContractIdentities()
	r := gt.Roots[0]
	truth := r.Responsibility
	return &gov.ResponsibilityReview{Semantics: citriage.ResponsibilityV2, SemanticsDigest: ids["responsibility_contract_digest"], InputVersion: citriage.InputV2, InputContractDigest: ids["input_contract_digest"], InputDigest: input.Digest(), BusinessDigest: gov.Freeze(in.Business).Digest(), Roots: []gov.ResponsibilityRootReview{{ID: r.ID, Target: &truth.Target, Relations: truth.Relations, Mechanism: true, Change: true, Owner: true, Resolution: "UNIQUE", Owners: []string{*truth.Owner}, Provenance: "TEST-ONLY source content/digest/pointers explicitly match OWNS/TARGETS", Causal: "TEST-ONLY controlled fault targets fixture-helper"}}}
}

func TestResponsibilityV2PostgresHTTPReviewGoldenAdmission(t *testing.T) {
	f := g8(t)
	wp07Permissions(t, f.g7Fixture)
	ctx := context.Background()
	input, gt := responsibilityFixture(t)
	for _, state := range []string{"CONFIRMED", "CORRECTED", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE", "REJECTED"} {
		t.Run(state, func(t *testing.T) {
			s := f.reviewScope
			s.ReviewerType = "SYSTEM_IMPORT"
			ref := asset.Ref{EntityID: asset.NewID(), Version: "test-only-pending-v2"}
			p := responsibilityPolicy()
			p.Family += "-" + state
			body := catalog.CaseContent{Input: input, GroundTruth: gov.Freeze(gt), TaskGoal: "TEST-ONLY责任合同验证", AcceptanceCriteria: []string{"冻结责任合同"}, Applicability: asset.Applicability{RuleRef: "test-only.v2"}, Type: catalog.AgentTask, Capability: "CI_FAILURE_TRIAGE", Criticality: catalog.Normal, BodyPolicy: catalog.Retained, Metadata: gov.Metadata(p)}
			if state == "CORRECTED" {
				bad := gt
				bad.Ticket = "ESCALATE_HUMAN"
				body.GroundTruth = gov.Freeze(bad)
			}
			store := catalog.CaseService{Store: postgres.Cases{Pool: f.k.Pool}}
			_, e := store.CreateCase(ctx, s.Scope.Scope, asset.Create{ID: ref.EntityID, Name: ref.EntityID})
			mustG7(t, e)
			_, e = store.PublishCaseVersion(ctx, s.Scope.Scope, asset.Publish[catalog.CaseContent]{Ref: ref, Body: body, Source: asset.Source{Kind: "EVALUATION_DATASET_GENERATION", Ref: "TEST-ONLY-WP12CT-contract", Principal: s.Principal}})
			mustG7(t, e)
			original, e := (postgres.Cases{Pool: f.k.Pool}).GetCaseVersion(ctx, s.Scope.Scope, ref)
			mustG7(t, e)
			item, e := f.reviews.EnqueueReview(ctx, s, rv.Enqueue{ID: asset.NewID(), Source: rv.SourceRef{Type: "CONTROLLED_CASE", Case: &ref}, Schema: f.quality, Kind: rv.Quality, Policy: rv.Policy{Ref: "test-only-v2", Reviews: 1, Protocol: rv.Protocol{Blind: true}, Sampling: rv.Sampling{Version: "review-hash.v1", Seed: "test-only", BasisPoints: 10000}}, Reason: "TEST-ONLY责任审核", Priority: "CRITICAL", Criticality: "NORMAL"})
			mustG7(t, e)
			claim, e := f.reviews.ClaimReview(ctx, s, item.ID, time.Minute)
			mustG7(t, e)
			value := map[string]string{"CONFIRMED": "PASS", "CORRECTED": "FAIL", "AMBIGUOUS": "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE": "INCONCLUSIVE", "REJECTED": "ERROR"}[state]
			annotation := submitG7(t, f.reviews, s, item, claim, value)
			r := gov.ReviewDecision{ID: asset.NewID(), ItemID: item.ID, Case: ref, PreviousGTDigest: body.GroundTruth.Digest(), State: state, GroundTruth: gov.Freeze(gt), Reason: "TEST_ONLY_CONTROLLED_SOURCE", AnnotationID: annotation.ID, DecisionContractVersion: gov.ResponsibilityReviewV2, ResponsibilityReview: responsibilityProof(t, input, gt)}
			if gov.HardGolden(state) {
				golden, e := f.reviews.PublishGoldenLabel(ctx, s, asset.NewID(), item.ID)
				mustG7(t, e)
				r.GoldenID = golden.ID
				bad := r
				bad.ID = asset.NewID()
				proof := *r.ResponsibilityReview
				proof.Roots = append([]gov.ResponsibilityRootReview{}, proof.Roots...)
				proof.Roots[0].Owners = []string{"component-b"}
				bad.ResponsibilityReview = &proof
				if _, e = f.reviews.StoreStage13GTReview(ctx, s, bad); e == nil {
					t.Fatal("booleans-only hard review stored")
				}
			} else {
				r.GroundTruth = asset.JSON{}
				r.ResponsibilityReview.Roots[0].Resolution = "INSUFFICIENT_EVIDENCE"
				r.ResponsibilityReview.Roots[0].Owner = false
			}
			reviewed, e := f.reviews.StoreStage13GTReview(ctx, s, r)
			mustG7(t, e)
			if reviewed.VerifyDigest() != nil {
				t.Fatal("stored proof digest")
			}
			replayed, e := f.reviews.StoreStage13GTReview(ctx, s, r)
			mustG7(t, e)
			if replayed.Digest != reviewed.Digest {
				t.Fatal("review replay")
			}
			p.State = state
			p.ReviewID = reviewed.ID
			p.Supersedes = &ref
			body.Metadata = gov.Metadata(p)
			body.GroundTruth = reviewed.GroundTruth
			body.Type = catalog.Golden
			if gov.HardGolden(state) {
				// 真实HTTP调用现有Publish入口，后台重新检查持久Review、来源和输入摘要。
				f.call(t, "judge", "POST", f.url("/cases/"+ref.EntityID+"/versions"), "v2-valid-"+state, map[string]any{"version": "test-only-golden-v2", "body": body}, 200)
				published, e := (postgres.Cases{Pool: f.k.Pool}).GetCaseVersion(ctx, s.Scope.Scope, asset.Ref{EntityID: ref.EntityID, Version: "test-only-golden-v2"})
				mustG7(t, e)
				dp := responsibilityPolicy()
				dp.Family = ""
				dp.State = ""
				dp.Families = []string{p.Family}
				ds := catalog.DatasetService{Store: postgres.Datasets{Pool: f.k.Pool}, Cases: postgres.Cases{Pool: f.k.Pool}}
				dr := asset.Ref{EntityID: asset.NewID(), Version: "test-only-v2"}
				_, e = ds.CreateDataset(ctx, s.Scope.Scope, asset.Create{ID: dr.EntityID, Name: dr.EntityID})
				mustG7(t, e)
				_, e = ds.PublishDatasetVersion(ctx, s.Scope.Scope, asset.Publish[catalog.DatasetContent]{Ref: dr, Body: catalog.DatasetContent{Cases: []asset.Ref{published.Ref()}, Metadata: gov.Metadata(dp)}, Source: asset.Source{Kind: "TEST", Ref: "WP12CT", Principal: s.Principal}})
				mustG7(t, e)
				altered := body
				var m map[string]any
				_ = input.Decode(&m)
				m["failure_summary"].(map[string]any)["observations"] = []string{"changed"}
				altered.Input = gov.Freeze(m)
				f.call(t, "judge", "POST", f.url("/cases/"+ref.EntityID+"/versions"), "v2-invalid-"+state, map[string]any{"version": "invalid-v2", "body": altered}, 403)
				_, e = f.db.pool.Exec(ctx, `UPDATE evaluation_stage13_gt_reviews SET decision_bytes='{}' WHERE id=$1`, reviewed.ID)
				if e == nil {
					t.Fatal("review proof mutable")
				}
			} else {
				if _, e = store.PublishCaseVersion(ctx, s.Scope.Scope, asset.Publish[catalog.CaseContent]{Ref: asset.Ref{EntityID: ref.EntityID, Version: "invalid-golden-v2"}, Body: body, Source: original.Content().Source}); e == nil {
					t.Fatal("nonhard entered Golden")
				}
			}
			after, e := (postgres.Cases{Pool: f.k.Pool}).GetCaseVersion(ctx, s.Scope.Scope, ref)
			mustG7(t, e)
			if !slices.Equal(after.Bytes(), original.Bytes()) {
				t.Fatal("original CaseVersion mutated")
			}
		})
	}
}

func TestResponsibilityV2DurableExecutionRequest(t *testing.T) {
	f := fixture(t)
	ctx := context.Background()
	input, _ := responsibilityFixture(t)
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: f.k.Pool}, Datasets: postgres.Datasets{Pool: f.k.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: f.k.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: f.k.Pool}}
	old := f.cmd.Snapshot.Input()
	cr := old.Manifest[0].Identity.Ref
	cr.Version = "test-only-execution-v2"
	body := testCase()
	body.Input = input
	source := asset.Source{Kind: "TEST", Ref: "WP12CT", Principal: f.s.Principal}
	_, e := (catalog.CaseService{Store: readers.Cases}).PublishCaseVersion(ctx, f.s.Scope, asset.Publish[catalog.CaseContent]{Ref: cr, Body: body, Source: source})
	mustG7(t, e)
	dr := old.Dataset.Ref
	dr.Version = "test-only-execution-v2"
	_, e = (catalog.DatasetService{Store: readers.Datasets, Cases: readers}).PublishDatasetVersion(ctx, f.s.Scope, asset.Publish[catalog.DatasetContent]{Ref: dr, Body: catalog.DatasetContent{Cases: []asset.Ref{cr}}, Source: source})
	mustG7(t, e)
	bindings := []catalog.EvaluatorBinding{}
	for _, b := range old.Evaluators {
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: b.Identity.Ref, Metrics: b.Metrics, Required: b.Required, Applicability: b.Applicability})
	}
	f.cmd.Snapshot, e = (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, f.s.Scope, ev.BuildRunSnapshot{Dataset: &dr, Evaluators: bindings})
	mustG7(t, e)
	m := map[string]any{"agent_id": "test_only_contract_subject"}
	for k, v := range citriage.ContractIdentities() {
		m[k] = v
	}
	var domain map[string]any
	_ = citriage.ComparisonDomain().Decode(&domain)
	m["output_schema_digest"] = domain["output_schema_digest"]
	f.cmd.Target.TimeoutMilliseconds = 180000
	f.cmd.Target.Config = gov.Freeze(map[string]any{"execution_policy_version": citriage.ExecutionPolicyV2, "expected_subject_manifest": m})
	var role string
	mustG7(t, f.k.Pool.QueryRow(ctx, "SELECT current_user").Scan(&role))
	_, e = f.db.pool.Exec(ctx, "GRANT INSERT ON evaluation_stage13_execution_requests TO "+pgx.Identifier{role}.Sanitize())
	mustG7(t, e)
	o := create(t, f)
	o = start(t, f, o, 3*time.Minute)
	state, e := f.k.ReadRunState(ctx, f.s.Scope, o.RunID)
	mustG7(t, e)
	q := state.Attempts[0].Request
	first, e := f.k.ReserveStage13Execution(ctx, f.s, q)
	mustG7(t, e)
	restart := postgres.Evaluation{Pool: f.k.Pool, Capabilities: f.k.Capabilities}
	again, e := restart.ReserveStage13Execution(ctx, f.s, q)
	mustG7(t, e)
	if first.String() != again.String() {
		t.Fatal("restart rewrote envelope")
	}
	var fields map[string]asset.JSON
	_ = first.Decode(&fields)
	var p citriage.ExecutionPolicy
	_ = fields["execution_policy"].Decode(&p)
	if p.Version != citriage.ExecutionPolicyV2 || p.DomainDigest != citriage.ComparisonDomain().Digest() || p.DeadlineAt.Sub(p.StartedAt) != 180*time.Second || p.SourceInputDigest != input.Digest() {
		t.Fatal("durable identities", p)
	}
	var actualStarted time.Time
	mustG7(t, f.k.Pool.QueryRow(ctx, `SELECT started_at FROM evaluation_attempts WHERE id=$1`, o.AttemptID).Scan(&actualStarted))
	if !p.StartedAt.Equal(actualStarted) {
		t.Fatal("execution budget did not originate from durable started_at")
	}
	var stored string
	mustG7(t, f.k.Pool.QueryRow(ctx, `SELECT request_digest FROM evaluation_stage13_execution_requests WHERE attempt_id=$1`, o.AttemptID).Scan(&stored))
	if stored != first.Digest() {
		t.Fatal("persisted request digest")
	}
	_, e = f.db.pool.Exec(ctx, `UPDATE evaluation_stage13_execution_requests SET request_digest=repeat('0',64) WHERE attempt_id=$1`, o.AttemptID)
	if e == nil {
		t.Fatal("immutable request changed")
	}
}
