# AgentEvalOps 仓库级 Coding Agent 工作规范

本文件适用于 AgentEvalOps 仓库根目录及全部子目录。子目录的 `AGENTS.md` 可补充更具体的规则，不得放宽本文件的安全、数据隔离、Scope、Authority 和验证要求。**当前正式评测后端主线是 Go，不是历史 PandaProbe Python/Celery 后端。** 历史名称、旧实现和文档可用于追溯，不能据此认定旧路径仍是生产权威。

## 1. 项目定位与架构边界

AgentEvalOps 是面向 AI Agent / Agent Harness 的评测、回归、观测与质量门禁平台。主线使用 Go（`net/http`、`context`、`pgx/v5`、PostgreSQL）实现 API 与 Worker，使用 Next.js/TypeScript 实现前端。

- **AgentEvalOps 拥有 Evaluation Truth**：版本化 Dataset / Case / Suite / Metric / Evaluator、冻结 EvaluationRun、ExecutionAttempt、EvaluatorWork、不可变 EvaluationResult、Regression / Comparison / Gate。
- **AgentCore / LocalAgent 拥有 Execution Truth**：Run/Step/Tool 的执行、预算、审批、副作用、Journal、恢复和终态。AgentEvalOps 经受控 ExecutionTarget、Trace 或版本化 Evidence 消费执行事实，不反向改写执行权威。
- `backend/` 下仍存在旧 Python/FastAPI/PandaProbe 代码及 Alembic 迁移。**不能把旧 Python evaluator/Celery 路径视为 Go 生产链的自动 fallback 或已上线能力**。是否仍有独立消费者，应以当前接线、部署和测试判断。
- 当前没有因为采用 Go 就天然获得生产 cutover、长时容量或多实例正确性；这些必须单独提供实测证据。

## 2. 事实依据与任务优先级

1. 当前用户明确指令与当前获授权任务的 Scope / Acceptance Criteria。
2. 当前真实源码、Git diff、配置、migration 和本轮真实执行的测试结果。
3. 仍有效的正式 Architecture / Contract / Owner 决策及经过验证的发布契约。
4. README、旧 AGENTS/CLAUDE、历史 Stage Handoff、PandaProbe 说明与注释仅供参考；发现漂移应明确记录，不可当成生产能力证明。

开始工作前阅读本文件、当前任务相关 Handoff 和适用的子目录规则，并核对 `git status`、源码入口及相关调用链。**规划为 TARGET、已批准为 APPROVED、已实现为 IMPLEMENTED、通过最终 Gate 是不同状态**。不要以设计文档替代代码与测试。

## 3. Repository Map（当前 Go 主线）

| 路径 | 长期职责 |
| --- | --- |
| `go-backend/cmd/api/` | Go API 进程入口 |
| `go-backend/cmd/worker/` | Go Worker 进程入口 |
| `go-backend/internal/bootstrap/` | API / Worker 组件装配、依赖与生命周期 |
| `go-backend/internal/httpapi/` | HTTP 路由、输入验证、项目认证/授权与协议适配 |
| `go-backend/internal/catalog/` | 版本化评测目录与发布语义 |
| `go-backend/internal/evaluation/` | Run / Attempt / Work / Result 等评测内核领域模型 |
| `go-backend/internal/worker/` | 执行目标、任务领取、租约、判分推进与退出协调 |
| `go-backend/internal/agentquality/`、`internal/metric/` | 已接线的质量评测器、指标定义与适用性规则；不能把目录中的 ContractOnly 指标算成实现 |
| `go-backend/internal/observation/` | Trace / Observation 的严格校验与受控数据投影 |
| `go-backend/internal/postgres/` | PostgreSQL 持久化、事务、Claim/Fencing、身份与结果读取 |
| `go-backend/internal/decision/` | Baseline/Candidate 比较、回归及发布门禁的领域逻辑 |
| `go-backend/testdata/` | 公开/合成的固定契约向量与测试资源；不得混入受控 Holdout |
| `frontend/` | Next.js / TypeScript 产品前端；当前 Go 产品 API 通过相应 API client 接入 |
| `backend/migrations/` | 当前仍使用的 Alembic 历史与增量数据库迁移，包括 Go 领域表 |
| `backend/app/` | 历史 Python 后端/部分离线参考实现；是否为当前调用路径必须查证 |
| `.ai/handoff/` | 审计、架构决策、实施和 Gate 交接材料；可按 Stage/WP 命名 |

以上为导航而非包内实现保证。查看实际文件、模块导入和 API / Worker bootstrap 再决定修改位置，不得盲目新建同名目录。

## 4. 入口、执行与 Authority

- API：`go-backend/cmd/api/main.go` → `internal/bootstrap/api.go` → `internal/httpapi/`，项目级 API 路由在当前 `server.go` 中核对。
- Worker：`go-backend/cmd/worker/main.go` → `internal/bootstrap/worker.go` → `internal/worker/`。API 和 Worker 是不同进程角色，共享 Go Domain 与 PostgreSQL 权威数据，不共享进程内锁作为分布式正确性依据。
- 当前 ExecutionTarget 通过明确接口接入 Fixture、LocalAgent HTTP 或受控业务 Target。ExecutionAttempt 的 `UNKNOWN`、Retry、租约与 Worker Epoch 必须保持独立语义；不能因 HTTP 超时推断外部执行一定失败或没有副作用。
- EvaluationRun 冻结评测输入，Attempt 记录执行历史，EvaluatorWork 可领取/恢复判分，EvaluationResult 是追加式事实；Gate 必须绑定可比较的基线、候选、证据和冻结策略。
- Trace 是可能丢失的诊断侧通道，**不是** Journal、完整执行轨迹或可靠评测证据的替代。正确性指标需要相应版本化 Evidence 和明确 coverage；缺失必要证据不得补造或默认 PASS。
- 当前支持的具体指标以 Go bootstrap 实际装配、evaluator 实现及集成测试为准。旧 Python evaluator、指标目录名称或已有接口均不构成 Go canonical `SUPPORTED` 证据。

## 5. 环境、配置与依赖

- Go 版本以 `go-backend/go.mod` 为准；Go 依赖由 `go.mod`/`go.sum` 管理。不要在根目录创建第二个 Go module，不为现有 Go 服务新引入 Python/Celery 执行核心。
- API/Worker 配置、身份、数据库连接参数以当前 bootstrap/config 实现为准；不要臆造 `.env` 变量。前端配置要核对 `frontend/src/lib/api/` 和 `NEXT_PUBLIC_GO_PRODUCT_API_URL` 等当前实际消费点。
- 前端使用 Next.js、TypeScript；执行包管理命令前确认当前 `package.json`、锁文件和现有脚本。不能直接把历史 PandaProbe 前端接线当成 Go 产品现状。
- 数据库迁移仍位于 `backend/migrations/versions/`，由项目现有 Alembic 工作流管理；不要因为 Go 主线就自行引入一套并行迁移 Ledger。
- `docker-compose.go-rehearsal.yml` 可用于 Go API / Worker / 前端受控演练，**Compose 存在不代表已完成真实生产 cutover 或 SLO 验收**。
- 不得提交或输出生产凭据、内部 Trace/Prompt/工具参数、受控评测答案或私有业务数据。已有 `.env*`、数据库和工作区状态一律保留。

## 6. 最小开发与测试命令

在仓库实际根目录与目标模块核对后执行，优先做与当前 diff 直接相关的验证：

```powershell
cd go-backend
go test ./internal/evaluation ./internal/agentquality ./internal/metric ./internal/observation ./internal/decision -count=1
```

- 根据修改路径进一步缩小 `go test ./internal/<affected-package> -run '<focused-test>' -count=1`；只在任务明确要求 Final Gate 或风险确有必要时扩大覆盖。
- 需要真实 PostgreSQL 时，使用仓库已有隔离 integration fixture、`-tags=integration` 和受限 API role；核对 `G1_TEST_POSTGRES_URL` 等测试门禁，只能指向明确隔离的本机测试数据库。没有可用条件标记 `BLOCKED/NOT_EXECUTED`，不能退化为 fake 后宣称跨进程持久化通过。
- `go test -race` 需要可用 CGO 编译链；Windows 不具备条件时，可在受控 Linux 容器验证，并明确报告未执行/实际结果。`-race` 不能替代多进程、PG 锁争用和恢复测试。
- 涉及前端时，先核对锁文件与脚本，再运行对应 TypeScript lint/typecheck/test；涉及 Go API/Frontend 契约需验证两端。
- 跨仓契约变更必须使用真正的 Python AgentCore producer 与 Go consumer 验证一致性，不能只验证各自仓库的 golden。真实 HTTP/PG 集成与公开合成 fixture 的证据须与纯 unit test 区分。
- 文档任务做路径、链接、契约的必要静态检查即可；不得为获得漂亮通过数重复跑已通过的大规模回归。

## 7. PostgreSQL、Worker 与一致性规则

- PostgreSQL 是评测领域的持久化权威；`pgx/v5`、显式 SQL 和事务边界以当前 repository 实现为准。Redis 可作缓存/限流，不能默认成为评测事实源或可靠任务队列。
- EvaluationRun/Attempt/Work/Result 的 identity、版本、唯一约束、lease、renew、owner epoch/fencing 与 terminal 状态应由存储约束和事务共同保证；不能仅靠 Go mutex、进程内缓存或一次 `go test -race` 宣称多实例安全。
- Worker 要维持有界并发、Claim/续租、扫描恢复、Drain 和退出期限；新 API 或 Job 需明确超时、取消、UNKNOWN、重试与幂等行为。不能将重复投递等同于新的业务执行授权。
- 修改 Schema 时新增 Alembic migration，检查 Domain、SQL、读写路径、索引、权限、兼容性和测试。禁止重写既有 migration 或修改历史已封存的结果来修复错误。
- 涉及 API Role / machine credential / project scope，先读现有认证授权链。Project/Organization 权限来自服务端可信身份，不得依赖用户提交的 `project_id` 或 Trace attributes 自行授权。
- Go `context.Context` 应传播 deadline、cancel 和请求级元数据；不要用 `WithValue` 隐藏决定核心业务行为的必填参数，或将取消误当成远程副作用回滚。
- 不为展示技术储备盲目引入 Kafka、NATS、RabbitMQ、Temporal、Kubernetes 或第二数据库/队列；新基础设施需要说明新 Owner、状态、故障恢复、验收依据和比现有机制更好的理由。

## 8. AgentCore 与 AgentEvalOps 的跨仓契约

- AgentCore = Execution Truth；AgentEvalOps = Evaluation Truth。任何 Trace、Observation、EvaluationResult 或 Report 都不允许改变 AgentCore 的工具调用、审批、Run 终态和执行恢复决策。
- 修改 Trace/Evidence/ExecutionTarget 前先核验双仓当前 producer/consumer、版本、fingerprint、路由、授权、payload digest 与历史 reader；不要只修改单边常量或放宽严格 decoder。
- `contract_version`、schema、source、canonical serialization、digest 与 idempotency 各有明确语义；未知/不支持契约应拒绝或标记不可评测，不能填默认值假装成功。
- 评测侧结果需绑定 frozen dataset/case、execution attempt、evaluator/prompt/model/metric 版本和所用 evidence；旧结果不可覆盖更新。Baseline/Candidate 不可比时应阻断门禁。
- Legacy Python 过程评测/记忆 evaluator 可以作为离线行为参考，但不能无审批隐式调用为 Go 判分 fallback。

## 9. 前端与文档规则

- Next.js 前端通过既有 API client、认证上下文和项目路由访问 Go Product API；修改之前检查实际消费模块，不混用历史 Python 路由和 Go 项目级路由。
- 更新用户可见状态、UNKNOWN、未支持指标、缺失 Evidence 或 Gate 状态时，前端不得把 `null`/未知偷偷显示为成功或零成本。
- 相关协议变更同步更新当前有效的 OpenAPI、调用方、测试与配置/运维文档；不以旧 README 或阶段报告覆盖真实接线。
- 遵循邻近代码的 `gofmt`、`go vet`（按变更需要）、Go 错误处理与 `context` 约定；前端遵循仓库 ESLint/格式化规则。不要对无关代码做全库 format 或升级依赖。

## 10. Git、工作区与受控 Holdout 安全

### 受控 Holdout 源码与开发访问边界

- 新 Holdout 的生成器、场景、evaluator-side GT、fixture 和答案映射均为 `HOLDOUT_RELEASE_ONLY / operator-only`，不得进入普通 Git 提交、远端或开发侧输出；受控源码保存在仓库外的独立 operator 工作区。
- 当前完整工作区可能含 `.ai/` 受控证据，禁止将其提供给 Candidate 开发进程。开发侧仅获得预生成的 `DEVELOPMENT_VISIBLE` 安全投影、必要开发代码及对应 scoped credential，不得获得 Holdout 源码、operator 目录或 Holdout/Calibration 凭据。
- Git ignore、迁出目录和净化环境变量不等同于 OS 级隔离。若无法落实开发进程的可读范围，必须先隔离运行环境或仅提供安全投影；未核实访问边界时，不得声称新 Holdout 未曝光。

开始修改前执行并理解：

```bash
git branch --show-current
git rev-parse HEAD
git status --short --branch
git diff
git diff --cached
```

- 用户已有改动一律保留；不得覆盖、`reset`、checkout 掉、stash、revert 或顺手纳入提交。
- 未经明确授权，不 commit、不 push、不创建 PR；禁止破坏性 Git 操作。用户明确授权的 Breaking Change 必须遵循其 Scope 并留下迁移/回滚记录。
- 只修改当前任务允许的文件；不借机扩大 API、Schema、Authority 或依赖，不为未来 Stage 提前实现未批准的能力。
- 发现架构冲突应记录路径、符号、调用链、diff/test 和真实影响，再交由当前任务指定的决策方处理。

## 11. Task Risk、Handoff 与验收

- **L**：低风险、局部易验证，直接实现并跑目标测试。
- **M**：跨文件但 Owner/Contract 已明确，实施后进行必要 Review。
- **H**：涉及 Architecture、Owner、Contract、State Machine、持久化一致性、并发、安全、跨仓契约、生产 cutover 或重大重构；原则上先源码审计/架构决策，再实施/测试，最后 Final Gate。
- 阶段 WP 的实施与最终 Gate 分开：设计批准不等于已实现；按用户约定，完成一个 WP 后停止，不自动开始下一个 WP。
- 大型任务使用 `.ai/handoff/<task_id>/`，根据任务需要产出审计、Decision、Implementation、Review 文件；不要为了凑齐 `00/10/20/30/40` 创建空文档。Handoff 不替代源码/测试。
- 变更兼容/废弃规则时记录 `BREAKING_CHANGE`、`OLD_BEHAVIOR`、`NEW_CANONICAL_BEHAVIOR`、`COMPATIBILITY_REQUIRED` 和具体删除/归档项；历史已发布结果、schema、Holdout 仍以明确边界处理，不因“删旧代码更快”擅自破坏。

宣布完成前确认：修改文件仅在当前 Scope 内；任务相关单元/契约/真实 PG 集成测试已根据风险执行；失败、skip、warning、超时、BLOCKED 均如实记录；没有把旧 Python 成果、受控模拟或测试桩当作 Go 正式能力；没有泄露敏感或 Holdout 数据。

## 12. 长时间运行任务与后台进程

当任务启动了仍属于当前任务范围的长时间运行命令、测试、评估、服务或后台进程时，不得仅因进程成功启动就结束任务。只要工具提供可继续查询的 process/session handle，就应继续轮询其状态，直到进程退出、达到明确终态、需要用户介入或出现无法继续的 blocker。不得只回复“已在后台运行”就结束当前交付。

## 13. 代码组织与可维护性规范（跨阶段长期适用）

**正式代码按业务语义与稳定职责组织，不按 Stage、WP、Sprint 或开发时间组织。开发阶段是项目管理维度，不是生产架构维度。**

1. **禁止阶段式生产目录或命名**：不得新增 `stageXX/`、`wpXX/`、`phaseXX/` 等生产包/目录，也不得以阶段编号命名正式模块、类、接口、主要函数、测试或长期维护脚本。协议真实兼容版本如 `trace_export/v2/`、`TraceEnvelopeV2` 例外。
2. **优先现有领域 Owner**：新增功能前先追踪调用链、职责和依赖。属于既有模块的职责就在原语义模块实现；只有出现真正独立、内聚的职责时才创建新包，避免重复实现、第二 Authority、循环依赖、无意义 wrapper。
3. **避免巨型模块与过度拆分**：不得因禁止 Stage 目录，就把全部新代码塞入现有大文件。按照 Contract、Domain、Application、Transport、Persistence 等真正边界合理拆分；不要为单个简单函数创建多层空抽象。
4. **测试、脚本、配置同样语义命名**：新增测试使用行为或能力名称，例如 `TestTraceHTTPReceipt`，不要用 `TestStage14WP1...`。测试仍遵循仓库实际目录规范与相应 Gate。
5. **历史目录增量治理**：历史 Stage 命名代码不是新功能目录模板；若本次任务涉及它，可在授权范围内渐进迁移并处理导入/兼容。不为整洁进行无关全仓搬迁或破坏历史契约。
6. **阶段名仅用于研发工件**：`.ai/handoff/stageXX_.../`、审计、决策与 Gate 报告可保留阶段编号；正式协议版本与产品域模型使用自身稳定语义。
7. **交付时检查组织质量**：确认新文件放置理由、跨层依赖、职责重复、单文件膨胀、测试发现路径以及是否引入阶段命名生产代码。新增不合理 Stage/WP 结构视为未满足可维护性要求。
