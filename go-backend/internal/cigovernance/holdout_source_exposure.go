package cigovernance

import "agentevalops/go-backend/internal/asset"

// WP12BRetired 投影源码访问暴露事件，不改写原资产、消费或评测事实。
// golden-v3 的原独立项目也退出 Release，避免显式 Case 或重包装绕过退役。
func WP12BRetired(projectID string, dataset asset.Ref) bool {
	return projectID == "de00633e-36db-5a50-a85c-b65ccb0724f0" || dataset.EntityID == "0f94fa11-8194-5afd-8c66-30be45cfec79"
}
