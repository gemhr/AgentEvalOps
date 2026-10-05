package httpapi

import (
	"agentevalops/go-backend/internal/analytics"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	"encoding/json"
	"net/url"
	"strconv"
	"strings"
	"time"
)

func analyticsQuery(values url.Values, kind string, now time.Time) (analytics.Query, error) {
	q := analytics.Query{Until: now.UTC(), Bucket: "day", Source: map[string]string{"overview": "ALL", "quality": "OFFLINE", "trends": "OFFLINE", "gates": "GATE", "online": "ONLINE", "human": "HUMAN"}[kind]}
	for name, v := range values {
		if len(v) != 1 || !strings.Contains("|window|from|until|bucket|source_kind|subject|environment|run_mode|metric|evaluator|rule|", "|"+name+"|") {
			return q, asset.ErrInvalid
		}
	}
	window := values.Get("window")
	if window == "" {
		window = "7d"
	}
	switch window {
	case "24h":
		q.From = q.Until.Add(-24 * time.Hour)
		q.Bucket = "hour"
	case "7d":
		q.From = q.Until.Add(-7 * 24 * time.Hour)
	case "30d":
		q.From = q.Until.Add(-30 * 24 * time.Hour)
	case "custom":
		var e error
		q.From, e = time.Parse(time.RFC3339Nano, values.Get("from"))
		if e != nil {
			return q, asset.ErrInvalid
		}
		q.Until, e = time.Parse(time.RFC3339Nano, values.Get("until"))
		if e != nil {
			return q, asset.ErrInvalid
		}
	default:
		return q, asset.ErrInvalid
	}
	if window != "custom" && (values.Get("from") != "" || values.Get("until") != "") {
		return q, asset.ErrInvalid
	}
	if v := values.Get("bucket"); v != "" {
		q.Bucket = v
	}
	if v := values.Get("source_kind"); v != "" && v != q.Source {
		return q, asset.ErrInvalid
	}
	q.Subject = values.Get("subject")
	q.Environment = values.Get("environment")
	q.RunMode = values.Get("run_mode")
	q.Metric = values.Get("metric")
	q.Evaluator = values.Get("evaluator")
	q.Rule = values.Get("rule")
	if kind != "quality" && kind != "trends" && (q.Subject != "" || q.Environment != "" || q.RunMode != "" || q.Metric != "" || q.Evaluator != "") {
		return q, asset.ErrInvalid
	}
	if kind != "online" && q.Rule != "" {
		return q, asset.ErrInvalid
	}
	q.From = q.From.UTC()
	q.Until = q.Until.UTC()
	return q, q.Validate()
}
func (r *request) gateCasePage() (any, error) {
	id := r.HTTP.PathValue("id")
	resource := "gate-cases:" + id
	after, n, e := r.pagination(resource)
	if e != nil {
		return nil, e
	}
	items, e := (postgres.ProductReader{Pool: r.server.Pool}).GateCases(r.ctx(), r.scope(), id, after, n+1)
	if e != nil {
		return nil, e
	}
	out := page{Items: items}
	if len(items) > n {
		out.Items = items[:n]
		out.NextCursor = r.server.encodeCursor(r.scope().ProjectID, resource, items[n-1].Key)
	}
	return out, nil
}
func (s *Server) analyticsRoutes() {
	reader := postgres.ProductReader{Pool: s.Pool}
	for _, kind := range []string{"quality", "trends", "gates", "online", "human"} {
		s.add("GET", "/analytics/"+kind, identity.Read, false, nil, func(r *request) (any, error) {
			q, e := analyticsQuery(r.HTTP.URL.Query(), kind, time.Now())
			if e != nil {
				return nil, e
			}
			return reader.Aggregate(r.ctx(), r.scope(), q, kind)
		})
	}
	s.add("GET", "/exports/results", identity.Read, false, nil, func(r *request) (any, error) {
		if e := exportQuery(r.HTTP.URL.Query(), false); e != nil {
			return nil, e
		}
		return r.resultPage("", true)
	})
	s.add("GET", "/exports/gates/{id}/cases", identity.Read, false, nil, func(r *request) (any, error) {
		if e := exportQuery(r.HTTP.URL.Query(), false); e != nil {
			return nil, e
		}
		return r.gateCasePage()
	})
	s.add("GET", "/exports/online", identity.Read, false, nil, func(r *request) (any, error) {
		if e := exportQuery(r.HTTP.URL.Query(), false); e != nil {
			return nil, e
		}
		after, n, e := r.pagination("export-online")
		if e != nil {
			return nil, e
		}
		items, e := reader.OnlineResults(r.ctx(), r.scope(), after, n+1)
		if e != nil {
			return nil, e
		}
		out := page{}
		if len(items) > n {
			out.NextCursor = s.encodeCursor(r.scope().ProjectID, "export-online", items[n-1].ID)
			items = items[:n]
		}
		values := []any{}
		for _, v := range items {
			values = append(values, map[string]any{"id": v.ID, "observation_id": v.ObservationID, "rule": v.Rule, "evaluator": v.Binding.Evaluator.Identity.Ref, "metric": v.Binding.Metric.Identity.Ref, "evaluation_verdict": v.Value.Verdict, "score": v.Value.Score, "applicability": v.Value.Applicability, "sampling": v.Sampling})
		}
		out.Items = values
		return out, nil
	})
	s.add("GET", "/exports/analytics/{kind}", identity.Read, false, nil, func(r *request) (any, error) {
		kind := r.HTTP.PathValue("kind")
		values := r.HTTP.URL.Query()
		if e := exportQuery(values, true); e != nil {
			return nil, e
		}
		values.Del("format")
		values.Del("limit")
		values.Del("cursor")
		q, e := analyticsQuery(values, kind, time.Now())
		if e != nil {
			return nil, e
		}
		v, e := reader.Aggregate(r.ctx(), r.scope(), q, kind)
		if e != nil {
			return nil, e
		}
		query, _ := json.Marshal(q)
		resource := "export-analytics:" + kind + ":" + string(query)
		key, n, e := r.pagination(resource)
		if e != nil {
			return nil, e
		}
		offset := 0
		if key != "" {
			offset, e = strconv.Atoi(key)
			if e != nil || offset < 0 || offset > len(v.Rows) {
				return nil, asset.ErrInvalid
			}
		}
		end := min(offset+n, len(v.Rows))
		out := page{Items: v.Rows[offset:end]}
		if end < len(v.Rows) {
			out.NextCursor = s.encodeCursor(r.scope().ProjectID, resource, strconv.Itoa(end))
		}
		return map[string]any{"items": out.Items, "next_cursor": out.NextCursor, "projection_version": v.ProjectionVersion, "as_of": v.AsOf, "query": v.Query, "unit": v.Unit, "denominator": v.Denominator}, nil
	})
}
func exportQuery(q url.Values, analytics bool) error {
	for k, v := range q {
		if len(v) != 1 {
			return asset.ErrInvalid
		}
		if !analytics && k != "format" && k != "limit" && k != "cursor" {
			return asset.ErrInvalid
		}
	}
	if format := q.Get("format"); format != "" && format != "json" && format != "ndjson" {
		return asset.ErrInvalid
	}
	if analytics && q.Get("cursor") != "" && q.Get("window") != "custom" {
		return asset.ErrInvalid
	}
	return nil
}
func (r *request) resultPage(run string, export bool) (any, error) {
	resource := "results:" + run
	if export {
		resource = "export-results"
	}
	after, n, e := r.pagination(resource)
	if e != nil {
		return nil, e
	}
	items, e := (postgres.ProductReader{Pool: r.server.Pool}).ResultPage(r.ctx(), r.scope(), run, after, n+1)
	if e != nil {
		return nil, e
	}
	out := page{Items: []resultResponse{}}
	if len(items) > n {
		out.NextCursor = r.server.encodeCursor(r.scope().ProjectID, resource, items[n-1].ID)
		items = items[:n]
	}
	values := []resultResponse{}
	for _, v := range items {
		values = append(values, resultProjection(v))
	}
	out.Items = values
	return out, nil
}
