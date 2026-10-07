//go:build integration

package postgres_test

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"slices"
	"strings"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	gov "agentevalops/go-backend/internal/cigovernance"
	"agentevalops/go-backend/internal/citriage"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5"
)

func wp07Permissions(t *testing.T, f *g7Fixture) {
	t.Helper()
	var role string
	mustG7(t, f.k.Pool.QueryRow(context.Background(), "SELECT current_user").Scan(&role))
	_, e := f.db.pool.Exec(context.Background(), "GRANT INSERT ON evaluation_stage13_gt_reviews,evaluation_stage13_feedback TO "+pgx.Identifier{role}.Sanitize())
	mustG7(t, e)
}
func wp07Case(t *testing.T, f *g7Fixture, role string, badGT bool) (rv.Scope, gov.Family, asset.Ref, catalog.CaseContent, rv.Item) {
	t.Helper()
	ctx := context.Background()
	s := f.reviewScope
	s.ReviewerType = "SYSTEM_IMPORT"
	family := gov.Generate("test-wp07", "seed").Families[0]
	family.Role = role
	ref := asset.Ref{EntityID: asset.NewID(), Version: "generated-v1"}
	gt, e := citriage.ReadGroundTruth(family.GT)
	mustG7(t, e)
	if badGT {
		gt.Ticket = "IGNORE"
	}
	p := gov.Policy{Version: gov.Contract, Role: role, Family: family.ID, State: "PENDING_REVIEW", GTMapping: gov.GTMapping, Split: gov.SplitVersion, Profile: gov.Profile, Frozen: true}
	body := catalog.CaseContent{Input: family.Input, GroundTruth: gov.Freeze(gt), TaskGoal: "受控 CI 审核", AcceptanceCriteria: []string{"冻结输出合同"}, Applicability: asset.Applicability{RuleRef: "wp07-test.v1"}, Type: catalog.AgentTask, Capability: "CI_FAILURE_TRIAGE", Criticality: catalog.Criticality(gt.Criticality), BodyPolicy: catalog.Retained, Metadata: gov.Metadata(p)}
	store := catalog.CaseService{Store: postgres.Cases{Pool: f.k.Pool}}
	_, e = store.CreateCase(ctx, s.Scope.Scope, asset.Create{ID: ref.EntityID, Name: ref.EntityID})
	mustG7(t, e)
	_, e = store.PublishCaseVersion(ctx, s.Scope.Scope, asset.Publish[catalog.CaseContent]{Ref: ref, Body: body, Source: asset.Source{Kind: "EVALUATION_DATASET_GENERATION", Ref: gov.Profile, Principal: s.Principal}})
	mustG7(t, e)
	c := rv.Enqueue{ID: asset.NewID(), Source: rv.SourceRef{Type: "CONTROLLED_CASE", Case: &ref}, Schema: f.quality, Kind: rv.Quality, Policy: rv.Policy{Ref: "wp07-controlled.v1", Reviews: 1, Protocol: rv.Protocol{Blind: true}, Sampling: rv.Sampling{Version: "review-hash.v1", Seed: "wp07", BasisPoints: 10000}}, Reason: "受控 GT 审核", Priority: "CRITICAL", Criticality: "CRITICAL"}
	item, e := f.reviews.EnqueueReview(ctx, s, c)
	mustG7(t, e)
	return s, family, ref, body, item
}
func wp07Decision(t *testing.T, f *g7Fixture, s rv.Scope, item rv.Item, gt asset.JSON, state string) gov.ReviewDecision {
	t.Helper()
	ctx := context.Background()
	claim, e := f.reviews.ClaimReview(ctx, s, item.ID, time.Minute)
	mustG7(t, e)
	value := map[string]string{"CONFIRMED": "PASS", "CORRECTED": "FAIL", "AMBIGUOUS": "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE": "INCONCLUSIVE", "REJECTED": "ERROR"}[state]
	a := submitG7(t, f.reviews, s, item, claim, value)
	r := gov.ReviewDecision{ID: asset.NewID(), ItemID: item.ID, Case: *item.Source.Case, PreviousGTDigest: item.Source.CaseBody.GroundTruth.Digest(), State: state, GroundTruth: gt, Reason: "CONTROLLED_SOURCE_MAPPING", AnnotationID: a.ID}
	if gov.HardGolden(state) {
		g, e := f.reviews.PublishGoldenLabel(ctx, s, asset.NewID(), item.ID)
		mustG7(t, e)
		r.GoldenID = g.ID
	}
	out, e := f.reviews.StoreStage13GTReview(ctx, s, r)
	mustG7(t, e)
	again, e := f.reviews.StoreStage13GTReview(ctx, s, r)
	mustG7(t, e)
	if out.Digest != again.Digest {
		t.Fatal("review replay changed")
	}
	return out
}
func wp07Publish(t *testing.T, f *g7Fixture, s rv.Scope, r gov.ReviewDecision, body catalog.CaseContent) catalog.CaseVersion {
	t.Helper()
	ctx := context.Background()
	p, e := gov.ReadPolicy(body.Metadata)
	mustG7(t, e)
	p.State = r.State
	p.ReviewID = r.ID
	p.Supersedes = &r.Case
	body.Metadata = gov.Metadata(*p)
	body.GroundTruth = r.GroundTruth
	g, e := citriage.ReadGroundTruth(body.GroundTruth)
	mustG7(t, e)
	body.Criticality = catalog.Criticality(g.Criticality)
	body.Type = catalog.Golden
	ref := r.Case
	ref.Version = "golden-v1"
	d, e := f.reviews.CreateReviewedCaseDraft(ctx, s, rv.Draft{ID: asset.NewID(), ItemID: r.ItemID, GoldenID: &r.GoldenID, Case: ref, Body: body, Sanitization: "APPROVED", Policy: gov.Contract, Reason: "typed CI GT 新版本"})
	mustG7(t, e)
	c, e := f.reviews.PublishReviewedCase(ctx, s, d.ID, ref.EntityID)
	mustG7(t, e)
	return c
}
func wp07Dataset(t *testing.T, f *g7Fixture, s rv.Scope, c catalog.CaseVersion, role string) catalog.DatasetVersion {
	t.Helper()
	ctx := context.Background()
	ds := catalog.DatasetService{Store: postgres.Datasets{Pool: f.k.Pool}, Cases: postgres.Cases{Pool: f.k.Pool}}
	p, e := gov.ReadPolicy(c.Content().Body.Metadata)
	mustG7(t, e)
	r := asset.Ref{EntityID: asset.NewID(), Version: "golden-v1"}
	_, e = ds.CreateDataset(ctx, s.Scope.Scope, asset.Create{ID: r.EntityID, Name: r.EntityID})
	mustG7(t, e)
	v, e := ds.PublishDatasetVersion(ctx, s.Scope.Scope, asset.Publish[catalog.DatasetContent]{Ref: r, Body: catalog.DatasetContent{Cases: []asset.Ref{c.Ref()}, Metadata: gov.Metadata(gov.Policy{Version: gov.Contract, Role: role, GTMapping: gov.GTMapping, Split: gov.SplitVersion, Profile: gov.Profile, Families: []string{p.Family}, Frozen: true})}, Source: asset.Source{Kind: "GOLDEN_DATASET", Ref: gov.Contract, Principal: s.Principal}})
	mustG7(t, e)
	return v
}

func TestWP07ReviewVersioningAndAccess(t *testing.T) {
	f := g8(t)
	wp07Permissions(t, f.g7Fixture)
	ctx := context.Background()
	ds := catalog.DatasetService{Store: postgres.Datasets{Pool: f.k.Pool}, Cases: postgres.Cases{Pool: f.k.Pool}}
	for _, state := range []string{"CONFIRMED", "CORRECTED", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE", "REJECTED"} {
		t.Run(state, func(t *testing.T) {
			s, family, ref, body, item := wp07Case(t, f.g7Fixture, "DEVELOPMENT", state == "CORRECTED")
			old, e := (postgres.Cases{Pool: f.k.Pool}).GetCaseVersion(ctx, s.Scope.Scope, ref)
			mustG7(t, e)
			truth := family.GT
			if !gov.HardGolden(state) {
				truth = asset.JSON{}
			}
			r := wp07Decision(t, f.g7Fixture, s, item, truth, state)
			if gov.HardGolden(state) {
				published := wp07Publish(t, f.g7Fixture, s, r, body)
				if published.Ref().EntityID != ref.EntityID || published.Ref().Version == ref.Version || published.Content().Body.GroundTruth.Digest() != truth.Digest() {
					t.Fatal("GT correction did not version")
				}
				p, e := gov.ReadPolicy(published.Content().Body.Metadata)
				mustG7(t, e)
				if *p.Supersedes != ref {
					t.Fatal("supersedes missing")
				}
				version := wp07Dataset(t, f.g7Fixture, s, published, "DEVELOPMENT")
				export, e := ds.ExportDevelopment(ctx, s.Scope.Scope, version.Ref())
				mustG7(t, e)
				if len(export) != 1 {
					t.Fatal("development export")
				}
				f.call(t, "reader", "GET", f.url("/datasets/"+version.Ref().EntityID+"/versions/"+version.Ref().Version+"/development-export"), "", nil, 200)
				changed := version.Content().Body
				changed.Cases = []asset.Ref{ref}
				_, e = ds.PublishDatasetVersion(ctx, s.Scope.Scope, asset.Publish[catalog.DatasetContent]{Ref: version.Ref(), Body: changed, Source: version.Content().Source})
				if e == nil {
					t.Fatal("frozen dataset mutation")
				}
			} else {
				p, e := gov.ReadPolicy(body.Metadata)
				mustG7(t, e)
				p.State = state
				p.ReviewID = r.ID
				p.Supersedes = &ref
				body.Metadata = gov.Metadata(*p)
				_, e = (postgres.Cases{Pool: f.k.Pool}).PublishCaseVersion(ctx, s.Scope.Scope, asset.Publish[catalog.CaseContent]{Ref: asset.Ref{EntityID: ref.EntityID, Version: "not-golden"}, Body: body, Source: old.Content().Source})
				if e == nil {
					t.Fatal("ambiguous/insufficient/rejected became hard golden")
				}
			}
			after, e := (postgres.Cases{Pool: f.k.Pool}).GetCaseVersion(ctx, s.Scope.Scope, ref)
			mustG7(t, e)
			if !slices.Equal(after.Bytes(), old.Bytes()) {
				t.Fatal("old CaseVersion mutated")
			}
			_, e = f.db.pool.Exec(ctx, `UPDATE evaluation_case_versions SET content_digest='changed' WHERE project_id=$1 AND entity_id=$2 AND version=$3`, s.ProjectID, ref.EntityID, ref.Version)
			if e == nil {
				t.Fatal("DB immutability missing")
			}
			_, e = f.db.pool.Exec(ctx, `UPDATE evaluation_stage13_gt_reviews SET state='CONFIRMED' WHERE id=$1`, r.ID)
			if e == nil {
				t.Fatal("review mutable")
			}
		})
	}
	t.Run("holdout export and release intent", func(t *testing.T) {
		s, _, _, body, item := wp07Case(t, f.g7Fixture, "HOLDOUT", false)
		r := wp07Decision(t, f.g7Fixture, s, item, body.GroundTruth, "CONFIRMED")
		c := wp07Publish(t, f.g7Fixture, s, r, body)
		d := wp07Dataset(t, f.g7Fixture, s, c, "HOLDOUT")
		changed := c.Content().Body
		policy, e := gov.ReadPolicy(changed.Metadata)
		mustG7(t, e)
		policy.Role = "DEVELOPMENT"
		changed.Metadata = gov.Metadata(*policy)
		if _, e = (postgres.Cases{Pool: f.k.Pool}).PublishCaseVersion(ctx, s.Scope.Scope, asset.Publish[catalog.CaseContent]{Ref: asset.Ref{EntityID: c.Ref().EntityID, Version: "relabelled-development"}, Body: changed, Source: c.Content().Source}); !errors.Is(e, asset.ErrForbidden) {
			t.Fatal("holdout family role relabelled via new version", e)
		}
		if _, e := ds.ExportDevelopment(ctx, s.Scope.Scope, d.Ref()); !errors.Is(e, asset.ErrForbidden) {
			t.Fatal("holdout GT export", e)
		}
		f.call(t, "reader", "GET", f.url("/datasets/"+d.Ref().EntityID+"/versions/"+d.Ref().Version+"/development-export"), "", nil, 403)
		// development-only credential 无法从通用 Case/Result/Dataset route 旁路。
		dev := seedProject(t, f.db.pool, asset.NewID())
		admin := identity.Access{Scope: dev, Principal: identity.Principal{ID: "CONTROLLED_SETUP", Capabilities: []identity.Capability{identity.ManageAPIKey, identity.Read}}}
		_, key, e := f.api.Identity.CreateCredential(ctx, admin, asset.NewID(), "WP07 development-only access test", []identity.Capability{identity.Read}, nil)
		mustG7(t, e)
		for _, path := range []string{"/cases/" + c.Ref().EntityID + "/versions/" + c.Ref().Version, "/datasets/" + d.Ref().EntityID + "/versions/" + d.Ref().Version + "/development-export", "/runs/" + asset.NewID() + "/results"} {
			req, e := http.NewRequest("GET", f.url(path), nil)
			mustG7(t, e)
			req.Header.Set("X-API-Key", key)
			res, e := http.DefaultClient.Do(req)
			mustG7(t, e)
			res.Body.Close()
			if res.StatusCode != 404 {
				t.Fatal("development-only identity accessed holdout project", path, res.StatusCode)
			}
		}
		snapshot, e := (ev.Builder{Assets: g6Readers(f.g6Fixture)}).BuildRunSnapshot(ctx, s.Scope.Scope, ev.BuildRunSnapshot{Dataset: ptrRef(d.Ref()), Evaluators: f.bindings})
		mustG7(t, e)
		cmd := f.cmd
		cmd.Intent = "RELEASE_EVALUATION"
		if _, e = ev.FreezeRun(cmd, s.ProjectID, f.k.Capabilities); !errors.Is(e, asset.ErrForbidden) {
			t.Fatal("historical/unmarked dataset cannot be final holdout", e)
		}
		cmd.Snapshot = snapshot
		cmd.Intent = "DEV_EVALUATION"
		if _, e = ev.FreezeRun(cmd, s.ProjectID, f.k.Capabilities); !errors.Is(e, asset.ErrForbidden) {
			t.Fatal("holdout DEV evaluation", e)
		}
		cmd.Intent = "RELEASE_EVALUATION"
		if _, e = ev.FreezeRun(cmd, s.ProjectID, f.k.Capabilities); e != nil {
			t.Fatal("release frozen holdout", e)
		}
		other := s.Scope.Scope
		other.ProjectID = asset.NewID()
		if _, e = (postgres.Cases{Pool: f.k.Pool}).GetCaseVersion(ctx, other, c.Ref()); !errors.Is(e, asset.ErrNotFound) {
			t.Fatal("cross-project case access", e)
		}
	})
	t.Run("critical priority", func(t *testing.T) {
		s, _, _, _, critical := wp07Case(t, f.g7Fixture, "DEVELOPMENT", false)
		ids, e := f.reviews.PendingStage13Reviews(ctx, s)
		mustG7(t, e)
		if len(ids) == 0 || ids[0] != critical.ID {
			t.Fatal("critical scheduling", ids)
		}
	})
}
func ptrRef(r asset.Ref) *asset.Ref { return &r }

func TestWP07FourReviewerProcesses(t *testing.T) {
	f := g7(t)
	wp07Permissions(t, f)
	ctx := context.Background()
	s, _, _, _, item := wp07Case(t, f, "DEVELOPMENT", false)
	exe, e := os.Executable()
	mustG7(t, e)
	type process struct {
		cmd   *exec.Cmd
		out   <-chan g7ProcessOutput
		owner rv.Scope
	}
	workers := []process{}
	for i := 0; i < 4; i++ {
		owner := s
		owner.Principal = fmt.Sprintf("CONTROLLED_TEST_REVIEWER:wp07-worker-%d", i)
		command := exec.Command(exe, "-test.run=^TestG7ProcessHelper$", "-test.v")
		scope, _ := json.Marshal(owner)
		input, _ := json.Marshal(g7ProcessInput{ItemID: item.ID, LeaseMilliseconds: 600})
		command.Env = append(os.Environ(), "G7_HELPER=claim", "G7_DSN="+g6DSN(t, f.g6Fixture), "G7_SCOPE="+string(scope), "G7_INPUT="+string(input))
		pipe, e := command.StdoutPipe()
		mustG7(t, e)
		stdin, e := command.StdinPipe()
		mustG7(t, e)
		command.Stderr = os.Stderr
		out := make(chan g7ProcessOutput, 1)
		mustG7(t, command.Start())
		go func() {
			defer close(out)
			scanner := bufio.NewScanner(pipe)
			for scanner.Scan() {
				if strings.HasPrefix(scanner.Text(), "G7_COMMITTED ") {
					var value g7ProcessOutput
					if json.Unmarshal([]byte(strings.TrimPrefix(scanner.Text(), "G7_COMMITTED ")), &value) == nil {
						out <- value
					}
				}
			}
		}()
		t.Cleanup(func() { _ = stdin.Close(); _ = command.Process.Kill(); _ = command.Wait() })
		workers = append(workers, process{command, out, owner})
	}
	var winner rv.Claim
	var owner rv.Scope
	wins := 0
	for _, p := range workers {
		select {
		case result := <-p.out:
			if result.Code == "APPLIED" {
				wins++
				winner = result.Claim
				owner = p.owner
			}
		case <-time.After(30 * time.Second):
			t.Fatal("process claim timeout")
		}
	}
	if wins != 1 {
		t.Fatal("four processes canonical winners", wins)
	}
	for _, p := range workers {
		mustG7(t, p.cmd.Process.Kill())
		if e := p.cmd.Wait(); e == nil {
			t.Fatal("expected OS kill")
		}
	}
	time.Sleep(650 * time.Millisecond)
	fresh := s
	fresh.Principal = "CONTROLLED_TEST_REVIEWER:fresh"
	next, e := f.reviews.ClaimReview(ctx, fresh, item.ID, time.Minute)
	mustG7(t, e)
	if next.Token == winner.Token {
		t.Fatal("token not renewed")
	}
	_, e = f.reviews.SubmitAnnotation(ctx, owner, rv.Submit{ID: asset.NewID(), ItemID: item.ID, Slot: winner.Slot, Token: winner.Token, SourceDigest: item.Source.Digest, SchemaDigest: item.SchemaDigest, Decision: humanDecision(item, "PASS")})
	if !errors.Is(e, rv.ErrOwnershipLost) {
		t.Fatal("stale token", e)
	}
	submitG7(t, f.reviews, fresh, item, next, "PASS")
	t.Log("WP07 four real OS workers: one claim, OS kill, DB lease expiry, reclaim, stale token rejected")
}
