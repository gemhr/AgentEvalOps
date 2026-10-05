//go:build integration

package postgres

import (
	"agentevalops/go-backend/internal/analytics"
	"agentevalops/go-backend/internal/asset"
	"context"
)

// ExplainAnalytics 仅进入受控 integration binary，审计实际生产聚合 SQL。
func (k ProductReader) ExplainAnalytics(ctx context.Context, s asset.Scope, q analytics.Query, kind string) ([]byte, error) {
	var raw []byte
	sql, ok := analyticsSQL[kind]
	if !ok {
		return nil, asset.ErrInvalid
	}
	e := k.Pool.QueryRow(ctx, "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "+sql, s.ProjectID, q.From, q.Until, q.Bucket, q.Subject, q.Environment, q.RunMode, q.Metric, q.Evaluator, q.Rule).Scan(&raw)
	return raw, e
}
