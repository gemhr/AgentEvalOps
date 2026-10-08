package main

import (
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/postgres"
	"context"
	"time"
)

// 受控 harness 的固定 pair 顺序；不改变 generic Kernel scanner。
type pairedBackend struct {
	postgres.Evaluation
	order  []string
	claims []map[string]any
}

func (b *pairedBackend) Candidates(ctx context.Context, kind postgres.CandidateKind, cursor postgres.EvaluationCursor, limit int) ([]postgres.EvaluationCandidate, error) {
	if kind != postgres.PendingExecution {
		return b.Evaluation.Candidates(ctx, kind, cursor, limit)
	}
	var c postgres.EvaluationCandidate
	err := b.Pool.QueryRow(ctx, `SELECT project_id::text,run_id::text,id::text FROM evaluation_attempts WHERE id=ANY($1::uuid[]) AND status='PENDING' ORDER BY array_position($1::uuid[],id) LIMIT 1`, b.order).Scan(&c.ProjectID, &c.RunID, &c.AttemptID)
	if err != nil {
		if err.Error() == "no rows in result set" {
			return nil, nil
		}
		return nil, err
	}
	return []postgres.EvaluationCandidate{c}, nil
}
func (b *pairedBackend) ClaimExecutionAttempt(ctx context.Context, s ev.Scope, o ev.Owned, owner string, d time.Duration) (ev.Reply, error) {
	start := time.Now()
	reply, err := b.Evaluation.ClaimExecutionAttempt(ctx, s, o, owner, d)
	b.claims = append(b.claims, map[string]any{"attempt_id": o.AttemptID, "claim_latency_ms": float64(time.Since(start).Microseconds()) / 1000, "code": reply.Code})
	return reply, err
}
