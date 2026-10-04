package postgres

import (
	"context"
	"encoding/json"

	"agentevalops/go-backend/internal/asset"
	ob "agentevalops/go-backend/internal/observation"
	"github.com/jackc/pgx/v5"
)

func (k Online) GetOnlineCoverage(ctx context.Context, s asset.Scope, ref asset.Ref) (ob.Coverage, error) {
	var c ob.Coverage
	if s.Validate(false) != nil || ref.Validate() != nil {
		return c, asset.ErrInvalid
	}
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if e != nil {
		return c, e
	}
	defer rollback(ctx, tx)
	rule, e := readRule(ctx, tx, s, ref)
	if e != nil {
		return c, e
	}
	c.Policy = rule.Body.Sampling.Policy
	c.Algorithm = rule.Body.Sampling.Algorithm
	c.Rule = ref
	c.RuleDigest = rule.Digest
	c.Sampling = rule.Body.Sampling
	c.Filter = rule.Body.Filter
	rows, e := tx.Query(ctx, `SELECT selection_bytes,outcome FROM evaluation_online_admissions WHERE project_id=$1 AND rule_id=$2 AND rule_version=$3`, s.ProjectID, ref.EntityID, ref.Version)
	if e != nil {
		return c, e
	}
	for rows.Next() {
		var raw []byte
		var outcome string
		if e = rows.Scan(&raw, &outcome); e != nil {
			rows.Close()
			return c, e
		}
		var selection ob.Selection
		if e = json.Unmarshal(raw, &selection); e != nil {
			rows.Close()
			return c, e
		}
		if selection.Eligible {
			c.Eligible++
		}
		if selection.Sampled {
			c.Sampled++
		}
		if outcome == "SKIPPED_BUDGET" {
			c.AdmissionBudgetSkipped++
			c.BudgetSkipped += len(rule.Body.Bindings)
		}
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return c, e
	}
	if e = tx.QueryRow(ctx, `SELECT count(*) FROM evaluation_online_works WHERE project_id=$1 AND rule_id=$2 AND rule_version=$3`, s.ProjectID, ref.EntityID, ref.Version).Scan(&c.WorkCreated); e != nil {
		return c, e
	}
	rows, e = tx.Query(ctx, `SELECT r.result_bytes FROM evaluation_online_results r JOIN evaluation_online_works w ON w.project_id=r.project_id AND w.id=r.work_id WHERE w.project_id=$1 AND rule_id=$2 AND rule_version=$3`, s.ProjectID, ref.EntityID, ref.Version)
	if e != nil {
		return c, e
	}
	for rows.Next() {
		var raw []byte
		if e = rows.Scan(&raw); e != nil {
			rows.Close()
			return c, e
		}
		var result ob.Result
		if e = json.Unmarshal(raw, &result); e != nil {
			rows.Close()
			return c, e
		}
		v := result.Value
		c.Completed++
		switch v.Applicability {
		case asset.NotApplicable:
			c.NotApplicable++
		case asset.MissingEvidence:
			c.MissingEvidence++
		case asset.UnsupportedEvidence:
			c.Unsupported++
		}
		switch v.Category {
		case "SKIPPED_BUDGET":
			c.BudgetSkipped++
		case "SKIPPED_RATE_LIMIT":
			c.RateLimited++
		default:
			if v.Verdict == "ERROR" {
				c.EvaluatorError++
			}
		}
		if v.Applicability == asset.Applicable && (v.Verdict == "PASS" || v.Verdict == "FAIL") {
			c.Decidable++
		}
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return c, e
	}
	c.Calculate()
	return c, tx.Commit(ctx)
}
func (k Online) ListOnlineResults(ctx context.Context, s asset.Scope, c ob.Cursor, limit int) ([]ob.Result, error) {
	if s.Validate(false) != nil || limit < 1 || limit > 100 {
		return nil, asset.ErrInvalid
	}
	rows, e := k.Pool.Query(ctx, `SELECT r.result_bytes,r.created_at FROM evaluation_online_results r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND ($3::timestamptz IS NULL OR (r.created_at,r.id)>($3,$4::uuid)) ORDER BY r.created_at,r.id LIMIT $5`, s.ProjectID, s.OrganizationID, nullableTime(c.Created), nullableID(c.ID), limit)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	result := []ob.Result{}
	for rows.Next() {
		var r ob.Result
		var raw []byte
		if e = rows.Scan(&raw, &r.Created); e != nil {
			return nil, e
		}
		created := r.Created
		if e = json.Unmarshal(raw, &r); e != nil {
			return nil, e
		}
		r.Created = created
		result = append(result, r)
	}
	return result, rows.Err()
}
func (k Online) ListFailureCandidates(ctx context.Context, s asset.Scope, c ob.Cursor, limit int) ([]ob.FailureCandidate, error) {
	results, e := k.ListOnlineResults(ctx, s, c, limit)
	if e != nil {
		return nil, e
	}
	out := []ob.FailureCandidate{}
	for _, r := range results {
		metricName := r.Binding.Metric.Definition.Name
		var config map[string]asset.JSON
		if r.Binding.Evaluator.Definition.Config.Decode(&config) == nil {
			_ = config["metric"].Decode(&metricName)
		}
		classification := ob.Classify(r.Value, metricName)
		if classification != "" {
			out = append(out, ob.FailureCandidate{ProjectID: s.ProjectID, ObservationID: r.ObservationID, ResultID: r.ID, Source: "ONLINE_RESULT", Classification: classification, ClassifierVersion: "failure-source.v1"})
		}
	}
	observations, e := k.ListObservations(ctx, s, c, limit)
	if e != nil {
		return nil, e
	}
	for _, o := range observations {
		if o.Status != "OK" {
			out = append(out, ob.FailureCandidate{ProjectID: s.ProjectID, ObservationID: o.Ref.ID, Source: "OBSERVATION_RUNTIME_STATUS", Classification: "OBSERVED_RUNTIME_ERROR", ClassifierVersion: "failure-source.v1"})
		}
		var envelope map[string]asset.JSON
		var attrs map[string]asset.JSON
		var delivery string
		_ = o.Envelope.Decode(&envelope)
		_ = envelope["attributes"].Decode(&attrs)
		_ = attrs["delivery_status"].Decode(&delivery)
		if delivery == "OUTCOME_UNKNOWN" {
			out = append(out, ob.FailureCandidate{ProjectID: s.ProjectID, ObservationID: o.Ref.ID, Source: "OBSERVATION_DELIVERY_STATUS", Classification: "OUTCOME_UNKNOWN", ClassifierVersion: "failure-source.v1"})
		}
	}
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}
