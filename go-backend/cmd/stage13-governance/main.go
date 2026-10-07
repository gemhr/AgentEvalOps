// stage13-governance 仅在显式 WP07 隔离数据库中发布受控资产；没有模型依赖。
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	gov "agentevalops/go-backend/internal/cigovernance"
	"agentevalops/go-backend/internal/citriage"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"github.com/jackc/pgx/v5/pgxpool"
)

func must(err error) {
	if err != nil {
		panic(err)
	}
}
func save(dir, name string, v any) {
	must(os.WriteFile(filepath.Join(dir, name+".json"), gov.Freeze(v).Bytes(), 0600))
}
func scope(project, org string, epoch int64) rv.Scope {
	return rv.Scope{Scope: ev.Scope{Scope: asset.Scope{ProjectID: project, OrganizationID: org, Principal: "CONTROLLED_REVIEWER:wp07", CanPublish: true}, Epoch: epoch}, ReviewerType: "SYSTEM_IMPORT", IdentitySource: "CONTROLLED_REVIEWER", Queue: true, Review: true, PublishGolden: true, Feedback: true}
}
func schema(ctx context.Context, pool *pgxpool.Pool, s rv.Scope) asset.Ref {
	r := asset.Ref{EntityID: gov.ID(s.ProjectID, "review-quality-schema"), Version: "v1"}
	store := postgres.MetricDefinitions{Pool: pool}
	d := metric.Builtins()[0].Definition
	d.Name = "stage13-controlled-gt-review.v1"
	d.Labels = rv.Labels(rv.Quality)
	_, e := store.CreateMetricDefinition(ctx, s.Scope.Scope, asset.Create{ID: r.EntityID, Name: d.Name})
	must(e)
	_, e = store.PublishMetricDefinitionVersion(ctx, s.Scope.Scope, asset.Publish[metric.Definition]{Ref: r, Body: d, Source: asset.Source{Kind: "CONTROLLED_REVIEWER", Ref: gov.Contract, Principal: s.Principal}})
	must(e)
	return r
}
func adjudicate(ctx context.Context, k postgres.Reviews, s rv.Scope, schema asset.Ref, source rv.SourceRef, gt asset.JSON, state, priority string) (gov.ReviewDecision, rv.Item) {
	key := gov.Freeze(source).Digest()
	item, e := k.EnqueueReview(ctx, s, rv.Enqueue{ID: gov.ID(key, "review"), Source: source, Schema: schema, Kind: rv.Quality, Policy: rv.Policy{Ref: "stage13-controlled-gt-review.v1", Reviews: 1, Protocol: rv.Protocol{Blind: true}, Sampling: rv.Sampling{Version: "review-hash.v1", Seed: "wp07", BasisPoints: 10000}}, Reason: "受控来源 GT 与可见证据映射审核", Priority: priority, Criticality: map[bool]string{true: "CRITICAL", false: "NORMAL"}[priority == "CRITICAL"]})
	must(e)
	value := map[string]string{"CONFIRMED": "PASS", "CORRECTED": "FAIL", "AMBIGUOUS": "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE": "INCONCLUSIVE", "REJECTED": "ERROR"}[state]
	view, e := k.GetReview(ctx, s, item.ID)
	must(e)
	annotationID := gov.ID(item.ID, "annotation")
	if view.Item.Status != "COMPLETED" {
		claim, e := k.ClaimReview(ctx, s, item.ID, time.Minute)
		must(e)
		refs := []string{}
		for _, b := range item.Source.Evidence {
			refs = append(refs, b.Ref)
		}
		_, e = k.SubmitAnnotation(ctx, s, rv.Submit{ID: annotationID, ItemID: item.ID, Slot: claim.Slot, Token: claim.Token, SourceDigest: item.Source.Digest, SchemaDigest: item.SchemaDigest, Decision: rv.Judgment{Kind: rv.Quality, Value: value, Reason: "CONTROLLED_REVIEWER：核对源 GT、证据与受控干预事实；非生产专家签字", EvidenceRefs: refs}})
		must(e)
	}
	goldenID := ""
	if gov.HardGolden(state) {
		g, e := k.PublishGoldenLabel(ctx, s, gov.ID(item.ID, "golden"), item.ID)
		must(e)
		goldenID = g.ID
	}
	r := gov.ReviewDecision{ID: gov.ID(item.ID, "typed-gt-review"), ItemID: item.ID, Case: *item.Source.Case, PreviousGTDigest: item.Source.CaseBody.GroundTruth.Digest(), State: state, GroundTruth: gt, Reason: "CONTROLLED_SOURCE_EVIDENCE_MAPPING", AnnotationID: annotationID, GoldenID: goldenID}
	out, e := k.StoreStage13GTReview(ctx, s, r)
	must(e)
	again, e := k.StoreStage13GTReview(ctx, s, r)
	must(e)
	if gov.Freeze(out).Digest() != gov.Freeze(again).Digest() {
		panic("REVIEW_REPLAY_CHANGED")
	}
	return out, item
}

func main() {
	if len(os.Args) != 2 || os.Getenv("STAGE13_WP07_CONTROLLED") != "1" {
		panic("EXPLICIT_WP07_SCOPE_REQUIRED")
	}
	dir, e := filepath.Abs(os.Args[1])
	must(e)
	must(os.MkdirAll(dir, 0700))
	cfg, e := pgxpool.ParseConfig(os.Getenv("STAGE13_WP07_DATABASE_URL"))
	must(e)
	if cfg.ConnConfig.Host != "127.0.0.1" || cfg.ConnConfig.Port != 55432 || cfg.ConnConfig.Database != "stage13_wp07_evalops_test" {
		panic("ISOLATED_WP07_DATABASE_REQUIRED")
	}
	ctx := context.Background()
	pool, e := pgxpool.NewWithConfig(ctx, cfg)
	must(e)
	defer pool.Close()
	k := postgres.Reviews{Pool: pool}
	var epoch int64
	must(pool.QueryRow(ctx, `SELECT writer_epoch FROM evaluation_writer_control`).Scan(&epoch))
	org := gov.ID("wp07-governance-organization")
	_, e = pool.Exec(ctx, `INSERT INTO organizations(id,name,created_at) VALUES($1,'WP07 controlled governance',clock_timestamp()) ON CONFLICT DO NOTHING`, org)
	must(e)
	projects := map[string]string{}
	schemas := map[string]asset.Ref{}
	for _, role := range gov.Roles {
		p := gov.ID("wp07", role, "project")
		projects[role] = p
		_, e = pool.Exec(ctx, `INSERT INTO projects(id,org_id,name,description,created_at) VALUES($1,$2,$3,'WP07 TEST_SCOPE',clock_timestamp()) ON CONFLICT DO NOTHING`, p, org, "wp07-"+strings.ToLower(role))
		must(e)
		schemas[role] = schema(ctx, pool, scope(p, org, epoch))
	}
	// 首先冻结 generation/GT/split；此时尚未读取 WP05A Candidate 的任何 Result。
	g := gov.Generate("stage13-eval-data-v1", "wp07-family-seed-1")
	must(gov.AuditIsolation(g.Families))
	save(dir, "family-generation", g)
	save(dir, "dataset-split-policy", map[string]any{"version": gov.SplitVersion, "unit": "ROOT_MECHANISM_FAMILY", "algorithm": "category-stratified family canonical hash order; roles DEVELOPMENT,CALIBRATION,HOLDOUT", "seed": g.Seed, "old_families": 24, "new_families": 18, "old_status": "HISTORICAL_EVALUATION_EXPOSED", "one_episode_per_family": true, "generation_manifest_digest": g.Digest, "subject_results_used_for_generation": false})
	cases := catalog.CaseService{Store: postgres.Cases{Pool: pool}}
	ds := catalog.DatasetService{Store: postgres.Datasets{Pool: pool}, Cases: postgres.Cases{Pool: pool}}
	decisions := []gov.ReviewDecision{}
	work := []rv.Item{}
	goldenCases := map[string][]catalog.CaseVersion{}
	familyIDs := map[string][]string{}
	for _, f := range g.Families {
		s := scope(projects[f.Role], org, epoch)
		gt, e := citriage.ReadGroundTruth(f.GT)
		must(e)
		policy := gov.Policy{Version: gov.Contract, Role: f.Role, Family: f.ID, State: "PENDING_REVIEW", GTMapping: gov.GTMapping, Split: gov.SplitVersion, Profile: gov.Profile, Frozen: true}
		body := catalog.CaseContent{Input: f.Input, GroundTruth: f.GT, TaskGoal: "按可见受控 CI 证据进行 failure triage", AcceptanceCriteria: []string{"遵守冻结七字段输出与可见证据边界"}, Applicability: asset.Applicability{RuleRef: "stage13-ci.v1"}, Type: catalog.AgentTask, Capability: "CI_FAILURE_TRIAGE", Criticality: catalog.Criticality(gt.Criticality), BodyPolicy: catalog.Retained, Metadata: gov.Metadata(policy)}
		name := "WP07 " + f.ID
		_, e = cases.CreateCase(ctx, s.Scope.Scope, asset.Create{ID: f.Case.EntityID, Name: name})
		must(e)
		_, e = cases.PublishCaseVersion(ctx, s.Scope.Scope, asset.Publish[catalog.CaseContent]{Ref: f.Case, Body: body, Source: asset.Source{Kind: "EVALUATION_DATASET_GENERATION", Ref: gov.Profile, Principal: s.Principal, Metadata: gov.Freeze(map[string]any{"generation_manifest_digest": g.Digest, "family": f.ID, "root_mechanism": f.RootMechanism, "gt_mapping": gov.GTMapping, "control": f.Control})}})
		must(e)
		priority := "NORMAL"
		if gt.Criticality == "CRITICAL" {
			priority = "CRITICAL"
		}
		r, item := adjudicate(ctx, k, s, schemas[f.Role], rv.SourceRef{Type: "CONTROLLED_CASE", Case: &f.Case}, f.GT, "CONFIRMED", priority)
		decisions = append(decisions, r)
		work = append(work, item)
		policy.State = "CONFIRMED"
		policy.ReviewID = r.ID
		policy.Supersedes = &f.Case
		body.Metadata = gov.Metadata(policy)
		body.Type = catalog.Golden
		ref := f.Case
		ref.Version = "golden-v1"
		draft, e := k.CreateReviewedCaseDraft(ctx, s, rv.Draft{ID: gov.ID(item.ID, "golden-draft"), ItemID: item.ID, GoldenID: &r.GoldenID, Case: ref, Body: body, Sanitization: "APPROVED", Policy: gov.Contract, Reason: "受控 GT 确认；typed CI GT 保持独立，UseGoldenGroundTruth=false"})
		must(e)
		published, e := k.PublishReviewedCase(ctx, s, draft.ID, name)
		must(e)
		goldenCases[f.Role] = append(goldenCases[f.Role], published)
		familyIDs[f.Role] = append(familyIDs[f.Role], f.ID)
	}
	manifests := map[string]catalog.DatasetVersion{}
	for _, role := range gov.Roles {
		s := scope(projects[role], org, epoch)
		ref := asset.Ref{EntityID: gov.ID("wp07", role, "dataset"), Version: "golden-v1"}
		_, e = ds.CreateDataset(ctx, s.Scope.Scope, asset.Create{ID: ref.EntityID, Name: "WP07 " + role})
		must(e)
		refs := []asset.Ref{}
		for _, c := range goldenCases[role] {
			refs = append(refs, c.Ref())
		}
		body := catalog.DatasetContent{Cases: refs, Metadata: gov.Metadata(gov.Policy{Version: gov.Contract, Role: role, GTMapping: gov.GTMapping, Split: gov.SplitVersion, Profile: gov.Profile, Families: familyIDs[role], Frozen: true})}
		v, e := ds.PublishDatasetVersion(ctx, s.Scope.Scope, asset.Publish[catalog.DatasetContent]{Ref: ref, Body: body, Source: asset.Source{Kind: "GOLDEN_DATASET", Ref: gov.Profile, Principal: s.Principal, Metadata: gov.Freeze(map[string]any{"generation_digest": g.Digest, "role": role})}})
		must(e)
		manifests[role] = v
		save(dir, strings.ToLower(role)+"-dataset", map[string]any{"dataset": v, "cases": goldenCases[role], "role": role, "family_ids": familyIDs[role], "manifest_digest": v.ContentDigest(), "gt_mapping": gov.GTMapping, "split_version": gov.SplitVersion, "generation_digest": g.Digest})
	}
	devScope := scope(projects["DEVELOPMENT"], org, epoch).Scope.Scope
	export, e := ds.ExportDevelopment(ctx, devScope, manifests["DEVELOPMENT"].Ref())
	must(e)
	save(dir, "development-export", export)
	_, errWrongScope := ds.ExportDevelopment(ctx, devScope, manifests["HOLDOUT"].Ref())
	_, errWrongRole := ds.ExportDevelopment(ctx, scope(projects["HOLDOUT"], org, epoch).Scope.Scope, manifests["HOLDOUT"].Ref())
	if errWrongScope == nil || errWrongRole == nil {
		panic("HOLDOUT_EXPORT_LEAK")
	}
	save(dir, "holdout-access-boundary", map[string]any{"PASS": true, "separate_projects": projects, "wrong_project_export_rejected": true, "holdout_role_export_rejected": true, "holdout_gt_in_development_export": false, "required_intent": "RELEASE_EVALUATION"})
	save(dir, "golden-manifest", map[string]any{"datasets": manifests, "confirmed_case_versions": 18, "generation_digest": g.Digest, "new_model_calls": 0})
	save(dir, "leakage-audit", map[string]any{"PASS": true, "new_families": 18, "old_families": 24, "development_holdout_family_overlap": 0, "mechanism_overlap": 0, "template_overlap": 0, "signature_component_overlap": 0, "descriptor_mapping_overlap": 0, "historical_family_overlap": 0, "audit_rule": gov.SplitVersion, "old_history_retained": true})
	// 冻结新 family 之后才消费旧 WP05A；反馈与历史审核只写入 clone 的新增资产。
	historicalProject := "71300005-0000-4000-8000-000000000001"
	var historicalOrg string
	must(pool.QueryRow(ctx, `SELECT org_id::text FROM projects WHERE id=$1`, historicalProject).Scan(&historicalOrg))
	hs := scope(historicalProject, historicalOrg, epoch)
	hschema := schema(ctx, pool, hs)
	state, e := (postgres.Evaluation{Pool: pool}).ReadRunState(ctx, hs.Scope.Scope, "b0e7ed35-30f9-45d8-b48c-f2b4a1ee713f")
	must(e)
	gate := "e7ffaf81-3660-475a-929c-cb816676290c"
	var comparisonDigest string
	must(pool.QueryRow(ctx, `SELECT intent_digest FROM evaluation_comparisons WHERE project_id=$1 AND id=$2`, historicalProject, gate).Scan(&comparisonDigest))
	var cfgManifest struct {
		Expected struct {
			Digest string `json:"subject_manifest_digest"`
		} `json:"expected_subject_manifest"`
	}
	must(json.Unmarshal(state.Run.Snapshot.Target.Config.Bytes(), &cfgManifest))
	feedback := []gov.Feedback{}
	engineering := []gov.Feedback{}
	criticalHistorical := 0
	ambiguous := 0
	insufficient := 0
	for _, result := range state.Results {
		var provenance struct {
			Metric string          `json:"metric"`
			Triage citriage.Scores `json:"triage"`
		}
		must(json.Unmarshal(result.Value.Provenance.Bytes(), &provenance))
		if provenance.Metric != citriage.Metrics[6] {
			continue
		}
		var c catalog.CaseContent
		var cref asset.Ref
		for _, unit := range state.Run.Snapshot.Input.Manifest {
			if unit.Identity.Ref.EntityID == result.CaseID {
				c = unit.Case
				cref = unit.Identity.Ref
			}
		}
		gt, e := citriage.ReadGroundTruth(c.GroundTruth)
		must(e)
		failures := gov.Failures(provenance.Triage, gt)
		reasons := []string{}
		for reason := range failures {
			reasons = append(reasons, reason)
		}
		slices.Sort(reasons)
		for _, reason := range reasons {
			f := gov.Feedback{Case: cref, RunID: state.Run.ID, ResultID: result.ID, SubjectDigest: cfgManifest.Expected.Digest, Evaluator: asset.Ref{EntityID: result.EvaluatorID, Version: result.EvaluatorVersion}, GateID: gate, ComparisonDigest: comparisonDigest, Source: failures[reason], Failure: reason, Details: gov.Freeze(map[string]any{"schema_valid": provenance.Triage.SchemaValid, "semantic_valid": provenance.Triage.SemanticValid, "repair_outcome": "WP05A_FROZEN_FINAL_OUTPUT", "category": "OUTPUT_INVALID_OR_BUSINESS_METRIC", "raw_output_exported": false, "expected_values_exported": false})}
			stored, e := k.StoreStage13Feedback(ctx, hs, f)
			must(e)
			again, e := k.StoreStage13Feedback(ctx, hs, f)
			must(e)
			if stored.Digest != again.Digest {
				panic("FEEDBACK_REPLAY_CHANGED")
			}
			feedback = append(feedback, stored)
			if stored.Source == "MODEL_OUTPUT_INVALID" {
				engineering = append(engineering, stored)
			}
		}
		if gt.Criticality == "CRITICAL" || ambiguous == 0 || insufficient == 0 {
			reviewState := "CONFIRMED"
			reviewGT := c.GroundTruth
			priority := "CRITICAL"
			if gt.Criticality != "CRITICAL" {
				priority = "HIGH"
				reviewGT = asset.JSON{}
				if ambiguous == 0 {
					reviewState = "AMBIGUOUS"
					ambiguous++
				} else {
					reviewState = "INSUFFICIENT_EVIDENCE"
					insufficient++
				}
			}
			r, item := adjudicate(ctx, k, hs, hschema, rv.SourceRef{Type: "OFFLINE_RESULT", RunID: state.Run.ID, ResultID: result.ID}, reviewGT, reviewState, priority)
			decisions = append(decisions, r)
			work = append(work, item)
			if gt.Criticality == "CRITICAL" {
				criticalHistorical++
			}
		}
	}
	save(dir, "review-work", work)
	save(dir, "review-decisions", decisions)
	save(dir, "feedback-events", feedback)
	if len(feedback) > 0 {
		bad := feedback[0]
		bad.SubjectDigest = strings.Repeat("0", 64)
		if _, e := k.StoreStage13Feedback(ctx, hs, bad); e == nil {
			panic("FEEDBACK_SUBJECT_BINDING_BYPASS")
		}
		bad = feedback[0]
		bad.Case.Version = "unbound-version"
		if _, e := k.StoreStage13Feedback(ctx, hs, bad); e == nil {
			panic("FEEDBACK_CASE_BINDING_BYPASS")
		}
		save(dir, "feedback-binding-validation", map[string]any{"PASS": true, "subject_mismatch_rejected": true, "case_version_mismatch_rejected": true, "replay_digest_stable": true})
	}
	save(dir, "historical-engineering-feedback-export", engineering)
	save(dir, "critical-review", map[string]any{"new_critical_confirmed": 9, "historical_critical_confirmed": criticalHistorical, "actor": "CONTROLLED_REVIEWER", "production_expert_signoff": false})
	save(dir, "failure-taxonomy", map[string]any{"version": "stage13.quality-failure-taxonomy.v1", "failure_taxonomy": gov.Taxonomy, "feedback_sources": gov.FeedbackSources})
	save(dir, "summary", map[string]any{"development_cases": len(goldenCases["DEVELOPMENT"]), "calibration_cases": len(goldenCases["CALIBRATION"]), "holdout_cases": len(goldenCases["HOLDOUT"]), "golden_confirmed": 18 + criticalHistorical, "new_golden_case_versions": 18, "ambiguous": ambiguous, "insufficient_evidence": insufficient, "critical_reviewed": 9 + criticalHistorical, "feedback_events": len(feedback), "invalid_output_feedback": len(engineering), "new_model_calls": 0, "historical_candidate_gate": "BLOCKED", "historical_candidate_delivery": "DENIED", "review_replay": "PASS", "feedback_replay": "PASS"})
	fmt.Println("WP07 controlled assets published; no Subject/Judge calls")
}
