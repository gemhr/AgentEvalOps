package postgres

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"sort"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	ob "agentevalops/go-backend/internal/observation"
	"github.com/jackc/pgx/v5"
)

// 同一 repeatable-read transaction 读取所有 source，不能拼接不同快照的 aggregate。
func decisionRun(ctx context.Context, tx pgx.Tx, s asset.Scope, id string) (ev.RunState, error) {
	r, e := getRun(ctx, tx, ev.Scope{Scope: s}, id, false)
	if e != nil {
		return ev.RunState{}, e
	}
	a, w, e := attemptsAndWorks(ctx, tx, ev.Scope{Scope: s}, id)
	if e != nil {
		return ev.RunState{}, e
	}
	for i := range a {
		if a[i].Metadata.ObservationBytes != nil {
			if e = json.Unmarshal(a[i].Metadata.ObservationBytes, &a[i].Metadata.Observation); e != nil {
				return ev.RunState{}, e
			}
		}
	}
	for i := range w {
		if e = restoreCalls(&w[i]); e != nil {
			return ev.RunState{}, e
		}
	}
	results := []ev.EvaluationResult{}
	rows, e := tx.Query(ctx, "SELECT to_jsonb(r) FROM evaluation_results r WHERE project_id=$1 AND run_id=$2 ORDER BY id", s.ProjectID, id)
	if e != nil {
		return ev.RunState{}, e
	}
	defer rows.Close()
	for rows.Next() {
		var raw []byte
		var v ev.EvaluationResult
		if e = rows.Scan(&raw); e != nil {
			return ev.RunState{}, e
		}
		if e = json.Unmarshal(raw, &v); e != nil {
			return ev.RunState{}, e
		}
		var meta struct{ ValueBytes []byte }
		if e = json.Unmarshal(v.Metadata.Bytes(), &meta); e != nil {
			return ev.RunState{}, e
		}
		if meta.ValueBytes == nil {
			return ev.RunState{}, asset.ErrUnsupported
		}
		if e = json.Unmarshal(meta.ValueBytes, &v.Value); e != nil {
			return ev.RunState{}, e
		}
		results = append(results, v)
	}
	return ev.RunState{Run: r, Attempts: a, Works: w, Results: results}, rows.Err()
}
func resolveDecisionSource(ctx context.Context, tx pgx.Tx, s asset.Scope, ref decision.SourceRef) (decision.Source, error) {
	if ref.Kind == "ONLINE" {
		return decisionOnline(ctx, tx, s, ref)
	}
	ids := ref.Runs
	if ref.Kind == "EXPERIMENT" {
		exp, e := getExperiment(ctx, tx, s, ref.Experiment)
		if e != nil {
			return decision.Source{}, e
		}
		ids = []string{}
		for _, slot := range exp.Slots {
			if slot.RunID == nil {
				return decision.Source{}, fmt.Errorf("%w: EXPERIMENT_NOT_MATERIALIZED", asset.ErrInvalid)
			}
			ids = append(ids, *slot.RunID)
		}
	}
	states := []ev.RunState{}
	for _, id := range ids {
		state, e := decisionRun(ctx, tx, s, id)
		if e != nil {
			return decision.Source{}, e
		}
		states = append(states, state)
	}
	return decision.Offline(ref, states)
}
func decisionHash(v any) string { j, _ := asset.Freeze(v); return j.Digest() }
func decisionOnline(ctx context.Context, tx pgx.Tx, s asset.Scope, ref decision.SourceRef) (decision.Source, error) {
	r, e := readRule(ctx, tx, s, ref.Rule)
	if e != nil {
		return decision.Source{}, e
	}
	filter := r.Body.Filter
	filter.Subject = ""
	filter.AgentVersion = ""
	src := decision.Source{Ref: ref, Owner: "ONLINE", SamplingUnit: "trace", Interpretation: "SAMPLED_ONLY_NOT_POPULATION_ESTIMATE", Dimensions: map[string]string{"subject": ref.Subject, "subject_version": ref.SubjectVersion, "environment": r.Body.Filter.Environment, "run_mode": r.Body.Filter.RunMode, "rule": decisionHash(ref.Rule), "filter": decisionHash(filter), "sampling": decisionHash(r.Body.Sampling), "window": fmt.Sprint(ref.Until.Sub(*ref.From)), "source": r.Body.Source, "trust": r.Body.Trust, "population": decisionHash(struct {
		Scope         ob.Kind
		Source, Trust string
		Filter        ob.Filter
	}{r.Body.Scope, r.Body.Source, r.Body.Trust, filter})}, Units: []decision.Unit{}, Reasons: []string{}}
	// 没有 canonical deployment pair；只允许具有显式已存 subject/version 的同规则总体。
	if r.Body.Filter.Environment == "" || r.Body.Filter.RunMode == "" {
		src.Reasons = append(src.Reasons, "ONLINE_POPULATION_IDENTITY_UNKNOWN")
	}
	rows, e := tx.Query(ctx, `SELECT observation_bytes,o.id::text FROM evaluation_observations o WHERE project_id=$1 AND completed_at >= $2 AND completed_at < $3 ORDER BY created_at,id LIMIT 10001`, s.ProjectID, ref.From, ref.Until)
	if e != nil {
		return src, e
	}
	observations := []ob.Observation{}
	for rows.Next() {
		var raw []byte
		var o ob.Observation
		var id string
		if e = rows.Scan(&raw, &id); e != nil {
			rows.Close()
			return src, e
		}
		if e = json.Unmarshal(raw, &o); e != nil {
			rows.Close()
			return src, e
		}
		observations = append(observations, o)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return src, e
	}
	if len(observations) > 10000 {
		return src, asset.ErrUnsupported
	}
	for _, o := range observations {
		dims := decision.SubjectDimensions(o.Metadata)
		if dims["subject"] != ref.Subject || dims["subject_version"] != ref.SubjectVersion {
			continue
		}
		if !ob.Match(r.Body, o) {
			continue
		}
		src.EligiblePopulation++
		var raw []byte
		var selection ob.Selection
		e = tx.QueryRow(ctx, `SELECT selection_bytes FROM evaluation_online_admissions WHERE project_id=$1 AND rule_id=$2 AND rule_version=$3 AND observation_id=$4`, s.ProjectID, ref.Rule.EntityID, ref.Rule.Version, o.Ref.ID).Scan(&raw)
		if e == pgx.ErrNoRows {
			src.Reasons = append(src.Reasons, "ONLINE_ADMISSION_INCOMPLETE")
			continue
		}
		if e != nil {
			return src, e
		}
		if e = json.Unmarshal(raw, &selection); e != nil {
			return src, e
		}
		if !selection.Sampled {
			continue
		}
		src.SampleCount++
		u := decision.Unit{Key: o.Ref.ID, CaseID: o.Ref.ID, CaseVersion: o.Ref.Schema, CaseDigest: o.Ref.Digest, Task: "MISSING_EVIDENCE", Metrics: []decision.MetricFact{}}
		for _, binding := range r.Body.Bindings {
			src.ExpectedSlots++
			m := decision.MetricFact{Metric: binding.Metric, Evaluator: binding.Evaluator, Applicability: asset.MissingEvidence, Category: "MISSING_EVIDENCE", EvidenceSchemas: []string{o.Ref.Schema}}
			var result ob.Result
			e = tx.QueryRow(ctx, `SELECT result_bytes FROM evaluation_online_results r JOIN evaluation_online_works w ON w.project_id=r.project_id AND w.id=r.work_id WHERE w.project_id=$1 AND rule_id=$2 AND rule_version=$3 AND observation_id=$4 AND evaluator_id=$5 AND evaluator_version=$6`, s.ProjectID, ref.Rule.EntityID, ref.Rule.Version, o.Ref.ID, binding.Evaluator.Identity.Ref.EntityID, binding.Evaluator.Identity.Ref.Version).Scan(&raw)
			if e != nil && e != pgx.ErrNoRows {
				return src, e
			}
			if e == nil {
				if e = json.Unmarshal(raw, &result); e != nil {
					return src, e
				}
				src.CompletedSlots++
				m.ResultID = result.ID
				m.Verdict = result.Value.Verdict
				m.Category = result.Value.Category
				m.Value = result.Value.Score
				m.Applicability = result.Value.Applicability
				m.Provenance = result.Value.Provenance
				m.SelectedCallIDs = result.Value.SelectedCallIDs
				for _, call := range result.Calls {
					m.Calls = append(m.Calls, decision.CallProvenance(call.Provenance))
				}
				m.EvidenceDigest = result.EvidenceDigest
			}
			for _, b := range o.Evidence {
				m.EvidenceSchemas = append(m.EvidenceSchemas, b.Schema)
			}
			m.Value = decision.MetricValue(m)
			sort.Strings(m.EvidenceSchemas)
			m.EvidenceSchemas = slices.Compact(m.EvidenceSchemas)
			if binding.Metric.Definition.ValueType == "ENUM" {
				u.Task = decision.TaskDecision(m)
			}
			u.Metrics = append(u.Metrics, m)
		}
		src.Units = append(src.Units, u)
	}
	if src.EligiblePopulation > 0 {
		v := float64(src.SampleCount) / float64(src.EligiblePopulation)
		src.SamplingCoverage = &v
	}
	src.Digest = decisionHash(src)
	return src, nil
}
