package httpapi

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	gov "agentevalops/go-backend/internal/cigovernance"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/provider"
)

type triageDevelopmentAttempt struct {
	AttemptID string             `json:"attempt_id"`
	CaseID    string             `json:"case_id"`
	Outcome   ev.OutcomeKind     `json:"execution_outcome"`
	Metadata  ev.AttemptMetadata `json:"outcome_metadata"`
	Artifact  *ev.Binding        `json:"artifact,omitempty"`
	Cleanup   asset.JSON         `json:"cleanup"`
}
type triageDevelopmentEvidence struct {
	RunID         string                     `json:"run_id"`
	Attempts      []triageDevelopmentAttempt `json:"attempts"`
	Summary       decision.Stage13Aggregate  `json:"summary"`
	Results       []ev.EvaluationResult      `json:"results"`
	SourceReasons []string                   `json:"source_reasons"`
}

// 仅导出开发用途的实际执行回执/输出，不返回 snapshot、Case GT 或隐藏 descriptor。
func projectTriageDevelopment(state ev.RunState) (triageDevelopmentEvidence, error) {
	out := triageDevelopmentEvidence{RunID: state.Run.ID, Attempts: []triageDevelopmentAttempt{}}
	if state.Run.Snapshot.Target.ID != provider.Stage13TargetID || state.Run.Snapshot.Input.Dataset == nil {
		return out, asset.ErrForbidden
	}
	var dataset asset.Content[catalog.DatasetContent]
	if state.Run.Snapshot.Input.Dataset.CanonicalContent.Decode(&dataset) != nil {
		return out, asset.ErrInvalid
	}
	policy, e := gov.ReadPolicy(dataset.Body.Metadata)
	if e != nil {
		return out, e
	}
	var exposed map[string]asset.JSON
	_ = dataset.Body.Metadata.Decode(&exposed)
	if !(policy != nil && policy.Role == "DEVELOPMENT") && exposed["usage"].String() != `"EXPOSED_SET"` {
		return out, asset.ErrForbidden
	}
	for _, a := range state.Attempts {
		out.Attempts = append(out.Attempts, triageDevelopmentAttempt{a.ID, a.CaseID, a.Outcome, a.Metadata, a.Metadata.Observation.Artifact, a.Metadata.Observation.Cleanup})
	}
	source, err := decision.Offline(decision.SourceRef{Kind: "RUN_SET", Runs: []string{state.Run.ID}}, []ev.RunState{state})
	if err != nil {
		return out, err
	}
	out.Summary = decision.SummarizeStage13(source)
	out.Results = state.Results
	out.SourceReasons = source.Reasons
	return out, nil
}
func (s *Server) stage13DevelopmentRoutes() {
	s.add("GET", "/runs/{id}/development-evidence", identity.Read, false, nil, func(r *request) (any, error) {
		state, e := s.Kernel.ReadRunState(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
		if e != nil {
			return nil, e
		}
		return projectTriageDevelopment(state)
	})
}
