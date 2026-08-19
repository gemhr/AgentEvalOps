# 1. 一句话项目 / 工作包定义

Stage4-Phase5-WP2 完成的是 **CI Release Gate Integration（CI 发布门禁集成）**：

> 将 Phase3 已经冻结的 `ReleaseDecision.PASS / FAIL` 通过一个 thin CLI adapter（薄命令行适配层）映射成稳定的进程退出契约，并接入 GitHub Actions，使 AgentEvalOps 的评估结果能够真实影响 CI Job 的成功或失败。

最终独立 Gate：

```text
CODE_REVIEW_GATE: PASS
WP2_CI_GATE_POSTGRESQL: PASS
STAGE4_PHASE5_WP2_PROJECT_GATE: PASS
PHASE5_ENGINEERING_HARDENING: COMPLETE
```

其中：

```text
PASS            → exit 0
Gate FAIL       → exit 2
Technical Error → exit 1
```

Remote GitHub Actions 尚未真实执行，因此当前只能宣称 **synthetic CI gate integration（合成 CI 门禁集成）已完成**，不能宣称 production release gate 已上线。

------

# 2. 为什么做

在 WP1 完成后，AgentEvalOps 已经能真实跑出：

```text
Trace
→ Feedback
→ Evaluation
→ Comparison
→ RegressionReport
→ ReleaseDecision
```

但 WP1 的进程语义是：

```text
ReleaseDecision.FAIL
→ Demo 执行成功
→ exit 0
```

这是合理的，因为 WP1 是 Demo。

但 CI 场景需要另一套 process contract（进程契约）：

```text
业务门禁通过
→ CI success

业务门禁失败
→ CI failure

程序本身异常
→ CI failure，但要和业务 Gate FAIL 区分
```

所以 WP2 解决的是：

> **如何把已经存在的应用层质量决策，可靠地转换成 CI 可消费的操作系统退出码。**

------

# 3. 真实性与完成边界

## 已真实实现

新增：

- `backend/scripts/ci/release_gate.py`
- CI package
- CI Gate unit tests
- PostgreSQL subprocess integration tests
- `.github/workflows/evaluation-release-gate.yml`
- README CI Release Gate 说明。

真实实现：

```text
ReleaseDecision.PASS → 0
ReleaseDecision.FAIL → 2
Technical Error      → 1
```

同时支持：

- PASS artifact
- FAIL artifact
- FAIL 后仍上传 artifact 的 workflow 设计
- synthetic workflow
- PostgreSQL fresh schema migration
- `workflow_dispatch`
- `workflow_call`。

## 已真实测试

Final Gate：

- Focused Unit：11 passed
- PostgreSQL CLI Integration：3 passed
- Relevant Regression：47 passed
- Full Unit：580 passed
- Ruff：PASS
- uv lock：PASS
- diff check：PASS
- compileall：PASS
- Alembic single head：PASS。

## 明确未实现

没有：

- Remote GitHub Actions 实际运行
- real production baseline selection
- automatic candidate version discovery
- production `release.yml` integration
- report DB persistence
- production deployment gate。

这些仍然是 Known Limitation / Deferred。

------

# 4. 修改前架构与根因

修改前：

```text
RegressionReportService
        ↓
ReleaseDecision.PASS / FAIL
        ↓
        STOP
```

应用层已经知道：

> “候选版本该不该通过。”

但是操作系统 / CI 并不知道。

所以缺的是：

```text
Application Decision
→ Process Contract
→ CI Job Status
```

而不是再做一个 Release Gate。

根因可以总结为：

> **业务 truth 已经存在，但缺少 delivery adapter（交付适配层）。**

------

# 5. 方案讨论与取舍

## 方案一：CLI 自己判断 regression

例如：

```text
if regression_count > 0:
    exit 2
```

被拒绝。

原因是这样会制造第二套 Gate 规则。

最终：

```text
Decision owner = RegressionReportService
```

CLI 只消费：

```text
report.release_decision
```

------

## 方案二：PASS/FAIL 都映射成 0/1

也被拒绝。

最终：

```text
0 = PASS
2 = Gate FAIL
1 = Technical Error
```

这样可以明确区分：

```text
“评估完成，但质量没过”
```

和：

```text
“系统根本没完成评估”
```

这是本 WP 最重要的设计之一。

------

## 方案三：直接改 production release.yml

没有做。

原因：

当前 baseline / candidate 还是 synthetic fixture。

如果直接挂到 production release：

> 会制造“已经真实保护生产发布”的错误叙事。

所以新增独立：

```text
evaluation-release-gate.yml
```

并明确标注 synthetic integration。

------

# 6. 最终架构

最终结构：

```text
Trace Feedback
        ↓
EvaluationLoop
        ↓
EvaluationComparisonService
        ↓
RegressionReportService
        ↓
ReleaseDecision
        ↓
scripts.ci.release_gate
        ↓
Process Exit Contract
        ↓
GitHub Actions
```

退出：

```text
PASS  → 0
FAIL  → 2
ERROR → 1
```

关键点：

> CLI 不是 Decision Engine，它只是 Protocol Adapter（协议适配器）。

------

# 7. 核心状态机与时序

## PASS

```text
CLI
 ↓
run_closed_loop_demo
 ↓
RegressionReportService
 ↓
ReleaseDecision.PASS
 ↓
write artifact
 ↓
exit 0
 ↓
CI green
```

## Gate FAIL

```text
CLI
 ↓
run_closed_loop_demo
 ↓
RegressionReportService
 ↓
ReleaseDecision.FAIL
 ↓
write FAIL artifact
 ↓
exit 2
 ↓
CI red
```

## Technical Error

```text
CLI
 ↓
invalid config / execution failure
 ↓
no valid ReleaseDecision
 ↓
stderr error
 ↓
exit 1
 ↓
CI red
```

所以：

```text
Gate FAIL
!=
Technical Error
```

------

# 8. 数据 / 权限 / Owner

| 内容                      | Owner                         |
| ------------------------- | ----------------------------- |
| Evaluation Result         | Evaluation Loop               |
| Regression Classification | `EvaluationComparisonService` |
| Criticality               | caller                        |
| Regression Report         | `RegressionReportService`     |
| ReleaseDecision           | `RegressionReportService`     |
| Exit Code Mapping         | CI CLI Adapter                |
| Workflow Job Status       | GitHub Actions                |

最关键的是：

> **CLI 只拥有“如何把 Decision 表达成 process exit”的权力，不拥有“这个 Decision 是什么”的权力。**

------

# 9. 兼容策略

Final Gate 确认：

```text
backend/app production diff: NO
models.py diff: NO
migration diff: NO
dependency diff: NO
frontend diff: NO
legacy evaluation diff: NO
release.yml diff: NO
new business API: NO
new schema: NO
```



所以这一 WP 属于典型：

```text
thin adapter integration
```

不是 core rewrite。

同时 WP1 原来的：

```text
Demo FAIL → exit 0
Demo PASS → exit 0
```

仍保持不变。

------

# 10. Bad Cases

## Bad Case 1：scenario 参数直接决定 exit

错误：

```text
if scenario == "fail":
    return 2
```

这样 CI 只是演戏。

当前 unit 已验证：

> 即使 scenario label 和真实 report decision 不一致，exit 仍然由 `report.release_decision` 决定。

------

## Bad Case 2：把所有失败都返回 exit 1

这样无法区分：

```text
质量门禁失败
```

和：

```text
系统异常
```

当前：

```text
FAIL  → 2
ERROR → 1
```

------

## Bad Case 3：未知 Decision 默认 PASS

这是典型 fail-open（失败开放）。

当前：

```text
unknown decision
→ ValueError
→ top-level exit 1
```

也就是：

```text
FAIL_CLOSED
```

------

## Bad Case 4：FAIL 时 artifact 没生成

错误时序：

```text
exit 2
→ report lost
```

这样 CI 失败后反而不知道为什么。

当前：

```text
write artifact
→ exit 2
```

Final Gate 已真实验证 FAIL artifact 存在。

------

## Bad Case 5：workflow 吞掉退出码

例如：

```text
command || true
```

或：

```text
continue-on-error: true
```

会让 Gate 失效。

Final Gate：

```text
continue-on-error: NO
|| true: NO
exit swallowed: NO
```



------

## Bad Case 6：FAIL 后 artifact upload 不执行

如果 upload step 只在前一步成功时执行：

```text
Gate FAIL
→ workflow stops
→ report没上传
```

当前：

```text
if: always()
```

所以业务 Gate FAIL 后仍上传 artifact。

------

## Bad Case 7：synthetic gate 冒充 production gate

这是很容易在简历上夸大的点。

当前 workflow 明确：

```text
SYNTHETIC
DEMO
```

且：

```text
production release.yml modified: NO
production gate claimed: NO
```

------

## Bad Case 8：CI 又泄露数据库密码

WP1 已出现过一次 credential P1，所以 WP2 延续严格扫描。

最终：

```text
credential-bearing DSN: NONE
workflow credential leak: NO
artifact credential leak: NO
```



------

# 11. 已真实执行 Tests / Gates

| Gate                            | Result      |
| ------------------------------- | ----------- |
| CI Gate Unit                    | 11 passed   |
| PostgreSQL subprocess CLI       | 3 passed    |
| Relevant Integration Regression | 47 passed   |
| Full Unit                       | 580 passed  |
| Ruff                            | PASS        |
| uv lock                         | PASS        |
| diff check                      | PASS        |
| compileall                      | PASS        |
| Alembic                         | single head |
| Code Review Gate                | PASS        |
| PostgreSQL Gate                 | PASS        |
| Project Gate                    | PASS        |



真实动态结果：

```text
PASS:
decision=PASS
exit=0
artifact=PASS

FAIL:
decision=FAIL
exit=2
artifact=FAIL
stderr traceback=NO

ERROR:
invalid scenario
exit=1
artifact=absent
```

------

# 12. Known Limitations

正式保留：

```text
Remote GitHub Actions execution:
NOT_EXECUTED

Baseline/Candidate:
SYNTHETIC

Automatic baseline selection:
NO

Candidate version discovery:
NO

Production release integration:
NO

Report persistence:
NO

UI:
NO
```



因此正确表述是：

> 已完成 CI Gate Adapter 和 GitHub Actions workflow 静态集成，并用真实 PostgreSQL subprocess 验证退出契约。

不能说：

> 已经在真实 GitHub production workflow 中上线。

------

# 13. 体现的工程能力

## 1. Application Truth 与 Process Contract 分离

业务世界：

```text
PASS / FAIL
```

进程世界：

```text
0 / 2 / 1
```

这是两个不同层次。

------

## 2. Failure Taxonomy（失败分类）

明确区分：

```text
Business Failure
Technical Failure
```

这是后端和平台工程中非常实用的设计。

------

## 3. Thin Adapter Design（薄适配器设计）

CLI 不接管业务规则，只负责协议转换。

------

## 4. CI Failure Propagation（CI 失败传播）

真实关注：

- exit code
- shell semantics
- workflow status
- artifact on failure。

而不是“写了一个 GitHub workflow 文件就算完成”。

------

## 5. Truthfulness Boundary（真实性边界）

Remote Actions 没执行，就明确写：

```text
NOT_EXECUTED
```

synthetic gate 就不宣称 production gate。

------

# 14. 30 秒面试版本

> 我在 AgentEvalOps 最后补了一层 CI Release Gate Integration。Phase3 已经有 `RegressionReportService` 和 `ReleaseDecision`，所以我没有再实现第二套门禁逻辑，只做一个 thin CLI adapter。
>
> 它把 `ReleaseDecision.PASS` 映射成 exit 0，`FAIL` 映射成 exit 2，而技术异常单独是 exit 1，这样 CI 能区分“评估成功但质量没过”和“评估程序执行失败”。
>
> FAIL 场景会先写 JSON report artifact 再 exit 2，GitHub Actions workflow 不使用 `continue-on-error` 或 `|| true`，并通过 `if: always()` 保证失败后仍上传报告。
>
> 最终真实 PostgreSQL subprocess 测试验证 PASS=0、FAIL=2、ERROR=1，Full Unit 580 passed。

------

# 15. 2 分钟面试版本

> AgentEvalOps 在 Phase3 已经实现了 RegressionReport 和 ReleaseDecision，但是当时只是应用层决策，CI 还不能消费。
>
> 我在 Phase5 做了一个非常薄的 CI adapter。CLI 直接复用前一个 WP 的 closed-loop driver，真实走 Trace Feedback、Evaluation、Comparison 和 RegressionReport，然后只读取 `report.release_decision`。
>
> 我专门把退出码定义成三类：PASS 是 0，业务 Gate FAIL 是 2，技术错误是 1。这样 CI 可以明确知道候选版本因为质量问题被阻断，还是平台本身没完成判断。
>
> 在 workflow 里我没有吞掉退出码，也没有 `continue-on-error` 或 `|| true`。FAIL 时 CLI 会先写 report artifact 再 exit 2，而 artifact upload 用 `if: always()`，所以 CI 失败后仍能看到 blocker。
>
> 同时我没有直接修改 production `release.yml`，因为当前 baseline/candidate 还是 deterministic synthetic fixture。这个 workflow 只证明 ReleaseDecision 可以真实控制 GitHub Actions Job，不宣称 production deployment gate 已上线。
>
> 最终真实 PostgreSQL subprocess 下 PASS 返回 0，FAIL 返回 2，技术错误返回 1；相关 integration 47 passed，Full Unit 580 passed。

------

# 16. 深入版本

可以把整个 WP 理解成三层：

```text
Domain Layer
ReleaseDecision.PASS / FAIL

        ↓

Adapter Layer
exit 0 / 2 / 1

        ↓

Automation Layer
GitHub Actions Job PASS / FAIL
```

每一层只负责自己的语义。

Domain 负责：

> 候选版本是否通过质量门禁？

CLI 负责：

> 如何把业务结论映射给操作系统？

Workflow 负责：

> 如何让进程状态影响 CI Job？

如果把三层混在一起，例如让 workflow 自己统计 regression 数量，就会破坏 Owner 边界。

------

# 17. 高频追问

## Q1：为什么 FAIL 用 2，不直接用 1？

为了区分：

```text
exit 2 = 正常评估完成，但 Gate FAIL
exit 1 = 工具/配置/执行错误
```

这种区分对 CI 日志、告警、排障都更清晰。

------

## Q2：为什么 unknown decision 不默认 PASS？

因为 Release Gate 应 fail closed。

未知状态不应该放行发布。

------

## Q3：为什么 FAIL 时还要生成 artifact？

因为：

> “CI 红了”不是足够的信息。

需要知道：

- 哪个 critical case
- 什么 classification
- 什么 blocker。

------

## Q4：为什么不用 `continue-on-error`？

因为那会把 Gate 变成 report-only。

CI Gate 的核心就是让 exit code 真正影响 Job status。

------

## Q5：为什么还没接 production release.yml？

因为现在 gate 使用 synthetic baseline/candidate。

如果直接接 production release，会过度宣称实际能力。

------

## Q6：GitHub Actions 真跑过吗？

没有。

真实边界是：

```text
Remote GitHub Actions execution:
NOT_EXECUTED
```

已完成 workflow static review + local PostgreSQL subprocess verification。

------

# 18. 最容易夸大 / 答错

### 错误说法 1

> “我们的 CI Release Gate 已经保护生产发布。”

错。

当前没有接 `release.yml`。

------

### 错误说法 2

> “GitHub Actions 已经在线上真实跑过。”

错。

Remote run：

```text
NOT_EXECUTED
```

------

### 错误说法 3

> “CLI 判断有没有 regression。”

错。

Classification Owner：

```text
EvaluationComparisonService
```

------

### 错误说法 4

> “CLI 决定 release 是否 PASS。”

错。

Owner：

```text
RegressionReportService
```

------

### 错误说法 5

> “所有非零退出都代表 Gate FAIL。”

错。

```text
2 = Gate FAIL
1 = Technical Error
```

------

### 错误说法 6

> “FAIL 时 workflow 没有报告。”

错。

设计明确：

```text
artifact write
→ exit 2
→ upload if: always()
```

------

# 19. P0 / P1 / P2

最终：

```text
P0 = 0
P1 = 0
P3 = 0
```



## 已关闭的高风险项

- CLI 重新实现 ReleaseDecision
- scenario 直接控制 exit
- FAIL / ERROR 混淆
- unknown fail-open
- workflow 吞 exit
- FAIL artifact 丢失
- credential 泄漏
- synthetic gate 冒充 production gate
- frozen core 改动。

## P2

- Remote GitHub Actions 未执行
- synthetic baseline/candidate
  -无 automatic baseline selection
  -无 candidate version discovery
  -无 production release integration
- report file-only。

这些不阻断当前求职项目闭环。

------

# 20. 速查表

| 问题                    | 当前答案                      |
| ----------------------- | ----------------------------- |
| WP                      | CI Release Gate Integration   |
| CLI                     | `scripts.ci.release_gate`     |
| Decision Owner          | `RegressionReportService`     |
| Classification Owner    | `EvaluationComparisonService` |
| CLI                     | ADAPTER_ONLY                  |
| PASS exit               | 0                             |
| Gate FAIL exit          | 2                             |
| Technical Error exit    | 1                             |
| Unknown decision        | FAIL_CLOSED                   |
| scenario直接决定exit    | NO                            |
| PASS artifact           | YES                           |
| FAIL artifact           | YES                           |
| FAIL stderr traceback   | NO                            |
| WP1 Demo FAIL exit      | 0                             |
| WP1 Demo PASS exit      | 0                             |
| Workflow                | `evaluation-release-gate.yml` |
| Trigger                 | `workflow_dispatch`           |
| workflow_call           | YES                           |
| PostgreSQL              | YES                           |
| Alembic upgrade         | YES                           |
| continue-on-error       | NO                            |
| `                       |                               |
| exit swallowed          | NO                            |
| artifact `if: always()` | YES                           |
| Credential leak         | NO                            |
| Production release.yml  | 未修改                        |
| Production gate claimed | NO                            |
| Remote GitHub Actions   | NOT_EXECUTED                  |
| PostgreSQL CLI tests    | 3 passed                      |
| Relevant Regression     | 47 passed                     |
| Full Unit               | 580 passed                    |
| P0/P1                   | 0 / 0                         |
| Phase5                  | Minimal Recommended COMPLETE  |

这个 WP 最值得记住的五句话：

> **第一，ReleaseDecision 是业务事实，exit code 是进程协议，两者必须通过明确 adapter 连接。**

> **第二，业务 Gate FAIL 和技术执行失败必须区分，否则 CI 无法正确诊断。**

> **第三，一个真正的 CI Gate 必须让非零退出码真实传播到 Job，而不是 `continue-on-error` 后只生成一份报告。**

> **第四，失败时仍然要产出 artifact，因为“为什么被阻断”与“是否被阻断”同样重要。**

> **第五，synthetic CI integration 可以证明机制正确，但不能被包装成 production release protection。**