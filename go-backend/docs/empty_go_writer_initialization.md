# 隔离新空库 Go Writer 初始化

仅用于新建、专用 E2E_TEST 库，不能接管历史 Python 数据或替代生产 Cutover。

原 Alembic 到当前唯一 Head 后，且业务项目、凭据、Catalog、API/Worker 创建/启动前，由数据库及 Writer 表 Owner 运行：

```text
cutoverctl --command initialize-empty-go --controlled --empty-identity <受限身份JSON> --repo-root <EO绝对根路径> --receipt <新dry-run回执>
cutoverctl --command initialize-empty-go --controlled --execute --empty-identity <同一受限身份JSON> --repo-root <EO绝对根路径> --receipt <新execute回执>
```

`DATABASE_URL` 仅从安全环境注入，不放命令行。身份 JSON 使用 `postgres.EmptyWriterIdentity` 的严格字段：task_id（32小写hex）、operation_id（UUID）、host=127.0.0.1、port=55435、database=ci_evaluation_<task_id>、database_oid、owner、system_identifier、container_id、volume=ci-e2e-eopg-<task_id>-data、migration_digest、code_digest。目录/JSON ACL 仅批准 operator 可读写，不能挂载到普通服务进程。

Docker 容器须 `ci-e2e-eopg-<task_id>`、owner label `ci-e2e-owner=<task_id>`、仅绑定127.0.0.1:55435，数据卷具有同一标签。代码在建立数据库连接前核对 pgx 实际目标及 fallback、Docker身份及源码摘要；源码摘要按相对路径排序，将每项 `<posix路径>\0<sha256文件内容>\n` 串联后SHA256，migration覆盖 backend/migrations/versions/*.py；code覆盖go-backend下*.go/go.mod/go.sum，排除.git和临时.e2e目录。

事务用 try advisory lock、全部用户表 ACCESS EXCLUSIVE NOWAIT、当前库其他客户端会话检查及Owner复核。public外关系、外部/分区/物化关系拒绝。除精确单行 migration ledger和原始writer seed以外，全部用户表必须零行。未知记录同样拒绝，不删除数据迎合检查。检查原控制触发器和关键表主键；只接受当前Head、原始PYTHON_ACTIVE/PYTHON/1/空cutover_id、原durable contract/required_schema/operator_ref=migration。

仅调用原cutover.Next的三个固定转换，原g2_control_guard始终启用；同一事务、同一OperationID完成1→1→2→3。dry-run只投影状态，不落库；回执Decision明确DRY_RUN/APPLIED/REJECTED/COMMIT_UNKNOWN_QUERY_SAME_OPERATION，回执O_EXCL新建，不覆盖。代码/迁移摘要、实际DB身份、operator、空库摘要和状态序列均为非Secret证据。

提交不明时停止执行，仅查询同库Writer状态与原OperationID，不更换ID重试；GO_ACTIVE或其他初态均拒绝初始化。初始化完成后由受限角色调用原VerifyAPI/VerifyWorker再启动正式bootstrap；不提供skip guard。历史Cutover、迁移、Kernel/Fencing均不改。本入口不证明生产cutover、历史数据移交或真实模型成功。
