# 推荐面试材料文件名

```
AGENTEVALOPS_STAGE4_PHASE3_WP2_REGRESSION_REPORT_RELEASE_GATE_INTERVIEW_MATERIAL.md
```

# 1. 一句话项目 / 工作包定义

Stage4-Phase3-WP2 完成了 AgentEvalOps 的 **Regression Report（回归报告）与 Release Gate Foundation（发布门禁基础）**：

> 在 WP1 已产生可信 `EvaluationRunComparison` 的基础上，由调用方显式提供 Critical Case（关键用例）集合，WP2 负责确定性聚合 Regression Report，并按照冻结策略输出 `PASS / FAIL` 两态 Release Decision（发布决策）。

最终闭环为：

```text
Baseline
vs
Candidate
    ↓
WP1 Regression Comparison
    ↓
EvaluationRunComparison
    +
Caller-supplied Critical Cases
    ↓
RegressionReport
    ↓
Critical Regression /
Critical NOT_COMPARABLE
    ↓
ReleaseDecision
PASS / FAIL
```

WP2 Final Gate 已 PASS，并正式使：

`PHASE3_REGRESSION_CORE: COMPLETE`。

------

# 2. 为什么做

WP1 已经能回答：

> Candidate 相比 Baseline，某个 Case/Evaluator slot 是退化、改善、未变化还是不可比较？

但它还不能回答：

> **这些回归里面，哪些严重到应该阻止发布？**

例如：

```text
Case A → REGRESSION
Case B → REGRESSION
```

如果 A 是核心业务 Critical Case，而 B 只是普通辅助用例，两者不一定拥有相同的 Release 影响。

因此 WP2 增加的不是第二套 comparison，而是：

> **把 WP1 的 comparison truth 转换成可解释的 Report，再应用一个最小明确的 Release Policy。**

------

# 3. 真实性与完成边界

## 已真实实现

已经实现：

- `ReleaseDecision`
  - `PASS`
  - `FAIL`
- `RegressionReport`
- `RegressionReportContractError`
- `RegressionReportService`
- caller-supplied Criticality
- Case 级 Criticality
- Critical ref validation
- Regression/Improvement/Unchanged/Not-comparable counts
- `regressions`
- `critical_regressions`
- `critical_not_comparable`
- Release Decision calculation
- deterministic ordering
- immutable Report DTO。

## 已真实测试

最终独立执行：

- WP2 focused unit：**14 passed**
- WP1 comparison unit：**25 passed**
- WP2 PostgreSQL 16 E2E：**2 passed**
- WP1 PostgreSQL regression：**3 passed**
- Full Unit：**520 passed**
- Ruff / lock / diff / Alembic：PASS
- P0/P1/P2/P3：全部 0。

## 明确未实现

当前没有：

- Criticality persistence；
- RegressionReport persistence；
- ReleaseDecision persistence；
- DB-only Release Decision replay；
- Score-only Release Gate；
- Severity hierarchy（严重度层级）；
- Policy DSL；
- Policy Registry；
- CI/CD integration；
  -历史 Release Decision；
  -自动 Baseline selection；
- API/UI；
- LocalAgent-specific release policy。

------

# 4. 修改前架构与根因

WP1 后：

```text
EvaluationRunComparison
├─ Case A / Evaluator X → REGRESSION
├─ Case B / Evaluator X → UNCHANGED
├─ Case C / Evaluator Y → NOT_COMPARABLE
└─ ...
```

缺少三个概念：

### 第一：哪些 Case 是 Critical？

当前持久化 Evaluation 数据中没有正式 Critical Case contract。

### 第二：Regression 怎么聚合成 Report？

WP1 只提供 per-slot comparison。

### 第三：什么情况阻止 Release？

例如：

```text
Critical REGRESSION
Critical NOT_COMPARABLE
Non-critical REGRESSION
```

三者是否都阻断，需要明确 Policy。

因此 H-1 发现真正架构缺口不在 Report DTO，而在：

> **Criticality 的 Owner 和 Release Decision 的 truth table 尚未定义。**

这也是为什么 WP2 真实执行了 H-2 Architecture Decision。

------

# 5. 方案讨论与取舍

## Criticality 方案一：扩展 Phase 2 Run Snapshot

让 Run 持久化：

```text
case_id
case_version
critical=true
```

优点：

-可 DB-only replay。

缺点：

-修改已经 Gate PASS 的 Phase 2 persistence contract；
-涉及 snapshot serialization / parser / regression；
-对当前最小闭环过重。

最终拒绝。

------

## 方案二：新增 Catalog Persistence

把 TestCase/Dataset/Suite 正式入库。

长期合理，但需要：

- ORM；
- Repository；
- Migration；
- Catalog lifecycle。

当前属于明显过度建设。

最终拒绝。

------

## 方案三：Caller-supplied Criticality

最终采用：

```text
tuple[CaseVersionRef, ...]
```

由 Caller 显式告诉 WP2：

> 当前哪些 CaseVersion 是 Critical。

优点：

-不改 Phase 2；
-不新增 DB；
-Owner 清晰；
-未来 Catalog 出现后，只需要成为这个 input 的 provider。

代价：

> Criticality 当前不能单靠 DB 重放。

这一点被明确保留为 Known Limitation，而不是继续扩大 Scope。

------

# 6. 最终架构

最终数据流：

```text
EvaluationRunComparison
        │
        │  WP1 authoritative comparison truth
        │
        ├───────────────────────┐
        │                       │
        ▼                       ▼
RegressionReportService   critical_case_refs
                          tuple[CaseVersionRef]
        │                       │
        └──────────┬────────────┘
                   ▼
           Criticality Validation
                   ↓
             Report Aggregation
                   ↓
        Critical Blocking Detection
                   ↓
           RegressionReport
                   ↓
         ReleaseDecision
          PASS / FAIL
```

最重要的是：

`RegressionReportService` **不读数据库**。

它只接受：

- `EvaluationRunComparison`
- `critical_case_refs`

然后进行纯确定性计算。

------

# 7. 核心状态机与时序

WP2 没有新增持久化状态机。

Release Decision 只是一个 deterministic algebra（确定性代数）：

```text
critical_regressions
        ∪
critical_not_comparable
        ↓
      empty?
      /    \
    YES     NO
     ↓       ↓
   PASS     FAIL
```

完整 Release truth table：

| Criticality  | Classification | Release     |
| ------------ | -------------- | ----------- |
| Critical     | REGRESSION     | FAIL        |
| Critical     | NOT_COMPARABLE | FAIL        |
| Critical     | IMPROVEMENT    | PASS        |
| Critical     | UNCHANGED      | PASS        |
| Non-critical | REGRESSION     | REPORT_ONLY |
| Non-critical | NOT_COMPARABLE | REPORT_ONLY |
| Non-critical | IMPROVEMENT    | PASS        |
| Non-critical | UNCHANGED      | PASS        |

这是整个 WP2 最核心需要记住的规则。

------

# 8. 数据 / 权限 / Owner

这一 WP 的 Owner 划分非常清楚。

| 数据/事实                          | Owner                 |
| ---------------------------------- | --------------------- |
| Per-slot Regression Classification | WP1 Comparison Domain |
| Critical Case Selection            | Caller Policy Input   |
| Report Aggregation                 | WP2                   |
| Release Blocking Policy            | WP2                   |
| Release Decision                   | WP2                   |
| Evaluation Result                  | Phase 2 Persistence   |
| Report Persistence                 | 无                    |
| Decision Persistence               | 无                    |

最重要的原则：

> **WP2 不重新拥有 WP1 的 comparison truth。**

它不能根据：

```text
baseline verdict
candidate verdict
score
```

重新计算 REGRESSION。

只能消费：

```text
AlignedResultComparison.classification
```

否则会形成双写语义。

------

# 9. 兼容策略

Criticality identity 使用：

```text
(case_id, case_version)
```

而不是只用 `case_id`。

例如 Caller 说：

```text
case-A@v1 is critical
```

但 Comparison 中是：

```text
case-A@v2
```

不能自动认为：

> 它还是同一个 Critical Case。

必须 contract error。

同样：

- Criticality 不绑定 Evaluator；
  -一个 Critical Case 可以有多个 Evaluator slots；
  -任意一个 blocking slot 就足以阻断 Release。

------

# 10. Bad Cases

## Bad Case 1：Critical Case 根本没出现在 Comparison，却默认放行

### 真实性

H-2 架构分析明确识别，并已实现 fail-closed 测试。

例如 Caller：

```text
critical_case_refs = [Case-A@v1]
```

但当前 Comparison 中完全不存在 A。

错误行为：

```text
找不到
→ 忽略
→ blockers = empty
→ PASS
```

这是严重问题。

因为它等价于：

> 一个本应关键的 Case 根本没评测，却被 Release Gate 当作没有问题。

最终策略：

```text
Critical ref outside comparison universe
→ RegressionReportContractError
```

不返回 Decision。

------

## Bad Case 2：重复 Critical Ref 被静默去重

例如：

```text
critical_case_refs = (
    A@v1,
    A@v1,
)
```

最简单写法：

```python
set(critical_case_refs)
```

然后继续。

当前实现没有这么做。

而是：

```text
duplicate critical ref
→ typed contract error
```

因为重复 input 代表 caller contract 本身不规范，不应该在 Release Decision 层偷偷修正。

------

## Bad Case 3：Critical NOT_COMPARABLE 仍然 Release PASS

这是 WP2 最重要的策略决策之一。

例如：

```text
Critical Case A
→ Candidate result missing
→ WP1 NOT_COMPARABLE
```

它不等于：

```
REGRESSION
```

但也不能证明：

> Candidate 没有 regression。

如果 Release PASS：

> Critical Case 没有可信证据也可以发布。

所以最终采用 fail-closed：

```text
Critical NOT_COMPARABLE
→ BLOCK
→ FAIL
```

但注意：

> **仍然保持 NOT_COMPARABLE，不把它伪装成 REGRESSION。**

这是 classification truth 和 release policy 的分离。

------

## Bad Case 4：普通 Regression 一律阻断 Release

看起来“更安全”，但不是当前冻结策略。

例如：

```text
Normal Case B
PASS → FAIL
```

WP1：

```text
REGRESSION
```

WP2：

```text
Report:
regression_count += 1

Release:
不单独阻断
```

只有 Critical Case 的 Regression 才阻断。

这体现：

> **Regression Detection ≠ Release Blocking。**

当前项目故意没有实现“任何 regression 都禁止发布”。

------

## Bad Case 5：Score 下降绕过 WP1 Classification 直接阻断

例如：

```text
Critical Case
Baseline PASS score=0.98
Candidate PASS score=0.80
```

WP1：

```text
classification = UNCHANGED
score_regressed = True
```

WP2 不能说：

```text
score_regressed=True
→ FAIL
```

最终：

```text
Release PASS
```

只要没有真正的 Critical `REGRESSION` 或 `NOT_COMPARABLE`。

因为 score-only release policy 仍然是：

`DEFERRED`。

------

## Bad Case 6：WP2 再算一遍 PASS→FAIL

错误设计：

```text
RegressionReportService
→ baseline verdict
→ candidate verdict
→ 自己判断 regression
```

这样 Phase 3 会出现：

```text
WP1 classification
WP2 classification
```

两套事实 Owner。

未来 Policy 一改，很容易出现：

```text
WP1 = UNCHANGED
WP2 = REGRESSION
```

当前实现严格禁止这一点。

WP2 只：

```text
filter/count item.classification
```

不重算。

------

# 11. 已真实执行 Tests / Gates

最终独立 Gate：

| 项目                      | 结果        |
| ------------------------- | ----------- |
| WP2 Focused Unit          | 14 passed   |
| WP1 Comparison Unit       | 25 passed   |
| WP2 PostgreSQL E2E        | 2 passed    |
| WP1 PostgreSQL Regression | 3 passed    |
| Full Unit                 | 520 passed  |
| Ruff                      | PASS        |
| uv lock                   | PASS        |
| diff check                | PASS        |
| Alembic                   | single head |
| P0                        | 0           |
| P1                        | 0           |
| P2                        | 0           |
| P3                        | 0           |

真实 PostgreSQL E2E 包含：

### Scenario 1

```text
Critical A:
Baseline PASS
Candidate FAIL
→ REGRESSION
→ critical_regression
→ FAIL
```

### Scenario 2

```text
Critical A:
UNCHANGED

Normal B:
PASS → FAIL
→ REGRESSION

→ critical_regressions empty
→ PASS
```

这两个场景基本就是 WP2 最核心业务价值的动态证明。

------

# 12. Known Limitations

必须保留：

- Criticality source：CALLER_SUPPLIED
- Criticality DB-only replay：NO
- RegressionReport persistence：NO
- ReleaseDecision persistence：NO
- Score-only release policy：DEFERRED
- Severity hierarchy：NO
- Policy DSL：NO
- CI/CD integration：NO
- Historical decision：NO
- Automatic baseline selection：NO
- UI/API：NO
- LocalAgent-specific policy：NO。

------

# 13. 体现的工程能力

## 1. Policy 与 Fact 分离

WP1：

> 事实：有没有 Regression？

WP2：

> 策略：这个 Regression 是否阻断 Release？

这是本 WP 最重要的设计能力。

------

## 2. Owner 设计

Criticality 不偷偷塞给 Repository，也不让 Report 自己猜。

明确：

```text
Caller owns Criticality
WP1 owns Classification
WP2 owns Release Policy
```

------

## 3. Fail-closed Release Design

Critical：

```text
REGRESSION
或
NOT_COMPARABLE
```

都阻断。

尤其 NOT_COMPARABLE 体现：

> 缺乏可信证据本身就不能证明关键场景安全。

------

## 4. Scope Control

没有为了“Release Gate”这个名字就建设：

- CI/CD；
- Policy Engine；
- Rule DSL；
- Decision database；
  -审批系统。

只做到面试闭环所需的最小 Foundation。

------

## 5. Deterministic Derivation

Report/Decision 可以由：

```text
EvaluationRunComparison
+
Criticality input
```

确定性重算。

因此当前无需新增 Persistence。

------

# 14. 30 秒面试版本

> 我在 AgentEvalOps 的 Regression Core 里继续做了一个最小 Release Gate。WP1 已经能告诉我 Baseline 和 Candidate 每个 Case/Evaluator 是 REGRESSION、IMPROVEMENT、UNCHANGED 还是 NOT_COMPARABLE，WP2 不重新算这些结果，而是由 caller 显式传入哪些 Case 是 Critical，然后做 Report 聚合和 Release Decision。
>
> 最小策略是 Critical REGRESSION 和 Critical NOT_COMPARABLE 都阻断，非 Critical regression 只进 Report 不阻断，最终 Decision 只有 PASS 和 FAIL。Criticality 当前不持久化，是 caller-supplied 的 `CaseVersionRef`，这样避免为了最小闭环去反向修改 Phase 2 snapshot 或新增 Catalog DB。最终真实 PostgreSQL E2E 跑通了 Critical regression → FAIL 和普通 regression → PASS。

------

# 15. 2 分钟面试版本

> Phase3-WP1 完成以后，我们已经能够稳定比较 Baseline 和 Candidate，但是 comparison 只能告诉我们哪些 slot 退化，还不能决定能不能发布。所以 WP2 做的是 Regression Report 和最小 Release Gate。
>
> 这里最大的架构问题其实是 Critical Case 从哪里来。当前 TestCase虽然有 tags和metadata，但是这些内容没有进入 Run snapshot，也没有 Catalog persistence，所以从数据库里无法可靠恢复 criticality。我没有为了这个功能重新改 Phase 2 persistence，而是让 caller显式传入 `tuple[CaseVersionRef, ...]`。WP2只负责验证这些 critical refs确实属于当前 comparison universe，然后做 deterministic aggregation。
>
> Release Policy很简单：Critical Case下只要有一个 evaluator slot是REGRESSION或者NOT_COMPARABLE，就FAIL；Critical IMPROVEMENT和UNCHANGED不阻断。普通Regression和NOT_COMPARABLE只进入Report，不单独阻断。
>
> 这里我特别保留了WP1的事实边界。比如Critical NOT_COMPARABLE会导致Release FAIL，但它仍然是NOT_COMPARABLE，不会被WP2改写成REGRESSION。Score也是一样，WP1如果判UNCHANGED但score_regressed=True，WP2当前不会自行升级成FAIL，因为score-only policy还没冻结。
>
> Report和Decision目前都不持久化，它们是EvaluationRunComparison加Criticality input的确定性派生。Final Gate用真实PostgreSQL 16验证了Critical regression阻断和Non-critical regression不阻断两个完整场景，Phase3 Regression Core也在这个WP结束后正式Complete。

------

# 16. 深入版本

整个 Phase 3 的事实链可以这样讲：

```text
Phase 2
Evaluation Facts
      ↓
Phase3-WP1
Cross-run Comparison Truth
      ↓
REGRESSION / IMPROVEMENT /
UNCHANGED / NOT_COMPARABLE
      ↓
Phase3-WP2
Policy Context:
Critical Case Set
      ↓
Regression Report
      ↓
Blocking Evidence
      ↓
PASS / FAIL
```

其中三个层次必须分开：

### Evaluation Fact

> Candidate 实际输出是什么？

Phase 2。

### Comparison Fact

> 相比 Baseline 是否退化？

WP1。

### Release Policy

> 这个退化是否足以阻止发布？

WP2。

这也是 Phase 3 最完整的架构主线。

------

# 17. 高频追问

## Q1：为什么 Criticality 不直接放在 TestCase metadata？

因为当前 metadata 没有进入持久化 Run snapshot。

如果 WP2在运行结束以后偷偷依赖：

```text
metadata["critical"]
```

就无法从现有 authoritative persistence路径恢复它。

当前选择显式 caller input，更符合真实数据边界。

------

## Q2：Caller 会不会漏传一个 Critical Case？

会，这是当前 Known Limitation。

WP2能验证：

> 传进来的 Critical Case 是否属于当前 comparison。

但不能证明：

> Caller 是否遗漏了业务上本应 Critical 的 Case。

未来 Catalog 可以成为 Criticality provider。

当前没有假装解决这个问题。

------

## Q3：为什么 Critical NOT_COMPARABLE 要 FAIL？

因为对 Critical Case：

> 没有可信比较结果

不等于：

> 已证明没有 Regression。

为了 Release Gate fail closed，关键用例缺乏可信证据时阻断。

------

## Q4：为什么普通 Regression 不 FAIL？

这是当前冻结的最小 Release Policy：

> Regression detection 和 blocking policy 分离。

普通 regression 进入 Report，只有 Critical regression 才成为 blocking evidence。

如果未来业务要求任何 regression 都阻断，那是 Policy 变化，而不是修改 WP1 classification。

------

## Q5：为什么 ReleaseDecision 只有 PASS / FAIL？

当前目标只是最小 Release Gate Foundation。

没有真实需求证明需要：

- WARN；
- MANUAL_REVIEW；
- CONDITIONAL；
- UNKNOWN。

因此不提前扩张状态机。

------

## Q6：为什么 Report 不持久化？

因为：

```text
WP1 Comparison
+
Criticality input
→ Report
→ Decision
```

当前是确定性派生。

没有 API、审批、历史审计或 CI/CD consumer 要求 decision snapshot。

所以新增数据库事实没有当前价值。

------

## Q7：Critical Case 多个 Evaluator 怎么处理？

Criticality 是 Case 级。

如果 Case A 有：

```text
Evaluator 1 → UNCHANGED
Evaluator 2 → REGRESSION
```

则 A 已有一个 blocking slot：

```
Release FAIL
```

不需要所有 Evaluator 都 regression。

------

# 18. 最容易夸大 / 答错

### 错误 1

> “我们已经做了完整生产 Release Gate。”

错。

当前只是 Release Gate Foundation，没有 CI/CD、审批、历史 Decision persistence。

------

### 错误 2

> “Critical Case 已经存在数据库。”

错。

当前是 caller-supplied，且不持久化。

------

### 错误 3

> “Release Decision 可以 DB-only replay。”

错。

Criticality input没有持久化，因此目前不能。

------

### 错误 4

> “所有 Regression 都会阻断。”

错。

只有 Critical Regression阻断。

------

### 错误 5

> “NOT_COMPARABLE 会变成 REGRESSION。”

错。

Classification保持 NOT_COMPARABLE，只是在Critical情况下Release Policy选择FAIL。

------

### 错误 6

> “Score下降会阻断Release。”

错。

当前 score-only policy仍 DEFERRED。

------

# 19. P0 / P1 / P2

最终：

```text
P0 = 0
P1 = 0
P2 = 0
P3 = 0
```

但依然存在 Scope-level Known Limitations：

- Criticality non-persistent；
- Report/Decision non-persistent；
- score-only policy deferred；
  -无 CI/CD / API/UI。

这些不是当前 defect。

------

# 20. 速查表

| 问题                        | 当前答案                                       |
| --------------------------- | ---------------------------------------------- |
| Report 输入                 | `EvaluationRunComparison + critical_case_refs` |
| Criticality 来源            | Caller supplied                                |
| Criticality 粒度            | Case                                           |
| Critical identity           | `case_id + case_version`                       |
| Criticality 入库了吗        | 没有                                           |
| Critical ref 缺失           | Contract Error                                 |
| Duplicate critical refs     | Fail closed                                    |
| Critical REGRESSION         | FAIL                                           |
| Critical NOT_COMPARABLE     | FAIL                                           |
| Critical IMPROVEMENT        | 不阻断                                         |
| Critical UNCHANGED          | 不阻断                                         |
| Non-critical REGRESSION     | Report only                                    |
| Non-critical NOT_COMPARABLE | Report only                                    |
| Score-only regression       | 不阻断                                         |
| ReleaseDecision             | PASS / FAIL                                    |
| WP2 重算 WP1 classification | 不允许                                         |
| Report persistence          | 无                                             |
| Decision persistence        | 无                                             |
| 新 DB schema                | 无                                             |
| PostgreSQL WP2 E2E          | 2 passed                                       |
| Full Unit                   | 520 passed                                     |
| Phase3 状态                 | COMPLETE                                       |

最应该记住的一句话：

> **WP1 决定“发生了什么变化”，WP2 决定“哪些变化足以阻止发布”；Comparison Fact 和 Release Policy 必须由不同 Owner 管理。**