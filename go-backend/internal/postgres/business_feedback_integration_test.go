//go:build integration

package postgres_test

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"strings"
	"testing"
)

// 由 Python 定向 pytest 启动真实 HTTP Producer，环境仅传受控身份和隔离 DSN。
func TestBusinessFeedbackPythonHTTP(t *testing.T) {
	base := os.Getenv("AC_FEEDBACK_TEST_URL")
	if base == "" {
		t.Fatal("AC_FEEDBACK_TEST_URL required: run Python cross-repo test")
	}
	x := g8(t)
	ctx := context.Background()
	var c rv.FeedbackConnection
	if json.Unmarshal([]byte(os.Getenv("AC_FEEDBACK_TEST_CONNECTION")), &c) != nil {
		t.Fatal("missing connection")
	}
	c.BaseURL = base
	config, _ := json.Marshal(map[string]rv.FeedbackConnection{x.s.ProjectID: c})
	t.Setenv("AC_FEEDBACK_CONNECTIONS", string(config))
	incident, feedback := os.Getenv("AC_FEEDBACK_TEST_INCIDENT"), os.Getenv("AC_FEEDBACK_TEST_ID")
	w, _, e := rv.FetchFeedback(ctx, x.s.ProjectID, incident, feedback)
	mustG7(t, e)
	created := x.call(t, "judge", "POST", x.url("/api-keys"), "feedback-machine", map[string]any{"name": "feedback source", "capabilities": []string{"READ", "WRITE"}}, 200)
	secret := created["secret"].(string)
	post := func(digest string, key bool) int {
		raw, _ := json.Marshal(map[string]string{"incident_id": incident, "feedback_id": feedback, "projection_digest": digest})
		req, _ := http.NewRequest("POST", x.url("/business-feedback"), bytes.NewReader(raw))
		req.Header.Set("Content-Type", "application/json")
		if key {
			req.Header.Set("X-API-Key", secret)
		}
		res, e := http.DefaultClient.Do(req)
		mustG7(t, e)
		defer res.Body.Close()
		return res.StatusCode
	}
	if post(w.Digest, false) != 401 {
		t.Fatal("unauthenticated intake accepted")
	}
	if post(w.Digest, true) != 200 || post(w.Digest, true) != 200 {
		t.Fatal("intake or idempotent retry failed")
	}
	if post(strings.Repeat("0", 64), true) != 409 {
		t.Fatal("wrong expected digest accepted")
	}
	var count int
	mustG7(t, x.db.pool.QueryRow(ctx, `SELECT count(*) FROM evaluation_business_feedback WHERE project_id=$1`, x.s.ProjectID).Scan(&count))
	if count != 1 {
		t.Fatal("duplicate source")
	}
	item := x.enqueue(t, rv.SourceRef{Type: "BUSINESS_FEEDBACK", FeedbackID: feedback}, x.quality, rv.Quality, 2, false)
	if item.Status != "PENDING" || !strings.Contains(item.Source.Evidence[0].Body.String(), "SYNTHETIC") {
		t.Fatal("source was not pending/synthetic")
	}
	if _, e = x.reviews.PublishGoldenLabel(ctx, x.reviewScope, asset.NewID(), item.ID); e == nil {
		t.Fatal("unreviewed source became Golden")
	}
	gold := x.double(t, item, "PASS", "PASS")
	body := testCase()
	body.BodyPolicy = catalog.Redacted
	draft, e := x.reviews.CreateReviewedCaseDraft(ctx, x.reviewScope, rv.Draft{ID: asset.NewID(), ItemID: item.ID, GoldenID: &gold.ID, Case: asset.Ref{EntityID: asset.NewID(), Version: "reviewed.v1"}, Body: body, Sanitization: "REDACTED", Policy: "explicit-human-review.v1", Reason: "controlled human supplied sanitized case", HumanSupplement: true})
	mustG7(t, e)
	published, e := x.reviews.PublishReviewedCase(ctx, x.reviewScope, draft.ID, "sanitized feedback case")
	mustG7(t, e)
	if !strings.Contains(published.Content().Source.Metadata.String(), feedback) {
		t.Fatal("feedback provenance lost")
	}
	// 源端追加撤回，旧导入和已发布版本保持不变；新发布被阻断。
	req, _ := http.NewRequest("POST", base+"/test/withdraw", nil)
	req.Header.Set("Authorization", "Bearer "+os.Getenv(c.TokenEnv))
	res, e := http.DefaultClient.Do(req)
	mustG7(t, e)
	res.Body.Close()
	if res.StatusCode != 200 {
		t.Fatal("source withdrawal failed")
	}
	if post(w.Digest, true) != 409 {
		t.Fatal("superseded source imported")
	}
	if _, e = x.reviews.PublishReviewedCase(ctx, x.reviewScope, draft.ID, "sanitized feedback case"); e == nil {
		t.Fatal("superseded source published")
	}
	receipt, e := x.reviews.BusinessFeedbackReceipt(ctx, x.s.Scope, feedback)
	mustG7(t, e)
	if receipt["requires_re_review"] != true {
		t.Fatal("missing re-review qualification")
	}
	kept, e := (postgres.Cases{Pool: x.pool}).GetCaseVersion(ctx, x.s.Scope, draft.Case)
	mustG7(t, e)
	if kept.ContentDigest() != published.ContentDigest() {
		t.Fatal("historical case mutated")
	}
	t.Log("Python Feedback -> authenticated HTTP projection -> Go machine intake -> human Review -> sanitized Draft/Case -> withdrawal refusal: PASS")
}
