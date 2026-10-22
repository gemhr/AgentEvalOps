package httpapi

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/cibusiness"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
)

type ciBusinessRunRequest struct {
	Suite asset.Ref `json:"suite"`
	Rule  string    `json:"rule_version"`
}

func (s *Server) ciBusinessRoutes() {
	s.add("POST", "/ci-business/runs", identity.Execute, true, ciBusinessRunRequest{}, func(r *request) (any, error) {
		var d ciBusinessRunRequest
		if r.decode(&d) != nil || !cibusiness.ValidRule(d.Rule) || s.CIBusinessTarget == nil {
			return nil, asset.ErrInvalid
		}
		snapshot, e := (ev.Builder{Assets: s.assets()}).BuildRunSnapshot(r.ctx(), r.scope(), ev.BuildRunSnapshot{Suite: &d.Suite})
		if e != nil {
			return nil, e
		}
		for _, c := range snapshot.Input().Manifest {
			if c.Case.Capability != "CI_BUSINESS_QUALITY" {
				return nil, asset.ErrInvalid
			}
		}
		for _, spec := range snapshot.Input().Evaluators {
			if spec.Definition.ImplementationRef != cibusiness.Implementation || s.SupportsEvaluator == nil || !s.SupportsEvaluator(spec.Definition) {
				return nil, asset.ErrInvalid
			}
		}
		subject, _ := asset.Freeze(map[string]any{"subject": "ci-incident-business-rules", "subject_version": d.Rule, "environment": "frozen-simulation", "run_mode": "OFFLINE_BUSINESS_REPLAY"})
		command := ev.CreateRun{CommandID: r.CommandID, Snapshot: snapshot, Subject: subject, Target: *s.CIBusinessTarget, Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}
		reply, e := s.kernelFor(snapshot.Input()).CreateRun(r.ctx(), r.evaluationScope(), command)
		return commandReply(reply, e)
	})
	s.add("GET", "/results/{id}/business-quality", identity.Read, false, nil, func(r *request) (any, error) {
		result, e := (postgres.ProductReader{Pool: s.Pool}).Result(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
		if e != nil {
			return nil, e
		}
		state, e := s.Kernel.ReadRunState(r.ctx(), r.scope(), result.RunID)
		if e != nil {
			return nil, e
		}
		for _, a := range state.Attempts {
			if a.ID != result.AttemptID {
				continue
			}
			if a.Request.Case.Case.Capability != "CI_BUSINESS_QUALITY" {
				return nil, asset.ErrNotFound
			}
			return map[string]any{"result_id": result.ID, "run_id": result.RunID, "attempt_id": a.ID, "case": a.Request.Case.Identity.Ref, "input": a.Request.Case.Case.Input, "reference": a.Request.Case.Case.GroundTruth, "actual": result.Value.Artifact.Body, "provenance": result.Value.Provenance, "score": result.Value.Score, "verdict": result.Value.Verdict, "reason": result.Value.Reason}, nil
		}
		return nil, asset.ErrNotFound
	})
}
