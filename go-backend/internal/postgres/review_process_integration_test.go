//go:build integration

package postgres_test

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5/pgxpool"
)

type g7ProcessInput struct {
	ItemID, Value, DraftID string
	LeaseMilliseconds      int
	Calibration            rv.CalibrationCommand
}
type g7ProcessOutput struct {
	Code, Digest, ID string
	Claim            rv.Claim
}

func TestG7ProcessHelper(t *testing.T) {
	mode := os.Getenv("G7_HELPER")
	if mode == "" {
		return
	}
	ctx := context.Background()
	pool, e := pgxpool.New(ctx, os.Getenv("G7_DSN"))
	if e != nil {
		t.Fatal("helper pool failed")
	}
	defer pool.Close()
	var s rv.Scope
	var input g7ProcessInput
	if json.Unmarshal([]byte(os.Getenv("G7_SCOPE")), &s) != nil || json.Unmarshal([]byte(os.Getenv("G7_INPUT")), &input) != nil {
		t.Fatal("helper input invalid")
	}
	k := postgres.Reviews{Pool: pool}
	output := g7ProcessOutput{Code: "APPLIED"}
	switch mode {
	case "claim", "submit":
		c, e := k.ClaimReview(ctx, s, input.ItemID, time.Duration(input.LeaseMilliseconds)*time.Millisecond)
		if errors.Is(e, asset.ErrConflict) {
			output.Code = "NOT_CLAIMED"
		} else if e != nil {
			t.Fatal("helper claim failed", e)
		} else {
			output.Claim = c
		}
		if mode == "submit" && e == nil {
			view, e := k.GetReview(ctx, s, input.ItemID)
			mustG7(t, e)
			if len(view.Annotations) != 0 || view.Item.Source.Automatic != nil {
				t.Fatal("independent review leakage")
			}
			a := submitG7(t, k, s, view.Item, c, input.Value)
			output.ID = a.ID
		}
	case "snapshot":
		snapshot, e := k.PrepareCalibration(ctx, s, input.Calibration)
		mustG7(t, e)
		j, _ := asset.Freeze(snapshot)
		output.Digest = j.Digest()
		output.ID = snapshot.ID
	case "report":
		r, e := k.CompleteCalibration(ctx, s, input.Calibration.ID)
		mustG7(t, e)
		j, _ := asset.Freeze(r)
		output.Digest = j.Digest()
		output.ID = r.ID
	case "publish":
		c, e := k.PublishReviewedCase(ctx, s, input.DraftID, "G7 process reviewed case")
		mustG7(t, e)
		output.Digest = c.ContentDigest()
		output.ID = c.Ref().EntityID
	default:
		t.Fatal("unknown helper mode")
	}
	raw, _ := json.Marshal(output)
	fmt.Println("G7_COMMITTED " + string(raw))
	// 唯一同步点是已 commit marker；父进程随后实际 OS kill。
	_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
}
func TestG7ReviewProcesses(t *testing.T) {
	f := g7(t)
	ctx := context.Background()
	run := f.run(t, "controlled", g6Plan{Success: 10})
	state, e := f.k.ReadRunState(ctx, f.s.Scope, run.Runs[0])
	mustG7(t, e)
	spawn := func(mode string, s rv.Scope, input g7ProcessInput) (*exec.Cmd, <-chan g7ProcessOutput) {
		t.Helper()
		exe, e := os.Executable()
		mustG7(t, e)
		command := exec.Command(exe, "-test.run=^TestG7ProcessHelper$", "-test.v")
		scope, _ := json.Marshal(s)
		raw, _ := json.Marshal(input)
		command.Env = append(os.Environ(), "G7_HELPER="+mode, "G7_DSN="+g6DSN(t, f.g6Fixture), "G7_SCOPE="+string(scope), "G7_INPUT="+string(raw))
		pipe, e := command.StdoutPipe()
		mustG7(t, e)
		stdin, e := command.StdinPipe()
		mustG7(t, e)
		command.Stderr = os.Stderr
		mustG7(t, command.Start())
		outputs := make(chan g7ProcessOutput, 1)
		scanDone := make(chan struct{})
		go func() {
			defer close(outputs)
			defer close(scanDone)
			scan := bufio.NewScanner(pipe)
			for scan.Scan() {
				if strings.HasPrefix(scan.Text(), "G7_COMMITTED ") {
					var out g7ProcessOutput
					if json.Unmarshal([]byte(strings.TrimPrefix(scan.Text(), "G7_COMMITTED ")), &out) == nil {
						outputs <- out
					}
				} else {
					t.Logf("G7 child %s: %s", mode, scan.Text())
				}
			}
		}()
		t.Cleanup(func() { _ = stdin.Close(); _ = command.Process.Kill(); _ = command.Wait(); <-scanDone })
		return command, outputs
	}
	marker := func(output <-chan g7ProcessOutput) g7ProcessOutput {
		t.Helper()
		select {
		case v, ok := <-output:
			if !ok {
				t.Fatal("child exited before commit")
			}
			return v
		case <-time.After(30 * time.Second):
			t.Fatal("child commit timeout")
		}
		return g7ProcessOutput{}
	}
	kill := func(c *exec.Cmd) {
		t.Helper()
		mustG7(t, c.Process.Kill())
		if e := c.Wait(); e == nil {
			t.Fatal("expected OS kill")
		}
	}
	item := f.enqueue(t, g7Source(t, state, f.bindings[0].Evaluator, 0), f.refs[0], rv.TaskSuccess, 1, true)
	sa, sb := f.reviewer("process-A"), f.reviewer("process-B")
	input := g7ProcessInput{ItemID: item.ID, LeaseMilliseconds: 500}
	p1, o1 := spawn("claim", sa, input)
	p2, o2 := spawn("claim", sb, input)
	a, b := marker(o1), marker(o2)
	winner := a
	owner := sa
	if a.Code == "NOT_CLAIMED" {
		winner = b
		owner = sb
	}
	if (a.Code == "APPLIED") == (b.Code == "APPLIED") {
		t.Fatal("H01 two processes must have one winner")
	}
	kill(p1)
	kill(p2)
	t.Log("H01 PASS real reviewer processes compete one slot")
	// 真正等待 DB lease 失效，绝不以进程被 kill 直接推断已经过期。
	fresh := f.reviewer("process-fresh")
	deadline := time.Now().Add(5 * time.Second)
	var next rv.Claim
	for {
		next, e = f.reviews.ClaimReview(ctx, fresh, item.ID, time.Minute)
		if e == nil {
			break
		}
		if !errors.Is(e, asset.ErrConflict) || time.Now().After(deadline) {
			t.Fatal("H02 reclaim", e)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if next.Token == winner.Claim.Token {
		t.Fatal("H02 fresh token missing")
	}
	_, e = f.reviews.SubmitAnnotation(ctx, owner, rv.Submit{ID: asset.NewID(), ItemID: item.ID, Slot: winner.Claim.Slot, Token: winner.Claim.Token, SourceDigest: item.Source.Digest, SchemaDigest: item.SchemaDigest, Decision: humanDecision(item, "SUCCESS")})
	if !errors.Is(e, rv.ErrOwnershipLost) {
		t.Fatal("H03 stale reviewer", e)
	}
	submitG7(t, f.reviews, fresh, item, next, "SUCCESS")
	t.Log("H02/H03 PASS process kill, DB lease expiry, reclaim and old submit rejected")
	independent := f.enqueue(t, g7Source(t, state, f.bindings[0].Evaluator, 1), f.refs[0], rv.TaskSuccess, 2, true)
	p1, o1 = spawn("submit", sa, g7ProcessInput{ItemID: independent.ID, Value: "SUCCESS", LeaseMilliseconds: 60000})
	p2, o2 = spawn("submit", sb, g7ProcessInput{ItemID: independent.ID, Value: "FAILURE", LeaseMilliseconds: 60000})
	a, b = marker(o1), marker(o2)
	if a.Code != "APPLIED" || b.Code != "APPLIED" || a.Claim.Slot == b.Claim.Slot {
		t.Fatal("H04 distinct slots")
	}
	kill(p1)
	kill(p2)
	view, e := f.reviews.GetReview(ctx, f.reviewScope, independent.ID)
	mustG7(t, e)
	if view.Item.Status != "ADJUDICATION_REQUIRED" || len(view.Annotations) != 2 {
		t.Fatal("H04 dispute missing")
	}
	_, e = f.reviews.AdjudicateReview(ctx, f.reviewer("process-adjudicator"), asset.NewID(), independent.ID, humanDecision(independent, "FAILURE"), "受控第三人", nil)
	mustG7(t, e)
	gold, e := f.reviews.PublishGoldenLabel(ctx, f.reviewScope, asset.NewID(), independent.ID)
	mustG7(t, e)
	t.Log("H04 PASS independent process submission, disagreement, adjudication")
	calibration := rv.CalibrationCommand{ID: asset.NewID(), Dataset: f.dataset, Evaluator: f.bindings[0].Evaluator, Schema: f.refs[0], Protocol: rv.Protocol{Blind: true}, Sampling: rv.Sampling{Version: "review-hash.v1", Seed: "G7_PROCESS", BasisPoints: 10000}, PositiveClass: "SUCCESS"}
	for i, c := range state.Run.Snapshot.Input.Manifest {
		sample := rv.CalibrationSample{Case: c.Identity.Ref, Result: g7Source(t, state, f.bindings[0].Evaluator, i)}
		if gold.Case != nil && *gold.Case == c.Identity.Ref {
			sample.GoldenID = gold.ID
		}
		calibration.Samples = append(calibration.Samples, sample)
	}
	p1, o1 = spawn("snapshot", f.reviewScope, g7ProcessInput{Calibration: calibration})
	a = marker(o1)
	kill(p1)
	snapshot, e := f.reviews.PrepareCalibration(ctx, f.reviewScope, calibration)
	mustG7(t, e)
	snapshotBytes, _ := asset.Freeze(snapshot)
	if snapshotBytes.Digest() != a.Digest {
		t.Fatal("H05 snapshot replaced")
	}
	expected, e := rv.Compute(snapshot)
	mustG7(t, e)
	expectedBytes, _ := asset.Freeze(expected)
	p2, o2 = spawn("report", f.reviewScope, g7ProcessInput{Calibration: calibration})
	b = marker(o2)
	kill(p2)
	report, e := f.reviews.GetCalibrationReport(ctx, f.reviewScope, calibration.ID)
	mustG7(t, e)
	reportBytes, _ := asset.Freeze(report)
	if reportBytes.Digest() != expectedBytes.Digest() || b.Digest != expectedBytes.Digest() {
		t.Fatal("H05 restart recomputed different report")
	}
	t.Log("H05 PASS snapshot commit kill/restart, identical immutable report")
	body := testCase()
	body.BodyPolicy = catalog.Redacted
	feedback := f.reviewer("process-feedback")
	d, e := f.reviews.CreateReviewedCaseDraft(ctx, feedback, rv.Draft{ID: asset.NewID(), ItemID: item.ID, Case: asset.Ref{EntityID: asset.NewID(), Version: "v1"}, Body: body, Sanitization: "REDACTED", Policy: "explicit-human-sanitized.v1", Reason: "人工补充脱敏正文", HumanSupplement: true})
	mustG7(t, e)
	p1, o1 = spawn("publish", feedback, g7ProcessInput{DraftID: d.ID})
	a = marker(o1)
	kill(p1)
	caseVersion, e := (postgres.Cases{Pool: f.k.Pool}).GetCaseVersion(ctx, f.s.Scope, d.Case)
	mustG7(t, e)
	if caseVersion.ContentDigest() != a.Digest || caseVersion.Content().Source.Ref != d.ID || !strings.Contains(caseVersion.Content().Source.Metadata.String(), item.ID) {
		t.Fatal("H06 lineage missing")
	}
	p2, o2 = spawn("publish", feedback, g7ProcessInput{DraftID: d.ID})
	b = marker(o2)
	kill(p2)
	if a.Digest != b.Digest {
		t.Fatal("H06 publish retry changed Case")
	}
	t.Log("H06 PASS Case commit kill/restart, unchanged lineage")
}
