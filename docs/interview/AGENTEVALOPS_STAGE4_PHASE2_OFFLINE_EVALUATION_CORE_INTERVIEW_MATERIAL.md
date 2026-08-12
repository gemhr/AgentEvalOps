# 推荐面试材料命名

```
AGENTEVALOPS_STAGE4_PHASE2_OFFLINE_EVALUATION_CORE_INTERVIEW_MATERIAL.md
```

# 1. 一句话项目定义

我在 AgentEvalOps 中从零建立了一套 **Offline Evaluation Core（离线评测核心）**：先定义 Dataset/TestCase/Suite/Evaluator 的不可变评测领域模型，再抽象 Runtime-neutral（运行时无关）的 ExecutionTarget（执行目标），随后实现 PostgreSQL 上的 Run/Attempt/Result 持久化与并发控制，最后通过 Attempt-oriented Evaluation Loop（以执行尝试为粒度的评测循环）把整个链路串成真正可运行的闭环。

最终 Phase 2 已形成：

```text
Dataset / TestCase
        ↓
EvaluationSuite
        ↓
EvaluationRun
        ↓
ExecutionAttempt
        ↓
ExecutionTarget
        ↓
ExecutionOutcome
        ↓
Evaluator(s)
        ↓
EvaluationResult
        ↓
Run Completion
```

这套核心保持 Evaluation-first（评测优先）、Trace optional、Runtime-neutral、Result append-only、短事务和明确的状态所有权，为后续 Phase 3 Regression Core（回归评测核心）提供基础。

------

# 2. Phase 2 最终完成状态

当前真实状态：

```text
Stage4-Phase2 — Offline Evaluation Core
│
├─ WP0 Environment Baseline               ✅ PASS
├─ WP1 Evaluation Domain Foundation       ✅ PASS
├─ WP2 Execution Target Foundation        ✅ PASS
├─ WP3 Run / Attempt Persistence          ✅ PASS
└─ WP4 Minimal Evaluation Loop            ✅ PASS
```

其中：

- WP0 建立开发与测试基线；
- WP1 建立评测领域语言；
- WP2 建立“如何执行被测系统”的统一合同；
- WP3 建立“执行事实如何安全持久化”的基础设施；
- WP4 建立“这些能力如何按正确顺序运行”的 Application Loop（应用循环）。

WP3 最终真实 PostgreSQL combined suite 为 **37 passed**，Full Unit 为 **296 passed**；WP3 Gate 为 PASS，并保留 1 个非阻塞 P3 maintainability limitation。

WP4 最终 PostgreSQL integration 为 **10 passed**，WP3+WP4 combined 为 **33 passed**，Full Unit 为 **338 passed**，P0/P1/P2/P3 均为 0。

------

# 3. 真实性与完成边界

这是整个 Phase 2 最应该先讲清楚的部分。

## 3.1 已真实实现

### Evaluation Domain

已经真实存在：

- `DatasetVersion`
- `TestCaseVersion`
- `EvaluationSuiteVersion`
- `EvaluatorSpec`
- `EvaluationPolicy`
- `EvaluationInput`
- `EvaluationResultDraft`
- `EvaluationResult`
- `VersionRef`
- `CaseVersionRef`
- `ArtifactRef`
- `EvidenceRef`
- `CapabilityRequirement`

这些对象以 immutable snapshot（不可变快照）为核心，Trace 只是 optional `EvidenceRef`，不是评测 identity。

### Execution Contract

已经实现：

- `ExecutionTargetRef`
- `ExecutionRequest`
- `ExecutionOutcome`
- `ExecutionTarget`
- Capability validation
- deterministic `FixtureExecutionTarget`

Outcome 已明确区分：

- `SUCCESS`
- `FAILURE`
- `TIMEOUT`
- `CANCELLED`
- `OUTCOME_UNKNOWN`。

### Persistence / Concurrency

已经实现：

- EvaluationRun
- ExecutionAttempt
- EvaluationResult
- PostgreSQL schema / Migration
- Application UoW（工作单元）
- Atomic Claim（原子认领）
- Claim Token Fencing（认领令牌栅栏）
- Retry lineage
- stale reconciliation
- Tenant composite ownership
- Result logical uniqueness
- Result append-only
- immutable Result Trigger
- transaction rollback
- concurrent retry typed convergence。

### Minimal Evaluation Loop

已经实现：

- `EvaluationLoopService`
- Attempt-oriented orchestration
- Target resolver seam
- Evaluator resolver seam
- multi-evaluator
- EvaluationInput assembly
- EvaluationResult assembly
- terminal SUCCESS re-entry
- duplicate Result convergence
- Run finish orchestration
- evaluator error policy normalization
- no external I/O inside persistence transaction。

------

## 3.2 已真实测试

Phase 2 不只是单元测试。

真实验证包括：

- PostgreSQL 16；
- Migration round-trip；
- Atomic Claim race；
- Wrong-token fencing；
- concurrent retry；
- stale vs finalize race；
- duplicate Result race；
- Tenant isolation；
- cross-project DB constraints；
- Result Trigger；
- SQL NULL semantics；
- Target capabilities + config + version；
- multi-evaluator；
- duplicate delivery；
- terminal re-entry；
- external Target/Evaluator transaction boundary。

WP3 最终动态矩阵全面 PASS。

WP4 最终又在真实 PostgreSQL 中覆盖 capability + config + CapabilityRequirement 的完整路径。

------

## 3.3 尚未实现

Phase 2 **没有**完成：

- Regression comparison；
- Baseline/Candidate；
- Critical Case；
- Release Gate；
- Production Catalog Loader；
- Production Target Registry；
- Production Evaluator Registry；
- concrete LLM Judge；
- HTTP Target；
- Replay Target；
- LocalAgent Target；
- Celery production dispatch；
- API/UI；
- automatic retry orchestration；
- heartbeat；
- durable cancellation；
- DB-only durable resume；
  -完整 Trace/Evidence store。

其中 Regression/Baseline/Candidate/Release Gate 本身就是下一阶段 Phase 3 的范围，而不是 Phase 2 遗漏。

------

# 4. Phase 2 的核心架构思想

整个 Phase 2 可以总结成四层。

```text
┌────────────────────────────────────┐
│ Evaluation Domain                  │
│ Dataset / Case / Suite / Evaluator │
└─────────────────┬──────────────────┘
                  │
┌─────────────────▼──────────────────┐
│ Execution Contract                 │
│ Request / Target / Outcome         │
└─────────────────┬──────────────────┘
                  │
┌─────────────────▼──────────────────┐
│ Persistence & Concurrency          │
│ Run / Attempt / Result / UoW       │
└─────────────────┬──────────────────┘
                  │
┌─────────────────▼──────────────────┐
│ Application Orchestration          │
│ EvaluationLoopService              │
└────────────────────────────────────┘
```

最重要的点：

> **这四层没有互相吞掉职责。**

Domain 不知道 PostgreSQL。

ExecutionTarget 不知道 Run 生命周期。

Repository 不知道如何调用 Evaluator。

EvaluationLoopService 不直接修改 ORM。

这种职责分离是整个 Phase 2 最核心的工程成果。

------

# 5. WP0 — Environment Baseline

WP0 没有业务功能，它的意义是：

> **先证明项目当前状态可靠，再开始改架构。**

当时建立的基线包括：

- Python 3.13.12；
- `uv sync --frozen --group test` PASS；
- `uv lock --check` PASS；
- Unit：210 passed；
- Ruff PASS；
- tracked files 无改动。

## 学习重点

大型改造前必须知道：

```text
Before:
210 passed

After:
338 passed
```

中间新增了什么，而不是面对一个本来就坏的仓库继续改。

这也是为什么：

> Baseline 本身就是工程资产。

------

# 6. WP1 — Evaluation Domain Foundation

WP1 解决的是：

> **Agent Evaluation 到底有哪些核心概念？**

而不是“先建数据库表”。

最终领域关系：

```text
DatasetVersion
      │
      └── CaseVersionRef
              │
              ▼
        TestCaseVersion

EvaluationSuiteVersion
      │
      ├── Case selection
      ├── EvaluatorSpec(s)
      ├── EvaluationPolicy
      └── CapabilityRequirement(s)

Evaluator
      │
      ▼
EvaluationResultDraft
      │
      ▼
EvaluationResult
```

WP1 明确采用新的 `app.core.evaluation` bounded context（限界上下文），没有继续扩展 legacy `app.core.evals`。Trace 只允许作为 optional evidence；LocalAgent Runtime schema 也没有提前进入核心 Domain。

------

# 7. 为什么 Domain 全部做 Versioned + Immutable

评测系统最重要的需求之一是：

> **可复现。**

如果：

```text
Case A
```

今天和明天内容可以偷偷变化，那么：

```text
Baseline Run #1
Candidate Run #2
```

即使都说“Case A”，也未必在测同样的东西。

所以使用：

```text
TestCaseVersion(case_id, version)
DatasetVersion(dataset_id, version)
EvaluationSuiteVersion(suite_id, version)
```

而不是只靠普通 ID。

同时 payload/config/metadata 递归冻结。

这为 Phase 3 的：

```text
Baseline
vs
Candidate
```

提供了稳定比较基础。

------

# 8. 为什么 Trace 不能成为 Evaluation Identity

PandaProbe 原来很多 Evaluation 行为和 Trace/Session 强相关。

Phase 2 明确改成：

```text
Evaluation
独立存在

Trace
只是 optional evidence
```

原因是未来执行对象可能是：

- LocalAgent；
- HTTP Agent；
- Replay；
- Fixture；
  -其他 Agent Runtime。

并不是每一个系统都天然使用 PandaProbe Trace。

如果核心 identity 设计成：

```text
trace_id
→ evaluation
```

AgentEvalOps 就无法成为通用评测平台。

------

# 9. WP2 — Execution Target Foundation

WP2 回答：

> **我们到底在评测什么系统，又如何统一调用它？**

关键抽象：

```text
ExecutionTargetRef
        ↓
ExecutionRequest
        ↓
ExecutionTarget.execute()
        ↓
ExecutionOutcome
```

Target 可以未来对应：

- LocalAgent；
- HTTP Agent；
- Replay；
- Fixture。

核心 Domain 完全不关心具体 Transport（传输方式）。

WP2 最终新增 27 个测试，WP1+WP2 focused 为 63 passed，Full Unit 从 246 增长到 273。

------

# 10. ExecutionOutcome 的五态为什么重要

WP2 最重要的学习点之一，就是没有用简单的：

```text
success = true / false
```

而是：

```text
SUCCESS
FAILURE
TIMEOUT
CANCELLED
OUTCOME_UNKNOWN
```

特别是：

## FAILURE

系统确认：

> 执行失败。

## TIMEOUT

系统确认：

> 超时，而且终止事实可以确认。

## CANCELLED

确认取消已经生效。

## OUTCOME_UNKNOWN

最关键：

> 我无法确定远端最终执行结果或副作用。

这和 `FAILURE` 完全不同。

------

# 11. SUCCESS != PASS

这是整个 Phase 2 必须记住的一条。

```text
Execution SUCCESS
≠
Evaluation PASS
```

例如模型正常返回：

> 北京是美国首都。

那么：

```text
ExecutionOutcome = SUCCESS
EvaluationVerdict = FAIL
```

因为：

- Execution 评价“有没有正常执行”；
- Evaluation 评价“结果质量如何”。

WP2 已经用自动化测试明确验证这两个代数互不污染。

------

# 12. WP3 — Run / Attempt Persistence

WP3 是整个 Phase 2 工程复杂度最高的一部分。

它解决的是：

> **评测开始运行以后，状态到底由谁拥有？并发 Worker 同时操作时，哪个事实算数？**

最终选择：

```text
EvaluationRun
    │
    ├── ExecutionAttempt Case-A #1
    ├── ExecutionAttempt Case-B #1
    └── ExecutionAttempt Case-C #1
```

一个 Attempt 表示：

> 某个 TestCase 的一次真实执行。

一个 EvaluationResult 表示：

> 某个 Evaluator 对这个 Attempt artifact 的一次判断。

所以：

```text
1 Attempt
→ N EvaluationResults
```

------

# 13. Run 与 Attempt 状态机

Run：

```text
PENDING
   ↓
RUNNING
   ├── COMPLETED
   ├── FAILED
   └── OUTCOME_UNKNOWN
```

Attempt：

```text
PENDING
   ↓
CLAIMED
   ↓
RUNNING
   ↓
TERMINAL
```

TERMINAL 再携带：

```text
SUCCESS
FAILURE
TIMEOUT
CANCELLED
OUTCOME_UNKNOWN
```

这里很重要的一点是：

> lifecycle state 和 execution outcome 没揉成一个枚举。

------

# 14. Atomic Claim

多个 Worker：

```text
Worker A ─┐
          ├── claim Attempt X
Worker B ─┘
```

不能：

```text
SELECT status
if status == PENDING:
    UPDATE
```

因为存在 TOCTOU（检查时与使用时竞态）。

WP3 将 Claim 下沉为 PostgreSQL 条件更新：

```text
UPDATE ...
WHERE status = PENDING
RETURNING ...
```

最终只有一个竞争者成功。

数据库负责：

> 谁赢得这个事实。

Application 不靠 Python lock 猜。

------

# 15. Fencing：为什么 Claim 成功还不够

Worker A 曾经 Claim 成功：

```text
token=A
```

随后 lease 失效。

旧 Worker A 又回来提交 Result。

不能因为：

> “它曾经是 Owner”

就允许写。

所以后续 mutation 都需要 Token：

```text
current token
==
persisted token
```

这就是 Fencing。

核心原则：

> **Past ownership ≠ current authority。**

------

# 16. Retry 为什么创建新 Attempt

失败后不能：

```text
Attempt #1 FAILURE
↓
reset PENDING
↓
Attempt #1 SUCCESS
```

这会覆盖历史。

正确：

```text
Attempt #1 FAILURE
       │
       └── retry
              ↓
         Attempt #2 SUCCESS
```

同时：

```text
retry_of_attempt_id = Attempt #1
```

所以：

- old Attempt 保留；
- old Outcome 保留；
- old Result 保留；
- history 可审计。

WP3 还规定 `OUTCOME_UNKNOWN` 不允许盲目自动 retry，需要显式 authorization，因为原执行可能已经产生副作用。

------

# 17. Result 为什么必须 Append-only

EvaluationResult 是：

> 历史评测事实。

所以不能：

```text
UPDATE score
```

WP3 从多个层次防止：

### Repository

没有 update/delete result path。

### DB uniqueness

logical slot：

```text
run
attempt
case
evaluator
```

必须唯一。

### PostgreSQL Trigger

Result UPDATE 被直接拒绝。

这为后面 Regression 提供了可信数据基础。

------

# 18. Tenant Isolation 为什么不能只靠 WHERE project_id

Application 查询带：

```text
project_id
```

还不够。

数据库也通过 composite FK 保证：

> Project B 的 Result 不能引用 Project A 的 Run/Attempt。

所以 Tenant boundary 有两层：

```text
Application scope
+
Database structural constraint
```

不是单靠“代码里记得加过滤条件”。

------

# 19. WP3 最大的三个动态问题

这三个问题是 Phase 2 最有面试价值的 Bad Case。

## Bad Case A — Python None ≠ JSON null ≠ SQL NULL

### 真实性

假设构造，真实 PostgreSQL 动态发现并修复。

### 问题

Domain：

```text
output_artifact_ref = None
```

预期：

```text
SQL NULL
```

但 JSONB binder 写成：

```text
JSON null
```

数据库 CHECK 使用：

```text
IS NULL
```

导致 non-success Outcome 无法持久化。

最终仅针对该列使用正确 `none_as_null` semantics，FAILURE/TIMEOUT/CANCELLED/OUTCOME_UNKNOWN 全部动态验证通过。

### 学习点

> Domain absence 必须有唯一的 persistence representation。

------

## Bad Case B — Migration Trigger 测试自己先挂了

### 真实性

假设构造，测试代码真实失败。

### 问题

SQLAlchemy `text()` 内嵌 JSON：

```text
{"timeout_seconds":1}
```

把：

```text
:1
```

错误解析成 bind parameter。

结果测试根本没执行到 UPDATE Trigger。

### 学习点

> Integration test 红了，不等于 production implementation 错了。

先判断：

```text
failure layer
```

是：

- Product；
- DB；
- Migration；
- Test harness；
- Environment。

------

## Bad Case C — Concurrent Retry 数据库正确，但 Adapter 仍然错误

两个 Retry 同时竞争。

数据库已经保证：

```text
child count = 1
```

但 loser 可能违反：

- direct-retry unique；
- case-number unique。

Repository 最初只识别其中一个，所以有时泄漏 raw `IntegrityError`。

最终采用：

```text
SAVEPOINT
→ insert/flush
→ conflict
→ rollback savepoint
→ query authoritative child
→ compare immutable retry intent
→ RetryAlreadyCreated
```

不相符则继续抛原 `IntegrityError`。

真实结果：

- success = 1；
- RetryAlreadyCreated = 1；
- raw IntegrityError = 0；
- direct child = 1。

### 学习点

> 数据库约束保证 invariant，Repository 还必须把数据库事实安全翻译成 Domain Contract。

------

# 20. SAVEPOINT 的真正作用

这里不要把 SAVEPOINT 理解成普通“嵌套事务”。

它的用途是：

> **发生可预期 Constraint Error 后，让当前 outer transaction 仍然有能力查询权威数据。**

因为：

```text
flush
→ IntegrityError
```

以后 transaction 通常已经处于 failed state。

如果没有 SAVEPOINT：

```text
catch
→ SELECT existing child
```

本身可能无法执行。

所以：

```text
Outer UoW
    │
    └── SAVEPOINT
            └── retry insert
```

局部失败后只回滚 SAVEPOINT。

Outer transaction仍然有效。

------

# 21. WP4 — Minimal Evaluation Loop

WP4 解决：

> WP1、WP2、WP3 都有了，但谁把它们真正串起来？

最终选择：

**Single Application Orchestrator（单一应用编排器）**

即：

```
EvaluationLoopService
```

但粒度不是：

```text
execute_run()
```

而是：

```text
execute_attempt()
```

因为 Atomic Claim、Fencing、Target execution 的 ownership 本身就是 Attempt 粒度。

------

# 22. WP4 最终执行链

正常 SUCCESS：

```text
preflight
    ↓
claim Attempt
    ↓
start Attempt
    ↓
commit
    ↓
ExecutionTarget.execute()
    ↓
record SUCCESS
    ↓
commit
    ↓
build EvaluationInput
    ↓
Evaluator A
    ↓
finalize Result A
    ↓
Evaluator B
    ↓
finalize Result B
    ↓
finish_run()
```

每一个 Persistence operation 都是短事务。

Target 和 Evaluator 调用期间没有打开 DB transaction。

------

# 23. 为什么 Application Loop 不使用长事务

错误设计：

```text
BEGIN
↓
claim
↓
调用 LLM 30 秒
↓
Evaluator
↓
写 Result
↓
COMMIT
```

风险：

-连接池被占用；
-数据库锁生命周期过长；
-外部服务故障拖住事务；

- deadlock 风险；
- rollback 与 external side effect不一致。

最终：

```text
DB transaction
→ close

external call

DB transaction
→ close
```

而且 WP4 真实测试使用 task-local `ContextVar` 证明 Target/Evaluator 调用时当前 coroutine active UoW 为 0。

------

# 24. 为什么 Preflight 要发生在 Claim 前

WP4 会先验证：

- TestCase identity；
- input payload；
- Target snapshot；
- Target version；
- Target resolver；
- Evaluator resolver；
- evaluator identity。

原则：

> 能在产生副作用前发现的 deterministic error，就不要等执行之后再报错。

否则可能：

```text
模型已经执行
↓
才发现 evaluator version 配错
```

此时已经留下无法正确评测的执行事实。

------

# 25. Target Exception 和 Evaluator Exception 为什么处理不同

这是 Phase 2 很高级但非常实用的一点。

## Target exception

如果：

```text
await target.execute()
```

直接抛异常：

你不知道：

- 请求是否已经发送；
  -远程任务是否完成；
  -是否产生副作用。

所以 WP4：

```text
不伪造 FAILURE
不伪造 OUTCOME_UNKNOWN
原异常传播
Attempt 保持 RUNNING
```

等待 stale reconciliation。

## Evaluator exception

Execution 已经 SUCCESS。

Evaluator 只是评价已有 artifact。

而 `EvaluationPolicy` 明确定义：

```text
evaluator_error
```

应该归一化成：

- FAIL；
- INCONCLUSIVE。

因此这里可以生成脱敏 Evaluation Result。

核心差异：

> **Execution Truth 与 Evaluation Judgment 的事实所有权不同。**

------

# 26. Multi-evaluator 与 Partial Survival

WP4 首版直接支持：

```text
1 Attempt
→ N Evaluators
→ N Results
```

每个 Result独立 commit。

所以：

```text
Evaluator A ✅
Evaluator B ✅
Evaluator C ❌ contract bug
```

A/B 不 rollback。

下一次 re-entry：

```text
skip A
skip B
only run C
```

这和 WP3 Result append-only contract天然匹配。

------

# 27. Re-entry 不等于 Replay

EvaluationLoopService 根据当前状态决定行为：

```text
Run terminal
→ ALREADY_COMPLETE

Attempt PENDING
→ claim + execute

Attempt CLAIMED / RUNNING
→ IN_PROGRESS

Attempt TERMINAL non-success
→ finish only

Attempt TERMINAL SUCCESS
→ do not execute Target again
→ fill missing Result slots
```

这叫：

**State-driven resume（状态驱动恢复）**

不是：

> 一重试整个函数就全部再执行一遍。

------

# 28. Duplicate Result Race

两个 terminal-success resume caller：

```text
Caller A ─┐
          ├── Evaluator B
Caller B ─┘
```

都可能执行外部 evaluator。

但最终数据库只有一个 slot。

loser 获得：

```
ResultAlreadyFinalized
```

WP4 不直接：

```text
except:
    pass
```

而是 authoritative reread。

只有 existing Result：

- logical slot一致；
- provenance一致；

才视为 benign convergence。

否则继续抛异常。

------

# 29. WP4 最大 Bad Case：Same Concept ≠ Same Projection

这是 Phase 2 后半段非常有价值的新知识。

Target full snapshot：

```text
id
kind
version
config
capabilities
```

但 PostgreSQL 中：

### Run view

```text
id
kind
version
capabilities
```

### Attempt view

```text
id
kind
version
config
```

它们描述：

> 同一个 Target。

但不是：

> 同一种数据投影。

原代码：

```text
full == run_view == attempt_view
```

所以合法 Target 带 capability/config 时反而被拒绝。

第一次 H-4 即使：

```text
PostgreSQL 9 passed
Unit 327 passed
```

仍然因为源码审查发现这一 coverage blind spot，Project Gate 被判 FAIL。

修复成：

```text
Run
→ 校验 Run拥有的字段

Attempt
→ 校验 Attempt拥有的字段

Resolved Target
→ 仍和 authoritative full snapshot 完整 equality
```

最终 capability + config + CapabilityRequirement 真实 PostgreSQL完整闭环 PASS。

## 学习点

> **Same entity does not mean same projection。**

这是整个 Phase 2 非常值得在系统设计面试里使用的经验。

------

# 30. Phase 2 的状态所有权

可以背这张表：

| Fact / State                      | Owner                        |
| --------------------------------- | ---------------------------- |
| Dataset / Case / Suite definition | Evaluation Domain            |
| ExecutionRequest                  | Attempt / WP3                |
| Execution classification          | ExecutionTarget Adapter      |
| Run lifecycle                     | WP3 Application + PostgreSQL |
| Attempt lifecycle                 | WP3 Application + PostgreSQL |
| Claim Token                       | WP3                          |
| Retry lineage                     | WP3                          |
| EvaluationInput assembly          | WP4 Loop                     |
| Evaluator execution ordering      | WP4 Loop                     |
| Evaluator quality judgment        | Evaluator                    |
| Result provenance assembly        | WP4 Loop                     |
| Result persistence / uniqueness   | WP3 + PostgreSQL             |
| Run finish decision               | WP3 `finish_run()`           |
| Automatic retry                   | **无人拥有，尚未实现**       |
| Celery dispatch                   | **尚未实现**                 |

一个核心原则：

> **一个事实只能有一个最终 owner。**

------

# 31. Phase 2 的三类“结果”一定不要混淆

## ExecutionOutcome

回答：

> 系统到底执行得怎么样？

```text
SUCCESS
FAILURE
TIMEOUT
CANCELLED
OUTCOME_UNKNOWN
```

## EvaluationVerdict

回答：

> 输出质量怎么样？

例如：

```text
PASS
FAIL
INCONCLUSIVE
```

## RunStatus

回答：

> 整个评测基础设施生命周期是否结束？

```text
PENDING
RUNNING
COMPLETED
FAILED
OUTCOME_UNKNOWN
```

所以可能：

```text
Execution SUCCESS
Evaluator FAIL
Run COMPLETED
```

完全正确。

------

# 32. Phase 2 的事务原则

一句话：

> **数据库只包数据库事实，不包外部世界。**

即：

```text
TX claim
commit

TX start
commit

Target external call

TX record outcome
commit

Evaluator external call

TX result
commit

TX finish
commit
```

这条原则以后可以直接迁移到：

- LLM 调用；
- Tool；
- RAG；
- MCP；
- payment；
- third-party API。

------

# 33. Phase 2 的幂等原则

不同层次有不同 identity：

### Request identity

```
request_id
```

### Stable business idempotency

```
idempotency_key
```

### Execution identity

```
attempt_id
```

### Ownership authority

```
claim_token
```

### Result logical uniqueness

```text
run
attempt
case
evaluator
```

它们不能合并成一个“万能 ID”。

这是非常好的面试知识点：

> **Idempotency Key、Entity Identity、Lease Token、Database Uniqueness 解决的是四类不同问题。**

------

# 34. Phase 2 的 Fail-closed 原则

多个地方都使用同一种思想。

### Capability 不满足

拒绝执行。

### Target version 缺失

拒绝评测。

### Evaluator identity mismatch

拒绝 Result。

### Retry conflict语义无法证明

抛原始 DB error，而不是乱映射。

### Result duplicate provenance不一致

不吞。

### Persistence projection mismatch

拒绝。

核心：

> **宁可明确失败，也不要生成“看起来正确但来源不可信”的评测事实。**

这对 Evaluation 系统尤其重要。

------

# 35. Phase 2 的测试哲学

整个阶段最值得学习的不只是测试数量，而是 Gate 的变化。

例如 WP3：

一开始 Unit 全绿。

静态审查仍发现 P1。

之后真实 PostgreSQL 又发现：

- JSONB null；
- Retry race；
- Trigger test问题。

最终才到：

```text
37 passed
```

WP4 同样：

第一次：

```text
9 PostgreSQL tests PASS
327 unit PASS
```

但 Codex源码审查仍发现合法 Target Projection 被误拒的 P1。

所以：

> **Green tests are evidence, not proof of completeness。**

最终 Gate 应由：

```text
Static review
+
Unit
+
Real DB dynamic
+
Adversarial scenarios
+
Architecture contract review
```

共同组成。

------

# 36. Phase 2 最有价值的 Bad Case 总表

| Bad Case                                        | 真实性                         | 核心知识                                      |
| ----------------------------------------------- | ------------------------------ | --------------------------------------------- |
| Python None → JSON null，而非 SQL NULL          | 假设构造，真实动态发现         | 跨层 canonical representation                 |
| Trigger test JSON bind先挂                      | 假设构造，测试真实失败         | Failure layer classification                  |
| Concurrent Retry loser泄漏 IntegrityError       | 假设构造，真实动态发现         | SAVEPOINT + typed error mapping               |
| Process-global UoW probe误判并发事务            | 实施测试真实发现               | Test instrumentation也有并发语义              |
| Full Target 与 partial projections直接 equality | Codex源码审查真实发现 + DB回归 | Same Concept ≠ Same Projection                |
| Target escaped exception被错误转 FAILURE        | 假设构造，测试覆盖             | Unknown是一等状态                             |
| concurrent terminal resume重复 evaluator call   | Known Limitation               | Exactly-one fact ≠ exactly-once external call |

如果面试只准备 3 个，优先：

1. Concurrent Retry + SAVEPOINT；
2. Target Projection；
3. SQL NULL / JSON null。

------

# 37. 当前 Known Limitations

Phase 2 完成不意味着“评测平台生产化完成”。

当前明确限制：

- DB-only durable resume：NO；
- caller仍需提供完整 TestCase；
  -没有 production Catalog loader；
  -没有 Target/Evaluator production registry；
  -没有 concrete production evaluator；
- timeout/cancellation enforcement由 Target Adapter负责；
- Target escaped exception后依赖 stale reconciliation；
- concurrent terminal resume可能重复 evaluator external call；
- `execution_outcome_ref=None`；
- fixture refs不解析；
- `required_result_missing` policy未消费；
  -无 automatic retry；
  -无 Celery/API/UI；
  -无 HTTP/Replay/LocalAgent Adapter；
- Run COMPLETED 不代表所有 verdict PASS。

另外 WP3 仍保留一个已接受非阻塞 P3：Suite snapshot drift guard 的 maintainability limitation。

------

# 38. Phase 2 对 Phase 3 的意义

Phase 2 完成后，现在系统终于拥有可信的基础数据：

```text
Dataset version
Suite version
Case version
Target version
Attempt history
Execution Outcome
Evaluator identity/version
Evaluation Result
```

因此 Phase 3 才能合法做：

```text
Baseline
     │
     ├── Case A PASS
     ├── Case B PASS
     └── Case C PASS

Candidate
     │
     ├── Case A PASS
     ├── Case B FAIL   ← regression
     └── Case C PASS
```

如果 Phase 2 没解决：

- version identity；
- Result immutable；
- Retry history；
- Target provenance；

那 Regression Report 根本不可信。

所以：

> **Phase 2 是“可信事实层”，Phase 3 才是“比较与决策层”。**

------

# 39. 对 LocalAgent 的意义

当前还没有接 LocalAgent，这一点必须保持真实性。

但 Phase 2 已经把未来接口留出来：

```text
AgentEvalOps
EvaluationLoopService
       ↓
ExecutionTargetResolver
       ↓
LocalAgentExecutionTarget   ← future
       ↓
LocalAgent Runtime
```

未来 LocalAgent Stage 3.5 Contract Freeze 后，可以把：

- Runtime version；
- Agent config；
- Tool contract；
- Retrieval version；
- Trace；
- output artifact；

通过 Adapter 映射进：

- `ExecutionTargetRef`
- `VersionRef`
- `ArtifactRef`
- `EvidenceRef`

而不用修改 Evaluation Core。

------

# 40. 一分钟面试回答

> 我在 AgentEvalOps 里完整做了一套离线评测核心。第一步不是直接建任务表，而是先把 Dataset、TestCase、Suite、Evaluator、Result 做成 versioned immutable domain，Trace 只作为 optional evidence，这样评测本身不会绑定某个 Agent Runtime。第二步抽象了 Runtime-neutral ExecutionTarget，把执行结果明确拆成 SUCCESS、FAILURE、TIMEOUT、CANCELLED 和 OUTCOME_UNKNOWN，并且明确执行成功不等于评测通过。
>
> 第三步是最复杂的持久化和并发控制。我把 Run、Attempt、Result 分开，用 PostgreSQL conditional update做 atomic claim，claim token做 fencing，Retry创建新 Attempt保留历史，Result做 append-only和 logical uniqueness，同时用 composite FK做 tenant ownership。并发 retry里还用了 SAVEPOINT，在 unique violation后查询 authoritative child，再决定能不能安全映射成 RetryAlreadyCreated。
>
> 最后用一个 Attempt-oriented EvaluationLoopService把执行和评测串起来。外部 Target和Evaluator调用不放在数据库事务中，SUCCESS Attempt支持多个 evaluator，Result逐个持久化，重入时只补 missing slots。
>
> 最终真实 PostgreSQL做过 claim race、retry race、duplicate result、tenant isolation、migration、multi-evaluator、duplicate delivery等验证，Phase 2最后 Full Unit是338 passed。下一步才进入 Baseline/Candidate和Regression Core。

------

# 41. 三分钟面试回答

> AgentEvalOps 的 Phase 2 我主要解决的是“如何得到可信、可复现、可并发执行的 Evaluation Fact”。我把它分成四层。
>
> 第一层是 Evaluation Domain。我定义 DatasetVersion、TestCaseVersion、EvaluationSuiteVersion、EvaluatorSpec、EvaluationPolicy 和 EvaluationResult等不可变对象，并且使用显式 version identity。Trace不是核心 identity，只是 optional Evidence，这样以后 LocalAgent、HTTP Agent、Replay都能复用同一个评测核心。
>
> 第二层是 Execution Contract。ExecutionTarget只暴露 execute(request)，Outcome分成 SUCCESS、FAILURE、TIMEOUT、CANCELLED、OUTCOME_UNKNOWN。这里特别强调 UNKNOWN不能压缩成 FAILURE，因为远端调用中断时经常无法确认副作用到底发生没有。同时 Execution SUCCESS和Evaluation PASS也是两个完全不同的维度。
>
> 第三层是 Run/Attempt/Result Persistence。Run表示整体任务，Attempt表示一次Case执行，Result表示某个Evaluator对该Attempt artifact的判断。Claim使用PostgreSQL条件UPDATE做CAS，后续mutation用claim token fencing。Retry不reset旧Attempt，而是创建新child Attempt。Result是append-only，同时数据库用唯一约束、组合外键和Trigger保证logical slot、tenant provenance以及不可修改。
>
> 这里动态测试发现过几个典型问题。比如Python None写JSONB时默认成JSON null，但数据库CHECK要求SQL NULL；并发retry时一个candidate可能同时撞两个unique constraint，数据库并不保证先报告哪个，所以最后我没有简单按constraint name吞异常，而是用SAVEPOINT恢复transaction，再查询existing child比较immutable retry intent，确认真的是同一retry才转成RetryAlreadyCreated。
>
> 第四层是Application Loop。我没有做execute_run，而是做attempt-oriented EvaluationLoopService，因为Attempt就是claim和execution ownership的粒度。Loop执行前做preflight，claim/start提交以后才调Target，然后另开事务record outcome。SUCCESS后按Suite顺序执行多个Evaluator，每个Result单独commit，re-entry只补missing result。Target escaped exception不猜成FAILURE，而是保持RUNNING等待stale reconciliation；Evaluator error则可以依据EvaluationPolicy归一化成FAIL或INCONCLUSIVE。
>
> Final Review还发现过一个很有代表性的projection bug：Run保存Target capabilities，Attempt保存config，full snapshot两者都有，原来直接拿三个dataclass做全等，导致合法数据被拒。后来改成按每个projection真正拥有的字段验证，而resolved Target仍与full snapshot严格全等。
>
> 最终Phase 2把“评测事实层”建设完了，下一阶段才有条件基于这些不可变、带版本和provenance的Result去做Baseline/Candidate比较和Regression Gate。

------

# 42. 高频追问

## Q1：为什么不直接基于 PandaProbe 原来的 EvalRun 改？

因为 Legacy EvalRun 与 Trace、Celery lifecycle、mutable score强绑定，而且 redelivery过程中存在 delete/reset 行为，与新的：

- Evaluation-first；
- Application owns lifecycle；
- Result append-only；

合同冲突。

------

## Q2：为什么需要 Attempt？

因为：

```text
TestCase
```

只是测试定义。

实际执行可能：

```text
Attempt #1 TIMEOUT
Attempt #2 SUCCESS
```

如果没有 Attempt，这些运行历史只能覆盖在 Case/Run 上。

------

## Q3：数据库唯一约束都在了，为什么还需要 Application validation？

数据库适合保证：

- uniqueness；
- FK ownership；
  -状态一致性。

但像：

- Evaluator threshold；
- Suite policy；
  -Target capability；
  -复杂 provenance；

仍需要 Domain/Application。

不要把所有业务逻辑复制成巨大 CHECK。

------

## Q4：为什么不保证 exactly-once？

基础设施在分布式环境更现实的能力通常是：

```text
at-least-once invocation
+
idempotent / unique persistence
```

目前 Phase 2 能保证 Result logical fact 唯一，但 concurrent resume仍可能重复调用 evaluator。

所以不能宣称 external exactly-once。

------

## Q5：为什么 OUTCOME_UNKNOWN 需要独立状态？

因为：

```text
确认失败
```

和：

```text
不知道成功还是失败
```

后续 Retry 策略完全不同。

特别是存在副作用时，UNKNOWN盲目 retry可能执行两遍。

------

## Q6：为什么 Run COMPLETED 不表示评测通过？

Run COMPLETED表示：

> 基础设施执行结束，required Result slots完整。

质量通过与否是 Result/后续 Regression/Release Gate的职责。

------

# 43. Phase 2 最应该背下来的 10 条

1. **Evaluation-first，Trace optional。**
2. **Definition 必须 versioned + immutable。**
3. **Execution SUCCESS != Evaluation PASS。**
4. **OUTCOME_UNKNOWN 不等于 FAILURE。**
5. **Run、Attempt、Result 各有唯一 owner。**
6. **Atomic Claim + Fencing 解决不同问题。**
7. **Retry 创建新事实，不覆盖旧事实。**
8. **Result append-only，历史评测结果不能原地改。**
9. **外部 I/O 不跨数据库事务。**
10. **Same Concept ≠ Same Projection。**

如果这 10 条能够连贯讲出来，基本就已经掌握了 Phase 2 的主要工程思想。

------

# 44. 整个 Phase 2 的最终主线

最后把整个阶段压缩成一句因果链：

```text
为了让 Regression 可信
        ↓
必须先让 Evaluation Result 可复现
        ↓
所以 Case/Suite/Evaluator 必须 versioned + immutable
        ↓
执行对象必须 Runtime-neutral
        ↓
Execution Truth 与 Evaluation Judgment 必须分离
        ↓
Run/Attempt/Result 必须有唯一状态 Owner
        ↓
并发事实交给 PostgreSQL 仲裁
        ↓
Result 必须 append-only
        ↓
Application Loop 只负责控制流，不重新拥有状态
        ↓
最终得到可信的 Offline Evaluation Fact Layer
        ↓
Phase 3 才能做 Baseline / Candidate / Regression / Release Gate
```

这就是 **Stage4-Phase2 Offline Evaluation Core** 最核心的学习结论。