package decision

import (
	"context"
	"fmt"
	"os"
	"reflect"
	"regexp"
	"slices"
	"time"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	ev "agentevalops/go-backend/internal/evaluation"
)

const IndependentPlanVersion = "ci-independent-evaluation-plan.v1"
const IndependentBindingVersion = "ci-independent-execution-binding.v1"
const IndependentReceiptVersion = "ci-independent-evaluation-receipt.v1"

var independentDigest = regexp.MustCompile(`^[0-9a-f]{64}$`)

type FrozenAssetRef struct {
	Ref    asset.Ref
	Digest string
}
type ExecutionPin struct {
	RuntimeBuild, RuntimeConfig, Prompt, Definition, Manifest, CaptureProfile string
	TargetVersion, TargetDigest, SubjectDigest                                string
}
type PlannedCase struct {
	Purpose                                            string
	Dataset, Case, Suite, Policy                       FrozenAssetRef
	Evaluators                                         []FrozenAssetRef
	GroundTruthVersion, GroundTruthDigest, Criticality string
	Lineage                                            catalog.CIDiagnosisMetadata
	Baseline, Candidate                                ExecutionPin
}

// 计划只有输入身份，没有未来 Run/Attempt/Result 或自身摘要。
type FrozenPlan struct {
	Schema, ID, Version, ProjectID, CandidateArtifactDigest string
	FrozenAt                                                time.Time
	Cases                                                   []PlannedCase
}
type ResultRef struct{ ID, Digest string }
type RunReceiptRefs struct {
	RunID, AttemptID, RequestID                    string
	Results                                        []ResultRef
	QualityDigest, SidecarDigest, ActualLoadDigest string
}
type BranchReceiptRefs struct {
	Case                                         asset.Ref
	GateID, ComparisonDigest, ReceiptDigest      string
	ComparabilityAuditDigest, HoldoutAuditDigest string
	Baseline, Candidate                          RunReceiptRefs
}
type ExecutionBinding struct {
	Schema, PlanDigest string
	Cases              []BranchReceiptRefs
}
type CaseGateReference struct {
	Case                           asset.Ref
	Purpose, GateID, ReceiptDigest string
	Decision                       Decision
	OriginalDecision               Decision
	GateReasons                    []string
	Reasons                        []string
	BaselineTask, CandidateTask    string
	Coverage                       struct{ Baseline, Candidate Coverage }
}
type IndependentEvaluationReceipt struct {
	Schema, PlanDigest, BindingDigest string
	Decision                          Decision
	Cases                             []CaseGateReference
	Missing, CriticalFailures         []string
}

func (p FrozenPlan) Validate() error {
	if p.Schema != IndependentPlanVersion || p.FrozenAt.IsZero() || !asset.ValidID(p.ID) || !asset.Text(p.Version) || !asset.ValidID(p.ProjectID) || !independentDigest.MatchString(p.CandidateArtifactDigest) || len(p.Cases) == 0 {
		return asset.ErrInvalid
	}
	seen := map[asset.Ref]bool{}
	for _, c := range p.Cases {
		if seen[c.Case.Ref] || !slices.Contains([]string{"GOLDEN", "FAILURE", "HOLDOUT"}, c.Purpose) || (c.GroundTruthVersion != catalog.CIDiagnosisGTVersion && c.GroundTruthVersion != catalog.CIDiagnosisGTVersionV2) || !independentDigest.MatchString(c.GroundTruthDigest) || !slices.Contains([]string{"CRITICAL", "NORMAL"}, c.Criticality) || len(c.Evaluators) == 0 {
			return asset.ErrInvalid
		}
		seen[c.Case.Ref] = true
		for _, a := range append([]FrozenAssetRef{c.Dataset, c.Case, c.Suite, c.Policy}, c.Evaluators...) {
			if a.Ref.Validate() != nil || !independentDigest.MatchString(a.Digest) {
				return asset.ErrInvalid
			}
		}
		for _, s := range []ExecutionPin{c.Baseline, c.Candidate} {
			if !asset.Text(s.TargetVersion) {
				return asset.ErrInvalid
			}
			for _, d := range []string{s.RuntimeBuild, s.RuntimeConfig, s.Prompt, s.Definition, s.Manifest, s.CaptureProfile, s.TargetDigest, s.SubjectDigest} {
				if !independentDigest.MatchString(d) {
					return asset.ErrInvalid
				}
			}
		}
		first := p.Cases[0]
		if c.Baseline.RuntimeBuild != c.Candidate.RuntimeBuild || c.Baseline.CaptureProfile != c.Candidate.CaptureProfile || c.Baseline.RuntimeBuild != first.Baseline.RuntimeBuild || c.Baseline.Prompt != first.Baseline.Prompt || c.Candidate.Prompt != first.Candidate.Prompt || c.Baseline.Definition != first.Baseline.Definition || c.Candidate.Definition != first.Candidate.Definition || c.Baseline.RuntimeConfig != first.Baseline.RuntimeConfig || c.Candidate.RuntimeConfig != first.Candidate.RuntimeConfig || c.Baseline.Manifest != first.Baseline.Manifest || c.Candidate.Manifest != first.Candidate.Manifest || c.Baseline.CaptureProfile != first.Baseline.CaptureProfile || c.Baseline.TargetVersion != first.Baseline.TargetVersion || c.Candidate.TargetVersion != first.Candidate.TargetVersion {
			return asset.ErrInvalid
		}
	}
	return nil
}

// 工件必须写入由受信 operator 控制的目录。O_EXCL 拒绝覆盖，不赋予 JSON 内 PASS 发布权。
func WriteIndependentArtifact(path string, value any) (string, error) {
	j, e := asset.Freeze(value)
	if e != nil {
		return "", e
	}
	f, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if e != nil {
		return "", e
	}
	_, e = f.Write(j.Bytes())
	if e == nil {
		e = f.Sync()
	}
	closeErr := f.Close()
	if e == nil {
		e = closeErr
	}
	if e != nil {
		return "", e
	}
	return j.Digest(), nil
}
func FreezeIndependentPlan(path string, p FrozenPlan) (string, error) {
	if e := p.Validate(); e != nil {
		return "", e
	}
	return WriteIndependentArtifact(path, p)
}
func ReadIndependentPlan(path, want string) (FrozenPlan, error) {
	var p FrozenPlan
	raw, e := os.ReadFile(path)
	if e != nil {
		return p, e
	}
	j, e := asset.ParseJSON(raw)
	if e != nil || string(raw) != j.String() || j.Digest() != want || j.Decode(&p) != nil || p.Validate() != nil {
		return p, asset.ErrInvalid
	}
	return p, nil
}

// CanonicalPairReader 由 agentquality 运维边界组合原 PostgreSQL Owner 和严格 Quality 重验。
// 不接收 HTTP 上传的 score/decision；后续 Run 缺失时仍可返回已经核实的原 Gate。
// 测试替身只验证合同，不能构成真实业务 Gate。
type CanonicalPairReader interface {
	ReadCanonicalPair(context.Context, asset.Scope, string, time.Time) (Snapshot, Receipt, []ev.RunState, error)
	VerifyIndependentPair(asset.Scope, FrozenPlan, PlannedCase, BranchReceiptRefs, Snapshot, []ev.RunState) error
	VerifyIndependentEvidence(asset.Scope, FrozenPlan, PlannedCase, ExecutionPin, RunReceiptRefs, string, ev.RunState) error
}

func AggregateIndependent(ctx context.Context, scope asset.Scope, p FrozenPlan, b ExecutionBinding, reader CanonicalPairReader) (IndependentEvaluationReceipt, error) {
	out := IndependentEvaluationReceipt{Schema: IndependentReceiptVersion, PlanDigest: digest(p), BindingDigest: digest(b), Decision: Pass, Cases: []CaseGateReference{}, Missing: []string{}, CriticalFailures: []string{}}
	if scope.Validate(false) != nil || p.Validate() != nil || scope.ProjectID != p.ProjectID || b.Schema != IndependentBindingVersion || b.PlanDigest != out.PlanDigest || reader == nil {
		return out, asset.ErrInvalid
	}
	byCase := map[asset.Ref]BranchReceiptRefs{}
	known := map[asset.Ref]bool{}
	for _, c := range p.Cases {
		known[c.Case.Ref] = true
	}
	for _, r := range b.Cases {
		if _, ok := byCase[r.Case]; ok || !known[r.Case] {
			return out, asset.ErrInvalid
		}
		byCase[r.Case] = r
	}
	purposes := map[string]int{}
	gates, runs := map[string]bool{}, map[string]bool{}
	for _, c := range p.Cases {
		purposes[c.Purpose]++
		item := CaseGateReference{Case: c.Case.Ref, Purpose: c.Purpose, Decision: Blocked, Reasons: []string{}}
		wrapped, _ := asset.Freeze(map[string]any{"ci_diagnosis": c.Lineage})
		_, metadataErr := catalog.ReadCIDiagnosisMetadata(wrapped)
		if metadataErr != nil || c.Lineage.Purpose != c.Purpose || c.Lineage.GroundTruthDigest != c.GroundTruthDigest {
			item.Reasons = append(item.Reasons, "LINEAGE_UNVERIFIED")
		}
		if c.Purpose == "HOLDOUT" && catalog.ValidateCIDiagnosisHoldoutSet(p.ProjectID, c.Dataset.Ref, c.Case.Ref, c.Lineage, otherLineage(p, c.Case.Ref)) != nil {
			item.Reasons = append(item.Reasons, "HOLDOUT_INDEPENDENCE_UNVERIFIED")
		}
		refs, ok := byCase[c.Case.Ref]
		if !ok {
			item.Reasons = append(item.Reasons, "CASE_GATE_MISSING")
		} else {
			item.GateID, item.ReceiptDigest = refs.GateID, refs.ReceiptDigest
			s, r, states, e := reader.ReadCanonicalPair(ctx, scope, refs.GateID, p.FrozenAt)
			canonical := asset.ValidID(refs.GateID) && s.ProjectID == scope.ProjectID && r.ProjectID == scope.ProjectID && s.Command.ID == refs.GateID && r.GateID == refs.GateID && r.Version == Contract && digest(s) == refs.ComparisonDigest && digest(r) == refs.ReceiptDigest && r.SnapshotDigest == refs.ComparisonDigest && r.PolicyRef == c.Policy.Ref && r.PolicyDigest == c.Policy.Digest && s.Command.Policy == c.Policy.Ref && s.PolicyDigest == c.Policy.Digest && digest(s.Baseline) == digest(r.Baseline) && digest(s.Candidate) == digest(r.Candidate)
			if canonical {
				for _, failure := range r.CriticalRegressions {
					out.CriticalFailures = append(out.CriticalFailures, refs.GateID+":"+failure)
				}
				item.OriginalDecision = r.Decision
				item.GateReasons = append([]string{}, r.Reasons...)
			}
			if e != nil {
				if ctx.Err() != nil {
					return out, ctx.Err()
				}
				item.Reasons = append(item.Reasons, "CANONICAL_REFERENCE_UNAVAILABLE")
			} else {
				// 先验证原 Gate 绑定，再保留其已确认 Critical failure，后续缺证据不能抹掉该事实。
				if !canonical {
					item.Reasons = append(item.Reasons, "CANONICAL_BINDING_INVALID")
				} else {
					item.Coverage.Baseline = r.Coverage.Baseline
					item.Coverage.Candidate = r.Coverage.Candidate
					if gates[refs.GateID] || runs[refs.Baseline.RunID] || runs[refs.Candidate.RunID] || refs.Baseline.RunID == refs.Candidate.RunID {
						item.Reasons = append(item.Reasons, "EXECUTION_REFERENCE_REUSED")
					}
					gates[refs.GateID] = true
					runs[refs.Baseline.RunID] = true
					runs[refs.Candidate.RunID] = true
					if r.Compatibility != Comparable && r.Compatibility != Accepted || r.Decision != Pass && r.Decision != Fail && r.Decision != Blocked {
						item.Reasons = append(item.Reasons, "COMPARABILITY_UNVERIFIED")
					}
					for _, side := range []struct {
						ref    RunReceiptRefs
						pin    ExecutionPin
						source Source
						mode   string
					}{{refs.Baseline, c.Baseline, s.Baseline, "BASELINE"}, {refs.Candidate, c.Candidate, s.Candidate, "CANDIDATE"}} {
						if e := verifyIndependentRun(scope, p, c, side.pin, side.ref, side.source, side.mode, states, reader); e != nil {
							item.Reasons = append(item.Reasons, side.mode+"_EVIDENCE_UNVERIFIED")
						}
					}
					if len(r.Cases) != 1 || r.Cases[0].CaseID != c.Case.Ref.EntityID || r.Cases[0].CaseVersion != c.Case.Ref.Version {
						item.Reasons = append(item.Reasons, "PAIRED_CASE_MISMATCH")
					} else {
						item.BaselineTask = r.Cases[0].BaselineTask
						item.CandidateTask = r.Cases[0].CandidateTask
						if r.Cases[0].BaselineRunID != refs.Baseline.RunID || r.Cases[0].CandidateRunID != refs.Candidate.RunID || r.Cases[0].BaselineAttemptID != refs.Baseline.AttemptID || r.Cases[0].CandidateAttemptID != refs.Candidate.AttemptID {
							item.Reasons = append(item.Reasons, "CASE_EXECUTION_BINDING_INVALID")
						}
						if !slices.Contains([]string{"SUCCESS", "FAILURE"}, item.BaselineTask) || !slices.Contains([]string{"SUCCESS", "FAILURE"}, item.CandidateTask) {
							item.Reasons = append(item.Reasons, "TASK_UNKNOWN")
						}
						if c.Purpose == "FAILURE" && item.BaselineTask == "SUCCESS" {
							item.Reasons = append(item.Reasons, "FAILURE_NOT_REPRODUCED")
						}
					}
					if err := reader.VerifyIndependentPair(scope, p, c, refs, s, states); err != nil {
						item.Reasons = append(item.Reasons, "TRUSTED_PAIR_AUDIT_UNVERIFIED")
					}
					if len(item.Reasons) == 0 {
						item.Decision = r.Decision
						if item.Decision != Blocked && (len(r.CriticalRegressions) > 0 || c.Purpose == "FAILURE" && item.CandidateTask == "FAILURE") {
							item.Decision = Fail
						}
					}
				}
			}
		}
		if item.Decision == Blocked {
			out.Decision = Blocked
			out.Missing = append(out.Missing, c.Purpose+":"+c.Case.Ref.EntityID)
		} else if item.Decision == Fail && out.Decision != Blocked {
			out.Decision = Fail
		}
		out.Cases = append(out.Cases, item)
	}
	for _, purpose := range []string{"GOLDEN", "FAILURE", "HOLDOUT"} {
		if purposes[purpose] == 0 {
			out.Decision = Blocked
			out.Missing = append(out.Missing, purpose+":BRANCH_MISSING")
		}
	}
	out.CriticalFailures = unique(out.CriticalFailures)
	return out, nil
}

func matchesAsset(pin FrozenAssetRef, actual *ev.AssetIdentity) bool {
	return actual != nil && actual.Ref == pin.Ref && actual.ContentDigest == pin.Digest && actual.CanonicalContent.Digest() == pin.Digest && actual.Algorithm == asset.CatalogAlgorithm
}
func verifyIndependentRun(scope asset.Scope, p FrozenPlan, c PlannedCase, pin ExecutionPin, ref RunReceiptRefs, source Source, mode string, states []ev.RunState, reader CanonicalPairReader) error {
	invalid := fmt.Errorf("%w: INDEPENDENT_EVIDENCE_UNVERIFIED", asset.ErrInvalid)
	if source.Ref.Kind != "RUN_SET" || !slices.Equal(source.Ref.Runs, []string{ref.RunID}) || len(source.Units) != 1 || len(source.Reasons) > 0 || source.ExpectedSlots == 0 || source.ExpectedSlots != source.CompletedSlots {
		return invalid
	}
	var state *ev.RunState
	for i := range states {
		if states[i].Run.ID == ref.RunID {
			if state != nil {
				return invalid
			}
			state = &states[i]
		}
	}
	if state == nil || state.Run.ProjectID != scope.ProjectID || !state.Run.Status.Terminal() || len(state.Attempts) != 1 {
		return invalid
	}
	s := state.Run.Snapshot
	in := s.Input
	a := state.Attempts[0]
	if in.Origin != ev.PublishedCatalog || !matchesAsset(c.Dataset, in.Dataset) || !matchesAsset(c.Suite, in.Suite) || len(in.Manifest) != 1 || !matchesAsset(c.Case, &in.Manifest[0].Identity) || string(in.Manifest[0].Case.Criticality) != c.Criticality || in.Manifest[0].Case.GroundTruth.Digest() != c.GroundTruthDigest || len(in.Evaluators) != len(c.Evaluators) || s.Retry.MaxAttempts != 1 || s.Retry.AllowUnknownFork || s.Target.Version != pin.TargetVersion || digest(s.Target) != pin.TargetDigest || s.Subject.Digest() != pin.SubjectDigest || a.ID != ref.AttemptID || a.RequestID != ref.RequestID || a.RunID != ref.RunID || a.Status != "TERMINAL" || a.Outcome != ev.Success || a.Request.Case.Identity.Ref != c.Case.Ref || a.Request.Case.Identity.ContentDigest != c.Case.Digest || digest(a.Request.Target) != pin.TargetDigest {
		return invalid
	}
	var gt map[string]asset.JSON
	var version string
	_ = in.Manifest[0].Case.GroundTruth.Decode(&gt)
	_ = gt["schema_version"].Decode(&version)
	if version != c.GroundTruthVersion || s.Target.Version == "ci-synthetic-target.v3" && version != catalog.CIDiagnosisGTVersionV2 || s.Target.Version == "ci-synthetic-target.v2" && version != catalog.CIDiagnosisGTVersion {
		return invalid
	}
	metadata, e := catalog.ReadCIDiagnosisMetadata(in.Manifest[0].Case.Metadata)
	if e != nil || !reflect.DeepEqual(metadata, c.Lineage) || catalog.ValidateCIDiagnosisCase(in.Manifest[0].Case) != nil {
		return invalid
	}
	seen := map[asset.Ref]bool{}
	for _, frozen := range c.Evaluators {
		found := false
		for _, spec := range in.Evaluators {
			if matchesAsset(frozen, &spec.Identity) && !seen[frozen.Ref] {
				found = true
			}
		}
		if !found {
			return invalid
		}
		seen[frozen.Ref] = true
	}
	if len(state.Results) != len(ref.Results) || len(ref.Results) != len(c.Evaluators) {
		return invalid
	}
	resultIDs := map[string]bool{}
	for _, r := range state.Results {
		found := false
		for _, rr := range ref.Results {
			if rr.ID == r.ID && rr.Digest == digest(r) {
				found = true
			}
		}
		if !found || resultIDs[r.ID] || r.ProjectID != scope.ProjectID || r.RunID != ref.RunID || r.AttemptID != a.ID || r.CaseID != c.Case.Ref.EntityID || r.CaseVersion != c.Case.Ref.Version || !slices.Contains([]string{"PASS", "FAIL"}, r.Value.Verdict) {
			return invalid
		}
		resultIDs[r.ID] = true
	}
	// 原 Offline 投影复核 Result/Work 与 Comparison 的引用，不调用 Compare、不重新评分。
	current, e := Offline(source.Ref, []ev.RunState{*state})
	if e != nil || digest(current) != digest(source) {
		return invalid
	}
	return reader.VerifyIndependentEvidence(scope, p, c, pin, ref, mode, *state)
}

func otherLineage(p FrozenPlan, current asset.Ref) []catalog.CIDiagnosisMetadata {
	out := []catalog.CIDiagnosisMetadata{}
	for _, c := range p.Cases {
		if c.Case.Ref != current {
			out = append(out, c.Lineage)
		}
	}
	return out
}
