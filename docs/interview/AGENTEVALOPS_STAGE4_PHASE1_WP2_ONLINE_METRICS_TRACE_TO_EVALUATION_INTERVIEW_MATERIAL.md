# 推荐面试材料文件名

```
AGENTEVALOPS_STAGE4_PHASE1_WP2_ONLINE_METRICS_TRACE_TO_EVALUATION_INTERVIEW_MATERIAL.md
```

# 1. 一句话项目 / 工作包定义

Stage4-Phase1-WP2 完成了 AgentEvalOps 的 **Online Metrics（在线指标）与 Trace-to-Evaluation Foundation（Trace 到评估基础）**：

> 在 WP1 已建立 runtime-neutral（运行时无关）Trace/Span 事实与统一 failure semantics（失败语义）的基础上，通过 PostgreSQL query-time aggregation（查询时聚合）计算 request、failure、latency 指标，并把 failing Trace（失败 Trace）确定性转换为 `TraceEvidenceCandidate → EvidenceRef`，形成生产运行数据进入离线 Evaluation（评估）体系的最小交接边界。

最终：

```text
STAGE4_PHASE1_WP2_PROJECT_GATE: PASS
PHASE1_GENERIC_ONLINE_CORE: COMPLETE
```

Full Unit 为 **541 passed**，P0=0、P1=0。

------

# 2. 为什么做

WP1 已经解决了：

```text
不同 runtime Trace
        ↓
Generic normalization
        ↓
统一 outcome / operation / source
        ↓
可查询 PostgreSQL projection
```

但它只能回答：

> “发生了哪些 Trace？哪些 Trace 是 failing？”

还不能形成 AgentEvalOps 最终需要的 Online → Offline 闭环。

真正还缺两步：

```text
第一步：
在线运行数据
→ 可量化的健康指标

第二步：
失败 Trace
→ 可交给 Evaluation 的 evidence reference
```

因此 WP2 的目标不是再造 Monitoring Platform（监控平台），而是做到：

```text
Trace facts
→ Metrics
→ Failing Trace
→ EvidenceRef
```

从而把“线上发现问题”和“离线回归验证”接起来。

------

# 3. 真实性与完成边界

## 已真实实现

### Online Metrics

已实现：

- `trace_count`
  - 作为 generic `request_count`
- `failure_count`
- average latency
- P50
- P90
- P99
- optional `normalized_source_kind` filter
- project/time scoped aggregation。

### Trace → Evaluation

已实现：

- `TraceEvidenceCandidate`
- failing-only candidate selection
- `EvidenceRef(kind="trace")`
- project-scoped Evidence resolution
- normalized Trace/Span read-back。

## 已真实测试

Final Gate：

- WP2 PostgreSQL suite：**5 passed**
- multiple-failing-span direct PostgreSQL probe：PASS
- WP1 / legacy / LocalAgent相关 PostgreSQL regression：**76 passed**
- Full Unit：**541 passed**
- Ruff：PASS
- uv lock：PASS
- diff check：PASS
- compileall：PASS
- Alembic single head：PASS。

## 明确没有实现

没有：

- Metrics persistence；
- metrics table / rollup；
- materialized view；
- candidate persistence；
- candidate HTTP API；
  -自动 Dataset creation；
  -自动 TestCase creation；
  -自动 EvaluationRun；
  -自动 Regression；
  -自动 Release Gate；
  -历史 metrics/trend；
- Alert/SLO/SLA。

------

# 4. 修改前架构与根因

WP1 后已经有：

```text
Generic Trace / Span
        ↓
PostgreSQL normalized projection
        ↓
GET /traces
        ↓
failing / operation / source query
```

但有两个核心缺口。

## 缺口一：旧 analytics 的 error 不等于 generic failure

旧 PandaProbe 已有：

```text
error_count
```

但它的语义是：

```text
Trace.status == ERROR
```

这无法表达 WP1 已冻结的：

```text
FAILURE
CANCELLED
TIMEOUT
```

尤其 LocalAgent 的 child Span 可能 `TIMEOUT`，但 legacy Trace 本身仍不是 `ERROR`。

所以不能把旧 `error_count` 当 generic failure metric。

------

## 缺口二：failing Trace 还不能进入 Evaluation 语义

虽然已经能：

```text
GET /traces?failing=true
```

但 Evaluation domain 并不知道：

> “这个 Trace 应该怎样作为 Evidence 被引用？”

所以需要一个稳定的 handoff contract，而不是直接把整份 Trace payload 塞进 Evaluation。

------

# 5. 方案讨论与取舍

## Metrics 方案一：新增 Metrics Store

例如：

```text
metrics table
rollup table
background aggregation
```

优点：

-适合大规模历史趋势；
-查询更快。

当前被拒绝。

因为 WP1 的 normalized projection 已足够确定性重算：

```text
request_count
failure_count
latency
```

当前没有容量证据证明必须预计算。

所以最终采用：

> **Query-time PostgreSQL analytics。**

------

## Candidate 方案一：新建 Candidate Table

例如：

```text
trace_evaluation_candidates
```

也被拒绝。

因为：

```text
failing Trace
→ TraceEvidenceCandidate
```

是完全确定性的派生。

当前没有：

-审批状态；
-人工标注生命周期；
-消费确认；
-历史 Candidate audit

这些持久化需求。

所以：

```text
Candidate persistence = NONE
```

------

## Evidence 方案

没有复制：

```text
input
output
error
attributes
span payload
```

而是只生成：

```text
EvidenceRef(
    kind="trace",
    identifier=str(trace_id)
)
```

这使 Evaluation 接收的是**引用**，而不是 Online Core 自己定义的新 Evaluation payload。

------

# 6. 最终架构

完整 Phase1 现在形成：

```text
Runtime / LocalAgent
        ↓
WP1
Generic Online Normalization
        ↓
PostgreSQL Trace/Span Projection
        │
        ├─────────────┐
        │             │
        ▼             ▼
     Metrics       Failing Query
        │             │
        ▼             ▼
request_count   TraceEvidenceCandidate
failure_count           │
latency                  ▼
                  EvidenceRef(kind="trace")
                         │
                         ▼
                   Evaluation Boundary
```

注意：

```text
EvidenceRef
≠
TestCase
```

它只是：

> “Evaluation 后续可以用这个 Trace 作为 Evidence。”

------

# 7. 核心状态机与时序

WP2 没有新增 durable state machine（持久状态机）。

它是两个 deterministic read flow（确定性读取流程）。

## Metrics Flow

```text
project + time range
        ↓
TraceRepository
        ↓
query-time SQL
        ↓
trace_count
failure_count
avg
p50
p90
p99
```

------

## Candidate Flow

```text
project context
        ↓
list_traces(failing=True)
        ↓
TraceEvidenceCandidate
        ↓
EvidenceRef
        ↓
project-scoped Trace resolver
```

Candidate 没有：

```text
PENDING
SELECTED
PROCESSED
CONSUMED
```

这些状态。

因为当前它不是持久化业务实体。

------

# 8. 数据 / 权限 / Owner

| 事实/能力                     | Owner                        |
| ----------------------------- | ---------------------------- |
| Generic Trace/Span facts      | WP1 Generic Online Core      |
| Failure semantics             | WP1                          |
| Metrics SQL aggregation       | `TraceRepository`            |
| Metrics application semantics | `TraceService`               |
| Analytics HTTP transport      | existing `/traces/analytics` |
| Trace 是 Evaluation candidate | Online Core                  |
| `EvidenceRef` 类型            | Evaluation reference domain  |
| 是否生成 TestCase             | Future Evaluation Catalog    |
| Dataset ownership             | Evaluation                   |
| Candidate persistence         | 无                           |

最关键的是：

> **WP2 可以说“这个 failing Trace 是一个 Evidence candidate”，但无权说“它现在已经是 TestCase”。**

------

# 9. 兼容策略

## 旧 Analytics

原有：

```text
error_count
```

继续保留。

新增：

```text
failure_count
```

而不是替换它。

因此兼容关系是：

```text
error_count
= legacy semantics

failure_count
= Generic Online semantics
```

Final Gate 动态场景实际证明二者可以不同：

```text
failure_count = 2
error_count = 1
```



------

## API

继续使用：

```text
GET /traces/analytics
```

仅增加：

- `failure_count`
- optional `normalized_source_kind`

没有新建：

```text
/online/metrics
```

------

## Candidate

仍然：

```text
INTERNAL_ONLY
```

没有新 HTTP endpoint。

------

# 10. Bad Cases

## Bad Case 1：把 `error_count` 直接改名成 `failure_count`

### 真实性

架构风险，已明确防止，并有动态场景证明二者不同。

例如：

```text
Trace C:
legacy status != ERROR

child Span:
TIMEOUT
```

Generic：

```text
failure_count += 1
```

legacy：

```text
error_count 不增加
```

如果直接复用 error_count，就会漏掉这类失败。

最终两种指标并存。

------

## Bad Case 2：只看 Trace outcome，漏掉 child Span failure

例如：

```text
Trace.normalized_outcome = SUCCESS

Span:
TIMEOUT
```

按照 WP1：

```text
Trace is failing
```

所以 metrics 必须：

```text
Trace failure
OR
EXISTS failing child
```

而不能：

```text
WHERE trace.normalized_outcome = FAILURE
```

真实 PostgreSQL 测试已覆盖 child TIMEOUT。

------

## Bad Case 3：一个 Trace 有多个失败 Span，被重复统计

错误 SQL：

```text
Trace
JOIN Span
WHERE Span failing
COUNT(*)
```

例如：

```text
Trace A
├─ Span 1 TIMEOUT
└─ Span 2 FAILURE
```

可能得到：

```text
failure_count = 2
```

实际上应该：

```text
failure_count = 1
```

当前实现采用 correlated `EXISTS`，H-4 还额外做了真实 PostgreSQL probe：

```text
1 Trace
2 failing Spans
→ failure_count = 1
```



------

## Bad Case 4：把 in-flight Trace latency 当 0

例如：

```text
started_at = 10:00
ended_at = NULL
```

如果：

```text
COALESCE(latency, 0)
```

会人为降低：

- avg；
- percentile。

当前策略：

```text
trace_count：
计入

latency：
排除
```

真实场景中：

```text
4 traces
其中1条 ended_at=NULL
avg由其余3条计算
```



------

## Bad Case 5：把整个 Trace payload 塞进 EvidenceRef

错误：

```text
EvidenceRef.metadata = {
    input,
    output,
    error,
    spans,
    attributes
}
```

这样会：

-复制事实；
-制造新的同步问题；
-扩大 Evaluation contract；
-可能泄漏不必要数据。

最终 Evidence 是 identity-only：

```text
kind = trace
identifier = trace UUID string
schema_version = None
metadata = {}
```

------

## Bad Case 6：EvidenceRef 有 UUID，所以不需要 project

这是一个典型 tenant boundary 错误。

即使：

```text
trace_id globally unique
```

也不代表：

> 任意 project 都应该有权读取。

正确解析必须：

```text
trace_id
+
caller project context
```

真实 PostgreSQL 验证：

```text
Project A Evidence
+ Project A context
→ PASS

Project A Evidence
+ Project B context
→ NotFoundError
```



------

## Bad Case 7：发现 failing Trace 后自动创建 TestCase

看似很顺手：

```text
failing Trace
→ Evidence
→ TestCaseVersion
→ Dataset
```

但这是严重 Owner 越界。

因为 Online Core 并不知道：

- expected output；
  -这个失败值不值得成为长期回归；
  -加入哪个 Dataset；
  -如何脱敏；
  -是否需要人工审核。

所以当前只到：

```text
EvidenceRef handoff
```

------

# 11. 已真实执行 Tests / Gates

Final Gate：

| 验证                                 | 结果                      |
| ------------------------------------ | ------------------------- |
| WP2 PostgreSQL                       | 5 passed                  |
| Multiple failing spans               | 1 Trace → failure_count=1 |
| WP1 / Legacy / LocalAgent regression | 76 passed                 |
| Full Unit                            | 541 passed                |
| Ruff                                 | PASS                      |
| uv lock                              | PASS                      |
| diff check                           | PASS                      |
| compileall                           | PASS                      |
| Alembic                              | single head               |

核心 PostgreSQL metrics 场景：

```text
Trace A → SUCCESS

Trace B → FAILURE

Trace C → child TIMEOUT

Trace D → UNKNOWN + ended_at NULL
```

得到：

```text
trace_count = 4
failure_count = 2
error_count = 1
```

这一个场景同时证明了：

- generic failure 和 legacy error 不同；
- child TIMEOUT 会计入；
- UNKNOWN 不计 failure；
- latency 可以独立排除 in-flight Trace。

------

# 12. Known Limitations

当前明确保留：

```text
Metrics persistence:
NO

Metrics aggregation:
QUERY_TIME

Historical normalized backfill:
NO

Historical generic failure coverage:
PARTIAL

Legacy error_count retained:
YES

Generic failure_count:
SEPARATE SEMANTIC

Latency in-flight rows:
EXCLUDED

Candidate persistence:
NO

Candidate API:
INTERNAL_ONLY

Trace Evidence schema version:
None

Automatic Dataset creation:
NO

Automatic TestCase creation:
NO

Automatic EvaluationRun:
NO

LocalAgent dependency:
NO

New DB schema:
NO
```



特别注意：

### Historical failure coverage = PARTIAL

WP1 前旧数据可能没有 normalized fields。

当前不会为了补数据偷偷使用：

```text
legacy status ERROR
```

参与 generic `failure_count`。

所以历史 Generic failure coverage 不完整，这是明确接受的 limitation。

------

# 13. 体现的工程能力

## 1. Metrics Semantic Ownership（指标语义所有权）

不是“数据库里有什么列就统计什么”。

先确定：

> 什么叫 request？什么叫 failure？什么叫 latency？

再写 SQL。

------

## 2. Single Semantic Source（单一语义来源）

WP1 的 failing rule 被：

```text
GET /traces?failing=true
```

和：

```text
failure_count
```

共同复用。

没有两套：

```text
query failure
metrics failure
```

------

## 3. Online / Offline Boundary

线上负责：

```text
发现异常
提供证据
```

离线 Evaluation 才负责：

```text
是否变 TestCase
怎么评测
是否 Regression
是否 Release Block
```

------

## 4. Multi-tenant Evidence Resolution

Evidence identity 和 access scope 分开：

```text
identifier = trace UUID
authorization/ownership = project context
```

这比把 `project_id` 拼到字符串里更符合 Owner 边界。

------

## 5. Avoiding Premature Persistence（避免过早持久化）

Metrics 和 Candidate 当前都可确定性重建，因此没有因为“以后可能需要”就建新表。

------

# 14. 30 秒面试版本

> 我在 AgentEvalOps 的 Generic Online Core 第二个 WP 里做了最小 Online Metrics 和 Trace→Evaluation handoff。Metrics 没有新增存储，而是直接基于 WP1 的 normalized Trace/Span 在 PostgreSQL query-time 聚合。
>
> request_count 以 Trace 为粒度，failure_count 严格复用 WP1 的 failing rule，也就是 Trace 本身失败或者任一 child Span 是 FAILURE、CANCELLED、TIMEOUT；旧的 error_count 仍保留，所以两者语义不会混淆。Latency 直接用 Trace start/end 算 avg、P50、P90、P99。
>
> 另外 failing Trace 会转换成一个 `TraceEvidenceCandidate` 和 identity-only `EvidenceRef`，Evaluation 后续可以通过 project-scoped resolver重新取 Trace，但这里不会自动创建 Dataset 或 TestCase。最终真实 PostgreSQL验证了failure统计、重复Span不重复计数和跨project Evidence解析 fail closed，Full Unit 541 passed。

------

# 15. 2 分钟面试版本

> WP1完成以后，AgentEvalOps已经能把LocalAgent和legacy Trace统一成runtime-neutral的Trace/Span，并且有统一的failing semantics。但这时还缺在线监控指标和线上失败样本进入离线评估的桥。
>
> Metrics这块我没有重新建设metrics store，因为现有PostgreSQL projection已经足够确定性计算。request_count直接复用Trace count；latency继续复用Trace `ended_at-started_at`和现有percentile_cont实现avg、P50、P90、P99。
>
> 比较关键的是failure_count。我没有复用原来的error_count，因为旧error_count只看legacy `Trace.status == ERROR`，而WP1已经把failure定义成Trace本身或者任意child Span出现FAILURE、CANCELLED、TIMEOUT。所以我抽了共享failing predicate，让Trace查询和metrics聚合使用同一套SQL语义。真实PostgreSQL里有一个Trace本身不是error，但child span是TIMEOUT，此时failure_count会增加，而error_count不增加。
>
> Trace→Evaluation这一侧我也控制了边界。failing Trace只转换成一个不可持久化的`TraceEvidenceCandidate`，里面保存最小identity和`EvidenceRef(kind="trace", identifier=str(trace_id))`。不复制Trace payload，也不会自动创建TestCase或者Dataset。Evidence解析必须同时带project context，所以另一个project即使知道UUID也解析不到。
>
> Final Gate真实验证了一个Trace两个失败Span仍只计一次、跨project Evidence解析fail closed，以及WP1/legacy/LocalAgent回归，最终541个unit全部通过。这个WP结束后Phase1 Generic Online Core正式Complete。

------

# 16. 深入版本

整个 Stage4 当前可以从数据流理解：

```text
Production Runtime
        ↓
Phase1-WP1
Generic Trace Facts
        ↓
Phase1-WP2
Metrics + Evidence Candidate
        ↓
Phase2
Offline Evaluation
        ↓
Phase3-WP1
Baseline/Candidate Comparison
        ↓
Phase3-WP2
Regression Report + Release Gate
```

因此完整闭环已经具备：

```text
Online observation
→ monitoring signal
→ failing evidence
→ offline evaluation
→ regression comparison
→ release decision
```

但注意，这里的：

```text
Evidence → TestCase
```

仍不是自动完成的。

这是当前架构有意留下的 Human/Policy Boundary（人工/策略边界）。

------

# 17. 高频追问

## Q1：failure_count 和 error_count 有什么区别？

`error_count` 是旧 legacy 语义：

```text
Trace.status == ERROR
```

`failure_count` 是 Generic Online 语义：

```text
Trace outcome failure
OR
child Span FAILURE/CANCELLED/TIMEOUT
```

所以真实情况下二者可以不同。

------

## Q2：为什么 CANCELLED 也算 failure？

在 Generic Online monitoring 场景里：

```text
SUCCESS
```

才代表正常完成。

CANCELLED/TIMEOUT 都属于没有正常完成的 terminal outcome（终态结果），因此被纳入 failing Trace。

这不代表取消一定是产品 bug，而是它应该进入异常监控集合。

------

## Q3：为什么 UNKNOWN 不算 failure？

UNKNOWN 的含义是：

> 当前没有足够事实安全判断。

它不是 SUCCESS，但也不是明确 FAILURE。

如果把 UNKNOWN 全部算 failure，会把历史无 normalized 数据、进行中状态等大量污染到 failure_count。

------

## Q4：为什么 latency 不用 Span duration？

因为 metrics 的 request grain 是 Trace。

一个 Trace 可以有：

```text
parallel spans
nested spans
```

sum span duration 会重复计算。

所以 request latency 选：

```text
Trace.ended_at - Trace.started_at
```

------

## Q5：为什么 Candidate 不落库？

当前 candidate：

```text
failing Trace
→ deterministic mapping
```

没有独立生命周期。

随时可以重新通过 failing query派生，因此没必要创造新 durable fact。

------

## Q6：为什么 EvidenceRef 不保存 project_id？

因为：

```text
trace_id = global identity

project_id = authorization/ownership context
```

二者职责不同。

EvidenceRef只引用事实。

解析时再要求 caller提供project context。

------

## Q7：为什么不自动把失败Trace加入Dataset？

因为失败Trace缺少：

- expected output；
  -长期回归价值判断；
  -脱敏/清洗策略；
- Dataset归属；
  -是否只是一次偶发基础设施错误。

所以 Online Core 不应该拥有 TestCase 创建决策。

------

# 18. 最容易夸大 / 答错

### 错误 1

> “已经做了实时Metrics系统。”

不准确。

当前是：

```text
QUERY_TIME PostgreSQL aggregation
```

没有 Metrics Store 或 streaming metrics。

------

### 错误 2

> “error_count就是failure_count。”

错误。

这是本 WP 明确分开的两种语义。

------

### 错误 3

> “历史所有Trace都可以统计generic failure。”

错误。

pre-WP1 normalized backfill没有做，所以 historical coverage是 PARTIAL。

------

### 错误 4

> “Candidate已经自动进入Dataset。”

错误。

Candidate只到 EvidenceRef。

------

### 错误 5

> “EvidenceRef本身保证tenant安全。”

错误。

EvidenceRef只是 identity。

Tenant安全依赖：

```text
project context
+
project-scoped resolver
```

------

### 错误 6

> “已经有Trace Evidence schema v1。”

错误。

当前：

```text
EvidenceRef.schema_version = None
```

因为没有冻结独立 Generic Trace Evidence schema。

------

# 19. P0 / P1 / P2

Final Gate：

```text
P0 = 0
P1 = 0
```



非阻断项：

### P2 / Known Limitations

- historical generic failure coverage partial；
- in-flight latency excluded；
- candidate internal-only；
- metrics persistence absent。

### P3

- correlated `EXISTS` 在大规模表上的性能尚未验证。

这些不是当前 Gate blocker。

------

# 20. 速查表

| 问题                     | 当前答案                                      |
| ------------------------ | --------------------------------------------- |
| request_count 粒度       | Trace                                         |
| API字段                  | 继续叫 `trace_count`                          |
| failure_count            | 新增                                          |
| legacy error_count       | 保留                                          |
| failure定义              | Trace或child Span为 FAILURE/CANCELLED/TIMEOUT |
| UNKNOWN算failure         | 否                                            |
| child failure算failure   | 是                                            |
| 多个failure Span重复计数 | 否                                            |
| Metrics存储              | 无                                            |
| Metrics方式              | Query-time SQL                                |
| latency来源              | `Trace.ended_at - started_at`                 |
| in-flight latency        | 排除                                          |
| latency指标              | AVG/P50/P90/P99                               |
| metrics source filter    | 支持                                          |
| candidate来源            | failing Trace                                 |
| Candidate持久化          | 无                                            |
| Candidate API            | Internal-only                                 |
| Evidence类型             | `EvidenceRef`                                 |
| Evidence kind            | `trace`                                       |
| Evidence identifier      | Trace UUID字符串                              |
| Evidence schema version  | None                                          |
| Evidence复制payload      | 否                                            |
| tenant解析               | project context + project-scoped resolver     |
| 自动Dataset              | 否                                            |
| 自动TestCase             | 否                                            |
| 自动EvaluationRun        | 否                                            |
| WP2 PostgreSQL           | PASS                                          |
| Full Unit                | 541 passed                                    |
| Phase1                   | COMPLETE                                      |

最应该记住的三句话：

> **第一，Online Metrics 不是“统计数据库字段”，而是对已经冻结的运行事实语义做聚合。**

> **第二，线上发现 failing Trace 的 Owner 和离线创建 TestCase 的 Owner 必须分开，EvidenceRef 就是两者之间的最小边界。**

> **第三，Evidence identity 和 tenant authorization 是两件事：UUID 负责“指向谁”，project context 负责“你能不能看”。**