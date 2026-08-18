# 1. 一句话项目 / 工作包定义

Stage4-Phase5-WP1 完成的是 **Demo Seed & Onboarding（演示种子与上手体验）**：

> 把 AgentEvalOps 原本“只有测试代码才能完整调用”的评估闭环，封装成一个可重复执行的 synthetic closed-loop demo（合成闭环演示），让开发者能够通过单一命令真实跑通 Trace Feedback → Evaluation → Regression Comparison → Regression Report → ReleaseDecision，而不新增业务 Owner、不修改冻结的 Evaluation Core。

最终独立 Gate：

```text
CODE_REVIEW_GATE: PASS
WP1_CLOSED_LOOP_POSTGRESQL: PASS
STAGE4_PHASE5_WP1_PROJECT_GATE: PASS
```

并且：

```text
READY_FOR_PHASE5_WP2: YES
```



------

# 2. 为什么做

在这个 WP 之前，AgentEvalOps 的核心能力其实已经比较完整：

```text
Trace
→ Feedback
→ TestCase / Dataset
→ Evaluation
→ Comparison
→ Regression Report
→ Release Gate
```

问题在于：

> 这些能力主要只能通过测试代码拼起来运行。

也就是说，系统“工程上存在闭环”，但一个陌生开发者或面试官并没有一个简单入口去亲自看到：

```text
Baseline
vs
Candidate
→ Regression
→ ReleaseDecision
```

所以这个 WP 解决的不是新业务能力，而是：

> **把已经存在的工程能力变成可见、可执行、可演示的工程事实。**

这也是为什么 H-1 把 Demo Seed / Onboarding 评为 Phase5 当前最高价值项。

------

# 3. 真实性与完成边界

## 已真实实现

新增了：

- `backend/scripts/demo/closed_loop_demo.py`
- demo package
- demo unit tests
- PostgreSQL closed-loop integration tests
- Root README 的 closed-loop demo 说明
- Makefile `demo` / `backend-demo`
- seed README stale API 路径修复。

Demo 支持：

```text
--scenario fail
--scenario pass
--json-output
--cleanup
--dsn
```

真实执行：

```text
Trace Feedback
→ EvaluationRun baseline
→ EvaluationRun candidate
→ EvaluationComparisonService
→ RegressionReportService
→ ReleaseDecision
```



## 已真实测试

最终 H-4：

- Focused Unit：13 passed
- PostgreSQL Demo：3 passed
- Relevant Regression：44 passed
- Full Unit：569 passed
- Ruff：PASS
- uv lock：PASS
- diff check：PASS
- compileall：PASS
- Alembic single head：PASS。

## 明确没做

没有：

- production ExecutionTarget
- external LLM
- Demo API
- Demo UI
- CI Release Gate
- ReleaseDecision → non-zero exit
- report persistence
- DB-only replay
- full Docker automated onboarding smoke。

这些都不是本 WP 的完成项。

------

# 4. 修改前架构与根因

修改前的系统大致是：

```text
EvaluationPersistenceService
EvaluationLoopService
EvaluationComparisonService
RegressionReportService
```

这些模块都已经存在。

但是调用入口主要来自：

```text
tests/
```

而不是：

```text
CLI
Demo driver
用户可见 workflow
```

所以根因不是：

> “系统没有能力。”

而是：

> “能力没有 composition entrypoint（组合入口）。”

这类问题在工程里很常见：

```text
components complete
!=
workflow consumable
```

------

# 5. 方案讨论与取舍

## 方案一：新增业务 API

例如：

```text
POST /demo
POST /release-gate
```

没有做。

原因：

- Demo 不是新的业务 Domain；
- 不值得为展示能力增加长期维护 API；
- 会扩大 production surface。

最终选：

```text
scripts/demo
```

作为 orchestration layer（编排层）。

------

## 方案二：在 Demo 中直接构造 Result

例如：

```text
EvaluationResult(...)
ComparisonResult(...)
```

这样实现最快，但被明确禁止。

因为这会让 Demo：

> 看起来跑通了，实际上绕过了真正的系统。

最终必须真实走：

```text
create_run
→ execute_attempt
→ persisted EvaluationResult
```

------

## 方案三：Demo 自己判断 Regression / Gate

比如：

```text
if baseline == PASS and candidate == FAIL:
    regression = True
```

也被拒绝。

最终：

```text
Comparison owner
= EvaluationComparisonService

ReleaseDecision owner
= RegressionReportService
```

Demo 只负责调用，不负责重新解释业务规则。

------

# 6. 最终架构

现在 Demo 结构是：

```text
ClosedLoopDemo Driver
        │
        ├─ seed synthetic failing Trace
        │
        ├─ TraceFeedbackService
        │      ↓
        │   TestCaseVersion
        │   DatasetVersion
        │
        ├─ EvaluationPersistenceService
        │      ↓
        │   Baseline Run
        │   Candidate Run
        │
        ├─ EvaluationLoopService
        │      ↓
        │   EvaluationResult
        │
        ├─ EvaluationComparisonService
        │      ↓
        │   Regression classifications
        │
        └─ RegressionReportService
               ↓
          ReleaseDecision
```

关键点：

> Driver 没有成为新的业务 Owner，它只是 Composition / Orchestration（组合与编排）层。

------

# 7. 核心状态机与时序

默认 `fail` 场景：

```text
Demo Start
    ↓
Create isolated org/project
    ↓
Seed synthetic failing Trace
    ↓
TraceFeedbackService
    ↓
Build Demo Dataset/TestCases
    ↓
Create Baseline EvaluationRun
    ↓
execute_attempt × N
    ↓
Baseline COMPLETED
    ↓
Create Candidate EvaluationRun
    ↓
execute_attempt × N
    ↓
Candidate COMPLETED
    ↓
compare_runs
    ↓
build_report
    ↓
ReleaseDecision.FAIL
    ↓
Demo process exit 0
```

这里最后一点很重要：

```text
ReleaseDecision.FAIL
!=
Demo execution failure
```

所以 WP1：

```text
business decision = FAIL
process exit = 0
```

而把 FAIL 映射为非零退出，是下一个 WP2 CI Release Gate 的职责。

------

# 8. 数据 / 权限 / Owner

| 内容                      | Owner                          |
| ------------------------- | ------------------------------ |
| Trace Feedback            | `TraceFeedbackService`         |
| Evaluation Run 创建       | `EvaluationPersistenceService` |
| Evaluation 执行           | `EvaluationLoopService`        |
| Regression classification | `EvaluationComparisonService`  |
| Criticality               | caller                         |
| Regression Report         | `RegressionReportService`      |
| ReleaseDecision           | `RegressionReportService`      |
| Demo 编排                 | closed-loop driver             |

因此最核心的一句话是：

> **Demo driver 是 orchestration owner，不是 business truth owner。**

------

# 9. 兼容策略

这个 WP 最大特点之一是：

**没有修改 frozen core。**

Final Gate 确认：

```text
backend/app production diff: NO
migration: NO
schema: NO
dependency: NO
frontend: NO
.github workflow: NO
new business API: NO
```



也就是说，它属于：

```text
thin composition layer
```

而不是：

```text
architecture rewrite
```

------

# 10. Bad Cases

## Bad Case 1：Demo 自己重算 Regression

### 风险

如果 Demo 写：

```text
PASS → FAIL = REGRESSION
```

那么未来 Phase3 规则变化时：

```text
真实系统
和
Demo
```

可能产生不同结果。

### 修复

只有：

```text
EvaluationComparisonService
```

负责 classification。

### 知识点

**Single Source of Truth（单一事实来源）不仅适用于数据库，也适用于业务规则。**

------

## Bad Case 2：Demo 自己决定 ReleaseDecision

错误：

```text
if critical_regression:
    FAIL
```

这会制造第二套 Release Gate。

最终：

```text
RegressionReportService.build_report(...)
```

是唯一 decision owner。

------

## Bad Case 3：直接构造 EvaluationResult

这样 Demo 虽然快，但绕开：

- Run
- Attempt
- claim
- executor
- persistence
- finalize_result。

最终 H-4 确认：

```text
Direct EvaluationResult construction in driver: NO
EvaluationLoop used: YES
```



------

## Bad Case 4：Trace Feedback 只是摆设

有些 Demo 会 seed 一条 Trace，但后续 TestCase 根本不是由它产生。

本 WP 真正验证：

```text
failing Trace
→ TraceFeedbackService
→ TestCaseVersion
→ EvidenceRef
→ EvaluationResult
```

并从 PostgreSQL read-back 验证 Trace Evidence 仍存在。

------

## Bad Case 5：Criticality 自动推断

错误：

```text
case name contains "critical"
→ critical
```

最终：

```text
Criticality: CALLER_SUPPLIED
Criticality auto-inferred: NO
```

------

## Bad Case 6：Demo FAIL 返回非零退出码

在 WP1 中这是错误边界。

因为默认 Demo 本来就是为了展示：

```text
critical regression
→ ReleaseDecision.FAIL
```

所以：

```text
FAIL scenario decision = FAIL
FAIL scenario process exit = 0
```

真正 non-zero 属于 CI WP2。

------

## Bad Case 7：Demo 删除其他数据

`--cleanup` 如果：

```text
TRUNCATE
delete all projects
```

风险非常高。

最终 H-4 用 sentinel org/project 验证：

```text
cleanup only removes demo-owned data: YES
```



------

## Bad Case 8：文档中硬编码开发数据库密码

这是本 WP 真正出现过的 H-4 P1。

第一次 Final Gate：

```text
CODE_REVIEW_GATE: FAIL
```

原因：

- README
- Makefile
- CLI `--help`

出现明文 PostgreSQL DSN password。

随后 H-3R 修复为：

```text
explicit --dsn
→ AGENTEVALOPS_DEMO_DATABASE_URL
→ project DB config
```

并重新 Final Gate：

```text
P1-1 plaintext DSN: CLOSED
CODE_REVIEW_GATE: PASS
```



这是非常适合面试讲的真实 Bad Case。

------

# 11. 已真实执行 Tests / Gates

最终证据：

| Gate                           | Result      |
| ------------------------------ | ----------- |
| Credential focused unit        | 13 passed   |
| PostgreSQL closed-loop demo    | 3 passed    |
| Relevant Phase2/3/4 regression | 44 passed   |
| Full unit                      | 569 passed  |
| Ruff                           | PASS        |
| uv lock                        | PASS        |
| git diff check                 | PASS        |
| compileall                     | PASS        |
| Alembic                        | single head |
| Code Review Gate               | PASS        |
| WP1 PostgreSQL Gate            | PASS        |
| WP1 Project Gate               | PASS        |



------

# 12. Known Limitations

最终保留：

```text
Demo data:
SYNTHETIC

External LLM:
NO

Production ExecutionTarget:
NO

Full Docker automated onboarding:
NO / OPTIONAL

Demo API:
NO

Demo UI:
NO

Gate non-zero exit:
NO

CI integration:
NO

Report persistence:
NO

DB-only replay:
NO

Legacy/current dev DB migration drift:
CONFIRMED, P2_ENVIRONMENT
```



其中本地旧 dev DB：

```text
alembic version = 7ca7dbab5b86
current head = d3a4e5f6b7c8
```

所以缺 `normalized_attributes`。

但 fresh DB 升级到 current head 后 Demo 已真实 PASS，因此这是：

```text
P2_ENVIRONMENT
```

不是代码失败。

------

# 13. 体现的工程能力

## 1. Composition Root（组合根）设计

你证明了不仅会写 Service，还能把多个 bounded capability（有边界能力）正确拼成真实工作流。

------

## 2. Owner Boundary（所有权边界）

Demo 没有为了方便重新实现：

- comparison
- release gate
- criticality。

这是很重要的工程纪律。

------

## 3. Demo ≠ Fake

很多项目所谓 Demo 实际是：

```text
手写 Result
→ 打印漂亮结果
```

这里则真实走数据库和 Evaluation Loop。

------

## 4. Deterministic Demo（确定性演示）

不依赖：

-外部模型；
-网络；
-真实生产服务。

所以结果可重复。

------

## 5. Secure Onboarding（安全上手）

第一次 Final Gate 真正发现：

```text
credential exposure
```

然后通过 runtime configuration 修复。

这是实际工程 hardening，不只是功能开发。

------

# 14. 30 秒面试版本

> AgentEvalOps 的核心 Evaluation、Regression 和 Release Gate 之前已经实现，但主要只能通过测试调用。我后来补了一个 closed-loop demo driver，把这些冻结的 Service 按真实调用链组合起来。
>
> Demo 会先生成 synthetic failing Trace，通过 TraceFeedbackService 形成带 EvidenceRef 的 TestCase，然后真实创建 baseline 和 candidate EvaluationRun，走 EvaluationLoop 持久化结果，再调用 ComparisonService 和 RegressionReportService 得到最终 ReleaseDecision。
>
> Driver 不重新实现 classification 或 release rule，只做 orchestration。默认 FAIL 场景能稳定产生 unchanged、improvement 和 critical regression，同时进程仍 exit 0，把 CI 的非零退出留给下一层适配。
>
> 最终真实 PostgreSQL Demo、Phase2/3/4 回归和 569 个 Unit 全部通过。

------

# 15. 2 分钟面试版本

> 我在 AgentEvalOps 后期发现一个比较典型的问题：系统的 Evaluation、Regression 和 Release Gate 核心其实都已经做完了，但这些能力主要只有测试代码知道怎么拼起来，对面试展示和新开发者 onboarding 都不友好。
>
> 所以我没有继续增加新的 Domain，而是做了一个 thin orchestration layer，也就是 closed-loop demo driver。
>
> 这个 Demo 会创建隔离的 synthetic org/project，先落一条 failing Trace，通过 Phase4 的 TraceFeedbackService 转成带 Trace EvidenceRef 的 TestCase，然后用现有 EvaluationPersistenceService 和 EvaluationLoopService 真实跑 baseline 和 candidate。结果持久化以后，再调用 EvaluationComparisonService 做分类，最后由 RegressionReportService 生成 ReleaseDecision。
>
> 我特别控制了 Owner 边界。Demo 不自己写 PASS→FAIL 等于 regression 的规则，也不自己判断 critical regression 是否阻断发布；这些都继续由原有 Service 负责。Criticality 也是 caller 显式传入。
>
> 默认 fail 场景会真实产生一个 unchanged、一个 improvement 和一个 critical regression，最后 ReleaseDecision.FAIL。但这个 WP 里进程仍 exit 0，因为 Demo 展示成功和业务 Gate FAIL 是两回事，非零退出码留给后续 CI adapter。
>
> 在第一次独立审查时还发现 README、Makefile 和 CLI help 中硬编码了开发 PostgreSQL 密码，所以 Gate 被判 FAIL。我后来改成显式参数、环境变量和项目配置三级解析，并增加 credential regression tests。最终重新审查后 P0/P1 都为 0，Full Unit 569 passed。

------

# 16. 深入版本

可以从三个层次理解这个 WP。

## 第一层：功能存在

```text
Evaluation
Regression
Release Gate
```

已经存在。

## 第二层：能力可组合

```text
Trace Feedback
→ Evaluation
→ Comparison
→ Report
```

Demo 证明这些模块接口之间是真的兼容。

## 第三层：能力可消费

开发者能够：

```text
one command
→ deterministic result
```

这是工程成熟度的进一步提升。

所以这个 WP 本质上不是：

> “增加了一个 Demo。”

而是：

> **证明系统已经从组件集合进化到可消费工作流。**

------

# 17. 高频追问

## Q1：为什么不直接写个 pytest 当 Demo？

因为测试的目标是验证 correctness（正确性），而 Demo 的目标是：

-让人运行；
-让人理解；
-让人看到完整业务输出。

而且 Demo 是真正的 composition entrypoint。

------

## Q2：为什么不用真实 LLM？

为了 deterministic。

真实 LLM 会引入：

- API key
  -网络依赖
  -随机性
  -成本
  -结果漂移。

这个 Demo 的目标不是评估模型质量，而是展示 AgentEvalOps 的 workflow correctness。

------

## Q3：FixtureExecutionTarget 会不会显得不真实？

不会，只要不夸大。

正确说法：

> Demo 使用 deterministic fixture target 验证 Evaluation Platform 的完整工作流，不宣称它是 production execution target。

------

## Q4：为什么 FAIL scenario exit 0？

因为：

```text
ReleaseDecision.FAIL
```

是业务输出。

而：

```text
process exit != 0
```

代表调用失败 / CI 阻断。

WP1 只负责演示；WP2 才负责把业务 decision 映射到 process contract。

------

## Q5：为什么还需要 PASS scenario？

它可以证明：

```text
--scenario pass
```

并不是简单 hard-code 输出，而是真的通过相同 Evaluation → Comparison → Report 链路得到 PASS。

------

## Q6：为什么第一次 Final Gate 会因为开发密码失败？

因为安全 review 的原则是：

> 即使是开发默认凭据，也不应该在 README、Makefile、CLI help 这些用户可见 surface 中硬编码。

最终改成 runtime config。

------

# 18. 最容易夸大 / 答错

### 错误说法 1

> “我们已经有完整 CI Release Gate。”

错。

当前：

```text
CI integration: NO
Gate non-zero exit: NO
```

那是 WP2。

------

### 错误说法 2

> “Demo 用真实生产模型。”

错。

使用：

```text
FixtureExecutionTarget
+
deterministic evaluator
```

------

### 错误说法 3

> “Demo 数据来自真实线上用户。”

错。

```text
Demo data = SYNTHETIC
```

------

### 错误说法 4

> “Demo driver 实现了 Regression 规则。”

错。

Owner 是：

```text
EvaluationComparisonService
```

------

### 错误说法 5

> “ReleaseDecision 是脚本算出来的。”

错。

来自：

```text
RegressionReportService
```

------

### 错误说法 6

> “Fresh onboarding 完全自动化。”

错。

当前：

```text
Full Docker automated onboarding:
NO / OPTIONAL
```

------

### 错误说法 7

> “第一次 H-4 就通过了。”

错。

真实历史是：

```text
第一次 H-4:
FAIL

P1:
plaintext DSN credential

H-3R:
fix

H-4 rerun:
PASS
```

这反而比“一次就过”更适合工程面试。

------

# 19. P0 / P1 / P2

最终：

```text
P0 = 0
P1 = 0
```



## 曾真实出现并关闭的 P1

```text
README / Makefile / CLI help
plaintext PostgreSQL DSN credential
```

修复后：

```text
P1-1 plaintext DSN: CLOSED
```

## P2

-旧 dev DB schema drift
-无 full Docker automated onboarding
-默认不 cleanup
-无 Demo UI
-无 production target

- report 不持久化
- DB-only replay 无
- CI Gate 尚未实现。

## P3

本轮无新 P3。

------

# 20. 速查表

| 问题                       | 答案                                 |
| -------------------------- | ------------------------------------ |
| WP 名称                    | Demo Seed & Onboarding               |
| Demo driver                | `scripts.demo.closed_loop_demo`      |
| Demo 数据                  | SYNTHETIC                            |
| External LLM               | NO                                   |
| External Network           | NO                                   |
| Production Target          | NO                                   |
| Trace Feedback             | YES                                  |
| Trace Evidence 到 Result   | YES                                  |
| Baseline Run               | REAL                                 |
| Candidate Run              | REAL                                 |
| EvaluationLoop             | REAL                                 |
| Comparison Owner           | `EvaluationComparisonService`        |
| ReleaseDecision Owner      | `RegressionReportService`            |
| Criticality                | CALLER_SUPPLIED                      |
| 默认 FAIL classification   | unchanged + improvement + regression |
| 默认 Gate                  | FAIL                                 |
| FAIL scenario process exit | 0                                    |
| PASS scenario              | YES                                  |
| PASS scenario process exit | 0                                    |
| Demo DB isolation          | fresh UUID                           |
| Cleanup                    | only demo-owned data                 |
| Demo API                   | NO                                   |
| Demo UI                    | NO                                   |
| New Schema                 | NO                                   |
| Frozen Core 修改           | NO                                   |
| CI Workflow                | NO                                   |
| Credential P1              | 已关闭                               |
| Focused Unit               | 13 passed                            |
| PostgreSQL Demo            | 3 passed                             |
| Relevant Regression        | 44 passed                            |
| Full Unit                  | 569 passed                           |
| P0/P1                      | 0 / 0                                |
| 下一步                     | Phase5-WP2 CI Release Gate           |

这个 WP 最值得记住的五句话是：

> **第一，核心组件“都存在”并不等于系统已经“可消费”。**

> **第二，Demo 应该复用真实业务 Owner，而不是复制一套看起来能工作的业务规则。**

> **第三，好的 Demo 应该 deterministic、isolated、repeatable，而不是依赖真实 LLM 才能证明项目价值。**

> **第四，业务结果 FAIL 和程序执行失败是两回事，因此 WP1 的 ReleaseDecision.FAIL 仍然 exit 0。**

> **第五，Onboarding 本身也是生产工程的一部分；README、CLI help、Makefile 中的 credential 暴露，同样应该进入正式 Gate。**