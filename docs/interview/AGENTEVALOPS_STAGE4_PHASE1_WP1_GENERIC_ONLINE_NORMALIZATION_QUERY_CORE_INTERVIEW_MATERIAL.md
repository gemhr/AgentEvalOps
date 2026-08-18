# 1. 一句话项目 / 工作包定义

Stage4-Phase1-WP1 完成了 AgentEvalOps 的 **Generic Online Normalization & Query Core（通用在线归一化与查询核心）**：

> 在不重建 LocalAgent 已有 Trace ingestion（链路接入）的前提下，引入 runtime-neutral（运行时无关）的 Trace / Span 归一化域，把 LocalAgent 和 legacy `/traces` 两种来源映射成统一 online projection（在线投影），扩展现有 PostgreSQL Trace/Span read model（读取模型）提供 failing / operation / source / outcome / contract version 查询，同时修复 legacy `/traces` 的跨项目 Trace/Span ownership P1。

最终独立 Final Gate：

```text
CODE_REVIEW_GATE: PASS
WP1_POSTGRESQL_DYNAMIC: PASS
STAGE4_PHASE1_WP1_PROJECT_GATE: PASS
```

真实 Full Unit 为 **532 passed**。

------

# 2. 为什么做

在 WP1 之前，AgentEvalOps 已经有两套 Trace 世界。

第一套是 LocalAgent compatibility ingestion：

```text
LocalAgent
→ frozen Trace Contract
→ AgentEvalOps
→ sidecar
→ legacy Trace/Span projection
```

这条链路已经能真实接收 LocalAgent 数据，因此**不应该重做**。

第二套是 PandaProbe 原有 legacy `/traces`：

```text
POST /traces
→ Celery
→ TraceRepository
→ traces / spans
```

问题是这两套能力之间缺一个真正通用的内部层。

LocalAgent 数据虽然进入了 AgentEvalOps，但它的关键运行语义仍然绑定在 LocalAgent sidecar；legacy Trace/Span 又只有比较粗的状态语义。

因此系统无法稳定回答：

```text
“无论 Trace 来自 LocalAgent
还是旧兼容接口，

什么叫 failure？
operation 怎么统一？
source 怎么标记？
以后 metrics 应该基于什么事实？”
```

H-1 最终确认真正缺失的是：

```text
runtime-specific producer
        ↓
runtime-neutral normalized domain
        ↓
generic queryable projection
```

而不是另一套 HTTP ingestion。

------

# 3. 真实性与完成边界

## 已真实实现

本 WP 已实现：

- `NormalizedOnlineTrace`
- `NormalizedOnlineSpan`
- `GenericOutcome`
- internal `TraceIngestPort`
- LocalAgent → Generic mapping
- legacy → Generic mapping
- normalized Trace/Span PostgreSQL projection
- generic source identity
- generic operation
- generic failure semantics
- Trace outcome summary
- Generic Query filters
- legacy Trace ownership P1 修复
- legacy Span ownership P1 修复
- source-kind ownership protection
- LocalAgent raw + normalized projection 同事务写入。

## 已真实动态验证

Final Gate 真实执行：

- focused unit：**78 passed**
- PostgreSQL tenant adversarial：**6 passed**
- LocalAgent PostgreSQL integration：**28 passed**
- legacy regression：**42 passed**
- Full Unit：**532 passed**
- fresh PostgreSQL migration：PASS
- Ruff：PASS
- `uv lock --check`：PASS
- `git diff --check`：PASS
- `compileall`：PASS
- Alembic single head：`d3a4e5f6b7c8`。

## 明确未实现

当前还没有：

- online metrics；
- request / failure / latency aggregation；
- Trace → Evaluation bridge；
- historical normalized backfill；
  -真实 subject/runtime version attribution；
  -新 Generic HTTP ingest endpoint；
  -新 UI；
  -新 Redis pipeline。

这些属于 Known Limitation 或下一 WP，不得面试时说成已完成。

------

# 4. 修改前架构与根因

修改前大致是：

```text
                   ┌─ LocalAgent sidecar
LocalAgent ────────┤
                   └─ legacy Trace/Span projection


Legacy /traces
      ↓
    Celery
      ↓
legacy Trace/Span
```

核心问题有两个。

## 问题一：两种 producer 没有统一 runtime-neutral 语义

LocalAgent 有：

```text
OK
ERROR
CANCELLED
TIMED_OUT
```

legacy 有：

```text
OK
ERROR
UNSET
```

LocalAgent operation 也有自己的 frozen operation。

如果 Monitoring 直接绑定某一个 producer：

```text
if localagent:
    ...
elif legacy:
    ...
```

Generic Online Core 就只是一个更大的 compatibility adapter，而不是真正的通用域。

------

## 问题二：legacy `/traces` 有真实 tenant P1

旧逻辑：

```text
ON CONFLICT(trace_id)
DO UPDATE
```

但没有确保：

```text
existing.project_id == incoming.project_id
```

因此：

```text
Project A
trace_id = X

Project B
trace_id = X
```

B 可能通过 global PK collision 修改 A 的 row。

Span 同样存在风险。

H-1 已确认这是旧 Phase 0 就识别过、但当前源码仍存在的真实 P1。

------

# 5. 方案讨论与取舍

## 方案一：重新做一个 Generic Trace ingestion API

例如：

```text
POST /online/traces
```

然后 LocalAgent 再去接它。

这个方案被拒绝。

因为 LocalAgent 已经有：

- contract validation；
- fingerprint；
- authentication；
- idempotency；
- sidecar；
- ownership；
- commit-before-2xx。

再建 ingestion，相当于重做已有能力。

最终选择：

> **外部 Adapter 保留，Generic Core 放在内部。**

------

## 方案二：新建 `generic_online_observations` 表

优点：

-语义干净；
-和 legacy 表分离。

缺点：

-第二套 Trace persistence；
-第二套 identity；
-第二套 repository；
-第二套 query；

- UI 又要重新接。

当前规模下属于过度建设。

最终采用：

> **扩展现有 `traces` / `spans` 表，增加最少 normalized projection columns。**

H-2 明确把 generic columns 定义为 projection，而不是新的 raw authority。

------

## 方案三：直接查询 LocalAgent sidecar

也被拒绝。

否则 Generic Query 会变成：

```text
Generic Core
↓
LocalAgent sidecar schema
```

这就产生了错误依赖方向。

正确方向是：

```text
LocalAgent Adapter
        ↓
Generic Domain
```

而不是：

```text
Generic Domain
        ↓
LocalAgent
```

------

# 6. 最终架构

最终架构可以画成：

```text
LocalAgent
   │
   ▼
LocalAgent Compatibility Layer
 contract / fingerprint / validation
 identity / digest / sidecar
   │
   ├──────────── Raw Authority
   │
   ▼
LocalAgent → Generic Mapper
   │
   ▼
NormalizedOnlineTrace / Span
   │
   ▼
Internal TraceIngestPort
   │
   ▼
TraceRepository
   │
   ▼
traces / spans normalized projection
   │
   ▼
Existing TraceService / GET /traces
```

另一边：

```text
Legacy /traces
     │
     ▼
Celery Worker
     │
     ▼
Legacy → Generic Mapping
     │
     ▼
Same Generic Projection
```

所以 Generic Core 实际支持两个 producer：

```text
LocalAgent
Legacy
```

这也证明它不是 LocalAgent-specific domain。

------

# 7. 核心状态机与时序

这个 WP 没有复杂的新 lifecycle state machine，但有两个非常重要的事务时序。

## LocalAgent 路径

```text
HTTP envelope
   ↓
contract validation
   ↓
digest / identity validation
   ↓
same PostgreSQL transaction
   ├─ external identity
   ├─ raw sidecar
   ├─ legacy Trace/Span
   └─ generic normalized projection
   ↓
COMMIT
   ↓
201 PERSISTED
```

关键点：

> Generic projection 失败，整个 transaction rollback。

因此不存在：

```text
sidecar 已成功
generic projection 失败
但 HTTP 仍返回 PERSISTED
```

Final Gate 已真实验证这个 rollback。

------

## Legacy `/traces` 路径

这是完全不同的语义：

```text
POST /traces
   ↓
enqueue
   ↓
HTTP 202
   ↓
Celery worker
   ↓
PostgreSQL transaction
   ↓
commit / rollback
```

因此必须记住：

> **202 ≠ PostgreSQL commit success。**

Final Gate 明确确认 legacy 202 只是 enqueue acknowledgement（入队确认），不是 durable receipt（持久化回执）或 exactly-once。

------

# 8. 数据 / 权限 / Owner

这部分是本 WP 最值得面试讲的内容之一。

| 事实                          | Owner                                |
| ----------------------------- | ------------------------------------ |
| LocalAgent 原始 envelope      | LocalAgent Compatibility Domain      |
| external ID binding           | LocalAgent compatibility persistence |
| LocalAgent sidecar            | Raw authoritative owner              |
| Generic normalized facts      | Generic Online Domain                |
| normalized DB columns         | Generic read projection              |
| legacy compatibility contract | legacy route/service                 |
| tenant arbitration            | PostgreSQL + Repository              |
| generic query                 | TraceService + TraceRepository       |
| metrics                       | WP2，当前未实现                      |

核心原则：

> **Raw fact 和 normalized projection 不能争同一个 Owner。**

LocalAgent sidecar：

```text
真实保存 producer 原始事实
```

normalized columns：

```text
为了通用查询而派生出的 read projection
```

Final Gate 特别确认：

```text
LocalAgent raw authority: SIDECAR
Generic normalized authority: READ_PROJECTION
```



------

# 9. 兼容策略

## LocalAgent

保持：

```text
KEEP + MAP_TO_GENERIC
```

没有修改：

- frozen contract；
- fingerprint validation；
- decoder；
- Redis admission；
- idempotency；
- wire response。

只是添加：

```text
LocalAgent
→ Generic mapper
```

------

## legacy

保留：

```text
POST /traces
202
Celery
```

同时增加：

```text
legacy → Generic normalized projection
```

也就是说：

> Generic Core 没有为了架构整洁而破坏旧兼容入口。

------

## 历史数据

Migration 前的数据：

```text
normalized_* = NULL
```

不做历史 backfill。

这是明确的 P2 limitation，而不是偷偷在 duplicate replay 时补写。

------

# 10. Bad Cases

## Bad Case 1：跨项目 Trace ID collision 覆盖别人数据

### 真实性

**真实源码问题，已真实修复并 PostgreSQL 验证。**

旧逻辑：

```text
Project A:
trace_id = X

Project B:
trace_id = X
```

如果：

```text
ON CONFLICT(trace_id)
DO UPDATE
```

没有 tenant predicate，B 可以覆盖 A。

修复：

```text
ON CONFLICT(trace_id)
DO UPDATE
WHERE existing.project_id == incoming.project_id
```

foreign owner：

```text
rowcount = 0
→ typed ownership conflict
→ rollback
```

Final Gate 不只是验证抛错，还验证 foreign Trace row 没有被改。

------

## Bad Case 2：Span ID 在同项目里换 Trace

即使：

```text
project_id 相同
```

也不能允许：

```text
Span X 原属于 Trace A

后来：
Span X → Trace B
```

否则 Span identity 被重绑定。

因此 Span 冲突需要同时满足：

```text
same span_id
same trace_id
same project owner
```

才允许 compatibility upsert。

Final Gate 实际验证：

```text
same span different trace:
FAIL_CLOSED
```



------

## Bad Case 3：把 CANCELLED/TIMEOUT 都压成 ERROR

原 LocalAgent → legacy 投影会损失语义。

如果 Generic Core继续这样：

```text
CANCELLED
TIMEOUT
↓
ERROR
```

后续 metrics 无法区分：

-真正失败；
-超时；
-取消。

因此 GenericOutcome 冻结成：

```text
SUCCESS
FAILURE
CANCELLED
TIMEOUT
UNKNOWN
```

这是 Generic Online Core 非常关键的一次语义升级。

------

## Bad Case 4：后来的 SUCCESS 把已经失败的 Trace“洗绿”

例如：

```text
Span 1 → FAILURE
Span 2 → SUCCESS
```

如果 Trace summary 只是：

```text
last span wins
```

最终可能变：

```text
Trace = SUCCESS
```

这显然错误。

最终 summary precedence：

```text
FAILURE
>
TIMEOUT
>
CANCELLED
>
UNKNOWN
>
SUCCESS
```

并真实验证：

> 已有 failing child 后，后续 SUCCESS 不会恢复为 SUCCESS。

------

## Bad Case 5：Generic Core 直接 import LocalAgent DTO

错误：

```text
GenericOnlineService
→ LocalAgentTraceEnvelopeInV1
```

这样 Generic Core 只是 LocalAgent service 的改名版本。

最终依赖方向严格为：

```text
LocalAgent
→ Generic
```

Final Gate 静态扫描确认：

```text
Generic domain imports LocalAgent: NO
```



------

## Bad Case 6：把 contract version 冒充 runtime version

当前 LocalAgent真实能提供：

```text
source_contract_identity
source_contract_version
```

但现有证据不能证明它提供：

```text
runtime_version
agent_version
model_version
prompt_version
```

错误做法：

```text
contract_version=1
→ runtime_version=1
```

这属于版本归因造假。

最终：

```text
source_contract_version = 1
subject_version_ref = None
```

并明确：

```text
Subject/runtime version fabricated: NO
```



------

## Bad Case 7：跨 producer 覆盖 source_kind

假设一个 Trace 已经：

```text
source_kind = localagent
```

后来 legacy producer 使用同一 canonical trace id：

```text
source_kind = legacy
```

不能 last-write-wins。

否则一个 Trace 的 producer provenance 会被篡改。

当前：

```text
source mismatch
→ NormalizedSourceConflictError
→ fail closed
```

并有 PostgreSQL 动态测试。

------

# 11. 已真实执行 Tests / Gates

Final Gate：

| Gate                              | 结果        |
| --------------------------------- | ----------- |
| Focused Unit                      | 78 passed   |
| PostgreSQL Tenant Adversarial     | 6 passed    |
| LocalAgent PostgreSQL Integration | 28 passed   |
| Legacy Regression                 | 42 passed   |
| Full Unit                         | 532 passed  |
| Fresh DB Migration                | PASS        |
| Ruff                              | PASS        |
| uv lock                           | PASS        |
| git diff check                    | PASS        |
| compileall                        | PASS        |
| Alembic                           | single head |

Tenant adversarial 真实覆盖：

- cross-project Trace collision；
- cross-project Span collision；
- same Span different Trace；
- source-kind replacement；
- failure summary；
- tenant-scoped Generic Query。

------

# 12. Known Limitations

当前必须明确：

```text
Historical normalized backfill:
NO

Legacy /traces HTTP 202 durable receipt:
NO

Generic normalized projection raw authority:
NO

LocalAgent raw authority:
SIDECAR

Subject/runtime version available:
NO

Metrics:
NOT IMPLEMENTED

Trace→Evaluation:
NOT IMPLEMENTED

New Generic Ingest HTTP API:
NO

New UI:
NO

New Redis pipeline:
NO
```



其中最容易被误认为缺陷的是：

### historical normalized NULL

这是因为当前刻意没有做 backfill。

### subject/runtime version = NO

不是“忘了实现”，而是：

> 当前 producer 没有可靠事实，宁可 `None`，也不能伪造版本归因。

------

# 13. 体现的工程能力

## 1. Adapter Boundary（适配器边界）

LocalAgent-specific logic 停留在：

```text
LocalAgent Compatibility Layer
```

进入 Generic Core 前必须转换成 runtime-neutral DTO。

------

## 2. Raw Fact 与 Projection 分离

这是非常典型的数据架构能力：

```text
Raw Source of Truth
≠
Queryable Projection
```

------

## 3. Multi-tenant Ownership

不是简单地：

```python
if row.project_id != project_id:
    raise
```

而是把 ownership predicate 放进 PostgreSQL conflict arbitration。

这解决了并发 TOCTOU（检查与使用时序竞争）问题。

------

## 4. Failure Semantics

没有简单复用 legacy `ERROR`。

而是保留：

```text
FAILURE
CANCELLED
TIMEOUT
UNKNOWN
```

为下一层 metrics提供可信语义。

------

## 5. Scope Control

没有：

-新 Trace 表；
-新 Generic API；
-新 UI；
-新 Redis pipeline；

- Kafka；
- ClickHouse。

只补当前闭环真正需要的 Generic Core。

------

# 14. 30 秒面试版本

> AgentEvalOps 最开始有 PandaProbe 的 legacy Trace，以及后来真实接入的 LocalAgent Trace，但两套语义并不统一。所以我做了一个 runtime-neutral Generic Online Core，把 LocalAgent 和 legacy 两个 producer 映射成统一的 Trace/Span projection，状态统一成 SUCCESS、FAILURE、CANCELLED、TIMEOUT、UNKNOWN，operation 保持开放字符串。
>
> 我没有新建第二套 Trace 表，而是扩展已有 Trace/Span read model；LocalAgent sidecar继续保存原始权威事实，generic columns只作为查询投影。另外我修复了 legacy `/traces` 的跨项目 upsert P1，把 project ownership 放进 PostgreSQL `ON CONFLICT` 仲裁里。真实 PostgreSQL adversarial tests 验证了跨 tenant Trace/Span collision fail closed，Final Gate Full Unit 532 passed。

------

# 15. 2 分钟面试版本

> 我在 AgentEvalOps 做 Generic Online Core 时，首先没有重建 LocalAgent ingestion，因为此前 LocalAgent 已经通过 frozen contract、fingerprint、idempotency和sidecar真实接入了AgentEvalOps。真正缺的是它和legacy PandaProbe Trace之间的runtime-neutral内部层。
>
> 所以我定义了 `NormalizedOnlineTrace`、`NormalizedOnlineSpan` 和统一的 `GenericOutcome`。LocalAgent的OK、ERROR、CANCELLED、TIMED_OUT分别映射成SUCCESS、FAILURE、CANCELLED和TIMEOUT；legacy UNSET映射成UNKNOWN，这样不会把取消和超时压缩成普通ERROR。
>
> Persistence上我没有新建第二套observation表，而是给已有traces和spans增加少量normalized projection字段。这里Owner是严格分开的：LocalAgent sidecar仍是raw authoritative truth，generic字段只是read projection。
>
> 这个WP还修了一个真实P1。legacy `/traces` 原来是global trace_id上的无条件upsert，cross-project ID collision可能修改其他tenant的数据。我没有改成composite PK，而是保留兼容identity，把project ownership predicate放进PostgreSQL `ON CONFLICT DO UPDATE WHERE`，Span还同时校验same trace identity。foreign collision直接typed fail closed。
>
> LocalAgent写generic projection时也和identity、sidecar、legacy projection放在同一个PostgreSQL事务里，因此generic写失败不会出现sidecar成功但query数据缺失的部分事实。
>
> 最后在已有GET `/traces`上增加failing、operation、source、normalized outcome和contract version过滤，没有新建第二套API/UI。Final Gate真实跑了PostgreSQL adversarial、LocalAgent integration和legacy regression，最终532个unit全部通过。

------

# 16. 深入版本

面试深挖时可以按四层讲。

## 第一层：Producer Contract

```text
LocalAgent contract
legacy contract
```

它们可以不同。

------

## 第二层：Adapter

负责：

```text
producer-specific schema
→ generic normalized schema
```

LocalAgent Adapter 可以理解 LocalAgent。

Generic Domain 不可以。

------

## 第三层：Generic Truth

```text
NormalizedOnlineTrace
NormalizedOnlineSpan
GenericOutcome
```

这里不再关心 producer-specific contract。

------

## 第四层：Projection / Query

```text
PostgreSQL
→ GET /traces
→ failing / operation / source / outcome
```

整体就是：

```text
Producer-specific Fact
        ↓
Adapter
        ↓
Runtime-neutral Fact
        ↓
Queryable Projection
```

这是本 WP 最完整的设计主线。

------

# 17. 高频追问

## Q1：为什么不直接用 OpenTelemetry？

当前目标不是重建一个完整 OTel 平台，而是让既有 LocalAgent 和 legacy Trace 有一个最小 runtime-neutral 中间层。

这个 WP 的重点是：

- ownership；
- outcome；
- operation；
- source；
- query。

没有证据需要完整 OTLP contract，所以没有扩大范围。

------

## Q2：为什么不新建 generic Trace table？

因为现有 Trace/Span 已经有：

- project FK；
- relation；
- query；
- API；
- UI。

新建表会产生第二套 identity / repository / query owner。

当前最小 extension 足够，因此选择 projection columns。

------

## Q3：Generic projection 和 LocalAgent sidecar 谁是真相？

LocalAgent 场景：

```text
sidecar = raw authoritative truth
generic columns = normalized read projection
```

Generic projection 不能反向覆盖 sidecar。

------

## Q4：为什么 tenant check 要放 `ON CONFLICT` 里？

因为：

```text
SELECT owner
↓
检查
↓
UPDATE
```

之间存在并发窗口。

把 ownership predicate 放进 conflict statement：

```text
ON CONFLICT
DO UPDATE
WHERE owner matches
```

由 PostgreSQL完成原子仲裁。

------

## Q5：为什么 global ID 不改成 `(project_id, trace_id)`？

因为现有 route、PK/FK、legacy compatibility 都已经基于 global UUID。

为修复 ownership P1 而重做 identity schema 成本太高。

当前可以在保留 global identity的情况下安全 fail closed。

------

## Q6：为什么 UNKNOWN 不算 failure？

UNKNOWN 表示：

> 证据不足以映射。

它既不是成功，也不是确定失败。

Generic failing rule 只接受明确：

```text
FAILURE
CANCELLED
TIMEOUT
```

------

## Q7：为什么 CANCELLED 算 failing trace？

当前 Generic Online 查询把它作为 abnormal terminal outcome（异常终态）的一部分。

这不是说 cancellation一定是业务 bug，而是：

> 对 online diagnosis / metrics 来说，这个 Trace不是正常 SUCCESS。

后续 WP2 metrics会复用同一 failing semantics，避免出现第二套定义。

------

## Q8：为什么 LocalAgent 需要 same transaction 写 generic projection？

因为 HTTP `PERSISTED` 是 commit-before-2xx。

如果：

```text
sidecar commit
↓
再异步写 generic projection
```

就可能出现：

```text
PERSISTED
但 Online Query查不到
```

造成 accepted ingest truth和monitoring read model不一致。

------

# 18. 最容易夸大 / 答错

### 错误说法 1

> “现在已经有完整实时监控平台。”

错。

Metrics还没实现。

------

### 错误说法 2

> “Generic Trace 是新的权威数据源。”

错。

LocalAgent raw authority仍是 sidecar。

------

### 错误说法 3

> “legacy `/traces` 返回202说明数据库已经写成功。”

错。

202只是 enqueue acknowledgement。

------

### 错误说法 4

> “我们已经有 runtime version attribution。”

错。

当前只有 LocalAgent source contract version，subject/runtime version仍不可用。

------

### 错误说法 5

> “所有历史Trace都已经normalized。”

错。

没有历史 backfill。

------

### 错误说法 6

> “Generic Core支持任意外部runtime直接HTTP上报。”

错。

没有新增 Generic HTTP ingest endpoint。

当前真实 producer仍是已有 Adapter：

- LocalAgent；
- legacy compatibility。

------

### 错误说法 7

> “LocalAgent和legacy用同一个raw schema。”

错。

它们只有进入 Generic Domain 以后共享 runtime-neutral representation。

------

# 19. P0 / P1 / P2

最终 Final Gate：

```text
P0 = 0
P1 = 0
```

Final Gate 报告的非阻断项主要是：

### P2

- historical normalized rows可能为 NULL；
- legacy 202不是 durable；
- subject/runtime version不可用。

### P3 / Deferred

-更多 query性能优化。

其中本 WP 最值得强调的真实修复是：

```text
Legacy Trace tenant P1:
CLOSED

Legacy Span tenant P1:
CLOSED
```



------

# 20. 速查表

| 问题                           | 当前答案                                                  |
| ------------------------------ | --------------------------------------------------------- |
| Generic Domain 粒度            | Trace + Span                                              |
| GenericOutcome                 | SUCCESS / FAILURE / CANCELLED / TIMEOUT / UNKNOWN         |
| operation                      | 开放字符串                                                |
| LocalAgent raw authority       | sidecar                                                   |
| Generic normalized authority   | read projection                                           |
| Generic Core import LocalAgent | NO                                                        |
| Generic新Trace表               | NO                                                        |
| Persistence方式                | 扩展现有 Trace/Span                                       |
| 新增normalized字段             | 11个                                                      |
| Generic ingest HTTP API        | NO                                                        |
| TraceIngestPort                | Internal application port                                 |
| LocalAgent→Generic             | YES                                                       |
| Legacy→Generic                 | YES                                                       |
| LocalAgent generic写事务       | 与sidecar同事务                                           |
| duplicate语义变化              | NO                                                        |
| legacy Trace跨租户P1           | CLOSED                                                    |
| legacy Span跨租户P1            | CLOSED                                                    |
| same Span换Trace               | Fail closed                                               |
| source-kind覆盖                | Fail closed                                               |
| failing定义                    | FAILURE/CANCELLED/TIMEOUT                                 |
| UNKNOWN算failure               | NO                                                        |
| Generic Query                  | 扩展现有 GET `/traces`                                    |
| query filters                  | outcome / failing / operation / source / contract version |
| runtime version                | 当前没有                                                  |
| historical backfill            | NO                                                        |
| Metrics                        | NOT IMPLEMENTED                                           |
| Trace→Evaluation               | NOT IMPLEMENTED                                           |
| PostgreSQL adversarial         | 6 passed                                                  |
| LocalAgent integration         | 28 passed                                                 |
| Legacy regression              | 42 passed                                                 |
| Full Unit                      | 532 passed                                                |
| Project Gate                   | PASS                                                      |

这个 WP 最应该记住的三句话是：

> **第一，Adapter 负责理解 producer，Generic Core 不应该理解 producer。**

> **第二，Raw authoritative fact 和 normalized read projection 必须分 Owner，不能因为方便查询就篡改事实源。**

> **第三，多租户 Upsert 的 ownership 不能只靠应用层先查后改，必须让 PostgreSQL 在 conflict arbitration 中参与最终判定。**