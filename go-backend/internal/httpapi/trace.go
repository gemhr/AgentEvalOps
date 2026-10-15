package httpapi

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
	ob "agentevalops/go-backend/internal/observation"
	"agentevalops/go-backend/internal/postgres"
	"bytes"
)

// traceRoutes 采用 G8 项目授权及 G5 strict owner；外部调用方须显式迁移新入口。
func (s *Server) traceRoutes() {
	s.add("POST", "/trace-envelopes", identity.Write, false, ob.Trace{}, func(r *request) (any, error) {
		if r.Access.Principal.Type != "MACHINE" || r.HTTP.Header.Get("X-API-Key") == "" {
			return nil, asset.ErrForbidden
		}
		scope := ob.Scope{Scope: r.scope(), Epoch: s.Epoch, Ingest: true, AuthenticatedSource: true}
		reply, err := (postgres.Online{Pool: s.Pool}).IngestStrict(r.ctx(), scope, bytes.NewReader(r.Raw))
		if mapped := traceValidationError(reply, err); mapped != nil {
			return nil, mapped
		}
		return commandReply(reply, err)
	})
}

func traceValidationError(reply ev.Reply, err error) error {
	if err == nil && reply.Code == ev.Rejected && (reply.Reason == "LOCALAGENT_ENVELOPE_INVALID" || reply.Reason == "TRACE_BODY_TOO_LARGE" || reply.Reason == "SELF_PARENT") {
		return asset.ErrInvalid
	}
	return nil
}
