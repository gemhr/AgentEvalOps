//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/identity"

	rv "agentevalops/go-backend/internal/review"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"

	"testing"
	"time"
)

type fixtureBearer struct {
	dev      identity.DevAuth
	fallback apiBearer
}

func (p fixtureBearer) AuthenticateBearer(ctx context.Context, token string) (identity.Principal, error) {
	if v, e := p.dev.AuthenticateBearer(ctx, token); e == nil {
		return v, nil
	}
	return p.fallback.AuthenticateBearer(ctx, token)
}

// 此测试保留真实 PG/HTTP fixture 供现有 Playwright 使用，不进入 API binary。
func TestG8FrontendFixture(t *testing.T) {
	path := os.Getenv("G8_E2E_FIXTURE_PATH")
	if path == "" {
		t.Skip("explicit frontend fixture only")
	}
	x := g8(t)
	ctx := context.Background()
	password, _ := identity.NewKey()
	signing, _ := identity.NewKey()
	control, _ := identity.NewKey()
	dev := identity.DevAuth{Environment: "test", PrincipalID: x.principals["judge"].ID, OrganizationID: x.s.OrganizationID, Password: password, SigningKey: []byte(signing), Lifetime: time.Hour}
	x.api.Dev = &dev
	x.api.Bearer = fixtureBearer{dev: dev, fallback: x.api.Bearer.(apiBearer)}
	x.api.Config.RequestsPerMinute = 600
	x.api.Config.AllowedOrigins = []string{"http://localhost:3000", "http://127.0.0.1:3000"}
	base := x.run(t, "frontend-base", g6Plan{Success: 10})
	candidate := x.run(t, "frontend-candidate", g6Plan{Success: 9, CriticalFail: true})
	missing := x.run(t, "frontend-missing", g6Plan{Success: 10, Missing: true})
	policy := x.policy(t, nil)
	gates := map[string]string{}
	for _, c := range []decision.SourceRef{base, candidate, missing} {
		r, e := x.gate.CreateGate(ctx, x.s, decision.Command{ID: asset.NewID(), Baseline: base, Candidate: c, Policy: policy})
		mustG7(t, e)
		gates[string(r.Decision)] = r.GateID
	}
	state, e := x.k.ReadRunState(ctx, x.s.Scope, base.Runs[0])
	mustG7(t, e)
	item := x.enqueue(t, g7Source(t, state, x.bindings[0].Evaluator, 0), x.refs[0], rv.TaskSuccess, 2, true)
	sa := x.reviewScope
	sa.Principal = x.principals["A"].ID
	sa.IdentitySource = "CONTROLLED_TEST"
	claim, e := x.reviews.ClaimReview(ctx, sa, item.ID, time.Minute)
	mustG7(t, e)
	submitG7(t, x.reviews, sa, item, claim, "SUCCESS")
	disagreement := x.enqueue(t, g7Source(t, state, x.bindings[0].Evaluator, 1), x.refs[0], rv.TaskSuccess, 2, true)
	for _, v := range []struct{ name, value string }{{"A", "SUCCESS"}, {"B", "FAILURE"}} {
		s := x.reviewScope
		s.Principal = x.principals[v.name].ID
		s.IdentitySource = "CONTROLLED_TEST"
		c, e := x.reviews.ClaimReview(ctx, s, disagreement.ID, time.Minute)
		mustG7(t, e)
		submitG7(t, x.reviews, s, disagreement, c, v.value)
	}
	goldenItem := x.enqueue(t, g7Source(t, state, x.bindings[0].Evaluator, 2), x.refs[0], rv.TaskSuccess, 2, true)
	golden := x.double(t, goldenItem, "SUCCESS", "SUCCESS")
	c := rv.CalibrationCommand{ID: asset.NewID(), Dataset: x.dataset, Evaluator: x.bindings[0].Evaluator, Schema: x.refs[0], Protocol: rv.Protocol{Blind: true}, Sampling: rv.Sampling{Version: "review-hash.v1", Seed: "G8_E2E", BasisPoints: 10000}, PositiveClass: "SUCCESS"}
	for i, v := range state.Run.Snapshot.Input.Manifest {
		sample := rv.CalibrationSample{Case: v.Identity.Ref, Result: g7Source(t, state, x.bindings[0].Evaluator, i)}
		if i == 2 {
			sample.GoldenID = golden.ID
		}
		c.Samples = append(c.Samples, sample)
	}
	_, e = x.reviews.PrepareCalibration(ctx, x.reviewScope, c)
	mustG7(t, e)
	_, e = x.reviews.CompleteCalibration(ctx, x.reviewScope, c.ID)
	mustG7(t, e)
	body := testCase()
	body.BodyPolicy = catalog.Redacted
	draft, e := x.reviews.CreateReviewedCaseDraft(ctx, x.reviewScope, rv.Draft{ID: asset.NewID(), ItemID: goldenItem.ID, GoldenID: &golden.ID, Case: asset.Ref{EntityID: asset.NewID(), Version: "e2e-feedback"}, Body: body, Sanitization: "REDACTED", Policy: "controlled.v1", Reason: "受控审核反馈", HumanSupplement: true})
	mustG7(t, e)
	// 控制路由仅存在于本 test fixture，拒绝没有随机控制 secret 的调用。
	closed := make(chan struct{})
	mux := http.NewServeMux()
	mux.Handle("/", x.api.Handler())
	mux.HandleFunc("POST /_test/stop", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Control") != control {
			http.Error(w, "denied", 403)
			return
		}
		w.WriteHeader(204)
		close(closed)
	})
	mux.HandleFunc("POST /_test/revoke-session", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Control") != control {
			http.Error(w, "denied", 403)
			return
		}
		_, e := x.db.pool.Exec(r.Context(), `DELETE FROM product_project_memberships WHERE project_id=$1 AND principal_id=$2`, x.s.ProjectID, x.principals["judge"].ID)
		if e != nil {
			http.Error(w, "fixture failed", 500)
			return
		}
		w.WriteHeader(204)
	})
	s := httptest.NewServer(mux)
	defer s.Close()
	fixture := map[string]any{"api_url": s.URL, "password": password, "control": control, "project": x.s.ProjectID, "organization": x.s.OrganizationID, "dataset": x.dataset, "run": base.Runs[0], "gates": gates, "review": item.ID, "adjudication_review": disagreement.ID, "adjudication_evidence_refs": humanDecision(disagreement, "SUCCESS").EvidenceRefs, "calibration": c.ID, "draft": draft.ID}
	raw, e := json.Marshal(fixture)
	mustG7(t, e)
	mustG7(t, os.WriteFile(path, raw, 0600))
	t.Log("G8_FRONTEND_FIXTURE_READY: isolated real PostgreSQL + Go HTTP; secrets only in explicit local fixture file")
	select {
	case <-closed:
	case <-time.After(20 * time.Minute):
		t.Fatal("frontend fixture timeout")
	}

}
