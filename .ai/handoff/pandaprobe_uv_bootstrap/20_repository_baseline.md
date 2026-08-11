# PandaProbe Upstream Baseline + AgentEvalOps Repository History Normalization

## 1. Final Status

- Repository normalization: **PASS**
- Baseline strategy: **UPSTREAM_HISTORY**
- Ready for independent review: **YES**
- 本轮仅规范化本地 Git 历史、恢复上游基线文件并保留既有 bootstrap 变更；未 push，未实施 Stage 4 Phase 0。

## 2. Original Repository Situation

- 当前仓库最初是在 GitHub 新建空的 `AgentEvalOps` 仓库后克隆到本地，再把 PandaProbe 源码复制进来。
- 本地 `main` 当时没有有效 `HEAD`；绝大多数 PandaProbe 文件未跟踪。
- 仓库初始化工具额外生成了根目录 `main.py`、根目录 `pyproject.toml`、根目录 `.venv`，它们不属于 PandaProbe 产品基线，已在前一阶段 bootstrap 中移除。
- 原始复制未包含上游 `.github/` 隐藏目录。

## 3. PandaProbe Upstream Identification

- 官方上游：`https://github.com/chirpz-ai/pandaprobe.git`
- 识别依据：当前 README 中的 clone URL、CI badge 与项目链接均指向该仓库。
- 采用分支：`upstream/main`
- 采用 revision：`7d45e99cc6020348d308b60296482c7e0f452717`
- Revision subject：`Merge pull request #127 from chirpz-ai/fix/judge-framework-tool-awareness`
- Revision date：`2026-08-09T21:56:23-05:00`
- 内容等价 tag：`v0.5.3`，指向 `69429885d46b0e6104c2e037c88f08ca04596284`。
- `upstream/main^{tree}` 与 `v0.5.3^{tree}` 均为 `e56eb7d736a246736ca6ef222b31eb8a21aae9a6`；tag revision 是所选 merge revision 的祖先。
- 排除已知 bootstrap 文件后，当前可版本化文件与 `upstream/main` 比较结果为 472 个 blob 完全一致、0 个不一致；上游独有的 12 个文件全部位于复制时遗漏的 `.github/`。
- 置信度：**HIGH**。源码树可高置信匹配；由于 `v0.5.3` 与随后 merge commit 的 tree 完全相同，无法仅按文件内容区分最初下载的是哪一个 ref。为保留默认分支的真实历史，本次选择 `upstream/main` merge commit 作为父基线。

## 4. Baseline Strategy

采用 **UPSTREAM_HISTORY**：将本地 `main` 建立在识别出的真实 PandaProbe upstream commit 上，而不是制造一个无父提交的 synthetic snapshot。这样可保留真实 ancestry、后续 upstream diff、merge-base 与升级审计能力。

## 5. Safety Backup

- 备份目录：`C:\Users\GemHr\AppData\Local\Temp\pandaprobe-history-backup-019ff106`
- 工作树归档：`worktree.tar`
- Manifest：`manifest.sha256.txt`
- Manifest SHA-256：`A4DC0641AB75AB4D6D021106C7C6FCF56366E1FB610365C2BCD4A17E98D35BB2`
- 共验证 622 个原有文件：从归档提取后逐文件 SHA-256 比较，missing 0、mismatch 0。
- 同时保存了工作树 patch、index patch、index 文件清单、remote 与 status 快照。
- 历史操作后再次核对 622 个原有文件，missing 0、mismatch 0。

## 6. History Operations

1. 保留原有 `origin = git@github.com:gemhr/AgentEvalOps.git`。
2. 增加 `upstream = https://github.com/chirpz-ai/pandaprobe.git` 并 fetch 全部分支与 tags。
3. 在完整、已验证的外部备份之后，执行 scoped history normalization，使本地 `main` 以 `7d45e99cc6020348d308b60296482c7e0f452717` 为基线，同时保留工作树内容。
4. 仅从该基线恢复复制时遗漏的 `.github/`；其内容与 upstream 完全一致，不进入 bootstrap diff。
5. 未 reset/restore/clean 掉用户 bootstrap 变更，未 stash，未 push。

## 7. Bootstrap Diff

本地 bootstrap commit 应只包含：

- `.gitignore`：不再全局忽略 `uv.lock`，使后端 lockfile 可受版本控制。
- `backend/Makefile`：`install` 直接执行 `uv sync --frozen`，不在项目命令中隐式安装 uv。
- `backend/pyproject.toml`：为 `uvloop` 增加 `sys_platform != 'win32'` marker，使官方依赖声明支持 Windows。
- `backend/uv.lock`：同步上述 marker；无依赖版本漂移。
- `AGENTS.md`：仓库级 Coding Agent 工作规范，并按已恢复的当前 CI workflow 修正事实。
- `.ai/handoff/`：本任务、前序 bootstrap 证据和本基线报告。

上游 `.github/` 已恢复为基线内容，但不属于 bootstrap commit diff。根目录初始化残留 `main.py`、`pyproject.toml` 与 `.venv` 均不属于上游，也不进入提交。

## 8. Verification

在 `backend/` 执行：

```text
uv lock --check
Resolved 149 packages in 1ms

uv sync --frozen --group test
Audited 147 packages in 16ms

uv run --frozen --no-sync pytest tests/unit/ -q -rs --basetemp <task-temp-dir>
210 passed in 3.23s

uv run --frozen --no-sync ruff check app/ tests/
All checks passed!
```

以上命令均 exit 0。`git diff --check` 未发现 whitespace error；仅有 Git for Windows 的 LF/CRLF 工作树提示。

## 9. Dynamic Verification Gaps

以下检查本轮未执行，状态为 **NOT_EXECUTED**：

- 使用真实 PostgreSQL/Redis 的 backend integration tests。
- 真实 Celery Worker/Beat 多进程启动与任务执行。
- Frontend install、Jest、lint、typecheck、format check 与 Playwright E2E。
- Docker Compose build/startup 与 migration 启动路径。

这些缺口不影响本次“历史规范化 + uv bootstrap”静态边界判断，但后续涉及对应运行路径时必须补测，不能据本报告宣称通过。

## 10. Out-of-Scope Risks

- Redis 配置或运行异常可能形成 HTTP 500；本轮未修改该行为。
- `backend/scripts/docker-entrypoint.sh` 当前在 migration 失败后仍可能继续启动；本轮未改变该 fail-open 行为。
- Python patch version 与 uv 工具版本尚未在仓库级强制 pin；本轮只遵循 `backend/pyproject.toml`、lockfile 与当前 CI/Docker 已声明边界。
- 上述事项属于后续独立审计或实现 Scope，不得混入本 bootstrap commit。

## 11. Current Git History

预期规范化后的直接关系为：

```text
HEAD  chore: bootstrap PandaProbe development environment
 |
7d45e99cc6020348d308b60296482c7e0f452717  upstream/main
```

最终验收必须确认 `HEAD^` 与 `git merge-base HEAD upstream/main` 均为上述 upstream revision，并确认 upstream revision 是 `HEAD` 的祖先。

## 12. Git Status

bootstrap commit 创建后，预期 `git status --short --branch` 无工作树或 index 变更。`origin/main` 当前不存在，因此 Git 可显示 `[gone]`；这不代表本轮进行过 push，也不影响本地 ancestry 结论。

## 13. Review Required

独立审查应至少核对：

- upstream URL、revision、tree 等价性与 merge-base；
- bootstrap commit 的 changed-file allowlist；
- `backend/app/`、`backend/migrations/`、`backend/tests/`、`frontend/src/`、`frontend/e2e/` 无产品代码 diff；
- lockfile marker 与 `pyproject.toml` 一致；
- 本报告中的已执行测试和 `NOT_EXECUTED` 缺口没有被扩大解释；
- 本轮没有 push。

## 14. Ready for Final Repository Review

**YES**。前提是最终 commit 后的 parent、merge-base、changed-file allowlist 与 clean status 检查全部通过。
