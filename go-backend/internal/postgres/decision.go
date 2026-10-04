package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Decisions struct{ Pool *pgxpool.Pool }

var policyTables = tables{"evaluation_policies", "evaluation_policy_versions"}

func (k Decisions) CreatePolicy(ctx context.Context, s asset.Scope, c asset.Create) (asset.Logical, error) {
	return createLogical(ctx, k.Pool, policyTables, s, c)
}
func (k Decisions) PublishPolicyVersion(ctx context.Context, s asset.Scope, c asset.Publish[decision.Policy]) (decision.PolicyVersion, error) {
	return publish(ctx, k.Pool, policyTables, s, c, nil, func(ctx context.Context, tx pgx.Tx, p decision.Policy) error {
		if p.TaskSuccess != nil {
			v, e := loadVersion[metric.Definition](ctx, tx, metricTables, s, p.TaskSuccess.Metric)
			if e != nil {
				return e
			}
			d := v.Content().Body
			if d.ValueType != metric.Enum || strings.Join(d.Labels, ",") != "SUCCESS,FAILURE,INCONCLUSIVE,NOT_APPLICABLE" {
				return asset.ErrInvalid
			}
		}
		for _, r := range p.Metrics {
			if _, e := loadVersion[metric.Definition](ctx, tx, metricTables, s, r.Metric); e != nil {
				return e
			}
		}
		return nil
	})
}
func (k Decisions) GetPolicyVersion(ctx context.Context, s asset.Scope, ref asset.Ref) (decision.PolicyVersion, error) {
	return loadVersion[decision.Policy](ctx, k.Pool, policyTables, s, ref)
}
func comparison(ctx context.Context, q queryer, s asset.Scope, id string) (decision.Snapshot, string, error) {
	var v decision.Snapshot
	var raw []byte
	var intent string
	e := q.QueryRow(ctx, `SELECT snapshot_bytes,intent_digest FROM evaluation_comparisons c JOIN projects p ON p.id=c.project_id WHERE c.project_id=$1 AND p.org_id=$2 AND c.id=$3`, s.ProjectID, s.OrganizationID, id).Scan(&raw, &intent)
	if e != nil {
		return v, "", dbError(e)
	}
	e = json.Unmarshal(raw, &v)
	return v, intent, e
}
func commandDigest(s asset.Scope, c decision.Command) string {
	return decisionHash(struct {
		Command decision.Command
		Actor   string
	}{c, s.Principal})
}
func (k Decisions) PrepareComparison(ctx context.Context, s ev.Scope, c decision.Command) (decision.Snapshot, error) {
	if s.Validate(false) != nil || !s.Create || c.Validate() != nil {
		return decision.Snapshot{}, asset.ErrInvalid
	}
	intent := commandDigest(s.Scope, c)
	old, oldIntent, e := comparison(ctx, k.Pool, s.Scope, c.ID)
	if e == nil {
		if oldIntent != intent {
			return old, asset.ErrConflict
		}
		return old, nil
	}
	if !errors.Is(e, asset.ErrNotFound) {
		return old, e
	}
	// 仅在一致读事务中冻结 facts；不持有 Run 写锁，不在事务中做回归计算。
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if e != nil {
		return old, e
	}
	defer rollback(ctx, tx)
	policy, e := loadVersion[decision.Policy](ctx, tx, policyTables, s.Scope, c.Policy)
	if e != nil {
		return old, e
	}
	b, e := resolveDecisionSource(ctx, tx, s.Scope, c.Baseline)
	if e != nil {
		return old, e
	}
	candidate, e := resolveDecisionSource(ctx, tx, s.Scope, c.Candidate)
	if e != nil {
		return old, e
	}
	snapshot := decision.Snapshot{ProjectID: s.ProjectID, Actor: s.Principal, Source: "CANONICAL_POSTGRES", Command: c, Policy: policy.Content().Body, PolicyDigest: policy.ContentDigest(), Baseline: b, Candidate: candidate}
	if e = tx.QueryRow(ctx, "SELECT clock_timestamp()").Scan(&snapshot.CapturedAt); e != nil {
		return old, e
	}
	if e = tx.Commit(ctx); e != nil {
		return old, e
	}
	frozen, e := asset.Freeze(snapshot)
	if e != nil {
		return old, e
	}
	if len(frozen.Bytes()) > 64*1024*1024 {
		return old, asset.ErrUnsupported
	}
	var saved decision.Snapshot
	result, e := (Evaluation{Pool: k.Pool}).transact(ctx, s, "", true, func(tx pgx.Tx, _ ev.Run, _ string) (ev.Reply, error) {
		if _, e := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", s.ProjectID+":"+c.ID); e != nil {
			return ev.Reply{}, e
		}
		v, existing, e := comparison(ctx, tx, s.Scope, c.ID)
		if e == nil {
			if existing != intent {
				return ev.Reply{}, asset.ErrConflict
			}
			saved = v
			return ev.Reply{Code: ev.AlreadyApplied}, nil
		}
		if !errors.Is(e, asset.ErrNotFound) {
			return ev.Reply{}, e
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_comparisons(id,project_id,policy_id,policy_version,intent_digest,snapshot_digest,snapshot_bytes,created_by) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, c.ID, s.ProjectID, c.Policy.EntityID, c.Policy.Version, intent, frozen.Digest(), frozen.Bytes(), s.Principal)
		saved = snapshot
		return ev.Reply{Code: ev.Applied}, e
	})
	if e == nil && result.Code != ev.Applied && result.Code != ev.AlreadyApplied {
		e = asset.ErrForbidden
	}
	if e == nil {
		slog.Info("comparison_created", "event", "comparison_created", "project_id", s.ProjectID, "gate_id", c.ID)
	}
	return saved, e
}
func (k Decisions) CreateGate(ctx context.Context, s ev.Scope, c decision.Command) (decision.Receipt, error) {
	if _, e := k.PrepareComparison(ctx, s, c); e != nil {
		return decision.Receipt{}, e
	}
	return k.CompleteGate(ctx, s, c.ID)
}

// CompleteGate 始终计算数据库中已提交的快照；caller 不能传 score/coverage/decision。
func (k Decisions) CompleteGate(ctx context.Context, s ev.Scope, id string) (decision.Receipt, error) {
	if s.Validate(false) != nil || !s.Create || !asset.ValidID(id) {
		return decision.Receipt{}, asset.ErrInvalid
	}
	r, e := k.GetGateReceipt(ctx, s.Scope, id)
	if e == nil {
		return r, nil
	}
	if !errors.Is(e, asset.ErrNotFound) {
		return r, e
	}
	snapshot, _, e := comparison(ctx, k.Pool, s.Scope, id)
	if e != nil {
		return r, e
	}
	if snapshot.Actor != s.Principal {
		return r, asset.ErrForbidden
	}
	r, e = decision.Compare(snapshot)
	if e != nil {
		return r, e
	}
	frozen, e := asset.Freeze(r)
	if e != nil {
		return r, e
	}
	result, e := (Evaluation{Pool: k.Pool}).transact(ctx, s, "", true, func(tx pgx.Tx, _ ev.Run, _ string) (ev.Reply, error) {
		tag, e := tx.Exec(ctx, `INSERT INTO evaluation_gate_receipts(project_id,gate_id,decision,receipt_bytes,receipt_digest) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING`, s.ProjectID, id, r.Decision, frozen.Bytes(), frozen.Digest())
		if e != nil {
			return ev.Reply{}, e
		}
		if tag.RowsAffected() == 0 {
			return ev.Reply{Code: ev.AlreadyApplied}, nil
		}
		for _, row := range r.Cases {
			raw, e := asset.Freeze(row)
			if e != nil {
				return ev.Reply{}, e
			}
			if _, e = tx.Exec(ctx, `INSERT INTO evaluation_case_comparisons(project_id,gate_id,unit_key,comparison_bytes) VALUES($1,$2,$3,$4)`, s.ProjectID, id, row.Key, raw.Bytes()); e != nil {
				return ev.Reply{}, e
			}
		}
		return ev.Reply{Code: ev.Applied}, nil
	})
	if e == nil && result.Code != ev.Applied && result.Code != ev.AlreadyApplied {
		e = asset.ErrForbidden
	}
	if e != nil {
		return r, e
	}
	r, e = k.GetGateReceipt(ctx, s.Scope, id)
	if e == nil {
		events := []string{"gate_" + strings.ToLower(string(r.Decision))}
		if r.Compatibility == decision.Incomparable {
			events = append(events, "comparison_incomparable")
		}
		if r.Summary.Regressions > 0 {
			events = append(events, "regression_detected")
		}
		if len(r.CriticalRegressions) > 0 {
			events = append(events, "critical_regression")
		}
		if len(r.Exceptions) > 0 {
			events = append(events, "gate_exception_applied")
		}
		for _, event := range events {
			slog.Info(event, "event", event, "project_id", s.ProjectID, "gate_id", id)
		}
	}
	return r, e
}
func (k Decisions) GetComparison(ctx context.Context, s asset.Scope, id string) (decision.Snapshot, error) {
	if s.Validate(false) != nil || !asset.ValidID(id) {
		return decision.Snapshot{}, asset.ErrInvalid
	}
	v, _, e := comparison(ctx, k.Pool, s, id)
	return v, e
}
func (k Decisions) GetGateReceipt(ctx context.Context, s asset.Scope, id string) (decision.Receipt, error) {
	var v decision.Receipt
	if s.Validate(false) != nil || !asset.ValidID(id) {
		return v, asset.ErrInvalid
	}
	var raw []byte
	e := k.Pool.QueryRow(ctx, `SELECT receipt_bytes FROM evaluation_gate_receipts r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND gate_id=$3`, s.ProjectID, s.OrganizationID, id).Scan(&raw)
	if e != nil {
		return v, dbError(e)
	}
	e = json.Unmarshal(raw, &v)
	return v, e
}
func (k Decisions) ListCaseComparisons(ctx context.Context, s asset.Scope, id string, offset, limit int) ([]decision.CaseComparison, error) {
	if offset < 0 || limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	r, e := k.GetGateReceipt(ctx, s, id)
	if e != nil {
		return nil, e
	}
	if offset >= len(r.Cases) {
		return []decision.CaseComparison{}, nil
	}
	return r.Cases[offset:min(offset+limit, len(r.Cases))], nil
}
func (k Decisions) GetMetricComparison(ctx context.Context, s asset.Scope, id string, ref asset.Ref) (decision.MetricComparison, error) {
	r, e := k.GetGateReceipt(ctx, s, id)
	if e != nil {
		return decision.MetricComparison{}, e
	}
	for _, m := range r.Metrics {
		if m.Metric == ref {
			return m, nil
		}
	}
	return decision.MetricComparison{}, asset.ErrNotFound
}
func (k Decisions) GetRegressionSummary(ctx context.Context, s asset.Scope, id string) (decision.Summary, error) {
	r, e := k.GetGateReceipt(ctx, s, id)
	return r.Summary, e
}
