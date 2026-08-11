# PandaProbe Repository Bootstrap + uv Migration

## 1. Repository Baseline

- Branch：`main`
- HEAD：不存在；`git rev-parse HEAD` 返回 `fatal: ambiguous argument 'HEAD'`。
- 初始状态：`No commits yet on main...origin/main [gone]`；除暂存为空文件的根 `main.py` 外，复制进来的 PandaProbe 文件基本均未跟踪。
- `PRE_EXISTING_WORKTREE_CHANGE`：根 `main.py` 在 index 中是空文件，worktree 中是 16 行 PyCharm 示例；`.ai/`、PandaProbe 源码、文档和配置均为未跟踪状态。
- 本任务未执行 `reset`、`checkout`、`restore`、`clean`、commit 或 push。

## 2. Original Environment

### Python

- `backend/pyproject.toml`：`requires-python = ">=3.12"`。
- `backend/.python-version`：`3.13`。
- `backend/Dockerfile`：`python:3.13.2-slim`。
- 当前 checkout 不含 `.github/`，无本地 CI Python 版本可核验。
- 当前机器 PATH 无 `python`/`py`；可用 `uv 0.10.9` 下载并运行 CPython 3.13.12。
- 根 `.venv` 是初始化残留：CPython 3.12.6 trampoline、prompt `AgentEvalOps`，不属于 PandaProbe 后端环境。

### Dependency manager and files

- PandaProbe 后端原本已经使用 uv，并非 requirements/setuptools/Poetry/Pipenv 项目。
- 权威文件：`backend/pyproject.toml`、`backend/uv.lock`、`backend/.python-version`。
- 原安装命令：`cd backend && uv sync --frozen`；Docker 也使用该锁文件执行 `uv sync --frozen --no-dev --no-install-project`。
- 未发现 `requirements*.txt`、`setup.py`、`setup.cfg`、`Pipfile` 或 `poetry.lock`。
- Frontend 独立使用 Yarn：`frontend/package.json`、`frontend/yarn.lock`；Docker 使用 Node.js 20。
- 根 `pyproject.toml` 是用户确认的初始化残留：空依赖的 `agentevalops 0.1.0`，不是 PandaProbe package metadata。

## 3. Original Run Baseline

原始进程与命令：

- API：`cd backend && uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`
- Worker：`cd backend && uv run celery -A app.infrastructure.queue.celery_app worker --loglevel=info`
- Beat：同一 Celery app 使用 `beat`。
- Frontend：`cd frontend && yarn dev`
- Docker 开发栈：`docker compose -f docker-compose.dev.yml up --build -d`

实际结果：

- 官方 `uv sync --frozen`：`PRE_EXISTING_BASELINE_FAILURE`。Windows 构建无条件依赖 `uvloop==0.22.1` 时失败，原始错误为 `RuntimeError: uvloop does not support Windows at the moment`。
- 临时使用 `uv sync --frozen --group test --no-install-package uvloop` 后，`app.main:app` 和 Celery app composition import 成功。
- Uvicorn 进程完成 application startup，没有立即 crash。
- PostgreSQL `5432/5433` 与 Redis `6379/6380` 均不可达。HTTP `/` 与 `/health` 因 SlowAPI 访问 Redis 失败返回既有 `500`，栈尾为 `AttributeError: 'ConnectionError' object has no attribute 'detail'`。
- Worker/Beat 实际消费启动：`BLOCKED_BY_ENVIRONMENT`（Redis/PostgreSQL 不可用）。
- Frontend：`NOT_EXECUTED`（Node/Yarn 不在 PATH）。
- Docker 栈：`NOT_EXECUTED`（Docker 不在 PATH）。

## 4. Original Test Baseline

| Command | Passed | Failed | Skipped | Error |
| --- | ---: | ---: | ---: | ---: |
| `uv run --no-sync pytest tests/unit/ -q -rs`（临时排除 uvloop 的环境） | 210 | 0 | 0 | 0 |
| 首次使用默认 pytest temp 的运行 | 209 | 0 | 0 | 1 |

首次 error 是沙箱默认 temp 目录的 `PermissionError: [WinError 5]`，发生在 `tmp_path` fixture setup，切换到任务专用可写 temp 后完整通过，不属于源码失败。

Integration tests：`NOT_EXECUTED`。官方测试要求 Docker 提供 PostgreSQL `:5433` 与 Redis `:6380`，当前环境缺少 Docker 且端口不可达。

## 5. uv Migration Changes

- 删除根 `main.py`：移除已确认无关的 PyCharm 示例入口。
- 删除根 `pyproject.toml`：避免根空项目与真实 `backend/` Python project 形成双重依赖事实。
- 删除根 `.venv`：移除可重建、已失效的初始化环境；真实后端和临时验证环境未删除。
- `backend/pyproject.toml`：为 `uvloop` 添加 `sys_platform != 'win32'` marker；非 Windows 依赖语义不变。
- `backend/uv.lock`：仅同步上述两处 marker；保留原 revision 2 格式。
- `backend/Makefile`：`install` 改为直接执行 `uv sync --frozen`，不再依赖系统 `pip` 安装 uv；`uv` 仍是 CONTRIBUTING 中声明的 prerequisite。
- `.gitignore`：不再忽略 `uv.lock`，使应用锁文件可纳入版本控制。
- `AGENTS.md`：明确 `backend/` 是唯一 Python/uv project，并补充 `cd backend && uv sync --frozen`。
- 未修改 `backend/app/`、Database schema、migration、Worker lifecycle、API contract 或 frontend 源码。

## 6. Dependency Changes

- Added package：0
- Removed package：0
- Package version changed：0
- 现有 lock 中仍解析 149 个 package；Windows clean environment 安装 147 个，按 marker 跳过 `uvloop`。
- `uvloop` 版本约束仍为 `>=0.21.0`，锁定版本仍为 `0.22.1`；仅 Windows applicability 改变。
- FastAPI、Pydantic、SQLAlchemy、asyncpg、Celery、pytest 等业务和测试依赖版本均未变化。
- 当前 `uv 0.10.9` 的完整 `uv lock` 会将 revision 2 大规模重写为 revision 3；本任务已撤销该机械格式 diff，只保留 marker。`uv lock --check` 已通过。

## 7. Before / After Verification

| Check | Before uv alignment | After uv alignment | Regression |
| --- | --- | --- | --- |
| Official Windows install | FAIL：uvloop 不支持 Windows | PASS：clean `uv sync --frozen --group test` | NO；修复 baseline blocker |
| Python | 3.13.12 | 3.13.12 | NO |
| Unit tests | 210 passed | 210 passed | NO |
| API composition | PASS | PASS | NO |
| Celery composition | PASS | PASS | NO |
| Uvicorn process startup | PASS | PASS | NO |
| HTTP without Redis | 500 | 500 | NO；既有环境相关行为 |
| Ruff | 未作为 before baseline 执行 | `All checks passed!` | N/A |
| Integration tests | BLOCKED_BY_ENVIRONMENT | BLOCKED_BY_ENVIRONMENT | UNKNOWN |
| Worker/Beat real startup | BLOCKED_BY_ENVIRONMENT | BLOCKED_BY_ENVIRONMENT | UNKNOWN |
| Frontend | NOT_EXECUTED | NOT_EXECUTED | UNKNOWN |

After 命令与结果：

```text
uv lock --check
Resolved 149 packages

uv sync --frozen --group test
Installed 147 packages

uv run --frozen --no-sync pytest tests/unit/ -q -rs
210 passed

uv run --frozen --no-sync ruff check app/ tests/
All checks passed!
```

## 8. Environment Blockers

- PATH 中无系统 Python、Node、Yarn、Docker、Make。
- PostgreSQL 和 Redis 的 dev/test 端口均不可达。
- 网络受沙箱限制；经授权后 uv 才完成 Python/依赖下载。
- 因上述条件，integration、真实 Worker/Beat、frontend 和 Compose 未执行。

## 9. Known Pre-existing Failures

1. Windows 原始 `uv sync --frozen` 因无条件 `uvloop` 失败；本任务已用平台 marker 修复。
2. Redis 不可达时，SlowAPI middleware 使 `/` 与 `/health` 返回 500，并出现 `AttributeError: 'ConnectionError' object has no attribute 'detail'`；与 uv 无关，本任务未改生产代码。
3. `backend/scripts/docker-entrypoint.sh` 在 Alembic upgrade 失败后仍继续启动，这是既有部署风险，本任务未扩大 Scope。

## 10. Migration Risks

- Linux/macOS 仍会安装 `uvloop`，但本地没有对应平台执行环境，非 Windows 安装路径仅由 lock marker 和 Dockerfile 静态确认。
- Integration 与真实 queue/database 行为尚未动态验证。
- `backend/.python-version` 固定 3.13 minor，Docker 固定 3.13.2，而 uv 当前取得 3.13.12；三者兼容 `requires-python >=3.12`，但 patch-level 未统一。
- 未固定 uv tool version；未来运行较新 `uv lock` 可能产生 revision 3 的大规模锁格式 diff，应独立 Review，不要与业务依赖升级混合。

## 11. Git Diff Summary

- 当前仓库无 HEAD，绝大多数 PandaProbe 文件未跟踪，因此普通 `git diff` 不能完整展示任务 delta。
- 任务 delta 限于：根 bootstrap 文件/环境清理、`backend/pyproject.toml`、`backend/uv.lock`、`backend/Makefile`、`.gitignore`、`AGENTS.md` 和本 Handoff。
- 根 `main.py` 当前显示为 `AD`，源于任务前 index 中已有空文件而 worktree 文件现已删除；未执行 index 修改。
- 无生产 Python/TypeScript 源码 diff。

## 12. Items Requiring Review

1. 首次提交前必须审查全部未跟踪文件并确认 remote/branch 初始化策略；当前不能用 HEAD diff 证明上游来源。
2. 确认将 `backend/uv.lock` 纳入首个 commit；应用项目需要确定性锁文件。
3. 在有 Docker 的环境运行 `make test-integration`，并验证 Linux Docker build 仍安装 `uvloop`。
4. 单独决定是否修复 Redis 不可用导致所有 HTTP route 500 的既有行为；不要并入本 uv 任务。
5. 单独决定是否统一 Python patch version和 pin uv tool version。
