package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
	"context"
	"fmt"
)

const CoverageInput = "coverage-execution-input.v1"
const CoverageOutput = "coverage-execution-output.v1"
const CoverageImplementation = "coverage-execution-deterministic.v1"
const CoverageMetric = "coverage_execution_correctness.v1"
const CoverageNormalization = "coverage-execution-score.v1"
const CoverageTargetVersion = "coverage-execution-http.v1"

type CoverageSelector struct {
	TaskID      string `json:"task_id"`
	ReportID    string `json:"report_id"`
	PlanID      string `json:"plan_id"`
	TestID      string `json:"test_id"`
	InstanceID  string `json:"instance_id"`
	ExecutionID string `json:"execution_id"`
	IncidentID  string `json:"incident_id"`
	Diagnosis   string `json:"diagnosis_kind"`
}
type CoverageSubject struct {
	Mode      string                      `json:"mode"`
	Selectors map[string]CoverageSelector `json:"selectors"`
}
type CoverageReceipt struct {
	Protocol    string     `json:"protocol"`
	ProjectID   string     `json:"project_id"`
	RunID       string     `json:"run_id"`
	AttemptID   string     `json:"attempt_id"`
	RequestID   string     `json:"request_id"`
	CaseID      string     `json:"case_id"`
	InputDigest string     `json:"frozen_input_digest"`
	Input       asset.JSON `json:"input"`
	Business    asset.JSON `json:"business"`
	Digest      string     `json:"business_digest"`
}
type CoverageTruth struct {
	Version     string     `json:"version"`
	Visibility  string     `json:"visibility"`
	InputDigest string     `json:"input_digest"`
	Gain        int        `json:"required_gain"`
	Asset       asset.JSON `json:"expected_asset"`
	AssetDigest string     `json:"expected_asset_digest"`
	Actual      int        `json:"actual"`
	Expected    int        `json:"expected"`
	Outcome     string     `json:"outcome"`
	Diagnosis   JointTruth `json:"diagnosis"`
}

func coverageObject(v any) map[string]any { m, _ := v.(map[string]any); return m }
func coverageJSON(v any) asset.JSON       { j, _ := asset.Freeze(v); return j }
func coverageWithout(m map[string]any, keys ...string) asset.JSON {
	c := map[string]any{}
	for k, v := range m {
		skip := false
		for _, key := range keys {
			skip = skip || k == key
		}
		if !skip {
			c[k] = v
		}
	}
	return coverageJSON(c)
}
func coverageList(v any) []any                    { a, _ := v.([]any); return a }
func coverageObjects(j asset.JSON) map[string]any { var m map[string]any; _ = j.Decode(&m); return m }

func (v CoverageReceipt) Validate(input asset.JSON, q CoverageSelector, project, run, attempt, request, caseID string) error {
	b := coverageObjects(v.Business)
	outer := coverageObjects(input)
	task := coverageObject(b["task_snapshot"])
	p := coverageObject(b["plan"])
	r := coverageObject(b["report_envelope"])
	test := coverageObject(b["test"])
	source := coverageObject(b["workflow_source"])
	a := coverageObject(b["case_asset"])
	d := coverageObject(b["distribution"])
	t := coverageObject(b["trigger"])
	e := coverageObject(b["execution"])
	approval := coverageObject(source["approval_ref"])
	if v.Protocol != CoverageOutput || v.ProjectID != project || v.RunID != run || v.AttemptID != attempt || v.RequestID != request || v.CaseID != caseID || v.InputDigest != input.Digest() || v.Input.Digest() != input.Digest() || v.Business.Digest() != v.Digest || outer["schema_version"] != CoverageInput || q.Diagnosis != "RULE" || b["diagnosis_kind"] != "RULE" || fmt.Sprint(b["model_calls"]) != "0" || fmt.Sprint(b["exposed_at"]) == "" || b["exposed_at"] == nil {
		return asset.ErrInvalid
	}
	if b["task_id"] != q.TaskID || r["report_id"] != q.ReportID || p["plan_id"] != q.PlanID || test["test_id"] != q.TestID || b["instance_id"] != q.InstanceID || e["execution_id"] != q.ExecutionID || e["incident_id"] != q.IncidentID || e["instance_id"] != q.InstanceID {
		return asset.ErrInvalid
	}
	if coverageJSON(task).Digest() != coverageJSON(outer["task"]).Digest() || coverageWithout(task, "input_digest").Digest() != task["input_digest"] || coverageJSON(task["manifest"]).Digest() != task["corpus_digest"] || coverageWithout(p, "plan_digest").Digest() != p["plan_digest"] || coverageJSON(b["report"]).Digest() != r["report_digest"] || p["report_digest"] != r["report_digest"] || p["report_id"] != q.ReportID || p["task_id"] != q.TaskID || r["task_id"] != q.TaskID || r["input_digest"] != task["input_digest"] || r["corpus_digest"] != task["corpus_digest"] {
		return asset.ErrInvalid
	}
	if source["task_id"] != q.TaskID || source["task_input_digest"] != task["input_digest"] || source["corpus_digest"] != task["corpus_digest"] || source["report_id"] != q.ReportID || source["report_digest"] != r["report_digest"] || source["report_version"] != r["report_version"] || source["plan_id"] != q.PlanID || source["plan_digest"] != p["plan_digest"] || source["test_id"] != q.TestID || source["test_digest"] != coverageJSON(test).Digest() || source["candidate_kind"] != r["candidate_kind"] || source["candidate_version"] != r["candidate_version"] || !oneOf(fmt.Sprint(r["candidate_kind"]), "RULE", "HUMAN") {
		return asset.ErrInvalid
	}
	selected := false
	approved := false
	original := false
	for _, item := range coverageList(p["tests"]) {
		selected = selected || coverageJSON(item).Digest() == coverageJSON(test).Digest()
	}
	for _, item := range coverageList(p["decisions"]) {
		decision := coverageObject(item)
		approved = approved || (decision["finding_id"] == test["finding_id"] && decision["decision"] == "ACCEPT")
	}
	for _, item := range coverageList(coverageObject(b["report"])["findings"]) {
		f := coverageObject(item)
		if f["finding_id"] == test["finding_id"] {
			for _, candidate := range coverageList(f["recommended_tests"]) {
				original = original || coverageJSON(candidate).Digest() == coverageWithout(test, "finding_id", "source").Digest()
			}
		}
	}
	if !selected || !approved || !original || approval["plan_id"] != q.PlanID || approval["review_request_id"] != b["review_request_id"] || approval["reviewer"] != p["reviewer"] {
		return asset.ErrInvalid
	}
	if a["asset_digest"] != coverageWithout(a, "asset_digest").Digest() || source["asset_digest"] != a["asset_digest"] || d["asset_digest"] != a["asset_digest"] || d["distribution_digest"] != coverageWithout(d, "distribution_digest").Digest() || d["mode"] != "LOCAL_SIMULATED" || d["target_ref"] != outer["executor_ref"] || d["actor_kind"] != "HUMAN" || d["actor_user_id"] != p["reviewer"] {
		return asset.ErrInvalid
	}
	if t["intent_digest"] != coverageWithout(t, "intent_digest", "triggered_at", "derivation").Digest() || t["actor_kind"] != "HUMAN" || t["actor_user_id"] != p["reviewer"] || t["request_id"] != e["request_id"] || t["state_revision"] != e["state_revision"] || t["asset_digest"] != a["asset_digest"] || t["executor_ref"] != d["target_ref"] || t["verification_ref"] != nil {
		return asset.ErrInvalid
	}
	var joint JointReceipt
	if coverageJSON(b["joint"]).Decode(&joint) != nil || joint.Validate(joint.Input, BasebandSelector{IncidentID: q.IncidentID, Kind: "RULE"}, project, run, attempt, request, caseID) != nil || joint.Source.ExecutionID != q.ExecutionID || joint.Input.Digest() != b["recipe_digest"] {
		return asset.ErrInvalid
	}
	summary := coverageObject(e["summary"])
	safe := coverageObject(summary["workflow_source"])
	if summary["input_digest"] != joint.Input.Digest() || summary["execution_id"] != q.ExecutionID || safe["task_id"] != q.TaskID || safe["plan_id"] != q.PlanID || safe["test_id"] != q.TestID || safe["asset_digest"] != a["asset_digest"] || safe["distribution_id"] != d["distribution_id"] || safe["trigger_id"] != t["request_id"] || safe["plan_digest"] != p["plan_digest"] {
		return asset.ErrInvalid
	}
	for _, ev := range joint.Evidence {
		if ev.Kind == "CI_SUMMARY" && ev.Content.Digest() != coverageJSON(summary).Digest() {
			return asset.ErrInvalid
		}
	}
	return nil
}

func coverageCatalog(task map[string]any) map[string]any {
	for _, doc := range coverageList(task["documents"]) {
		d := coverageObject(doc)
		if d["kind"] == "INTERFACE_CATALOG" {
			return coverageObject(coverageObject(d["facts"])["catalog"])
		}
	}
	return nil
}
func coverageDefinition(c map[string]any, kind string, ref any) map[string]any {
	for _, raw := range coverageList(c[kind]) {
		v := coverageObject(raw)
		if coverageJSON(v["ref"]).Digest() == coverageJSON(ref).Digest() {
			return v
		}
	}
	return nil
}
func coverageValidTest(c, test map[string]any) bool {
	f := coverageDefinition(c, "features", test["feature_ref"])
	i := coverageDefinition(c, "interfaces", test["interface_ref"])
	t := coverageDefinition(c, "tags", test["tag_ref"])
	m := coverageDefinition(c, "messages", test["message_ref"])
	tc := coverageDefinition(c, "testcases", test["testcase_ref"])
	eq := func(a, b any) bool { return coverageJSON(a).Digest() == coverageJSON(b).Digest() }
	values := false
	for _, v := range coverageList(t["allowed_values"]) {
		values = values || eq(v, coverageObject(test["case_configuration"])["gain"])
	}
	return len(f) > 0 && len(i) > 0 && len(t) > 0 && len(m) > 0 && len(tc) > 0 && values && eq(f["interface_ref"], i["ref"]) && eq(t["interface_ref"], i["ref"]) && eq(m["interface_ref"], i["ref"]) && eq(m["tags"], []any{t["ref"]}) && eq(tc["message_ref"], m["ref"]) && eq(tc["feature_ref"], f["ref"])
}
func CoverageScore(v CoverageReceipt, gt CoverageTruth) (map[string]bool, map[string]int) {
	b := coverageObjects(v.Business)
	task := coverageObject(b["task_snapshot"])
	p := coverageObject(b["plan"])
	a := coverageObject(b["case_asset"])
	test := coverageObject(b["test"])
	c := coverageCatalog(task)
	counts := map[string]int{"required": 1, "matched": 0, "missing": 0, "unjustified": 0, "duplicate": 0}
	expected := coverageObjects(gt.Asset)
	for _, raw := range coverageList(p["tests"]) {
		t := coverageObject(raw)
		if !coverageValidTest(c, t) {
			counts["unjustified"]++
			continue
		}
		if fmt.Sprint(coverageObject(t["case_configuration"])["gain"]) == fmt.Sprint(gt.Gain) && coverageJSON(t["feature_ref"]).Digest() == coverageJSON(expected["feature_ref"]).Digest() && coverageJSON(t["message_ref"]).Digest() == coverageJSON(expected["message_ref"]).Digest() {
			counts["matched"]++
		}
	}
	if counts["matched"] == 0 {
		counts["missing"] = 1
	}
	if counts["matched"] > 1 {
		counts["duplicate"] = counts["matched"] - 1
	}
	selectedFinding := map[string]any{}
	for _, raw := range coverageList(coverageObject(b["report"])["findings"]) {
		f := coverageObject(raw)
		if f["finding_id"] == test["finding_id"] {
			selectedFinding = f
		}
	}
	historicalGap := selectedFinding["historical_feature_id"] == coverageObject(expected["feature_ref"])["id"] && selectedFinding["existing_coverage"] == "PARTIAL" && selectedFinding["coverage_class"] == "MUST_TEST"
	checks := map[string]bool{"coverage": historicalGap && counts["missing"] == 0 && counts["unjustified"] == 0 && counts["duplicate"] == 0, "approval_source": true, "asset": coverageValidTest(c, test) && coverageWithout(a, "asset_digest").Digest() == gt.AssetDigest && gt.Asset.Digest() == gt.AssetDigest, "execution": true, "diagnosis": true}
	var joint JointReceipt
	_ = coverageJSON(b["joint"]).Decode(&joint)
	checks["execution"] = joint.Input.Digest() == gt.Diagnosis.InputDigest && coverageObject(b["execution"])["outcome"] == gt.Outcome
	receiptFound := false
	for _, ev := range joint.Evidence {
		if ev.Kind == "EXECUTION_RECEIPT" {
			r := coverageObjects(ev.Content)
			receiptFound = true
			checks["execution"] = checks["execution"] && fmt.Sprint(r["actual"]) == fmt.Sprint(gt.Actual) && fmt.Sprint(r["expected"]) == fmt.Sprint(gt.Expected) && r["outcome"] == gt.Outcome
			stages := coverageList(r["stages"])
			checks["execution"] = checks["execution"] && len(stages) == 3
			for n, stage := range stages {
				s := coverageObject(stage)
				if n >= 3 || s["stage"] != []string{"PRECHECK", "L1", "COMPARISON"}[n] || s["status"] != "EXECUTED" {
					checks["execution"] = false
				}
			}
		}
	}
	checks["execution"] = checks["execution"] && receiptFound
	for _, ok := range JointScore(joint.Report, gt.Diagnosis, joint.Evidence) {
		checks["diagnosis"] = checks["diagnosis"] && ok
	}
	return checks, counts
}
func CoverageSupported(d metric.EvaluatorDefinition) bool {
	var c Config
	return d.Config.Decode(&c) == nil && c.Metric == CoverageMetric && d.ImplementationRef == CoverageImplementation && d.Normalization == CoverageNormalization && d.Validate() == nil && d.Kind == metric.Deterministic && d.Availability != asset.Unsupported && d.InputContract == CoverageInput && d.SchemaVersion == CoverageOutput && len(d.OutputMetrics) == 1 && d.Model == nil && d.PromptRef == nil && d.Budget.MaxProviderCalls == 0
}
func evaluateCoverage(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if ctx.Err() != nil {
		return worker.EvaluationOutput{}, ctx.Err()
	}
	value := BaseValue(in, "INCONCLUSIVE", "MISSING_COVERAGE_EXECUTION_SOURCE")
	c := in.Attempt.Request.Case.Case
	var gt CoverageTruth
	var actual CoverageReceipt
	var subject CoverageSubject
	a := in.Attempt.Metadata.Observation.Artifact
	if c.GroundTruth.Decode(&gt) != nil || gt.Version != "coverage-execution-truth.v1" || gt.Visibility != "DEVELOPMENT_VISIBLE" || gt.InputDigest != c.Input.Digest() || a == nil || !bound(*a, in) || a.Schema != CoverageOutput || a.Availability != "AVAILABLE" || in.Attempt.Outcome != "SUCCESS" || a.Body.Decode(&actual) != nil || in.Run.Snapshot.Subject.Decode(&subject) != nil || subject.Mode != "SEALED_BUSINESS_REPLAY" {
		return worker.EvaluationOutput{Value: value}, nil
	}
	selector, ok := subject.Selectors[in.Attempt.Request.Case.Identity.Ref.EntityID]
	if !ok || actual.Validate(c.Input, selector, in.Scope.ProjectID, in.Attempt.RunID, in.Attempt.ID, in.Attempt.RequestID, in.Attempt.Request.Case.Identity.Ref.EntityID) != nil {
		return worker.EvaluationOutput{Value: value}, nil
	}
	checks, counts := CoverageScore(actual, gt)
	score := 1.0
	verdict := "PASS"
	for _, ok := range checks {
		if !ok {
			score = 0
			verdict = "FAIL"
		}
	}
	value.Verdict = verdict
	value.SourceVerdict = verdict
	value.SourceError = ""
	value.Score = &score
	value.Reason = "COVERAGE_EXECUTION_SCORED"
	value.Provenance, _ = asset.Freeze(map[string]any{"applicability": asset.Applicable, "metric": CoverageMetric, "checks": checks, "coverage": counts, "input_digest": c.Input.Digest(), "ground_truth_digest": c.GroundTruth.Digest(), "business_digest": actual.Digest, "source": selector, "execution_outcome": coverageObject(coverageObjects(actual.Business)["execution"])["outcome"], "planning": "RULE/HUMAN", "diagnosis": "RULE", "model_calls": 0, "incremental_agent_value": "NOT_VALIDATED"})
	return worker.EvaluationOutput{Value: value}, nil
}
