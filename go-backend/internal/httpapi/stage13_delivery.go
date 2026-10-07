package httpapi

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/delivery"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
)

func (s *Server) stage13DeliveryRoutes() {
	s.add("POST", "/stage13/delivery-authorizations", identity.Read, false, delivery.Request{}, func(r *request) (any, error) {
		if r.Access.Principal.Type != "MACHINE" || r.HTTP.Header.Get("X-API-Key") == "" {
			return nil, asset.ErrForbidden
		}
		var q delivery.Request
		if e := r.decode(&q); e != nil {
			return nil, e
		}
		return (postgres.Decisions{Pool: s.Pool}).AuthorizeStage13Delivery(r.ctx(), r.scope(), q)
	})
}
