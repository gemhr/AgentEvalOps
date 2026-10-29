package httpapi

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
)

type businessFeedbackRequest struct {
	IncidentID string `json:"incident_id"`
	FeedbackID string `json:"feedback_id"`
	Digest     string `json:"projection_digest"`
}

func (s *Server) businessFeedbackRoutes() {
	s.add("POST", "/business-feedback", identity.Write, false, businessFeedbackRequest{}, func(r *request) (any, error) {
		if r.Access.Principal.Type != "MACHINE" || r.HTTP.Header.Get("X-API-Key") == "" {
			return nil, asset.ErrForbidden
		}
		var d businessFeedbackRequest
		if r.decode(&d) != nil {
			return nil, asset.ErrInvalid
		}
		w, e := (postgres.Reviews{Pool: s.Pool}).StoreBusinessFeedback(r.ctx(), ev.Scope{Scope: r.scope(), Epoch: s.Epoch, Create: true}, d.IncidentID, d.FeedbackID, d.Digest)
		if e != nil {
			return nil, e
		}
		return map[string]any{"feedback_id": d.FeedbackID, "projection_digest": w.Digest, "status": "AWAITING_HUMAN_REVIEW", "source_type": "SYNTHETIC"}, nil
	})
	s.add("GET", "/business-feedback/{id}", identity.Read, false, nil, func(r *request) (any, error) {
		// 查询同样回查 AC 当前修订状态；失败或撤回不能显示有效。
		return (postgres.Reviews{Pool: s.Pool}).BusinessFeedbackReceipt(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
	})
}
