package agentquality

import (
	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/catalog"
	"crypto/sha256"
	"fmt"
)

// PublicQualityCases 是公开合成任务。GT 由 Go 单独发布，不进入 Agent 输入。
// Memory 引用由独立有权的 Fixture 准备者提供，在 EvaluationRun 创建前冻结。
func PublicQualityCases(requiredMemory, forbiddenMemory string) ([]catalog.CaseContent, error) {
	result := []catalog.CaseContent{}
	emptyArgs, _ := asset.Freeze(map[string]any{})
	digest := func(text string) *string { value := fmt.Sprintf("%x", sha256.Sum256([]byte(text))); return &value }
	for _, name := range []string{"loop", "batch", "handoff", "compaction", "memory"} {
		gt := QualityCase{Schema: "agent-quality-case.v1", RequiredEvidence: []string{"execution"}, RequiredCapabilities: []string{}}
		gt.Root.Decision = "FINAL"
		gt.Root.Outcome = "SUCCEEDED"
		gt.Tools.Mode = "NOT_REQUESTED"
		gt.Tools.Required = []ToolExpectation{}
		gt.Tools.Forbidden = []string{}
		gt.Tools.Order = []string{}
		gt.Handoff.Mode = "NOT_REQUESTED"
		gt.Context.Mode = "NOT_REQUESTED"
		gt.Context.Facts = []FactExpectation{}
		gt.Memory.Mode = "NOT_REQUESTED"
		gt.Memory.Required = []string{}
		gt.Memory.Forbidden = []string{}
		gt.Memory.Epistemic = []string{}
		gt.Memory.Formation = "NOT_REQUESTED"
		gt.Memory.Conversation = "NOT_REQUESTED"
		gt.Memory.Private = "NOT_REQUESTED"
		gt.Answer.Mode = "REQUIRED"
		expected := "public " + name + " result"
		tool := func(alias, args string, count int, follow bool) ToolExpectation {
			status := "SUCCEEDED"
			return ToolExpectation{Alias: alias, Arguments: &args, Count: count, FollowUp: follow, Parameters: []ParameterConstraint{}, Content: digest("public read result"), ExecutionStatus: &status}
		}
		switch name {
		case "loop":
			gt.RequiredCapabilities = []string{"TOOL"}
			gt.Tools.Mode = "REQUIRED"
			gt.Tools.Required = []ToolExpectation{tool("read_a", emptyArgs.Digest(), 1, true), tool("read_b", emptyArgs.Digest(), 1, true)}
			gt.Tools.Order = []string{"read_a", "read_b"}
		case "batch":
			gt.RequiredCapabilities = []string{"BATCH"}
			gt.Tools.Mode = "REQUIRED"
			gt.Tools.Required = []ToolExpectation{tool("read_a", emptyArgs.Digest(), 1, true), tool("read_b", emptyArgs.Digest(), 1, true)}
		case "handoff":
			gt.RequiredCapabilities = []string{"HANDOFF"}
			gt.Handoff.Mode = "REQUIRED"
			target, _ := asset.Freeze("reader")
			targetDigest := target.Digest()
			gt.Handoff.Target = &targetDigest
			gt.Handoff.Result = digest("public child result")
			gt.Handoff.ParentConsumed = true
			expected = "public parent result"
		case "compaction":
			gt.RequiredCapabilities = []string{"COMPACTION"}
			gt.Context.Mode = "REQUIRED"
			value, _ := asset.Freeze("AB7")
			gt.Context.Facts = []FactExpectation{{Key: "route_code", Digest: value.Digest(), Source: "compacted-request"}}
			expected = "public compaction final"
		case "memory":
			if !loopDigest.MatchString(requiredMemory) || !loopDigest.MatchString(forbiddenMemory) {
				return nil, asset.ErrInvalid
			}
			gt.RequiredCapabilities = []string{"PROJECT_MEMORY"}
			gt.Memory.Mode = "REQUIRED"
			gt.Memory.Required = []string{requiredMemory}
			gt.Memory.Forbidden = []string{forbiddenMemory}
			gt.Memory.Epistemic = []string{"USER_ASSERTION", "OBSERVATION"}
			value, _ := asset.Freeze("ZX9")
			gt.Context.Mode = "REQUIRED"
			gt.Context.Facts = []FactExpectation{{Key: "delivery_code", Digest: value.Digest(), Source: "actual-request"}}
			expected = "delivery_code=ZX9"
		}
		gt.Answer.Digest = digest(expected)
		groundTruth, err := asset.Freeze(gt)
		if err != nil {
			return nil, err
		}
		if _, err = ReadQualityCase(groundTruth); err != nil {
			return nil, err
		}
		input, _ := asset.Freeze(map[string]any{"agent_id": "core_router", "query": "public loop_a loop_b quality case=" + name})
		c := catalog.CaseContent{Input: input, TaskGoal: "完成公开合成 " + name + " 任务", AcceptanceCriteria: []string{"按任务使用工具和已授权来源，交付最终回答"},
			ExpectedOutput: asset.JSON{}, Assertions: []catalog.Assertion{}, GroundTruth: groundTruth, Applicability: asset.Applicability{RuleRef: "agent-quality-case.v1"},
			Type: catalog.AgentTask, Capability: "AGENT_QUALITY", Criticality: catalog.Critical, Tags: []string{"public", "synthetic", name}, BodyPolicy: catalog.Retained}
		if err = c.Validate(); err != nil {
			return nil, err
		}
		result = append(result, c)
	}
	return result, nil
}
