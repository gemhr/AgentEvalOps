# PandaProbe 仓库级 Coding Agent 工作规范

本文件适用于仓库根目录及全部子目录。子目录若以后增加更具体的 `AGENTS.md`，仅可细化本文件，不得放宽安全、Scope 与验证要求。

## 1. Repository Overview

PandaProbe 是一个用于 AI Agent tracing、evaluation、monitoring 与 debugging 的开源工程平台。本仓库是 monorepo，主要运行组件为：

- `backend/`：Python 3.12+、FastAPI、Pydantic、async SQLAlchemy、PostgreSQL、Alembic、Redis、Celery/Beat、LiteLLM；依赖由 `uv` 和 `backend/uv.lock` 管理。
- `frontend/`：Node.js 20、Next.js 16 App Router、React 19、TypeScript、TanStack Query；依赖由 Yarn 和 `frontend/yarn.lock` 管理。
- Docker Compose：编排 `app`、`worker`、`beat`、`frontend`、PostgreSQL 16 与 Redis 7。
- `docs/`：Mintlify 文档站；文档中的能力声明必须再由当前源码或测试确认。

仓库根目录不定义 Python package；`backend/` 是唯一的 Python/uv project。不要在根目录再次运行 `uv init` 或创建第二套依赖声明。

## 2. Source of Truth

按以下顺序判断目标与事实：

1. 当前任务文件是本次目标、Scope 与 Acceptance Criteria 的权威来源；H 级 Handoff 中通常为 `.ai/handoff/<task_id>/00_task.md`。
2. 当前真实源码、当前 Git diff 和本轮实际测试结果是实现状态的权威来源。
3. 正式冻结的 Architecture / Contract 文档是其对应长期设计边界的权威来源。
4. `README.md`、`CLAUDE.md`、其他说明、注释与 `10/20/30/40` Handoff 仅提供上下文，不能覆盖源码和测试事实。

Handoff 是阶段性交接材料，不是事实代理。每个 Agent 开始工作时必须重新阅读本文件、当前任务及当前阶段 Handoff，并重新检查源码、`git status` 和 `git diff`。发现文档与实现冲突时，保留证据并报告，不得靠猜测消解冲突。

## 3. Repository Map

- `backend/app/main.py`：FastAPI composition root、lifespan、中间件、异常处理与 `/v1` router 注册。
- `backend/app/api/`：HTTP router、依赖、middleware、request context 与 rate limit；route 负责协议适配和编排。
- `backend/app/services/`：应用用例与业务编排。
- `backend/app/core/`：domain entity、repository protocol、evaluation metric 与 cadence 逻辑。
- `backend/app/infrastructure/`：PostgreSQL repository、Redis、auth adapter、Celery task、LLM provider 等外部适配器。
- `backend/app/registry/`：`Settings`、常量、安全与异常等横切配置。
- `backend/migrations/`：Alembic 环境及不可变的历史 migration。
- `backend/tests/unit/`、`backend/tests/integration/`：后端单元和真实 PostgreSQL/Redis 集成测试。
- `frontend/src/app/`：Next.js App Router 页面、layout 与路由边界。
- `frontend/src/components/`：provider、feature 和 UI 组件。
- `frontend/src/lib/`：API client、auth、query key 与共享工具。
- `frontend/src/__tests__/`、`frontend/e2e/`：Jest/jsdom 单元测试与 Playwright E2E。
- `docs/`：Mintlify `.mdx` 内容、`docs.json` 与 OpenAPI 快照。
- `.ai/handoff/`：大型 H 级任务的审计、决策、执行和 Final Gate 交接材料。

## 4. Entry Points and Composition

- API：从 `backend/` 运行 `uvicorn app.main:app`；`backend/app/main.py` 是 composition root，`backend/app/api/v1/router.py` 汇总 `/v1` routes。
- Worker：`celery -A app.infrastructure.queue.celery_app worker`；任务实现在 `backend/app/infrastructure/queue/tasks.py`。
- Scheduler：同一 Celery app 以 `beat` 启动，周期任务配置位于 `celery_app.py`。
- Frontend：`frontend/src/app/layout.tsx` 是根 layout，`frontend/src/app/page.tsx` 是根页面；认证后的项目页面位于 `org/[orgId]/project/[projectId]/`。
- Migration：从 `backend/` 使用 `alembic.ini` 与 `migrations/env.py`，metadata 来自 `app.infrastructure.db.models.Base`。
- Docker API 镜像入口为 `backend/scripts/docker-entrypoint.sh`，随后执行 `app.main:app`。该脚本会尝试 `alembic upgrade head`，但当前实现会在 migration 失败后继续启动；不得仅凭容器已启动推断 migration 成功。

API 存在两类认证上下文：management plane 使用 Bearer JWT；data plane 可使用带项目 header 的 JWT 或 `X-API-Key` + `X-Project-Name`。修改 route 前先核对 `backend/app/api/dependencies.py` 中的 `get_api_context`、`get_data_plane_context` 与 `require_project`，不得绕过既有组织/项目隔离。

## 5. Environment and Configuration

- 后端最低 Python 版本由 `backend/pyproject.toml` 定义为 `>=3.12`；当前 Docker 镜像使用 Python 3.13.2。后端安装使用 `uv sync --frozen`。
- 前端 Docker 使用 Node.js 20，安装使用 `yarn install --frozen-lockfile`。
- 后端配置唯一入口是 `backend/app/registry/settings.py::Settings`。当前 Pydantic 配置直接读取工作目录下 `.env` 和 `.env.development`；Compose 另通过 `env_file` 注入 `.env.${APP_ENV}`。不要假设 host 运行时会由 Pydantic 自动选择任意 `.env.${APP_ENV}`。
- 前端本地配置模板为 `frontend/.env.example`；`NEXT_PUBLIC_*` 值会进入客户端或在 build 时固化，尤其 `NEXT_PUBLIC_AUTH_ENABLED` 不是容器运行时开关。
- 复制 `.env.example` 创建本地文件，不覆盖用户已有 `.env*`。不得提交 secret、真实 credential、token、私钥或生产连接串，也不得在日志、测试 fixture、Handoff 中泄露它们。
- CI 配置位于 `.github/workflows/`；Backend jobs 当前使用 Python 3.12，Frontend jobs 使用 Node.js 20。判断 gate 时读取当前 workflow，不要只依据 README badge。

## 6. Confirmed Development Commands

根 `Makefile` 是开发命令入口；其环境变量赋值和 `start.sh` 使用 POSIX shell 语法，在 Windows 上应使用兼容 shell。

```bash
make install
make dev
make worker
make up
make down
make lint
make format
make typecheck
make test-unit
make test-integration
make test-all
make migration msg="describe change"
make migrate
```

常用最小命令：

```bash
cd backend && uv sync --frozen
cd backend && uv run --group test pytest tests/unit/test_traces.py::test_name -v
cd backend && uv run --group dev ruff check app/ tests/
cd frontend && yarn test src/__tests__/lib/api/traces.test.ts
cd frontend && yarn lint
cd frontend && yarn typecheck
cd frontend && yarn format:check
```

`make test-integration` 会启动 `docker-compose.test.yml` 的 PostgreSQL `:5433` 和 Redis `:6380`，执行后端 integration tests，并以 `down -v` 清理测试数据卷。不得把集成测试指向开发或生产数据库。自托管公开镜像使用 `./start.sh` 与 `docker-compose.yml`；源码热更新开发栈使用 `make up` 与 `docker-compose.dev.yml`，两者不要混淆。

## 7. Testing Rules

- 只改后端纯逻辑：至少运行相关 `backend/tests/unit/`；涉及共享 domain/service 时扩大到相邻单元测试。
- 改 API、repository、SQLAlchemy model、Redis、Celery 调度或跨进程数据流：除相关 unit tests 外，运行对应 `backend/tests/integration/`；需要完整隔离时使用 `make test-integration`。
- 改前端 API/auth/provider/query/component：运行对应 Jest 测试，并执行 `yarn lint`、`yarn typecheck`、`yarn format:check`。
- 改路由导航、登录流程或关键用户路径：运行对应 Playwright E2E；首次运行前执行 `make frontend-e2e-install`。
- 跨 backend/frontend、公共协议、migration、依赖或发布路径的修改，应运行受影响两侧测试；风险足够高或任务要求时运行 `make test-all`。
- Integration fixture 使用真实 PostgreSQL/Redis、Celery eager mode、`TRUNCATE ... CASCADE` 和 Redis `FLUSHDB` 隔离。添加测试前先读 `backend/tests/integration/conftest.py`，不要改成会遮蔽 worker 独立事务的 rollback-only fixture。
- 不得删除、skip、弱化断言或修改测试来迎合错误实现。若测试无法执行，明确记录 `NOT_EXECUTED`、原始原因、未覆盖范围；不得声称通过。

纯文档任务不必默认运行完整测试，但必须静态核对所写命令、路径和技术事实。

## 8. Database and Migration Rules

- PostgreSQL schema 由 `backend/app/infrastructure/db/models.py` 的 async SQLAlchemy ORM 与 `backend/migrations/versions/` 的 Alembic 历史共同约束；repository 位于 `infrastructure/db/repositories/`，对上返回 `core/` entities。
- 修改字段时同时核对 ORM model、domain entity、repository mapping、Pydantic schema/API、查询索引和测试；不得只改其中一层。
- FastAPI 的 `get_db_session` 在请求成功后 commit、异常时 rollback；worker 使用独立 `NullPool` session 并显式 commit。现有部分 service 也包含显式事务边界，修改前先还原真实调用链，不得机械新增或删除 commit。
- Schema 变更必须新增 Alembic migration 并验证 upgrade 路径。禁止删除、重写、重排既有 migration，禁止在未确认兼容策略时改变历史 revision。
- `make migration`/`make migrate` 默认指向本地 PostgreSQL `:5432`。执行前必须确认目标数据库；不得为调试修改生产 schema，不得对用户数据运行 destructive migration。

## 9. Worker and Async Rules

- Celery 使用 Redis 作为 broker/result backend；同步 task 通过 `asyncio.run()` 进入 async SQLAlchemy，worker engine 使用 `NullPool` 避免跨 event loop 复用连接。
- 全局 soft/hard time limit 为 300/360 秒，evaluation task 单独覆盖为 3300/3600 秒；重试次数和 delay 由各 task decorator 单独定义。修改任务时必须逐项核对 retry、timeout、异常状态更新与重复执行后果，不能套用统一假设。
- 检查 task ownership、enqueue 前后的 commit 顺序、idempotency、Redis lock TTL、并发、部分成功、quota rollback、shutdown 和 heartbeat。不得假定 Celery retry 等价于 exactly-once。
- 当前未发现统一的业务 cancellation contract；涉及取消、撤销或 shutdown 语义时必须先审计生产调用链并升级为 H 级，不得自行补定义。
- API request session 与 worker session 是不同事务边界；测试和实现都必须保留这种可见性。

## 10. Frontend Rules

- 资源请求统一经过 `frontend/src/lib/api/client.ts` 和 `src/lib/api/` 的资源模块；不要绕开 interceptor 手工拼接认证/组织/项目 header。
- Provider 顺序具有行为含义：`AuthProvider -> PostHogProvider -> ToastProvider -> QueryClientProvider -> ApiConfigProvider`。调整前必须补充针对初始化、401 refresh 与 cache 清理的测试。
- TanStack Query key 统一来自 `frontend/src/lib/query/keys.ts`，新增或修改 key 时同步更新其测试，禁止散落 inline key。
- 项目级页面遵循 `org/[orgId]/project/[projectId]/` 上下文；认证开关同时核对 middleware、provider 与 build-time env 行为。

## 11. Coding Conventions

- 优先遵循邻近代码与现有工具配置，不引入另一套风格。
- Backend：Ruff line length 119，启用 `E/F/B/ERA/D` 与 Google docstring convention；保持 async I/O、typed Pydantic/domain entity、service/repository 分层和 structlog 事件式日志。
- Route 只做协议转换和用例编排；业务规则放 service/core，外部系统访问放 infrastructure。API key 只存 hash，不记录 raw key。
- Frontend：TypeScript、ESLint flat config、Prettier、`@/* -> src/*` alias；保持 API resource module、provider 与 query-key 约定。
- 不做与任务无关的全库 format、cleanup、依赖升级或重构。

## 12. Git / Worktree Safety and Scope

开始修改前必须执行并理解：

```bash
git branch --show-current
git rev-parse HEAD
git status --short --branch
git diff
git diff --cached
```

- 用户已有改动一律保留；不得覆盖、`reset`、checkout 掉、stash、revert 或顺手纳入提交。
- 未经明确授权，不 commit、不 push、不创建 PR。禁止 destructive Git 操作，除非任务明确要求且目标已核实。
- 只修改当前 Scope 允许的文件。不因附近代码存在问题而扩大重构，不擅自扩大 public API、改变 schema/contract、引入大型依赖或实现未来阶段。
- 新发现的架构问题应记录路径、符号、调用链、diff/test 证据并升级，不得以推测代替实现事实。

## 13. Task Risk and Handoff

- **L**：低风险、需求明确、易于测试；通常由 ZCode / DeepSeek 实施。
- **M**：跨文件或存在一定设计风险；通常由 ZCode / DeepSeek 实施、Codex Review。
- **H**：涉及 Architecture、Owner、Scope、Contract、state machine、concurrency、cancellation、timeout、lifecycle、persistence、compatibility、public API、重大重构或复杂跨模块行为。顺序为 Scout/Audit -> Codex Architecture Decision 或关键实现 -> ZCode Execute/Test/Documentation -> Codex Final Gate。

大型 H 级任务使用 `.ai/handoff/<task_id>/`，典型文件为 `00_task.md`、`10_zcode_audit.md`、`20_codex_decision.md`、`30_zcode_execution.md`、`40_codex_review.md`。只有 `00_task.md` 定义当前任务目标、Scope 与 Acceptance Criteria；其余文件是阶段性交接，不替代当前源码、Git diff 和测试。

## 14. Verification Before Completion

宣布完成前必须：

1. 重新检查 `git status`、工作区 diff 和 cached diff。
2. 确认只有任务允许的文件发生变化，没有覆盖用户改动。
3. 运行任务要求及与风险相称的最小测试/检查，并记录精确命令和结果。
4. 报告失败、warning、skip、`NOT_EXECUTED` 与环境限制，不隐瞒真实证据。
5. 说明已知限制、未完成项和 deliberately out-of-scope 内容。
6. 明确区分当前已实现事实、文档声明、推断和未来建议。
