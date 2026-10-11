package citriage

import (
	"time"

	"agentevalops/go-backend/internal/asset"
)

const ExecutionPolicyV2 = "stage13.attempt-execution.v2"
const ReceiptV2 = "localagent-ci-triage-receipt.v2"

type ContractBinding struct {
	InputVersion          string `json:"input_contract_version"`
	InputDigest           string `json:"input_contract_digest"`
	ResponsibilityVersion string `json:"responsibility_contract_version"`
	ResponsibilityDigest  string `json:"responsibility_contract_digest"`
	ProjectionVersion     string `json:"projection_version"`
	ProjectionDigest      string `json:"projection_digest"`
}

func FrozenContractBinding() *ContractBinding {
	var b ContractBinding
	_ = contractJSON("identities.json").Decode(&b)
	return &b
}
func HasContractIdentity(j asset.JSON) bool {
	var m map[string]asset.JSON
	_ = j.Decode(&m)
	for key := range ContractIdentities() {
		if _, ok := m[key]; ok {
			return true
		}
	}
	return false
}

func materializeExecutionV2(input, manifest asset.JSON, runID, attemptID string, timeout int64, started time.Time) (asset.JSON, error) {
	if started.IsZero() || timeout != 180000 || !asset.ValidID(runID) || !asset.ValidID(attemptID) || ValidateInputV2(input) != nil || ValidateContractIdentity(manifest) != nil {
		return asset.JSON{}, asset.ErrInvalid
	}
	var m struct {
		Agent  string `json:"agent_id"`
		Schema string `json:"output_schema_digest"`
	}
	_ = manifest.Decode(&m)
	var domain map[string]asset.JSON
	_ = ComparisonDomain().Decode(&domain)
	var schema string
	_ = domain["output_schema_digest"].Decode(&schema)
	if !safe(m.Agent) || m.Schema != schema {
		return asset.JSON{}, asset.ErrInvalid
	}
	source, err := SemanticInput(input)
	if err != nil {
		return asset.JSON{}, err
	}
	projected, err := ProjectInputV2(source)
	if err != nil {
		return asset.JSON{}, err
	}
	p := ExecutionPolicy{Version: ExecutionPolicyV2, RunID: runID, AttemptID: attemptID, TimeoutSeconds: 180, StartedAt: started.UTC(), DeadlineAt: started.UTC().Add(180 * time.Second), SemanticDigest: projected.Digest(), ContractBinding: FrozenContractBinding(), SourceInputDigest: input.Digest(), SourceSemanticDigest: source.Digest(), ProjectedDigest: projected.Digest(), DomainDigest: ComparisonDomain().Digest()}
	var body, policy map[string]asset.JSON
	_ = projected.Decode(&body)
	_ = body["evidence_policy"].Decode(&policy)
	policy["deadline_at"] = freeze(p.DeadlineAt.Format(time.RFC3339Nano))
	body["evidence_policy"] = freeze(policy)
	return asset.Freeze(map[string]any{"run_id": attemptID, "agent_id": m.Agent, "query": freeze(body).String(), "timeout_seconds": 180, "expected_subject_manifest": manifest, "execution_policy": p})
}

// ValidateReceiptV2 核对实际receipt中的合同/原始及投影身份，禁止v2请求用v1回执。
func ValidateReceiptV2(receipt, manifest, policy asset.JSON, requestDigest string) error {
	if ValidateContractIdentity(receipt) != nil || ValidateContractIdentity(manifest) != nil || ValidateContractIdentity(policy) != nil {
		return asset.ErrInvalid
	}
	var r, p map[string]asset.JSON
	_ = receipt.Decode(&r)
	_ = policy.Decode(&p)
	var version, rv string
	_ = p["version"].Decode(&version)
	_ = r["receipt_version"].Decode(&rv)
	if version != ExecutionPolicyV2 || rv != ReceiptV2 || r["execution_policy"].String() != policy.String() || r["actual_subject_manifest"].String() != manifest.String() || !digestID.MatchString(requestDigest) || r["execution_request_digest"].String() != freeze(requestDigest).String() {
		return asset.ErrInvalid
	}
	for _, key := range []string{"source_input_digest", "source_semantic_input_digest", "projected_input_digest", "semantic_input_digest", "comparison_domain_digest"} {
		var s string
		if p[key].Decode(&s) != nil || !digestID.MatchString(s) || r[key].String() != p[key].String() {
			return asset.ErrInvalid
		}
	}
	if p["comparison_domain_digest"].String() != freeze(ComparisonDomain().Digest()).String() || p["semantic_input_digest"].String() != p["projected_input_digest"].String() || p["source_semantic_input_digest"].String() != p["projected_input_digest"].String() {
		return asset.ErrInvalid
	}
	return nil
}
