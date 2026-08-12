# 1. 一句话项目定义

在 AgentEvalOps 的离线评测核心中，我为 EvaluationRun（评测运行）、ExecutionAttempt（执行尝试）和 EvaluationResult（评测结果）建立了一套独立的 PostgreSQL 持久化与并发控制机制，用数据库约束、CAS（比较并交换）、Fencing（栅栏）、SAVEPOINT（保存点）和 append-only（只追加）结果模型，解决多执行者场景下的重复认领、重复重试、重复结果、租户串写以及事务一致性问题。

WP3 最终已经通过静态 Gate 和真实 PostgreSQL 16 动态 Gate：最终 Codex 独立重跑两个 WP3 Integration（集成）模块 **37 passed / 0 failed / 0 errors**，完整 Unit Test（单元测试）**296 passed**，P0/P1/P2 均为 0。

------

# 2. 真实性与边界

## 2.1 已真实实现

WP3 已真实实现：

- 独立的 `EvaluationRun`、`ExecutionAttempt`、`EvaluationResult` 持久化模型；
- Run 五态：
  - `PENDING`
  - `RUNNING`
  - `COMPLETED`
  - `FAILED`
  - `OUTCOME_UNKNOWN`
- Attempt 四态：
  - `PENDING`
  - `CLAIMED`
  - `RUNNING`
  - `TERMINAL`
- PostgreSQL 原子 Claim；
- Claim Token（认领令牌）Fencing；
- Attempt Retry（重试）创建新 Attempt，而不是重置旧 Attempt；
- Result logical slot 唯一性；
- EvaluationResult append-only；
- PostgreSQL `BEFORE UPDATE` Trigger（触发器）阻止 Result 更新；
- Run + initial Attempts 同一 Unit of Work（工作单元）原子创建；
- `project_id` 租户隔离与 Composite Foreign Key（组合外键）；
- stale reconcile（过期协调）；
- Suite / Evaluator / Policy Snapshot（快照）；
- Result provenance（来源信息）校验；
- `ExecutionAttemptModel.output_artifact_ref` 的 SQL NULL canonical representation；
- Concurrent Retry（并发重试）下 Repository-local SAVEPOINT；
- PostgreSQL unique violation → `RetryAlreadyCreated` 的安全类型化映射。

最终源码审查确认 Repository 不接管 outer transaction 的 commit/rollback，Application/UoW 仍然是事务 owner。

## 2.2 已真实测试

不是只跑了 Mock/SQLite。

最终使用：

- PostgreSQL `16-alpine`
- Redis `7-alpine`
- 独立 Docker Compose project
- 真实 AsyncSession（异步会话）
- 真实并发 transaction

最终 Codex 独立验证：

```text
WP3 combined PostgreSQL suite:
37 passed
0 failed
0 errors
0 skipped

Full unit:
296 passed
0 failed
0 errors
0 skipped

Ruff:
PASS

uv lock --check:
PASS

Alembic head:
PASS

git diff --check:
PASS
```

而且 Atomic Claim、Fencing、Retry Race、Result Race、Tenant FK、Transaction Rollback、Migration Round-trip、Trigger 都是真实 PostgreSQL 动态测试，不是只看代码得出的结论。

## 2.3 只完成设计 / 尚未实现

以下不能在面试中说成已经做完：

- Celery（分布式任务队列）执行适配器；
- Worker Dispatch（工作节点分发）；
- 自动 Retry Policy；
- Heartbeat（心跳）；
- 完整 Cancellation（取消）合同；
- WP4 Evaluation Orchestration Loop（评测编排循环）；
- Concrete Evaluator（具体评测器）执行；
- HTTP / Replay / LocalAgent ExecutionTarget；
- API / UI；
- analytics projection；
- Legacy PandaProbe evaluation migration；
- 自动 LocalAgent Trace → Dataset/TestCase 转换。

最终 Closure Review 也明确把这些列为 WP3 范围之外。

## 2.4 Known Limitation

当前保留一个非阻塞 P3：

> Suite snapshot drift guard 没有完全直接绑定实际 snapshot keys。

它属于可维护性限制，目前没有发现 correctness failure，因此没有阻塞 WP3 Project Gate。

------

# 3. 原始工程场景

这里不是“用户在聊天框输入一句话”的产品场景，而是 **AgentEvalOps 离线评测的工程执行场景**。

假设一个 Evaluation Suite（评测套件）有 100 个 Test Case（测试用例）。

一个 EvaluationRun 被创建以后，需要为 Case 创建 ExecutionAttempt：

```text
EvaluationRun
    │
    ├── Attempt Case-A #1
    ├── Attempt Case-B #1
    ├── Attempt Case-C #1
    ...
```

后续执行环境可能出现：

```text
Worker A ─┐
          ├─ 同时 claim Attempt-X
Worker B ─┘
```

也可能：

```text
Worker A ─┐
          ├─ 同时 retry Attempt-X
Worker B ─┘
```

或者：

```text
Evaluator A ─┐
             ├─ 同时 finalize 同一 logical result
Evaluator B ─┘
```

如果只靠：

```python
if attempt.status == "PENDING":
    attempt.status = "CLAIMED"
```

这种应用层判断，在并发下会产生典型的 TOCTOU（检查时与使用时竞态）：

```text
A read PENDING
B read PENDING

A thinks it can claim
B thinks it can claim
```

所以 WP3 的核心不是“把三个对象存进 PostgreSQL”，而是：

> **谁拥有状态、谁有资格修改状态，以及多个执行者竞争同一状态时，数据库如何成为最终仲裁者。**

------

# 4. 架构演进

## 4.1 演进 A：从 PandaProbe Trace-coupled Evaluation 解耦

原 PandaProbe 的 Evaluation 更偏：

```text
Trace
  ↓
EvalRun
  ↓
TraceScore / SessionScore
```

问题是 Evaluation 身份与 Trace 强绑定，而且很多生命周期被 Celery 驱动。

新的 AgentEvalOps 目标则是：

```text
Dataset
  ↓
TestCase
  ↓
EvaluationSuite
  ↓
EvaluationRun
  ↓
ExecutionAttempt
  ↓
EvaluationResult
```

Trace 只能是 Evidence（证据），不能成为 Evaluation 的核心身份。

------

## 4.2 演进 B：Run、Attempt、Result 分离

三者职责被明确拆开。

### EvaluationRun

回答：

> 这一次整体评测任务是什么？

保存：

- Dataset/Suite identity；
- Target snapshot；
- evaluator/policy snapshot；
- overall lifecycle。

### ExecutionAttempt

回答：

> 某个 Case 这一次实际执行尝试是什么？

保存：

- Case；
- attempt number；
- request identity；
- claim ownership；
- outcome；
- retry lineage。

### EvaluationResult

回答：

> 某个 Evaluator 对某次 Attempt 的输出判断是什么？

Result 不能代替 Attempt。

尤其：

```text
ExecutionOutcome.SUCCESS
≠
EvaluationVerdict.PASS
```

模型成功返回一个错误答案：

```text
ExecutionOutcome = SUCCESS
EvaluationResult = FAIL
```

完全合理。

------

## 4.3 演进 C：把并发正确性交给 PostgreSQL

最初很多逻辑从业务上看都可以：

```text
read
check
write
```

但 WP3 最终收敛为：

> Application 负责业务决策，PostgreSQL 负责并发仲裁。

典型例子：

```text
Atomic Claim
→ UPDATE ... WHERE status=PENDING ... RETURNING
```

而不是：

```text
SELECT status
if PENDING:
    UPDATE
```

------

## 4.4 演进 D：从“唯一约束能挡住”升级到“稳定的 Typed Contract”

Concurrent Retry 最有代表性。

数据库原本已经正确保证：

```text
two retries
→ exactly one child
```

但 loser 有时违反：

```text
uq_evaluation_attempts_direct_retry
```

有时违反：

```text
uq_evaluation_attempts_case_number
```

如果 Repository 假设 PostgreSQL 永远返回第一个约束名，就会泄漏 raw `IntegrityError`。

最终演进为：

```text
SAVEPOINT
   ↓
INSERT
   ↓
UniqueViolation
   ↓
rollback SAVEPOINT
   ↓
查询 authoritative existing child
   ↓
比较 immutable retry intent
   ↓
确认确实是同一个 source 的 duplicate retry
   ↓
RetryAlreadyCreated
```

这一步把：

> “数据库没产生脏数据”

提升成：

> “数据库错误还能稳定翻译成领域可理解的类型化错误”。

------

# 5. 方案讨论与取舍

## 5.1 Claim：为什么不是 SELECT FOR UPDATE？

最终选择 Atomic Conditional UPDATE：

```text
UPDATE attempt
SET ...
WHERE
    status = PENDING
    AND claim_token IS NULL
RETURNING ...
```

优点：

- 单条 SQL 完成判断 + 状态修改；
- loser 不需要先获得 Python 对象再判断；
- 数据库天然提供原子竞争点；
- 更适合 claim 这种单状态转换。

核心思想：

> 能让数据库用一个原子 statement 表达的状态转换，不要拆成 read-check-write。

------

## 5.2 Retry：为什么不能只加一个 UNIQUE？

实际上 WP3 有两个约束：

```text
case-number uniqueness
direct-retry uniqueness
```

因为它们保护的是两个不同 invariant：

```text
同一个 Case 的 attempt_no 不重复

同一个 source 最多一个 direct retry
```

所以两个都必须保留。

------

## 5.3 为什么不把两个 UniqueViolation 全部直接映射成 RetryAlreadyCreated？

因为：

```text
case-number collision
```

只证明：

```text
相同 Case
相同 Attempt number
```

不能证明：

```text
它就是当前 source 的 child
```

所以：

```text
constraint name
```

只能作为候选信号。

最终还要查询 existing child，并校验 immutable retry intent。

这体现的是：

> **Fail Closed（失败时默认拒绝）优先于“错误信息看起来差不多就吞掉”。**

------

## 5.4 为什么需要 SAVEPOINT？

因为 SQLAlchemy flush 抛 `IntegrityError` 后，当前 transaction 会进入 failed state。

此时直接：

```text
catch IntegrityError
SELECT ...
```

通常不能安全继续。

所以使用：

```text
outer transaction
    ↓
SAVEPOINT
    ↓
INSERT + flush
```

SAVEPOINT 失败以后，只回滚局部：

```text
SAVEPOINT rollback
```

outer UoW 仍然可查询 existing child。

这里 SAVEPOINT 的作用不是“做嵌套业务事务”，而是：

> **给数据库错误翻译提供一个局部可恢复边界。**

------

## 5.5 JSON `null` 还是 SQL `NULL`？

最终规定：

```text
Domain:
None

Persistence:
SQL NULL
```

而不是：

```text
JSONB 'null'
```

对于 non-success Attempt：

```text
output_artifact_ref IS NULL
```

代表：

> 根本没有 output artifact。

最终选择单列：

```text
JSONB(none_as_null=True)
```

而不是：

- 修改 DB CHECK 同时接受两种 null；
- Repository 每条写路径手工 special-case；
- 全局修改所有 JSONB。

这样一个 Domain absence 只有一种 persistence representation。

------

# 6. 核心状态机与时序

## 6.1 Run 状态机

```text
PENDING
   │
   ▼
RUNNING
   │
   ├──────────────► COMPLETED
   │
   ├──────────────► FAILED
   │
   └──────────────► OUTCOME_UNKNOWN
```

Terminal Run 不重新打开。

Evaluator 返回 FAIL 不代表：

```text
Run = FAILED
```

只要执行正常完成，Evaluator 判断答案错误，Run 仍然可以正常完成。

------

## 6.2 Attempt 状态机

```text
PENDING
   │
   │ atomic claim
   ▼
CLAIMED
   │
   │ valid token
   ▼
RUNNING
   │
   │ outcome
   ▼
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

------

## 6.3 Claim + Fencing 时序

```text
Worker A                    PostgreSQL                   Worker B

 claim(token=A) ──────────►
                           UPDATE ... WHERE PENDING
                           ─────────────────────────► SUCCESS

                                                     claim(token=B)
                                      ◄──────────────
                           WHERE status=PENDING
                           不再匹配
                                                     FAIL
```

之后所有关键 mutation 都要验证 token。

所以即使旧 Worker 晚到：

```text
old token
→ fail closed
```

这就是 Fencing 的意义。

------

## 6.4 Stale Worker Race

真实测试覆盖：

```text
Worker A
record_outcome(SUCCESS)
        │
        ├──────── race
        │
Reconciler
mark OUTCOME_UNKNOWN
```

最终只能有一个 terminal fact 成功。

不能：

```text
SUCCESS
→ 又被 OUTCOME_UNKNOWN 覆盖
```

------

## 6.5 Retry

Retry 不做：

```text
old_attempt.status = PENDING
```

而是：

```text
Attempt #1
TERMINAL FAILURE
      │
      │ retry
      ▼
Attempt #2
PENDING
retry_of_attempt_id = Attempt #1
```

这样历史事实不会被覆盖。

------

# 7. 数据与权限边界

WP3 的隔离单位是：

```text
project_id
```

Run、Attempt、Result 都带 project ownership。

除了 Application 查询带 project 条件，还使用 Composite FK：

```text
(project_id, run_id)
→ EvaluationRun(project_id, id)
```

Result 对 Run/Attempt 的 provenance 也是组合外键。

这意味着：

> 即使绕过 Application，直接向 PostgreSQL 插入一个 Project B 的 Result，却引用 Project A 的 Run/Attempt，数据库也会拒绝。

这个场景已经真实 PostgreSQL 动态测试通过。

但注意：

WP3 做的是：

> persistence tenant ownership。

不是完整：

- RBAC（基于角色的访问控制）；
- 用户登录鉴权；
- SaaS permission system。

面试里不能把它说成“我做了完整权限系统”。

------

# 8. 兼容策略

WP3 没有直接推翻 PandaProbe 旧表。

而是新增独立三表：

```text
EvaluationRun
ExecutionAttempt
EvaluationResult
```

并保持：

- legacy evaluation tables 不动；
- Trace tables 不动；
- Session 继续只是旧 projection；
- LocalAgent contract 不提前冻结。

Migration 采用 additive（增量）方式。

真实测试覆盖：

```text
Empty DB
→ Head

Parent Revision
→ Head

Head
→ Parent
→ Head
```

全部 PASS。

这是一种典型的：

> **兼容迁移，而不是一次性重写旧系统。**

------

# 9. 问题 / 失败边界与 Bad Case

下面三个最适合拿去面试，因为都来自真实 PostgreSQL 动态验证过程，而不是“我想象可能有问题”。

但它们都不是线上生产事故，所以真实性统一按我们的约定标记。

------

## Bad Case 1：Python None 被写成 JSON `null`

**名称**

Non-success Outcome 被 PostgreSQL CHECK 拒绝。

**真实性**

真实性：假设构造，已通过测试覆盖。

**触发条件**

构造：

```text
ExecutionOutcome = FAILURE
output_artifact_ref = None
```

通过真实 PostgreSQL persistence path 写入 Attempt。

**错误表现**

Domain `None` 经默认 JSONB binder 写成：

```text
JSON null
```

而数据库要求：

```sql
output_artifact_ref IS NULL
```

于是触发 `CheckViolationError`。

**风险**

所有：

```text
FAILURE
TIMEOUT
CANCELLED
OUTCOME_UNKNOWN
```

都可能无法正常 terminalize。

这不是单个 Retry 用例失败，而是整个 non-success lifecycle 的 correctness bug。

**根因**

混淆：

```text
Python None
JSON null
SQL NULL
```

三个层次。

**错误设计**

默认认为：

```text
Python None
→ 数据库 NULL
```

而没有验证 JSONB 的 bind semantics。

**修复**

仅对：

```text
ExecutionAttempt.output_artifact_ref
```

配置：

```text
none_as_null=True
```

Canonical Contract：

```text
Domain None
→ SQL NULL
```

**回归测试**

四种 Outcome 均通过真实 PostgreSQL：

```text
FAILURE          PASS
TIMEOUT          PASS
CANCELLED        PASS
OUTCOME_UNKNOWN  PASS
```

并且直接 SQL：

```sql
output_artifact_ref IS NULL
```

为 True。

**LocalAgent 影响**

以后 LocalAgent 的执行结果映射到 AgentEvalOps 时，不能只定义 Python Object Contract，还要明确 JSON / SQL persistence semantics。

**面试表达**

> 这个问题让我意识到强类型 Domain Contract 还不够，跨越 ORM 和数据库边界时还要定义 canonical persistence representation。Python None、JSON null 和 SQL NULL 在业务语义上看起来一样，但数据库约束并不认为它们一样。

------

## Bad Case 2：Trigger 测试本身先挂了

**名称**

Immutable Result Trigger 测试未真正执行到 UPDATE。

**真实性**

真实性：假设构造，已通过测试覆盖。

**触发条件**

Migration Integration Test 使用 SQLAlchemy `text()`，SQL 中直接内嵌：

```text
{"timeout_seconds":1}
```

**错误表现**

其中：

```text
:1
```

被 SQLAlchemy 当成 bind parameter。

在真正：

```sql
UPDATE evaluation_results
```

之前测试就报错。

**风险**

如果只看到：

```text
migration test failed
```

容易误判为 Trigger 或 Migration 有问题。

更严重的是，如果用手工 probe 替代正式测试，正式 Gate 始终没有真正覆盖 trigger runtime behavior。

**根因**

Test Harness（测试支架）的 SQL parameter binding 写错，而不是生产 Migration 错。

**错误设计**

把 JSON literal 直接嵌入 SQLAlchemy `text()`。

**修复**

使用：

```text
named bind parameter
+
JSONB typed bind
```

**回归测试**

最终正式 Migration Test：

```text
INSERT valid Result
→ UPDATE Result
→ Trigger rejects
→ DBAPIError contains immutable
```

真实完成。

**LocalAgent 影响**

以后对 LocalAgent Trace/Version Schema 做 integration test 时，也要区分：

```text
产品失败
测试支架失败
环境失败
```

三者不能混在一起。

**面试表达**

> 我们有一次动态 Gate 看上去像 Migration 失败，但进一步定位发现 SQL 根本没执行到 Trigger，而是 SQLAlchemy 在解析测试 SQL 时把 JSON 里的冒号误认为 bind parameter。这个问题属于测试代码，不属于生产 Schema。我会先确认 failure layer，再决定修哪里，而不是看到 integration test 红了就去改数据库。

------

## Bad Case 3：数据库正确挡住了 Retry，但 Repository 仍然错

**名称**

Concurrent Retry loser 泄漏 raw `IntegrityError`。

**真实性**

真实性：假设构造，已通过测试覆盖。

**触发条件**

两个 transaction 同时：

```text
retry same source Attempt
```

**错误表现**

数据库结果实际上正确：

```text
success = 1
direct child = 1
```

但 loser 命中了：

```text
uq_evaluation_attempts_case_number
```

Repository 原来只识别：

```text
uq_evaluation_attempts_direct_retry
```

因此 raw `IntegrityError` 泄漏。

**风险**

Infrastructure vendor-specific error 泄漏到 Application。

上层无法依赖稳定的：

```text
RetryAlreadyCreated
```

合同。

**根因**

一条 retry candidate 同时满足两个唯一约束：

```text
case-number uniqueness
direct-retry uniqueness
```

PostgreSQL 没有义务保证永远先报告哪一个。

**错误设计**

假设：

```text
duplicate retry
→ PostgreSQL 一定报告 direct-retry constraint
```

**修复**

最终使用：

```text
SAVEPOINT
+
exact known constraint extraction
+
existing direct-child query
+
immutable retry intent verification
```

只有：

```text
known constraint
AND
same parent
AND
same immutable retry intent
```

才：

```text
RetryAlreadyCreated
```

否则 raw `IntegrityError` 继续上抛，fail closed。

**回归测试**

真实 PostgreSQL：

```text
2 retry commands
1 success
1 RetryAlreadyCreated
0 raw IntegrityError
1 direct child
```

同时 deterministic tests 覆盖：

```text
direct-UQ + match                → typed duplicate
case-number-UQ + match           → typed duplicate
case-number-UQ + mismatch        → raw error
direct-UQ + mismatch             → raw error
unknown UQ                       → raw error
```

**LocalAgent 影响**

以后 Tool/Runtime 等 Adapter 如果需要把 Redis/PostgreSQL/HTTP vendor error 转成稳定 Runtime Contract，也应该采用类似原则：

> 底层错误只是证据，不能简单等价成业务语义。

**面试表达**

> 这个问题比较有价值，因为数据库其实已经保证 exactly-one-child 了，但工程仍然不完整。我们还要求基础设施层对上暴露稳定的 typed error。最后我没有简单把两个 constraint name 都吞成 duplicate，而是用 SAVEPOINT 恢复 transaction，再查询 authoritative row 校验业务 intent，避免掩盖真正的数据损坏。

------

# 10. 测试与验证

## 10.1 最终 PostgreSQL 动态覆盖

Codex 最终独立 Combined Suite 验证：

| 能力                              | 结果 |
| --------------------------------- | ---- |
| Atomic Claim race                 | PASS |
| Wrong-token Fencing               | PASS |
| Stale vs Finalize race            | PASS |
| Duplicate Result race             | PASS |
| FAILURE SQL NULL                  | PASS |
| TIMEOUT SQL NULL                  | PASS |
| CANCELLED SQL NULL                | PASS |
| OUTCOME_UNKNOWN SQL NULL          | PASS |
| Concurrent Retry typed loser      | PASS |
| Retry exactly-one-child           | PASS |
| Retry constraint mapping variants | PASS |
| Retry false-positive protection   | PASS |
| SAVEPOINT recovery                | PASS |
| Retry preservation                | PASS |
| Tenant isolation                  | PASS |
| Cross-project DB FK               | PASS |
| Create Run rollback               | PASS |
| Immutable Result Trigger          | PASS |
| Empty → Head                      | PASS |
| Parent → Head                     | PASS |
| Head → Parent → Head              | PASS |
| Schema parity                     | PASS |
| Catalog Boolean semantics         | PASS |

最终：

```text
Persistence + Migration:
37 passed

Unit:
296 passed

P0 = 0
P1 = 0
P2 = 0
P3 = 1 deferred
```

## 10.2 为什么 SQLite / Mock 不够

WP3 的关键问题大量依赖 PostgreSQL 真实行为：

- partial unique index；
- composite FK；
- concurrent transaction；
- `UPDATE ... RETURNING`；
- trigger；
- JSONB；
- SQL NULL；
- constraint diagnostic；
- SAVEPOINT；
- Alembic round-trip。

所以：

```text
Unit PASS
```

只能证明业务代码基本正确。

不能证明：

```text
PostgreSQL concurrency contract
```

正确。

WP3 实际也证明了这一点：静态 Gate 已经 PASS 后，真实 PostgreSQL 仍发现了 JSONB null 和 Retry constraint mapping 两个生产级问题。

------

# 11. 抽取出的通用工程原则

## 11.1 Single Owner per Concept

Run、Attempt、Result 不要同时拥有同一状态。

```text
Run
→ overall evaluation lifecycle

Attempt
→ execution lifecycle

Result
→ evaluator judgment
```

职责必须单一。

------

## 11.2 数据库负责并发事实，Application 负责业务语义

不要试图用 Python：

```text
if not exists
```

解决数据库 concurrency。

正确模式是：

```text
Application:
决定“允许 retry”

Database:
保证“只能产生一个 retry”

Repository:
把数据库结果翻译为稳定 typed contract
```

------

## 11.3 At-least-once 不等于业务执行 Exactly-once

即使未来 Celery 重复投递：

基础设施只能做到：

```text
at-least-once delivery
```

业务层仍需通过：

- idempotency key；
- unique constraint；
- claim；
- fencing；
- immutable result；

把副作用控制到业务允许范围。

------

## 11.4 Retry 应创建新事实，而不是改写历史

```text
Attempt #1 FAILURE
Attempt #2 SUCCESS
```

比：

```text
Attempt #1 FAILURE
↓ reset
Attempt #1 SUCCESS
```

更适合：

-审计；
-调试；
-评测复现；
-回归分析。

------

## 11.5 Fencing Token 解决“旧执行者回来晚了”

Cancel/timeout/worker crash 后，旧 worker 可能仍然回来写结果。

所以：

```text
“我曾经拥有任务”
```

不等于：

```text
“我现在仍然有权写任务”
```

Fencing Token 就是用来证明第二件事。

------

## 11.6 Result 要 Append-only

评测平台最怕：

```text
昨天 baseline Result
今天被 UPDATE
```

这样回归结论无法解释。

所以 EvaluationResult：

```text
insert
never update
```

并且不仅 Application 不提供 update，数据库 Trigger 也阻止直接 UPDATE。

------

## 11.7 Fail Closed 比“尽量猜对”更重要

Concurrent Retry 中：

```text
case-number constraint
```

“可能”意味着 duplicate retry。

但可能 ≠ 能证明。

因此必须再验证 authoritative child。

不能证明就：

```text
IntegrityError
```

而不是假装：

```text
RetryAlreadyCreated
```

------

# 12. LocalAgent 映射

这一节一定要强调：

> **下面是架构映射，不是当前已经完成的 LocalAgent Integration。**

当前 WP3 已经提供：

```text
Run
Attempt
Result
ExecutionTargetRef
VersionRef
EvidenceRef
```

未来 LocalAgent 接入后，大致关系会是：

```text
AgentEvalOps EvaluationRun
        │
        ├─ Dataset / Suite
        │
        └─ ExecutionAttempt
                 │
                 ▼
          LocalAgent ExecutionTarget
                 │
                 ▼
           ExecutionOutcome
                 │
                 ▼
          EvaluationResult
```

LocalAgent 的：

```text
RunContext
RuntimeEvent
AgentState
ToolResult
RetrievalResult
Trace
VersionFingerprint
```

当前都没有被 WP3 固化进核心表。

它们继续通过：

```text
opaque VersionRef
EvidenceRef
adapter payload
```

留出兼容缝隙。

具体 LocalAgent mapping 仍要等 Stage 3.5 Contract Freeze。

这正是前面 H2 的：

> Stable algebra, open payload。

------

# 13. 对 AgentEvalOps 后续闭环的意义

Stage4-Phase2 当前链路已经变成：

```text
WP1
Dataset / TestCase / Suite / Evaluator Domain
            ↓
WP2
ExecutionTarget / ExecutionOutcome
            ↓
WP3
Run / Attempt / Result Persistence
            ↓
WP4
Minimal Evaluation Loop
```

WP3 解决的是：

> “Evaluation Loop 一旦开始跑，状态和事实放在哪里，谁拥有它们，并发执行时怎么保证可信。”

所以 WP4 才可以专注：

```text
Case
→ ExecutionTarget
→ ExecutionOutcome
→ Evaluator
→ EvaluationResult
→ Run completion
```

而不用再重新解决 persistence correctness。

注意：

**WP4 尚未开始实现。** 最终 Closure Review 只确认 `Ready for Stage4-Phase2-WP4 planning: YES`。

------

# 14. 面试答案与高频追问

## 14.1 一分钟回答

> 我在 AgentEvalOps 离线评测里做过一套 Run、Attempt 和 Result 的持久化与并发控制。核心不是 CRUD，而是解决多个 Worker 同时 claim、retry、finalize 时的数据一致性。
>
> 我把 Run、Attempt、Result 的 owner 分开，Claim 用 PostgreSQL 条件 UPDATE + RETURNING 做原子竞争，再用 claim token 做 fencing；Retry 不重置旧 Attempt，而是创建新 Attempt，用唯一约束保证同一 source 最多一个 direct child；EvaluationResult 做 append-only，数据库 Trigger 直接禁止 UPDATE。
>
> 动态测试时还发现了两个比较典型的数据库边界问题，一个是 Python None 写 JSONB 时默认变成 JSON null，和 SQL NULL CHECK 不一致；另一个是并发 Retry 同时违反两个 unique constraint，数据库不保证报告哪一个，所以最后我在 Repository 里用 SAVEPOINT 恢复 transaction，再查询 existing child 校验 retry intent，确认真的是 duplicate retry 才转换成 RetryAlreadyCreated。
>
> 最终在 PostgreSQL 16 上并发、租户隔离、Retry、Trigger、Migration round-trip 等 37 个 WP3 Integration Case 全部通过。

------

## 14.2 三分钟回答

> AgentEvalOps 的离线评测不是简单地“执行 Case 然后存个分数”。我在设计 WP3 时首先把 EvaluationRun、ExecutionAttempt 和 EvaluationResult 拆成三个不同 owner。Run 表示一次整体评测任务，Attempt 表示某个 Case 的一次真实执行，Result 则表示某个 Evaluator 对这个 Attempt 输出的判断，所以执行成功和评测通过本身就是两个概念。
>
> 并发方面，我尽量让数据库成为最终 arbiter。比如 claim 不做 SELECT 后再 UPDATE，而是一条带 PENDING 和 token 条件的 UPDATE RETURNING，保证只有一个 worker 能 claim。Claim 后还要带 token 做 fencing，防止失去所有权的旧 worker 后来又写 outcome。Result 有 logical unique slot，而且采用 append-only，数据库还装了 BEFORE UPDATE Trigger，防止绕过 Application 改历史评测结果。
>
> Retry 是这个阶段最复杂的点。我没有把失败 Attempt reset 成 PENDING，而是创建新的 Attempt，旧 Attempt 保留 immutable history。数据库同时有 case-attempt-number unique 和 direct-retry partial unique 两个约束。动态并发测试时发现，同一次 concurrent retry 的 loser 有时不是命中 direct-retry constraint，而是 case-number constraint。数据库实际上已经正确保证只有一个 child，但 Repository 泄漏了 raw IntegrityError。
>
> 最后没有简单把两个约束名都映射成 RetryAlreadyCreated，因为 case-number collision 单独看并不能证明一定是同一个 parent。我的处理是在 create_retry 的 insert/flush 周围开 SAVEPOINT，冲突时只回滚 savepoint，再查询同 tenant、run、parent 下的 existing child，比较 case、attempt number、target、idempotency key、request snapshot 等 immutable retry intent。只有完全一致才映射成 RetryAlreadyCreated，否则继续抛原 IntegrityError，保持 fail closed。
>
> 另外动态测试还抓到过 Python None 在 JSONB 默认会绑定成 JSON null，而我们的数据库 CHECK 要求 SQL NULL，这类问题 Mock 和 SQLite 很难发现。最后使用 PostgreSQL 16 跑了 claim race、fencing、stale race、duplicate result、retry race、tenant composite FK、transaction rollback、trigger、Alembic round-trip 等动态测试，WP3 combined suite 37 passed，Unit 296 passed，最终 P0/P1/P2 都是 0。

------

# 15. 五个高频追问

## Q1：为什么 Claim 不使用 SELECT FOR UPDATE？

核心回答：

> Claim 是一个非常明确的条件状态转换，我更倾向单条 conditional UPDATE + RETURNING，让 PostgreSQL 在一条 statement 内完成判断和写入。SELECT FOR UPDATE 当然也能实现，但会引入显式锁定和 read-then-update 两步，对于这种“谁抢到 PENDING 谁成为 owner”的场景，CAS 风格的 UPDATE 更简单。

------

## Q2：有 Unique Constraint 了，为什么还要 SAVEPOINT？

> Unique Constraint 只能保证数据库最终没有两个 child，它不能保证 Repository 能给上层稳定的业务错误。并发 retry 时一条 candidate 同时违反两个 unique invariant，PostgreSQL 可能报告不同 constraint。另一方面 flush 抛 IntegrityError 后 transaction 已经失败，不能直接继续 SELECT，所以我用 SAVEPOINT 只包 retry insert。局部 rollback 后 outer UoW 仍可查询 authoritative child，从而做安全的 typed error translation。

------

## Q3：为什么不直接把 case-number unique violation 当 RetryAlreadyCreated？

> 因为 case-number conflict 只能证明相同 case/version/attempt_no 已经被占用，不能证明 existing row 的 retry parent 就是当前 source。如果数据库里存在历史坏数据或另一条 lineage，一律映射会掩盖 corruption。所以必须再验证 existing direct child 和 immutable retry intent。

------

## Q4：为什么 EvaluationResult 不能 UPDATE？

> 评测系统后面要做 Baseline/Candidate 和 Regression。如果历史 Result 可以被覆盖，昨天的 baseline 和今天看到的 baseline 可能已经不是同一份事实，结果就不可复现。所以 Result 是 append-only，修改只能产生新 Result 或 superseding relation，而不是直接 UPDATE；WP3 甚至在 PostgreSQL Trigger 层阻止 UPDATE。

------

## Q5：Execution SUCCESS 和 Evaluation PASS 为什么要分开？

> Execution SUCCESS 只说明目标系统成功完成请求并返回了结果，它不说明结果正确。比如模型正常返回“北京是美国首都”，Execution 仍是 SUCCESS，但 Evaluator 应该给 FAIL。把这两个状态混在一起，会导致运行故障和质量问题无法区分。

------

# 16. 与相似概念的区别

### Idempotency（幂等） vs Uniqueness（唯一性）

```text
Idempotency
→ 多次同一业务请求应该产生稳定业务效果

Uniqueness
→ DB 不允许两行违反某个唯一约束
```

Unique Constraint 是实现幂等的一种工具，不等于幂等本身。

### Retry vs Reset

```text
Reset:
覆盖旧事实

Retry:
创建新 Attempt
保留旧事实
```

评测、审计、恢复系统更适合第二种。

### Optimistic CAS vs Fencing

```text
CAS:
决定当前谁能获得状态转换

Fencing:
决定获得过权限的人现在是否仍然有权写
```

两者解决不同阶段的问题。

### SAVEPOINT vs Unit of Work

```text
UoW:
业务事务整体边界

SAVEPOINT:
一个事务内部的局部恢复点
```

WP3 的 Repository SAVEPOINT 没有取代 UoW。

### Outcome vs Verdict

```text
ExecutionOutcome:
系统有没有正常执行

EvaluationVerdict:
执行结果质量好不好
```

这是 Agent Evaluation 系统里非常值得主动讲出的区别。

------

# 17. 当前最应该记住的面试主线

如果只记一条主线，不要背 37 个测试名称，记：

> **WP3 的核心不是 PostgreSQL CRUD，而是“事实所有权 + 并发仲裁 + 不可变历史”。**

然后围绕三个关键设计展开：

```text
Claim
→ CAS + Fencing

Retry
→ New Attempt + DB uniqueness + SAVEPOINT typed mapping

Result
→ Logical uniqueness + Append-only + Trigger
```

最后用三个真实测试阶段发现的问题证明你不是只做了纸面架构：

```text
Python None
≠ JSON null
≠ SQL NULL

多个 Unique Constraint
≠ PostgreSQL 固定返回某一个

Integration Test 失败
≠ 一定是 Production 失败
```

这三点是整个 WP3 最有区分度的工程学习成果。最终 Closure Gate 已确认 WP3 正式 PASS，但 **WP4 尚未实施**。