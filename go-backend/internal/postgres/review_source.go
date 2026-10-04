package postgres

import (
	"context"
	"encoding/json"
	"slices"
	"strings"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	ob "agentevalops/go-backend/internal/observation"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5"
)

func reviewHash(v any) string {
	j, e := asset.Freeze(v)
	if e != nil {
		panic(e)
	}
	return j.Digest()
}
func reviewDecode(ctx context.Context, q queryer, s asset.Scope, table, column, id string, dst any) error {
	// table/column 仅允许本包静态调用，不向 caller 暴露 SQL identifiers。
	var raw []byte
	e := q.QueryRow(ctx, "SELECT "+column+" FROM "+table+" r JOIN projects p ON p.id=r.project_id WHERE r.project_id=$1 AND p.org_id=$2 AND r.id=$3", s.ProjectID, s.OrganizationID, id).Scan(&raw)
	if e != nil {
		return dbError(e)
	}
	return json.Unmarshal(raw, dst)
}
func reviewAutomatic(ref rv.SourceRef, value, category string, prov asset.JSON, spec ev.EvaluatorSpec, m ev.MetricInput, calls []ev.ProviderCall, selected []string) *rv.Automatic {
	a := &rv.Automatic{Source: ref, Decision: value, Category: category, Evaluator: spec.Identity.Ref, EvaluatorDigest: spec.Identity.ContentDigest, Definition: spec.Definition, Metric: m.Identity.Ref, MetricDigest: m.Identity.ContentDigest}
	var p struct {
		TaskJudgment *struct{ Decision string } `json:"task_success"`
		Category     string
	}
	_ = json.Unmarshal(prov.Bytes(), &p)
	if p.Category != "" {
		a.Category = p.Category
	}
	if strings.Join(m.Definition.Labels, ",") == "SUCCESS,FAILURE,INCONCLUSIVE,NOT_APPLICABLE" {
		a.Decision = "INCONCLUSIVE"
		if p.TaskJudgment != nil {
			a.Decision = p.TaskJudgment.Decision
		}
	}
	a.ProviderError = strings.HasPrefix(a.Category, "PROVIDER_") || a.Category == "BUDGET_EXHAUSTED"
	a.InvalidOutput = a.Category == "INVALID_JUDGE_OUTPUT"
	a.IdentityDrift = a.Category == "PROVIDER_IDENTITY_MISMATCH" || a.Category == "UNKNOWN_PROVIDER_IDENTITY"
	for _, c := range calls {
		found := false
		for _, id := range selected {
			found = found || id == c.ID
		}
		if !found {
			continue
		}
		c.Response = asset.JSON{}
		c.ResponseBytes = nil
		c.Draft = nil
		c.DraftBytes = nil
		a.Calls = append(a.Calls, c)
		if spec.Definition.Kind == "LLM_JUDGE" && (c.ActualProvider == "" || c.ActualProvider == "UNKNOWN" || c.ActualModel == "" || c.ActualModel == "UNKNOWN" || c.ActualRevision == "" || c.ActualRevision == "UNKNOWN" || spec.Definition.Model == nil || c.ActualProvider != spec.Definition.Model.Provider || c.ActualModel != spec.Definition.Model.Model || c.ActualRevision != spec.Definition.Model.Revision) {
			a.IdentityDrift = true
		}
	}
	if spec.Definition.Kind == "LLM_JUDGE" && len(a.Calls) == 0 {
		a.IdentityDrift = true
	}
	if !slices.Contains(m.Definition.Labels, a.Decision) {
		a.InvalidOutput = true
	}
	if a.ProviderError || a.InvalidOutput || a.IdentityDrift {
		a.Decision = "INCONCLUSIVE"
	}
	return a
}
func resolveReviewSource(ctx context.Context, tx pgx.Tx, s asset.Scope, ref rv.SourceRef) (rv.Source, error) {
	source := rv.Source{Ref: ref, BodyAvailability: "UNAVAILABLE", Retention: "REFERENCE_ONLY", Evidence: []ev.Binding{}}
	switch ref.Type {
	case "OFFLINE_RESULT", "CALIBRATION_SAMPLE":
		state, e := decisionRun(ctx, tx, s, ref.RunID)
		if e != nil {
			return source, e
		}
		var result *ev.EvaluationResult
		for i := range state.Results {
			if state.Results[i].ID == ref.ResultID {
				result = &state.Results[i]
			}
		}
		if result == nil {
			return source, asset.ErrNotFound
		}
		var spec ev.EvaluatorSpec
		var m ev.MetricInput
		for _, v := range state.Run.Snapshot.Input.Evaluators {
			if v.Identity.Ref == (asset.Ref{EntityID: result.EvaluatorID, Version: result.EvaluatorVersion}) {
				spec = v
			}
		}
		if len(spec.Metrics) != 1 {
			return source, asset.ErrUnsupported
		}
		for _, v := range state.Run.Snapshot.Input.Metrics {
			if v.Identity.Ref == spec.Metrics[0] {
				m = v
			}
		}
		for _, c := range state.Run.Snapshot.Input.Manifest {
			if c.Identity.Ref == (asset.Ref{EntityID: result.CaseID, Version: result.CaseVersion}) {
				r, b := c.Identity.Ref, c.Case
				source.Case = &r
				source.CaseBody = &b
			}
		}
		if source.Case == nil {
			return source, asset.ErrUnsupported
		}
		source.Evidence = append(source.Evidence, result.Value.Artifact)
		source.Evidence = append(source.Evidence, result.Value.Evidence...)
		source.BodyAvailability = result.Value.Artifact.Availability
		source.Retention = string(source.CaseBody.BodyPolicy)
		var calls []ev.ProviderCall
		for _, w := range state.Works {
			if result.WorkID != nil && w.ID == *result.WorkID {
				calls = w.Calls
			}
		}
		source.Automatic = reviewAutomatic(ref, result.Value.Verdict, result.Value.SourceError, result.Value.Provenance, spec, m, calls, result.Value.SelectedCallIDs)
	case "ONLINE_RESULT", "FAILURE_CANDIDATE":
		var result ob.Result
		if ref.ResultID != "" {
			if e := reviewDecode(ctx, tx, s, "evaluation_online_results", "result_bytes", ref.ResultID, &result); e != nil {
				return source, e
			}
			source.ObservationID = result.ObservationID
			if ref.ObservationID != "" && ref.ObservationID != result.ObservationID {
				return source, asset.ErrNotFound
			}
		} else {
			source.ObservationID = ref.ObservationID
		}
		o, e := readObservation(ctx, tx, s, source.ObservationID)
		if e != nil {
			return source, e
		}
		if ref.Type == "FAILURE_CANDIDATE" {
			classification := ""
			switch ref.CandidateSource {
			case "ONLINE_RESULT":
				if result.ID == "" {
					return source, asset.ErrInvalid
				}
				name := result.Binding.Metric.Definition.Name
				var cfg map[string]asset.JSON
				_ = result.Binding.Evaluator.Definition.Config.Decode(&cfg)
				_ = cfg["metric"].Decode(&name)
				classification = ob.Classify(result.Value, name)
			case "OBSERVATION_RUNTIME_STATUS":
				if o.Status != "OK" {
					classification = "OBSERVED_RUNTIME_ERROR"
				}
			case "OBSERVATION_DELIVERY_STATUS":
				var root map[string]asset.JSON
				var attrs map[string]asset.JSON
				var delivery string
				_ = o.Envelope.Decode(&root)
				_ = root["attributes"].Decode(&attrs)
				_ = attrs["delivery_status"].Decode(&delivery)
				if delivery == "OUTCOME_UNKNOWN" {
					classification = "OUTCOME_UNKNOWN"
				}
			}
			if classification == "" || classification != ref.Classification {
				return source, asset.ErrInvalid
			}
		}
		source.BodyAvailability = o.BodyAvailability
		source.Retention = o.Retention
		for _, b := range o.Evidence {
			source.Evidence = append(source.Evidence, ev.Binding{ProjectID: s.ProjectID, Ref: b.Kind + ":" + b.Digest, Digest: b.Digest, Schema: b.Schema, Availability: b.Availability, Body: b.Body})
		}
		if len(source.Evidence) == 0 {
			source.Evidence = append(source.Evidence, ev.Binding{ProjectID: s.ProjectID, Ref: o.Ref.ID, Digest: o.Ref.Digest, Schema: o.Ref.Schema, Availability: "UNAVAILABLE"})
		}
		if result.ID != "" {
			calls := []ev.ProviderCall{}
			for _, c := range result.Calls {
				calls = append(calls, c.Provenance)
			}
			source.Automatic = reviewAutomatic(ref, result.Value.Verdict, result.Value.Category, result.Value.Provenance, result.Binding.Evaluator, result.Binding.Metric, calls, result.Value.SelectedCallIDs)
		}
	case "GATE_DECISION":
		var raw []byte
		var receipt decision.Receipt
		e := tx.QueryRow(ctx, `SELECT receipt_bytes FROM evaluation_gate_receipts WHERE project_id=$1 AND gate_id=$2`, s.ProjectID, ref.GateID).Scan(&raw)
		if e != nil {
			return source, dbError(e)
		}
		if e = json.Unmarshal(raw, &receipt); e != nil {
			return source, e
		}
		source.Evidence = []ev.Binding{{ProjectID: s.ProjectID, Ref: ref.GateID, Digest: reviewHash(receipt), Schema: decision.Contract, Availability: "UNAVAILABLE"}}
	}
	// 去重 presented evidence，避免 artifact 同时出现在 evidence 数组。
	unique := []ev.Binding{}
	seen := map[string]bool{}
	for _, b := range source.Evidence {
		if !seen[b.Ref] {
			unique = append(unique, b)
			seen[b.Ref] = true
		}
	}
	source.Evidence = unique
	source.EvidenceDigest = reviewHash(struct {
		Case     *asset.Ref
		Evidence []ev.Binding
	}{source.Case, source.Evidence})
	if source.Automatic != nil {
		source.Automatic.EvidenceDigest = source.EvidenceDigest
	}
	source.Digest = reviewHash(source)
	return source, nil
}

func reviewCaseSource(d rv.Draft, item rv.Item) asset.Source {
	metadata, _ := asset.Freeze(struct {
		ReviewItem, SourceDigest, DraftID, ObservationID string
		Source                                           rv.SourceRef
		GoldenID                                         *string
		AnnotationIDs                                    []string
		AdjudicationID                                   *string
		Schema                                           asset.Ref
		Protocol                                         rv.Protocol
		Reviewer                                         rv.Reviewer
		Sanitization, SanitizedDigest                    string
		HumanSupplement                                  bool
	}{item.ID, item.Source.Digest, d.ID, item.Source.ObservationID, item.Source.Ref, d.GoldenID, d.AnnotationIDs, d.AdjudicationID, item.Command.Schema, item.Command.Policy.Protocol, d.Reviewer, d.Sanitization, d.SanitizedDigest, d.HumanSupplement})
	return asset.Source{Kind: "REVIEWED_CASE", Ref: d.ID, Principal: d.Reviewer.Principal, Metadata: metadata}
}
