// stage13-wp11 只装配正式 Release owners；普通输出限定为元数据与聚合结果。
package main

import (
	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	gov "agentevalops/go-backend/internal/cigovernance"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"context"
	"encoding/json"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	"log/slog"
	"os"
	"path/filepath"
	"time"
)

const holdoutProject = "7134c30f-894a-5e26-b782-bb8b996596eb"
const candidateDigest = "4694175c673fe40607808d9c9b67bb3ca06fa6fcb2b81eebfeb943b3facb8aab"
const datasetDigest = "80fa0b7790bca519cd43afecfcf6b2cddcfaa3c51ddfe2720ed5b987fb00b034"

var datasetRef = asset.Ref{EntityID: "e7a70b04-0712-58bc-a0c8-655eac0aa463", Version: "golden-v2"}

func must(e error) {
	if e != nil {
		panic(e)
	}
}
func freeze(v any) asset.JSON { j, e := asset.Freeze(v); must(e); return j }
func load(p string, v any)    { b, e := os.ReadFile(p); must(e); must(json.Unmarshal(b, v)) }
func save(dir, name string, v any) {
	must(os.WriteFile(filepath.Join(dir, name+".json"), freeze(v).Bytes(), 0600))
}
func requireV2(p decision.Policy) error {
	if p.Stage13ReleaseVersion != decision.Stage13ReleaseV2 {
		return asset.ErrForbidden
	}
	return p.Validate()
}
func verifyMetadata(m catalog.HoldoutVerification) error {
	if m.Dataset != datasetRef || m.ManifestDigest != datasetDigest || m.CaseCount != 6 || m.CriticalCount != 3 || m.NormalCount != 3 || m.Role != "HOLDOUT" || m.IntegrityStatus != "PASS" {
		return asset.ErrInvalid
	}
	return nil
}

type targets map[string]*provider.Stage13Target

func (t targets) Execute(ctx context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	if s.ProjectID != holdoutProject {
		return ev.Outcome{}, asset.ErrForbidden
	}
	if p := t[q.Target.Config.Digest()]; p != nil {
		return p.Execute(ctx, s, q)
	}
	return ev.Outcome{}, asset.ErrForbidden
}

func main() {
	if len(os.Args) != 3 || os.Getenv("STAGE13_WP11_CONTROLLED") != "1" {
		panic("OPERATOR_ONLY")
	}
	out, e := filepath.Abs(os.Args[1])
	must(e)
	cfg, e := pgxpool.ParseConfig(os.Getenv("STAGE13_WP11_DATABASE_URL"))
	must(e)
	if cfg.ConnConfig.Host != "127.0.0.1" || cfg.ConnConfig.Port != 55432 || cfg.ConnConfig.Database != "stage13_wp11_evalops_test" {
		panic("ISOLATED_WP11_REQUIRED")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 7200*time.Second)
	defer cancel()
	pool, e := pgxpool.NewWithConfig(ctx, cfg)
	must(e)
	defer pool.Close()
	if os.Args[2] == "--metadata-api" {
		metadataAPI(ctx, pool)
		return
	}
	if os.Args[2] != "--execute" {
		panic("UNKNOWN_MODE")
	}
	op := filepath.Join(out, "operator-only")
	must(os.MkdirAll(op, 0700))
	k := postgres.Evaluation{Pool: pool}
	epoch, e := k.VerifyWorker(ctx, gov.Schema)
	must(e)
	var org string
	must(pool.QueryRow(ctx, "SELECT org_id::text FROM projects WHERE id=$1", holdoutProject).Scan(&org))
	s := ev.Scope{Scope: asset.Scope{ProjectID: holdoutProject, OrganizationID: org, Principal: "system:stage13-wp11-release", CanPublish: true}, Epoch: epoch, Create: true, Execute: true, Evaluate: true, Coordinate: true, Repair: true}
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: pool}, Datasets: postgres.Datasets{Pool: pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: pool}}
	ds := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	meta, e := ds.VerifyHoldoutMetadata(ctx, s.Scope, datasetRef)
	must(e)
	must(verifyMetadata(meta))
	var plan provider.Stage13PolicyDocument
	load(filepath.Join(out, "release-plan.json"), &plan)
	identity, e := gov.ReleaseSubject(freeze(map[string]any{"expected_subject_manifest": plan.Candidate.Manifest}))
	must(e)
	if identity.ID != "ci_triage_candidate" || identity.Version != "wp10b-flash-candidate-3" || identity.Digest != candidateDigest {
		panic("FROZEN_CANDIDATE_MISMATCH")
	}
	baseline, e := gov.ReleaseSubject(freeze(map[string]any{"expected_subject_manifest": plan.Baseline.Manifest}))
	must(e)
	if baseline.ID != "ci_triage_baseline" || baseline.Version != "wp04b-flash-baseline-1" {
		panic("FROZEN_BASELINE_MISMATCH")
	}
	var historical ev.RunState
	load(filepath.Join(op, "historical-baseline-state.json"), &historical)
	owner := postgres.Decisions{Pool: pool}
	var historicalOrg string
	must(pool.QueryRow(ctx, "SELECT org_id::text FROM projects WHERE id=$1", historical.Run.ProjectID).Scan(&historicalOrg))
	hs := asset.Scope{ProjectID: historical.Run.ProjectID, OrganizationID: historicalOrg, Principal: s.Principal}
	ps := s.Scope
	ps.ProjectID = "ca57dddb-cc22-5272-8f81-8d56b73ba640"
	must(pool.QueryRow(ctx, "SELECT org_id::text FROM projects WHERE id=$1", ps.ProjectID).Scan(&ps.OrganizationID))
	original, e := owner.GetPolicyVersion(ctx, ps, asset.Ref{EntityID: "4445efa4-98b2-510b-bd8c-6a536168b422", Version: "v2"})
	must(e)
	if original.ContentDigest() != "7f682a3b5c67b29fd1d18be71a6d2a52094cc3c83f21e9dc6683e84e369ec448" {
		panic("FROZEN_V2_POLICY_MISMATCH")
	}
	policy := original.Content().Body
	must(requireV2(policy))
	ms := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
	es := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	source := asset.Source{Kind: "CONTROLLED_RELEASE_BINDING", Ref: "stage13.wp11-release.v1", Principal: s.Principal}
	bindings := []catalog.EvaluatorBinding{}
	metricBodies := map[string]asset.Ref{}
	for _, spec := range historical.Run.Snapshot.Input.Evaluators {
		if !agentquality.DeterministicSupported(spec.Definition) {
			panic("JUDGE_FORBIDDEN")
		}
		def := spec.Definition
		refs := []asset.Ref{}
		for _, old := range def.OutputMetrics {
			previous, e := readers.MetricDefinitions.GetMetricDefinitionVersion(ctx, hs, old)
			must(e)
			r := asset.Ref{EntityID: gov.ID("wp11-metric", holdoutProject, old.EntityID), Version: old.Version}
			_, e = ms.CreateMetricDefinition(ctx, s.Scope, asset.Create{ID: r.EntityID, Name: previous.Content().Body.Name})
			must(e)
			_, e = ms.PublishMetricDefinitionVersion(ctx, s.Scope, asset.Publish[metric.Definition]{Ref: r, Body: previous.Content().Body, Source: source})
			must(e)
			refs = append(refs, r)
			metricBodies[freeze(previous.Content().Body).Digest()] = r
		}
		def.OutputMetrics = refs
		r := asset.Ref{EntityID: gov.ID("wp11-evaluator", holdoutProject, spec.Identity.Ref.EntityID), Version: spec.Identity.Ref.Version}
		_, e = es.CreateEvaluatorDefinition(ctx, s.Scope, asset.Create{ID: r.EntityID, Name: "WP11 deterministic " + def.Config.String()})
		must(e)
		_, e = es.PublishEvaluatorDefinitionVersion(ctx, s.Scope, asset.Publish[metric.EvaluatorDefinition]{Ref: r, Body: def, Source: source})
		must(e)
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: r, Metrics: refs, Required: spec.Required, Applicability: spec.Applicability})
	}
	k.Capabilities, e = k.LoadEvaluatorCapabilities(ctx, agentquality.DeterministicSupported)
	must(e)
	transport := provider.DefaultLocalAgentConfig(os.Getenv("STAGE13_WP11_LOCALAGENT_BASE_URL"), "STAGE13_WP11_LOCALAGENT_SERVICE_TOKEN")
	transport.CallMilliseconds = 180000
	// 恢复时必须复用初次冻结的 transport/config，而不是改变已有 Run intent。
	configs := []provider.Stage13Config{{Transport: transport, ExpectedSubjectManifest: plan.Baseline.Manifest, ExecutionPolicyVersion: provider.Stage13ExecutionPolicyVersion}, {Transport: transport, ExpectedSubjectManifest: plan.Candidate.Manifest, ExecutionPolicyVersion: provider.Stage13ExecutionPolicyVersion}}
	for i := range policy.Metrics {
		previous, err := readers.MetricDefinitions.GetMetricDefinitionVersion(ctx, ps, policy.Metrics[i].Metric)
		must(err)
		ref, ok := metricBodies[freeze(previous.Content().Body).Digest()]
		if !ok {
			panic("FROZEN_METRIC_MAPPING_MISMATCH")
		}
		policy.Metrics[i].Metric = ref
	}
	if len(policy.AcceptedDifferences) != 1 || policy.AcceptedDifferences[0].Dimension != "target_config" {
		panic("FROZEN_POLICY_UNEXPECTED")
	}
	policy.AcceptedDifferences[0].Baseline = targetProofDigest(configs[0])
	policy.AcceptedDifferences[0].Candidate = targetProofDigest(configs[1])
	policy.AcceptedDifferences[0].Proof = "stage13.model-comparability.v2:" + freeze(plan).Digest()
	policy.AcceptedDifferences[0].Actor = s.Principal
	must(requireV2(policy))
	pr := asset.Ref{EntityID: gov.ID("wp11-policy", holdoutProject, candidateDigest), Version: "v2"}
	_, e = owner.CreatePolicy(ctx, s.Scope, asset.Create{ID: pr.EntityID, Name: "WP11 frozen v2 binding"})
	must(e)
	pv, e := owner.PublishPolicyVersion(ctx, s.Scope, asset.Publish[decision.Policy]{Ref: pr, Body: policy, Source: source})
	must(e)
	save(out, "policy-binding", map[string]any{"policy_ref": pr, "policy_digest": pv.ContentDigest(), "policy": policy, "source_policy_ref": original.Ref(), "source_policy_digest": original.ContentDigest(), "rules_changed": false})
	snapshot, e := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, s.Scope, ev.BuildRunSnapshot{Dataset: &datasetRef, Evaluators: bindings})
	must(e)
	before := snapshot.Input()
	save(op, "frozen-release-input", before)
	ids := []string{}
	states := []ev.RunState{}
	commands := []ev.CreateRun{}
	router := targets{}
	for i, cfg := range configs {
		p, e := provider.NewStage13Target(cfg)
		must(e)
		p.ReserveExecution = k.ReserveStage13Execution
		router[freeze(cfg).Digest()] = p
		defer p.Close()
		var m map[string]asset.JSON
		must(cfg.ExpectedSubjectManifest.Decode(&m))
		cmd := ev.CreateRun{Intent: "RELEASE_EVALUATION", CommandID: gov.ID("wp11-command", fmt.Sprint(i)), Snapshot: snapshot, Target: ev.Target{ID: provider.Stage13TargetID, Kind: "LOCALAGENT_HTTP", Version: provider.Stage13TargetVersion, Config: freeze(cfg), TimeoutMilliseconds: 180000}, Subject: freeze(map[string]any{"subject": "ci-failure-triage", "subject_version": m["subject_version"], "environment": "controlled-local", "run_mode": "AGENT_REGRESSION", "dataset_usage": "HOLDOUT"}), Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}
		save(op, fmt.Sprintf("creation-command-%d", i), cmd)
		reply, e := k.CreateRun(ctx, s, cmd)
		must(e)
		if reply.Code != ev.Applied && reply.Code != ev.AlreadyApplied {
			panic("RELEASE_CREATION_REJECTED")
		}
		ids = append(ids, reply.ID)
		commands = append(commands, cmd)
		st, e := k.ReadRunState(ctx, s.Scope, reply.ID)
		must(e)
		states = append(states, st)
		if i == 1 {
			saveConsumption(ctx, out, pool, reply.ID, "", "")
		}
	}
	save(out, "main-run-ids", ids)
	order := []string{}
	for _, c := range before.Manifest {
		for _, st := range states {
			for _, a := range st.Attempts {
				if a.CaseID == c.Identity.Ref.EntityID {
					order = append(order, a.ID)
				}
			}
		}
	}
	save(op, "deterministic-schedule", order)
	backend := &pairedBackend{Evaluation: k, order: order}
	wc := worker.DefaultConfig()
	wc.WorkerID = "system:wp11-release"
	wc.ExecutionConcurrency = 1
	wc.EvaluatorConcurrency = 1
	wc.OnlineConcurrency = 0
	wc.ScanBatch = 1
	wc.PollInterval = 100 * time.Millisecond
	runtime := worker.Runtime{Config: wc, Backend: backend, Target: router, Evaluator: agentquality.DeterministicEvaluator{}, Epoch: epoch, Log: slog.Default()}
	terminalBefore := true
	for _, st := range states {
		terminalBefore = terminalBefore && st.Run.Status.Terminal()
	}
	if !terminalBefore {
		runctx, stop := context.WithCancel(ctx)
		done := make(chan error, 1)
		go func() { done <- runtime.Run(runctx) }()
		last := -1
		for {
			terminal := true
			n := 0
			states = nil
			for _, id := range ids {
				st, e := k.ReadRunState(ctx, s.Scope, id)
				must(e)
				states = append(states, st)
				terminal = terminal && st.Run.Status.Terminal()
				for _, a := range st.Attempts {
					if a.Status == "TERMINAL" {
						n++
					}
				}
			}
			if n != last {
				fmt.Printf("RELEASE attempts=%d/12\n", n)
				last = n
			}
			if terminal {
				break
			}
			select {
			case <-ctx.Done():
				stop()
				must(<-done)
				panic("RUN_OVERALL_DEADLINE")
			case <-time.After(2 * time.Second):
			}
		}
		stop()
		must(<-done)
	}
	if !terminalBefore {
		save(op, "scheduler", map[string]any{"order": order, "claims": backend.claims, "execution_concurrency": 1, "evaluator_concurrency": 1})
	}
	for i, st := range states {
		save(op, []string{"baseline-run-state", "candidate-run-state"}[i], st)
	}
	finish(ctx, out, owner, s, plan, states, ids, pr)
	var gate decision.Receipt
	load(filepath.Join(op, "release-gate-full.json"), &gate)
	saveConsumption(ctx, out, pool, ids[1], gate.GateID, string(gate.Decision))
	replays := []ev.Reply{}
	for _, cmd := range commands {
		cmd.CommandID = asset.NewID()
		r, e := k.CreateRun(ctx, s, cmd)
		must(e)
		if r.Code != ev.AlreadyApplied {
			panic("DUPLICATE_RELEASE_CREATED")
		}
		replays = append(replays, r)
	}
	save(out, "consumption-idempotency", map[string]any{"PASS": true, "replies": replays, "new_attempts": 0, "new_model_calls": 0})
	projectAuthorization(ctx, out, owner, s, gate, states[1])
	after, e := (ev.Builder{Assets: readers}).BuildRunSnapshot(ctx, s.Scope, ev.BuildRunSnapshot{Dataset: &datasetRef, Evaluators: bindings})
	must(e)
	if freeze(before).Digest() != freeze(after.Input()).Digest() {
		panic("DATASET_MUTATED")
	}
	save(out, "dataset-integrity-before-after", map[string]any{"status": "UNCHANGED", "manifest_digest": datasetDigest, "dataset_case_gt_review_family_snapshot_digest_before": freeze(before).Digest(), "dataset_case_gt_review_family_snapshot_digest_after": freeze(after.Input()).Digest()})
}

func targetProofDigest(cfg provider.Stage13Config) string {
	cfg.ExecutionPolicyVersion = ""
	return freeze(cfg).Digest()
}
func saveConsumption(ctx context.Context, out string, pool *pgxpool.Pool, run, gate, d string) {
	var created time.Time
	var metadata []byte
	must(pool.QueryRow(ctx, "SELECT created_at,metadata FROM evaluation_runs WHERE id=$1", run).Scan(&created, &metadata))
	save(out, "holdout-consumption", map[string]any{"version": gov.HoldoutConsumption, "state": "CONSUMED", "holdout_dataset_version": datasetRef, "candidate_subject_manifest_digest": candidateDigest, "release_evaluation_run": run, "consumed_at": created, "gate_id": gate, "gate_decision": d, "run_metadata": json.RawMessage(metadata)})
}
