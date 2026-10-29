# 业务反馈来源接入 v1

实现范围为 CI SYNTHETIC/HUMAN 安全投影接入既有 Review；不改变 Case/GT/EvaluationResult Authority，不自动判分或发布。

## 部署配置和身份

`AC_FEEDBACK_CONNECTIONS` 为部署管理的 JSON map，key 为 EO project UUID；value 为 `base_url`、`token_env`、`tenant_id`、`owner_user_id`。例如以下仅为占位符：

```json
{"<project UUID>":{"base_url":"https://<trusted AC host>","token_env":"AC_FEEDBACK_SOURCE_TOKEN","tenant_id":"<authorized tenant>","owner_user_id":"<authorized user UUID>"}}
```

token_env 指向现有安全环境变量，值是 AC 已授权 HUMAN bearer，本版不授予 AC machine 跨用户读取能力。token 过期、配置缺失、tenant/owner 不匹配均 fail closed；不写入日志或持久化投影。每个项目首版仅映射一个 owner；需要其他 owner 应显式重配或使用另一个受权项目，不能由请求授予权限。

只接受 HTTPS 或 loopback HTTP；禁止 URL userinfo/query/fragment/额外路径与重定向；单次源端请求上限 2 秒/32KiB，遵循请求 context 期限。配置及凭据不来自请求正文。未知字段/版本/枚举、重复 JSON key、错误摘要拒绝。

迁移为 `backend/migrations/versions/c17a00100001_business_feedback_source.py`：追加 `evaluation_business_feedback` 不可更新/删除来源表，扩展既有 Review source CHECK。通过原 `postgres.ProductRoleGrants` 在表存在时授予 SELECT/INSERT，不授予 UPDATE/DELETE/TRUNCATE。operator 显式迁移/重授 role，本轮不做生产迁移。已知追加 Schema 被启动检查显式接受，旧 Schema 路径保留，未知版本仍拒绝。

## HTTP 和审核流程

项目入口前缀沿现有 API：`/api/v1/projects/{project_id}`。

1. 通过 AC 原 owner 身份读取 `GET /api/ci/incidents/{incident_id}/feedback/{feedback_id}/projection`，获得 `projection_digest`。
2. EO `POST /business-feedback`：现有 X-API-Key machine，要求 WRITE 项目权限；正文仅 `incident_id, feedback_id, projection_digest`。EO 从受信源端重新拉取、校验身份/版本/digest，不信任 caller 的投影。返回 200、AWAITING_HUMAN_REVIEW、feedback_id/digest/source_type；相同 project/feedback/digest 幂等，不同 digest 409。请求 identity 就是指定 feedback 版本，无需另造幂等键。
3. EO `GET /business-feedback/{id}`：现有项目 READ 权限；返回原 digest 和当前 source status、requires_re_review。源不可达/认证失败为 UNKNOWN；已知撤回、修订或摘要变化不能显示有效。
4. 独立人工通过原 `POST /reviews` 指定 source=`{type:"BUSINESS_FEEDBACK",feedback_id:"..."}` 与原 schema/policy；建立 PENDING ReviewItem。导入暂存不是已完成 Review 或已批准 ReviewedCaseDraft。
5. 原 Claim/Annotation/Adjudication/Golden 权限不变。原 `/drafts` 仍要求 COMPLETED review，并由人工显式提供脱敏 Case 正文；不隐式复制 AC reason/correction。原 `/drafts/{id}/publish`、Dataset revision 继续使用原目录 Owner。

来源 `SourceRef.FeedbackID` 用 omitempty，仅新类型使用；旧来源 canonical 内容不增加空字段。Review Source/Evidence 保存安全投影，人工 Case provenance 保留 SourceRef、source digest、review、annotation/adjudication 与 draft 引用；通过反馈 id 可追溯 Run/report，而非将 HUMAN 变成 GT。

## 失效、失败与历史

resolve 来源、首次创建 ReviewedCaseDraft、Golden 发布、Case 发布、通过审核草稿构建新 Dataset revision 时回查 AC 当前状态/摘要。SUPERSEDED/WITHDRAWN/绑定失效/网络失败阻止继续发布。导入源端失败或未确认不能宣称成功；调用方可同 identity 重试或 GET receipt；即使响应丢失，唯一键也不产生第二来源。

已发布 Case/Golden/冻结 Dataset 不被修改或删除；GET receipt 的 requires_re_review 为当前使用资格提示，由现有人工流程处理。没有新增自动撤销历史事实的 Authority，也没有后台扫描所有历史来源。当前资格为查询时回查计算，不是持久化的失效事件流。

跨仓没有分布式事务：回查通过表示检查时来源有效，不能承诺源端在 EO commit 之后永远不再撤回。不可达时 fail closed；后续查询发现失效要求重审。若需要跨仓线性化发布/持续撤销所有历史使用，属于新的架构决策，不在本 WP 自动实现。

## 定向验证

- AC `uv run python -m pytest tests/test_ci_feedback_projection.py -q`。
- Go `go test ./internal/review -run '^TestBusinessFeedback' -count=1`。
- AC `uv run python -m pytest tests/test_ci_feedback_cross_repo.py::test_python_projection_go_review_http_postgres -q`：启动测试专用 Python HTTP Producer，调用 Go 的单条 integration 测试；正常 Runtime/模型服务不启动。

联调首版显式使用 AC 本机 5433 的测试配置，在 `/postgres` 管理连接创建随机 `agentevalops_g1_*` 临时 Go 数据库；不读写 AC Runtime/业务数据库。只有 TestBusinessFeedback 前缀可经 AC_FEEDBACK_TEST_PG_URL 使用该已知本机端口，旧 G1 测试仍只接受 55432；fixture 清理临时库/role。测试 DSN 不输出。G1_TEST_PYTHON 指定既有 backend venv，不安装依赖。
