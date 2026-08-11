# Stage 4 Phase 0 — PandaProbe 源码审计与 AgentEvalOps 架构映射

## 1. Task ID

```
stage4_phase0_pandaprobe_audit
```

------

## 2. 当前项目背景

当前总体工程路线如下：

- Stage 2.5：多 Agent 编排恢复【已完成】
- Stage 3：最小必要生产化【进行中】
  - 当前正在推进配置、部署与运维相关工作
  - 后续还包括结构化日志 / Metrics、Trace / Span 完善、AgentEvalOps Trace Exporter
- Stage 3.5：Contract Freeze v1【尚未完成】
- Stage 4：AgentEvalOps 最小闭环【准备并行启动】
- Stage 5：Evaluation-Driven Optimization【未来阶段】

AgentEvalOps 基于 PandaProbe 进行二次开发。

AgentEvalOps 的目标不是建设通用 SaaS，而是形成服务 LocalAgent 及未来其他 Agent 的工程质量闭环：

Trace 接入
→ 在线监控
→ 问题定位
→ Trace 回流 Dataset
→ Evaluation Suite
→ Baseline / Candidate
→ Regression Report
→ Critical Case
→ Release Gate

当前 Stage 4 不整体提前。

本任务只启动其中 Phase 0：

- PandaProbe 源码审计
- 架构映射
- 复用边界调查
- 后续改造风险调查
- 判断哪些 Stage 4 工作可以在 LocalAgent Stage 3 / Stage 3.5 完成前并行实施

------

# 3. 本阶段核心目标

通过真实源码审计，准确回答以下问题：

1. PandaProbe 当前真实系统架构是什么？
2. Trace / Span 当前如何产生、传递、存储和查询？
3. Evaluation 当前如何定义、执行和保存？
4. Metrics 当前如何产生、聚合、存储和展示？
5. Monitor 当前依赖哪些数据和服务？
6. Worker 的任务模型、执行模型和生命周期是什么？
7. Database 的模型、Repository / ORM / Migration 边界是什么？
8. PandaProbe 当前有哪些核心 Domain Model？
9. 哪些代码是真正的业务核心，哪些只是 API / UI / Infrastructure？
10. 哪些能力可以直接复用到 AgentEvalOps？
11. 哪些能力适合保留但增加 Adapter？
12. 哪些能力与 AgentEvalOps 目标存在根本冲突，应考虑替换？
13. 哪些模块依赖 PandaProbe 原有业务假设？
14. 哪些模块隐式耦合 Trace Schema、Database Schema 或 Worker？
15. 哪些修改必须等待 LocalAgent Stage 3.5 Contract Freeze？
16. 哪些模块现在即可独立实现，不依赖最终 LocalAgent Contract？

------

# 4. 重点审计范围

必须覆盖以下六个方向。

## 4.1 Trace / Span

调查：

- Trace 数据模型
- Span 数据模型
- Trace / Span ID
- Parent / Child 关系
- Trace 创建入口
- Span 创建入口
- Context 传播
- 生命周期
- Status / Error
- Attribute / Metadata
- Token / Model / Tool 等字段
- Persist 路径
- 查询路径
- API
- UI / Monitor 使用方式
- 是否存在 OpenTelemetry 或其他标准依赖
- 是否存在 Schema Version
- 是否存在外部 Trace Ingestion 能力
- 是否支持来自其他 Agent Runtime 的 Trace

重点判断：

- PandaProbe Trace 是否可以成为 AgentEvalOps Domain Model
- 还是应该仅作为 Infrastructure / Adapter
- 哪些字段绝不能在 Stage 3.5 前冻结

------

## 4.2 Evaluation

调查：

- Dataset
- Test Case
- Evaluation Run
- Evaluation Suite
- Evaluator
- Score
- Result
- Execution Target
- Experiment / Benchmark
- Baseline / Candidate

如果某概念不存在，也必须明确记录“不存在”。

调查真实调用链：

Dataset / Case
→ Execution
→ Evaluation
→ Persistence
→ Query / Report

重点判断：

- Evaluation 是否与某一种 Agent 强耦合
- 是否可以离线运行
- 是否支持 Replay
- Evaluator 与 Agent Execution 是否解耦
- Evaluation Result 是否可以独立于 Trace 存在
- 是否已有可以支撑 AgentEvalOps Phase 2 的抽象

------

## 4.3 Metrics

调查：

- Metric 类型
- Counter / Gauge / Histogram 等支持情况
- Metric 来源
- Trace → Metrics 是否存在映射
- 聚合发生位置
- Label / Dimension
- Persistence
- 时间窗口
- 查询 API
- Monitor 消费方式
- 是否依赖 Worker
- 是否依赖定时任务

重点判断：

- Runtime Metrics 能否直接接入
- Metrics 是否过度依赖 PandaProbe 内部 Trace Schema
- Metric 与 Monitor 是否存在职责混合

------

## 4.4 Monitor

调查：

- Monitor API
- Dashboard / 页面
- 查询 Service
- 数据来源
- Trace 查询
- Metrics 查询
- Error 查询
- 时间范围
- Filtering
- Aggregation
- Drill-down

重点判断：

- Monitor 是纯查询展示层还是包含业务计算
- Production Monitor 能否建立在现有模块上
- 哪些 Monitor 能力需要等 LocalAgent Trace Contract 后才能接入

------

## 4.5 Worker

调查：

- Worker 类型
- Worker 启动入口
- Job / Task 模型
- Queue
- Polling
- Async / Sync
- Retry
- Failure handling
- Timeout
- Cancellation
- Concurrency
- Shutdown
- Task ownership
- Idempotency
- Database transaction
- 定时任务

重点判断：

- Evaluation 是否适合复用现有 Worker
- Metrics 是否依赖 Worker
- Worker 是否能够支撑离线 Evaluation
- Worker 是否与 PandaProbe 原业务强耦合

------

## 4.6 Database

调查：

- Database 技术
- ORM / SQL Layer
- Migration
- Repository
- Transaction
- Schema
- Trace 表
- Span 表
- Evaluation 表
- Metric 表
- Dataset 表
- Worker / Job 表
- 外键关系
- Index
- JSON / JSONB 字段
- Version 字段

重点判断：

- 哪些 Schema 可以复用
- 哪些 Schema 属于 PandaProbe 历史负担
- 哪些 Schema 不应在 Stage 3.5 前绑定 LocalAgent 字段

------

# 5. 必须调查的横向问题

除上述模块外，还必须调查：

## 5.1 Composition Root

找到：

- 应用启动入口
- Dependency Injection
- Service 创建
- Repository 创建
- Worker 创建
- Router 注册
- Config 加载

明确真正的 Composition Root。

## 5.2 Domain Boundary

识别：

- Domain
- Application
- Infrastructure
- API
- UI

如果当前仓库没有明确分层，则基于真实依赖关系描述，不允许根据目录名直接假设。

## 5.3 Data Flow

至少还原以下真实数据流，如果存在：

### Trace

Producer
→ Ingestion
→ Service
→ Persistence
→ Query
→ Monitor

### Evaluation

Dataset
→ Case
→ Target
→ Execution
→ Evaluator
→ Result

### Worker

Job creation
→ Queue / DB
→ Worker
→ Execution
→ Result
→ Retry / Failure

------

# 6. AgentEvalOps 目标映射

必须建立 PandaProbe → AgentEvalOps 映射矩阵。

每个重要模块至少分类为：

- KEEP：基本可直接复用
- ADAPT：主体可复用，但必须增加 Adapter / Boundary
- REFACTOR：核心有价值，但职责或依赖需要重构
- REPLACE：与目标冲突，建议替换
- REMOVE：AgentEvalOps 不需要
- UNKNOWN：证据不足，需要 Codex 决策

每项必须给出：

- 当前源码位置
- 当前职责
- 当前依赖
- 分类
- 证据
- 风险
- 是否依赖 Stage 3.5 Contract Freeze
- 是否可立即并行

注意：

该分类只是 Scout 建议，不是最终架构决策。

最终 KEEP / ADAPT / REFACTOR / REPLACE 决策由后续 Codex Architecture Decision 阶段完成。

------

# 7. 当前允许并行开发的调查目标

本次特别需要判断以下模块能否在 Stage 3 / Stage 3.5 完成前独立建设。

候选：

- Dataset Domain Model
- Test Case Domain Model
- Evaluation Suite
- Evaluator Interface
- Evaluation Result
- Replay Execution Target
- Fixture Execution Target
- Baseline
- Candidate
- Regression Comparison
- Regression Result
- Critical Case
- Release Gate 的纯决策核心

以及：

- Trace Ingest Port
- LocalAgent Adapter Boundary

对于最后两项：

只允许判断抽象边界。

不得提前冻结 LocalAgent Trace Schema。

------

# 8. 明确禁止事项

本阶段是 Audit，不是 Implementation。

禁止：

1. 不得修改 PandaProbe 生产代码。
2. 不得进行架构重构。
3. 不得创建新的正式 Domain Model。
4. 不得修改 Database Schema。
5. 不得执行 Migration。
6. 不得修改 API Contract。
7. 不得修改 Trace Schema。
8. 不得定义最终 Version Fingerprint。
9. 不得实现 LocalAgent Trace Adapter。
10. 不得实现 LocalAgent HTTP Execution Target。
11. 不得提前绑定 LocalAgent Runtime Event。
12. 不得因为发现设计问题而自行扩大修改范围。
13. 不得把推测描述成已经实现的事实。
14. 不得仅根据 README 判断实现状态。
15. 不得仅根据文件名或类名判断实际职责。
16. 不得为了“改善代码”顺手进行格式化或 cleanup。

除 `.ai/handoff/stage4_phase0_pandaprobe_audit/` 下的 Handoff 文档外，不应产生源码 Diff。

------

# 9. 真实性要求

所有结论必须严格区分：

- SOURCE_CONFIRMED
  已通过源码确认
- TEST_CONFIRMED
  已通过真实测试或执行确认
- DOC_ONLY
  仅文档声明，未从源码确认
- INFERRED
  根据源码关系推断
- UNKNOWN
  当前证据不足

禁止把：

- README 描述
- TODO
- 注释
- Roadmap
- 未调用代码
- 测试 Fixture

直接描述成生产能力。

------

# 10. 必须执行的验证

至少执行：

1. `git status`
2. `git diff`
3. 查看当前 branch / HEAD
4. 阅读仓库根目录 `AGENTS.md`
5. 识别项目运行 / 测试方式
6. 检查现有测试目录
7. 在不修改环境和生产数据的前提下，尽可能执行现有核心测试

如果因为：

- 环境依赖
- Database
- Docker
- 外部服务
- Secret
- 网络

无法执行测试，必须记录真实原因。

不得为了让测试通过而修改源码。

审计结束再次执行：

- `git status`
- `git diff`

确认生产代码没有被修改。

------

# 11. 输出文件

Scout 最终必须生成：

```
.ai/handoff/stage4_phase0_pandaprobe_audit/10_zcode_audit.md
```

该文件必须能够让一个没有读取 Scout 聊天记录的 Codex 继续工作。

不得依赖聊天上下文。

------

# 12. 验收标准

Phase 0 Scout 完成的最低标准：

- PandaProbe 主要目录和入口已定位
- Composition Root 已定位
- Trace / Span 调用链已还原
- Evaluation 调用链已还原或明确不存在
- Metrics 调用链已还原或明确不存在
- Monitor 调用链已还原
- Worker 生命周期已还原
- Database 主要模型及关系已调查
- 核心 Domain Boundary 已识别
- PandaProbe → AgentEvalOps Mapping 已生成
- Stage 3.5 依赖矩阵已生成
- 可并行模块已识别
- 关键架构风险已列出
- UNKNOWN 项已列出
- 没有修改生产代码
- Git Diff 已核实
- 所有结论附带源码路径 / 符号 / 调用链证据

------

# 13. 后续阶段

本任务属于 H 级任务，后续严格按以下顺序执行：

1. ZCode / DeepSeek Scout & Audit
2. Codex Architecture Decision / Critical Implementation Decision
3. ZCode Execute / Test / Documentation
4. Codex Final Gate

当前只执行第 1 阶段。

不得提前开始第 2～4 阶段。