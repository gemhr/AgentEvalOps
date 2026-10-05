package httpapi

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"strconv"
	"strings"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/postgres"
)

type createLogicalRequest struct {
	Name string `json:"name"`
}
type publishRequest[T any] struct {
	Version string `json:"version"`
	Body    T      `json:"body"`
}
type page struct {
	Items      any     `json:"items"`
	NextCursor *string `json:"next_cursor"`
}
type cursor struct{ Project, Resource, Key string }

func (s *Server) encodeCursor(p, resource, key string) *string {
	raw, _ := json.Marshal(cursor{p, resource, key})
	data := base64.RawURLEncoding.EncodeToString(raw)
	h := hmac.New(sha256.New, s.Identity.Pepper)
	h.Write([]byte(data))
	v := data + "." + base64.RawURLEncoding.EncodeToString(h.Sum(nil))
	return &v
}
func (r *request) pagination(resource string) (string, int, error) {
	limit := 25
	if v := r.HTTP.URL.Query().Get("limit"); v != "" {
		n, e := strconv.Atoi(v)
		if e != nil || n < 1 || n > 100 {
			return "", 0, asset.ErrInvalid
		}
		limit = n
	}
	raw := r.HTTP.URL.Query().Get("cursor")
	if raw == "" {
		return "", limit, nil
	}
	if len(raw) > 2048 {
		return "", 0, asset.ErrInvalid
	}
	data, sig, ok := strings.Cut(raw, ".")
	h := hmac.New(sha256.New, r.server.Identity.Pepper)
	h.Write([]byte(data))
	b, e := base64.RawURLEncoding.DecodeString(sig)
	if !ok || e != nil || !hmac.Equal(b, h.Sum(nil)) {
		return "", 0, asset.ErrInvalid
	}
	b, e = base64.RawURLEncoding.DecodeString(data)
	var c cursor
	if e != nil || json.Unmarshal(b, &c) != nil || c.Project != r.scope().ProjectID || c.Resource != resource {
		return "", 0, asset.ErrInvalid
	}
	return c.Key, limit, nil
}
func logical(v asset.Logical) any {
	return map[string]any{"id": v.ID, "project_id": v.ProjectID, "name": v.Name, "created_by": v.CreatedBy, "created_at": v.CreatedAt}
}
func source(r *request) asset.Source {
	return asset.Source{Kind: "PRODUCT_API", Ref: "command:" + r.CommandID, Principal: r.scope().Principal}
}
func registerCatalog[T any](s *Server, kind string, cap identity.Capability, create func(context.Context, asset.Scope, asset.Create) (asset.Logical, error), get func(context.Context, asset.Scope, string) (asset.Logical, error), publish func(context.Context, asset.Scope, asset.Publish[T]) (asset.Version[T], error), version func(context.Context, asset.Scope, asset.Ref) (asset.Version[T], error)) {
	s.add("POST", "/"+kind, identity.Write, true, createLogicalRequest{}, func(r *request) (any, error) {
		var d createLogicalRequest
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		v, e := create(r.ctx(), r.scope(), asset.Create{ID: r.CommandID, Name: d.Name})
		return logical(v), e
	})
	s.add("GET", "/"+kind, identity.Read, false, nil, func(r *request) (any, error) {
		after, n, e := r.pagination(kind)
		if e != nil {
			return nil, e
		}
		items, e := (postgres.ProductReader{Pool: s.Pool}).List(r.ctx(), r.scope(), kind, after, n+1)
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
	s.add("GET", "/"+kind+"/{id}", identity.Read, false, nil, func(r *request) (any, error) {
		v, e := get(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
		return logical(v), e
	})
	s.add("POST", "/"+kind+"/{id}/versions", cap, true, publishRequest[T]{}, func(r *request) (any, error) {
		var d publishRequest[T]
		if e := r.decode(&d); e != nil {
			return nil, e
		}
		scope := r.scope()
		scope.CanPublish = true
		v, e := publish(r.ctx(), scope, asset.Publish[T]{Ref: asset.Ref{EntityID: r.HTTP.PathValue("id"), Version: d.Version}, Body: d.Body, Source: source(r)})
		if e == nil {
			e = s.Identity.Audit(r.ctx(), r.Access, r.ID, kind+"_published", v.Ref().EntityID)
		}
		return v, e
	})
	s.add("GET", "/"+kind+"/{id}/versions", identity.Read, false, nil, func(r *request) (any, error) {
		resource := kind + ":" + r.HTTP.PathValue("id")
		after, n, e := r.pagination(resource)
		if e != nil {
			return nil, e
		}
		refs, e := (postgres.ProductReader{Pool: s.Pool}).Versions(r.ctx(), r.scope(), kind, r.HTTP.PathValue("id"), after, n+1)
		if e != nil {
			return nil, e
		}
		out := page{}
		if len(refs) > n {
			out.NextCursor = s.encodeCursor(r.scope().ProjectID, resource, refs[n-1])
			refs = refs[:n]
		}
		items := []asset.Version[T]{}
		for _, ref := range refs {
			v, e := version(r.ctx(), r.scope(), asset.Ref{EntityID: r.HTTP.PathValue("id"), Version: ref})
			if e != nil {
				return nil, e
			}
			items = append(items, v)
		}
		out.Items = items
		return out, nil
	})
	s.add("GET", "/"+kind+"/{id}/versions/{version}", identity.Read, false, nil, func(r *request) (any, error) {
		return version(r.ctx(), r.scope(), asset.Ref{EntityID: r.HTTP.PathValue("id"), Version: r.HTTP.PathValue("version")})
	})
}
func (s *Server) catalogRoutes() {
	cases := catalog.CaseService{Store: postgres.Cases{Pool: s.Pool}}
	datasets := catalog.DatasetService{Store: postgres.Datasets{Pool: s.Pool}, Cases: postgres.Cases{Pool: s.Pool}}
	suites := catalog.SuiteService{Store: postgres.Suites{Pool: s.Pool}, References: s.assets()}
	metrics := metric.MetricDefinitionService{Store: postgres.MetricDefinitions{Pool: s.Pool}}
	evaluators := metric.EvaluatorDefinitionService{Store: postgres.EvaluatorDefinitions{Pool: s.Pool}, Metrics: postgres.MetricDefinitions{Pool: s.Pool}}
	registerCatalog(s, "cases", identity.PublishDataset, cases.CreateCase, cases.GetCase, cases.PublishCaseVersion, cases.GetCaseVersion)
	registerCatalog(s, "datasets", identity.PublishDataset, datasets.CreateDataset, datasets.GetDataset, datasets.PublishDatasetVersion, datasets.GetDatasetVersion)
	registerCatalog(s, "suites", identity.Write, suites.CreateSuite, suites.GetSuite, suites.PublishSuiteVersion, suites.GetSuiteVersion)
	registerCatalog(s, "metrics", identity.ManagePolicy, metrics.CreateMetricDefinition, metrics.GetMetricDefinition, metrics.PublishMetricDefinitionVersion, metrics.GetMetricDefinitionVersion)
	registerCatalog(s, "evaluators", identity.ManagePolicy, evaluators.CreateEvaluatorDefinition, evaluators.GetEvaluatorDefinition, evaluators.PublishEvaluatorDefinitionVersion, evaluators.GetEvaluatorDefinitionVersion)
	decisions := postgres.Decisions{Pool: s.Pool}
	registerCatalog(s, "policies", identity.ManagePolicy, decisions.CreatePolicy, func(ctx context.Context, scope asset.Scope, id string) (asset.Logical, error) {
		return (postgres.ProductReader{Pool: s.Pool}).GetPolicy(ctx, scope, id)
	}, func(ctx context.Context, scope asset.Scope, c asset.Publish[decision.Policy]) (decision.PolicyVersion, error) {
		// 外部政策不能自声明 proof/actor；质量例外由专门 proof endpoint 提供，另版本显式发布。
		if len(c.Body.AcceptedDifferences) > 0 || len(c.Body.Exceptions) > 0 {
			return decision.PolicyVersion{}, asset.ErrInvalid
		}
		return decisions.PublishPolicyVersion(ctx, scope, c)
	}, decisions.GetPolicyVersion)
}
