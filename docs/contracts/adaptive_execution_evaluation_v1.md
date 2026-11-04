# 自适应执行评测接线 v1

## 生产接线

`LOCALAGENT_ADAPTIVE_ENABLED=1` 选择 `localagent-adaptive-http`；Target version 为 `adaptive-execution-evaluate.v1`。复用原 Worker、Claim/Fencing、Run Snapshot、ExecutionAttempt、EvaluatorWork、EvaluationResult 和 Compare/Gate。与其他 LocalAgent Target 开关冲突时拒绝装配。

`LOCALAGENT_TARGET_CONFIG` 为严格 JSON：

```text
{transport: 原 AgentLoopConfig（base_url/token_env/trusted_project_id/
  max_response_bytes/call_milliseconds/http）, group: BASELINE|CANDIDATE_A|CANDIDATE_B,
 policy_digest: ci-adaptive-evaluation.v1 的 digest,
 dataset_digest: 当前冻结 Dataset 的 ContentDigest}
```

密钥只读 token_env 指定的安全环境变量。Core 专用入口要求预配置的 HUMAN 身份与冻结 Case，并保留原 JWT、CI Ownership 与工具治理；不能使用旧 SERVICE token 提升权限。请求携带真实目录 Case ID/version/content digest、Dataset digest、input digest、Run/Attempt/request ID。结构化业务 input 为 `{slice,incident_id,execution_id,execution_digest}`，不能塞进 query 字符串替代合同。

`AdaptiveTarget` 从请求前开始计时，到终态响应完成读取、严格校验及业务投影回执结束；所有退出路径均把本地观察写入原 Outcome.Cleanup。202、传输错误、本地超时、非法响应保留 OUTCOME_UNKNOWN/不确定性，不伪造远端 FAILED。已确认 Core Deadline 才映射 TIMEOUT。无自动盲重试；Core 的 GET 原回执入口可用于后续人工/既有恢复流程，本 WP 不建设自动对账平台。

## Strict Consumer 与兼容

新证据 `adaptive-execution-evidence.v1` 具有严格共同身份、comparability、策略、Runtime/Business、metrics 和 Fast/Loop 判别联合。所有字段必须存在，可空字段显式 null；拒绝未知字段、未知版本、模式混用、身份/策略不符、Stop digest 不符及 FINAL_ONLY 后新启动工具。业务输出同样检查 `ci-status.v1` 或 `ci-diagnosis.v1` 形状。

Fast 只有 Step/Tool 引用，没有 Frame。Loop 有原 Frame/Iteration/Observation 摘要、原持久 Stop Decision 及工具启动关联。普通 Loop 未触发 FINAL_ONLY 时保持 null。旧 AgentLoopTarget、`loop-evidence.v1`、旧 strict decoder 与协议解释保持原样。公开诊断向量 `internal/agentquality/testdata/adaptive_diagnosis_public.json` 来源于 Core 真实隔离 PG 的合成调查，用于消费者合同测试；真正双仓链路另由 HTTP/Worker/PG 测试证明。

## 指标

目录发布使用 `AdaptiveMetricDefinition(name)`、`AdaptiveEvaluatorDefinition(ref,name)`，implementation 为 `adaptive-execution-metrics.v1`，result schema 为 `adaptive-metric-result.v1`。原 Bootstrap capability 发现决定可执行能力，目录 CONTRACT_ONLY 本身不表示已运行。

| metric | 来源与判定 | 缺失 |
|---|---|---|
| quality | STATUS：完整输出 canonical JSON 与冻结 ExpectedOutput 精确比较，包括 IDs/revision/summary；DIAGNOSIS：原业务 Validator/coverage，通过后仅以可信冻结 root_cause_hypothesis GT 判语义 | 无 GT 为 INCONCLUSIVE；SUPPORTED 不等于根因正确 |
| client_rt_ms | 原 Attempt Cleanup 的 `client_observed_rt_ms` | null，不填 0 |
| model_attempts | 原 PG ModelInvocation 实际 started_at 非空数，包含失败/UNKNOWN | 无回执为 null；Fast 的原事实可证明 0 |
| tool_invocations | 原 PG ToolInvocation 实际启动数，COMMITTED replay 不重计 | 无回执为 null |
| provider_tokens | 原 Provider Usage 的总 token，要求全部已启动模型 Usage 已知，回执另有可用次数 | null，不用估计 Tokenizer 替代 |
| actual_cost_micros | 有可信实际费用时的金额；当前 Core 不提供实际账单 | 当前始终 null，不使用 estimated cost_units |

Runtime Outcome、业务 Outcome、Evaluator Verdict 保持分离。业务不足、Validator rejected、答案错误、Runtime failure、Timeout、UNKNOWN、不可判定分别保留。INSUFFICIENT_EVIDENCE 的 quality 为 0，不能计完整根因完成。

`Source.task_completion_over_planned` 采用原 `case-run` 业务任务采样单位：最新 Attempt 的完整质量成功数 / 全部计划 Case-run 数，包含失败和不可判定；零分母为 null。`adaptive_task_outcomes` 是相同单位的分类计数。原 TaskRate（可判定样本分母）保持原语义，不将其冒充全部计划任务完成率。

`Source.adaptive_latency` 使用该 Run 集合全部 ExecutionAttempt（包括 retry）的本地观察，报告 Planned、Known、Censored、各原 Outcome 数、P50/P95/P99。算法为 nearest-rank.v1：对已知非负耗时排序，取 `ceil(p*n)-1`；缺失不填零。截断本地 RT 标记 censored，不代表远端实际完成时间。它是 Evaluation Client Observed RT，不是浏览器 RT、TTFT 或 SSE 首次有效答案；单样本仅为描述统计。

## Compare / Gate

原 Case/Dataset/Evaluator/Metric/Subject 检查保留。Target Config 策略差异只能由已有 `AcceptedDifference` 的精确 baseline/candidate digest + actor/reason/proof 放行，不能关闭配置检查。`adaptive_context:<case_id>` 使用 Core 实际模型配置、工具快照、授权、超时、冻结输入及调查 Seed 摘要，仍 MUST_MATCH；不能用策略差异证明放行环境或前置 Memory 漂移。

缺失 Usage/费用仍计入 coverage；若政策要求全部指标已知，Gate 必须 BLOCKED。WP3 合成合同验收可以明确允许两项缺失，同时强制 quality=1，验证机制可用；这不是 WP4 的正式业务或性能阈值。没有加权总分、优化百分比或真实 Provider 收益声明。

## 定向验证

Core：`uv run python -m pytest tests/test_adaptive_evaluation_evidence.py -q`。

Go：`go test ./internal/agentquality ./internal/evaluation ./internal/provider -run '^TestAdaptive' -count=1`；旧协议 `go test ./internal/provider -run '^TestAgentLoopPythonProducerStrictTarget$' -count=1`。

真实链路：`go test -tags integration ./internal/postgres -run '^TestAdaptiveRealWorkerCoreHTTPPostgresGate$' -count=1 -timeout 4m -v`。显式 `AC_ADAPTIVE_TEST_PG_URL` 仅接受 localhost:5433/postgres 的本机隔离测试实例，仍新建并删除独立 `agentevalops_g1_<uuid>` 数据库；不修改生产库。Core 子进程使用现有 `_test` 库安全校验，不能与重建同库的 pytest 并行。实际生产 auth middleware/JWT/业务服务/Runtime/PG 均执行，测试显式关闭限流，受控模型仅复述本次真实工具结果，无真实 DeepSeek。子进程 token 仅在私有父子管道/内存传递，不写日志或文件。
