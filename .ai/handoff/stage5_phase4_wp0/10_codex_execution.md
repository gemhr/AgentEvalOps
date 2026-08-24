# Stage5-Phase4-WP0 Execution Handoff

## A. Final Status

```text
WP0_STATUS = COMPLETE_WITH_LIMITATIONS
```

## B. Source Project

```text
SOURCE_PROJECT = kubernetes/enhancements
SOURCE_REPOSITORY = https://github.com/kubernetes/enhancements
SOURCE_COMMIT = c4f439c2dd4acb928094660be0ea771bf63f2b76
RETRIEVED_AT = 2026-08-25T00:00:00+00:00
```

公开仓库以 shallow clone 取得，并通过 `git rev-parse HEAD` 核对为上述 commit；每个 KEP 原文和 `kep.yaml` 被保存为 raw snapshot。

## C. Candidate KEPs

| KEP | Title | Result | Reason |
|---:|---|---|---|
| 541 | External credential providers | SELECT | 安全凭据边界、风险段与 Test Plan 清晰。 |
| 625 | In-tree Storage Plugin to CSI Migration | REJECT | 变更面过大，首个 Demo 不适合承载迁移矩阵。 |
| 624 | Scheduling Framework | REJECT | 设计质量高，但文档仅有 Test Plans，缺少独立风险段。 |
| 647 | APIServer Tracing | REJECT | 可用，但与 selected 1602 同属 instrumentation，优先保留更广泛的日志兼容性案例。 |
| 753 | Sidecar Containers | SELECT | 容器 lifecycle/state 和多组件 feature gate 变更丰富。 |
| 757 | Pid Limiting | REJECT | 有风险段但未发现可稳定提取的 Test Plan。 |
| 1287 | In-place Update of Pod Resources | SELECT | API、kubelet、scheduler、CRI、重试和状态一致性变化明确。 |
| 1432 | Volume Health Monitor | REJECT | 可用但与 selected storage case 相比历史追踪材料较少聚焦。 |
| 1472 | Storage Capacity Constraints for Pod Scheduling | SELECT | CSIStorageCapacity 的持久化/调度交互和 Test Plan 清晰。 |
| 1602 | Structured Logging | SELECT | 配置、日志格式兼容性与可观测性升级边界明确。 |

## D. Final Selected Cases

`k8s_541`、`k8s_753`、`k8s_1287`、`k8s_1472`、`k8s_1602`。共 5 个，覆盖 security/authorization、lifecycle/state、storage/scheduling、observability/compatibility。

## E. Dataset Inventory

```text
feature_documents = 5
historical_knowledge_sources = 10  (5 KEP raw sources + 5 tracking issues)
historical_issues = 5
test_plans = 5
test_cases = 0 (PARTIAL mapping)
```

数据根目录：`backend/evaluation_assets/feature_risk_review_v1/`。

## F. Source Authenticity

| 类型 | 内容 |
|---|---|
| REAL_SOURCE | KEP README、kep.yaml、GitHub issue API 原始 JSON、从 KEP 提取的 Test Plan。 |
| CURATED | case selection、agent/evaluation 边界、risk domain 和目录投影。 |
| SYNTHETIC | 无。 |
| MOCK | 无。 |

`historical_issues.json` 是 enhancement tracking issue，而非已人工验证的生产 bug；没有官方 severity 的记录保持 `severity = null`。

## G. External Data

```text
EXTERNAL_DATA_BLOCKED = NO
```

只访问公开 GitHub：冻结 KEP checkout 与 `kubernetes/enhancements/issues/<KEP_ID>` API 快照。未使用 credential，未向远端写入。

## H. Code Changes

- 新增数据集、raw snapshots、manifest 和 README：`backend/evaluation_assets/feature_risk_review_v1/`。
- 新增可重建准备脚本：`backend/scripts/prepare_feature_risk_review_dataset.py`。
- 新增最小校验器和 focused test：`backend/scripts/validate_feature_risk_review_dataset.py`、`backend/tests/unit/test_feature_risk_review_dataset.py`。
- 新增本 handoff 和 `00_task.md`。

## I. Validation

```text
python backend/scripts/validate_feature_risk_review_dataset.py backend/evaluation_assets/feature_risk_review_v1
PASS

backend: uv run --group test pytest tests/unit/test_feature_risk_review_dataset.py -v
PASS (1 passed; pytest cache directory permission warning only)
```

## J. Known Limitations

- `REAL_TEST_CASE_MAPPING = PARTIAL`：仅冻结 Test Plan，未声称真实测试函数映射。
- 5 条 historical evidence 是 enhancement tracking issue；未来 WP 可在必要时扩展为少量相关 k/k issue/PR snapshot。
- Ground Truth annotation 为空且仍需人工审核。

## K. Truthful Boundary

```text
KUBERNETES_FEATURE_DOCUMENTS = REAL
HISTORICAL_ISSUE_SOURCE = REAL
TEST_PLAN_SOURCE = REAL
GROUND_TRUTH = PENDING
RAG_INDEX_BUILT = NO
REAL_AGENT_EXECUTION = NO
PRODUCTION_CHANGE = NO
LOCALAGENT_WRITE = NONE
```
