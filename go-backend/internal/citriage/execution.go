package citriage

import (
	"agentevalops/go-backend/internal/asset"
	"encoding/json"

	"time"
)

const ExecutionPolicyVersion = "stage13.attempt-execution.v1"

type ExecutionPolicy struct {
	Version        string    `json:"version"`
	RunID          string    `json:"evaluation_run_id"`
	AttemptID      string    `json:"evaluation_attempt_id"`
	TimeoutSeconds int       `json:"timeout_seconds"`
	StartedAt      time.Time `json:"execution_started_at"`
	DeadlineAt     time.Time `json:"execution_deadline_at"`
	SemanticDigest string    `json:"semantic_input_digest"`
}

// MaterializeStage13 使用 Kernel 已冻结的 started_at；重放不读取当前时钟。
func MaterializeExecution(caseInput, manifest asset.JSON, runID, attemptID string, timeoutMilliseconds int64, started time.Time) (asset.JSON, error) {
	if started.IsZero() || timeoutMilliseconds != 180000 {
		return asset.JSON{}, asset.ErrInvalid
	}
	semantic, err := SemanticInput(caseInput)
	if err != nil {
		return asset.JSON{}, err
	}
	p := ExecutionPolicy{ExecutionPolicyVersion, runID, attemptID, 180, started.UTC(), started.UTC().Add(180 * time.Second), semantic.Digest()}
	var input, ep map[string]asset.JSON
	_ = semantic.Decode(&input)
	_ = input["evidence_policy"].Decode(&ep)
	ep["deadline_at"], _ = asset.Freeze(p.DeadlineAt.Format(time.RFC3339Nano))
	input["evidence_policy"], _ = asset.Freeze(ep)
	query, err := asset.Freeze(input)
	if err != nil {
		return asset.JSON{}, err
	}
	var m struct {
		AgentID string `json:"agent_id"`
	}
	if json.Unmarshal(manifest.Bytes(), &m) != nil {
		return asset.JSON{}, asset.ErrInvalid
	}
	return asset.Freeze(map[string]any{"run_id": attemptID, "agent_id": m.AgentID, "query": query.String(), "timeout_seconds": 180, "expected_subject_manifest": manifest, "execution_policy": p})
}
