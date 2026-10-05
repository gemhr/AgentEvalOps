package httpapi

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
	"reflect"
	"strings"
	"time"
)

func (s *Server) OpenAPI() map[string]any {
	paths := map[string]any{}
	errorSchema := map[string]any{"type": "object", "required": []string{"error"}, "properties": map[string]any{"error": map[string]any{"type": "object", "required": []string{"code", "message", "request_id"}, "properties": map[string]any{"code": map[string]any{"type": "string"}, "message": map[string]any{"type": "string"}, "request_id": map[string]any{"type": "string", "format": "uuid"}}}}}
	for _, r := range s.routes {
		path, ok := paths[r.Path].(map[string]any)
		if !ok {
			path = map[string]any{}
			paths[r.Path] = path
		}
		responses := map[string]any{"200": map[string]any{"description": "命令执行成功；Gate FAIL/BLOCKED 仍为 200", "content": map[string]any{"application/json": map[string]any{"schema": s.responseSchema(r)}}}}
		for _, code := range []string{"400", "401", "403", "404", "409", "412", "413", "429", "500", "503"} {
			responses[code] = map[string]any{"description": "稳定错误 envelope", "content": map[string]any{"application/json": map[string]any{"schema": errorSchema}}}
		}
		parameters := []any{}
		for _, part := range strings.Split(r.Path, "/") {
			if strings.HasPrefix(part, "{") {
				parameters = append(parameters, map[string]any{"name": strings.Trim(part, "{}"), "in": "path", "required": true, "schema": map[string]any{"type": "string"}})
			}
		}
		if r.Create {
			parameters = append(parameters, map[string]any{"name": "Idempotency-Key", "in": "header", "required": true, "schema": map[string]any{"type": "string", "minLength": 1, "maxLength": 128}})
		}
		if r.Method == "GET" {
			for _, name := range []string{"cursor", "limit"} {
				parameters = append(parameters, map[string]any{"name": name, "in": "query", "schema": map[string]any{"type": "string"}})
			}
		}
		op := map[string]any{"operationId": strings.ReplaceAll(r.Method+r.Path, "/", "_"), "x-capability": r.Capability, "security": []any{map[string]any{"ApiKey": []string{}}, map[string]any{"Bearer": []string{}}}, "responses": responses, "parameters": parameters}
		if r.Input != nil {
			op["requestBody"] = map[string]any{"required": true, "content": map[string]any{"application/json": map[string]any{"schema": schema(r.Input, 0)}}}
		}
		path[strings.ToLower(r.Method)] = op
	}
	for path, method := range map[string]string{"/health/live": "get", "/health/ready": "get", "/api/v1/openapi.json": "get", "/api/v1/auth/dev-login": "post", "/api/v1/me": "get", "/api/v1/projects": "get"} {
		out := any(map[string]any{"type": "object"})
		if path == "/api/v1/me" {
			out = fieldSchema(identity.Principal{})
		}
		if path == "/api/v1/projects" {
			out = listSchema(fieldSchema(postgres.ProductProject{}))
		}
		op := map[string]any{"responses": map[string]any{"200": map[string]any{"description": "成功", "content": map[string]any{"application/json": map[string]any{"schema": out}}}, "401": map[string]any{"description": "UNAUTHENTICATED"}, "503": map[string]any{"description": "UNAVAILABLE"}}}
		if method == "post" {
			op["requestBody"] = map[string]any{"content": map[string]any{"application/json": map[string]any{"schema": fieldSchema(loginRequest{})}}}
		}
		paths[path] = map[string]any{method: op}
	}
	return map[string]any{"openapi": "3.1.0", "info": map[string]string{"title": "AgentEvalOps Product API", "version": "1.0.0"}, "paths": paths, "components": map[string]any{"securitySchemes": map[string]any{"ApiKey": map[string]string{"type": "apiKey", "in": "header", "name": "X-API-Key"}, "Bearer": map[string]string{"type": "http", "scheme": "bearer"}}}}
}
func schema(t reflect.Type, depth int) any {
	return typedSchema(t, depth, false)
}
func typedSchema(t reflect.Type, depth int, response bool) any {
	if depth > 12 {
		return map[string]any{}
	}
	if t == reflect.TypeOf(asset.JSON{}) {
		return map[string]any{"description": "JSON 用户数据；不提供身份或权限"}
	}
	if t == reflect.TypeOf(time.Time{}) {
		return map[string]any{"type": "string", "format": "date-time"}
	}
	switch t.Kind() {
	case reflect.Pointer:
		return map[string]any{"anyOf": []any{typedSchema(t.Elem(), depth+1, response), map[string]any{"type": "null"}}}
	case reflect.String:
		return map[string]any{"type": "string"}
	case reflect.Bool:
		return map[string]any{"type": "boolean"}
	case reflect.Int, reflect.Int64, reflect.Int32:
		return map[string]any{"type": "integer"}
	case reflect.Float64, reflect.Float32:
		return map[string]any{"type": "number"}
	case reflect.Slice:
		return map[string]any{"anyOf": []any{map[string]any{"type": "array", "items": typedSchema(t.Elem(), depth+1, response)}, map[string]any{"type": "null"}}}
	case reflect.Array:
		return map[string]any{"type": "array", "items": typedSchema(t.Elem(), depth+1, response)}
	case reflect.Map:
		return map[string]any{"anyOf": []any{map[string]any{"type": "object", "additionalProperties": typedSchema(t.Elem(), depth+1, response)}, map[string]any{"type": "null"}}}
	case reflect.Struct:
		properties := map[string]any{}
		required := []string{}
		for n := 0; n < t.NumField(); n++ {
			f := t.Field(n)
			if !f.IsExported() {
				continue
			}
			name := strings.Split(f.Tag.Get("json"), ",")[0]
			if name == "-" {
				continue
			}
			if name == "" {
				name = f.Name
			}
			properties[name] = typedSchema(f.Type, depth+1, response)
			if response && !strings.Contains(f.Tag.Get("json"), "omitempty") {
				required = append(required, name)
			}
		}
		return map[string]any{"type": "object", "additionalProperties": false, "properties": properties, "required": required}
	}
	return map[string]any{}
}
