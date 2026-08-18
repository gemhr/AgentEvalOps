# 1. 一句话项目 / 工作包定义

Stage4-Phase4-WP1 完成了 AgentEvalOps 的 **Trace-to-Dataset Feedback Foundation（Trace 到数据集反馈基础）**：

> 将线上 Generic Online Core（通用在线核心）发现的 failing Trace（失败 Trace），经过 project-scoped（项目作用域）校验后，转换成 caller-confirmed（调用方确认）的 `TestCaseVersion` 和新的 `DatasetVersion`，并保留原始 Trace 的 `EvidenceRef`；随后可由调用方显式进入现有 Evaluation Loop（评估循环），最终把生产问题证据传递到 `EvaluationResult`，形成最小生产反馈闭环。

最终独立 Gate：

```text
CODE_REVIEW_GATE: PASS
PHASE4_WP1_POSTGRESQL_DYNAMIC: PASS
STAGE4_PHASE4_WP1_PROJECT_GATE: PASS
PHASE4_PRODUCTION_FEEDBACK_LOOP: COMPLETE
```

这里的 `COMPLETE` **只表示 Minimal Production Feedback Loop（最小生产反馈闭环）完成**，Human Review、Alert、Monitor redesign、Judge/Human Agreement 仍然是 Deferred。

------

# 2. 为什么做

在 WP1 之前，Stage4 已经有这样一条链路：

```text
Production Trace
        ↓
Generic normalization
        ↓
Online metrics
        ↓
failing Trace
        ↓
TraceEvidenceCandidate
        ↓
EvidenceRef
```

问题是链路停在了 `EvidenceRef`。

也就是说系统已经知道：

> “线上出现了一个值得关注的失败 Trace。”

但还没有真正回答：

> “这个生产失败怎么安全地进入离线回归体系？”

Phase4 H-1 审计确认，旧 Roadmap 中 Monitor、趋势、采样等很多能力已经存在或价值较低，当前唯一高价值缺口就是：

```text
TraceEvidenceCandidate
→ TestCaseVersion
→ DatasetVersion
→ Evaluation
```

因此 Phase4 最终只推荐 **1 个 WP**，专门完成这个连接。

------

# 3. 真实性与完成边界

## 已真实实现

新增核心能力：

- `TraceFeedbackCommand`
- `TraceFeedbackService`
- failing Trace 校验
- cross-project feedback fail closed
- caller-supplied sanitized `input_payload`
- caller-supplied optional `expected_output`
- caller-supplied dataset/case version
- `TestCaseVersion` 构造
- `DatasetVersion` NEW_VERSION
- base case refs 保留
- Trace `EvidenceRef` 写入 TestCase
- feedback 与 Evaluation execution 分离
- feedback-created case 可显式进入现有 `create_run → execute_attempt`
- Trace Evidence 最终可从 `EvaluationResult` 读回。

## 已真实测试

Final Gate 实际验证：

- Focused Unit：**15 passed**
- PostgreSQL Feedback E2E：**3 passed**
- child Span failure direct PostgreSQL probe：PASS
- cross-project PostgreSQL：PASS
- Phase1 regression：**21 passed**
- Phase2/Phase3相关 PostgreSQL regression：**52 passed**
- Full Unit：**556 passed**
- Ruff / lock / diff / compileall / Alembic：全部 PASS。

## 明确未实现

没有：

- Durable Dataset Catalog（持久化数据集目录）
- feedback HTTP API
- UI
  -自动脱敏
  -自动 TestCase生成
  -自动 expected output推断
  -自动 criticality推断
  -自动 Evaluation
- feedback dedupe
- Human Review
- Alert
- Monitor redesign
- Judge/Human Agreement。

------

# 4. 修改前架构与根因

修改前：

```text
Production failing Trace
        ↓
TraceEvidenceCandidate
        ↓
EvidenceRef
        ↓
        STOP
```

另一方面 Phase2 已经有：

```text
DatasetVersion
TestCaseVersion
EvaluationPersistenceService
EvaluationLoopService
EvaluationResult
```

但是这两套能力没有连接。

所以问题并不是“缺 Evaluation Engine”，而是缺少一个 **Application Boundary（应用层边界）**：

```text
Online Evidence
        ↓
explicit feedback action
        ↓
Evaluation Catalog Fact
```

如果没有这层，就只能手工在两个模块之间拼对象。

------

# 5. 方案讨论与取舍

## 方案一：自动从 Trace 生成 TestCase

最直观的方案是：

```text
Trace
→ 自动提取 input
→ production output 当 expected output
→ 自动生成 TestCase
```

这个方案被明确拒绝。

原因是生产 Trace 中的数据：

-可能含敏感信息；
-不一定适合作为测试输入；
-生产 output 不代表正确答案；
-一次失败不代表应该成为长期回归 Case。

最终采用：

> **caller-supplied sanitized input。**

Feedback Service 不读取 Trace payload 来构造 TestCase。

------

## 方案二：新增 Dataset/TestCase 数据库表

也没有做。

当前 `DatasetVersion` / `TestCaseVersion` 已能作为 immutable domain facts（不可变领域事实），`EvaluationRun` 又会保存 snapshot。

所以为了最小闭环，新建 durable catalog 不必要。

最终：

```text
Dataset persistence:
IN_MEMORY_DOMAIN_ONLY

Durable Dataset Catalog:
NO
```

------

## 方案三：feedback 后自动执行 Evaluation

也被拒绝。

最终拆成：

```text
Action A:
create_feedback_case

Action B:
create_run

Action C:
execute_attempt
```

这保证：

> “收录一个生产失败案例”和“执行一次评估”是不同业务动作。

------

# 6. 最终架构

现在整个项目链路已经变成：

```text
LocalAgent / Runtime
        ↓
Production Trace
        ↓
Generic Online Core
        ↓
failure detection
        ↓
TraceEvidenceCandidate
        ↓
TraceFeedbackService
        ↓
caller-supplied sanitized input
        ↓
TestCaseVersion
        ↓
new DatasetVersion
        ↓
explicit create_run
        ↓
explicit execute_attempt
        ↓
EvaluationResult
        ↓
Regression Comparison
        ↓
Release Gate
```

本 WP 最关键的桥是：

```text
Online Core
   ↓ EvidenceRef
Evaluation Catalog
```

而不是：

```text
Online Core
   ↓ raw production payload
Evaluation Catalog
```

------

# 7. 核心状态机与时序

这个 WP 本身没有新持久状态机，关键是**两阶段动作边界**。

## 阶段一：Feedback

```text
project_id + trace_id
        ↓
project-scoped Trace lookup
        ↓
failing validation
        ↓
caller sanitized input
        ↓
TestCaseVersion
        ↓
DatasetVersion
        ↓
RETURN
```

此时：

```text
EvaluationRun = 0
ExecutionAttempt = 0
EvaluationResult = 0
```

Final Gate 已真实验证 feedback 后数据库没有 Run/Attempt/Result。

## 阶段二：显式 Evaluation

```text
caller
  ↓
create_run
  ↓
execute_attempt
  ↓
EvaluationResult
```

因此：

```text
Feedback != Evaluation Execution
```

------

# 8. 数据 / 权限 / Owner

这是本 WP 最核心的学习点。

| 事实                    | Owner                                |
| ----------------------- | ------------------------------------ |
| Trace 是否 failing      | Generic Online Core                  |
| Trace tenant ownership  | TraceService / project-scoped lookup |
| 哪个 Trace 被选中反馈   | caller + TraceFeedbackService        |
| 输入是否完成脱敏        | caller                               |
| sanitized input         | caller                               |
| expected output         | caller                               |
| dataset version         | caller                               |
| case version            | caller                               |
| criticality             | caller / Phase3 report               |
| Evidence identity       | `EvidenceRef`                        |
| TestCase / Dataset 构造 | TraceFeedbackService                 |
| Evaluation execution    | Evaluation Persistence / Loop        |
| Regression              | Phase3                               |
| Release decision        | Phase3 Release Gate                  |

所以 `TraceFeedbackService` 的 Owner 非常窄：

> **它负责把一个合法 production evidence 变成 Evaluation 可以消费的 catalog fact，而不是决定什么是正确答案。**

------

# 9. 兼容策略

本 WP 几乎没有碰旧系统。

Final Gate 确认没有修改：

- `models.py`
- migrations
- TraceRepository
- TraceService
- LocalAgent
- Celery / Redis
- legacy eval
- Phase2/3既有逻辑
- frontend
- dependency
- HTTP routes。

所以它属于典型：

```text
additive application-layer bridge
```

而不是重构核心系统。

这也是为什么整个 WP 不需要新 schema。

------

# 10. Bad Cases

## Bad Case 1：Project B 回流 Project A 的 Trace

### 真实性

真实 PostgreSQL adversarial（对抗）场景，已覆盖。

```text
Project A
Trace F

Project B
feedback(F)
```

必须：

```text
FAIL_CLOSED
```

最终不仅验证抛异常，还验证：

```text
EvaluationRun
ExecutionAttempt
EvaluationResult
```

before / after 都是 `(0, 0, 0)`。

### 知识点

**Authorization（授权）不能只验证“对象存在”，必须验证“对象属于当前作用域”。**

------

## Bad Case 2：Trace 自己 SUCCESS，但 child Span TIMEOUT

如果 feedback 只看：

```text
trace.normalized_outcome
```

那么：

```text
Trace SUCCESS
└─ Span TIMEOUT
```

会被错误拒绝。

当前 `is_failing_trace()` 直接复用：

```text
FAILURE_OUTCOMES
```

并同时检查 Trace + child Span。

Final Gate direct PG probe：

```text
SUCCESS + child TIMEOUT → ACCEPTED
SUCCESS no failure     → REJECTED
UNKNOWN no failure     → REJECTED
```



------

## Bad Case 3：自动复制生产 Trace payload

错误方案：

```text
test_case.input_payload = trace.input
```

风险包括：

-敏感信息泄漏；
-数据不适合长期测试；
-线上输入格式可能包含内部上下文；
-脱敏责任不清晰。

最终：

```text
TestCaseVersion.input_payload
=
TraceFeedbackCommand.input_payload
```

而不是 Trace payload。

------

## Bad Case 4：把生产 output 当 expected output

生产执行结果可能正是错误结果。

如果：

```text
expected_output = production_output
```

那么回归测试就会把 bug 固化为 ground truth。

所以：

```text
expected_output:
CALLER_SUPPLIED_OPTIONAL
```

如果 caller 不知道：

```text
None
```

也比伪造答案正确。

------

## Bad Case 5：failing Trace 自动标 Critical

Failure 和 Critical 是两个维度。

```text
failing
```

表示这次运行异常。

```text
critical
```

表示它是否属于 Release Gate 的关键场景。

当前严格：

```text
Criticality:
CALLER_SUPPLIED
```

没有因为 Trace failing 自动推断 Critical。

------

## Bad Case 6：Service 自动生成版本号

当前没有 durable Dataset Catalog。

如果做：

```text
MAX(version) + 1
```

没有 authoritative owner（权威所有者），并发下也不可靠。

因此：

```text
dataset_version:
CALLER_SUPPLIED

case_version:
CALLER_SUPPLIED
```

------

## Bad Case 7：修改旧 DatasetVersion

错误：

```text
V1.cases.append(new_case)
```

正确：

```text
V1
↓
V2(parent=V1)
```

Final Gate 确认：

```text
DatasetVersion: NEW_VERSION
Existing Dataset mutated: NO
```



------

## Bad Case 8：Feedback 成功就自动跑 Evaluation

错误链路：

```text
feedback()
→ create_run()
→ execute_attempt()
```

这样 feedback 就产生了隐藏副作用。

最终：

```text
create_feedback_case()
→ zero DB write

caller explicitly:
create_run()
execute_attempt()
```

这是真正的 command separation（命令分离）。

------

# 11. 已真实执行 Tests / Gates

Final Gate 的真实证据如下。

| 验证                     | 结果        |
| ------------------------ | ----------- |
| Focused Feedback Unit    | 15 passed   |
| PostgreSQL Feedback E2E  | 3 passed    |
| Child Span Failure Probe | PASS        |
| Cross-project DB Probe   | PASS        |
| Phase1 Regression        | 21 passed   |
| Phase2/Phase3 Regression | 52 passed   |
| Full Unit                | 556 passed  |
| Ruff                     | PASS        |
| uv lock                  | PASS        |
| diff check               | PASS        |
| compileall               | PASS        |
| Alembic                  | single head |

最有价值的真实 E2E：

```text
failing Trace F
→ TraceFeedbackService
→ TestCaseVersion
→ DatasetVersion V2
→ feedback 后 Run count = 0
→ explicit create_run
→ explicit execute_attempt
→ EvaluationResult
→ EvidenceRef(trace F) 仍存在
```

------

# 12. Known Limitations

当前正式保留：

```text
Feedback sanitization:
CALLER_SUPPLIED

Automatic payload extraction:
NO

Automatic expected output:
NO

Criticality inference:
NO

Dataset persistence:
IN_MEMORY_DOMAIN_ONLY

Durable Dataset Catalog:
NO

Version allocation:
CALLER_SUPPLIED

Duplicate feedback dedupe:
NO

Feedback API:
INTERNAL_ONLY

Auto evaluation after feedback:
NO

Human Review:
NO

Alert:
NO

Monitor redesign:
NO

Judge/Human Agreement:
NO

New Schema:
NO
```



另外 Final Gate 有一个 P3：

> `test_trace_feedback.py` 的 module-level asyncio mark 让两个同步测试产生 2 个 PytestWarning。

测试仍然全部通过，没有影响 production correctness。

------

# 13. 体现的工程能力

## 1. Production-to-Evaluation Boundary Design（生产到评估边界设计）

不是简单“把线上 Trace 保存进 Dataset”，而是把：

```text
生产事实
```

和：

```text
测试事实
```

明确分开。

------

## 2. Trust Boundary（信任边界）

Production payload 不能天然信任。

所以：

```text
Trace
→ Evidence identity
```

而：

```text
Test input
→ caller-supplied sanitized data
```

------

## 3. Immutable Versioning（不可变版本化）

Dataset/TestCase 不是 mutable container（可变容器）。

采用：

```text
V1
→ V2
```

而不是原地修改。

------

## 4. Explicit Side Effects（显式副作用）

Feedback 不自动 Evaluation。

这体现：

```text
Command A
!=
Command B
```

避免 hidden side effects（隐藏副作用）。

------

## 5. Ownership Discipline（所有权纪律）

Service 没有顺手接管：

- sanitization
- version allocation
- expected output
- criticality
- Evaluation execution。

这比“一个 service 全做完”更生产化。

------

# 14. 30 秒面试版本

> 我在 AgentEvalOps 做了一个 Production Feedback Loop，把线上 failing Trace 真正接到离线 Evaluation。系统先通过 Generic Online Core 找出 failing Trace，然后 `TraceFeedbackService` 在 project scope 内校验这个 Trace，并把它转换成带 `EvidenceRef` 的 `TestCaseVersion` 和新的 `DatasetVersion`。
>
> 我没有自动复制生产 Trace 的 payload，而是要求 caller 提供已经脱敏的 input；expected output、criticality 和版本也都不自动推断。Dataset 采用 immutable NEW_VERSION。
>
> Feedback 本身不会创建 EvaluationRun，只有 caller 显式 `create_run → execute_attempt` 后才执行评估。真实 PostgreSQL E2E 验证了 EvidenceRef 能一路传到最终 EvaluationResult，同时跨项目 feedback fail closed。Final Gate Full Unit 556 passed。

------

# 15. 2 分钟面试版本

> AgentEvalOps 前面已经完成了生产 Trace 接入、Generic Online normalization、failure metrics、离线 Evaluation 和 Regression Gate，但还缺一段：线上发现的失败 Trace 怎么安全进入离线数据集。
>
> 我没有设计成自动从 Trace 生成 TestCase，因为 production payload 可能含敏感信息，而且 production output 本身也可能就是错误答案。所以我新增了 `TraceFeedbackService`，它只接受 project-scoped failing Trace，以及 caller 提供的 sanitized input、可选 expected output 和显式 dataset/case version。
>
> Service 会构造一个 `TestCaseVersion`，其中通过 `EvidenceRef(kind="trace", identifier=trace_id)` 保留生产来源，然后创建新的 immutable `DatasetVersion`，而不是修改旧 Dataset。
>
> 这里我还专门拆开了 feedback 和 evaluation。`create_feedback_case` 只产生内存中的 catalog facts，不写 EvaluationRun。只有 caller 明确调用现有 `create_run` 和 `execute_attempt` 才会真正执行评估。
>
> 在真实 PostgreSQL E2E 中，我验证了 failing Trace 可以变成 TestCase/Dataset，再进入 EvaluationLoop，并且原始 Trace Evidence 最终仍能从 EvaluationResult 读回来；跨 project 用 Trace UUID 回流则 fail closed，而且不会生成任何 Run、Attempt 或 Result。
>
> 这样整个项目就形成了 Production Trace → Online Failure Detection → Dataset/TestCase → Offline Evaluation → Regression → Release Gate 的闭环。

------

# 16. 深入版本

从架构角度，可以把这一闭环分成五层：

```text
1. Observation
Production Trace

2. Diagnosis
Generic failure semantics

3. Evidence
TraceEvidenceCandidate / EvidenceRef

4. Feedback Catalog
TestCaseVersion / DatasetVersion

5. Evaluation
Run / Attempt / Result
```

重点是每一层只负责自己的 truth（事实）。

### Observation

回答：

> 生产环境发生了什么？

### Evidence

回答：

> 哪个生产事实值得作为后续分析依据？

### TestCase

回答：

> 我们决定用什么输入、什么预期去长期验证？

这两个不是同一个事实。

因此：

```text
Production Trace
!=
TestCase
```

这就是本 WP 最大的架构价值。

------

# 17. 高频追问

## Q1：为什么不自动从 Trace 生成 TestCase？

因为 Trace 是 production fact，而 TestCase 是 curated evaluation fact（经过整理的评估事实）。

两者之间至少涉及：

-脱敏；
-输入裁剪；
-expected behavior；
-长期回归价值判断。

所以必须存在显式 feedback boundary。

------

## Q2：为什么 TestCase 要保留 EvidenceRef？

为了 provenance（来源追踪）。

后面一个 EvaluationResult 失败时，可以回答：

> “这个 TestCase 最初来自哪条生产 Trace？”

Final Gate 已真实证明 EvidenceRef 能一直传播到 `EvaluationResult`。

------

## Q3：为什么 feedback 不自动执行 Evaluation？

因为：

```text
收录测试样本
```

和：

```text
执行测试
```

是两个业务动作。

拆开后 caller 可以决定：

-何时执行；
-用哪个 Suite；
-哪个 Target；
-哪个 Evaluator。

------

## Q4：为什么 Dataset 不落库？

当前最小闭环只需要：

```text
in-memory Dataset/TestCase
→ EvaluationRun snapshot
```

已经能证明架构。

如果以后需要：

-跨进程 Dataset 管理；
-长期复用；
-搜索；
-多人编辑；

才需要 Durable Dataset Catalog。

------

## Q5：为什么 version 由 caller 给？

因为当前没有 durable catalog 作为 version authority。

如果 service 自动计算 version，就会创造一个没有可靠 owner 的事实。

------

## Q6：同一个 Trace 回流两次怎么办？

当前：

```text
CALLER_VERSIONED
```

允许 caller 用不同 case/version 创建多个反馈。

没有自动 dedupe，这是明确 P2 limitation。

------

## Q7：为什么 failing Trace 不自动是 Critical Case？

因为：

```text
failure
```

描述一次运行结果。

而：

```text
critical
```

描述这个 Case 对 Release Gate 的业务重要性。

两个维度不能混淆。

------

# 18. 最容易夸大 / 答错

### 错误说法 1

> “现在系统已经自动把生产失败转成回归测试。”

错。

当前是：

```text
explicit caller-confirmed feedback
```

不是 automatic mining（自动挖掘）。

------

### 错误说法 2

> “生产 Trace 会自动脱敏。”

错。

当前：

```text
Sanitization owner = CALLER
```

------

### 错误说法 3

> “Dataset 已经持久化管理。”

错。

当前：

```text
Dataset persistence:
IN_MEMORY_DOMAIN_ONLY
```

------

### 错误说法 4

> “Feedback 后自动执行 Evaluation。”

错。

Final Gate 特别证明 feedback 后 Run/Attempt/Result 仍为 0。

------

### 错误说法 5

> “我们自动生成 expected output。”

错。

这是明确禁止的。

------

### 错误说法 6

> “Phase4 所有 Roadmap 功能都完成了。”

错。

Phase4 COMPLETE 只表示：

> **Minimal Production Feedback Loop COMPLETE。**

Human Review、Alert、Monitor redesign、Judge/Human Agreement 都是 Deferred。

------

# 19. P0 / P1 / P2

最终：

```text
P0 = 0
P1 = 0
```



## 已关闭的高风险问题

-跨项目 Trace feedback

- non-failing Trace进入反馈
- child Span failure被忽略
  -自动复制生产 payload
  -自动推断 expected output
  -自动推断 criticality
  -无 Owner 的自动 version
  -修改旧 Dataset
- EvidenceRef丢失
- feedback隐式执行 Evaluation
- Phase2 truth 被修改。

## P2

- caller负责 sanitization
- caller负责 version
- manual/internal feedback
  -无 durable catalog
  -无 dedupe
  -无 HTTP/UI
  -无 Human Review / Alert。

## P3

- 2 个非阻断 Pytest asyncio-mark warnings。

------

# 20. 速查表

| 问题                   | 当前答案                       |
| ---------------------- | ------------------------------ |
| Feedback Service       | `TraceFeedbackService`         |
| Service 类型           | Application Service            |
| Candidate 来源         | `TraceEvidenceCandidate`       |
| Feedback 대상          | failing-only                   |
| child TIMEOUT 可反馈   | YES                            |
| SUCCESS 无失败 child   | REJECT                         |
| UNKNOWN 无失败 child   | REJECT                         |
| Cross-project          | FAIL_CLOSED                    |
| Sanitization           | caller负责                     |
| 自动复制 Trace payload | NO                             |
| expected output        | caller optional                |
| 自动 expected output   | NO                             |
| criticality            | caller supplied                |
| 自动 critical          | NO                             |
| dataset version        | caller supplied                |
| case version           | caller supplied                |
| 自动 version           | NO                             |
| Dataset mutation       | NEW_VERSION                    |
| old Dataset 修改       | NO                             |
| base refs              | 保留                           |
| Evidence kind          | `trace`                        |
| Evidence identifier    | Trace UUID string              |
| Evidence schema        | None                           |
| Evidence进入 Result    | YES                            |
| Feedback 本身 DB 写入  | NO                             |
| 自动 Evaluation        | NO                             |
| create_run             | caller显式                     |
| execute_attempt        | caller显式                     |
| Dataset catalog        | in-memory only                 |
| Feedback dedupe        | NO                             |
| Feedback HTTP API      | NO                             |
| 新 Schema              | NO                             |
| PostgreSQL E2E         | 3 passed                       |
| Phase2/3 Regression    | 52 passed                      |
| Full Unit              | 556 passed                     |
| P0/P1                  | 0 / 0                          |
| Phase4                 | Minimal Feedback Loop COMPLETE |

这个 WP 最值得真正理解并记住的是四句话：

> **第一，生产 Trace 是 Evidence，不天然等于 TestCase。**

> **第二，从 production fact 到 evaluation fact 之间必须存在一个显式的信任与数据治理边界。**

> **第三，Feedback、Evaluation Execution、Regression、Release Gate 是四个不同 Owner，不能因为流程连续就做成一个隐式大事务。**

> **第四，生产级设计很多时候不是“自动化越多越好”，而是明确哪些事实可以自动推导、哪些事实必须由有权限的 caller 明确确认。**