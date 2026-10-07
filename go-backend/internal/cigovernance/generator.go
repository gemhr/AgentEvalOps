package cigovernance

import (
	"slices"
	"sort"

	"agentevalops/go-backend/internal/asset"
	"agentevalops/go-backend/internal/citriage"
)

// Scenario 是 evaluator-side 受控机制定义；与 Agent input 分离。
// 每个场景有不同干预和诊断证据组合，既有 WP01 模板不会被替换。
type Scenario struct {
	ID, Category, Mechanism, Component, Related, Event, RootMechanism string
	Observations                                                      []string
	Control                                                           string
}

func Scenarios() []Scenario {
	return []Scenario{
		{"duplicate-commit", "PRODUCT", "PRODUCT_BEHAVIOR", "checkout", "storage", "change-idempotency-index", "concurrent-idempotency-key-check-before-insert", []string{"两个并发请求使用同一 idempotency key，返回不同订单 ID", "数据库唯一索引在本次迁移被移除，串行请求保持单订单"}, "恢复唯一索引后并发请求仅产生一个订单"},
		{"tenant-cache", "PRODUCT", "PRODUCT_BEHAVIOR", "gateway", "identity", "change-cache-partition", "cache-key-omits-tenant-after-auth", []string{"两个租户读取同一资源 ID 时响应缓存命中同一条记录", "认证日志中的 tenant_id 不同；直连后端时结果隔离"}, "禁用网关缓存后交叉租户响应消失"},
		{"dst-window", "PRODUCT", "PRODUCT_BEHAVIOR", "messaging", "telemetry", "change-calendar-window", "calendar-window-fixed-seconds-over-dst", []string{"Europe/Berlin 时区跨夏令时日期的任务重复触发", "UTC 同一任务无重复；调度器按 86400 秒而非本地日期推进"}, "改用当地日历日期递增后每业务日只触发一次"},
		{"fixture-order", "TEST_CASE", "TEST_LOGIC", "catalog", "storage", "change-test-fixture-scope", "module-fixture-mutated-by-test-predecessor", []string{"测试 B 单独运行通过，紧跟测试 A 后失败", "A 删除共享 fixture 中的字段；两次服务请求响应相同"}, "将 fixture scope 改为 function 后所有运行顺序均通过"},
		{"clock-assertion", "TEST_CASE", "TEST_LOGIC", "telemetry", "messaging", "change-test-clock-helper", "assertion-mixes-local-clock-with-utc-response", []string{"测试比较本地 naive datetime 和协议 UTC instant，稳定相差八小时", "独立 UTC 客户端校验通过；服务器响应 offset 为 Z"}, "只修正断言的 timezone-aware conversion 后通过"},
		{"unordered-json", "TEST_CASE", "TEST_LOGIC", "search", "catalog", "change-serializer-test", "assertion-compares-object-serialization-order", []string{"同一响应对象换键顺序后字符串比较失败", "按 JSON 对象比较时字段和值一致，业务检索结果集合不变"}, "断言改为解析后的对象比较，服务代码不变即可通过"},
		{"expired-client-fixture", "TEST_DATA", "TEST_DATA", "identity", "gateway", "event-fixture-cert-expiry", "fixture-client-certificate-expired", []string{"CI mTLS client fixture 的 notAfter 早于本次测试时间", "同一服务与 CA 配置使用新签发测试证书成功；服务证书有效"}, "仅替换过期客户端 fixture 证书后连接成功"},
		{"locale-fixture", "TEST_DATA", "TEST_DATA", "catalog", "search", "change-fixture-import", "fixture-csv-locale-decimal-separator", []string{"CSV fixture 中价格使用逗号小数分隔符，声明的导入格式要求点号", "校验失败发生在发送服务请求前；同值规范化数据可以导入"}, "修正 fixture 的小数分隔格式后导入与查询通过"},
		{"nonce-fixture", "TEST_DATA", "TEST_DATA", "gateway", "identity", "event-fixture-snapshot", "recorded-nonce-timestamp-outside-acceptance-window", []string{"回放 fixture 的签名 nonce 时间戳来自七天前", "当前生成的 nonce 通过认证；服务和 runner 时钟由同一 NTP 校验"}, "重新生成带当前时间的 fixture 后通过，服务窗口不变"},
		{"proxy-loop", "ENVIRONMENT", "ENVIRONMENT_CONFIG", "gateway", "storage", "change-ci-proxy-config", "no-proxy-excludes-internal-service-domain", []string{"集成环境中服务域名走 HTTP_PROXY 到外部代理并返回 407", "NO_PROXY 只包含 localhost；同一网络内直连服务健康"}, "补入内部服务域名的 NO_PROXY 后请求直达且通过"},
		{"uid-volume", "ENVIRONMENT", "ENVIRONMENT_CONFIG", "storage", "checkout", "change-container-run-user", "mounted-workdir-owned-by-host-uid", []string{"容器进程 uid=1001，挂载目录 uid=1000 且 mode=0700", "文件写入返回 EACCES；临时容器内目录可正常写入"}, "只校正挂载目录权限后写入通过"},
		{"trust-bundle", "ENVIRONMENT", "ENVIRONMENT_CONFIG", "identity", "gateway", "change-ci-trust-mount", "runner-trust-bundle-missing-intermediate-ca", []string{"CI runner TLS 报 unable to get local issuer certificate", "服务器链有效；runner 挂载 trust bundle 未含中间 CA，独立验证完整 bundle 成功"}, "只补齐环境 trust bundle 后握手通过"},
		{"xml-reporter", "TOOL_CHAIN", "TOOL_EXECUTION", "telemetry", "catalog", "change-junit-reporter", "junit-reporter-emits-unescaped-control-byte", []string{"测试进程 exit=0，JSON 报告全部通过，JUnit XML parser 在控制字符处拒绝", "报告器直接把原始 stdout 字节写入 XML CDATA"}, "修正报告器字符转义后同一测试输出可被读取"},
		{"path-glob", "TOOL_CHAIN", "TOOL_EXECUTION", "search", "storage", "change-runner-path-resolver", "runner-glob-treated-as-literal-path", []string{"测试文件可读，但 runner 把 tests/**/*.py 当作字面文件名打开", "原工作区 shell glob 可匹配文件；目标测试进程没有启动"}, "修正 runner 的 glob expansion 后测试被发现并执行"},
		{"exit-code-truncation", "TOOL_CHAIN", "TOOL_EXECUTION", "checkout", "telemetry", "change-runner-exit-adapter", "process-return-code-truncated-to-unsigned-byte", []string{"测试进程返回 256，runner 报告 exit code 0 并声称成功", "原始 waitpid 记录为 256；报告适配器对返回码执行低八位截断"}, "修正适配器状态映射后失败如实传递"},
		{"dns-negative-cache", "INFRASTRUCTURE", "INFRASTRUCTURE_SERVICE", "gateway", "identity", "event-dns-rollout", "shared-resolver-negative-cache-outlives-service-registration", []string{"多个无关环境同时解析服务域名得到 NXDOMAIN，直接访问服务 IP 健康", "权威 DNS 已有记录，共享递归 resolver 仍返回旧负缓存 TTL"}, "清除共享递归 resolver 负缓存后无配置修改即恢复"},
		{"lb-idle-reset", "INFRASTRUCTURE", "INFRASTRUCTURE_SERVICE", "messaging", "gateway", "event-loadbalancer-policy", "shared-loadbalancer-idle-timeout-shorter-than-client-keepalive", []string{"多个客户端复用超过 30 秒的连接时收到 RST，新连接立即成功", "独立探针绕过共享 LB 没有重置；LB idle timeout 新设为 30 秒"}, "恢复 LB idle timeout 后同一客户端连接稳定"},
		{"object-quota", "INFRASTRUCTURE", "INFRASTRUCTURE_SERVICE", "storage", "telemetry", "event-storage-quota", "shared-object-store-account-byte-quota-exhausted", []string{"多个项目 artifact 上传同时返回 account quota exceeded，下载正常", "共享账户已用 bytes 达限，独立探针上传同样失败，CI workspace 空间充足"}, "恢复共享对象存储账户容量后所有上传恢复"},
	}
}

type Family struct {
	ID                 string     `json:"family_id"`
	RootMechanism      string     `json:"root_mechanism_identity"`
	Template           string     `json:"template_identity"`
	SignatureComponent string     `json:"signature_component_digest"`
	GT                 asset.JSON `json:"ground_truth"`
	GTDigest           string     `json:"gt_digest"`
	Control            string     `json:"controlled_intervention"`
	Role               string     `json:"role"`
	Input              asset.JSON `json:"input"`
	Case               asset.Ref  `json:"case_version"`
}
type Generation struct {
	Profile   string   `json:"profile"`
	GTMapping string   `json:"gt_mapping_version"`
	Split     string   `json:"split_algorithm"`
	Seed      string   `json:"seed"`
	Namespace string   `json:"namespace"`
	Families  []Family `json:"families"`
	Digest    string   `json:"manifest_digest"`
}

// Generate 先冻结全部 family/GT，再按 category 内的稳定 family hash 分配角色。
// 没有模型结果输入，也不按输出质量选择 split。
func Generate(namespace, seed string) Generation {
	specs := Scenarios()
	g := Generation{Profile: Profile, GTMapping: GTMapping, Split: SplitVersion, Seed: seed, Namespace: namespace}
	for _, s := range specs {
		change := s.Event
		gt := citriage.GroundTruth{Version: citriage.Contract, Category: s.Category, Ticket: "IGNORE", Criticality: "NORMAL", Roots: []citriage.Root{{ID: "hidden-eval-root-" + s.ID, Relevance: 3, Descriptors: []citriage.Descriptor{{Component: s.Component, Mechanism: s.Mechanism, Change: &change}}}}}
		actions := map[string]string{"PRODUCT": "INVESTIGATE_PRODUCT", "TEST_CASE": "FIX_TEST", "TEST_DATA": "FIX_TEST_DATA", "ENVIRONMENT": "RETRY_ENVIRONMENT", "TOOL_CHAIN": "REPAIR_TOOL_CHAIN", "INFRASTRUCTURE": "RESTORE_INFRASTRUCTURE"}
		gt.Actions = []string{actions[s.Category]}
		if slices.Contains([]string{"PRODUCT", "ENVIRONMENT", "TOOL_CHAIN"}, s.Category) {
			gt.Criticality = "CRITICAL"
		}
		if s.Category == "PRODUCT" {
			gt.Ticket = "CREATE_PRODUCT_TICKET"
		}
		gt.EvidencePolicy.Decidable = true
		gt.EvidencePolicy.Missing = "BLOCKED"
		gt.EvidencePolicy.Types = []string{"FAILURE_DETAIL", "CHANGE_METADATA", "AUTHORIZED_ARTIFACT"}
		truth := Freeze(gt)
		evidence := []any{}
		for i, kind := range gt.EvidencePolicy.Types {
			body := Freeze(map[string]any{"component_ids": []string{s.Component, s.Related}, "visible_change_refs": []string{s.Event}, "observations": s.Observations})
			if kind == "CHANGE_METADATA" {
				body = Freeze(map[string]any{"component_ids": []string{s.Component, s.Related}, "change_ref": s.Event, "timeline": "变更/事件先于诊断观测，源记录已冻结"})
			}
			if kind == "AUTHORIZED_ARTIFACT" {
				body = Freeze(map[string]any{"component_ids": []string{s.Component, s.Related}, "control_observation": s.Control})
			}
			evidence = append(evidence, map[string]any{"evidence_id": "evidence:" + ID(namespace, s.ID, fmtIndex(i)), "type": kind, "digest": body.Digest(), "availability": "AVAILABLE", "content": body})
		}
		input := Freeze(map[string]any{"schema_version": "stage13.triage-input.v1", "incident_ref": map[string]any{"incident_id": ID(namespace, s.ID, "incident"), "evidence_revision": 1, "manifest_digest": Freeze(evidence).Digest()}, "scope": map[string]string{"automation_project_id": "automation-demo", "suite_id": "evaluation-generation", "business_date": "2026-10-08"}, "environment_samples": []any{map[string]string{"environment_id": "eval-environment", "channel_group": "controlled-evaluation"}}, "version_samples": []string{"evaluation-realization-v1"}, "failure_summary": map[string]any{"failed_cases": 1, "component_scope": []string{s.Component, s.Related}, "observations": s.Observations}, "visible_evidence": evidence, "visible_change_inventory": []string{s.Event}, "evidence_policy": map[string]any{"allowed_types": gt.EvidencePolicy.Types, "max_tool_reads": 8, "max_bytes": 1048576, "deadline_at": "2026-10-08T00:00:00Z"}})
		g.Families = append(g.Families, Family{ID: "eval-family-" + s.ID, RootMechanism: s.RootMechanism, Template: Profile + "/" + s.ID, SignatureComponent: Freeze([]any{s.Observations, []string{s.Component, s.Related}}).Digest(), GT: truth, GTDigest: truth.Digest(), Control: s.Control, Input: input, Case: asset.Ref{EntityID: ID(namespace, s.ID, "case"), Version: "generated-v1"}})
	}
	if err := AuditIsolation(g.Families); err != nil {
		panic(err)
	}
	roles := map[string]string{}
	for _, category := range []string{"PRODUCT", "TEST_CASE", "TEST_DATA", "ENVIRONMENT", "TOOL_CHAIN", "INFRASTRUCTURE"} {
		var group []string
		for _, s := range specs {
			if s.Category == category {
				group = append(group, s.ID)
			}
		}
		sort.Slice(group, func(i, j int) bool {
			return Freeze([]string{SplitVersion, seed, group[i]}).Digest() < Freeze([]string{SplitVersion, seed, group[j]}).Digest()
		})
		for i, id := range group {
			roles[id] = Roles[i]
		}
	}
	for i := range g.Families {
		g.Families[i].Role = roles[specs[i].ID]
	}
	g.Digest = Freeze(g).Digest()
	return g
}
func fmtIndex(i int) string { return string(rune('0' + i)) }

// AuditIsolation 同时核对机制、模板、症状/组件、descriptor 映射；修改 ID 不能规避。
func AuditIsolation(families []Family) error {
	keys := map[string]string{}
	ids := map[string]bool{}
	for _, f := range families {
		if ids[f.ID] || (f.Role != "" && !slices.Contains(Roles, f.Role)) || f.GT.Digest() != f.GTDigest {
			return asset.ErrConflict
		}
		ids[f.ID] = true
		g, err := citriage.ReadGroundTruth(f.GT)
		if err != nil {
			return err
		}
		checks := []string{"root:" + f.RootMechanism, "template:" + f.Template, "signature:" + f.SignatureComponent}
		for _, root := range g.Roots {
			for _, d := range root.Descriptors {
				checks = append(checks, "mapping:"+d.Key())
			}
		}
		for _, key := range checks {
			if _, ok := keys[key]; ok {
				return asset.ErrConflict
			}
			keys[key] = f.Role
		}
	}
	return nil
}
