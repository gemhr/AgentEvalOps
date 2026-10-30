package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"agentevalops/go-backend/internal/worker"
	"context"
	"os"
	"time"
)

// 端口只消费原 PostgreSQL Decisions 的只读 Owner；不接受外部上传结果。
type IndependentPairStore interface {
	ReadCanonicalPair(context.Context, asset.Scope, string, time.Time) (decision.Snapshot, decision.Receipt, []ev.RunState, error)
}
type verifiedIndependentReader struct {
	IndependentPairStore
	Audits IndependentAuditFiles
}

// 受控运维入口；plan/binding/输出目录必须来自受信 operator，不能映射为用户上传路径。
// 不创建 Run/Gate/Result，不访问 Provider，不写评测表。顶层 Receipt 仍只是原事实的引用工件。
func SummarizeIndependent(ctx context.Context, store IndependentPairStore, s asset.Scope, planPath, planDigest, bindingPath, bindingDigest, receiptPath string, audits ...IndependentAuditFiles) (decision.IndependentEvaluationReceipt, error) {
	var out decision.IndependentEvaluationReceipt
	if s.Validate(false) != nil || store == nil {
		return out, asset.ErrForbidden
	}
	p, e := decision.ReadIndependentPlan(planPath, planDigest)
	if e != nil {
		return out, e
	}
	raw, e := os.ReadFile(bindingPath)
	if e != nil {
		return out, e
	}
	j, e := asset.ParseJSON(raw)
	var binding decision.ExecutionBinding
	if e != nil || string(raw) != j.String() || j.Digest() != bindingDigest || j.Decode(&binding) != nil {
		return out, asset.ErrInvalid
	}
	files := IndependentAuditFiles{}
	if len(audits) == 1 {
		files = audits[0]
	}
	if len(audits) > 1 {
		return out, asset.ErrInvalid
	}
	out, e = decision.AggregateIndependent(ctx, s, p, binding, verifiedIndependentReader{IndependentPairStore: store, Audits: files})
	if e != nil {
		return out, e
	}
	_, e = decision.WriteIndependentArtifact(receiptPath, out)
	return out, e
}

func (verifiedIndependentReader) VerifyIndependentEvidence(scope asset.Scope, p decision.FrozenPlan, c decision.PlannedCase, pin decision.ExecutionPin, ref decision.RunReceiptRefs, mode string, state ev.RunState) error {
	invalid := asset.ErrInvalid
	a := state.Attempts[0]
	s := state.Run.Snapshot
	for _, spec := range s.Input.Evaluators {
		if (!CIDiagnosisSupported(spec.Definition) || s.Target.Version != "ci-synthetic-target.v2") && (!CIDiagnosisV2Supported(spec.Definition) || s.Target.Version != CIPairTargetVersion) {
			return invalid
		}
	}
	evidence, e := ReadCIDiagnosisEvidence(worker.EvaluationInput{Scope: ev.Scope{Scope: scope}, Run: state.Run, Attempt: a})
	if e != nil || evidence.QualityDigest != ref.QualityDigest || evidence.SidecarDigest != ref.SidecarDigest || evidence.LoadDigest != ref.ActualLoadDigest || !completeCIDiagnosisEvidence(evidence) {
		return invalid
	}
	if s.Target.Version == CIPairTargetVersion {
		var cfg map[string]asset.JSON
		_ = s.Target.Config.Decode(&cfg)
		var f catalog.CIPairFrozen
		plan, _ := asset.Freeze(p)
		if cfg["frozen_ci"].Decode(&f) != nil || f.Validate() != nil || f.PlanID != p.ID || f.PlanVersion != p.Version || evidence.PlanDigest != plan.Digest() || f.Dataset != c.Dataset.Ref.EntityID || f.DatasetVersion != c.Dataset.Ref.Version || f.CaseID != c.Case.Ref.EntityID || f.CaseVersion != c.Case.Ref.Version || f.CaseDigest != c.Case.Digest {
			return invalid
		}
		if _, err := VerifyCISourceBinding(evidence.SourceBinding, evidence, f, evidence.Execution, evidence.Incident, mode); err != nil {
			return invalid
		}
	}
	var manifest QualityManifest
	_ = evidence.Bundle.Manifest.Decode(&manifest)
	agent := manifest.Agents["ci_incident_investigator"]
	if manifest.Build != pin.RuntimeBuild || agent.ConfigDigest != pin.RuntimeConfig || agent.DefinitionDigest != pin.Definition || evidence.Bundle.ManifestDigest != pin.Manifest || evidence.Bundle.ProfileDigest != pin.CaptureProfile {
		return invalid
	}
	var cfg map[string]asset.JSON
	_ = s.Target.Config.Decode(&cfg)
	var prompt, artifact, actualMode string
	_ = cfg["prompt_digest"].Decode(&prompt)
	_ = cfg["candidate_manifest_digest"].Decode(&artifact)
	_ = cfg["mode"].Decode(&actualMode)
	if prompt != pin.Prompt || artifact != p.CandidateArtifactDigest || actualMode != mode {
		return invalid
	}
	return nil
}
