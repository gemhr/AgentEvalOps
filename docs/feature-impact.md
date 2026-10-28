# 历史特性影响业务评测接入

`internal/featureimpact` 拥有独立覆盖评分；不复用 CI 修复或 RAG 排名指标。AgentCore 拥有模拟执行与公开业务资料。当前只有 RULE/HUMAN 薄 Target，真实 Agent 未执行。

## 配置

API/Worker 显式设置 `FEATURE_IMPACT_ENABLED=1`。`FEATURE_IMPACT_TARGET_CONFIG` 为现有 `provider.LocalAgentConfig` JSON（base_url、token_env、max_response_bytes、call_milliseconds、http）；token_env 指向 AgentCore SERVICE JWT 环境变量，scope 必须为 `localagent:feature-impact:evaluate`。

配置中不写 token。`http` 使用现有 HTTPConfig 各项有界连接/响应超时。该模式与 CI business、旧 Loop、AgentQuality、Stage13 Target 模式互斥；Worker online concurrency 必须为 0。数据库使用现有 Alembic head `c13a00300001`、GO_ACTIVE writer 及既有受限角色；不修改内核、队列、Schema 或历史结果。

## Catalog / Run / Result

Case 输入为 `{task_id,snapshot,candidate_kind,candidate_version,report}`，snapshot 是 AgentCore 的冻结公开输入（不含界面字段和报告列表）。开发规则版本 `dependency-lifecycle.v1`。独立参考 `feature-impact-truth.v1` 绑定 input_digest/corpus_digest，单元包含 feature/mechanism/conditions/class/developer_known/support_sources/counter_sources/existing_coverage/mechanism_aliases。参考先于候选输出冻结；只保存在 EO，不能发给 Target。

指标 `feature-impact-quality.v1`；evaluator implementation `feature-impact-deterministic.v1`，input_contract `feature-impact-input.v1`，schema_version `feature-impact-output.v1`，normalization `feature-impact-counts.v1`，max_provider_calls=0。通过已有 Case/Dataset/Suite 发布并冻结 Run。

`POST /api/v1/projects/{project}/feature-impact/runs` 接受 `{suite: Ref}`，使用现有项目 Execute 权限、Idempotency-Key、Worker 和 ResultValue。Target 只把冻结公开 Case Input 发给 AgentCore，检查真实 Attempt/Subject/Target/回执绑定。HUMAN 原报告来源保留，不冒充新执行。

结果沿现有 `/runs/{id}` 与 `/results/{id}` 查询。`ResultValue.Provenance.counts` 保存逐单元判定与原始计数；无加权总分。未知语义支持为 `INCONCLUSIVE / SEMANTIC_SUPPORT_REVIEW_PENDING`，Score=null；伪造证据、重复、关键覆盖或可操作性缺口为 FAIL。候选自报 FULL 不替代独立已有覆盖参考。

`scripts/prepare-feature-impact-evaluation.py` 将显式输入的开发 bundle 发布至现有 API；bundle 包含 purpose=DEVELOPMENT_EXPOSED、reference_review_ref、input、coverage_truth。CLI 的 --bundle/--project/--base-url/--token-env/--output 必填；--run 可显式创建 Run。只处理已曝光开发材料，不处理 operator-only Holdout。脚本创建既有 Gate policy，pending/缺证据阻断质量放行，不设置未授权的百分比阈值。Agent 与规则比较、真人提效和最终 Gate 均未执行。

前端独立页面 `/feature-impact` 使用 AgentCore HUMAN JWT；`LOCAL_AGENT_API_URL` 配置同现有 CI rewrite。可完成资料阅读、人工报告、规则报告、按版本审核、计划封存及独立执行。页面不接收隐藏参考。

## 定向验证

真实 Catalog HTTP 发布 + API/Worker bootstrap + Python HTTP Target + 受限 PostgreSQL role + 既有 Gate 对 pending 保持 BLOCKED：

```powershell
go test -tags=integration ./internal/postgres -run '^TestFeatureImpactWorkerHTTPPostgres$' -count=1 -v
```

沿用 G1_TEST_POSTGRES_URL（隔离 localhost:55432/postgres）和 G1_TEST_PYTHON（迁移环境）；另设 FEATURE_IMPACT_AGENTCORE、FEATURE_IMPACT_PYTHON。测试以私有管道启动 AgentCore 演练服务，JWT 不进入日志；模型调用数为 0。测试 fixture 结束清理 EO 临时数据库，落盘的非凭据证据保留实际 Run/ResultValue 和二次 PG 读取证明。

前端 `npx playwright test e2e/feature-impact.spec.ts --project=chromium --workers=1 --reporter=line`；业务 HTTP 使用 18091。Next 开发进程先设置 LOCAL_AGENT_API_URL=http://127.0.0.1:18091；若本机无 yarn，可先用 npm run dev 启动并让既有 Playwright 配置复用。该浏览器操作是自动化 HUMAN 工作流演练，不是真人效率实验。

## AGENT 封存报告的受信重放

E2 为既有业务 Input 增加 `candidate_kind=AGENT / candidate_version=1.1.0`，report **只能**是 `{report_id,run_id}`；不接受客户端传入的 AGENT 报告正文或 source_record。现有 FEATURE_IMPACT_HTTP Target 仍仅发送冻结 Case Input，不发送 GroundTruth，也不调用 Agent 模型。AgentCore 在 evaluate HTTP 内从自身持久化 Run/FINAL/owner/Session/观察记录重新核验并返回原封存报告；失败直接拒绝，不降级为 HUMAN。

`featureimpact.Output.Validate` 对 AGENT 检查服务端返回的 `feature-impact-agent-source.v1`、选择器、Run/Session/owner/tenant、AgentDefinition 1.1.0、DELIVERED receipt 摘要、原始输出与报告摘要。Score 分别核对 TASK_INPUT/EVIDENCE_READ 的来源列表，再复用原机制/条件评分和静态可操作性；不使用候选质量分数。RULE/HUMAN 仍要求 envelope.run_id=null，并保留原语义。

来源权威在已固定的 AgentCore HTTP 服务及其 PG/Runtime repository。Go 的投影校验不能独立认证任意离线客户端自行编写的 receipt；只有正常受信 Target 返回的输出才能成为实际评测事实。合同测试 fixture 必须明确 TEST_FIXTURE，不能登记成真实 Agent Run/执行证明。SemanticReview 继续 PENDING，旧 Gate 不回写。
