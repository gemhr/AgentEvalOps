package agentquality

import (
	"context"
	"crypto/sha256"
	"fmt"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/metric"
	"agentevalops/go-backend/internal/worker"
)

type ToolExpectation struct {
	Alias           string                `json:"alias"`
	Count           int                   `json:"count"`
	Arguments       *string               `json:"arguments_digest"`
	FollowUp        bool                  `json:"follow_up"`
	Content         *string               `json:"result_content_digest"`
	ExecutionStatus *string               `json:"execution_status"`
	Parameters      []ParameterConstraint `json:"parameters"`
}
type ParameterConstraint struct {
	Path   string       `json:"path"`
	Equals asset.JSON   `json:"equals"`
	Min    *int64       `json:"minimum"`
	Max    *int64       `json:"maximum"`
	Set    []asset.JSON `json:"allowed_values"`
}
type FactExpectation struct {
	Key    string `json:"key"`
	Digest string `json:"value_digest"`
	Source string `json:"source_kind"`
}
type QualityCase struct {
	Schema               string   `json:"schema_version"`
	RequiredEvidence     []string `json:"required_evidence"`
	RequiredCapabilities []string `json:"required_capabilities"`
	Root                 struct {
		Decision string `json:"decision"`
		Outcome  string `json:"outcome"`
	} `json:"root"`
	Tools struct {
		Mode      string            `json:"mode"`
		Required  []ToolExpectation `json:"required"`
		Forbidden []string          `json:"forbidden"`
		Order     []string          `json:"order"`
	} `json:"tools"`
	Handoff struct {
		Mode           string  `json:"mode"`
		Target         *string `json:"target_ref"`
		Result         *string `json:"result_digest"`
		ParentConsumed bool    `json:"parent_consumed"`
	} `json:"handoff"`
	Context struct {
		Mode  string            `json:"mode"`
		Facts []FactExpectation `json:"facts"`
	} `json:"context"`
	Memory struct {
		Mode         string   `json:"mode"`
		Required     []string `json:"required_record_refs"`
		Forbidden    []string `json:"forbidden_record_refs"`
		Formation    string   `json:"formation"`
		Conversation string   `json:"conversation"`
		Private      string   `json:"private_scope"`
		Epistemic    []string `json:"allowed_epistemic_kinds"`
	} `json:"memory"`
	Answer struct {
		Mode   string  `json:"mode"`
		Digest *string `json:"content_digest"`
	} `json:"answer"`
}

func ReadQualityCase(j asset.JSON) (QualityCase, error) {
	var c QualityCase
	if StrictQualityShape(j.Bytes(), &c) != nil || c.Schema != "agent-quality-case.v1" || len(c.Tools.Required) > 16 || len(c.Context.Facts) > 16 ||
		!oneOf(c.Tools.Mode, "REQUIRED", "NOT_REQUESTED") || !oneOf(c.Handoff.Mode, "REQUIRED", "NOT_REQUESTED") ||
		!oneOf(c.Context.Mode, "REQUIRED", "NOT_REQUESTED") || !oneOf(c.Memory.Mode, "REQUIRED", "NOT_REQUESTED") || !oneOf(c.Answer.Mode, "REQUIRED", "NOT_REQUESTED") {
		return c, asset.ErrInvalid
	}
	for _, r := range c.Tools.Required {
		if !asset.Text(r.Alias) || r.Count < 1 || r.Count > 64 {
			return c, asset.ErrInvalid
		}
		if r.Arguments != nil && !loopDigest.MatchString(*r.Arguments) {
			return c, asset.ErrInvalid
		}
	}
	if c.Answer.Mode == "REQUIRED" && (c.Answer.Digest == nil || !loopDigest.MatchString(*c.Answer.Digest)) {
		return c, asset.ErrInvalid
	}
	if !oneOf(c.Root.Decision, "FINAL", "REFUSAL") || c.Root.Outcome != "SUCCEEDED" {
		return c, asset.ErrInvalid
	}
	for _, kind := range c.RequiredEvidence {
		if !oneOf(kind, "loop", "context", "memory", "execution") {
			return c, asset.ErrInvalid
		}
	}
	for _, kind := range c.RequiredCapabilities {
		if !oneOf(kind, "TOOL", "BATCH", "HANDOFF", "COMPACTION", "PROJECT_MEMORY") {
			return c, asset.ErrInvalid
		}
	}
	if c.Memory.Mode == "REQUIRED" && (!oneOf(c.Memory.Formation, "REQUIRED", "NOT_REQUESTED") || !oneOf(c.Memory.Conversation, "REQUIRED", "NOT_REQUESTED") || !oneOf(c.Memory.Private, "REQUIRED", "NOT_REQUESTED")) {
		return c, asset.ErrInvalid
	}
	if c.Tools.Mode == "NOT_REQUESTED" && (len(c.Tools.Required) > 0 || len(c.Tools.Forbidden) > 0 || len(c.Tools.Order) > 0) ||
		c.Handoff.Mode == "NOT_REQUESTED" && (c.Handoff.Target != nil || c.Handoff.Result != nil || c.Handoff.ParentConsumed) ||
		c.Context.Mode == "NOT_REQUESTED" && len(c.Context.Facts) > 0 {
		return c, asset.ErrInvalid
	}
	return c, nil
}
func BehaviorSupported(d metric.EvaluatorDefinition) bool {
	var c Config
	return d.Validate() == nil && d.Kind == metric.Deterministic && d.Availability != asset.Unsupported &&
		d.ImplementationRef == BehaviorImplementation && d.InputContract == BehaviorInput && d.SchemaVersion == BehaviorResult &&
		d.Normalization == BehaviorNormalization && len(d.OutputMetrics) == 1 && d.Config.Decode(&c) == nil && c.K == 0 &&
		oneOf(c.Metric, "agent_execution_integrity.v1", "task_success.v1")
}
func BehaviorEvaluatorDefinition(output asset.Ref, name string) metric.EvaluatorDefinition {
	config, _ := asset.Freeze(Config{Metric: name})
	return metric.EvaluatorDefinition{Kind: metric.Deterministic, InputContract: BehaviorInput, OutputMetrics: []asset.Ref{output},
		Applicability: asset.Applicability{RequiredEvidence: []asset.EvidenceRequirement{{Kind: "agent_quality", Schema: QualitySchema, BodyRequired: true}}, RuleRef: BehaviorImplementation},
		Availability:  asset.ContractOnly, ImplementationRef: BehaviorImplementation, SchemaVersion: BehaviorResult, Config: config,
		Budget: metric.Budget{TotalMilliseconds: 1000, MaxResponseBytes: 4096}, Retry: metric.RetryPolicy{MaxEvaluationAttempts: 1}, Normalization: BehaviorNormalization}
}
func integrityQuality(b QualityBundle, o QualityObservations) (string, string) {
	if b.Coverage != "COMPLETE" || o.Coverage != "COMPLETE" || len(b.Gaps) > 0 || len(o.Gaps) > 0 {
		return "INCONCLUSIVE", "QUALITY_EVIDENCE_INCOMPLETE"
	}
	if b.Terminal.Sequence == nil || *b.Terminal.Sequence < 1 {
		return "INCONCLUSIVE", "ROOT_TERMINAL_UNPROVEN"
	}
	if o.Memory == nil || o.Memory.Effects == nil {
		return "INCONCLUSIVE", "MEMORY_EFFECT_POLICY_UNPROVEN"
	}
	effects := o.Memory.Effects
	if effects.Exchanges != 0 || effects.Models != 0 || effects.Operations != 0 {
		return "FAIL", "DISABLED_MEMORY_EFFECT_EXECUTED"
	}
	if effects.Policy != "SERVICE_PROJECT_READ_ONLY.v1" || effects.ReadMode != "PROJECT_SCOPED" || effects.Final == nil ||
		effects.Conversation.Status != "NOT_ATTEMPTED" || effects.Formation.Status != "NOT_ATTEMPTED" || effects.Conversation.Reason != "DISABLED_BY_POLICY" || effects.Formation.Reason != "DISABLED_BY_POLICY" {
		return "INCONCLUSIVE", "MEMORY_EFFECT_POLICY_UNPROVEN"
	}
	if b.Policy == "durable-agent-loop.v1" {
		found := false
		for _, part := range b.Components {
			if part.Kind == "loop" && part.Availability == "AVAILABLE" {
				found = true
				loop, err := DecodeLoopEvidence(part.Body, b.Correlation.Attempt, LoopCorrelation{b.Correlation.EvaluationRun, b.Correlation.Attempt, b.Correlation.Request})
				if err != nil {
					return "ERROR", "LOOP_COMPONENT_INVALID"
				}
				if verdict, reason := LoopIntegrity(loop); verdict != "PASS" {
					return verdict, reason
				}
			}
		}
		if !found {
			return "INCONCLUSIVE", "LOOP_COMPONENT_MISSING"
		}
	} else {
		found := false
		for _, part := range b.Components {
			if part.Kind == "execution" && part.Availability == "AVAILABLE" {
				found = true
				if verdict, reason := executionIntegrity(part.Body, b, o); verdict != "PASS" {
					return verdict, reason
				}
			}
		}
		if !found {
			return "INCONCLUSIVE", "EXECUTION_COMPONENT_MISSING"
		}
	}
	if verdict, reason := budgetIntegrity(o.Budget); verdict != "PASS" {
		return verdict, reason
	}
	seen := map[string]bool{}
	for _, t := range o.Tools {
		if t.State == "NOT_EXECUTED" {
			if t.NotExecuted == nil {
				return "INCONCLUSIVE", "NOT_EXECUTED_UNPROVEN"
			}
			continue
		}
		if t.State != "COMMITTED" || t.Invocation == nil || t.Result == nil {
			return "INCONCLUSIVE", "TOOL_OUTCOME_UNCONFIRMED"
		}
		if seen[*t.Invocation] {
			return "FAIL", "TOOL_INVOCATION_DUPLICATED"
		}
		seen[*t.Invocation] = true
		if !t.ReadOnly && t.EffectProof != "VERIFIED" {
			return "INCONCLUSIVE", "EXTERNAL_EFFECT_UNPROVEN"
		}
	}
	for _, m := range o.Models {
		if !m.Verified || m.State == "UNKNOWN" || m.State == "STARTED" {
			return "INCONCLUSIVE", "MODEL_REQUEST_UNPROVEN"
		}
		if m.InputTokens == nil || m.OutputTokens == nil || m.TotalTokens == nil {
			return "INCONCLUSIVE", "MODEL_USAGE_UNAVAILABLE"
		}
		if *m.InputTokens < 0 || *m.OutputTokens < 0 || *m.TotalTokens < 0 {
			return "ERROR", "MODEL_USAGE_INVALID"
		}
		budgetBound := false
		for _, op := range o.Budget.Operations {
			budgetBound = budgetBound || (op.Ref == m.Budget && op.Kind == "MODEL" && op.State == "SETTLED")
		}
		if !budgetBound {
			return "INCONCLUSIVE", "MODEL_BUDGET_SOURCE_MISSING"
		}
	}
	for _, snapshot := range o.Contexts {
		if snapshot.Kind != "COMPACTED" {
			continue
		}
		bound := false
		charged := false
		for _, comparison := range snapshot.Comparisons {
			if !oneOf(comparison.Mode, "EXACT", "BOUNDED_ESTIMATE") || comparison.Raw-comparison.Compacted != comparison.Delta || comparison.Delta <= 0 || comparison.Cursor < 0 || comparison.Reserve < 1 {
				return "ERROR", "COMPACTION_COMPARISON_INVALID"
			}
			for _, m := range o.Models {
				bound = bound || (m.Snapshot != nil && snapshot.Source == "snapshot:"+*m.Snapshot && m.Request == comparison.CompactedRequest && m.Profile == comparison.Profile)
				charged = charged || (snapshot.Compaction != nil && m.Compaction != nil && *snapshot.Compaction == *m.Compaction && m.Purpose == "COMPACTION" && m.State == "COMPLETED")
			}
		}
		if !bound || !charged {
			return "INCONCLUSIVE", "COMPACTION_COMPARISON_UNPROVEN"
		}
	}
	return "PASS", "EXECUTION_INTEGRITY_CONFIRMED"
}
func taskQuality(c QualityCase, b QualityBundle, o QualityObservations, answer *asset.JSON) (string, string) {
	uncertain := b.Coverage != "COMPLETE" || o.Coverage != "COMPLETE"
	for _, capability := range c.RequiredCapabilities {
		observed := false
		switch capability {
		case "TOOL":
			observed = len(o.Tools) > 0
		case "BATCH":
			counts := map[string]int{}
			for _, tool := range o.Tools {
				key := fmt.Sprintf("%s:%d", tool.Frame, tool.Iteration)
				counts[key]++
				observed = observed || counts[key] > 1
			}
		case "HANDOFF":
			observed = len(o.Children) > 0
		case "COMPACTION":
			for _, model := range o.Models {
				observed = observed || (model.Purpose == "COMPACTION" && model.State == "COMPLETED")
			}
		case "PROJECT_MEMORY":
			observed = o.Memory != nil && o.Memory.ActualUse && len(o.Memory.Selected) > 0 && o.Memory.Effects != nil && o.Memory.Effects.ReadMode == "PROJECT_SCOPED"
		}
		if !observed {
			if !uncertain {
				return "FAIL", "REQUIRED_CAPABILITY_NOT_EXERCISED"
			}
			uncertain = true
		}
	}
	for _, kind := range c.RequiredEvidence {
		available := false
		for _, part := range b.Components {
			available = available || (part.Kind == kind && part.Availability == "AVAILABLE")
		}
		if !available {
			uncertain = true
		}
	}
	if c.Root.Decision != "" {
		found := false
		for _, m := range o.Models {
			if m.Root && m.AcceptedKind != nil && oneOf(*m.AcceptedKind, "FINAL", "REFUSAL") {
				found = true
				if *m.AcceptedKind != c.Root.Decision {
					return "FAIL", "ROOT_DECISION_MISMATCH"
				}
			}
		}
		if !found {
			uncertain = true
		}
	}
	if c.Tools.Mode == "REQUIRED" {
		byAlias := map[string][]QualityTool{}
		for _, t := range o.Tools {
			if t.Alias == nil {
				uncertain = true
				continue
			}
			byAlias[*t.Alias] = append(byAlias[*t.Alias], t)
		}
		for _, forbidden := range c.Tools.Forbidden {
			if len(byAlias[forbidden]) > 0 {
				return "FAIL", "FORBIDDEN_TOOL_OBSERVED"
			}
		}
		for _, r := range c.Tools.Required {
			actual := byAlias[r.Alias]
			if len(actual) != r.Count {
				if !uncertain {
					return "FAIL", "REQUIRED_TOOL_COUNT_MISMATCH"
				}
				continue
			}
			for _, t := range actual {
				if t.State != "COMMITTED" {
					uncertain = true
					continue
				}
				if r.Content != nil {
					if t.Content == nil {
						uncertain = true
					} else if *r.Content != *t.Content {
						return "FAIL", "TOOL_RESULT_MISMATCH"
					}
				}
				if r.ExecutionStatus != nil {
					if t.ExecutionStatus == nil {
						uncertain = true
					} else if *r.ExecutionStatus != *t.ExecutionStatus {
						return "FAIL", "TOOL_EXECUTION_STATUS_MISMATCH"
					}
				}
				if r.FollowUp {
					used := false
					if t.Observation != nil {
						for _, m := range o.Models {
							if m.Frame != nil && *m.Frame == t.Frame && m.AcceptedKind != nil {
								for _, s := range m.Sources {
									used = used || (s.Digest != nil && *s.Digest == *t.Observation)
								}
							}
						}
					}
					if !used {
						uncertain = true
					}
				}
				if r.Arguments != nil {
					if t.Arguments == nil {
						uncertain = true
					} else if *r.Arguments != *t.Arguments {
						return "FAIL", "TOOL_ARGUMENTS_MISMATCH"
					}
				}
				for _, constraint := range r.Parameters {
					found := false
					for _, p := range t.Parameters {
						if p.Path != constraint.Path {
							continue
						}
						found = true
						if !p.Supported {
							uncertain = true
							continue
						}
						if constraint.Equals.String() != "null" && p.Value.String() != constraint.Equals.String() {
							return "FAIL", "TOOL_PARAMETER_MISMATCH"
						}
						if constraint.Min != nil || constraint.Max != nil {
							var n int64
							if p.Value.Decode(&n) != nil {
								uncertain = true
							} else if constraint.Min != nil && n < *constraint.Min || constraint.Max != nil && n > *constraint.Max {
								return "FAIL", "TOOL_PARAMETER_BOUND_VIOLATED"
							}
						}
						if len(constraint.Set) > 0 {
							inside := false
							for _, v := range constraint.Set {
								inside = inside || v.String() == p.Value.String()
							}
							if !inside {
								return "FAIL", "TOOL_PARAMETER_SET_VIOLATED"
							}
						}
					}
					if !found {
						uncertain = true
					}
				}
			}
		}
		if len(c.Tools.Order) > 0 {
			// Observation 顺序只能证明同 Frame 的接受次序，不能声明并行工具起止顺序。
			cursor := 0
			frame := ""
			for _, alias := range c.Tools.Order {
				found := false
				for i := cursor; i < len(o.Tools); i++ {
					t := o.Tools[i]
					if t.Alias != nil && *t.Alias == alias {
						if frame != "" && frame != t.Frame {
							uncertain = true
						}
						frame = t.Frame
						cursor = i + 1
						found = true
						break
					}
				}
				if !found && !uncertain {
					return "FAIL", "TOOL_ACCEPT_ORDER_MISMATCH"
				}
			}
		}
	}
	if c.Handoff.Mode == "REQUIRED" {
		matched := false
		for _, d := range o.Children {
			if c.Handoff.Target != nil && (d.Target == nil || *d.Target != *c.Handoff.Target) {
				continue
			}
			matched = true
			if !d.Verified || !d.Accepted {
				uncertain = true
			}
			if c.Handoff.Result != nil && d.Content != nil && *c.Handoff.Result != *d.Content {
				return "FAIL", "CHILD_RESULT_MISMATCH"
			}
			if c.Handoff.Result != nil && d.Content == nil {
				uncertain = true
			}
			if c.Handoff.ParentConsumed && (d.ParentModel == nil || d.ParentObservation == nil) {
				uncertain = true
			}
		}
		if !matched && !uncertain {
			return "FAIL", "HANDOFF_TARGET_MISSING"
		}
	}
	if c.Context.Mode == "REQUIRED" {
		var profile QualityProfile
		if b.Profile.Decode(&profile) != nil {
			uncertain = true
		}
		for _, wanted := range c.Context.Facts {
			supported := false
			for _, key := range profile.FactKeys {
				supported = supported || key == wanted.Key
			}
			if !supported {
				uncertain = true
				continue
			}
			found := false
			complete := false
			for _, fact := range o.Facts {
				if fact.Key == wanted.Key && hasSourceKind(fact.Source, wanted.Source) {
					complete = true
					found = found || fact.Digest == wanted.Digest
				}
			}
			if !found {
				for _, m := range o.Models {
					if m.AcceptedKind != nil && m.ScanComplete && m.Verified {
						if wanted.Source == "actual-request" {
							complete = true
						}
						if wanted.Source == "compacted-request" && m.Snapshot != nil {
							for _, s := range o.Contexts {
								complete = complete || (s.Source == "snapshot:"+*m.Snapshot && s.Kind == "COMPACTED")
							}
						}
					}
				}
				if complete && !uncertain {
					return "FAIL", "CRITICAL_FACT_MISMATCH"
				}
				uncertain = true
			}
		}
	}
	if c.Memory.Mode == "REQUIRED" {
		if c.Memory.Private == "REQUIRED" || c.Memory.Conversation == "REQUIRED" {
			uncertain = true
		}
		if o.Memory == nil || !o.Memory.ActualUse {
			uncertain = true
		} else {
			for _, record := range o.Memory.Selected {
				if record.Epistemic == nil && len(c.Memory.Epistemic) > 0 {
					uncertain = true
					continue
				}
				allowed := len(c.Memory.Epistemic) == 0
				for _, kind := range c.Memory.Epistemic {
					allowed = allowed || (record.Epistemic != nil && *record.Epistemic == kind)
				}
				if !allowed {
					return "FAIL", "MEMORY_EPISTEMIC_KIND_FORBIDDEN"
				}
			}
			for _, r := range c.Memory.Required {
				found := false
				for _, a := range o.Memory.Selected {
					found = found || a.Ref == r
				}
				if !found {
					return "FAIL", "REQUIRED_MEMORY_MISSING"
				}
			}
			for _, r := range c.Memory.Forbidden {
				for _, a := range o.Memory.Selected {
					if a.Ref == r {
						return "FAIL", "FORBIDDEN_MEMORY_USED"
					}
				}
			}
		}
		if c.Memory.Formation != "NOT_REQUESTED" {
			uncertain = true
		}
	}
	if c.Answer.Mode == "REQUIRED" {
		if answer == nil {
			uncertain = true
		} else {
			var text string
			if answer.Decode(&text) != nil {
				return "ERROR", "ANSWER_ARTIFACT_INVALID"
			}
			if fmt.Sprintf("%x", sha256.Sum256([]byte(text))) != *c.Answer.Digest {
				return "FAIL", "FINAL_ANSWER_MISMATCH"
			}
		}
	}
	if uncertain {
		return "INCONCLUSIVE", "BUSINESS_EVIDENCE_INCOMPLETE"
	}
	return "PASS", "TASK_EXPECTATIONS_CONFIRMED"
}
func hasSourceKind(source, kind string) bool {
	return len(source) > len(kind) && source[:len(kind)+1] == kind+":"
}

func evaluateBehavior(ctx context.Context, in worker.EvaluationInput) (worker.EvaluationOutput, error) {
	if err := ctx.Err(); err != nil {
		return worker.EvaluationOutput{}, err
	}
	var cfg Config
	_ = in.Work.Metadata.Spec.Definition.Config.Decode(&cfg)
	value := BaseValue(in, "INCONCLUSIVE", "QUALITY_EVIDENCE_MISSING")
	var b QualityBundle
	var o QualityObservations
	count := 0
	for _, binding := range in.Attempt.Metadata.Observation.Evidence {
		if binding.Schema != QualitySchema {
			continue
		}
		count++
		if !bound(binding, in) || binding.Availability != "AVAILABLE" {
			continue
		}
		var input struct {
			Agent string `json:"agent_id"`
			Query string `json:"query"`
		}
		var config map[string]asset.JSON
		var profile QualityProfile
		request := in.Attempt.Request
		if request.Case.Case.Input.Decode(&input) != nil || request.Target.Config.Decode(&config) != nil ||
			StrictQualityShape(config["capture_profile"].Bytes(), &profile) != nil {
			value = BaseValue(in, "ERROR", "QUALITY_BUNDLE_INVALID")
			continue
		}
		expected := QualityCorrelation{Agent: input.Agent, Run: in.Attempt.ID, Attempt: in.Attempt.ID,
			Request: in.Attempt.RequestID, EvaluationRun: in.Attempt.RunID, Project: in.Scope.ProjectID,
			Case: request.Case.Identity.Ref.EntityID, CaseVersion: request.Case.Identity.Ref.Version,
			CaseDigest: request.Case.Identity.ContentDigest, ManifestDigest: config["execution_manifest"].Digest(),
			ProfileID: profile.ID, ProfileVersion: profile.Version, ProfileDigest: config["capture_profile"].Digest(),
			Timeout: request.Target.TimeoutMilliseconds, QueryDigest: fmt.Sprintf("%x", sha256.Sum256([]byte(input.Query)))}
		var err error
		b, o, err = DecodeQuality(binding.Body, expected)
		if err != nil {
			value = BaseValue(in, "ERROR", "QUALITY_BUNDLE_INVALID")
			continue
		}
		verdict, reason := integrityQuality(b, o)
		if cfg.Metric == "task_success.v1" {
			c, err := ReadQualityCase(in.Attempt.Request.Case.Case.GroundTruth)
			if err != nil {
				verdict, reason = "ERROR", "QUALITY_GROUND_TRUTH_INVALID"
			} else {
				var answer *asset.JSON
				if artifact := in.Attempt.Metadata.Observation.Artifact; artifact != nil && bound(*artifact, in) && artifact.Availability == "AVAILABLE" {
					answer = &artifact.Body
				}
				if verdict == "ERROR" || verdict == "FAIL" {
					verdict, reason = "INCONCLUSIVE", "EXECUTION_INTEGRITY_UNPROVEN"
				} else {
					integrity := verdict
					verdict, reason = taskQuality(c, b, o, answer)
					if verdict == "PASS" && integrity != "PASS" {
						verdict, reason = "INCONCLUSIVE", "EXECUTION_INTEGRITY_UNPROVEN"
					}
				}
			}
		}
		value = BaseValue(in, verdict, reason)
	}
	if count != 1 {
		value = BaseValue(in, "INCONCLUSIVE", "QUALITY_EVIDENCE_MISSING")
	}
	if oneOf(value.Verdict, "PASS", "FAIL") {
		score := 0.0
		if value.Verdict == "PASS" {
			score = 1
		}
		value.Score = &score
	}
	applicability := asset.Applicable
	if value.Verdict == "INCONCLUSIVE" {
		applicability = asset.MissingEvidence
	}
	provenance := map[string]any{"metric": cfg.Metric, "implementation_ref": BehaviorImplementation, "bundle_schema": QualitySchema, "attribution": value.SourceError,
		"applicability": applicability, "capture_profile_digest": b.ProfileDigest, "execution_manifest_digest": b.ManifestDigest}
	if cfg.Metric == "task_success.v1" {
		decision := TaskInconclusive
		if value.Verdict == "PASS" {
			decision = TaskSuccess
		} else if value.Verdict == "FAIL" {
			decision = TaskFailure
		}
		provenance["task_success"] = TaskJudgment{Version: "task_success.v1", Decision: decision, Method: "DETERMINISTIC", Reason: value.Reason, ProducerRef: BehaviorImplementation, EvidenceRefs: []string{b.ID}}
	}
	value.Provenance, _ = asset.Freeze(provenance)
	return worker.EvaluationOutput{Value: value}, nil
}
