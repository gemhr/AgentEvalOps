# 推荐面试材料文件名

```
AGENTEVALOPS_STAGE4_COMPLETE_EVALUATION_REGRESSION_PLATFORM_INTERVIEW_MATERIAL.md
```

下面这份作为 **Stage4 整体学习 / 面试总结**。它不是再按 WP 重复堆内容，而是把 Phase0～Phase5 串成一条可以在面试中讲清楚的完整工程主线。最终 Closeout 已确认：`STAGE4_PROJECT_GATE: PASS`、`OPEN_P0=0`、`OPEN_P1=0`、`INTERVIEW_READY=YES`、`STAGE4_FREEZE=YES`，但 `Production Ready=PARTIAL`。

------

# 1. 一句话项目 / Stage 定义

> 我基于 PandaProbe 改造了一个面向 AI Agent 的 Evaluation & Regression Platform（评估与回归平台），把生产 Trace 从“观测数据”转换成可回流的 Evaluation Evidence（评估证据），建立 Dataset/TestCase、Run/Attempt/Result、Baseline/Candidate Regression、Critical Case Release Gate，再通过可执行 Demo 和 CI Adapter 把质量决策映射到进程退出契约。

最终核心链路：

```text
Trace
→ Generic Online Normalization
→ Failure Detection
→ TraceEvidenceCandidate
→ Trace-to-Dataset Feedback
→ DatasetVersion / TestCaseVersion
→ EvaluationRun / Attempt / Result
→ Baseline / Candidate Comparison
→ RegressionReport
→ ReleaseDecision
→ Closed-loop Demo
→ CI Exit Contract
```

Stage4 推荐范围已经正式完成并冻结。

------

# 2. 为什么要做 Stage4

原始 PandaProbe 更偏向：

```text
Trace
→ Score
→ Monitor
```

这条链路适合观察“发生了什么”，但不足以回答：

- 某个 Agent 版本是否比 baseline 退化？
- 某条生产失败 Trace 能否转成回归样例？
- Critical Case 是否应该阻断候选版本？
- 一次 Evaluation 是否可追踪 Run / Attempt / Result？
- CI 如何消费质量决策？

所以 Stage4 真正解决的是：

> **把 Observation（观测）系统演进成 Evaluation（评估）系统。**

核心思想：

```text
Trace ≠ Evaluation Result
```

Trace 是：

```text
Observation / Evidence Source
```

而不是：

```text
Evaluation Truth
```

这也是整个 Stage4 最重要的架构转折。

------

# 3. 真实性与完成边界

## 已真实实现

Phase0～Phase5 推荐范围全部完成：

```text
Phase0 Source Audit                  COMPLETE
Phase1 Generic Online Core          COMPLETE
Phase2 Offline Evaluation Core      COMPLETE
Phase3 Regression Core              COMPLETE
Phase4 Minimal Feedback Loop        COMPLETE
Phase5 Minimal Hardening            COMPLETE
```



## 已真实验证

当前 HEAD Closeout 实际重新执行：

```text
跨阶段 PostgreSQL Regression:
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
single head
```



## 没有完成

明确没有：

- Production ExecutionTarget
- Remote GitHub Actions execution proof
- production `release.yml` integration
- production multi-worker wiring
- Durable Dataset Catalog
- Report / ReleaseDecision persistence
- Human Review
- OTLP Adapter
- automatic baseline selection
- candidate version discovery。

因此：

```text
Production Ready = PARTIAL
```

而不是 YES。

------

# 4. 架构演进：Stage4 最重要的故事

整个 Stage4 可以理解成五次演进。

## 第一次：Trace 与 Evaluation 解耦

从：

```text
Trace
→ Score
```

变成：

```text
Trace
→ Evidence

Evaluation Domain
→ 独立业务事实
```

这是 Phase0。

------

## 第二次：建立 runtime-neutral Online Core

从 LocalAgent / Legacy 特定 Trace：

```text
LocalAgent Trace
Legacy Trace
```

统一投影为：

```text
NormalizedOnlineTrace
NormalizedOnlineSpan
```

建立统一：

```text
GenericOutcome
Failure Semantics
Metrics
TraceEvidenceCandidate
```

这是 Phase1。

------

## 第三次：Evaluation 成为独立 Runtime

建立：

```text
DatasetVersion
TestCaseVersion
ExecutionTarget
EvaluationRun
ExecutionAttempt
EvaluationResult
```

Evaluation 不再依赖 Trace 作为状态机 Owner。

这是 Phase2。

------

## 第四次：从单次 Evaluation 演进到版本回归

```text
Baseline
    │
    ├── Result
    │
Candidate
    │
    └── Result
        ↓
Comparison
        ↓
RegressionReport
        ↓
ReleaseDecision
```

这是 Phase3。

------

## 第五次：把线上失败反馈回离线评估

```text
Production Trace
→ EvidenceCandidate
→ Feedback
→ New TestCaseVersion
→ New DatasetVersion
```

然后再进入 Evaluation / Regression。

这是 Phase4。

------

## 最后：让整个系统可见、可消费

Phase5：

```text
Closed-loop Demo
+
CI Release Gate Adapter
```

把“内部能力”变成“外部可执行工作流”。

------

# 5. Phase0：最关键的架构决策

Phase0 最大的价值不是写代码，而是冻结 Owner。

最终明确：

```text
Trace Domain
=
Observation / Compatibility / Evidence Source

Evaluation Domain
=
Dataset / TestCase
Execution
Run / Attempt / Result
Comparison
Release Gate
```



这个决策避免了最危险的问题：

> 把 Trace、Score、EvaluationResult 混成一套数据模型。

这样后续才能保证：

```text
Observation Truth
≠
Evaluation Truth
```

------

# 6. Phase1：Generic Online Core

Phase1解决：

> “不同 Agent Runtime 的 Trace 如何统一进入 AgentEvalOps？”

最终：

```text
Runtime-specific Trace
        ↓
Generic Normalization
        ↓
Normalized Trace / Span
```

并定义：

```text
SUCCESS
FAILURE
CANCELLED
TIMEOUT
UNKNOWN
```

Failure rule：

```text
FAILURE
CANCELLED
TIMEOUT
→ failure

UNKNOWN
→ NOT failure
```



这里非常重要：

```text
UNKNOWN
!=
FAILURE
```

不能因为“不知道”就污染 failure metrics。

Phase1 进一步产生：

```text
TraceEvidenceCandidate
```

它只是候选证据，不是自动的 TestCase。

------

# 7. Phase2：Evaluation Runtime

这是 Stage4 后端工程深度最高的一部分。

## 核心模型

```text
EvaluationRun
     ↓
ExecutionAttempt
     ↓
EvaluationResult
```

同时还有：

```text
DatasetVersion
TestCaseVersion
ExecutionTarget
EvaluatorSpec
EvaluationSuiteVersion
```

------

## 状态机

Run：

```text
PENDING
→ RUNNING
→ COMPLETED
  / FAILED
  / OUTCOME_UNKNOWN
```

Attempt：

```text
PENDING
→ CLAIMED
→ RUNNING
→ terminal
```

------

## 并发控制

实现：

```text
Atomic Claim CAS
Claim Token Fencing
Retry Lineage
Stale Reconciliation
Logical Result Uniqueness
Append-only Result
```



核心思想：

> 一个 Attempt 被某个 worker claim 后，后续写入必须证明“我仍然是这个 Attempt 的合法 owner”。

所以不是：

```text
status == RUNNING
```

就可以写。

而是必须同时满足：

```text
claim_token match
```

这就是 fencing（栅栏令牌）。

------

# 8. Phase2 最重要的数据一致性原则

## Run / Attempt / Result 分层

Run：

> 一次 Evaluation execution 的整体事实。

Attempt：

> 对某个 Case 的一次执行尝试。

Result：

> 已最终确定的 Evaluation fact。

------

## Retry 不修改历史

错误：

```text
FAILED Attempt
→ reset PENDING
```

正确：

```text
Attempt A FAILED
        ↓
Attempt B retry child
```

所以历史事实不可覆盖。

------

## Result append-only

一旦：

```text
EvaluationResult finalized
```

就不能 mutable update。

这样 Regression 才有可信基础。

------

# 9. Phase3：Regression Comparison

Phase3 解决：

> “Baseline 和 Candidate 怎么比较？”

Alignment key（对齐键）：

```text
(
  case_id,
  case_version,
  evaluator_id,
  evaluator_version
)
```

然后分类：

```text
PASS → FAIL
= REGRESSION

FAIL → PASS
= IMPROVEMENT

same
= UNCHANGED

缺失 / ERROR / INCONCLUSIVE
= NOT_COMPARABLE
```

------

## Critical Case

Criticality 不是系统自动判断：

```text
Criticality:
CALLER_SUPPLIED
```



这是很重要的 Owner 原则。

系统不能因为：

```text
regression
```

就偷偷推断：

```text
critical
```

因为 Criticality 本质是业务策略。

------

# 10. Phase3：Release Gate

最终：

```text
critical REGRESSION
→ BLOCK

critical NOT_COMPARABLE
→ BLOCK

non-critical REGRESSION
→ report only

non-critical NOT_COMPARABLE
→ report only
```

ReleaseDecision：

```text
PASS
FAIL
```

Owner：

```text
RegressionReportService
```

而不是 CI。

这使得后面 Phase5 的 CLI 只能消费 Decision，不能重新计算 Gate。

------

# 11. Phase4：Trace-to-Dataset Feedback

Phase4解决：

> “线上失败怎样回到离线 Evaluation？”

最终：

```text
Failing Trace
    ↓
TraceEvidenceCandidate
    ↓
TraceFeedbackCommand
    ↓
TraceFeedbackService
    ↓
TestCaseVersion
    ↓
DatasetVersion
```



核心设计：

```text
Production Trace
≠
Automatically trusted TestCase
```

------

## Sanitization

由 caller负责：

```text
SANITIZATION:
CALLER_SUPPLIED
```

系统不自动复制生产 payload。

------

## Expected Output

同样不自动推断：

```text
expected_output:
caller supplied / optional
```

因为：

> 生产中的实际输出不等于正确答案。

------

## Dataset 更新

不是 mutate：

```text
DatasetVersion V1
→ modify V1
```

而是：

```text
DatasetVersion V1
→ DatasetVersion V2
```

即：

```text
NEW_VERSION
```

------

# 12. Phase5-WP1：Closed-loop Demo

WP1解决的是：

> “系统真的能串起来吗？”

真实 Demo：

```text
Synthetic failing Trace
→ TraceFeedbackService
→ Dataset / TestCase
→ Baseline Evaluation
→ Candidate Evaluation
→ Comparison
→ RegressionReport
→ ReleaseDecision
```

而且是真 PostgreSQL，不是直接 new Result。



------

## 默认 Scenario

三个 Case：

```text
PASS → PASS
= UNCHANGED

FAIL → PASS
= IMPROVEMENT

PASS → FAIL
= REGRESSION
```

Critical Case 是第三个。

所以：

```text
ReleaseDecision.FAIL
```

但：

```text
Demo process exit = 0
```

因为 Demo 成功展示 FAIL，本身不是程序错误。

------

# 13. Phase5-WP2：CI Release Gate

WP2再加最后一层：

```text
ReleaseDecision
→ Process Exit Code
```

冻结：

```text
PASS
→ 0

Gate FAIL
→ 2

Technical Error
→ 1
```



这一步非常适合面试。

因为它体现：

```text
Business Failure
!=
Technical Failure
```

------

# 14. 最终 Owner Matrix

| Truth / Operation             | Owner                            |
| ----------------------------- | -------------------------------- |
| Trace source truth            | Trace Repository / Ingestion     |
| Generic normalized projection | Online Core                      |
| Failure semantics             | Generic Online failure predicate |
| TraceEvidenceCandidate        | TraceService                     |
| Feedback                      | TraceFeedbackService             |
| Dataset/TestCase facts        | Evaluation Catalog Domain        |
| EvaluationRun                 | EvaluationPersistenceService     |
| ExecutionAttempt              | Persistence + EvaluationLoop     |
| EvaluationResult              | Result Repository                |
| Regression Classification     | EvaluationComparisonService      |
| Criticality                   | Caller                           |
| RegressionReport              | RegressionReportService          |
| ReleaseDecision               | RegressionReportService          |
| Demo orchestration            | `run_closed_loop_demo`           |
| Exit mapping                  | CI CLI Adapter                   |
| CI Job Status                 | GitHub Actions                   |

Final Closeout 没发现 Dual Owner。

------

# 15. Persistence Matrix

理解这个表非常重要。

| Fact                           | 类型               |
| ------------------------------ | ------------------ |
| Trace                          | PostgreSQL durable |
| Span                           | PostgreSQL durable |
| Normalized projection          | PostgreSQL         |
| EvaluationRun                  | PostgreSQL         |
| ExecutionAttempt               | PostgreSQL         |
| EvaluationResult               | PostgreSQL         |
| TraceEvidenceCandidate         | In-memory derived  |
| Feedback Result                | In-memory          |
| DatasetVersion/TestCaseVersion | 当前 In-memory     |
| RegressionComparison           | Derived            |
| RegressionReport               | Derived            |
| ReleaseDecision                | Derived            |
| CI artifact                    | File output        |



所以不能说：

> RegressionReport 已经持久化。

也不能说：

> Dataset Catalog 已经 durable。

------

# 16. Stage4 真实 Bad Cases

## Bad Case 1：Trace 与 Evaluation 双 Owner

错误：

```text
Trace Score
=
Evaluation Result
```

后果：

- 在线观测和离线评估耦合；
  -版本难以冻结；
- Regression truth 不可靠。

修复：

```text
Trace = Evidence
EvaluationResult = Evaluation truth
```

------

## Bad Case 2：UNKNOWN 当 failure

错误：

```text
UNKNOWN → failure_count
```

会污染生产指标。

修复：

```text
UNKNOWN != FAILURE
```

------

## Bad Case 3：Run 和 Plan / Runtime 状态混写

Stage4 Evaluation 同样坚持：

```text
Execution truth
只属于 Run / Attempt
```

而不是静态 Catalog。

------

## Bad Case 4：Retry 覆盖旧 Attempt

错误：

```text
FAILED
→ reset
```

修复：

```text
new retry child
```

保留 lineage。

------

## Bad Case 5：并发 worker 同时 finalize

通过：

```text
CAS
claim token
logical uniqueness
```

保证 DB truth 收敛。

------

## Bad Case 6：Production Trace 自动复制为 TestCase

风险：

- PII
- noisy payload
- wrong expected output。

修复：

```text
caller-supplied sanitized input
```

------

## Bad Case 7：Regression 自动变 Critical

错误：

```text
REGRESSION
→ automatically critical
```

修复：

```text
criticality caller-supplied
```

------

## Bad Case 8：CLI 重新实现 Gate

错误：

```text
if regression_count:
    exit 2
```

修复：

```text
report.release_decision
→ exit adapter
```

------

## Bad Case 9：Gate FAIL 和程序 Error 都 exit 1

修复：

```text
Gate FAIL = 2
Technical Error = 1
```

------

## Bad Case 10：Demo 泄露 PostgreSQL 密码

这是实际发生过的 P1。

第一次 WP1 Final Gate：

```text
FAIL
```

因为：

- README
- Makefile
- CLI help

包含明文 DSN password。

修复为：

```text
explicit --dsn
→ environment
→ project config
```

然后 H-4 rerun PASS。

这是非常好的真实安全 Bad Case。

------

# 17. 测试与 Gate 总结

最终 Stage4 的证据不是“跑了一个总测试数字”。

而是：

## Phase1

Online Core：

```text
WP1 full unit: 532
WP2 full unit: 541
```

## Phase2

Persistence / Loop：

```text
WP3 dynamic: 37
WP4 loop: 10
WP4 combined: 33
```

## Phase3

Regression：

```text
WP1 PG: 3
WP2 PG: 2
```

## Phase4

Feedback：

```text
PG E2E: 3
Full unit: 556
```

## Phase5

WP1：

```text
PG Demo: 3
Relevant: 44
Full unit: 569
```

WP2：

```text
PG CLI: 3
Relevant: 47
Full unit: 580
```

最终 Closeout 当前 HEAD：

```text
Cross-phase PostgreSQL:
61 passed

Unit:
580 passed
```



------

# 18. 高频面试主线

如果面试官让你完整介绍 AgentEvalOps，不要按 Phase1～5 报流水账。

按下面讲。

## 版本一：系统设计主线

> PandaProbe 原本更偏 Trace 和 Score，我改造时首先把 Observation 和 Evaluation 的 Owner 分开。Trace 只作为 Evidence Source，Evaluation 独立拥有 Dataset/TestCase、ExecutionTarget、Run/Attempt/Result。
>
> 然后我建立 Generic Online Core，把 LocalAgent 和 legacy Trace 统一投影成 runtime-neutral Trace/Span，并定义统一 failure semantics 和 TraceEvidenceCandidate。
>
> Offline 部分我建立 Evaluation Runtime，通过 PostgreSQL 实现 Run/Attempt/Result 持久化、atomic claim CAS、claim token fencing、retry lineage 和 append-only Result。
>
> 在此基础上再做 Baseline/Candidate Comparison、Critical Case Regression Report 和 ReleaseDecision。
>
> 最后把生产失败 Trace 通过显式 Feedback 转为新的 Dataset/TestCase Version，并做了 deterministic closed-loop demo 和 CI adapter。

------

# 19. 30 秒面试版本

> AgentEvalOps 是我基于 PandaProbe 改造成的 Agent Evaluation 与 Regression 平台。核心改造是把 Trace 从 Evaluation Truth 中解耦出来，让 Trace 只作为 Evidence Source，然后建立独立的 Dataset/TestCase、Run/Attempt/Result Runtime。
>
> Evaluation 执行层用 PostgreSQL 做了 atomic claim CAS、claim token fencing、retry lineage 和 append-only Result；上层实现 Baseline/Candidate Regression、Critical Case Release Gate，再把 failing Trace 显式回流成新 TestCaseVersion。
>
> 最后我做了真实 PostgreSQL closed-loop demo 和 CI adapter，其中 PASS=0、Gate FAIL=2、Technical Error=1。当前 Stage4 已完成并冻结，但 production CI、production target、durable catalog 等明确保留为 deferred。

------

# 20. 2 分钟面试版本

> 我这个项目一开始是基于 PandaProbe 做二次开发。最初最大的架构问题是 Trace、Score 和 Evaluation 的边界不够清楚，所以第一步我先把 Observation 和 Evaluation 解耦：Trace 只是线上事实和 Evidence Source，Evaluation Domain 独立拥有 Dataset、TestCase、ExecutionTarget、Run、Attempt、Result 和 Regression。
>
> Online 侧我做了 runtime-neutral 的 Trace/Span normalization，统一了 SUCCESS、FAILURE、CANCELLED、TIMEOUT、UNKNOWN，其中 UNKNOWN 不会被算 failure，再从 failing Trace 生成 TraceEvidenceCandidate。
>
> Offline Evaluation 是整个系统工程深度最高的地方。我通过 PostgreSQL 实现 EvaluationRun 和 ExecutionAttempt 状态机，用 atomic claim CAS 防止多个 worker 同时 claim，用 claim token fencing 防止旧 owner 回写，Retry 新建 child Attempt 而不是覆盖历史，最终 EvaluationResult 是 append-only 的。
>
> 在这些可信 Result 上再做 Baseline/Candidate Comparison，按照 case/version/evaluator 对齐，产生 regression、improvement、unchanged、not comparable。Criticality 由 caller 显式提供，最后 RegressionReportService 产生 ReleaseDecision。
>
> 为了闭环，我又做了 production Trace feedback，但不会自动把生产 payload 当测试数据，而是要求 caller 提供 sanitized input、expected output 和版本，再创建新的 DatasetVersion/TestCaseVersion。
>
> 最后我做了 synthetic closed-loop demo 和 CI adapter，CI 只消费 ReleaseDecision，不重新实现 Gate。PASS 返回 0，业务 Gate FAIL 返回 2，技术错误返回 1。Stage4 最终 Closeout 是 P0/P1 都为 0、580 个 Unit 和 61 个跨阶段 PostgreSQL 回归通过。

------

# 21. 最值得强调的工程能力

你可以把 Stage4 总结成六个能力。

### 1. Bounded Context（限界上下文）

```text
Trace
≠
Evaluation
```

### 2. State Machine（状态机）

```text
Run
Attempt
Result
```

### 3. Concurrency Control（并发控制）

```text
CAS
Fencing
Uniqueness
```

### 4. Immutable Facts（不可变事实）

```text
Append-only Result
Versioned Dataset/TestCase
```

### 5. Policy Ownership（策略所有权）

```text
Criticality = caller
ReleaseDecision = RegressionReportService
```

### 6. Adapter Boundary（适配器边界）

```text
Domain Decision
→ CLI Exit
→ CI
```

------

# 22. 高频追问

## Q1：为什么不直接把 TraceScore 当 EvaluationResult？

因为 TraceScore 属于 Observation，而 EvaluationResult 必须绑定：

- TestCaseVersion
- EvaluatorVersion
- ExecutionTarget
- Run
- Attempt。

它们的 provenance（来源链）不同。

------

## Q2：为什么 Result 要 append-only？

Regression 的基础必须是稳定历史事实。

如果 Candidate Result 可以事后修改，就无法可信地重现版本对比。

------

## Q3：claim token fencing 解决什么？

解决 stale worker。

例如：

```text
Worker A claim
→ lease过期

Worker B重新获得执行权

Worker A后来恢复
```

如果没有 token fencing，A 仍然可能写 Result。

------

## Q4：为什么 Retry 不直接 reset Attempt？

因为 Attempt 本身是历史事实。

Retry 是新的 execution fact。

------

## Q5：为什么 UNKNOWN 不算 failure？

UNKNOWN 表示没有足够信息判断，而不是已知失败。

------

## Q6：为什么生产 Trace 不能自动变 Dataset？

因为：

```text
observed input
!=
approved evaluation input
```

其中还有 sanitization、expected output、criticality、version ownership 等治理问题。

------

## Q7：为什么 criticality 不自动推断？

因为是否 critical 是产品 / 业务风险策略，不是 evaluator 能天然决定的。

------

## Q8：为什么 CI Gate FAIL 用 exit 2？

为了和：

```text
technical error = 1
```

区分。

------

## Q9：GitHub Actions 真跑过吗？

没有。

当前：

```text
STATIC_WORKFLOW_PROVEN
```

不是：

```text
REMOTE_EXECUTION_PROVEN
```



------

# 23. 最容易夸大的地方

下面这些一定不要说错。

## 不能说

> “已经 production-ready。”

正确：

```text
Production Ready: PARTIAL
```

------

## 不能说

> “有真实 production multi-worker proof。”

实际是：

```text
DOMAIN_CONCURRENCY_CORRECTNESS_PROVEN
```

------

## 不能说

> “GitHub Actions 已真实线上执行。”

实际：

```text
NOT_EXECUTED
```

------

## 不能说

> “DatasetVersion 已持久化。”

当前：

```text
IN_MEMORY
```

------

## 不能说

> “RegressionReport 已持久化。”

当前：

```text
DERIVED
```

------

## 不能说

> “CI 已保护 production release。”

`release.yml` 没有修改。

------

# 24. Known Limitations

统一记成八类即可。

## A. Productionization

- production ExecutionTarget 无
- production multi-worker wiring 无。

## B. Persistence

- Durable Dataset Catalog 无
- Report/Decision persistence 无
- DB-only replay 无。

## C. Automation

- automatic baseline selection 无
- candidate discovery 无
- feedback automation 无。

## D. Evaluation Coverage

- 当前 Demo 是 deterministic synthetic fixture
- real external model evaluation 未接。

## E. Human Workflow

- Human Review 无
- Judge/Human Agreement 无。

## F. Protocol

- OTLP Adapter 无
- retention/redaction automation 无。

## G. CI

- Remote GitHub Actions 未执行
- production release integration 无。

## H. Environment

- 历史 dev DB schema drift 等环境限制。

这些全部是 P2 / Deferred，不阻断当前投递。

------

# 25. P0 / P1

Stage4 最终：

```text
OPEN_P0 = 0
OPEN_P1 = 0
```

历史关闭 P1 包括：

- Phase1 legacy Trace tenant integrity
- Phase1 legacy Span tenant integrity
- Phase2 persistence dynamic findings
- Phase2 WP4 Target projection
- Phase5-WP1 plaintext DSN credential。



面试时反而可以讲：

> 我们不仅记录最终通过，还保留了 Final Gate 曾经失败以及 corrective remediation 的历史。

这比“所有东西一次就过”更像真实工程。

------

# 26. 最终速查表

| 维度                              | 当前真实状态           |
| --------------------------------- | ---------------------- |
| Stage4                            | COMPLETE_AND_FROZEN    |
| Open P0                           | 0                      |
| Open P1                           | 0                      |
| Online normalization              | YES                    |
| Online metrics                    | YES                    |
| Trace Evidence Candidate          | YES                    |
| Dataset/TestCase Domain           | YES                    |
| Durable Dataset Catalog           | NO                     |
| Run / Attempt                     | PostgreSQL             |
| Result                            | PostgreSQL append-only |
| CAS                               | YES                    |
| Claim Fencing                     | YES                    |
| Retry lineage                     | YES                    |
| Comparison                        | YES                    |
| RegressionReport                  | YES                    |
| ReleaseDecision                   | YES                    |
| Criticality                       | CALLER_SUPPLIED        |
| Feedback                          | YES                    |
| Sanitization                      | CALLER_SUPPLIED        |
| Auto Evaluation                   | NO                     |
| Closed-loop Demo                  | REAL_LOCAL_SYNTHETIC   |
| Demo PostgreSQL                   | YES                    |
| CI Exit Contract                  | PASS                   |
| PASS exit                         | 0                      |
| Gate FAIL exit                    | 2                      |
| Technical Error exit              | 1                      |
| GitHub workflow                   | STATIC_WORKFLOW_PROVEN |
| Remote Actions                    | NOT_EXECUTED           |
| Production Release Gate           | NO                     |
| Production multi-worker proof     | NO                     |
| Current cross-phase PG regression | 61 passed              |
| Full Unit                         | 580 passed             |
| Interview Ready                   | YES                    |
| Production Ready                  | PARTIAL                |
| Recommended Action                | APPLY_FIRST            |



------

# 最后真正需要记住的 8 句话

> **1. Trace 是 Observation / Evidence，不是 Evaluation Truth。**

> **2. Evaluation 的可信基础是版本化 Case + Run/Attempt/Result provenance，而不是一个简单 Score。**

> **3. 并发正确性靠 CAS、claim token fencing 和数据库唯一约束共同保证。**

> **4. Retry 应新增事实，而不是覆盖旧事实。**

> **5. Regression Classification、Criticality、ReleaseDecision 必须有清晰且唯一的 Owner。**

> **6. Production Trace 回流到 Dataset 必须经过显式的 sanitization、expected-output 和 version ownership 边界。**

> **7. Business Gate FAIL 与 Technical Error 是两类完全不同的失败，因此分别使用 exit 2 和 exit 1。**

> **8. Stage4 已经足够支撑求职，但“Interview Ready”不等于“Production Ready”；真实边界比堆更多功能更重要。**