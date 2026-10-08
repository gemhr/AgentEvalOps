package cigovernance

import "agentevalops/go-backend/internal/asset"

// WP09Retired 是不可撤销的披露 incident 投影；拒绝新 Release，保留历史消费重放。
// 对原 Dataset 和六个 Case 的新版本/重包装同样生效；family 重新生成另由 AuditRenewal 拒绝。
func WP09Retired(dataset asset.Ref, cases []asset.Ref) bool {
	if dataset.EntityID == "6fb67037-6512-562a-8227-e842851e6f0f" {
		return true
	}
	for _, ref := range cases {
		switch ref.EntityID {
		case "ccebd448-f92f-52e5-b421-a685e01bc92e", "ae9f9c6a-a812-5214-a5a3-ca6a4d7f99c6", "e8f57538-ea2e-536b-8a6d-4e7b58f0693d", "1a98685a-3c1d-5a9e-a5a8-ec72c6c0d773", "880ee15e-dd86-5dda-8541-e8520e6a49c4", "4e1a74c2-05c0-5189-b5f6-96ced4d5487d":
			return true
		}
	}
	return false
}

// WP11Retired 在 WP12A 的 golden-v3 冻结后登记旧资产的用途限制。
// 原 golden-v2 的 role、正文、GT 和消费事实不变；本轮仅开放后续曝光分析资格。
// 新版本或重新包装原 Case 不能恢复独立 Release 资格。
func WP11Retired(dataset asset.Ref, cases []asset.Ref) bool {
	if dataset.EntityID == "e7a70b04-0712-58bc-a0c8-655eac0aa463" {
		return true
	}
	for _, ref := range cases {
		switch ref.EntityID {
		case "93703f50-d29f-5fa5-9c42-f5652027383d", "fd5d2343-1d3c-5c9f-8067-627e4ee1cbeb", "4a46786a-2e67-597e-bc72-efc90526d51e", "df831bc1-6b59-5004-90fe-dc4e795908a1", "8624c585-6f3c-5749-a2b5-7a475af42135", "03bc75f8-bcdd-5c9d-89a3-6a1b3ed12caa":
			return true
		}
	}
	return false
}
