package evaluation

import "agentevalops/go-backend/internal/asset"

// validateAgentQualityFreeze 只约束新 Target，不改变旧执行协议或旧 Run。
func validateAgentQualityFreeze(c CreateRun, in SnapshotInput, project string) error {
	if c.Target.ID != "localagent-agent-quality-http" {
		return nil
	}
	if c.Target.Version != "agent-quality-evaluation.v1" || c.Target.Kind != "LOCALAGENT_HTTP" || c.Retry.Version != "NO_RETRY.v1" ||
		c.Retry.MaxAttempts != 1 || len(c.Retry.Allowed) != 0 || c.Retry.AllowUnknownFork {
		return asset.ErrInvalid
	}
	var config map[string]asset.JSON
	var subject struct {
		Subject     string `json:"subject"`
		Version     string `json:"subject_version"`
		Environment string `json:"environment"`
		RunMode     string `json:"run_mode"`
	}
	if c.Target.Config.Decode(&config) != nil || c.Subject.Decode(&subject) != nil || subject.Version != config["execution_manifest"].Digest() {
		return asset.ErrInvalid
	}
	var trustedProject string
	if config["trusted_project_id"].Decode(&trustedProject) != nil || trustedProject != project {
		return asset.ErrForbidden
	}
	var manifest struct {
		Schema       string                `json:"schema_version"`
		Build        string                `json:"runtime_build_digest"`
		Fixture      string                `json:"public_fixture_version"`
		MemoryEffect string                `json:"memory_effect_policy"`
		Agents       map[string]asset.JSON `json:"agents"`
	}
	if config["execution_manifest"].Decode(&manifest) != nil || manifest.Schema != "agent-quality-execution-manifest.v1" || manifest.Agents[subject.Subject].String() == "null" {
		return asset.ErrInvalid
	}
	required := map[string]bool{"agent_execution_integrity.v1": false, "task_success.v1": false}
	for _, e := range in.Evaluators {
		if e.Definition.ImplementationRef != "agent-behavior-deterministic.v1" || !e.Required {
			continue
		}
		var cfg struct {
			Metric string `json:"metric"`
			K      int    `json:"k"`
		}
		if e.Definition.Config.Decode(&cfg) == nil {
			if _, ok := required[cfg.Metric]; ok {
				required[cfg.Metric] = true
			}
		}
	}
	for _, present := range required {
		if !present {
			return asset.ErrInvalid
		}
	}
	for _, item := range in.Manifest {
		var input struct {
			Agent string `json:"agent_id"`
			Query string `json:"query"`
		}
		var gt map[string]asset.JSON
		var schema string
		if item.Case.Capability != "AGENT_QUALITY" || item.Case.Input.Decode(&input) != nil || input.Agent != subject.Subject ||
			item.Case.GroundTruth.Decode(&gt) != nil || gt["schema_version"].Decode(&schema) != nil || schema != "agent-quality-case.v1" {
			return asset.ErrInvalid
		}
	}
	return nil
}
