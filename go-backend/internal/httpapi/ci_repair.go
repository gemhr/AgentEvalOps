package httpapi

import (
	aq "agentevalops/go-backend/internal/agentquality"
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/identity"
	"agentevalops/go-backend/internal/postgres"
)

type repairRunRequest struct {
	Suite          asset.Ref `json:"suite"`
	Role           string    `json:"role"`
	WorkflowID     string    `json:"workflow_run_id"`
	EvidenceDigest string    `json:"evidence_digest"`
}

type repairGateResponse struct {
	GateID         string            `json:"gate_id"`
	ProjectID      string            `json:"project_id"`
	OrganizationID string            `json:"organization_id"`
	Decision       decision.Decision `json:"decision"`
	WorkflowID     string            `json:"workflow_run_id"`
	EvidenceDigest string            `json:"evidence_digest"`
	BaselineRunID  string            `json:"baseline_run_id"`
	CandidateRunID string            `json:"candidate_run_id"`
}

func repairMetricRefs(input ev.SnapshotInput) map[string]asset.Ref {
	if len(input.Manifest) != 2 || len(input.Evaluators) != 2 || len(input.Metrics) != 2 || input.Suite == nil || input.Dataset == nil {
		return nil
	}
	seen := map[string]bool{}
	for _, item := range input.Manifest {
		c, _, valid := aq.RepairCatalogCase(item.Case.Input, item.Case.GroundTruth)
		if !valid || item.Case.Capability != "CI_REPAIR" || item.Case.Criticality != "CRITICAL" || seen[c.CaseID] {
			return nil
		}
		seen[c.CaseID] = true
	}
	refs := map[string]asset.Ref{}
	for _, spec := range input.Evaluators {
		var cfg aq.Config
		if !aq.RepairSupported(spec.Definition) || !spec.Required || len(spec.Metrics) != 1 ||
			spec.Definition.Config.Decode(&cfg) != nil || refs[cfg.Metric] != (asset.Ref{}) {
			return nil
		}
		refs[cfg.Metric] = spec.Metrics[0]
	}
	if refs[aq.RepairCorrectness] == refs[aq.RepairValidity] {
		return nil
	}
	return refs
}

func repairPolicy(policy decision.Policy, refs map[string]asset.Ref) bool {
	if len(refs) != 2 || policy.Validate() != nil || !policy.Offline || policy.Online || !policy.CriticalMissing ||
		!policy.BlockPerCase || len(policy.Criticalities) != 1 || policy.Criticalities[0] != "CRITICAL" ||
		policy.Coverage != (decision.CoveragePolicy{MinimumDecision: 1, MinimumEvaluation: 1}) ||
		policy.TaskSuccess != nil || len(policy.Metrics) != 2 || len(policy.Exceptions) != 0 ||
		len(policy.AcceptedDifferences) != 0 || policy.Stage13ReleaseVersion != "" {
		return false
	}
	for _, rule := range policy.Metrics {
		if !rule.Required || !rule.PerCase || rule.Aggregation != "mean" || rule.MinimumCoverage != 1 ||
			rule.Minimum == nil || *rule.Minimum != 1 || rule.AbsoluteTolerance != nil || rule.RelativeTolerance != nil ||
			rule.TargetRange != nil || rule.Maximum != nil ||
			(rule.Metric != refs[aq.RepairValidity] && rule.Metric != refs[aq.RepairCorrectness]) {
			return false
		}
	}
	return true
}

func (s *Server) repairPair(r *request, baseline, candidate decision.SourceRef, policy asset.Ref) (aq.RepairLocator, error) {
	decisions := postgres.Decisions{Pool: s.Pool}
	var subjects [2]aq.RepairSubject
	var metricRefs map[string]asset.Ref
	var policyDigest string
	for i, source := range []decision.SourceRef{baseline, candidate} {
		if source.Kind != "RUN_SET" || len(source.Runs) != 1 {
			return aq.RepairLocator{}, asset.ErrInvalid
		}
		state, err := s.Kernel.ReadRunState(r.ctx(), r.scope(), source.Runs[0])
		if err != nil {
			return aq.RepairLocator{}, err
		}
		snapshot := state.Run.Snapshot
		if snapshot.Target.Version != aq.RepairTargetVersion || snapshot.Subject.Decode(&subjects[i]) != nil ||
			!subjects[i].Valid() || subjects[i].Role != []string{"BASELINE", "CANDIDATE"}[i] {
			return aq.RepairLocator{}, asset.ErrInvalid
		}
		pinned := snapshot.Input.Policy
		if pinned == nil || pinned.PolicyID != policy.EntityID || pinned.Version != policy.Version ||
			(i == 1 && policyDigest != pinned.Digest) {
			return aq.RepairLocator{}, asset.ErrInvalid
		}
		policyDigest = pinned.Digest
		refs := repairMetricRefs(snapshot.Input)
		if refs == nil || (i == 1 && (refs[aq.RepairValidity] != metricRefs[aq.RepairValidity] ||
			refs[aq.RepairCorrectness] != metricRefs[aq.RepairCorrectness])) {
			return aq.RepairLocator{}, asset.ErrInvalid
		}
		metricRefs = refs
	}
	if subjects[0].RepairLocator != subjects[1].RepairLocator {
		return aq.RepairLocator{}, asset.ErrInvalid
	}
	p, err := decisions.GetPolicyVersion(r.ctx(), r.scope(), policy)
	if err != nil {
		return aq.RepairLocator{}, err
	}
	if p.ContentDigest() != policyDigest || !repairPolicy(p.Content().Body, metricRefs) {
		return aq.RepairLocator{}, asset.ErrInvalid
	}
	return subjects[0].RepairLocator, nil
}

func (s *Server) repairRoutes() {
	s.add("POST", "/ci-repair/runs", identity.Execute, true, repairRunRequest{}, func(r *request) (any, error) {
		var d repairRunRequest
		if r.decode(&d) != nil || s.BasebandTarget == nil {
			return nil, asset.ErrInvalid
		}
		subject := aq.NewRepairSubject(d.Role, aq.RepairLocator{WorkflowID: d.WorkflowID, EvidenceDigest: d.EvidenceDigest})
		if !subject.Valid() {
			return nil, asset.ErrInvalid
		}
		snapshot, err := (ev.Builder{Assets: s.assets()}).BuildRunSnapshot(r.ctx(), r.scope(), ev.BuildRunSnapshot{Suite: &d.Suite})
		if err != nil {
			return nil, err
		}
		if repairMetricRefs(snapshot.Input()) == nil {
			return nil, asset.ErrInvalid
		}
		target := *s.BasebandTarget
		target.Version = aq.RepairTargetVersion
		target.Capabilities, _ = asset.Freeze(map[string]any{"mode": "SEALED_RUN_REPLAY", "input_contract": aq.RepairInput})
		frozen, _ := asset.Freeze(subject)
		cmd := ev.CreateRun{CommandID: r.CommandID, Snapshot: snapshot, Subject: frozen, Target: target,
			Retry: ev.RetryPolicy{Version: "NO_RETRY.v1", MaxAttempts: 1}}
		reply, err := s.kernelFor(snapshot.Input()).CreateRun(r.ctx(), r.evaluationScope(), cmd)
		return commandReply(reply, err)
	})
	s.add("POST", "/ci-repair/gates", identity.RunGate, true, gateRequest{}, func(r *request) (any, error) {
		var d gateRequest
		if err := r.decode(&d); err != nil {
			return nil, err
		}
		if _, err := s.repairPair(r, d.Baseline, d.Candidate, d.Policy); err != nil {
			return nil, err
		}
		scope := r.evaluationScope()
		scope.Create = true
		decisions := postgres.Decisions{Pool: s.Pool}
		v, err := decisions.CreateGate(r.ctx(), scope, decision.Command{ID: r.CommandID, Baseline: d.Baseline, Candidate: d.Candidate, Policy: d.Policy})
		if err == nil {
			err = s.Identity.Audit(r.ctx(), r.Access, r.ID, "gate_requested", v.GateID)
		}
		return gateProjection(v), err
	})
	s.add("GET", "/ci-repair/gates/{id}", identity.Read, false, nil, func(r *request) (any, error) {
		decisions := postgres.Decisions{Pool: s.Pool}
		v, err := decisions.GetGateReceipt(r.ctx(), r.scope(), r.HTTP.PathValue("id"))
		if err != nil {
			return nil, err
		}
		loc, err := s.repairPair(r, v.Baseline.Ref, v.Candidate.Ref, v.PolicyRef)
		if err != nil {
			return nil, err
		}
		return repairGateResponse{GateID: v.GateID, ProjectID: v.ProjectID, OrganizationID: r.scope().OrganizationID,
			Decision: v.Decision, WorkflowID: loc.WorkflowID, EvidenceDigest: loc.EvidenceDigest,
			BaselineRunID: v.Baseline.Ref.Runs[0], CandidateRunID: v.Candidate.Ref.Runs[0]}, nil
	})
}
