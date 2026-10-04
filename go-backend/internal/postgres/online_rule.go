package postgres

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	ob "agentevalops/go-backend/internal/observation"
	"github.com/jackc/pgx/v5"
)

var onlineRules = tables{logical: "evaluation_online_rules", version: "evaluation_online_rule_versions"}

type ProjectOnlinePolicy struct {
	Budget ob.Budget
	Rate   ob.Rate
}

func (k Online) ConfigureProjectLimits(ctx context.Context, s ob.Scope, p ProjectOnlinePolicy) (ev.Reply, error) {
	if !s.CanPublish || p.Budget.MaxSampled < 0 || p.Budget.MaxWorks < 0 || p.Budget.MaxProviderCalls < 0 || p.Budget.MaxEstimatedTokens < 0 || p.Budget.MaxReportedTokens < 0 || (p.Rate.Mode != "BOUNDED_CONCURRENCY" && p.Rate.Mode != "DURABLE_INTERVAL") || (p.Rate.Mode == "DURABLE_INTERVAL" && (p.Rate.MaxCalls < 1 || p.Rate.IntervalSeconds < 1)) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	raw := encode(p)
	return k.transaction(ctx, s, true, func(tx pgx.Tx) (ev.Reply, error) {
		_, e := tx.Exec(ctx, `INSERT INTO evaluation_online_project_limits(project_id,policy_bytes) VALUES($1,$2) ON CONFLICT DO NOTHING`, s.ProjectID, raw)
		if e != nil {
			return ev.Reply{}, e
		}
		var old []byte
		e = tx.QueryRow(ctx, `SELECT policy_bytes FROM evaluation_online_project_limits WHERE project_id=$1`, s.ProjectID).Scan(&old)
		if e != nil {
			return ev.Reply{}, e
		}
		if !bytes.Equal(raw, old) {
			return ev.Reply{}, stop(ev.Conflict, "PROJECT_POLICY_IMMUTABLE")
		}
		return ev.Reply{Code: ev.Applied}, nil
	})
}
func (k Online) CreateOnlineRule(ctx context.Context, s asset.Scope, c asset.Create) (asset.Logical, error) {
	return createLogical(ctx, k.Pool, onlineRules, s, c)
}
func (k Online) GetOnlineRule(ctx context.Context, s asset.Scope, id string) (asset.Logical, error) {
	return getLogical(ctx, k.Pool, onlineRules, s, id)
}
func (k Online) ListOnlineRules(ctx context.Context, s asset.Scope, limit int) ([]asset.Logical, error) {
	return listLogical(ctx, k.Pool, onlineRules, s, limit)
}
func onlineIdentity[T any](v asset.Version[T]) ev.AssetIdentity {
	j, _ := asset.ParseJSON(v.Bytes())
	return ev.AssetIdentity{Ref: v.Ref(), ProjectID: v.ProjectID(), Algorithm: v.Algorithm(), ContentDigest: v.ContentDigest(), SemanticDigest: v.SemanticDigest(), CanonicalContent: j}
}
func (k Online) PublishOnlineRuleVersion(ctx context.Context, s ob.Scope, ref asset.Ref, r ob.Rule) (ev.Reply, error) {
	if !s.CanPublish || ref.Validate() != nil || r.Validate() != nil {
		return ev.Reply{Code: ev.Rejected, Reason: "INVALID_RULE"}, nil
	}
	return k.transaction(ctx, s, true, func(tx pgx.Tx) (ev.Reply, error) {
		var id string
		if e := tx.QueryRow(ctx, `SELECT id::text FROM evaluation_online_rules WHERE project_id=$1 AND id=$2 FOR UPDATE`, s.ProjectID, ref.EntityID).Scan(&id); e != nil {
			return ev.Reply{}, dbError(e)
		}
		return k.publishRule(ctx, tx, s, ref, r)
	})
}
func (k Online) publishRule(ctx context.Context, tx pgx.Tx, s ob.Scope, ref asset.Ref, r ob.Rule) (ev.Reply, error) {
	for i, b := range r.Bindings {
		evaluator, e := loadVersion[metric.EvaluatorDefinition](ctx, tx, evaluatorTables, s.Scope, b.Evaluator.Identity.Ref)
		if e != nil {
			return ev.Reply{}, e
		}
		m, e := loadVersion[metric.Definition](ctx, tx, metricTables, s.Scope, b.Metric.Identity.Ref)
		if e != nil {
			return ev.Reply{}, e
		}
		def := evaluator.Content().Body
		if len(def.OutputMetrics) != 1 || def.OutputMetrics[0] != m.Ref() {
			return ev.Reply{}, stop(ev.Rejected, "METRIC_BINDING_MISMATCH")
		}
		r.Bindings[i] = ob.Binding{Evaluator: ev.EvaluatorSpec{Identity: onlineIdentity(evaluator), Definition: def, Metrics: []asset.Ref{m.Ref()}, Required: b.Evaluator.Required, Applicability: b.Evaluator.Applicability}, Metric: ev.MetricInput{Identity: onlineIdentity(m), Definition: m.Content().Body}}
	}
	if r.Validate() != nil {
		return ev.Reply{}, stop(ev.Rejected, "INVALID_RULE")
	}
	raw, e := asset.Freeze(r)
	if e != nil {
		return ev.Reply{}, e
	}
	var existing []byte
	var actor string
	e = tx.QueryRow(ctx, `SELECT canonical_bytes,published_by FROM evaluation_online_rule_versions WHERE project_id=$1 AND entity_id=$2 AND version=$3`, s.ProjectID, ref.EntityID, ref.Version).Scan(&existing, &actor)
	if e == nil {
		if bytes.Equal(existing, raw.Bytes()) && actor == s.Principal {
			return ev.Reply{Code: ev.AlreadyApplied, ID: ref.EntityID}, nil
		}
		return ev.Reply{}, stop(ev.Conflict, "RULE_VERSION_IMMUTABLE")
	}
	if !errors.Is(e, pgx.ErrNoRows) {
		return ev.Reply{}, e
	}
	_, e = tx.Exec(ctx, `INSERT INTO evaluation_online_rule_versions(project_id,entity_id,version,canonical_bytes,digest,published_by,enabled) VALUES($1,$2,$3,$4,$5,$6,$7)`, s.ProjectID, ref.EntityID, ref.Version, raw.Bytes(), raw.Digest(), s.Principal, r.Enabled)
	if e != nil {
		return ev.Reply{}, e
	}
	for _, b := range r.Bindings {
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_online_bindings(project_id,rule_id,rule_version,evaluator_id,evaluator_version,metric_id,metric_version) VALUES($1,$2,$3,$4,$5,$6,$7)`, s.ProjectID, ref.EntityID, ref.Version, b.Evaluator.Identity.Ref.EntityID, b.Evaluator.Identity.Ref.Version, b.Metric.Identity.Ref.EntityID, b.Metric.Identity.Ref.Version)
		if e != nil {
			return ev.Reply{}, e
		}
	}
	_, e = tx.Exec(ctx, `INSERT INTO evaluation_online_rule_usage(project_id,rule_id,rule_version) VALUES($1,$2,$3)`, s.ProjectID, ref.EntityID, ref.Version)
	return ev.Reply{Code: ev.Applied, ID: ref.EntityID}, e
}
func readRule(ctx context.Context, q queryer, s asset.Scope, ref asset.Ref) (ob.RuleVersion, error) {
	var v ob.RuleVersion
	v.ProjectID = s.ProjectID
	v.Ref = ref
	e := q.QueryRow(ctx, `SELECT canonical_bytes,digest,published_by,published_at FROM evaluation_online_rule_versions v JOIN projects p ON p.id=v.project_id WHERE v.project_id=$1 AND p.org_id=$2 AND v.entity_id=$3 AND v.version=$4`, s.ProjectID, s.OrganizationID, ref.EntityID, ref.Version).Scan(&v.Canonical, &v.Digest, &v.PublishedBy, &v.PublishedAt)
	if e != nil {
		return v, dbError(e)
	}
	j, e := asset.ParseJSON(v.Canonical)
	if e != nil || j.Digest() != v.Digest {
		return v, asset.ErrInvalid
	}
	e = j.Decode(&v.Body)
	return v, e
}
func (k Online) GetOnlineRuleVersion(ctx context.Context, s asset.Scope, ref asset.Ref) (ob.RuleVersion, error) {
	if s.Validate(false) != nil || ref.Validate() != nil {
		return ob.RuleVersion{}, asset.ErrInvalid
	}
	return readRule(ctx, k.Pool, s, ref)
}

type usage struct {
	Sampled, Works, Calls int
	Estimated, Reported   int64
}

func limits(ctx context.Context, tx pgx.Tx, s ob.Scope, ref asset.Ref) (ProjectOnlinePolicy, usage, usage, error) {
	var p ProjectOnlinePolicy
	var raw []byte
	var project, rule usage
	e := tx.QueryRow(ctx, `SELECT policy_bytes,sampled,works,calls,estimated_tokens,reported_tokens FROM evaluation_online_project_limits WHERE project_id=$1 FOR UPDATE`, s.ProjectID).Scan(&raw, &project.Sampled, &project.Works, &project.Calls, &project.Estimated, &project.Reported)
	if errors.Is(e, pgx.ErrNoRows) {
		return p, project, rule, stop(ev.Rejected, "PROJECT_ONLINE_POLICY_REQUIRED")
	}
	if e != nil {
		return p, project, rule, e
	}
	if e = json.Unmarshal(raw, &p); e != nil {
		return p, project, rule, e
	}
	e = tx.QueryRow(ctx, `SELECT sampled,works,calls,estimated_tokens,reported_tokens FROM evaluation_online_rule_usage WHERE project_id=$1 AND rule_id=$2 AND rule_version=$3 FOR UPDATE`, s.ProjectID, ref.EntityID, ref.Version).Scan(&rule.Sampled, &rule.Works, &rule.Calls, &rule.Estimated, &rule.Reported)
	return p, project, rule, e
}
func (k Online) Materialize(ctx context.Context, s ob.Scope, ref asset.Ref, id string, backfill bool) (ev.Reply, error) {
	if !s.Reconcile || ref.Validate() != nil || !asset.ValidID(id) {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	return k.transaction(ctx, s, true, func(tx pgx.Tx) (ev.Reply, error) { return k.materialize(ctx, tx, s, ref, id, backfill) })
}
func (k Online) materialize(ctx context.Context, tx pgx.Tx, s ob.Scope, ref asset.Ref, id string, backfill bool) (ev.Reply, error) {
	v, e := readRule(ctx, tx, s.Scope, ref)
	if e != nil {
		return ev.Reply{}, e
	}
	o, e := readObservation(ctx, tx, s.Scope, id)
	if e != nil {
		return ev.Reply{}, e
	}
	// project/rule 计数器锁同时串行化相同 admission 的检查和创建。
	policy, pu, ru, e := limits(ctx, tx, s, ref)
	if e != nil {
		return ev.Reply{}, e
	}
	var exists bool
	e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM evaluation_online_admissions WHERE project_id=$1 AND rule_id=$2 AND rule_version=$3 AND observation_id=$4)`, s.ProjectID, ref.EntityID, ref.Version, id).Scan(&exists)
	if e != nil {
		return ev.Reply{}, e
	}
	if exists {
		return ev.Reply{Code: ev.AlreadyApplied, ID: id}, nil
	}
	if backfill && !v.Body.AllowBackfill {
		return ev.Reply{}, stop(ev.Rejected, "BACKFILL_DISABLED")
	}
	eligible := ob.Match(v.Body, o) && (backfill || !o.Created.Before(v.PublishedAt))
	selection := ob.Sample(s.ProjectID, ref, o.TraceID, v.Body.Sampling, eligible)
	outcome := "INELIGIBLE"
	if eligible {
		outcome = "NOT_SAMPLED"
	}
	if selection.Sampled {
		outcome = "MATERIALIZED"
		for _, entry := range []struct {
			b ob.Budget
			u usage
		}{{policy.Budget, pu}, {v.Body.Budget, ru}} {
			if entry.u.Sampled >= entry.b.MaxSampled || entry.u.Works+len(v.Body.Bindings) > entry.b.MaxWorks {
				outcome = "SKIPPED_BUDGET"
			}
		}
	}
	_, e = tx.Exec(ctx, `INSERT INTO evaluation_online_admissions(project_id,rule_id,rule_version,observation_id,selection_bytes,outcome) VALUES($1,$2,$3,$4,$5,$6)`, s.ProjectID, ref.EntityID, ref.Version, id, encode(selection), outcome)
	if e != nil {
		return ev.Reply{}, e
	}
	if outcome == "MATERIALIZED" {
		for _, b := range v.Body.Bindings {
			_, e = tx.Exec(ctx, `INSERT INTO evaluation_online_works(id,project_id,observation_id,rule_id,rule_version,evaluator_id,evaluator_version,binding_bytes) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, asset.NewID(), s.ProjectID, id, ref.EntityID, ref.Version, b.Evaluator.Identity.Ref.EntityID, b.Evaluator.Identity.Ref.Version, encode(b))
			if e != nil {
				return ev.Reply{}, e
			}
		}
		_, e = tx.Exec(ctx, `UPDATE evaluation_online_project_limits SET sampled=sampled+1,works=works+$2 WHERE project_id=$1`, s.ProjectID, len(v.Body.Bindings))
		if e != nil {
			return ev.Reply{}, e
		}
		_, e = tx.Exec(ctx, `UPDATE evaluation_online_rule_usage SET sampled=sampled+1,works=works+$4 WHERE project_id=$1 AND rule_id=$2 AND rule_version=$3`, s.ProjectID, ref.EntityID, ref.Version, len(v.Body.Bindings))
		if e != nil {
			return ev.Reply{}, e
		}
	}
	event := "online_skipped"
	if eligible {
		event = "online_rule_matched"
	}
	slog.Info(event, "event", event, "project_id", s.ProjectID, "observation_id", id, "outcome", outcome)
	if selection.Sampled {
		slog.Info("online_sampled", "event", "online_sampled", "observation_id", id)
	}
	if outcome == "SKIPPED_BUDGET" {
		slog.Info("online_budget_skipped", "event", "online_budget_skipped", "observation_id", id)
	}
	return ev.Reply{Code: ev.Applied, ID: id, Status: outcome}, nil
}

type MaterializationCandidate struct {
	ob.Candidate
	Rule asset.Ref
}

func (k Online) BackfillCandidates(ctx context.Context, c ob.Cursor, limit int) ([]ob.Candidate, error) {
	if limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	rows, e := k.Pool.Query(ctx, `SELECT project_id::text,id::text,created_at FROM evaluation_online_backfills WHERE NOT completed AND ($1::timestamptz IS NULL OR (created_at,id)>($1,$2::uuid)) ORDER BY created_at,id LIMIT $3`, nullableTime(c.Created), nullableID(c.ID), limit)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []ob.Candidate{}
	for rows.Next() {
		var v ob.Candidate
		if e = rows.Scan(&v.ProjectID, &v.ID, &v.Created); e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

func (k Online) MaterializationCandidates(ctx context.Context, c ob.Cursor, limit int) ([]MaterializationCandidate, error) {
	if limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	rows, e := k.Pool.Query(ctx, `WITH active AS (SELECT DISTINCT ON(project_id,entity_id) * FROM evaluation_online_rule_versions ORDER BY project_id,entity_id,published_at DESC,version DESC)
 SELECT o.project_id::text,o.id::text,o.created_at,v.entity_id::text,v.version FROM evaluation_observations o JOIN active v ON v.project_id=o.project_id AND v.enabled AND o.created_at>=v.published_at
 WHERE NOT EXISTS(SELECT 1 FROM evaluation_online_admissions a WHERE a.project_id=o.project_id AND a.rule_id=v.entity_id AND a.rule_version=v.version AND a.observation_id=o.id)
 AND ($1::timestamptz IS NULL OR (o.created_at,o.id)>($1,$2::uuid)) ORDER BY o.created_at,o.id,v.entity_id LIMIT $3`, nullableTime(c.Created), nullableID(c.ID), limit)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	result := []MaterializationCandidate{}
	for rows.Next() {
		var v MaterializationCandidate
		if e = rows.Scan(&v.ProjectID, &v.ID, &v.Created, &v.Rule.EntityID, &v.Rule.Version); e != nil {
			return nil, e
		}
		result = append(result, v)
	}
	return result, rows.Err()
}
func (k Online) StartBackfill(ctx context.Context, s ob.Scope, b ob.Backfill) (ev.Reply, error) {
	if !s.Reconcile || !asset.ValidID(b.CommandID) || b.Rule.Validate() != nil || b.From.IsZero() || !b.Until.After(b.From) || b.MaxRecords < 1 || b.MaxRecords > 1000 {
		return ev.Reply{Code: ev.Rejected}, nil
	}
	raw := encode(b)
	return k.transaction(ctx, s, true, func(tx pgx.Tx) (ev.Reply, error) {
		v, e := readRule(ctx, tx, s.Scope, b.Rule)
		if e != nil {
			return ev.Reply{}, e
		}
		if !v.Body.AllowBackfill || b.MaxRecords > v.Body.BackfillMax {
			return ev.Reply{}, stop(ev.Rejected, "BACKFILL_POLICY")
		}
		_, e = tx.Exec(ctx, `INSERT INTO evaluation_online_backfills(id,project_id,rule_id,rule_version,intent_bytes) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING`, b.CommandID, s.ProjectID, b.Rule.EntityID, b.Rule.Version, raw)
		if e != nil {
			return ev.Reply{}, e
		}
		var old []byte
		e = tx.QueryRow(ctx, `SELECT intent_bytes FROM evaluation_online_backfills WHERE project_id=$1 AND id=$2`, s.ProjectID, b.CommandID).Scan(&old)
		if e != nil {
			return ev.Reply{}, dbError(e)
		}
		if !bytes.Equal(old, raw) {
			return ev.Reply{}, stop(ev.Conflict, "BACKFILL_COMMAND_CONFLICT")
		}
		slog.Info("backfill_started", "event", "backfill_started", "backfill_id", b.CommandID)
		return ev.Reply{Code: ev.Applied, ID: b.CommandID}, nil
	})
}
func (k Online) ContinueBackfill(ctx context.Context, s ob.Scope, id string, batch int) (ob.BackfillReceipt, error) {
	result := ob.BackfillReceipt{ID: id}
	if !s.Reconcile || !asset.ValidID(id) || batch < 1 || batch > 100 {
		return result, asset.ErrInvalid
	}
	r, e := k.transaction(ctx, s, true, func(tx pgx.Tx) (ev.Reply, error) {
		var raw []byte
		var cursorAt *time.Time
		var cursorID *string
		var snapshotAt time.Time
		e := tx.QueryRow(ctx, `SELECT intent_bytes,processed,completed,cursor_at,cursor_id::text,created_at FROM evaluation_online_backfills WHERE project_id=$1 AND id=$2 FOR UPDATE`, s.ProjectID, id).Scan(&raw, &result.Processed, &result.Completed, &cursorAt, &cursorID, &snapshotAt)
		if e != nil {
			return ev.Reply{}, dbError(e)
		}
		if result.Completed {
			return ev.Reply{Code: ev.AlreadyApplied}, nil
		}
		var b ob.Backfill
		if e = json.Unmarshal(raw, &b); e != nil {
			return ev.Reply{}, e
		}
		rows, e := tx.Query(ctx, `SELECT id::text,created_at FROM evaluation_observations WHERE project_id=$1 AND completed_at>=$2 AND completed_at<$3 AND ($4::timestamptz IS NULL OR (created_at,id)>($4,$5::uuid)) AND created_at<=$6 ORDER BY created_at,id LIMIT $7`, s.ProjectID, b.From, b.Until, cursorAt, cursorID, snapshotAt, min(batch, b.MaxRecords-result.Processed))
		if e != nil {
			return ev.Reply{}, e
		}
		items := []ob.Candidate{}
		for rows.Next() {
			var c ob.Candidate
			if e = rows.Scan(&c.ID, &c.Created); e != nil {
				rows.Close()
				return ev.Reply{}, e
			}
			items = append(items, c)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return ev.Reply{}, e
		}
		for _, c := range items {
			if _, e = k.materialize(ctx, tx, s, b.Rule, c.ID, true); e != nil {
				return ev.Reply{}, e
			}
			result.Processed++
			cursorAt = &c.Created
			cursorID = &c.ID
		}
		result.Completed = len(items) < min(batch, b.MaxRecords-(result.Processed-len(items))) || result.Processed >= b.MaxRecords
		_, e = tx.Exec(ctx, `UPDATE evaluation_online_backfills SET processed=$3,completed=$4,cursor_at=$5,cursor_id=$6 WHERE project_id=$1 AND id=$2`, s.ProjectID, id, result.Processed, result.Completed, cursorAt, cursorID)
		return ev.Reply{Code: ev.Applied}, e
	})
	if e == nil && r.Code != ev.Applied && r.Code != ev.AlreadyApplied {
		e = asset.ErrForbidden
	}
	if result.Completed && e == nil {
		slog.Info("backfill_completed", "event", "backfill_completed", "backfill_id", id)
	}
	return result, e
}
