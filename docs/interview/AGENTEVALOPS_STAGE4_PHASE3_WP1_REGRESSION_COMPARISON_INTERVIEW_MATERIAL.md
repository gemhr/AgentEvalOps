# 1. 一句话项目 / 工作包定义

Stage4-Phase3-WP1 实现了 AgentEvalOps 的 **Regression Comparison Core（回归比较核心）**：

> 输入一个 Baseline EvaluationRun（基线评测运行）和一个 Candidate EvaluationRun（候选评测运行），在保证 tenant、Run、Dataset、Suite、Target 和 Evaluator provenance（来源信息）兼容的前提下，对两侧 `EvaluationResult` 做跨 Run 对齐，并输出每个 Case/Evaluator 粒度的 `REGRESSION / IMPROVEMENT / UNCHANGED / NOT_COMPARABLE`。

该 WP 不持久化 Comparison，不做 Regression Report（回归报告）、Critical Case（关键用例）或 Release Gate（发布门禁），只建设 Phase 3 后续能力所依赖的**可信比较事实层**。最终 Project Gate 正式 PASS。

------

# 2. 为什么做

Phase 2 完成以后，系统已经能够可靠地产生：

- `EvaluationRun`
- `ExecutionAttempt`
- `EvaluationResult`
- Dataset / Suite / Case version
- Evaluator identity/version
- Target identity/version

但它只能回答：

> “这一次评测结果是什么？”

还不能回答：

> “这个新版本相对于旧版本，是变好了还是变差了？”

所以 Phase 3 的第一步不能直接做 Release Gate。

必须先建立最底层：

```text
Baseline Result
      +
Candidate Result
      ↓
正确对齐
      ↓
正确判断变化
```

否则后面的：

- Regression Report；
- Critical Case；
- Release Decision；

全部建立在不可信的比较结果上。

因此 WP1 的核心不是“做个 diff”，而是：

> **建立两个 EvaluationRun 之间可信、确定、可解释的 comparison truth。**

------

# 3. 真实性与完成边界

## 已真实实现

已实现：

- `RegressionClassification`
  - `REGRESSION`
  - `IMPROVEMENT`
  - `UNCHANGED`
  - `NOT_COMPARABLE`
- `ComparisonReason`
- `AlignedResultComparison`
- `RunComparisonProvenance`
- `EvaluationRunComparison`
- `EvaluationComparisonService`
- Baseline/Candidate Run eligibility 校验
- tenant fail-closed
- Run-level comparability 校验
- cross-run Result alignment
- missing Result 处理
- Evaluator config/prompt compatibility
- score evidence
- deterministic ordering
- duplicate alignment fail-closed

Comparison 是纯 Application computation（应用层计算），没有新增数据库表、Repository 或 Migration。

## 已真实测试

最终独立 Gate 实际验证：

- focused unit：**25 passed**
- Phase 2 relevant regression：**107 passed**
- PostgreSQL 16 integration：**3 passed**
- Full Unit：**506 passed**
- Ruff：PASS
- `uv lock --check`：PASS
- `git diff --check`：PASS
- Alembic：single head
- P0/P1/P2/P3：全部 0。

## 明确未实现

当前没有：

- Regression Report；
- Critical Case aggregation；
- Release Gate；
- Release Decision；
- Comparison persistence；
  -历史 comparison；
  -自动 baseline selection；
- FAILED / OUTCOME_UNKNOWN Run comparison；
- score-only regression 升级策略；
- LocalAgent-specific comparison；
- API/UI。

这些不能在面试时说成已完成。

------

# 4. 修改前架构与根因

Phase 2 结束时已有：

```text
EvaluationRun
      │
      ├── Attempt
      │
      └── EvaluationResult
```

但 Baseline 和 Candidate 是两个完全独立的 Run：

```text
Baseline Run
├─ Result A
├─ Result B
└─ Result C

Candidate Run
├─ Result X
├─ Result Y
└─ Result Z
```

系统缺少三个关键问题的答案：

### 第一，两个 Run 能不能比？

例如：

```text
Dataset A
vs
Dataset B
```

显然不能直接算 regression。

### 第二，哪个 Result 应该跟哪个 Result 对齐？

不能靠：

- `run_id`
- `attempt_id`
- `execution_request_id`

因为两侧天然不同。

### 第三，对齐以后什么叫 regression？

至少要区分：

```text
PASS → FAIL
FAIL → PASS
PASS → PASS
FAIL → FAIL
```

并且处理：

- INCONCLUSIVE
- ERROR
- missing result
- evaluator config mismatch

所以根因不是缺少一个 `compare()` 函数，而是：

> **跨 Run comparison identity 和 comparison semantics 尚未建立。**

------

# 5. 方案讨论与取舍

## 方案一：为 Baseline/Candidate 新建数据库实体

例如：

```text
RegressionRun
ComparisonRun
BaselineBinding
CandidateBinding
```

优点：

-历史可审计；
-以后可以查 comparison history。

缺点：

- WP1 还只是确定性派生计算；
- Phase 2 的 append-only Result 已经是 authoritative fact；
  -过早新增表会增加 Repository、Migration、事务和状态机。

最终没有采用。

### 最终选择

WP1 comparison 做：

> **Pure Application Computation（纯应用层计算）**

输入：

```text
project_id
baseline_run_id
candidate_run_id
```

读取既有 authoritative facts，现场计算结果。

Comparison persistence 留给后续真实需要时再决定。

------

## 方案二：Cross-run key 使用 persistence logical slot

Phase 2 Result 的 Run 内 logical slot 包含：

```text
run_id
attempt_id
case_id
case_version
evaluator_id
evaluator_version
```

如果机械复用：

Baseline 与 Candidate 永远不可能对齐，因为：

```text
run_id 不同
attempt_id 不同
```

最终明确分离：

### Intra-run identity

用于：

> 一个 Run 内 Result 去重。

### Cross-run identity

用于：

> 两个 Run 之间语义对齐。

最终跨 Run key：

```text
(case_id, case_version, evaluator_id, evaluator_version)
```

这是 WP1 最关键的设计选择之一。

------

# 6. 最终架构

最终结构：

```text
Caller
  │
  │ project_id
  │ baseline_run_id
  │ candidate_run_id
  ▼
EvaluationComparisonService
  │
  ├─ get baseline Run
  ├─ get candidate Run
  │
  ├─ validate eligibility
  ├─ validate comparability
  │
  ├─ list baseline Results
  ├─ list candidate Results
  │
  ├─ build alignment indexes
  │
  ├─ union alignment keys
  │
  ├─ compare slot by slot
  │
  ▼
EvaluationRunComparison
  │
  └─ AlignedResultComparison[]
```

其中 Comparison Service 只依赖：

```
EvaluationPersistenceService
```

不直接访问：

- Repository；
- ORM；
- `AsyncSession`；
- raw SQL。

因此 Phase 2 的 persistence owner 没有被破坏。

------

# 7. 核心状态机与时序

WP1 本身没有新增持久化状态机。

这是刻意的。

它只接受：

```text
Baseline Run = COMPLETED
Candidate Run = COMPLETED
```

流程：

```text
compare_runs()
    ↓
读取 Baseline Run
    ↓
读取 Candidate Run
    ↓
检查两侧都是 COMPLETED
    ↓
检查 tenant / dataset / suite / target
    ↓
读取两侧 Results
    ↓
构造 cross-run index
    ↓
union(keys)
    ↓
逐 slot comparison
    ↓
排序
    ↓
返回 EvaluationRunComparison
```

以下 Run：

```text
PENDING
RUNNING
FAILED
OUTCOME_UNKNOWN
```

全部 fail closed。

这里必须记住：

> `Run COMPLETED` 仍然不等于所有 Evaluator PASS。

COMPLETED 是 lifecycle truth；PASS/FAIL 是 evaluation truth。

------

# 8. 数据 / 权限 / Owner

## EvaluationRun

Owner：

Phase 2 Persistence/Application。

WP1 只读。

## EvaluationResult

Owner：

Phase 2 append-only persistence。

WP1 只读。

## Regression Classification

Owner：

WP1 Comparison Domain。

它是派生事实：

```text
Baseline Result
+
Candidate Result
→ classification
```

当前不持久化。

## Tenant

所有读取都必须：

```text
get_run(project_id, ...)
list_results(project_id, ...)
```

Comparison Service 不允许绕过 project scope。

真实 PostgreSQL integration 已覆盖 cross-project comparison fail closed。

------

# 9. 兼容策略

Run-level 最小 comparability 规则：

## 必须相同

- `project_id`
- `dataset_id`
- `suite_id`
- `execution_target_id`

## 允许不同

- `dataset_version`
- `suite_version`
- `target_version_ref`

为什么 target version 必须允许不同？

因为 Regression 的典型输入就是：

```text
Target v1
vs
Target v2
```

如果要求 Target Version 完全一致，那么 Comparison Core 会直接失去最重要的用途。

但允许不同不等于丢弃 provenance。

两侧 version 都保存在：

```
RunComparisonProvenance
```

中。

------

# 10. Bad Cases

## Bad Case 1：把 Attempt ID 放进 Cross-run Alignment Key

### 真实性

架构审计识别的高风险 Bad Case，已经通过测试证明当前实现不会这么做。

### 错误做法

```text
(case_id,
 evaluator_id,
 attempt_id)
```

Baseline：

```text
attempt-A
```

Candidate：

```text
attempt-B
```

于是同一个 Case 永远无法匹配。

### 根因

混淆：

> persistence identity

和：

> semantic comparison identity。

### 正确做法

跨 Run：

```text
(case_id,
 case_version,
 evaluator_id,
 evaluator_version)
```

真实 PostgreSQL scenario 也验证了两侧 `attempt_id` 不同仍然正确对齐。

------

## Bad Case 2：Target Version 不同就拒绝比较

### 真实性

属于审查重点，当前实现已避免。

如果：

```text
Baseline = model-v1
Candidate = model-v2
```

却要求：

```text
target_version_ref MUST MATCH
```

那么 Regression 系统无法比较版本升级。

正确规则：

```text
execution_target_id MUST MATCH
target_version MAY DIFFER
```

并记录两侧 provenance。

------

## Bad Case 3：单侧 Missing Result 被判 Regression

### 真实性

已真实测试。

场景：

```text
Baseline:
Evaluator A Result = PASS

Candidate:
Evaluator A Result = missing
```

Candidate 可能只是：

> optional evaluator 没执行。

不能推出：

> Candidate regression。

所以：

```text
candidate_missing
→ NOT_COMPARABLE
```

反向同理。

Comparison 使用：

```text
union(baseline keys, candidate keys)
```

避免静默丢掉 missing slot。

------

## Bad Case 4：Evaluator Config 改了还直接比较

### 真实性

已真实测试。

即使：

```text
evaluator_id
evaluator_version
```

相同，

但：

```text
config_ref
```

或：

```text
prompt_ref
```

改变，

实际评测语义可能已经不同。

所以不能：

```text
PASS → FAIL
= REGRESSION
```

必须：

```text
NOT_COMPARABLE
```

这是 provenance-first（来源优先）原则。

------

## Bad Case 5：Score 变差就自动改成 Regression

### 真实性

明确 Known Limitation / Deferred 对应的假设场景，并有测试保证当前不会误翻转。

例如：

```text
Baseline:
PASS
score=0.95

Candidate:
PASS
score=0.80
```

当前 WP1：

```text
classification = UNCHANGED
score_regressed = True
```

而不是：

```text
classification = REGRESSION
```

原因：

> score-only regression 的 policy 还没有正式冻结。

WP1 只记录 evidence，不提前创造 Policy semantics。

------

## Bad Case 6：同一 Run 一个 Alignment Key 出现多个 Result

这是 WP1 最值得讲的一个防御性设计。

Comparison key 不包含 Attempt。

理论风险：

```text
Attempt #1 → Result
Attempt #2 → Result

同 case/evaluator
```

然后：

```text
dict[key] = result
```

谁最后写入就选谁。

当前设计没有 first/last wins。

首先 Codex 独立证明当前合法 COMPLETED Run 的状态机下，一个 cross-run key 至多存在一个 authoritative Result。

同时 Comparison Service 仍然做防御：

```text
duplicate cross-run key
→ ResultAlignmentAmbiguous
→ fail closed
```

所以即使未来 Contract 漂移，也不会静默选错数据。

------

# 11. 已真实执行 Tests / Gates

最终 H-4 独立验证：

| Gate                        | 结果        |
| --------------------------- | ----------- |
| Focused Unit                | 25 passed   |
| Relevant Phase 2 Regression | 107 passed  |
| PostgreSQL Integration      | 3 passed    |
| Full Unit                   | 506 passed  |
| Ruff                        | PASS        |
| uv lock                     | PASS        |
| git diff check              | PASS        |
| Alembic                     | single head |
| P0                          | 0           |
| P1                          | 0           |
| P2                          | 0           |
| P3                          | 0           |

PostgreSQL integration 使用：

- PostgreSQL 16；
- Redis 7；
  -隔离 Compose project。

其中真实覆盖：

1. PASS→FAIL / FAIL→PASS；
   2.不同 Attempt ID 正确对齐；
   3.不同 Target Version 可比较；
   4.cross-project fail closed；
   5.Suite incompatibility fail closed。

------

# 12. Known Limitations

当前明确：

- Score-only regression：DEFERRED
- Regression Report：NOT IMPLEMENTED
- Critical Case：NOT IMPLEMENTED
- Release Gate：NOT IMPLEMENTED
- Comparison persistence：NO
- Historical comparison：NO
- Automatic baseline selection：NO
- FAILED / OUTCOME_UNKNOWN Run comparison：NO
- LocalAgent-specific logic：NO
- UI/API：NO。

这些不影响 WP1 comparison truth。

------

# 13. 体现的工程能力

这个 WP 最适合体现以下能力。

## 1. Identity Design（身份设计）

能区分：

- persistence identity；
- execution identity；
- cross-run semantic identity。

这是整个 WP 的核心。

## 2. Provenance Design

不是只比较：

```text
PASS / FAIL
```

还检查：

- evaluator version；
- evaluator config；
- evaluator prompt；
- target identity；
- case version。

## 3. Fail-closed

不确定时：

```text
NOT_COMPARABLE
```

或直接 typed error。

而不是为了生成 Report 强行比较。

## 4. Scope Control

没有因为进入 Regression 阶段就立即新增：

- DB schema；
- Report；
- Release Gate；
  -复杂 policy。

只实现真正必须的 Comparison Core。

## 5. Reuse Existing Truth

WP1 没有重新拥有 Phase 2 的状态。

它只消费已有 append-only facts。

------

# 14. 30 秒面试版本

> 我在 AgentEvalOps 里做了 Baseline 和 Candidate 的 Regression Comparison Core。核心不是简单比较两个 score，而是先解决跨 Run 的 identity 和 provenance 问题。Run 内 Result 唯一键包含 run 和 attempt，但跨 Run 比较时这两个字段天然不同，所以最终用 case id/version 加 evaluator id/version 作为 alignment key，同时检查 evaluator config 和 prompt 是否一致。
>
> 两边只允许比较 COMPLETED Run，并且要求同 tenant、同 dataset、suite 和 target identity，但允许 target version 不同，因为这正是版本回归的主要场景。最终输出 REGRESSION、IMPROVEMENT、UNCHANGED、NOT_COMPARABLE 四类结果。PostgreSQL 16 的真实 integration 也验证了不同 attempt、不同 target version 和跨 tenant fail closed。

------

# 15. 2 分钟面试版本

> Phase 2 完成之后，我们已经有了不可变的 EvaluationResult，但还不能回答 Candidate 相比 Baseline 到底退化了哪些 Case，所以 Phase 3 的第一个 WP 我先实现了 Regression Comparison Core。
>
> 我一开始重点解决的是 cross-run alignment identity。Phase 2 的 Result logical slot 包含 run_id 和 attempt_id，这是为了 Run 内去重，但 Baseline 和 Candidate 天然属于不同 Run 和 Attempt，所以这套 key不能直接用于 comparison。我最后使用 `(case_id, case_version, evaluator_id, evaluator_version)` 做跨 Run对齐，run/attempt只保留为 provenance。
>
> 在真正比较前，还会 fail closed 检查两边都必须是 COMPLETED，同 project、dataset、suite和execution target identity。Target version允许不同，因为Regression本来就是用来比较旧版本和新版本的。
>
> 对齐以后最小分类是 PASS→FAIL 为 REGRESSION，FAIL→PASS为IMPROVEMENT，同 verdict 为UNCHANGED，INCONCLUSIVE、ERROR或单侧missing Result都标成NOT_COMPARABLE。另外即使 evaluator id/version 相同，只要config_ref或prompt_ref不同，也不会硬比较，因为评测语义已经可能变化。
>
> Score在这个WP里只作为附加证据。比如两边都是PASS但Candidate分数下降，我会记录score_regressed，但不直接把classification改成REGRESSION，因为score-only policy还没有冻结。
>
> 整个Comparison是纯Application计算，没有新建数据库表。Final Gate用PostgreSQL 16真实跑了Baseline/Candidate比较、跨tenant和不兼容Run场景，最终Project Gate PASS。

------

# 16. 深入版本

如果面试官继续问，可以沿这条主线讲：

```text
Phase 2
产生可信 Evaluation Fact
        ↓
Phase 3 WP1
解决 Cross-run Identity
        ↓
Run Eligibility
        ↓
Run-level Comparability
        ↓
Result Alignment
        ↓
Evaluator Provenance Check
        ↓
Verdict Classification
        ↓
Regression Comparison Fact
```

核心是三个“不能混”：

### 第一

```text
Intra-run logical slot
≠
Cross-run alignment key
```

### 第二

```text
Run COMPLETED
≠
Evaluator PASS
```

### 第三

```text
Same evaluator id/version
≠
Same evaluator semantics
```

因为 config/prompt 仍可能变化。

整个设计因此坚持：

> **Identity 决定能否对齐，Provenance 决定能否比较，Verdict 决定如何分类。**

------

# 17. 高频追问

## Q1：为什么 Case Version 必须在 alignment key 里？

因为 Case 内容、输入或期望变化后：

```text
case-A@v1
```

和：

```text
case-A@v2
```

已经不是同一个实验条件。

如果直接比较，Candidate 的变化可能来自 TestCase变化，而不是被测系统退化。

------

## Q2：为什么 Dataset Version 可以不同，但 Case Version 必须相同？

因为 DatasetVersion 是一个集合级 snapshot。

两个 Dataset version 可以因为：

-新增其它 Case；

- metadata变化；

而不同。

只要当前实际比较的 slot：

```text
case_id + case_version
```

完全一致，就仍有局部 comparison意义。

------

## Q3：为什么 Suite Version 也可以不同？

同理。

真正对当前 Result语义有直接约束的是：

- evaluator identity/version；
- config；
- prompt。

Suite版本可以变化，但对应 evaluator semantics仍需兼容。

------

## Q4：为什么 execution_target_id 必须相同，而 version可以不同？

因为：

```text
LocalAgent v1
vs
LocalAgent v2
```

属于版本回归。

但：

```text
LocalAgent
vs
CompletelyDifferentAgent
```

默认不是同一被测主体的 regression。

------

## Q5：为什么不持久化 Comparison？

当前 Comparison是完全由 immutable/append-only EvaluationResult 推导的。

所以：

```text
same inputs
→ same comparison
```

WP1不需要引入额外持久化真值。

后续 Report/Release Gate如果确实需要历史 decision snapshot，再单独评估。

------

## Q6：为什么 Missing Result 不是 Regression？

因为 Missing 只能证明：

> 当前 comparison slot没有两侧完整证据。

不能证明：

> Candidate质量下降。

所以必须 NOT_COMPARABLE。

------

## Q7：为什么 ERROR 不是 Regression？

Evaluator ERROR 表示：

> 评测过程自身没给出可信质量判断。

不能把 evaluator失败错误地归因给 Candidate。

------

## Q8：为什么 config mismatch 不直接 fail 整个 Comparison？

因为只是某一个 slot 不可比较。

其它 Case/Evaluator slot 仍然可能完全兼容。

所以 slot级：

```
NOT_COMPARABLE
```

比整个 Run compare失败更精确。

------

# 18. 最容易夸大 / 答错

### 错误说法 1

> “我们已经做完 Regression Report。”

错。

当前只做 per-slot comparison。

------

### 错误说法 2

> “Comparison Result 已经入库了。”

错。

当前是 pure Application computation。

------

### 错误说法 3

> “PASS→PASS 但 score下降也会判 regression。”

错。

当前仍：

```
UNCHANGED
```

只记录 score evidence。

------

### 错误说法 4

> “我们支持任意两个 Run比较。”

错。

当前只接受两个 `COMPLETED` Run，而且存在明确 comparability约束。

------

### 错误说法 5

> “Regression可以跨不同 evaluator版本比较。”

错。

`evaluator_id/version` 是 alignment identity的一部分。

------

### 错误说法 6

> “LocalAgent已经接入 Regression Core。”

错。

WP1完全 Runtime-neutral，没有 LocalAgent-specific dependency。

------

# 19. P0 / P1 / P2

最终 Gate：

```text
P0 = 0
P1 = 0
P2 = 0
P3 = 0
```

当前虽然没有 Gate级 P2，但仍有明确 Deferred：

- score-only regression policy；
- comparison persistence；
- FAILED Run comparison；
- historical comparison；
- automatic baseline selection。

它们属于范围边界，而不是当前 defect。

------

# 20. 速查表

| 问题                                   | WP1 答案                                                    |
| -------------------------------------- | ----------------------------------------------------------- |
| Baseline/Candidate 是新 DB entity 吗？ | 否                                                          |
| Comparison 是否持久化？                | 否                                                          |
| 输入是什么？                           | 两个 EvaluationRun ID + project_id                          |
| Run 必须是什么状态？                   | COMPLETED                                                   |
| 跨 tenant允许吗？                      | 不允许                                                      |
| dataset_id 必须一致？                  | 是                                                          |
| suite_id 必须一致？                    | 是                                                          |
| target id 必须一致？                   | 是                                                          |
| target version 必须一致？              | 否                                                          |
| Cross-run key                          | case id/version + evaluator id/version                      |
| attempt_id进入 key？                   | 否                                                          |
| PASS→FAIL                              | REGRESSION                                                  |
| FAIL→PASS                              | IMPROVEMENT                                                 |
| PASS→PASS                              | UNCHANGED                                                   |
| FAIL→FAIL                              | UNCHANGED                                                   |
| ERROR/INCONCLUSIVE                     | NOT_COMPARABLE                                              |
| 单侧 missing                           | NOT_COMPARABLE                                              |
| config/prompt mismatch                 | NOT_COMPARABLE                                              |
| score下降能翻转 classification？       | 当前不能                                                    |
| Result ambiguity                       | fail closed                                                 |
| PostgreSQL真实验证                     | 3 passed                                                    |
| Full Unit                              | 506 passed                                                  |
| 下一层能力                             | Regression Report / Critical Case / Release Gate Foundation |

这个 WP 最值得记住的一句话是：

> **Phase 2 解决“评测事实是否可信”，Phase3-WP1 解决“两个可信事实是否真的可以比较，以及应该如何对齐和分类”。**