package httpapi

import (
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	fi "agentevalops/go-backend/internal/featureimpact"
	"agentevalops/go-backend/internal/identity"
)

type featureImpactRunRequest struct {
	Suite asset.Ref `json:"suite"`
}

func (s *Server) featureImpactRoutes() {
	s.add("POST", "/feature-impact/runs", identity.Execute, true, featureImpactRunRequest{}, func(r *request) (any, error) {
		var d featureImpactRunRequest
		if r.decode(&d) != nil || s.FeatureImpactTarget == nil {
			return nil, asset.ErrInvalid
		}
		snapshot, e := (ev.Builder{Assets: s.assets()}).BuildRunSnapshot(r.ctx(), r.scope(), ev.BuildRunSnapshot{Suite: &d.Suite})
		if e != nil {
			return nil, e
		}
		version := ""
		for _, c := range snapshot.Input().Manifest {
			in, e := fi.ReadInput(c.Case.Input.Bytes())
			if e != nil || c.Case.Capability != "FEATURE_IMPACT" || (version != "" && version != in.CandidateVersion) {
				return nil, asset.ErrInvalid
			}
			version = in.CandidateVersion
		}
		for _, spec := range snapshot.Input().Evaluators {
			if spec.Definition.ImplementationRef != fi.Implementation || s.SupportsEvaluator == nil || !s.SupportsEvaluator(spec.Definition) {
				return nil, asset.ErrInvalid
			}
		}
		subject, _ := asset.Freeze(map[string]any{"subject": "feature-impact", "subject_version": version, "environment": "frozen-public-corpus", "run_mode": "OFFLINE_BUSINESS_REPLAY"})
		command := ev.CreateRun{CommandID: r.CommandID, Snapshot: snapshot, Subject: subject, Target: *s.FeatureImpactTarget, Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}
		reply, e := s.kernelFor(snapshot.Input()).CreateRun(r.ctx(), r.evaluationScope(), command)
		return commandReply(reply, e)
	})
}
