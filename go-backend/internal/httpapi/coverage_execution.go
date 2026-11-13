package httpapi

import (
	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
)

type coverageRunRequest struct {
	Suite     asset.Ref                      `json:"suite"`
	Selectors map[string]aq.CoverageSelector `json:"selectors"`
}

func (s *Server) coverageRoutes() {
	s.add("POST", "/coverage-execution/runs", identity.Execute, true, coverageRunRequest{}, func(r *request) (any, error) {
		var d coverageRunRequest
		if r.decode(&d) != nil || s.BasebandTarget == nil {
			return nil, asset.ErrInvalid
		}
		snapshot, e := (ev.Builder{Assets: s.assets()}).BuildRunSnapshot(r.ctx(), r.scope(), ev.BuildRunSnapshot{Suite: &d.Suite})
		if e != nil {
			return nil, e
		}
		if len(d.Selectors) != len(snapshot.Input().Manifest) {
			return nil, asset.ErrInvalid
		}
		for _, c := range snapshot.Input().Manifest {
			q, ok := d.Selectors[c.Identity.Ref.EntityID]
			var shape map[string]any
			if !ok || c.Case.Input.Decode(&shape) != nil || shape["schema_version"] != aq.CoverageInput || c.Case.Capability != "COVERAGE_EXECUTION" || q.Diagnosis != "RULE" || q.TaskID == "" || q.ReportID == "" || q.PlanID == "" || q.TestID == "" || q.InstanceID == "" || q.ExecutionID == "" || q.IncidentID == "" {
				return nil, asset.ErrInvalid
			}
		}
		for _, spec := range snapshot.Input().Evaluators {
			if !aq.CoverageSupported(spec.Definition) || s.SupportsEvaluator == nil || !s.SupportsEvaluator(spec.Definition) {
				return nil, asset.ErrInvalid
			}
		}
		target := *s.BasebandTarget
		target.Version = aq.CoverageTargetVersion
		target.Capabilities, _ = asset.Freeze(map[string]any{"mode": "SEALED_BUSINESS_REPLAY", "input_contract": aq.CoverageInput})
		subject, _ := asset.Freeze(aq.CoverageSubject{Mode: "SEALED_BUSINESS_REPLAY", Selectors: d.Selectors})
		command := ev.CreateRun{CommandID: r.CommandID, Snapshot: snapshot, Subject: subject, Target: target, Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}
		reply, e := s.kernelFor(snapshot.Input()).CreateRun(r.ctx(), r.evaluationScope(), command)
		return commandReply(reply, e)
	})
}
