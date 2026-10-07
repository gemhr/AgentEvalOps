package httpapi

import (
	"agentevalops/go-backend/internal/analytics"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/metric"
	ob "agentevalops/go-backend/internal/observation"
	"agentevalops/go-backend/internal/postgres"
	rv "agentevalops/go-backend/internal/review"
	"reflect"
	"strings"
	"time"
)

func objectSchema(required []string, fields map[string]any) map[string]any {
	return map[string]any{"type": "object", "required": required, "properties": fields, "additionalProperties": true}
}
func fieldSchema(v any) any { return typedSchema(reflect.TypeOf(v), 0, true) }
func listSchema(item any) any {
	return objectSchema([]string{"items", "next_cursor"}, map[string]any{"items": map[string]any{"type": "array", "items": item}, "next_cursor": map[string]any{"anyOf": []any{map[string]any{"type": "string"}, map[string]any{"type": "null"}}}})
}
func versionSchema(body any) any {
	return objectSchema([]string{"ref", "project_id", "content_digest", "content", "published_at"}, map[string]any{"ref": fieldSchema(asset.Ref{}), "project_id": fieldSchema(""), "content_digest": fieldSchema(""), "semantic_digest": fieldSchema(""), "algorithm_ref": fieldSchema(""), "published_by": fieldSchema(""), "published_at": fieldSchema(time.Time{}), "content": objectSchema([]string{"body", "source"}, map[string]any{"body": body, "source": fieldSchema(asset.Source{})})})
}

func (s *Server) responseSchema(r route) any {
	path := strings.TrimPrefix(r.Path, "/api/v1/projects/{project_id}")
	parts := strings.Split(strings.TrimPrefix(path, "/"), "/")
	kind := parts[0]
	if kind == "datasets" && strings.HasSuffix(path, "/development-export") {
		return objectSchema([]string{"ref", "cases"}, map[string]any{"ref": fieldSchema(asset.Ref{}), "cases": fieldSchema([]catalog.DevelopmentCase{})})
	}
	if kind == "analytics" {
		return fieldSchema(analytics.Response{})
	}
	if kind == "exports" && strings.Contains(path, "/analytics/") {
		return objectSchema([]string{"items", "next_cursor", "projection_version", "as_of", "query", "unit", "denominator"}, map[string]any{"items": fieldSchema([]map[string]any{}), "next_cursor": fieldSchema((*string)(nil)), "projection_version": fieldSchema(""), "as_of": fieldSchema(time.Time{}), "query": fieldSchema(analytics.Query{}), "unit": fieldSchema(""), "denominator": fieldSchema("")})
	}
	if path == "/exports/online" {
		return listSchema(map[string]any{"type": "object"})
	}
	if strings.HasPrefix(path, "/exports/gates/") {
		return listSchema(fieldSchema(decision.CaseComparison{}))
	}
	if kind == "exports" && path == "/exports/results" || kind == "results" && len(parts) == 1 {
		return listSchema(fieldSchema(resultResponse{}))
	}
	logicalSchema := objectSchema([]string{"id", "project_id", "name", "created_at"}, map[string]any{"id": fieldSchema(""), "project_id": fieldSchema(""), "name": fieldSchema(""), "created_by": fieldSchema(""), "created_at": fieldSchema(time.Time{})})
	bodyTypes := map[string]any{"cases": catalog.CaseContent{}, "datasets": catalog.DatasetContent{}, "suites": catalog.SuiteContent{}, "metrics": metric.Definition{}, "evaluators": metric.EvaluatorDefinition{}, "policies": decision.Policy{}}
	if body, ok := bodyTypes[kind]; ok {
		if len(parts) == 1 && r.Method == "GET" {
			return listSchema(fieldSchema(postgres.ProductRow{}))
		}
		if len(parts) > 2 && parts[2] == "versions" || strings.Contains(path, "versions-with-exceptions") {
			v := versionSchema(fieldSchema(body))
			if r.Method == "GET" && len(parts) == 3 {
				return listSchema(v)
			}
			return v
		}
		return logicalSchema
	}
	if r.Method == "GET" && len(parts) == 1 && kind != "overview" && kind != "reviews" {
		return listSchema(fieldSchema(postgres.ProductRow{}))
	}
	if kind == "runs" && r.Method == "GET" {
		if strings.HasSuffix(path, "/results") {
			return listSchema(fieldSchema(resultResponse{}))
		}
		return fieldSchema(runResponse{})
	}
	if kind == "trace-envelopes" || kind == "runs" && r.Method == "POST" || kind == "online" && r.Method == "POST" || kind == "rules" && r.Method == "POST" && len(parts) > 2 {
		return objectSchema([]string{"id", "status", "command_status"}, map[string]any{"id": fieldSchema(""), "status": fieldSchema(""), "command_status": fieldSchema("")})
	}
	if kind == "results" {
		return fieldSchema(resultResponse{})
	}
	if kind == "overview" {
		return objectSchema([]string{"id", "organization_id", "name", "created_at", "analytics"}, map[string]any{"id": fieldSchema(""), "organization_id": fieldSchema(""), "name": fieldSchema(""), "created_at": fieldSchema(time.Time{}), "analytics": fieldSchema(analytics.Response{})})
	}
	if kind == "api-keys" {
		if r.Create {
			return fieldSchema(credentialCreated{})
		}
		return fieldSchema(identity.Credential{})
	}
	if kind == "gates" && (len(parts) == 1 || len(parts) == 2) {
		return objectSchema([]string{"gate_id", "decision", "reason_codes", "baseline", "candidate", "policy_ref", "coverage"}, map[string]any{"gate_id": fieldSchema(""), "decision": map[string]any{"type": "string", "enum": []string{"PASS", "FAIL", "BLOCKED"}}, "reason_codes": fieldSchema([]string{}), "baseline": map[string]any{"type": "object"}, "candidate": map[string]any{"type": "object"}, "policy_ref": fieldSchema(asset.Ref{}), "policy_digest": fieldSchema(""), "coverage": map[string]any{"type": "object"}})
	}
	if kind == "gates" {
		if strings.HasSuffix(path, "/cases") {
			return listSchema(fieldSchema(decision.CaseComparison{}))
		}
		if strings.HasSuffix(path, "/regressions") {
			return fieldSchema(decision.Summary{})
		}
		return fieldSchema(decision.MetricComparison{})
	}
	if kind == "reviews" {
		if path == "/reviews/coverage" {
			return fieldSchema(rv.Coverage{})
		}
		if strings.HasSuffix(path, "/golden") {
			return goldenSchema()
		}
		if strings.HasSuffix(path, "/adjudications") {
			return objectSchema([]string{"id", "item_id", "annotation_ids", "decision"}, map[string]any{"id": fieldSchema(""), "item_id": fieldSchema(""), "annotation_ids": fieldSchema([]string{}), "decision": fieldSchema(rv.Judgment{})})
		}
		if strings.HasSuffix(path, "/annotations") {
			return objectSchema([]string{"id", "item_id", "decision", "submitted_at"}, map[string]any{"id": fieldSchema(""), "item_id": fieldSchema(""), "decision": fieldSchema(rv.Judgment{}), "submitted_at": fieldSchema(time.Time{})})
		}
		if r.Method == "POST" && len(parts) == 1 {
			return objectSchema([]string{"id", "status"}, map[string]any{"id": fieldSchema(""), "status": fieldSchema("")})
		}
		if strings.HasSuffix(path, "/release") {
			return objectSchema([]string{"released"}, map[string]any{"released": fieldSchema(true)})
		}
		if r.Method == "GET" {
			if len(parts) == 1 {
				return listSchema(fieldSchema(reviewResponse{}))
			}
			if !strings.HasSuffix(path, "/coverage") {
				return fieldSchema(reviewResponse{})
			}
		}
		if strings.HasSuffix(path, "/claim") || strings.HasSuffix(path, "/renew") {
			return objectSchema([]string{"item_id", "slot", "token", "lease"}, map[string]any{"item_id": fieldSchema(""), "slot": fieldSchema(0), "token": fieldSchema(""), "lease": fieldSchema(time.Time{})})
		}
	}
	if kind == "golden" {
		return goldenSchema()
	}
	if kind == "drafts" && strings.HasSuffix(path, "/publish") {
		return versionSchema(fieldSchema(catalog.CaseContent{}))
	}
	if kind == "dataset-feedback" {
		return versionSchema(fieldSchema(catalog.DatasetContent{}))
	}
	if kind == "rules" {
		if strings.HasSuffix(path, "/coverage") {
			return fieldSchema(ob.Coverage{})
		}
		if len(parts) < 3 {
			return logicalSchema
		}
		v := objectSchema([]string{"ref", "digest", "enabled", "sampling", "bindings"}, map[string]any{"ref": fieldSchema(asset.Ref{}), "digest": fieldSchema(""), "enabled": fieldSchema(true), "sampling": fieldSchema(ob.Sampling{}), "bindings": map[string]any{"type": "array"}})
		if len(parts) == 3 && r.Method == "GET" {
			return listSchema(v)
		}
		return v
	}
	if kind == "online" {
		if strings.HasSuffix(path, "failure-candidates") {
			return listSchema(fieldSchema(ob.FailureCandidate{}))
		}
		if parts[1] == "backfills" {
			return objectSchema([]string{"id", "processed", "completed", "rule", "created_at"}, map[string]any{"id": fieldSchema(""), "processed": fieldSchema(0), "completed": fieldSchema(true), "rule": fieldSchema(asset.Ref{}), "created_at": fieldSchema(time.Time{})})
		}
		return objectSchema([]string{"id", "state", "observation_id", "rule"}, map[string]any{"id": fieldSchema(""), "state": fieldSchema(""), "observation_id": fieldSchema(""), "rule": fieldSchema(asset.Ref{}), "result": map[string]any{"type": "object"}})
	}
	if kind == "observations" {
		return objectSchema([]string{"id", "kind", "digest", "schema", "body_availability", "retention"}, map[string]any{"id": fieldSchema(""), "kind": fieldSchema(""), "digest": fieldSchema(""), "schema": fieldSchema(""), "body_availability": fieldSchema(""), "retention": fieldSchema("")})
	}
	if kind == "calibrations" && !strings.HasSuffix(path, "/compare") {
		return objectSchema([]string{"id", "agreement", "protocol", "sampling", "provider_identities"}, map[string]any{"id": fieldSchema(""), "agreement": fieldSchema(rv.Agreement{}), "protocol": fieldSchema(rv.Protocol{}), "sampling": fieldSchema(rv.Sampling{}), "provider_identities": fieldSchema([]map[string]string{}), "evaluator": fieldSchema(asset.Ref{}), "dataset": fieldSchema(asset.Ref{})})
	}
	if kind == "calibrations" {
		return fieldSchema(rv.EvaluatorRegression{})
	}
	if kind == "gate-exceptions" {
		if strings.HasSuffix(path, "/proof") {
			return objectSchema([]string{"proof", "next_step"}, map[string]any{"proof": fieldSchema(decision.Exception{}), "next_step": fieldSchema("")})
		}
		return fieldSchema(rv.GateException{})
	}
	if kind == "drafts" && !strings.HasSuffix(path, "/publish") {
		return objectSchema([]string{"id", "item_id", "body", "sanitization", "case"}, map[string]any{"id": fieldSchema(""), "item_id": fieldSchema(""), "body": fieldSchema(catalog.CaseContent{}), "sanitization": fieldSchema(""), "case": fieldSchema(asset.Ref{})})
	}
	if kind == "experiments" {
		return objectSchema([]string{"id", "name", "state", "slots", "runs"}, map[string]any{"id": fieldSchema(""), "name": fieldSchema(""), "state": fieldSchema(""), "slots": map[string]any{"anyOf": []any{map[string]any{"type": "array"}, map[string]any{"type": "null"}}}, "runs": map[string]any{"anyOf": []any{map[string]any{"type": "array", "items": fieldSchema(runResponse{})}, map[string]any{"type": "null"}}}})
	}
	return map[string]any{"type": "object", "description": "领域命令的公开响应；内部租约、writer 和 provider 内容不属于此合同"}
}

func goldenSchema() any {
	return objectSchema([]string{"id", "item_id", "schema", "source_digest", "origin", "decision", "protocol"}, map[string]any{"id": fieldSchema(""), "item_id": fieldSchema(""), "schema": fieldSchema(asset.Ref{}), "source_digest": fieldSchema(""), "origin": fieldSchema(""), "decision": fieldSchema(rv.Judgment{}), "protocol": fieldSchema(rv.Protocol{})})
}
