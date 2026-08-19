# 推荐文档名

```
AGENTEVALOPS_STAGE4_COMPLETE_LEARNING_AND_INTERVIEW_SUMMARY.md
```

下面这份按 **整个 Stage4** 来总结，不再以单个 WP 为中心，而是重点理解：**为什么这样演进、每一层解决什么工程问题、核心状态机和 Owner 如何划分、各阶段如何串成完整 Evaluation 闭环，以及面试时应该如何讲。**

Stage4 最终已经经过独立 Closeout：Phase0～Phase5 推荐范围全部完成，`OPEN_P0=0`、`OPEN_P1=0`、`INTERVIEW_READY=YES`、`STAGE4_FREEZE=YES`，当前应停止继续扩张 Stage4；但 `Production Ready=PARTIAL`，不能把本地 PostgreSQL、Synthetic Demo（合成演示）和静态 GitHub Workflow 验证夸大成完整生产落地。

------

# 1. Stage4 到底做成了什么

一句话概括：

> **Stage4 把 PandaProbe 从一个偏 Trace / Score / Monitoring 的观测系统，演进成了一个拥有独立 Evaluation Domain（评估领域）、可执行 Runtime（运行时）、Regression（回归）、Release Gate（发布门禁）以及 Trace Feedback（Trace 回流）能力的 Agent Evaluation Platform（智能体评估平台）。**

最终形成：

```text
Production Trace
        ↓
Generic Online Normalization
        ↓
Online Metrics / Failure Detection
        ↓
TraceEvidenceCandidate
        ↓
Trace-to-Dataset Feedback
        ↓
DatasetVersion / TestCaseVersion
        ↓
EvaluationRun / ExecutionAttempt / EvaluationResult
        ↓
Baseline / Candidate Comparison
        ↓
RegressionReport
        ↓
ReleaseDecision
        ↓
Closed-loop Demo
        ↓
CI Exit Adapter
        ↓
PASS=0 / FAIL=2 / ERROR=1
```

这是整个 Stage4 的主线。

------

# 2. 最核心的认知：Observation 和 Evaluation 必须分开

Stage4 最重要的并不是某一个类或者接口，而是 Phase0 冻结下来的架构原则：

```text
Trace ≠ EvaluationResult
```

Trace 表达的是：

```text
系统实际发生了什么
```

也就是 Observation（观测事实）。

而 EvaluationResult 表达的是：

```text
在明确 TestCase、Evaluator、ExecutionTarget、
版本、Run、Attempt 上下文下，
评估系统得到了什么判断
```

也就是 Evaluation Truth（评估事实）。

如果把两者混起来，就会出现非常严重的问题：

```text
线上一次模型失败
↓
直接等于测试失败

Trace Score
↓
直接等于 Evaluation Result

线上实际输出
↓
直接等于 expected output
```

这些推导都不成立。

所以 Stage4 最根本的架构变化是：

```text
Trace Domain
= Observation / Evidence Source

Evaluation Domain
= Evaluation Truth Owner
```

后续所有 Phase 都建立在这个基础上。

------

# 3. Stage4 六个阶段分别解决什么

| 阶段   | 解决的问题                             | 最重要结果                     |
| ------ | -------------------------------------- | ------------------------------ |
| Phase0 | Observation 与 Evaluation 的边界是什么 | 冻结 Domain / Owner            |
| Phase1 | 不同 Runtime 的 Trace 如何统一分析     | Generic Online Core            |
| Phase2 | Evaluation 如何可靠执行和持久化        | Run / Attempt / Result Runtime |
| Phase3 | Baseline 与 Candidate 如何判断退化     | Regression + Release Gate      |
| Phase4 | 线上失败如何回流离线测试               | Trace-to-Dataset Feedback      |
| Phase5 | 已有闭环如何变得可运行、可消费         | Demo + CI Gate Adapter         |

这六个阶段不是六组独立功能，而是一层一层建立依赖关系。

------

# 4. Phase0：为什么先做 Owner，而不是先写代码

Phase0 的关键成果是 Architecture Mapping（架构映射）和 Owner Freeze（所有权冻结）。

最终划分可以理解为：

```text
Observation Side
├─ Trace
├─ Span
├─ Normalized Projection
├─ Online Metrics
└─ Evidence Candidate

Evaluation Side
├─ Dataset / TestCase
├─ ExecutionTarget
├─ EvaluationRun
├─ ExecutionAttempt
├─ EvaluationResult
├─ Comparison
├─ RegressionReport
└─ ReleaseDecision
```

这一步的价值在于防止后面不断出现“双写 Truth”。

比如如果：

```text
Trace.status
EvaluationRun.status
EvaluationResult.verdict
legacy Score
```

都可以解释“Evaluation 是否成功”，系统迟早会出现冲突。

所以一个很重要的系统设计经验是：

> **复杂系统开发前，先明确谁拥有事实，再决定谁调用谁。**

Stage4 后续 Closeout 没发现 Dual Owner，`Owner consistency: PASS`。

------

# 5. Phase1：为什么需要 Generic Online Core

PandaProbe 原有 Trace 结构和 LocalAgent Trace Contract 并不天然属于同一个 Runtime。

如果后续 Evaluation 平台直接针对 LocalAgent 写：

```text
if source == LocalAgent:
    ...
```

或者：

```text
LocalAgentTrace → Evaluation
LegacyTrace → 另一套 Evaluation
```

平台就失去了 runtime-neutral（运行时中立）的能力。

因此 Phase1 引入：

```text
NormalizedOnlineTrace
NormalizedOnlineSpan
GenericOutcome
```

不同 Runtime：

```text
LocalAgent
Legacy
未来其它 Agent Runtime
```

先投影到 Generic Online Domain，再由后续 Metrics / Evidence 层消费。

------

# 6. Failure Semantics：UNKNOWN 为什么不能算失败

Phase1 定义的 outcome 中，有一个特别容易答错的点：

```text
SUCCESS
FAILURE
CANCELLED
TIMEOUT
UNKNOWN
```

实际 failing rule：

```text
FAILURE
CANCELLED
TIMEOUT
→ failure

UNKNOWN
→ not failure
```

为什么？

因为：

```text
UNKNOWN
```

代表：

> 当前信息不足，无法确认结果。

而：

```text
FAILURE
```

代表：

> 已经确认执行失败。

如果把 UNKNOWN 算 failure：

```text
未知数据
→ failure_count 上升
→ 在线指标失真
→ 质量判断被污染
```

所以这是一个非常典型的数据语义问题：

> **缺乏证据，不能自动转换成负面事实。**

Final Closeout 也明确保留了 historical normalized `NULL`，没有为了指标完整性伪造历史值。

------

# 7. TraceEvidenceCandidate 的真正作用

Phase1 没有直接：

```text
failing Trace
→ TestCase
```

而是先得到：

```text
TraceEvidenceCandidate
```

这个 Candidate 表达：

> 这条 Trace 值得进入 Evaluation Feedback 流程。

但它还不是：

```text
正式 Dataset
正式 TestCase
expected output
critical case
```

这层中间态非常重要。

因为：

```text
发现问题
```

和：

```text
把问题正式纳入回归测试资产
```

是两个不同的治理动作。

------

# 8. Phase2：整个 Stage4 后端工程深度最高的一层

Phase2 真正建立了 Evaluation Runtime。

核心实体：

```text
DatasetVersion
TestCaseVersion
EvaluationSuiteVersion
EvaluatorSpec
ExecutionTarget

EvaluationRun
ExecutionAttempt
EvaluationResult
```

其中最关键的是：

```text
Run
Attempt
Result
```

三层。

------

# 9. Run / Attempt / Result 为什么必须分开

## EvaluationRun

表示：

> 一次完整 Evaluation execution。

例如：

```text
Candidate v2
+
Dataset v5
+
Suite v3
+
Target v7
```

整体跑一次，就是一个 Run。

## ExecutionAttempt

表示：

> 某个 TestCase 的某一次执行尝试。

因为一个 Case 可能：

```text
第一次执行
→ TIMEOUT

第二次 Retry
→ SUCCESS
```

所以 Attempt 是 execution fact。

## EvaluationResult

表示：

> 最终已经确定并持久化的评估事实。

因此：

```text
Run = 批次
Attempt = 执行过程
Result = 最终评估事实
```

如果只有一个表，就很难同时表达：

- Retry
- Timeout
- stale worker
  -并发 claim
  -最终 Result 不可变。

------

# 10. Phase2 状态机

Run：

```text
PENDING
   ↓
RUNNING
   ↓
COMPLETED
FAILED
OUTCOME_UNKNOWN
```

Attempt：

```text
PENDING
   ↓
CLAIMED
   ↓
RUNNING
   ↓
Terminal
```

这里最重要的不是“有几个枚举值”，而是：

> **谁有资格推进状态，以及推进时必须满足什么条件。**

------

# 11. Atomic Claim CAS 解决什么

多个 worker 同时看到：

```text
Attempt = PENDING
```

如果都：

```text
SELECT
→ 判断 PENDING
→ UPDATE CLAIMED
```

就可能双重 claim。

所以使用 CAS（Compare-And-Set，比较并设置）思想：

```text
UPDATE attempts
SET status = CLAIMED,
    claim_token = X
WHERE id = ?
  AND status = PENDING
  AND claim_token IS NULL
RETURNING ...
```

只有一个 worker 成功。

其他 worker：

```text
UPDATE affected rows = 0
```

即 claim 失败。

这比：

```text
Python if
```

可靠得多，因为最终竞争发生在数据库。

------

# 12. Claim Token Fencing 为什么比 status 更重要

只判断：

```text
status == RUNNING
```

仍然不够。

典型场景：

```text
Worker A claim Attempt
        ↓
A 卡死
        ↓
lease expired
        ↓
系统允许新的 owner 接手
        ↓
Worker B 开始执行
        ↓
Worker A 突然恢复
```

如果 A 只检查：

```text
status == RUNNING
```

它仍有可能写入结果。

所以还必须证明：

```text
my_claim_token
==
current_attempt_claim_token
```

这就是 fencing。

它解决的是：

> **旧 Owner 恢复以后不能继续污染新 Owner 的执行事实。**

这是一个非常好的并发面试点。

------

# 13. Retry 为什么必须创建新 Attempt

错误方案：

```text
Attempt #1 FAILED
↓
status 重置 PENDING
↓
重新执行
```

这样会覆盖历史。

最终采用：

```text
Attempt #1 FAILED
        ↓
Attempt #2
retry_of = Attempt #1
```

因此：

```text
Retry
=
New Fact
```

而不是：

```text
Mutation of Old Fact
```

这保证：

- 可审计；
- 可追踪；
  -可恢复；
  -不同 Attempt 的执行结果不会混起来。

------

# 14. EvaluationResult 为什么必须 Append-only

如果 EvaluationResult 可以更新：

```text
FAIL
↓
人工修改
↓
PASS
```

那么后续：

```text
Baseline vs Candidate
```

就失去可信历史基础。

因此 Result 最终是：

```text
Append-only
+
Logical Uniqueness
```

Logical slot 使用：

```text
(run_id,
 attempt_id,
 case_id,
 case_version,
 evaluator_id,
 evaluator_version)
```

来约束最终事实。

这体现一个很重要的 Evaluation 平台原则：

> **Regression 的可靠性取决于底层 Result 是否是稳定事实。**

------

# 15. Phase2 已验证什么，没有验证什么

Final Closeout 明确：

```text
LOCAL_POSTGRESQL_PROVEN
DOMAIN_CONCURRENCY_CORRECTNESS_PROVEN
```

但不是：

```text
PRODUCTION_MULTI_WORKER_PROVEN
```

也就是说：

已经真实验证：

- PostgreSQL CAS；
- fencing；
  -retry；
- uniqueness；
  -并发语义。

但没有：

-生产 Celery wiring；
-真实多个 worker 进程 crash/restart；

- distributed production load proof。

这个真实性边界面试时一定要守住。

------

# 16. Phase3：为什么有了 Result 还需要 Comparison Domain

单次 EvaluationResult 只能回答：

```text
这个版本表现怎么样？
```

Regression 需要回答：

```text
Candidate 相比 Baseline 发生了什么变化？
```

所以需要明确 Alignment Key（对齐键）：

```text
(
  case_id,
  case_version,
  evaluator_id,
  evaluator_version
)
```

只有对齐后的两个 Result，才能比较。

------

# 17. Regression 四种核心结果

```text
PASS → FAIL
= REGRESSION

FAIL → PASS
= IMPROVEMENT

same
= UNCHANGED

missing / ERROR / INCONCLUSIVE
= NOT_COMPARABLE
```

这里尤其要注意：

```text
NOT_COMPARABLE
```

不是：

```text
FAIL
```

它表达的是：

> 无法进行可信比较。

这和 Phase1 的 UNKNOWN 思想是一致的：

> **未知、缺失、不确定，不能随便强制折叠成失败。**

------

# 18. Criticality 为什么必须 Caller Supplied

系统能知道：

```text
Case A regression
```

但不能天然知道：

```text
Case A 对业务是否 Critical
```

Criticality 可能来自：

-产品策略；
-SLA；
-安全等级；
-核心路径；
-业务部门要求。

所以：

```text
Criticality Owner = caller
```

而不是：

```text
Evaluator
ComparisonService
CI
```

这也是 Stage4 Owner Boundary 中最重要的一点之一。

------

# 19. Release Gate 到底如何判断

当前规则可以理解为：

```text
Critical REGRESSION
→ BLOCK

Critical NOT_COMPARABLE
→ BLOCK

Non-critical REGRESSION
→ REPORT ONLY

Non-critical NOT_COMPARABLE
→ REPORT ONLY
```

最终：

```text
ReleaseDecision.PASS
ReleaseDecision.FAIL
```

Owner：

```text
RegressionReportService
```

CI 后面只消费这个结果。

所以不能说：

> “GitHub Workflow 决定是否发布。”

正确说：

> “RegressionReportService 产生业务 ReleaseDecision，CI Adapter 将它映射成进程状态。”

------

# 20. Phase4：为什么 Production Trace 不能自动转 TestCase

这是 Feedback Loop 最核心的设计问题。

看起来最自动化的方案是：

```text
Production failure
→ 自动创建 TestCase
→ 自动进入 Regression
```

但这里隐藏了很多事实 Owner：

```text
这个 payload 是否已经脱敏？
谁决定 input？
哪个输出才是 expected output？
是否 Critical？
case version 是多少？
dataset version 是多少？
```

系统本身没有权威答案。

所以最终选择：

```text
Trace
→ EvidenceCandidate
→ Explicit Feedback Command
→ Caller-approved TestCase
```

而不是完全自动化。

------

# 21. Sanitization 为什么是 Caller Supplied

生产 Trace 可能包含：

-个人数据；
-业务数据；
-密钥；
-长上下文；
-系统 Prompt；
-内部 metadata。

所以不能默认：

```text
production payload
→ evaluation dataset
```

Final contract：

```text
sanitization = CALLER_SUPPLIED
```

也就是调用 Feedback 的上层必须提供已经确认可进入测试集的 input。

------

# 22. Expected Output 为什么也不能自动取生产输出

非常重要：

```text
actual production output
!=
correct expected output
```

比如这条 Trace 本身就是因为模型输出错了才被选中。

如果把错误输出自动保存成 expected output：

```text
线上错误
→ Golden Answer
```

会直接污染 Dataset。

因此：

```text
expected_output
=
caller supplied / optional
```

------

# 23. Dataset 为什么必须 NEW_VERSION

已有：

```text
DatasetVersion V1
```

Production Trace 被批准加入后，不应该：

```text
修改 V1
```

而应：

```text
V1
↓
V2
```

因为如果 Baseline 当时跑的是 V1，而 Dataset 后来被原地修改：

```text
历史 Evaluation
```

就无法准确解释。

所以：

> **Evaluation Dataset 也是版本化事实。**

------

# 24. Phase4 当前真正的完成边界

实现的是：

```text
MINIMAL PRODUCTION FEEDBACK LOOP
```

而不是：

```text
Full Production Feedback Platform
```

当前没有：

```text
Durable Dataset Catalog
Feedback HTTP API
Auto Evaluation
Human Review
Alert
Judge/Human Agreement
```

Final Closeout 已明确这些是 Deferred。

------

# 25. Phase5-WP1：为什么需要 Closed-loop Demo

到 Phase4 时，其实核心组件已经齐了。

但问题是：

```text
组件存在
!=
别人能运行
```

所以 WP1 解决：

> 如何证明整个闭环不是一堆彼此独立的 Service，而是真的可以组合运行？

最终 Demo 真实调用：

```text
Trace Feedback
→ EvaluationPersistenceService
→ EvaluationLoopService
→ EvaluationComparisonService
→ RegressionReportService
```

并使用真实 PostgreSQL。

这证明的是：

```text
Composition Correctness
```

------

# 26. 为什么 Demo 使用 Fixture 而不是真实 LLM

因为 Demo 的目标是证明：

```text
Evaluation Platform workflow
```

而不是测试：

```text
某一个外部模型 API
```

使用真实 LLM 会引入：

-网络依赖；
-API Key；
-费用；
-随机性；
-模型版本漂移。

所以 deterministic FixtureExecutionTarget 更适合工程 Demo。

正确表述：

```text
REAL LOCAL POSTGRESQL
+
SYNTHETIC DETERMINISTIC DATA
```

而不是 Production Agent Evaluation。

------

# 27. WP1 最真实的 Bad Case：明文 DSN

第一次 Final Gate 实际失败：

```text
P1:
README / Makefile / CLI Help
包含 PostgreSQL DSN password
```

这个 Bad Case 很值得学习。

错误认知：

> “只是开发环境密码，没关系。”

实际工程规则：

> 用户可见文档、Help、脚本中不应该硬编码 Credential（凭据）。

最终修改：

```text
explicit --dsn
↓
AGENTEVALOPS_DEMO_DATABASE_URL
↓
project config
```

并重新 Final Gate PASS。

所以 Stage4 不只是有“假设构造 Bad Case”，也有真实独立审查发现的问题。

------

# 28. Phase5-WP2：业务 FAIL 与技术 FAIL 为什么一定要区分

应用层：

```text
ReleaseDecision.PASS
ReleaseDecision.FAIL
```

进程层需要：

```text
0
2
1
```

最终：

```text
0 = Gate PASS
2 = Gate FAIL
1 = Technical Error
```

为什么不用：

```text
PASS=0
everything else=1
```

因为 CI 看到 `1` 时无法知道：

> 是候选版本质量不合格？

还是：

> AgentEvalOps 数据库挂了？

这两个处理方式完全不同。

------

# 29. 为什么 Gate FAIL 用 exit 2

`2` 本身不是重点。

重点是：

```text
Business Failure
≠
Technical Failure
```

所以建立稳定 contract：

```text
Gate FAIL → 2
Technical Error → 1
```

未来：

- CI 日志；
  -告警；
  -统计；
  -自动处理

都能准确区分。

------

# 30. CI CLI 为什么不能自己判断 Regression

错误：

```text
if regression_count > 0:
    exit 2
```

这会让 CLI 成为第二个 Release Policy Owner。

正确：

```text
RegressionReportService
        ↓
ReleaseDecision
        ↓
CLI Adapter
        ↓
Exit Code
```

所以 CLI 只是：

```text
Protocol Adapter
```

不是：

```text
Decision Engine
```

------

# 31. 为什么 FAIL 也必须生成 Artifact

CI 红灯只能告诉你：

```text
failed
```

但工程人员还需要知道：

```text
哪个 Critical Case？
为什么 block？
REGRESSION 还是 NOT_COMPARABLE？
Baseline/Candidate 是谁？
```

所以必须：

```text
write artifact
↓
exit 2
```

而不是：

```text
exit 2
↓
report lost
```

Workflow 使用：

```text
if: always()
```

保证 Gate FAIL 后 artifact upload 仍执行。

------

# 32. 为什么没有直接修改 production release.yml

因为目前：

```text
baseline/candidate
=
synthetic
```

如果此时把 synthetic Gate 挂进 production release pipeline：

> 会制造错误的生产能力叙事。

所以 Stage4 有意只做：

```text
STATIC_WORKFLOW_PROVEN
```

没有宣称：

```text
REMOTE_EXECUTION_PROVEN
PRODUCTION_RELEASE_PROVEN
```

这是非常重要的工程真实性原则。

------

# 33. 整个 Stage4 的 Owner 思维

最终 Owner Matrix 可以浓缩成：

| 事实              | 唯一 Owner                   |
| ----------------- | ---------------------------- |
| Failure semantics | Online Core                  |
| Trace Candidate   | TraceService                 |
| Feedback          | TraceFeedbackService         |
| Run               | EvaluationPersistenceService |
| Attempt execution | EvaluationLoop + Persistence |
| Result            | Evaluation Result Repository |
| Regression        | EvaluationComparisonService  |
| Criticality       | Caller                       |
| Report            | RegressionReportService      |
| ReleaseDecision   | RegressionReportService      |
| Demo              | Orchestration only           |
| Exit code         | CI Adapter                   |
| Job status        | GitHub Actions               |

这张表如果真正理解了，你对 Stage4 架构基本就掌握了。

------

# 34. 整个 Stage4 的 Persistence 思维

另一个非常重要的区分：

```text
Durable Fact
Derived Fact
In-memory Fact
File Artifact
```

具体：

| 内容                             | 类型                |
| -------------------------------- | ------------------- |
| Trace / Span                     | Durable PostgreSQL  |
| EvaluationRun                    | Durable PostgreSQL  |
| ExecutionAttempt                 | Durable PostgreSQL  |
| EvaluationResult                 | Durable PostgreSQL  |
| TraceEvidenceCandidate           | Derived / In-memory |
| DatasetVersion / TestCaseVersion | 当前 In-memory      |
| RegressionComparison             | Derived             |
| RegressionReport                 | Derived             |
| ReleaseDecision                  | Derived             |
| CI report JSON                   | File Artifact       |



所以：

> “系统里有对象”不等于“这个对象已经 durable”。

------

# 35. Stage4 最重要的十个工程原则

| 原则                            | 核心含义                                |
| ------------------------------- | --------------------------------------- |
| Observation ≠ Evaluation        | Trace 不能直接当 Evaluation Truth       |
| Single Owner                    | 一个业务事实只能有一个权威 Owner        |
| Versioned Facts                 | Dataset/TestCase 等事实不能随意原地修改 |
| Append-only Result              | 最终评估结果不覆盖历史                  |
| CAS                             | 并发 ownership 在数据库层解决           |
| Fencing                         | stale owner 不能继续写                  |
| Retry creates fact              | Retry 新建 Attempt，而不是 reset        |
| Fail closed                     | Unknown/invalid state 不能默认通过      |
| Business FAIL ≠ Technical ERROR | Release Gate 与系统异常分开             |
| Evidence ≠ Ground Truth         | Production Trace 只是证据来源           |

------

# 36. Stage4 的真实验证证据

最终 Closeout 当前 HEAD：

```text
Cross-phase PostgreSQL Regression:
61 passed

Full Unit:
580 passed

Ruff:
PASS

uv lock:
PASS

compileall:
PASS

Alembic:
d3a4e5f6b7c8 single head
```



但面试时不要把所有历史测试数量加起来，说成：

> “总共几千个测试。”

正确表达是：

> 各 WP 有独立 Gate，最终 Stage4 Closeout 又在当前 HEAD 上重新执行了 61 个跨阶段 PostgreSQL Regression 和 580 个 Unit。

------

# 37. Stage4 的真实性等级

需要牢牢记住：

```text
LOCAL_POSTGRESQL_PROVEN
SYNTHETIC_DEMO_PROVEN
STATIC_WORKFLOW_PROVEN
DOMAIN_CONCURRENCY_CORRECTNESS_PROVEN
```

但没有：

```text
PRODUCTION_MULTI_WORKER_PROVEN
REMOTE_GITHUB_ACTIONS_PROVEN
PRODUCTION_RELEASE_GATE_PROVEN
PRODUCTION_EXECUTION_TARGET_PROVEN
```



这套表达非常适合面试，既体现工程成果，又不会过度包装。

------

# 38. 整个 Stage4 最值得讲的 Bad Cases

如果面试只允许讲 5 个，我建议优先这几个：

| Bad Case                             | 为什么有价值               |
| ------------------------------------ | -------------------------- |
| Trace 与 Evaluation 双 Owner         | 系统架构                   |
| Persisted Target projection 错误比较 | Contract / Projection 边界 |
| Concurrent Retry IntegrityError 泄漏 | PostgreSQL 并发            |
| Production Trace 自动当 TestCase     | 数据治理 / Agent Eval      |
| README / CLI Help 明文 DSN           | 安全 / Final Gate          |

其中 DSN 是实际 H-4 阻断后修复；其他还包括不同阶段源码审计、动态测试和构造验证发现的问题，面试时要按真实来源分类，不能全部说成线上事故。

------

# 39. 面试时不要按 Phase 流水账介绍

不要这样：

> Phase1 我做了 xxx，Phase2 我做了 xxx，Phase3……

更好的主线是：

```text
先讲原系统的问题
↓
讲 Owner 重构
↓
讲 Evaluation Runtime
↓
讲 Regression
↓
讲 Production Feedback
↓
讲 Demo / CI
↓
最后讲 Known Limitations
```

这样才像完整工程项目。

------

# 40. 30 秒总回答

> AgentEvalOps 是我基于 PandaProbe 改造的 Agent Evaluation 与 Regression 平台。核心是把 Trace 从 Evaluation Truth 里解耦出来，Trace 只作为 Observation 和 Evidence Source，然后建立独立的 Dataset/TestCase、ExecutionTarget、Run/Attempt/Result Runtime。
>
> Evaluation 执行层通过 PostgreSQL 做 atomic claim CAS、claim-token fencing、retry lineage 和 append-only Result；上层实现 Baseline/Candidate Regression、Critical Case Release Gate，并支持 failing Trace 显式回流成新的 TestCaseVersion。
>
> 最后我补了 deterministic closed-loop demo 和 CI Adapter，PASS 返回 0、业务 Gate FAIL 返回 2、技术错误返回 1。Stage4 已通过最终 Closeout，但 production CI、production target、多 Worker 等仍明确属于 Deferred。

------

# 41. 2 分钟总回答

> PandaProbe 原本主要围绕 Trace、Score 和 Monitoring，我改造时首先解决的是 Observation 和 Evaluation 的 Owner 问题。我把 Trace 定义为线上观测事实和 Evidence Source，而 Evaluation Domain 独立拥有 Dataset、TestCase、ExecutionTarget、Run、Attempt、Result、Regression 和 Release Gate。
>
> Online 侧先做 Generic Normalization，把不同 Runtime 的 Trace/Span 映射到统一模型，并定义 FAILURE、CANCELLED、TIMEOUT 才属于 failure，UNKNOWN 不会污染 failure_count。失败 Trace 可以生成 TraceEvidenceCandidate，但不会直接变成 TestCase。
>
> Offline Evaluation 侧，我用 PostgreSQL 实现了 Run/Attempt/Result Runtime，通过 atomic claim CAS 解决并发 claim，通过 claim token fencing 防 stale worker 回写，Retry 新建 child Attempt 而不是覆盖旧事实，最终 Result append-only。
>
> 有稳定 Result 后，再做 Baseline/Candidate Comparison，以 case/version/evaluator 为 key 对齐，产生 regression、improvement、unchanged、not comparable。Criticality 是 caller-supplied，最后由 RegressionReportService 统一产生 ReleaseDecision。
>
> Production Feedback 里我没有自动把 Trace payload 复制成测试集，而是要求 caller 提供 sanitized input、expected output、criticality 和版本，再生成新的 Dataset/TestCase Version。
>
> 最后通过真实 PostgreSQL synthetic Demo 验证整个闭环，并加一个 CI adapter，把 PASS 映射为 0、业务 Gate FAIL 映射为 2、技术异常映射为 1。当前 Stage4 最终 Closeout 是 P0/P1 都为 0，当前 HEAD 有 61 个跨阶段 PostgreSQL 回归和 580 个 Unit 通过，但 Remote GitHub Actions 和 Production Release Gate 仍没有宣称完成。

------

# 42. 面试追问速答

| 问题                                | 一句话答案                                        |
| ----------------------------------- | ------------------------------------------------- |
| Trace 为什么不是 EvaluationResult？ | Observation 和 Evaluation 的 provenance 不同      |
| 为什么 UNKNOWN 不算 failure？       | 信息不足不能自动转成失败事实                      |
| 为什么 Run/Attempt 分开？           | 支持 Retry、并发 claim 和执行历史                 |
| CAS 解决什么？                      | 多 worker 同时 claim                              |
| Fencing 解决什么？                  | stale worker 恢复后非法写入                       |
| Retry 为什么不 reset？              | 保留历史 execution fact                           |
| Result 为什么 append-only？         | 保证 Regression 历史可信                          |
| Criticality 谁决定？                | Caller                                            |
| 为什么 Trace 不自动回流？           | 缺 sanitization/expected output/version authority |
| 为什么 FAIL exit 2？                | 区分 Business Gate FAIL 和 Technical Error        |
| CI 判断 ReleaseDecision 吗？        | 不，RegressionReportService 判断，CI 只适配       |
| GitHub Actions 真跑过吗？           | 没有，当前是 Static Workflow Proven               |
| Production-ready 吗？               | Partial，不是完全 production-ready                |

------

# 43. Known Limitations 应如何统一回答

不需要面试时罗列几十个点，可以归纳为：

| 类别                | 当前缺口                                      |
| ------------------- | --------------------------------------------- |
| Production Runtime  | production target、多 Worker wiring           |
| Persistence         | Durable Dataset Catalog、Report persistence   |
| Automation          | baseline selection、candidate discovery       |
| Human               | Human Review / Judge Agreement                |
| Protocol            | OTLP、Retention / Redaction                   |
| CI/CD               | Remote Action、production release integration |
| Evaluation Coverage | 当前 Demo 是 synthetic fixture                |

这些全部属于当前 Deferred，不阻断 Stage4 完成和投递。

------

# 44. 简历里最安全的核心表述

> 基于 PandaProbe 改造 Agent Evaluation & Regression 平台，完成 Generic Trace Normalization、Trace Evidence Feedback、版本化 Evaluation Domain、Run/Attempt/Result 执行模型、Baseline/Candidate Regression 与 Critical Case Release Gate。

> 基于 PostgreSQL 实现 Evaluation Attempt 的 Atomic Claim CAS、Claim Token Fencing、Retry Lineage 与 append-only Result，保证并发执行下状态与最终 Result 收敛。

> 构建 failing Trace → TestCase/Dataset → Evaluation → Regression → ReleaseDecision 的 deterministic closed-loop demo，并实现 CI Gate Adapter，将 PASS / Gate FAIL / Technical Error 分别映射为进程退出码 0 / 2 / 1。

这些都由最终 Stage4 Closeout 支持。

------

# 45. 最不能写进简历的内容

当前不能写成：

> Production GitHub Actions 已自动阻断真实模型版本发布。

> 已实现 Production Multi-worker exactly-once。

> Dataset / RegressionReport 已全部持久化。

> 已支持 OTLP、真实 Production ExecutionTarget 和 external LLM。

> 已建立完整 Human Review Platform。

> AgentEvalOps 已 Production Ready。

最终真实状态是：

```text
INTERVIEW_READY = YES
Production Ready = PARTIAL
```



------

# 46. Stage4 最终应该形成的知识框架

可以把整个 Stage4 压缩成下面这张图：

```text
┌──────────────────────────────┐
│ Observation                  │
│ Trace / Span                 │
│ Generic Normalization        │
│ Metrics / Failure Detection  │
└──────────────┬───────────────┘
               │ Evidence
               ▼
┌──────────────────────────────┐
│ Feedback Boundary            │
│ Sanitization                 │
│ Version / Expected Output    │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ Evaluation Domain            │
│ Dataset / TestCase           │
│ ExecutionTarget              │
│ Run / Attempt / Result       │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Regression                   │
│ Baseline vs Candidate        │
│ Comparison                   │
│ Critical Case                │
│ RegressionReport             │
│ ReleaseDecision              │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Delivery / Automation        │
│ Closed-loop Demo             │
│ CLI Exit Contract            │
│ GitHub Actions Adapter       │
└──────────────────────────────┘
```

真正掌握 Stage4，不是记住每个 WP 做了什么，而是理解这四层之间的 **Truth、Owner、Persistence 和 Adapter Boundary**。

------

# 47. 最后需要真正背住的 10 句话

> **1. Trace 是 Observation，不是 Evaluation Truth。**

> **2. Evidence Candidate 不等于 TestCase，中间必须存在显式 Feedback Boundary。**

> **3. Evaluation 的可信来源是 versioned Case + Run + Attempt + Result provenance。**

> **4. Atomic Claim CAS 解决并发抢占，Claim Token Fencing 解决 stale owner 回写。**

> **5. Retry 是新 Attempt，而不是重置旧 Attempt。**

> **6. EvaluationResult 是 append-only fact，Regression 才能建立在稳定历史上。**

> **7. Regression Classification、Criticality 和 ReleaseDecision 必须分别有唯一 Owner。**

> **8. Production Trace 回流不能自动推断 sanitization、expected output、criticality 和 version。**

> **9. ReleaseDecision 是业务事实，exit code 是 Process Contract；Gate FAIL 和 Technical Error 必须区分。**

> **10. Stage4 已经 Interview Ready，但只有 Partial Production Ready；不夸大证据边界本身就是工程能力。**

最终 Stage4 已正式：

```text
STAGE4_PROJECT_GATE: PASS
STAGE4_FREEZE: YES
NO_MORE_STAGE4_FEATURE_EXPANSION: YES
INTERVIEW_READY: YES
APPLICATION_PRIORITY: APPLY_NOW
```

所以这部分学习完成之后，**重点已经不应该继续做 Stage4 功能，而应该把 LocalAgent + AgentEvalOps 两个项目整合到简历、项目追问材料和实际投递中。**