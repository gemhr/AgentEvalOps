package httpapi

import (
	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
)

type basebandRunRequest struct {
	Suite     asset.Ref                      `json:"suite"`
	Selectors map[string]aq.BasebandSelector `json:"selectors"`
}

func (s *Server) basebandRoutes() {
	s.add("POST", "/baseband/runs", identity.Execute, true, basebandRunRequest{}, func(r *request) (any, error) {
		var d basebandRunRequest
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
		kind := ""
		for _, c := range snapshot.Input().Manifest {
			selector, ok := d.Selectors[c.Identity.Ref.EntityID]
			if kind != "" && selector.Kind != kind {
				return nil, asset.ErrInvalid
			}
			kind = selector.Kind
			var input struct {
				Schema string `json:"schema_version"`
			}
			// schema 只作身份检查；具体输入由生产端冻结规则验证。
			var shape map[string]any
			if c.Case.Input.Decode(&shape) != nil {
				return nil, asset.ErrInvalid
			}
			input.Schema, _ = shape["schema_version"].(string)
			if !ok || c.Case.Capability != "BASEBAND" || input.Schema != aq.BasebandInput || selector.IncidentID == "" || (selector.Kind != "RULE" && selector.Kind != "AGENT") || (selector.Kind == "AGENT" && (selector.RunID == nil || selector.InvestigationID == nil)) || (selector.Kind == "RULE" && (selector.RunID != nil || selector.InvestigationID != nil)) {
				return nil, asset.ErrInvalid
			}
		}
		for _, spec := range snapshot.Input().Evaluators {
			if !aq.BasebandSupported(spec.Definition) || s.SupportsEvaluator == nil || !s.SupportsEvaluator(spec.Definition) {
				return nil, asset.ErrInvalid
			}
		}
		version := "baseband-rule.v1"
		if kind == "AGENT" {
			version = "baseband_test_investigator/1.0.0"
		}
		subject, _ := asset.Freeze(aq.BasebandSubject{Subject: "baseband", Version: version, Environment: "development-visible", Mode: "SEALED_RUN_REPLAY", Selectors: d.Selectors})
		command := ev.CreateRun{CommandID: r.CommandID, Snapshot: snapshot, Subject: subject, Target: *s.BasebandTarget, Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}
		reply, e := s.kernelFor(snapshot.Input()).CreateRun(r.ctx(), r.evaluationScope(), command)
		return commandReply(reply, e)
	})
}
