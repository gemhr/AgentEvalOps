package decision

import (
	"agentevalops/go-backend/internal/metric"
	"encoding/json"
	"fmt"
	"math"
	"sort"

	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
)

func SubjectDimensions(j asset.JSON) map[string]string {
	var fields map[string]asset.JSON
	_ = j.Decode(&fields)
	read := func(keys ...string) string {
		for _, k := range keys {
			var s string
			if fields[k].Decode(&s) == nil && s != "" {
				return s
			}
		}
		return "UNKNOWN"
	}
	out := map[string]string{"subject": read("subject", "agent_id", "agent"), "subject_version": read("subject_version", "agent_version", "revision"), "environment": read("environment", "deployment_environment"), "run_mode": read("run_mode")}
	for _, k := range []string{"workflow_id", "workflow_version", "toolset_identity", "runtime_version", "profile"} {
		if v, ok := fields[k]; ok {
			out[k] = v.String()
		}
	}
	return out
}
func Offline(ref SourceRef, states []ev.RunState) (Source, error) {
	s := Source{Ref: ref, Owner: "OFFLINE", SamplingUnit: "case-run", Interpretation: "FROZEN_CASE_RUN_SAMPLES", Dimensions: map[string]string{}, Units: []Unit{}, Reasons: []string{}}
	for n, state := range states {
		if !state.Run.Status.Terminal() {
			s.Reasons = append(s.Reasons, "SOURCE_NOT_TERMINAL")
		}
		in := state.Run.Snapshot.Input
		if in.Origin != ev.PublishedCatalog {
			s.Reasons = append(s.Reasons, "LEGACY_INSUFFICIENT_PROVENANCE")
		}
		dims := SubjectDimensions(state.Run.Snapshot.Subject)
		dims["target_kind"] = state.Run.Snapshot.Target.Kind
		dims["target_version"] = state.Run.Snapshot.Target.Version
		dims["target_config"] = state.Run.Snapshot.Target.Config.Digest()
		dims["dataset"] = digest(in.Dataset)
		dims["suite"] = digest(in.Suite)
		for k, v := range dims {
			if n == 0 {
				s.Dimensions[k] = v
			} else if s.Dimensions[k] != v {
				s.Reasons = append(s.Reasons, "RUN_SET_IDENTITY_MISMATCH")
			}
		}
		latest, e := ev.Latest(state.Attempts)
		if e != nil {
			return s, e
		}
		if len(latest) != len(in.Manifest) {
			s.Reasons = append(s.Reasons, "CASE_MANIFEST_INCOMPLETE")
		}
		for _, c := range in.Manifest {
			u := Unit{Key: fmt.Sprintf("%s/%d", c.Identity.Ref.EntityID, n+1), CaseID: c.Identity.Ref.EntityID, CaseVersion: c.Identity.Ref.Version, CaseDigest: c.Identity.ContentDigest, Criticality: string(c.Case.Criticality), RunID: state.Run.ID, Repeat: n + 1, Task: "MISSING_EVIDENCE", TaskAvailability: "MISSING_EVIDENCE", Metrics: []MetricFact{}}
			a, ok := latest[u.CaseID]
			if c.Case.Capability == "CI_FAILURE_TRIAGE" {
				actual, valid := stage13Identity(a, state.Run.Snapshot.Target.Config, c.Case.Input)
				if !valid {
					s.Reasons = append(s.Reasons, "STAGE13_ACTUAL_MODEL_IDENTITY_BLOCKED")
				} else if previous, exists := s.Dimensions["ci_actual_model"]; exists && previous != actual {
					s.Reasons = append(s.Reasons, "STAGE13_ACTUAL_MODEL_IDENTITY_MISMATCH")
				} else {
					s.Dimensions["ci_actual_model"] = actual
				}
			}
			if !ok || a.Status != "TERMINAL" || a.CaseVersion != u.CaseVersion {
				s.Reasons = append(s.Reasons, "CASE_MANIFEST_INCOMPLETE")
			} else {
				u.Outcome = a.Outcome
				u.AttemptID = a.ID
			}
			switch a.Outcome {
			case ev.Failure, ev.Timeout, ev.Cancelled:
				u.Task = "FAILURE"
			case ev.Unknown:
				u.Task = "UNKNOWN_EXECUTION"
			}
			for _, spec := range in.Evaluators {
				if len(spec.Metrics) != 1 {
					s.Reasons = append(s.Reasons, "MULTI_METRIC_RESULT_UNSUPPORTED")
					continue
				}
				m := MetricFact{Evaluator: spec, Applicability: asset.MissingEvidence, Category: "MISSING_EVIDENCE"}
				for _, d := range in.Metrics {
					if d.Identity.Ref == spec.Metrics[0] {
						m.Metric = d
					}
				}
				s.ExpectedSlots++
				for _, res := range state.Results {
					if res.AttemptID != a.ID || res.EvaluatorID != spec.Identity.Ref.EntityID || res.EvaluatorVersion != spec.Identity.Ref.Version {
						continue
					}
					if m.ResultID != "" {
						return s, asset.ErrInvalid
					}
					if res.Value.SpecDigest != spec.Identity.ContentDigest || res.Value.InputDigest != c.Identity.ContentDigest {
						s.Reasons = append(s.Reasons, "RESULT_BINDING_INVALID")
					}
					m.ResultID = res.ID
					m.Verdict = res.Value.Verdict
					m.Category = res.Value.SourceError
					m.Value = res.Value.Score
					m.Provenance = res.Value.Provenance
					m.SelectedCallIDs = res.Value.SelectedCallIDs
					for _, w := range state.Works {
						if res.WorkID != nil && w.ID == *res.WorkID && w.Status == "COMPLETED" && w.ResultID != nil && *w.ResultID == res.ID {
							for _, call := range w.Calls {
								m.Calls = append(m.Calls, CallProvenance(call))
							}
							s.CompletedSlots++
						}
					}
					for _, b := range res.Value.Evidence {
						m.EvidenceSchemas = append(m.EvidenceSchemas, b.Schema)
					}
					m.EvidenceSchemas = append(m.EvidenceSchemas, res.Value.Artifact.Schema)
					m.EvidenceSchemas = unique(m.EvidenceSchemas)
					m.EvidenceDigest = digest(struct {
						Artifact ev.Binding
						Evidence []ev.Binding
					}{res.Value.Artifact, res.Value.Evidence})
				}
				readApplicability(&m)
				if metricKey(spec) == "task_success.v1" {
					if a.Outcome == ev.Success || m.Applicability == asset.NotApplicable {
						u.Task = TaskDecision(m)
					}
					u.TaskAvailability = string(m.Applicability)
				}
				if spec.Definition.ImplementationRef == "stage13.ci-triage-deterministic.v1" && metricKey(spec) == "ci_triage_decidability.v1" {
					u.Task = "INCONCLUSIVE"
					if m.Verdict == "PASS" {
						u.Task = "SUCCESS"
					}
				}
				u.Metrics = append(u.Metrics, m)
			}
			s.Units = append(s.Units, u)
		}
	}
	keys := []string{}
	for _, u := range s.Units {
		keys = append(keys, u.Key+"@"+u.CaseVersion+":"+u.CaseDigest)
	}
	sort.Strings(keys)
	s.Dimensions["case_set"] = digest(keys)
	s.EligiblePopulation = len(s.Units)
	s.SampleCount = len(s.Units)
	s.SamplingCoverage = ratio(s.SampleCount, s.EligiblePopulation)
	s.Reasons = unique(s.Reasons)
	s.Digest = digest(s)
	return s, nil
}

// stage13Identity 只消费本 Attempt 正式 Target 保存的实际回执，不使用历史 probe。
func stage13Identity(a ev.Attempt, config, input asset.JSON) (string, bool) {
	var cfg struct {
		ExpectedSubjectManifest asset.JSON `json:"expected_subject_manifest"`
	}
	if config.Decode(&cfg) != nil {
		return "", false
	}
	var cleanup struct {
		Comparability string `json:"comparability"`
		Model         struct {
			Policy     string  `json:"policy_version"`
			Comparable bool    `json:"comparable"`
			Provider   *string `json:"actual_provider"`
			Model      *string `json:"actual_model"`
		} `json:"model_comparability_decision"`
	}
	// 该事实由正式 Go Target 独立重算并绑定本 Attempt，不能用客户端声明替代。
	if a.Metadata.Observation.Cleanup.Decode(&cleanup) != nil || cleanup.Comparability != "COMPARABLE" || cleanup.Model.Policy != "stage13.model-comparability.v2" || !cleanup.Model.Comparable || cleanup.Model.Provider == nil || cleanup.Model.Model == nil {
		return "", false
	}
	for _, e := range a.Metadata.Observation.Evidence {
		if e.Schema != "stage13-subject-receipt.v1" {
			continue
		}
		var wire map[string]asset.JSON
		_ = e.Body.Decode(&wire)
		var selected string
		_ = wire["selected_final_run_id"].Decode(&selected)
		receipts := []asset.JSON{wire["actual_subject_receipt"]}
		var children []asset.JSON
		_ = wire["child_subject_receipts"].Decode(&children)
		receipts = append(receipts, children...)
		for _, receipt := range receipts {
			var r struct {
				Run      string     `json:"run_id"`
				Attempt  *string    `json:"evaluation_attempt_id"`
				Input    string     `json:"actual_input_digest"`
				Manifest asset.JSON `json:"actual_subject_manifest"`
			}
			if receipt.Decode(&r) != nil || r.Run != selected || r.Attempt == nil || *r.Attempt != a.ID || r.Input != input.Digest() || r.Manifest.String() != cfg.ExpectedSubjectManifest.String() {
				continue
			}
			var m map[string]asset.JSON
			_ = r.Manifest.Decode(&m)
			return digest(struct {
				Provider, Model *string
				Profile, Schema asset.JSON
			}{cleanup.Model.Provider, cleanup.Model.Model, m["model_profile_digest"], m["output_schema_digest"]}), true
		}
	}
	return "", false
}
func metricKey(s ev.EvaluatorSpec) string {
	var cfg struct {
		Metric string `json:"metric"`
	}
	_ = json.Unmarshal(s.Definition.Config.Bytes(), &cfg)
	return cfg.Metric
}
func readApplicability(m *MetricFact) {
	if m.ResultID == "" {
		return
	}
	var p struct {
		Applicability asset.ApplicabilityState `json:"applicability"`
	}
	if json.Unmarshal(m.Provenance.Bytes(), &p) == nil && p.Applicability != "" {
		m.Applicability = p.Applicability
	} else {
		m.Applicability = asset.MissingEvidence
		m.Category = "MISSING_APPLICABILITY_PROVENANCE"
		m.Value = nil
	}
	m.Value = MetricValue(*m)
}

func MetricValue(m MetricFact) *float64 {
	if m.Metric.Definition.ValueType == metric.Cost || m.Metric.Definition.ValueType == metric.Count {
		var fields map[string]json.RawMessage
		_ = json.Unmarshal(m.Provenance.Bytes(), &fields)
		key := "usage_availability"
		if m.Metric.Definition.ValueType == metric.Cost {
			key = "cost_availability"
		}
		var availability string
		_ = json.Unmarshal(fields[key], &availability)
		if availability != "AVAILABLE" {
			return nil
		}
	}
	if m.Verdict == "ERROR" || m.Verdict == "INCONCLUSIVE" || m.Applicability != asset.Applicable {
		return nil
	}
	return m.Value
}
func TaskDecision(m MetricFact) string {
	if m.Applicability == asset.NotApplicable {
		return "NOT_APPLICABLE"
	}
	if m.Applicability == asset.UnsupportedEvidence {
		return "UNSUPPORTED"
	}
	if m.Verdict == "ERROR" {
		return "EVALUATOR_ERROR"
	}
	if m.Verdict == "INCONCLUSIVE" && m.Applicability == asset.Applicable {
		return "INCONCLUSIVE"
	}
	if m.ResultID == "" || m.Applicability == asset.MissingEvidence {
		return "MISSING_EVIDENCE"
	}
	var p struct {
		Task struct {
			Version, Decision, Method, Reason string
			Confidence                        *float64
			ProducerRef                       string `json:"producer_ref"`
		} `json:"task_success"`
	}
	if json.Unmarshal(m.Provenance.Bytes(), &p) != nil || p.Task.Version != "task_success.v1" || !one(p.Task.Decision, "SUCCESS", "FAILURE", "INCONCLUSIVE", "NOT_APPLICABLE") || !one(p.Task.Method, "DETERMINISTIC", "LLM_JUDGE", "HUMAN", "IMPORTED") || p.Task.ProducerRef == "" || !asset.ContentText(p.Task.Reason) || (p.Task.Confidence != nil && (math.IsNaN(*p.Task.Confidence) || math.IsInf(*p.Task.Confidence, 0) || *p.Task.Confidence < 0 || *p.Task.Confidence > 1)) {
		return "MISSING_EVIDENCE"
	}
	return p.Task.Decision
}

// CallProvenance 保留事实身份与 digest，不把 Provider 正文复制到 Gate Receipt。
func CallProvenance(c ev.ProviderCall) ev.ProviderCall {
	c.Response, c.Draft, c.ResponseBytes, c.DraftBytes = asset.JSON{}, nil, nil, nil
	return c
}
