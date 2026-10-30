package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"agentevalops/go-backend/internal/decision"
	ev "agentevalops/go-backend/internal/evaluation"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writePublicAudit(t *testing.T, f IndependentAuditFiles, v any) string {
	t.Helper()
	j := diagnosisJSON(v)
	_, e := decision.WriteIndependentArtifact(filepath.Join(f.Directory, j.Digest()+".json"), v)
	if e != nil {
		t.Fatal(e)
	}
	return j.Digest()
}
func publicComparability(t *testing.T, mutation string) (IndependentAuditFiles, decision.FrozenPlan, decision.PlannedCase, decision.BranchReceiptRefs, decision.Snapshot) {
	t.Helper()
	f := IndependentAuditFiles{Directory: t.TempDir(), Authority: "public-auditor"}
	p := decision.FrozenPlan{ID: asset.NewID(), ProjectID: asset.NewID(), CandidateArtifactDigest: diagnosisHash("candidate"), FrozenAt: time.Now().UTC().Add(-time.Hour)}
	c := decision.PlannedCase{Case: decision.FrozenAssetRef{Ref: asset.Ref{EntityID: asset.NewID(), Version: "v1"}}, Policy: decision.FrozenAssetRef{Ref: asset.Ref{EntityID: asset.NewID(), Version: "v1"}, Digest: diagnosisHash("policy")}, Lineage: catalog.CIDiagnosisMetadata{Developer: "public-developer"}}

	raw, err := os.ReadFile("testdata/ci_prompt_derivation.json")
	if err != nil {
		t.Fatal(err)
	}
	vectorJSON, err := asset.ParseJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	var vector struct {
		Schema    string `json:"schema_version"`
		Producer  string `json:"producer"`
		Candidate string `json:"candidate_id"`
		Manifest  string `json:"candidate_manifest_digest"`
		Sides     []struct {
			Mode             string     `json:"mode"`
			Definition       asset.JSON `json:"definition"`
			Runtime          asset.JSON `json:"runtime"`
			DefinitionDigest string     `json:"definition_digest"`
			RuntimeDigest    string     `json:"runtime_digest"`
			PromptDigest     string     `json:"prompt_digest"`
		} `json:"sides"`
	}
	if vectorJSON.Decode(&vector) != nil || len(vector.Sides) != 2 {
		t.Fatal("public Producer vector")
	}
	p.CandidateArtifactDigest = vector.Manifest
	configurations := []ComparableConfiguration{}
	for i, side := range vector.Sides {
		if side.Definition.Digest() != side.DefinitionDigest || side.Runtime.Digest() != side.RuntimeDigest {
			t.Fatal("Python Producer digest differs from Go canonical digest")
		}
		var runtime, definition, options, selection map[string]asset.JSON
		_ = side.Runtime.Decode(&runtime)
		_ = side.Definition.Decode(&definition)
		_ = definition["business_options"].Decode(&options)
		_ = options["ci_prompt_selection"].Decode(&selection)
		if i == 1 {
			switch mutation {
			case "tools":
				runtime["tools"] = diagnosisJSON([]string{"unauthorized_write"})
			case "budget":
				options["loop_policy"] = diagnosisJSON(map[string]any{"max_model_calls": 100})
			case "context":
				runtime["context_policy"] = diagnosisJSON(map[string]string{"mode": "unbounded"})
			case "model":
				runtime["generation_options"] = diagnosisJSON(map[string]int{"max_tokens": 99999})
			case "security-rule":
				var prompt string
				_ = runtime["security_prompt"].Decode(&prompt)
				runtime["security_prompt"] = diagnosisJSON(strings.Replace(prompt, "Runtime Governance", "Model Choice", 1))
			case "security-extra":
				var prompt string
				_ = runtime["security_prompt"].Decode(&prompt)
				runtime["security_prompt"] = diagnosisJSON(prompt + "\n额外未批准规则")
			case "mode-definition":
				selection["mode"] = diagnosisJSON("BASELINE")
			case "selection-prompt":
				selection["prompt_digest"] = diagnosisJSON(diagnosisHash("wrong prompt"))
			case "selection-manifest":
				selection["manifest_digest"] = diagnosisJSON(diagnosisHash("wrong manifest"))
			case "selection-schema":
				selection["schema_version"] = diagnosisJSON("ci-prompt-selection.unknown")
			case "selection-candidate":
				selection["candidate_id"] = diagnosisJSON("ci-prompt-" + diagnosisHash("other candidate"))
			}
		}
		options["ci_prompt_selection"] = diagnosisJSON(selection)
		definition["business_options"] = diagnosisJSON(options)
		config := ComparableConfiguration{Definition: diagnosisJSON(definition)}
		runtime["agent"] = config.Definition
		if i == 1 && mutation == "agent-definition" {
			var a map[string]asset.JSON
			_ = config.Definition.Decode(&a)
			a["role"] = diagnosisJSON("未经批准的角色")
			runtime["agent"] = diagnosisJSON(a)
		}
		config.Runtime = diagnosisJSON(runtime)
		manifest := diagnosisJSON(QualityManifest{Build: diagnosisHash("build"), Agents: map[string]QualityAgentPin{"ci_incident_investigator": {DefinitionDigest: config.Definition.Digest(), ConfigDigest: config.Runtime.Digest()}}})
		profile := diagnosisJSON(map[string]string{"public": "profile"})
		mode := side.Mode
		if i == 1 && mutation == "mode-target" {
			mode = "BASELINE"
		}
		targetConfig := map[string]any{"quality": map[string]any{"execution_manifest": manifest, "capture_profile": profile}, "mode": mode, "prompt_digest": side.PromptDigest, "candidate_id": vector.Candidate, "candidate_manifest_digest": vector.Manifest}
		if i == 1 && mutation == "target-prompt" {
			targetConfig["prompt_digest"] = diagnosisHash("wrong prompt")
		}
		config.Target = diagnosisJSON(ev.Target{ID: "ci-synthetic-http", Version: "ci-synthetic-target.v2", Config: diagnosisJSON(targetConfig)})
		pin := decision.ExecutionPin{RuntimeBuild: diagnosisHash("build"), RuntimeConfig: config.Runtime.Digest(), Definition: config.Definition.Digest(), Manifest: manifest.Digest(), CaptureProfile: profile.Digest(), TargetDigest: config.Target.Digest(), Prompt: side.PromptDigest}
		if i == 0 {
			c.Baseline = pin
		} else {
			c.Candidate = pin
		}
		configurations = append(configurations, config)
	}
	p.Cases = []decision.PlannedCase{c}
	sum := diagnosisJSON(p).Digest()
	audit := ComparabilityAudit{Schema: "ci-comparability-audit.v1", PlanDigest: sum, Candidate: p.CandidateArtifactDigest, Project: p.ProjectID, Reviewer: f.Authority, Case: c.Case.Ref, Policy: c.Policy.Ref, PolicyDigest: c.Policy.Digest, Baseline: c.Baseline, CandidatePin: c.Candidate, BaselineContent: configurations[0], CandidateContent: configurations[1], ReviewedAt: p.FrozenAt.Add(time.Minute)}
	diffs := configurationDifferences("Runtime", configurations[0].Runtime, configurations[1].Runtime)
	td := configurationDifferences("Target", configurations[0].Target, configurations[1].Target)
	// 非批准差异也如实声明，不能因声明本身就放行。
	diffs = append(diffs, configurationDifferences("Definition", configurations[0].Definition, configurations[1].Definition)...)
	for _, d := range td {
		diffs = append(diffs, strings.Replace(d, "Target.Config.", "Target.", 1))
	}
	audit.AllowedPaths = diffs
	refs := decision.BranchReceiptRefs{ComparabilityAuditDigest: writePublicAudit(t, f, audit)}
	return f, p, c, refs, decision.Snapshot{CapturedAt: time.Now().UTC()}
}
func TestIndependentTrustedComparability(t *testing.T) {
	for _, mutation := range []string{"", "instructions", "tools", "budget", "context", "model", "security-rule", "security-extra", "mode-definition", "mode-target", "selection-prompt", "target-prompt", "selection-manifest", "selection-schema", "selection-candidate", "agent-definition", "missing"} {
		t.Run(mutation, func(t *testing.T) {
			f, p, c, refs, s := publicComparability(t, mutation)
			var actual ComparabilityAudit
			if f.read(refs.ComparabilityAuditDigest, &actual) != nil || actual.BaselineContent.Runtime.Digest() != c.Baseline.RuntimeConfig || actual.CandidateContent.Runtime.Digest() != c.Candidate.RuntimeConfig || actual.CandidateContent.Definition.Digest() != c.Candidate.Definition || actual.CandidateContent.Target.Digest() != c.Candidate.TargetDigest {
				t.Fatal("each side must match its own actual pin")
			}
			if mutation == "missing" {
				refs.ComparabilityAuditDigest = diagnosisHash("absent")
			}
			err := (verifiedIndependentReader{Audits: f}).VerifyIndependentPair(asset.Scope{ProjectID: p.ProjectID}, p, c, refs, s, nil)
			if (err == nil) != (mutation == "" || mutation == "instructions") {
				t.Fatal("untrusted behavior difference admitted", mutation, err)
			}
		})
	}
}
func publicHoldoutAudits(t *testing.T) (IndependentAuditFiles, decision.FrozenPlan, decision.PlannedCase, decision.BranchReceiptRefs, HoldoutAudit) {
	t.Helper()
	f := IndependentAuditFiles{Directory: t.TempDir(), Authority: "public-auditor"}
	freeze := time.Now().UTC().Add(-time.Hour)
	p := decision.FrozenPlan{ID: asset.NewID(), ProjectID: asset.NewID(), CandidateArtifactDigest: diagnosisHash("candidate"), FrozenAt: freeze}
	c := decision.PlannedCase{Purpose: "HOLDOUT", Dataset: decision.FrozenAssetRef{Ref: asset.Ref{EntityID: asset.NewID(), Version: "v1"}}, Case: decision.FrozenAssetRef{Ref: asset.Ref{EntityID: asset.NewID(), Version: "v1"}}, Lineage: catalog.CIDiagnosisMetadata{Operator: "public-operator", Developer: "public-developer", SourceDigest: diagnosisHash("source"), InputDigest: diagnosisHash("input"), FamilyDigest: diagnosisHash("family")}}
	acl := HoldoutACLAudit{Schema: "ci-holdout-acl-audit.v1", Project: p.ProjectID, Operator: c.Lineage.Operator, Reviewer: f.Authority, State: "APPROVED", Boundary: "INDEPENDENT_OPERATOR_ACCOUNT", Dataset: c.Dataset.Ref, Case: c.Case.Ref, ReviewedAt: freeze.Add(-time.Minute)}
	c.Lineage.ACLDigest = writePublicAudit(t, f, acl)
	pre := HoldoutAudit{Schema: "ci-holdout-audit.v1", Candidate: p.CandidateArtifactDigest, Project: p.ProjectID, Operator: c.Lineage.Operator, Reviewer: f.Authority, Dataset: c.Dataset.Ref, Case: c.Case.Ref, SourceDigest: c.Lineage.SourceDigest, InputDigest: c.Lineage.InputDigest, FamilyDigest: c.Lineage.FamilyDigest, ACLDigest: c.Lineage.ACLDigest, Exposure: "UNEXPOSED", State: "APPROVED", FrozenAt: freeze.Add(-time.Hour), ReviewedAt: freeze.Add(-time.Minute)}
	c.Lineage.AuditDigest = writePublicAudit(t, f, pre)
	p.Cases = []decision.PlannedCase{c}
	post := pre
	post.PlanDigest = diagnosisJSON(p).Digest()
	post.SourceAuditDigest = c.Lineage.AuditDigest
	post.ApprovedAt = freeze.Add(time.Minute)
	return f, p, c, decision.BranchReceiptRefs{HoldoutAuditDigest: writePublicAudit(t, f, post)}, post
}
func TestIndependentHoldoutAuditAndDedup(t *testing.T) {
	for _, mutation := range []string{"", "missing", "plan", "candidate", "project", "acl", "identity"} {
		t.Run(mutation, func(t *testing.T) {
			f, p, c, refs, post := publicHoldoutAudits(t)
			switch mutation {
			case "missing":
				refs.HoldoutAuditDigest = diagnosisHash("absent")
			case "plan":
				post.PlanDigest = diagnosisHash("wrong")
			case "candidate":
				post.Candidate = diagnosisHash("wrong")
			case "project":
				post.Project = asset.NewID()
			case "acl":
				post.ACLDigest = diagnosisHash("wrong")
			case "identity":
				f.Authority = "unapproved-auditor"
			}
			if mutation != "" && mutation != "missing" && mutation != "identity" {
				refs.HoldoutAuditDigest = writePublicAudit(t, f, post)
			}
			if err := f.holdout(p, c, refs); (err == nil) != (mutation == "") {
				t.Fatal("invalid audit admitted", mutation, err)
			}
		})
	}
	for _, kind := range []string{"source", "input", "failure-family", "golden-source"} {
		t.Run(kind, func(t *testing.T) {
			h := catalog.CIDiagnosisMetadata{Version: catalog.CIDiagnosisMetadataVersion, Purpose: "HOLDOUT", SourceDigest: diagnosisHash("source"), InputDigest: diagnosisHash("input"), FamilyDigest: diagnosisHash("family"), GroundTruthDigest: diagnosisHash("gt"), ReviewID: asset.NewID(), Reviewer: "reviewer", Developer: "dev", Operator: "operator", AuditDigest: diagnosisHash("audit"), ACLDigest: diagnosisHash("acl"), Exposure: "UNEXPOSED"}
			other := h
			other.SourceDigest = diagnosisHash("other source")
			other.InputDigest = diagnosisHash("other input")
			other.FamilyDigest = diagnosisHash("other family")
			switch kind {
			case "source":
				other.SourceDigest = h.SourceDigest
			case "input":
				other.InputDigest = h.InputDigest
			case "failure-family":
				other.Purpose = "FAILURE"
				other.FamilyDigest = h.FamilyDigest
			case "golden-source":
				other.Purpose = "GOLDEN"
				other.SourceDigest = h.SourceDigest
			}
			if catalog.ValidateCIDiagnosisHoldoutSet(asset.NewID(), asset.Ref{EntityID: asset.NewID(), Version: "v1"}, asset.Ref{EntityID: asset.NewID(), Version: "v1"}, h, []catalog.CIDiagnosisMetadata{other}) == nil {
				t.Fatal("duplicate source/input/family accepted")
			}
		})
	}
}
