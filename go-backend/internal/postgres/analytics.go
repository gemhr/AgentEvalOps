package postgres

import (
	"agentevalops/go-backend/internal/analytics"
	"agentevalops/go-backend/internal/asset"
	"context"
	"encoding/json"
	"github.com/jackc/pgx/v5"
	"sort"
)

// Aggregate 在一个短只读快照中聚合，最多返回 2000 个分组；不截断后声称完整。
func (k ProductReader) Aggregate(ctx context.Context, s asset.Scope, q analytics.Query, kind string) (analytics.Response, error) {
	out := analytics.Response{ProjectionVersion: analytics.Version, ProjectID: s.ProjectID, Timezone: "UTC", Query: q, Consistency: "DIRECT_QUERY_REPEATABLE_READ", Rows: []map[string]any{}}
	if s.Validate(false) != nil || q.Validate() != nil {
		return out, asset.ErrInvalid
	}
	sql, ok := analyticsSQL[kind]
	if !ok {
		return out, asset.ErrInvalid
	}
	expected := map[string]string{"overview": "ALL", "quality": "OFFLINE", "trends": "OFFLINE", "gates": "GATE", "online": "ONLINE", "human": "HUMAN"}[kind]
	if q.Source != "" && q.Source != expected {
		return out, asset.ErrInvalid
	}
	q.Source = expected
	out.Query = q
	tx, e := k.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if e != nil {
		return out, e
	}
	defer rollback(ctx, tx)
	var found bool
	e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM projects WHERE id=$1 AND org_id=$2),transaction_timestamp()`, s.ProjectID, s.OrganizationID).Scan(&found, &out.AsOf)
	if e != nil {
		return out, e
	}
	if !found {
		return out, asset.ErrNotFound
	}
	out.AsOf = out.AsOf.UTC()
	out.Unit = analyticsUnits[kind]
	out.Denominator = analyticsDenominators[kind]
	rows, e := tx.Query(ctx, sql, s.ProjectID, q.From, q.Until, q.Bucket, q.Subject, q.Environment, q.RunMode, q.Metric, q.Evaluator, q.Rule)
	if e != nil {
		return out, e
	}
	for rows.Next() {
		var raw []byte
		var row map[string]any
		if e = rows.Scan(&raw); e == nil {
			e = json.Unmarshal(raw, &row)
		}
		if e != nil {
			rows.Close()
			return out, e
		}
		out.Rows = append(out.Rows, row)
		if len(out.Rows) > 2000 {
			rows.Close()
			return out, asset.ErrUnsupported
		}
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return out, e
	}
	keys := map[string][]string{"quality": {"dataset", "suite", "subject", "subject_version", "environment", "run_mode", "evaluator", "evaluator_digest", "metric_refs"}, "trends": {"bucket", "dataset", "suite", "subject", "subject_version", "environment", "run_mode", "evaluator", "evaluator_digest", "metric_refs"}, "gates": {"policy", "policy_digest", "baseline_kind", "candidate_kind"}, "online": {"rule_id", "rule_version", "source"}, "human": {"calibration_report_id"}}[kind]
	identity := func(row map[string]any) string {
		values := []any{}
		for _, key := range keys {
			values = append(values, row[key])
		}
		raw, _ := json.Marshal(values)
		return string(raw)
	}
	sort.Slice(out.Rows, func(i, j int) bool { return identity(out.Rows[i]) < identity(out.Rows[j]) })
	return out, tx.Commit(ctx)
}

var analyticsUnits = map[string]string{"overview": "named canonical entities", "quality": "case-run per frozen evaluator segment; evaluation slots", "trends": "case-run per frozen evaluator segment", "gates": "gate receipt", "online": "observation-rule-version; observation-evaluator-slot", "human": "review item; review slot; immutable calibration report"}
var analyticsDenominators = map[string]string{"overview": "counts within [from,until)", "quality": "task rate=success/(success+failure); task coverage=decidable/eligible; evaluation coverage=completed/expected slots", "trends": "same as quality; UTC bucket from immutable Run created_at", "gates": "receipts per frozen policy and source kinds", "online": "sampling=sampled/eligible admissions; evaluation=completed/work; decision=decidable/work; sampled only, not population estimate", "human": "human label coverage=current valid golden items/eligible items; agreement per immutable report, never merged across reports"}

// 保留参数类型使所有查询可共用同一严格绑定形状。
const analyticsParams = `WITH args AS (SELECT $1::uuid p,$2::timestamptz f,$3::timestamptz u,$4::text bucket,$5::text subject,$6::text environment,$7::text run_mode,$8::text metric,$9::text evaluator,$10::text rule)`

var analyticsSQL = map[string]string{
	"overview": analyticsParams + ` SELECT jsonb_build_object(
 'cases',(SELECT count(*) FROM evaluation_cases,args WHERE project_id=p AND created_at>=f AND created_at<u),
 'datasets',(SELECT count(*) FROM evaluation_datasets,args WHERE project_id=p AND created_at>=f AND created_at<u),
 'suites',(SELECT count(*) FROM evaluation_suites,args WHERE project_id=p AND created_at>=f AND created_at<u),
 'runs',(SELECT count(*) FROM evaluation_runs,args WHERE project_id=p AND created_at>=f AND created_at<u),
 'experiments',(SELECT count(*) FROM evaluation_experiments,args WHERE project_id=p AND created_at>=f AND created_at<u),
 'results',(SELECT count(*) FROM evaluation_results,args WHERE project_id=p AND created_at>=f AND created_at<u),
 'observations',(SELECT count(*) FROM evaluation_observations,args WHERE project_id=p AND created_at>=f AND created_at<u),
 'online_work',(SELECT count(*) FROM evaluation_online_works,args WHERE project_id=p AND created_at>=f AND created_at<u),
 'online_results',(SELECT count(*) FROM evaluation_online_results,args WHERE project_id=p AND created_at>=f AND created_at<u),
 'gate_decisions',(SELECT count(*) FROM evaluation_gate_receipts,args WHERE project_id=p AND created_at>=f AND created_at<u),
 'review_queue',(SELECT count(*) FROM evaluation_review_items,args WHERE project_id=p AND created_at>=f AND created_at<u AND status<>'COMPLETED'),
 'golden_labels',(SELECT count(*) FROM evaluation_golden_labels,args WHERE project_id=p AND created_at>=f AND created_at<u))`,
}

// 一个 evaluator segment 中每个 case-run 一槽。所有绑定取 Run 的冻结输入，不读取 latest definition。
const qualityFacts = analyticsParams + `, runs AS MATERIALIZED(
 SELECT r.* FROM evaluation_runs r,args WHERE r.project_id=p AND r.created_at>=f AND r.created_at<u AND r.kernel_snapshot IS NOT NULL
 AND (subject='' OR r.subject_ref->>'agent'=subject) AND (environment='' OR r.subject_ref->>'environment'=environment) AND (run_mode='' OR r.subject_ref->>'run_mode'=run_mode)
), latest AS(
 SELECT DISTINCT ON(a.run_id,a.case_id,a.case_version) a.* FROM evaluation_attempts a JOIN runs r ON r.id=a.run_id AND r.project_id=a.project_id ORDER BY a.run_id,a.case_id,a.case_version,a.attempt_no DESC
), facts AS(
 SELECT r.created_at,concat_ws('@',r.dataset_id,r.dataset_version) dataset,concat_ws('@',r.suite_id,r.suite_version) suite,
 coalesce(r.subject_ref->>'agent','UNKNOWN') subject,coalesce(r.subject_ref->>'revision','UNKNOWN') subject_version,coalesce(r.subject_ref->>'environment','UNKNOWN') environment,coalesce(r.subject_ref->>'run_mode','UNKNOWN') run_mode,
 concat_ws('@',spec->'identity'->'ref'->>'entity_id',spec->'identity'->'ref'->>'version') evaluator,
 spec->'identity'->>'content_digest' evaluator_digest, spec->'definition'->'config_snapshot'->>'metric' metric,
 spec->'metric_bindings' metric_refs, spec->'definition'->>'implementation_ref' implementation,
 a.execution_outcome_kind outcome,x.id result_id,x.verdict,x.kernel_value->'Provenance' provenance
 FROM runs r CROSS JOIN LATERAL jsonb_array_elements(r.kernel_snapshot->'Input'->'ordered_manifest') c
 CROSS JOIN LATERAL jsonb_array_elements(r.kernel_snapshot->'Input'->'evaluator_specs') spec
 LEFT JOIN latest a ON a.run_id=r.id AND a.project_id=r.project_id AND a.case_id=c->'identity'->'ref'->>'entity_id' AND a.case_version=c->'identity'->'ref'->>'version'
 LEFT JOIN evaluation_results x ON x.project_id=a.project_id AND x.attempt_id=a.id AND x.evaluator_id=spec->'identity'->'ref'->>'entity_id' AND x.evaluator_version=spec->'identity'->'ref'->>'version'
), filtered AS(SELECT facts.* FROM facts,args WHERE (args.metric='' OR facts.metric=args.metric) AND (args.evaluator='' OR facts.evaluator=args.evaluator))`

func init() {
	for _, kind := range []string{"quality", "trends"} {
		bucket := "NULL::timestamptz"
		if kind == "trends" {
			bucket = "date_trunc((SELECT bucket FROM args),created_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
		}
		analyticsSQL[kind] = qualityFacts + `, classified AS(SELECT *,` + bucket + ` bucket,
 CASE WHEN metric<>'task_success.v1' OR metric IS NULL THEN 'N/A'
 WHEN provenance->>'applicability'='NOT_APPLICABLE' THEN 'N/A'
 WHEN outcome='OUTCOME_UNKNOWN' THEN 'unknown'
 WHEN outcome IN('FAILURE','TIMEOUT','CANCELLED') THEN 'failure'
 WHEN result_id IS NULL OR outcome IS NULL OR provenance->>'applicability'='MISSING_EVIDENCE' THEN 'missing'
 WHEN provenance->>'applicability'='UNSUPPORTED' THEN 'unsupported'
 WHEN provenance->'task_success'->>'version'='task_success.v1' AND provenance->'task_success'->>'decision'='SUCCESS' THEN 'success'
 WHEN provenance->'task_success'->>'version'='task_success.v1' AND provenance->'task_success'->>'decision'='FAILURE' THEN 'failure'
 WHEN provenance->'task_success'->>'decision'='NOT_APPLICABLE' THEN 'N/A' ELSE 'inconclusive' END task
 FROM filtered), summary AS(SELECT bucket,dataset,suite,subject,subject_version,environment,run_mode,evaluator,evaluator_digest,metric,metric_refs,implementation,
 count(*) expected,count(result_id) completed,count(*) FILTER(WHERE result_id IS NULL) evaluation_missing,
 count(*) FILTER(WHERE verdict='PASS') pass,count(*) FILTER(WHERE verdict='FAIL') fail,count(*) FILTER(WHERE verdict='INCONCLUSIVE') evaluation_inconclusive,count(*) FILTER(WHERE verdict='ERROR') error,
 count(*) FILTER(WHERE provenance->>'applicability'='NOT_APPLICABLE') not_applicable,
 count(*) FILTER(WHERE task='success') success,count(*) FILTER(WHERE task='failure') failure,count(*) FILTER(WHERE task='inconclusive') inconclusive,count(*) FILTER(WHERE task='unknown') unknown,count(*) FILTER(WHERE task='missing') missing,count(*) FILTER(WHERE task='unsupported') unsupported,
 count(*) FILTER(WHERE task<>'N/A') eligible,count(*) FILTER(WHERE task IN('success','failure')) decidable
 FROM classified GROUP BY bucket,dataset,suite,subject,subject_version,environment,run_mode,evaluator,evaluator_digest,metric,metric_refs,implementation)
 SELECT to_jsonb(summary)||jsonb_build_object('task_success_rate',success::double precision/nullif(decidable,0),'decision_coverage',decidable::double precision/nullif(eligible,0),'evaluation_coverage',completed::double precision/nullif(expected,0)) FROM summary ORDER BY bucket,subject,subject_version,evaluator LIMIT 2001`
	}
	analyticsSQL["gates"] = analyticsParams + `, facts AS(SELECT g.*,convert_from(receipt_bytes,'UTF8')::jsonb body FROM evaluation_gate_receipts g,args WHERE project_id=p AND created_at>=f AND created_at<u), summary AS(
 SELECT body->'policy_ref' policy,body->>'policy_digest' policy_digest,body->'Baseline'->'Ref'->>'kind' baseline_kind,body->'Candidate'->'Ref'->>'kind' candidate_kind,
 count(*) receipts,count(*) FILTER(WHERE decision='PASS') pass,count(*) FILTER(WHERE decision='FAIL') fail,count(*) FILTER(WHERE decision='BLOCKED') blocked,
 sum(jsonb_array_length(coalesce(body->'critical_regressions','[]'::jsonb))) critical_regressions,
 sum((SELECT count(*) FROM jsonb_array_elements(coalesce(body->'issues','[]'::jsonb)) i WHERE i->>'Code' LIKE '%COVERAGE%' OR i->>'Code' LIKE '%MISSING%')) coverage_blockers,
 count(*) FILTER(WHERE body->>'comparability'='INCOMPARABLE') incomparables
 FROM facts GROUP BY body->'policy_ref',body->>'policy_digest',body->'Baseline'->'Ref'->>'kind',body->'Candidate'->'Ref'->>'kind') SELECT to_jsonb(summary) FROM summary LIMIT 2001`
	analyticsSQL["online"] = analyticsParams + `, admissions AS(
 SELECT a.*,o.source,convert_from(a.selection_bytes,'UTF8')::jsonb selection FROM evaluation_online_admissions a JOIN evaluation_observations o ON o.project_id=a.project_id AND o.id=a.observation_id,args WHERE a.project_id=p AND o.completed_at>=f AND o.completed_at<u AND (rule='' OR concat_ws('@',a.rule_id,a.rule_version)=rule)
 ), population AS(SELECT rule_id,rule_version,source,count(*) FILTER(WHERE (selection->>'Eligible')::boolean) eligible,count(*) FILTER(WHERE (selection->>'Sampled')::boolean) sampled,count(*) FILTER(WHERE outcome='SKIPPED_BUDGET') admission_budget_skipped FROM admissions GROUP BY rule_id,rule_version,source), slots AS(
 SELECT a.rule_id,a.rule_version,a.source,w.evaluator_id,w.evaluator_version,b.metric_id,b.metric_version,count(*) work,count(r.id) completed,
 count(*) FILTER(WHERE convert_from(r.result_bytes,'UTF8')::jsonb->'Value'->>'Applicability'='APPLICABLE' AND convert_from(r.result_bytes,'UTF8')::jsonb->'Value'->>'Verdict' IN('PASS','FAIL')) decidable,
 count(*) FILTER(WHERE convert_from(r.result_bytes,'UTF8')::jsonb->'Value'->>'Applicability'='MISSING_EVIDENCE') missing,
 count(*) FILTER(WHERE convert_from(r.result_bytes,'UTF8')::jsonb->'Value'->>'Category'='SKIPPED_BUDGET') budget_skipped,
 count(*) FILTER(WHERE convert_from(r.result_bytes,'UTF8')::jsonb->'Value'->>'Category'='SKIPPED_RATE_LIMIT') rate_limited
 FROM admissions a JOIN evaluation_online_works w USING(project_id,rule_id,rule_version,observation_id) JOIN evaluation_online_bindings b USING(project_id,rule_id,rule_version,evaluator_id,evaluator_version) LEFT JOIN evaluation_online_results r ON r.project_id=w.project_id AND r.work_id=w.id
 GROUP BY a.rule_id,a.rule_version,a.source,w.evaluator_id,w.evaluator_version,b.metric_id,b.metric_version)
 SELECT to_jsonb(population)||jsonb_build_object('sampling_coverage',sampled::double precision/nullif(eligible,0),'sampling_population','SAMPLED_ONLY_NOT_POPULATION_ESTIMATE','sampling',convert_from(v.canonical_bytes,'UTF8')::jsonb->'Sampling','filter',convert_from(v.canonical_bytes,'UTF8')::jsonb->'Filter','segments',coalesce((SELECT jsonb_agg(to_jsonb(slots)||jsonb_build_object('evaluation_coverage',completed::double precision/nullif(work,0),'decision_coverage',decidable::double precision/nullif(work,0))) FROM slots WHERE slots.rule_id=population.rule_id AND slots.rule_version=population.rule_version AND slots.source=population.source),'[]'::jsonb)) FROM population JOIN evaluation_online_rule_versions v ON v.project_id=$1 AND v.entity_id=population.rule_id AND v.version=population.rule_version LIMIT 2001`
	analyticsSQL["human"] = analyticsParams + `, items AS(SELECT i.* FROM evaluation_review_items i,args WHERE project_id=p AND created_at>=f AND created_at<u), summary AS(
 SELECT count(*) eligible,(SELECT count(*) FROM evaluation_review_slots s JOIN items i ON i.project_id=s.project_id AND i.id=s.item_id WHERE s.status='CLAIMED' AND s.lease_expires_at>transaction_timestamp()) assigned,
 (SELECT count(*) FROM evaluation_review_slots s JOIN items i ON i.project_id=s.project_id AND i.id=s.item_id WHERE s.status='COMPLETED') submitted,
 (SELECT count(DISTINCT a.item_id) FROM evaluation_adjudications a JOIN items i ON i.project_id=a.project_id AND i.id=a.item_id) adjudicated,
 (SELECT count(DISTINCT g.item_id) FROM evaluation_golden_labels g JOIN items i ON i.project_id=g.project_id AND i.id=g.item_id WHERE i.status='COMPLETED' AND NOT EXISTS(SELECT 1 FROM evaluation_golden_annotation_inputs gi JOIN evaluation_human_annotations n ON n.project_id=gi.project_id AND n.supersedes=gi.annotation_id WHERE gi.project_id=g.project_id AND gi.golden_id=g.id) AND NOT EXISTS(SELECT 1 FROM evaluation_adjudications n WHERE n.project_id=g.project_id AND n.supersedes=g.adjudication_id)) golden FROM items)
 SELECT to_jsonb(summary)||jsonb_build_object('human_label_coverage',golden::double precision/nullif(eligible,0)) FROM summary
 UNION ALL SELECT jsonb_build_object('calibration_report_id',r.id,'evaluator',b->'Evaluator','protocol',b->'Protocol','agreement',b->'Agreement','dataset',b->'Dataset') FROM evaluation_calibration_reports r CROSS JOIN LATERAL(SELECT convert_from(r.report_bytes,'UTF8')::jsonb b) v,args WHERE r.project_id=p AND r.created_at>=f AND r.created_at<u LIMIT 2001`
}
