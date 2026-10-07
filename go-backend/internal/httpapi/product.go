package httpapi

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/experiment"
	"agentevalops/go-backend/internal/identity"

	ob "agentevalops/go-backend/internal/observation"
	"agentevalops/go-backend/internal/postgres"
	"encoding/json"
	"time"
)

func (s *Server) assets() postgres.PublishedAssets {
	return postgres.PublishedAssets{Cases: postgres.Cases{Pool: s.Pool}, Datasets: postgres.Datasets{Pool: s.Pool}, Suites: postgres.Suites{Pool: s.Pool}, MetricDefinitions: postgres.MetricDefinitions{Pool: s.Pool}, EvaluatorDefinitions: postgres.EvaluatorDefinitions{Pool: s.Pool}}
}

// 使用服务器刚读取的冻结定义构建请求局部能力，不缓存目录发布状态。
func (s *Server) kernelFor(input ev.SnapshotInput) postgres.Evaluation {
	k := s.Kernel
	if s.SupportsEvaluator != nil {
		k.Capabilities = ev.EvaluatorExecutionCapability{}
		for _, spec := range input.Evaluators {
			if s.SupportsEvaluator(spec.Definition) {
				k.Capabilities.Bindings = append(k.Capabilities.Bindings, ev.Capability{Evaluator: spec.Identity.Ref, ImplementationRef: spec.Definition.ImplementationRef, DefinitionDigest: spec.Identity.ContentDigest})
			}
		}
	}
	return k
}

type targetRequest struct {
	ID           string     `json:"id"`
	Kind         string     `json:"kind"`
	Version      string     `json:"version"`
	Config       asset.JSON `json:"config"`
	Capabilities asset.JSON `json:"capabilities"`
	Timeout      int64      `json:"timeout_milliseconds"`
}
type runRequest struct {
	Intent     string                     `json:"intent,omitempty"`
	Suite      *asset.Ref                 `json:"suite"`
	Dataset    *asset.Ref                 `json:"dataset"`
	Cases      []asset.Ref                `json:"selected_cases"`
	Evaluators []catalog.EvaluatorBinding `json:"evaluators"`
	Target     targetRequest              `json:"target"`
	Subject    asset.JSON                 `json:"subject"`
	Retry      ev.RetryPolicy             `json:"retry"`
}

func (r *request) runCommand(d runRequest) (ev.CreateRun, error) {
	snapshot, e := (ev.Builder{Assets: r.server.assets()}).BuildRunSnapshot(r.ctx(), r.scope(), ev.BuildRunSnapshot{Suite: d.Suite, Dataset: d.Dataset, SelectedCases: d.Cases, Evaluators: d.Evaluators})
	if e != nil {
		return ev.CreateRun{}, e
	}
	if d.Retry.Version == "" {
		d.Retry = ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}
	}
	return ev.CreateRun{Intent: d.Intent, CommandID: r.CommandID, Snapshot: snapshot, Target: ev.Target{ID: d.Target.ID, Kind: d.Target.Kind, Version: d.Target.Version, Config: d.Target.Config, Capabilities: d.Target.Capabilities, TimeoutMilliseconds: d.Target.Timeout}, Subject: d.Subject, Retry: d.Retry}, nil
}

type experimentRequest struct {
	Name      string               `json:"name"`
	Candidate asset.JSON           `json:"candidate"`
	Baseline  *experiment.Baseline `json:"baseline"`
	Repeat    int                  `json:"repeat"`
	Run       runRequest           `json:"run"`
}
type gateRequest struct {
	Baseline  decision.SourceRef `json:"baseline"`
	Candidate decision.SourceRef `json:"candidate"`
	Policy    asset.Ref          `json:"policy"`
}
type credentialRequest struct {
	Name         string                `json:"name"`
	Capabilities []identity.Capability `json:"capabilities"`
	ExpiresAt    *time.Time            `json:"expires_at"`
}
type credentialCreated struct {
	Credential identity.Credential `json:"credential"`
	Secret     string              `json:"secret,omitempty"`
}
type ruleBindingRequest struct {
	Evaluator     asset.Ref           `json:"evaluator"`
	Metric        asset.Ref           `json:"metric"`
	Required      bool                `json:"required"`
	Applicability asset.Applicability `json:"applicability"`
}
type ruleRequest struct {
	Version       string               `json:"version"`
	Enabled       bool                 `json:"enabled"`
	Scope         ob.Kind              `json:"scope"`
	Trust         string               `json:"trust"`
	Source        string               `json:"source"`
	Filter        ob.Filter            `json:"filter"`
	Sampling      ob.Sampling          `json:"sampling"`
	Budget        ob.Budget            `json:"budget"`
	Rate          ob.Rate              `json:"rate"`
	AllowBackfill bool                 `json:"allow_backfill"`
	BackfillMax   int                  `json:"backfill_max"`
	LiveFrom      *time.Time           `json:"live_from"`
	LiveUntil     *time.Time           `json:"live_until"`
	Bindings      []ruleBindingRequest `json:"bindings"`
}
type backfillRequest struct {
	Rule       asset.Ref `json:"rule"`
	From       time.Time `json:"from"`
	Until      time.Time `json:"until"`
	MaxRecords int       `json:"max_records"`
}
type onlineLimitsRequest struct {
	Budget ob.Budget `json:"budget"`
	Rate   ob.Rate   `json:"rate"`
}

func (r *request) onlineScope() ob.Scope { return ob.Scope{Scope: r.scope(), Epoch: r.server.Epoch} }
func (s *Server) productRoutes() {
	reader := postgres.ProductReader{Pool: s.Pool}
	decisions := postgres.Decisions{Pool: s.Pool}
	online := postgres.Online{Pool: s.Pool}
	exp := experiment.Service{Store: postgres.Experiments{Pool: s.Pool}, Kernel: s.Kernel, Reader: s.Kernel, Builder: ev.Builder{Assets: s.assets()}, Capabilities: s.Kernel.Capabilities}
	for _, kind := range []string{"runs", "experiments", "gates", "rules", "calibrations", "drafts", "api-keys"} {
		cap := identity.Read
		if kind == "api-keys" {
			cap = identity.ManageAPIKey
		}
		if kind == "drafts" {
			cap = identity.PublishDataset
		}
		if kind == "calibrations" {
			cap = identity.ManagePolicy
		}
		s.add("GET", "/"+kind, cap, false, nil, func(r *request) (any, error) {
			after, n, e := r.pagination(kind)
			if e != nil {
				return nil, e
			}
			items, e := reader.List(r.ctx(), r.scope(), kind, after, n+1)
			if e != nil {
				return nil, e
			}
			out := page{Items: items}
			if len(items) > n {
				out.Items = items[:n]
				out.NextCursor = s.encodeCursor(r.scope().ProjectID, kind, items[n-1].ID)
			}
			return out, nil
		})
	}
	s.add("GET", "/overview", identity.Read, false, nil, func(r *request) (any, error) {
		q, e := analyticsQuery(r.HTTP.URL.Query(), "overview", time.Now())
		if e != nil {
			return nil, e
		}
		v, e := reader.Aggregate(r.ctx(), r.scope(), q, "overview")
		if e != nil {
			return nil, e
		}
		var p postgres.ProductProject
		e = s.Pool.QueryRow(r.ctx(), `SELECT id::text,org_id::text,name,created_at FROM projects WHERE id=$1 AND org_id=$2`, r.scope().ProjectID, r.scope().OrganizationID).Scan(&p.ID, &p.OrganizationID, &p.Name, &p.CreatedAt)
		return map[string]any{"id": p.ID, "organization_id": p.OrganizationID, "name": p.Name, "created_at": p.CreatedAt, "analytics": v}, e
	})
	s.add("POST", "/api-keys", identity.ManageAPIKey, true, credentialRequest{}, func(r *request) (any, error) {
		var d credentialRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		c, key, e := s.Identity.CreateCredential(r.ctx(), r.Access, r.CommandID, d.Name, d.Capabilities, d.ExpiresAt)
		if e == nil {
			e = s.Identity.Audit(r.ctx(), r.Access, r.ID, "credential_created", c.ID)
		}
		return credentialCreated{c, key}, e
	})
	s.add("GET", "/api-keys/{id}", identity.ManageAPIKey, false, nil, func(r *request) (any, error) {
		return s.Identity.GetCredential(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
	})
	s.add("POST", "/api-keys/{id}/revoke", identity.ManageAPIKey, false, struct{}{}, func(r *request) (any, error) {
		if e := r.decode(&struct{}{}); e != nil {
			return nil, e
		}
		v, e := s.Identity.Revoke(r.ctx(), r.Access, r.HTTP.PathValue("id"))
		if e == nil {
			e = s.Identity.Audit(r.ctx(), r.Access, r.ID, "credential_revoked", v.ID)
		}
		return v, e
	})
	s.add("POST", "/runs", identity.Execute, true, runRequest{}, func(r *request) (any, error) {
		var d runRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		c, e := r.runCommand(d)
		if e != nil {
			return nil, e
		}
		reply, e := s.kernelFor(c.Snapshot.Input()).CreateRun(r.ctx(), r.evaluationScope(), c)
		return commandReply(reply, e)
	})
	for _, path := range []string{"/runs/{id}", "/runs/{id}/state"} {
		s.add("GET", path, identity.Read, false, nil, func(r *request) (any, error) {
			v, e := s.Kernel.ReadRunState(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
			return runProjection(v), e
		})
	}
	s.add("GET", "/runs/{id}/results", identity.Read, false, nil, func(r *request) (any, error) { return r.resultPage(r.HTTP.PathValue("id"), false) })
	s.add("GET", "/results", identity.Read, false, nil, func(r *request) (any, error) { return r.resultPage("", false) })
	s.add("GET", "/results/{id}", identity.Read, false, nil, func(r *request) (any, error) {
		v, e := reader.Result(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
		return resultProjection(v), e
	})
	s.add("POST", "/experiments", identity.Execute, true, experimentRequest{}, func(r *request) (any, error) {
		var d experimentRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		c, e := r.runCommand(d.Run)
		if e != nil {
			return nil, e
		}
		service := exp
		k := s.kernelFor(c.Snapshot.Input())
		service.Kernel, service.Capabilities = k, k.Capabilities
		v, e := service.Create(r.ctx(), r.evaluationScope(), experiment.Create{ID: r.CommandID, Name: d.Name, Candidate: d.Candidate, Baseline: d.Baseline, Repeat: d.Repeat, Run: c})
		return experimentProjection(v, "PENDING", nil), e
	})
	s.add("POST", "/experiments/{id}/materialize", identity.Execute, true, struct{}{}, func(r *request) (any, error) {
		if e := r.decode(&struct{}{}); e != nil {
			return nil, e
		}
		frozen, e := exp.Store.GetExperiment(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
		if e != nil {
			return nil, e
		}
		service := exp
		k := s.kernelFor(frozen.Intent.Snapshot.Input)
		service.Kernel, service.Capabilities = k, k.Capabilities
		v, e := service.Materialize(r.ctx(), r.evaluationScope(), r.HTTP.PathValue("id"))
		return experimentProjection(v, "MATERIALIZED", nil), e
	})
	s.add("GET", "/experiments/{id}", identity.Read, false, nil, func(r *request) (any, error) {
		v, e := exp.Get(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
		runs := []runResponse{}
		for _, run := range v.Runs {
			runs = append(runs, runProjection(run))
		}
		return experimentProjection(v.Experiment, v.Status, runs), e
	})
	s.add("POST", "/gates", identity.RunGate, true, gateRequest{}, func(r *request) (any, error) {
		var d gateRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		scope := r.evaluationScope()
		scope.Create = true
		v, e := decisions.CreateGate(r.ctx(), scope, decision.Command{ID: r.CommandID, Baseline: d.Baseline, Candidate: d.Candidate, Policy: d.Policy})
		if e == nil {
			e = s.Identity.Audit(r.ctx(), r.Access, r.ID, "gate_requested", v.GateID)
		}
		return gateProjection(v), e
	})
	s.add("GET", "/gates/{id}", identity.Read, false, nil, func(r *request) (any, error) {
		v, e := decisions.GetGateReceipt(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
		return gateProjection(v), e
	})
	s.add("GET", "/gates/{id}/cases", identity.Read, false, nil, func(r *request) (any, error) { return r.gateCasePage() })
	s.add("GET", "/gates/{id}/regressions", identity.Read, false, nil, func(r *request) (any, error) {
		return decisions.GetRegressionSummary(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
	})
	s.add("GET", "/gates/{id}/metrics/{metric}/{version}", identity.Read, false, nil, func(r *request) (any, error) {
		return decisions.GetMetricComparison(r.ctx(), r.scope(), r.HTTP.PathValue("id"), asset.Ref{EntityID: r.HTTP.PathValue("metric"), Version: r.HTTP.PathValue("version")})
	})
	s.add("POST", "/rules", identity.ManagePolicy, true, createLogicalRequest{}, func(r *request) (any, error) {
		var d createLogicalRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		v, e := online.CreateOnlineRule(r.ctx(), r.scope(), asset.Create{ID: r.CommandID, Name: d.Name})
		return logical(v), e
	})
	s.add("GET", "/rules/{id}", identity.Read, false, nil, func(r *request) (any, error) {
		v, e := online.GetOnlineRule(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
		return logical(v), e
	})
	s.add("POST", "/rules/{id}/versions", identity.ManagePolicy, true, ruleRequest{}, func(r *request) (any, error) {
		var d ruleRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		rule := ob.Rule{Enabled: d.Enabled, Scope: d.Scope, Trust: d.Trust, Source: d.Source, Filter: d.Filter, Sampling: d.Sampling, Budget: d.Budget, Rate: d.Rate, AllowBackfill: d.AllowBackfill, BackfillMax: d.BackfillMax, LiveFrom: d.LiveFrom, LiveUntil: d.LiveUntil}
		for _, b := range d.Bindings {
			evaluator, e := s.assets().GetEvaluatorDefinitionVersion(r.ctx(), r.scope(), b.Evaluator)
			if e != nil {
				return nil, e
			}
			m, e := s.assets().GetMetricDefinitionVersion(r.ctx(), r.scope(), b.Metric)
			if e != nil {
				return nil, e
			}
			rule.Bindings = append(rule.Bindings, ob.Binding{Evaluator: ev.EvaluatorSpec{Identity: assetIdentity(evaluator), Definition: evaluator.Content().Body, Metrics: []asset.Ref{b.Metric}, Required: b.Required, Applicability: b.Applicability}, Metric: ev.MetricInput{Identity: assetIdentity(m), Definition: m.Content().Body}})
		}
		reply, e := online.PublishOnlineRuleVersion(r.ctx(), r.onlineScope(), asset.Ref{EntityID: r.HTTP.PathValue("id"), Version: d.Version}, rule)
		return commandReply(reply, e)
	})
	s.add("GET", "/rules/{id}/versions/{version}", identity.Read, false, nil, func(r *request) (any, error) {
		v, e := online.GetOnlineRuleVersion(r.ctx(), r.scope(), pathRef(r))
		if e != nil {
			return nil, e
		}
		return ruleProjection(v), nil
	})
	s.add("GET", "/rules/{id}/versions", identity.Read, false, nil, func(r *request) (any, error) {
		resource := "rule-versions:" + r.HTTP.PathValue("id")
		after, n, e := r.pagination(resource)
		if e != nil {
			return nil, e
		}
		versions, e := reader.Versions(r.ctx(), r.scope(), "rules", r.HTTP.PathValue("id"), after, n+1)
		if e != nil {
			return nil, e
		}
		out := page{}
		if len(versions) > n {
			out.NextCursor = s.encodeCursor(r.scope().ProjectID, resource, versions[n-1])
			versions = versions[:n]
		}
		items := []any{}
		for _, version := range versions {
			v, e := online.GetOnlineRuleVersion(r.ctx(), r.scope(), asset.Ref{EntityID: r.HTTP.PathValue("id"), Version: version})
			if e != nil {
				return nil, e
			}
			items = append(items, ruleProjection(v))
		}
		out.Items = items
		return out, nil
	})
	s.add("GET", "/rules/{id}/versions/{version}/coverage", identity.Read, false, nil, func(r *request) (any, error) { return online.GetOnlineCoverage(r.ctx(), r.scope(), pathRef(r)) })
	s.add("POST", "/online/limits", identity.ManagePolicy, true, onlineLimitsRequest{}, func(r *request) (any, error) {
		var d onlineLimitsRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		v, e := online.ConfigureProjectLimits(r.ctx(), r.onlineScope(), postgres.ProjectOnlinePolicy{Budget: d.Budget, Rate: d.Rate})
		return commandReply(v, e)
	})
	s.add("POST", "/online/backfills", identity.Execute, true, backfillRequest{}, func(r *request) (any, error) {
		var d backfillRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		scope := r.onlineScope()
		// G5 将创建回填意图归于 Reconcile；这里只调用 StartBackfill，运行权限由 API role 隔离。
		scope.Reconcile = r.Access.Principal.Has(identity.Execute)
		v, e := online.StartBackfill(r.ctx(), scope, ob.Backfill{CommandID: r.CommandID, Rule: d.Rule, From: d.From, Until: d.Until, MaxRecords: d.MaxRecords})
		return commandReply(v, e)
	})
	s.add("GET", "/online/works/{id}", identity.Read, false, nil, func(r *request) (any, error) {
		w, result, e := online.GetOnlineEvaluationState(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
		out := map[string]any{"id": w.ID, "state": w.Status, "observation_id": w.ObservationID, "rule": w.Rule}
		if result != nil {
			out["result"] = map[string]any{"id": result.ID, "evaluation_verdict": result.Value.Verdict, "score": result.Value.Score, "applicability": result.Value.Applicability, "sampling": result.Sampling}
		}
		return out, e
	})
	s.add("GET", "/online/backfills/{id}", identity.Read, false, nil, func(r *request) (any, error) {
		return online.GetProductBackfill(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
	})
	s.add("GET", "/observations/{id}", identity.Read, false, nil, func(r *request) (any, error) {
		v, e := online.GetObservation(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
		return map[string]any{"id": v.Ref.ID, "kind": v.Ref.Kind, "digest": v.Ref.Digest, "schema": v.Ref.Schema, "status": v.Status, "operation": v.Operation, "created_at": v.Created, "body_availability": v.BodyAvailability, "retention": v.Retention, "parent_integrity": v.ParentIntegrity}, e
	})
	s.add("GET", "/online/failure-candidates", identity.Read, false, nil, func(r *request) (any, error) {
		key, n, e := r.pagination("failure-candidates")
		if e != nil {
			return nil, e
		}
		var c postgres.FailurePageCursor
		if key != "" && json.Unmarshal([]byte(key), &c) != nil {
			return nil, asset.ErrInvalid
		}
		items, next, e := online.ListFailureCandidatePage(r.ctx(), r.scope(), c, n)
		if e != nil {
			return nil, e
		}
		out := page{Items: items}
		if next != nil {
			b, _ := json.Marshal(next)
			out.NextCursor = s.encodeCursor(r.scope().ProjectID, "failure-candidates", string(b))
		}
		return out, nil
	})
}
func assetIdentity[T any](v asset.Version[T]) ev.AssetIdentity {
	j, _ := asset.ParseJSON(v.Bytes())
	return ev.AssetIdentity{Ref: v.Ref(), ProjectID: v.ProjectID(), Algorithm: v.Algorithm(), ContentDigest: v.ContentDigest(), SemanticDigest: v.SemanticDigest(), CanonicalContent: j}
}
func pathRef(r *request) asset.Ref {
	return asset.Ref{EntityID: r.HTTP.PathValue("id"), Version: r.HTTP.PathValue("version")}
}
func experimentProjection(v experiment.Experiment, status string, runs []runResponse) any {
	return map[string]any{"id": v.ID, "project_id": v.ProjectID, "name": v.Intent.Name, "state": status, "runs": runs, "slots": v.Slots, "digest": v.Digest, "created_at": v.CreatedAt}
}
func ruleProjection(v ob.RuleVersion) any {
	bindings := []map[string]any{}
	for _, b := range v.Body.Bindings {
		bindings = append(bindings, map[string]any{"evaluator": b.Evaluator.Identity.Ref, "metric": b.Metric.Identity.Ref, "required": b.Evaluator.Required})
	}
	return map[string]any{"ref": v.Ref, "digest": v.Digest, "enabled": v.Body.Enabled, "filter": v.Body.Filter, "sampling": v.Body.Sampling, "budget": v.Body.Budget, "rate": v.Body.Rate, "bindings": bindings, "published_at": v.PublishedAt}
}
