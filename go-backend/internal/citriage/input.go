package citriage

import "agentevalops/go-backend/internal/asset"

// SemanticInput 投影历史 v1 输入；只移除执行时刻，不改变授权或证据。
func SemanticInput(input asset.JSON) (asset.JSON, error) {
	if err := ValidateInput(input); err != nil {
		return asset.JSON{}, err
	}
	var body, policy map[string]asset.JSON
	if input.Decode(&body) != nil || body["evidence_policy"].Decode(&policy) != nil || policy == nil {
		return asset.JSON{}, asset.ErrInvalid
	}
	delete(policy, "deadline_at")
	body["evidence_policy"], _ = asset.Freeze(policy)
	return asset.Freeze(body)
}
