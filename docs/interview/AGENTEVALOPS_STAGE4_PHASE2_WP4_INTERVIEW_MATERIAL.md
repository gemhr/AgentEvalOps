# 推荐面试材料命名

```
AGENTEVALOPS_STAGE4_PHASE2_WP4_INTERVIEW_MATERIAL.md
```

# 1. 一句话项目定义

在 AgentEvalOps 的离线评测核心中，我实现了一个 **Attempt-oriented Minimal Evaluation Loop（以执行尝试为粒度的最小评测循环）**，用 `EvaluationLoopService` 把此前已经独立完成的 Dataset/Suite/Evaluator Domain、ExecutionTarget 和 Run/Attempt/Result Persistence 串成真正可运行的闭环：

**Attempt → ExecutionTarget → ExecutionOutcome → Evaluator → EvaluationResult → Run Completion**

同时保持数据库短事务、Target/Evaluator 外部调用与持久化事务隔离、多 Evaluator、幂等重入、Result append-only（只追加）以及 Runtime-neutral（运行时无关）边界。

最终 WP4 Project Gate 正式 PASS，真实 PostgreSQL integration 为 **10 passed**，WP3+WP4 combined 为 **33 passed**，Full Unit 为 **338 passed**，P0/P1/P2/P3 均为 0。

------

# 2. 真实性与完成边界

## 2.1 已真实实现

WP4 已真实实现：

- `EvaluationLoopService`；
- Attempt 粒度的最小评测编排；
- `ExecutionTargetResolver` application seam；
- `EvaluatorResolver` application seam；
- 多 Evaluator 顺序执行；
- `EvaluationInput` 统一组装；
- `EvaluationResultDraft -> EvaluationResult` 统一组装；
- EvaluationPolicy（评测策略）的部分消费；
- Target/Evaluator preflight（预检查）；
- Target version provenance（来源）校验；
- terminal SUCCESS re-entry；
- duplicate Result convergence；
- typed loop result：
  - `PROGRESSED`
  - `NOT_CLAIMED`
  - `IN_PROGRESS`
  - `ALREADY_COMPLETE`
  - `RUN_NOT_READY`
- Persistence query facade：
  - `get_run`
  - `get_attempt`
  - `list_attempts`
  - `list_results`

Loop 不直接持有 Repository、ORM 或 `AsyncSession`，所有 WP3 fact 仍经 `EvaluationPersistenceService` 读写。

## 2.2 已真实测试

最终真实验证包括：

- PostgreSQL 16；
- Redis；
- full success；
- `SUCCESS != PASS`；
- multi-evaluator；
- existing result slot skip；
- FAILURE / TIMEOUT / CANCELLED / OUTCOME_UNKNOWN；
- claim loser；
- duplicate delivery；
- terminal SUCCESS re-entry；
- duplicate Result race；
- cross-project fail closed；
- Target/Evaluator 执行期间无 open persistence UoW；
- Target 同时包含非空 capabilities、config、version，并实际通过 CapabilityRequirement 的完整 PostgreSQL 链路。

最终：

- WP4 PostgreSQL：**10 passed**
- WP3 + WP4：**33 passed**
- Full Unit：**338 passed**
- Ruff / Lock / Diff / Alembic：PASS。

## 2.3 明确未实现

以下不能在面试里说成已经完成：

- DB-only durable resume；
- Production Catalog Loader；
- Production Target Registry；
- Production Evaluator Registry；
- production composition wiring；
- concrete production evaluator；
- HTTP / Replay / LocalAgent ExecutionTarget；
- Celery dispatch；
- API / UI；
- automatic retry；
- heartbeat；
- durable cancellation；
  -完整 timeout enforcement；
- Trace/Evidence store；
- Regression / Baseline / Candidate；
- Release Gate。

这些仍是 WP4 的明确边界。

------

# 3. WP4 解决的核心问题

WP1、WP2、WP3 完成后，其实还只有几个“零件”：

- WP1：什么是 TestCase、Suite、Evaluator、Result；
- WP2：什么是 ExecutionTarget、ExecutionRequest、ExecutionOutcome；
- WP3：Run、Attempt、Result 如何安全持久化。

但还缺：

> **一次真正的 Evaluation 到底由谁按照什么顺序把这些能力串起来？**

如果没有 WP4，系统还是：

```text
Domain        有了
Execution     有了
Persistence   有了

但：

谁执行？
谁组装 Input？
谁调用 Evaluator？
谁组装 Result？
什么时候 finish Run？
失败后做什么？
重入后做什么？
```

都没有 application owner。

所以 WP4 的本质不是“再加一个 Service”，而是建立：

> **Evaluation Application Control Flow（评测应用控制流）**

------

# 4. 为什么选择 Attempt-oriented，而不是 Run-oriented

最终没有实现：

```
execute_run(run_id)
```

而是：

```
execute_attempt(project_id, attempt_id, ...)
```

这是 WP4 最重要的架构取舍之一。

原因有三个。

## 第一，WP3 的并发所有权就是 Attempt

Atomic Claim（原子认领）和 Fencing（栅栏）保护的都是：

```
attempt_id
```

因此 Application Loop 与它保持同一粒度最自然。

## 第二，一个 Attempt 对应一次真实执行

典型关系：

```text
Attempt
   ↓
one ExecutionTarget call
   ↓
one ExecutionOutcome
   ↓
N Evaluators
   ↓
N Results
```

所以 Attempt 是执行和评测之间的天然聚合点。

## 第三，避免让 WP4 提前变成调度器

如果实现 Run-oriented：

```text
execute_run()
→ 遍历所有 Case
→ 调度所有 Attempt
→ 管并发
→ 管 Retry
→ 管 Run completion
```

就会开始侵入 Scheduler / Celery / Durable Execution 的职责。

Attempt-oriented 则能让未来 Celery 只做：

> dispatch `project_id + attempt_id`

而真正的生命周期仍由 Application + WP3 持有。

------

# 5. 核心架构

整体可以记成：

```text
Caller / Future Dispatcher
          │
          ▼
  EvaluationLoopService
          │
          ├── preflight
          │
          ├── claim/start
          │
          ▼
   ExecutionTarget
          │
          ▼
   ExecutionOutcome
          │
          ▼
EvaluationPersistenceService
          │
          ▼
      PostgreSQL
          │
          ▼
      Evaluator(s)
          │
          ▼
EvaluationResultDraft
          │
          ▼
EvaluationLoopService
   assemble Result
          │
          ▼
finalize_result()
          │
          ▼
     finish_run()
```

唯一的 orchestrator（编排器）是：

**EvaluationLoopService**

它不成为：

- Repository owner；
- transaction owner；
- Worker owner；
- Retry owner；
- Run Scheduler。

------

# 6. Preflight：为什么执行前要做大量校验

WP4 有一个很重要的原则：

> **凡是可以在副作用发生前确定的错误，都应该在 claim / Target call 前 fail closed。**

所以执行之前会校验：

- Project / Run / Attempt relationship；
- TestCase id/version；
- caller input 与 persisted ExecutionRequest input；
- Target snapshot；
- Target version；
- Target resolver；
- Target identity；
- Evaluator specs；
- Evaluator resolver；
- Evaluator identity。

原因很简单。

假如先：

```text
claim
start
调用模型
```

之后才发现：

```text
Evaluator version 配错了
```

系统就留下一个已经执行过外部副作用，但无法正确评测的 Attempt。

所以合理顺序是：

```text
能提前发现的 configuration / contract bug
        ↓
全部先检查
        ↓
再进入有副作用阶段
```

这也是 Fail Fast（快速失败）和 Fail Closed 的结合。

------

# 7. 为什么 TestCase 由 Caller 提供

WP4 没有实现 DB-only durable resume。

原因不是忘了做，而是当前 WP3 snapshot 里没有完整 TestCase：

数据库能恢复：

- case id/version；
- input；

但不能完整恢复：

- expected output；
- assertion specs；
- fixture refs；
- evidence refs。

H-2 因此明确选择：

> caller 提供完整 immutable `TestCaseVersion`。

Loop 只验证：

- case id/version；
- input payload；

与数据库事实一致。

这是一个非常典型的工程边界：

> **当前信息不足时，不为了“看起来支持恢复”而编造一个假的 durable resume。**

面试时可以明确说：

> 当前 WP4 支持 persistence-safe re-entry，但不是 DB-only durable resume，这两个不是一回事。

------

# 8. ExecutionTarget Resolver 与 Evaluator Resolver

WP2 已有：

```
ExecutionTarget
```

WP1 已有：

```
Evaluator
```

但问题是：

> 有 Interface 不代表 Application 知道该用哪个实现。

因此 WP4 新增两个 Runtime-neutral seam：

```text
ExecutionTargetRef
        ↓
ExecutionTargetResolver
        ↓
ExecutionTarget
```

以及：

```text
EvaluatorSpec
      ↓
EvaluatorResolver
      ↓
ResolvedEvaluator
```

但 WP4 **没有实现 Production Registry**。

这是刻意的。

因为现在如果直接引入：

- LocalAgent；
- HTTP；
- Replay；
- LLM Judge；
- Celery plugin；

WP4 就会从 application loop 变成 infrastructure integration。

所以当前只冻结：

> “Application 怎么拿到 executable implementation”

而不冻结：

> “Production registry 内部怎么管理所有 adapter”。

------

# 9. 为什么 Target Resolver 要前后两层校验

Target 的完整 authority 来自 persisted snapshot。

Resolver 返回 executable Target 后：

必须确认：

```text
resolved Target ref
==
authoritative Target ref
```

包括：

- id；
- kind；
- version；
- config；
- capabilities。

为什么？

因为：

```text
我要运行 target-A@v2
```

Resolver 如果错误返回：

```text
target-A@v1
```

模型可能仍然能执行成功。

但结果 provenance 已经错了。

这种错误比普通执行失败危险，因为：

> **系统会生成一份“看似成功但归属错版本”的评测结果。**

因此 provenance mismatch 是 contract error，不是 Execution FAILURE。

------

# 10. Target Exception 为什么不能自动转 FAILURE

这是 WP4 很值得面试讲的地方。

假设：

```python
await target.execute(...)
```

直接抛异常。

最简单的写法是：

```text
except Exception:
    outcome = FAILURE
```

WP4 明确没有这样做。

因为异常发生时，我们不知道：

- 请求有没有发出去；
  -远端有没有执行；
  -有没有发生副作用；
  -是否只是本地响应读取失败。

所以：

```text
Python exception
≠
confirmed execution FAILURE
```

WP4 的规则是：

### Adapter 能确认事实

返回：

- FAILURE；
- TIMEOUT；
- CANCELLED；
- OUTCOME_UNKNOWN。

### Adapter exception 直接逃逸

Loop 不猜。

结果：

- 原异常传播；
- Attempt 保持 RUNNING；
  -后续由 explicit stale reconciliation 处理。

这体现一个很重要的原则：

> **不要把“观察失败”误写成“业务失败”。**

------

# 11. 为什么 Loop 不自己做 asyncio.timeout

WP4 已有：

```
ExecutionRequest.timeout
```

但 Loop 没使用：

- `asyncio.timeout`
- `asyncio.wait_for`

去强制终止 Target。

原因：

对于远程系统：

```text
coroutine timeout
```

只能证明：

> 本地没有继续等待。

不能证明：

> 远端任务已经停止。

如果执行带副作用 Tool：

本地 timeout 后远端可能还在执行。

因此 Timeout 由具体 Adapter 负责分类：

```text
确定终止
→ TIMEOUT

不知道是否终止
→ OUTCOME_UNKNOWN
```

这和 LocalAgent Runtime 中的 Cancellation/Fencing 思路其实是相通的。

------

# 12. Multi-evaluator 设计

WP4 首版直接支持：

**1 Attempt → N Evaluators**

而不是为了 MVP 先限制成一个 Evaluator。

原因是 WP3 persistence 已经冻结：

```text
(run,
 attempt,
 case,
 evaluator_id,
 evaluator_version)
```

作为 Result logical slot。

如果 WP4 只支持一个 Evaluator，反而会人为削弱已有 Domain Contract。

执行流程：

```text
Evaluator A
→ Result A commit

Evaluator B
→ Result B commit

Evaluator C
→ Result C commit
```

每个 Result 独立 transaction。

因此如果：

```text
A success
B success
C crash
```

A/B 不 rollback。

下一次 re-entry：

```text
skip A
skip B
only execute C
```

------

# 13. EvaluationPolicy 如何使用

WP4 没有把整个 EvaluationPolicy 都消费掉。

当前只消费：

- `evaluator_error`
- `evaluator_inconclusive`

不消费：

- `required_result_missing`

所以这是：

**PARTIAL consumption**

### Evaluator 正常返回 ERROR

根据：

```
evaluator_error
```

归一化为：

- FAIL
- INCONCLUSIVE

### Evaluator 抛普通 Exception

Application 构造 sanitized synthetic ERROR draft，然后套用同样 policy。

### Evaluator 返回 INCONCLUSIVE

根据：

```
evaluator_inconclusive
```

映射。

### PASS / FAIL

保持原值。

但：

```
required_result_missing
```

仍由 WP3 的 `finish_run()` 通过 slot completeness 处理，不由 WP4制造假 Result。

------

# 14. 为什么 Evaluator Exception 可以归一化，而 Target Exception 不可以

这是很容易被面试官追问的地方。

看起来两边都：

```text
except Exception
```

为什么一个能转 Result，一个不能？

区别在于：

### Target Exception

关系到外部执行事实：

```text
任务究竟执行没执行？
有没有副作用？
```

不知道。

所以不能猜。

### Evaluator Exception

Target SUCCESS 已经是 persisted fact。

Evaluator 只是：

> 对已存在结果做质量判断。

所以 evaluator 自己失败并不会改变原 execution truth。

EvaluationPolicy 已经显式定义：

> evaluator error 应该如何作为评测事实处理。

因此可以合法转为：

- FAIL；
- INCONCLUSIVE。

这是：

> **Execution Truth 与 Evaluation Judgment 的边界。**

------

# 15. SUCCESS != PASS

这应该是 WP4 面试中必讲的一点。

真实系统中：

```text
ExecutionOutcome.SUCCESS
```

只代表：

> Target 成功完成执行并返回 artifact。

完全不意味着：

```text
EvaluationVerdict.PASS
```

例如：

```text
模型成功返回答案：
“北京是美国首都”
```

那么：

```text
ExecutionOutcome = SUCCESS
EvaluationVerdict = FAIL
```

仍然成立。

WP4 的真实 unit/integration 已覆盖：

```text
Target SUCCESS
Evaluator FAIL
Run COMPLETED
```

因为 Run 的 infrastructure lifecycle 表示：

> 执行结束 + required Result slots齐全

而不是：

> 所有评测指标都通过。

这一点为后面的 Regression / Release Gate 留出了清晰边界。

------

# 16. Run Completion 为什么继续归 WP3

WP4 没有自己定义：

```text
if results good:
    run.status = COMPLETED
```

而是继续调用：

`EvaluationPersistenceService.finish_run()`。

Run lifecycle owner 仍是 WP3。

WP4 只是选择：

> 什么时候尝试 finish。

时机：

### Non-success

```text
record outcome
→ finish once
```

### SUCCESS

```text
所有 missing evaluator slots处理完
→ finish once
```

### terminal re-entry

```text
补完缺失 slot
→ finish once
```

而：

```
RunNotFinishable
```

也不能直接全部吞成 `RUN_NOT_READY`。

必须重新读 authoritative state，确认真的是：

- 其他 Attempt仍 active；
- required slot仍缺；

才把它当正常 control result。

这防止：

> 把真正的数据库竞态或状态损坏误认为“只是还没结束”。

------

# 17. 为什么 ResultAlreadyFinalized 不能直接吞

并发 terminal re-entry 时：

```text
Caller A ─┐
          ├→ Evaluator B
Caller B ─┘
```

两边都可能执行 evaluator。

最终 DB 只能有一个 Result slot。

loser 会得到：

```
ResultAlreadyFinalized
```

最简单做法：

```text
except ResultAlreadyFinalized:
    pass
```

WP4 没这么做。

而是：

```text
ResultAlreadyFinalized
        ↓
authoritative reread
        ↓
exact logical slot exists?
        ↓
provenance identical?
        ↓
YES → benign convergence
NO  → propagate
```

为什么？

因为：

> “这个 slot 有数据”

和

> “这个 slot 已经有我预期的那份权威数据”

是两个不同概念。

------

# 18. Transaction Boundary

最终事务边界：

```text
Q1
读取 Run / Attempt / Results
commit / close

preflight
resolver
无 DB transaction

TX1
claim
commit

TX2
start
commit

ExecutionTarget.execute()
无 DB transaction

TX3
record outcome
commit

Evaluator.evaluate()
无 DB transaction

TX4
finalize Result
commit

TX5
finish Run
commit
```

这种设计避免：

```text
BEGIN TRANSACTION
    ↓
等 LLM 30 秒
    ↓
等 HTTP
    ↓
Evaluator
    ↓
COMMIT
```

否则会带来：

- 长事务；
  -连接池占用；
  -锁持有时间过长；
- deadlock 风险；
  -数据库 version cleanup 压力；
  -外部失败与 DB rollback 耦合。

真实 PostgreSQL tests 还用 task-local `ContextVar` instrumentation 验证 Target/Evaluator 执行时当前 task 的 active UoW 为 0。

------

# 19. Bad Case 1：合法 Target 被 preflight 错误拒绝

## 真实性

**源码审查 + 定向探针真实发现，后续真实 PostgreSQL 回归覆盖。**

不是生产事故，也不是纯假设。

## 触发条件

完整 authoritative Target：

```text
capabilities = ("TEXT",)
config_ref = config-v1
```

但 WP3 有两个合法 persistence projection：

### Run

```text
capabilities = ("TEXT",)
config_ref = None
```

### Attempt

```text
capabilities = ()
config_ref = config-v1
```

原 WP4 却做：

```text
authoritative == run_ref == attempt_ref
```

## 表现

合法 Target 在 claim 前被：

```
EvaluationLoopContractError
```

误拒绝。

H-4 因此即使当时：

- PostgreSQL 9 passed；
- combined 32 passed；
- unit 327 passed；

仍然把 Project Gate 打回 FAIL。

## 根因

混淆了：

**同一个 Concept**

和：

**同一种 Projection**

Run / Attempt / authoritative snapshot 描述的是同一个 Target。

但它们不是同构对象。

------

# 20. Bad Case 1 的修复原则

不能简单：

> 那就别校验了。

也不能：

> 把 Repository 改成三个对象全都存完整 Target。

因为 Repository 的 projection 本来就是合理设计。

最终修复：

### Authoritative full Target

完整校验：

- id
- kind
- version
- config
- capabilities

### Run persisted view

只校验它拥有的：

- id
- kind
- version
- capabilities

并确认：

```
config_ref is None
```

### Attempt persisted view

只校验：

- id
- kind
- version
- config

并确认：

```
capabilities == ()
```

### Resolved Target

仍必须：

```text
resolved_target
==
authoritative full target
```

完整 equality。

所以修复不是：

**放宽验证**

而是：

> **让验证尊重 Projection Ownership（投影所有权）。**

最终 PostgreSQL 新增真实场景同时包含：

- capability；
- config；
- version；
- Suite CapabilityRequirement；

完整闭环 PASS。

------

# 21. Bad Case 2：并发事务探针本身误报

## 真实性

**实施测试中真实发现的 Test Instrumentation Bug（测试插桩问题）。**

不是 Production Bug。

## 场景

为了验证：

> Target/Evaluator 执行时没有 open UoW

测试最初使用 process-global counter。

两个 concurrent caller：

```text
Task A → Target.execute
Task B → 进入短 DB query
```

此时 global counter：

```text
> 0
```

测试会认为：

> Task A 的 Target 正在 DB transaction 中。

但实际上这个 UoW 属于 Task B。

## 根因

测量的是：

> 整个进程有没有 UoW

但想验证的是：

> 当前 coroutine 有没有 UoW。

## 修复

改为：

```
ContextVar
```

按 asyncio task 隔离。

这样能够检测：

- 当前 task；
- nested UoW；

又不会把其他 concurrent task 的 transaction错误归属于当前 Target。

H-4 独立复核认为该分类可信，最终：

`FLAKY_DYNAMIC_FAILURE_OBSERVED: NO`。

这个案例很适合面试，因为它体现：

> **测试探针本身也有并发语义。**

------

# 22. Bad Case 3：Target exception 不应该“好心”转 FAILURE

## 真实性

**假设构造，已由测试覆盖。**

## 错误实现

```python
try:
    await target.execute(...)
except Exception:
    record_outcome(FAILURE)
```

## 风险

假设远程 Tool 已经执行成功，但是本地连接在读取 response 时断开。

你写：

```
FAILURE
```

实际上是在创造错误事实。

## 正确行为

escaped exception：

- propagate；
  -不 record fabricated Outcome；
- Attempt 保持 RUNNING；
  -由 stale reconciliation 后续处理。

核心原则：

> **Unknown is a first-class state（未知是一等状态），不要把未知压缩成失败。**

------

# 23. Bad Case 4：Concurrent resume 重复 Evaluator

## 真实性

**Known Limitation，真实并发测试覆盖 Result convergence，但没有实现 exactly-once Evaluator call。**

两个 resume caller可能：

```text
A → Evaluator
B → Evaluator
```

两个外部 evaluation call 都发生。

但 PostgreSQL保证：

```text
Result slot rows = 1
```

WP4 做到的是：

> persistence exactly-one fact

而不是：

> external evaluator exactly-once execution。

这是当前明确接受的边界。

面试不能说：

> 我们保证 Evaluator exactly once。

应该说：

> 当前可以保证 append-only Result logical slot 唯一和并发收敛，但 evaluator external call 在 concurrent terminal resume 下仍可能重复。

------

# 24. 为什么没有复用 PandaProbe Legacy Celery Evaluation

旧 PandaProbe 本身已经有 Celery Evaluation Flow。

直接改旧 Task 看起来很省事。

但旧架构是：

```text
Celery task
→ own EvalRun lifecycle
→ mutable Score
→ reset/delete on redelivery
→ Trace-centric identity
```

而新架构要求：

```text
Application/Domain
→ owns lifecycle

Celery
→ future dispatcher only

Result
→ append-only

Evaluation
→ Trace optional
```

如果直接复用旧 Celery lifecycle，就会把 WP3 刚建立的：

- Claim；
- Fencing；
- Run owner；
- Result immutable；

重新破坏掉。

所以 WP4 明确没有改 Celery。

------

# 25. WP4 与 WP3 的关系

可以记一句：

> **WP3 解决“事实如何安全存在”，WP4 解决“这些事实如何被应用流程正确推进”。**

WP3：

```text
Run
Attempt
Result
Claim
Fencing
Retry
Persistence
```

WP4：

```text
什么时候 claim
什么时候调用 Target
什么时候调用 Evaluator
如何生成 Result
如何 re-entry
什么时候 finish
```

WP4 没有重新实现 WP3 的状态机，而是消费它。

这就是 Application Layer（应用层）正确的职责。

------

# 26. 通用工程原则

## 26.1 Orchestrator 不等于 Owner Everything

`EvaluationLoopService` 是 control flow owner。

但它不是：

- Run state owner；
- DB transaction owner；
- retry owner；
- target implementation owner。

“负责调用顺序”和“拥有所有状态”是两件事。

------

## 26.2 Same Entity ≠ Same Projection

这是本 WP 最重要的新知识点之一。

比如：

```text
UserFullProfile
UserListProjection
UserAuthProjection
```

都描述 User。

但不能：

```text
full_profile == auth_projection
```

来判断数据一致。

正确做法是：

> 根据每个 projection 的字段 ownership 验证。

------

## 26.3 Error Classification 要基于事实所有权

Target Adapter负责：

> 外部执行事实。

Evaluator负责：

> 质量判断。

Persistence负责：

> 数据事实。

Application负责：

> 控制流。

所以：

```text
Target exception
```

Application无法证明 execution failure。

而：

```text
Evaluator exception
```

Application有 EvaluationPolicy 可定义如何记录。

------

## 26.4 Re-entry 不等于 Replay

重入时不能默认：

> 再执行一次。

正确：

```text
PENDING
→ claim

CLAIMED/RUNNING
→ IN_PROGRESS

TERMINAL SUCCESS
→ fill missing Results

TERMINAL failure
→ finish

Run terminal
→ ALREADY_COMPLETE
```

这是状态驱动的 resume。

------

## 26.5 Append-only 能显著降低恢复复杂度

已有 Result：

```text
不要改
不要删
不要 rollback
```

只补 missing slot。

这样并发和恢复的 reasoning 会简单很多。

------

# 27. 测试与验收

最终 WP4 Gate：

| 项目                       | 最终结果   |
| -------------------------- | ---------- |
| WP4 PostgreSQL Integration | 10 passed  |
| WP3 + WP4 Combined         | 33 passed  |
| Full Unit                  | 338 passed |
| Ruff                       | PASS       |
| uv lock                    | PASS       |
| diff check                 | PASS       |
| Alembic head               | PASS       |
| P0                         | 0          |
| P1                         | 0          |
| P2                         | 0          |
| P3                         | 0          |
| Flaky Dynamic Failure      | NO         |

而且第一次 H-4 **没有因为 9 个 PostgreSQL Case 全 PASS 就放行**，仍通过源码审查发现了合法 Target projection 的 coverage blind spot。

这是一个很好的工程验收案例：

> **测试全绿只是证据之一，不等于正确性证明完成。**

------

# 28. LocalAgent 映射

这一节仍然只是**未来架构映射，不是已经接入**。

未来 LocalAgent 可以作为：

```
ExecutionTarget
```

比如：

```text
EvaluationLoop
      ↓
ExecutionTargetResolver
      ↓
LocalAgentExecutionTarget
      ↓
LocalAgent Run
      ↓
Artifact / Evidence
      ↓
Evaluator
```

WP4 已经确保：

- Loop 不依赖 LocalAgent；
  -不依赖 HTTP；
  -不依赖 Celery；
  -不依赖 Trace identity。

所以 Stage 3.5 Contract Freeze 后，只需要新增具体 Adapter，而不是重新修改 Evaluation Core。

但目前：

**LocalAgent Adapter 尚未实现。**

------

# 29. 一分钟面试答案

> 我在 AgentEvalOps 里做了一个最小 Evaluation Loop，把 Dataset/Suite、ExecutionTarget 和 Run/Attempt/Result Persistence 真正串成可执行闭环。Loop 按 Attempt 粒度运行，一个 Attempt 只执行一次 Target，成功后可以顺序跑多个 Evaluator，并把每个 Result独立 append-only 持久化。
>
> 我没有让这个 Orchestrator 直接持数据库 Session，而是继续复用 WP3 的短 UoW Service，所以 claim、start、record outcome、finalize result、finish run 都是独立短事务，Target 和 Evaluator 外部调用期间没有 open DB transaction。
>
> 在失败语义上我特别区分 execution truth 和 evaluation truth。Target 如果明确返回 FAILURE/TIMEOUT/CANCELLED/UNKNOWN 才记录 Outcome；如果 Target 直接抛异常，因为无法确认远端到底执行到什么程度，我不会猜成 FAILURE，而是让 Attempt 保持 RUNNING，后续通过 stale reconciliation处理。相反 Evaluator 的异常可以根据 EvaluationPolicy 转成 FAIL 或 INCONCLUSIVE Result。
>
> 最终我们用 PostgreSQL 16 做了 multi-evaluator、duplicate delivery、terminal re-entry、duplicate Result race、cross-project 和 transaction-boundary测试。Final Review还抓到一个 Target persistence projection问题：Run和Attempt各自只保存完整 Target 的不同字段子集，不能直接和 full snapshot做 dataclass equality。修复成 field-aware validation后，最终 WP4 integration 10 passed，combined 33 passed，unit 338 passed。

------

# 30. 三分钟面试答案

> WP4 主要解决的是 Application orchestration。前面 WP1 已经定义了 Dataset、Suite、Evaluator，WP2 定义了 ExecutionTarget/Outcome，WP3 又做了 Run、Attempt、Result 的持久化和并发控制，但是它们还没有真正串成一个能运行的评测闭环。
>
> 我最后选择了 Attempt-oriented 的 Single Application Orchestrator。不是 execute_run，而是 execute_attempt，因为 WP3 的 claim、fencing 和 execution ownership 本身就是 Attempt 粒度，而且这也方便未来 Celery 只负责 dispatch attempt id，而不拥有业务生命周期。
>
> 整个流程首先会做 preflight，包括 Case identity/input、Target version、Target resolver、Evaluator resolver 和 provenance validation，这些 deterministic error 尽量都在 claim 和外部执行之前 fail closed。然后通过 WP3 进行 claim/start，事务提交以后才调用 ExecutionTarget，得到 Outcome 后再开新的短事务持久化。SUCCESS Attempt 才进入 evaluator loop，支持多个 evaluator，每个 Result独立 commit，所以前两个 evaluator已经完成、第三个失败时不会回滚前面的事实，之后 re-entry只补 missing slot。
>
> Error semantics也做了明确区分。如果 Target自己返回 FAILURE、TIMEOUT、CANCELLED 或 OUTCOME_UNKNOWN，我们把它当确认的 execution fact。但如果 execute直接抛 Exception，我不会擅自转成 FAILURE，因为远端可能已经执行了，只是我们没拿到响应。这种情况保留 RUNNING，交给 stale reconciliation。Evaluator异常不同，因为执行结果已经确定，EvaluationPolicy明确规定 evaluator error应该归一化成 FAIL或INCONCLUSIVE，所以可以持久化一个脱敏 Result继续后续 evaluator。
>
> 另外 ResultAlreadyFinalized 也不能直接吞掉，并发 resume时要 authoritative reread，确认 logical slot和 provenance完全一致，才能当成 benign convergence。
>
> 这阶段比较典型的 bug 是 Final Review发现合法 Target被 preflight拒绝。原因是 Run保存 capabilities、Attempt保存 config，而 full snapshot两者都有，我们原来把三个对象直接做全等比较。后来改成按照每个 persistence projection实际拥有的字段分别验证，而 resolved Target仍和 authoritative full snapshot严格全等。最终真实 PostgreSQL capability+config+requirement链路通过，WP4 integration 10 passed，combined 33 passed，unit 338 passed。

------

# 31. 高频追问

## Q1：为什么不是 execute_run，而是 execute_attempt？

因为 Attempt 才是执行 ownership、Claim Token 和 Fencing 的基本粒度。Run-oriented 会把 Case traversal、调度、并发、retry 等职责一起拉进 WP4，容易重新做一个 Scheduler。

------

## Q2：为什么 Target 抛异常不直接转 OUTCOME_UNKNOWN？

因为普通程序异常本身不一定代表 external truth unknown，也可能是本地 programming/config bug。当前合同要求 Adapter 对它能识别的 operational state显式返回 Outcome；escaped exception直接传播，避免 Application替 Adapter 猜事实。

------

## Q3：为什么 Evaluator exception却可以转换？

因为 Target execution truth 已经持久化。Evaluator只是在判断已存在结果的质量，而且 Suite已经有 EvaluationPolicy定义 evaluator error怎么解释，所以这里有明确业务合同。

------

## Q4：为什么 multi-evaluator 每个 Result独立 transaction？

因为 Result是 append-only fact。独立提交意味着部分成功可以保留，恢复时只补缺失 slot，不需要让多个外部 evaluator call绑定在一个长事务或大原子操作里。

------

## Q5：为什么 `ResultAlreadyFinalized` 不直接忽略？

因为它只能说明 unique slot冲突，不能证明数据库里已经存在的 Result就是当前调用预期的 provenance。必须 reread authoritative Result验证后才能安全收敛。

------

## Q6：什么叫 DB-only durable resume 没做？

当前从数据库单独恢复 Run/Attempt，不足以重建 TestCase的 expected output、assertions、fixtures、evidence。所以 resume仍需要 caller提供完整 immutable TestCaseVersion。当前做的是 persistence-safe re-entry，不是完整 durable execution。

------

## Q7：为什么 Run COMPLETED 但 Evaluator可以 FAIL？

Run status描述 infrastructure lifecycle：执行结束并且 required evaluator slots完整。Evaluator FAIL描述质量。真正“这个 Candidate能不能发布”应该由后续 Regression/Release Gate决定，而不是污染 Run lifecycle。

------

# 32. 当前最值得记住的主线

WP4 可以浓缩成四句话：

> **第一，Attempt 是 Application Loop 的执行粒度。**

> **第二，DB transaction 和外部 I/O 必须切开。**

> **第三，Execution Truth、Evaluation Judgment、Persistence Fact 必须由不同 owner负责。**

> **第四，同一个业务实体的不同 Persistence Projection 不能因为“描述的是同一个东西”就直接做 full equality。**

如果面试官继续深挖，再展开三个最有价值的问题：

1. Target escaped exception 为什么不能伪造成 FAILURE；
2. duplicate Result 为什么要 authoritative reread；
3. Target full snapshot / Run projection / Attempt projection 为什么要 field-aware validation。

这三个问题最能体现 WP4 不只是“写了个循环”，而是真正在处理 Agent Evaluation application layer 的 **控制流、事实边界、事务边界和恢复语义**。