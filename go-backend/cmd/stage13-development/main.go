// stage13-development 仅为 WP08 operator 装配正式 HTTP API 与既有 Worker owners。
package main

import (
	"agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	gov "agentevalops/go-backend/internal/cigovernance"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/httpapi"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
	"agentevalops/go-backend/internal/provider"
	"agentevalops/go-backend/internal/worker"
	"context"
	"encoding/json"
	"fmt"
	"github.com/jackc/pgx/v5/pgxpool"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"
)

const developmentProject = "6a9bffe4-98fa-5def-a6f5-28f66b64e09a"

func must(err error) {
	if err != nil {
		panic(err)
	}
}
func freeze(v any) asset.JSON { j, e := asset.Freeze(v); must(e); return j }

type target struct{ kernel postgres.Evaluation }

func (t target) Execute(ctx context.Context, s ev.Scope, q ev.Request) (ev.Outcome, error) {
	if s.ProjectID != developmentProject {
		return ev.Outcome{}, asset.ErrForbidden
	}
	var cfg provider.Stage13Config
	if q.Target.Config.Decode(&cfg) != nil {
		return ev.Outcome{}, asset.ErrInvalid
	}
	if cfg.Transport.BaseURL != os.Getenv("WP08_OPERATOR_LOCALAGENT_BASE_URL") {
		return ev.Outcome{}, asset.ErrForbidden
	}
	p, e := provider.NewStage13Target(cfg)
	if e != nil {
		return ev.Outcome{}, e
	}
	defer p.Close()
	p.ReserveExecution = t.kernel.ReserveStage13Execution
	return p.Execute(ctx, s, q)
}

func main() {
	if len(os.Args) == 3 && os.Args[1] == "compare" {
		body, err := os.ReadFile(os.Args[2])
		must(err)
		var docs []provider.Stage13PolicyDocument
		must(json.Unmarshal(body, &docs))
		results := make([]provider.Stage13PairDecision, 0, len(docs))
		for _, doc := range docs {
			results = append(results, provider.CompareStage13Models(doc))
		}
		must(json.NewEncoder(os.Stdout).Encode(results))
		return
	}
	if os.Getenv("WP08_OPERATOR_BOOTSTRAP") != "1" || len(os.Args) != 2 {
		panic("OPERATOR_ONLY")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	cfg, e := pgxpool.ParseConfig(os.Getenv("WP08_OPERATOR_DATABASE_URL"))
	must(e)
	if cfg.ConnConfig.Host != "127.0.0.1" || cfg.ConnConfig.Database != "stage13_wp08_evalops_test" {
		panic("ISOLATED_WP08_REQUIRED")
	}
	pool, e := pgxpool.NewWithConfig(ctx, cfg)
	must(e)
	defer pool.Close()
	k := postgres.Evaluation{Pool: pool}
	epoch, e := k.VerifyWorker(ctx, gov.Schema)
	must(e)
	var org string
	must(pool.QueryRow(ctx, "SELECT org_id::text FROM projects WHERE id=$1", developmentProject).Scan(&org))
	scope := asset.Scope{ProjectID: developmentProject, OrganizationID: org, Principal: "system:wp08-operator", CanPublish: true}
	// 按冻结定义生成同一 evaluator 实现的新项目内引用，公式、denominator 与阈值不改。
	readers := postgres.PublishedAssets{Cases: postgres.Cases{Pool: pool}, Datasets: postgres.Datasets{Pool: pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: pool}}
	ms := metric.MetricDefinitionService{Store: readers.MetricDefinitions}
	es := metric.EvaluatorDefinitionService{Store: readers.EvaluatorDefinitions, Metrics: readers}
	var old struct {
		Run struct {
			Snapshot struct {
				Input ev.SnapshotInput `json:"Input"`
			} `json:"kernel_snapshot"`
		} `json:"Run"`
	}
	raw, e := os.ReadFile(filepath.Join(os.Args[1], "historical-baseline-state.json"))
	must(e)
	must(json.Unmarshal(raw, &old))
	source := asset.Source{Kind: "CONTROLLED_SETUP", Ref: "stage13.wp08-development.v1", Principal: scope.Principal}
	bindings := []catalog.EvaluatorBinding{}
	for _, spec := range old.Run.Snapshot.Input.Evaluators {
		if !agentquality.DeterministicSupported(spec.Definition) {
			panic("NON_DETERMINISTIC_EVALUATOR")
		}
		def := spec.Definition
		refs := []asset.Ref{}
		for _, metricRef := range def.OutputMetrics {
			historicalScope := asset.Scope{ProjectID: "71300005-0000-4000-8000-000000000001", OrganizationID: "71300005-0000-4000-8000-000000000002", Principal: scope.Principal}
			previous, e := readers.MetricDefinitions.GetMetricDefinitionVersion(ctx, historicalScope, metricRef)
			must(e)
			r := asset.Ref{EntityID: asset.NewID(), Version: "wp08-v1"}
			_, e = ms.CreateMetricDefinition(ctx, scope, asset.Create{ID: r.EntityID, Name: previous.Content().Body.Name})
			must(e)
			_, e = ms.PublishMetricDefinitionVersion(ctx, scope, asset.Publish[metric.Definition]{Ref: r, Body: previous.Content().Body, Source: source})
			must(e)
			refs = append(refs, r)
		}
		def.OutputMetrics = refs
		r := asset.Ref{EntityID: asset.NewID(), Version: "wp08-v1"}
		_, e = es.CreateEvaluatorDefinition(ctx, scope, asset.Create{ID: r.EntityID, Name: "WP08 " + spec.Definition.Config.String()})
		must(e)
		_, e = es.PublishEvaluatorDefinitionVersion(ctx, scope, asset.Publish[metric.EvaluatorDefinition]{Ref: r, Body: def, Source: source})
		must(e)
		bindings = append(bindings, catalog.EvaluatorBinding{Evaluator: r, Metrics: refs, Required: true, Applicability: def.Applicability})
	}
	// 已曝光历史输入复制为独立开发资产；原 Case/Dataset/Result 不改。
	var hist struct {
		Cases []struct {
			Ref     asset.Ref                          `json:"ref"`
			Content asset.Content[catalog.CaseContent] `json:"content"`
		} `json:"cases"`
	}
	raw, e = os.ReadFile(filepath.Join(os.Args[1], "historical-dataset.json"))
	must(e)
	must(json.Unmarshal(raw, &hist))
	cs := catalog.CaseService{Store: readers.Cases}
	ds := catalog.DatasetService{Store: readers.Datasets, Cases: readers}
	caseRefs := []asset.Ref{}
	mapping := map[string]string{}
	for _, c := range hist.Cases {
		ref := asset.Ref{EntityID: asset.NewID(), Version: "wp08-exposed-v1"}
		body := c.Content.Body
		_, e = cs.CreateCase(ctx, scope, asset.Create{ID: ref.EntityID, Name: "EXPOSED_SET " + c.Ref.EntityID})
		must(e)
		_, e = cs.PublishCaseVersion(ctx, scope, asset.Publish[catalog.CaseContent]{Ref: ref, Body: body, Source: source})
		must(e)
		caseRefs = append(caseRefs, ref)
		mapping[ref.EntityID] = c.Ref.EntityID
	}
	historicalDataset := asset.Ref{EntityID: asset.NewID(), Version: "wp08-exposed-v1"}
	_, e = ds.CreateDataset(ctx, scope, asset.Create{ID: historicalDataset.EntityID, Name: "WP08 EXPOSED_SET"})
	must(e)
	_, e = ds.PublishDatasetVersion(ctx, scope, asset.Publish[catalog.DatasetContent]{Ref: historicalDataset, Body: catalog.DatasetContent{Cases: caseRefs, Metadata: freeze(map[string]any{"usage": "EXPOSED_SET", "generalization_proof": false})}, Source: source})
	must(e)
	ident := postgres.ProductIdentity{Pool: pool, Pepper: []byte(os.Getenv("PRODUCT_API_PEPPER"))}
	caps := []identity.Capability{identity.Read, identity.Execute}
	access := identity.Access{Scope: scope, Principal: identity.Principal{ID: scope.Principal, Capabilities: append(caps, identity.ManageAPIKey)}}
	credential, key, e := ident.CreateCredential(ctx, access, asset.NewID(), "WP08 Development-only", caps, nil)
	must(e)
	k.Capabilities, e = k.LoadEvaluatorCapabilities(ctx, agentquality.DeterministicSupported)
	must(e)
	apiPoolCfg, e := pgxpool.ParseConfig(os.Getenv("WP08_OPERATOR_API_DATABASE_URL"))
	must(e)
	apiPool, e := pgxpool.NewWithConfig(ctx, apiPoolCfg)
	must(e)
	defer apiPool.Close()
	config := httpapi.DefaultConfig()
	config.RequestsPerMinute = 600
	api, e := httpapi.New(httpapi.Server{Pool: apiPool, Identity: postgres.ProductIdentity{Pool: apiPool, Pepper: ident.Pepper}, Config: config, Epoch: epoch, Kernel: postgres.Evaluation{Pool: apiPool}, SupportsEvaluator: agentquality.DeterministicSupported})
	must(e)
	_, e = api.Identity.VerifyAPI(ctx)
	must(e)
	listener, e := net.Listen("tcp", "127.0.0.1:0")
	must(e)
	wc := worker.DefaultConfig()
	wc.WorkerID = "system:wp08-development"
	wc.ExecutionConcurrency = 1
	wc.EvaluatorConcurrency = 1
	wc.OnlineConcurrency = 0
	wc.ScanBatch = 1
	wc.PollInterval = 100 * time.Millisecond
	runtime := worker.Runtime{Config: wc, Backend: k, Target: target{k}, Evaluator: agentquality.DeterministicEvaluator{}, Epoch: epoch, Log: slog.Default()}
	// stdout 是 operator 内部匿名管道，含 secret 的 ready 消息不得写入日志或 evidence。
	ready := map[string]any{"base_url": "http://" + listener.Addr().String(), "api_key": key, "credential_id": credential.ID, "project_id": developmentProject, "evaluators": bindings, "historical_dataset": historicalDataset, "historical_case_mapping": mapping}
	encoded, _ := json.Marshal(ready)
	fmt.Println(string(encoded))
	done := make(chan error, 1)
	go func() { done <- runtime.Run(ctx) }()
	must(httpapi.Serve(ctx, listener, api.Handler(), config.ShutdownTimeout))
	must(<-done)
}
