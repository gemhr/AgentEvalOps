# Stage5-Phase4-WP1 Execution Handoff

## A. Status

```text
WP1_IMPLEMENTATION_STATUS = COMPLETE
```

## B. Contracts

`app.core.feature_risk_review.contracts` 实现：`FeatureDocument`、`FeatureChangePoint`、`EvidenceRef`、`HistoricalIssue`、`TestPlan`、可为空的 `TestCase`、`RiskFinding`、仅 contract 的 `FeatureRiskReviewReport`、`EvaluationAnnotation`、`AnnotationStatus` 和 `FeatureRiskReviewCase`。

`HistoricalIssue` 保留 source `severity = null`；人工 `curated_severity` 必须带 `annotation_source`。正常 case 不含 annotation。

## C. Dataset Projection

```text
raw/                         = WP0 冻结外部事实，未改动
cases/                       = WP0 feature/evaluation-reference 边界，未改动
normalized/cases.v1.json     = WP1 业务投影，含 Feature、Issue、Test Plan、空 TestCase[]
annotations/annotations.v1.json = evaluation-only typed annotation；5 个均 PENDING
```

`backend/scripts/build_feature_risk_review_projection.py` 可从现有 WP0 snapshot 重建 projection，不访问网络。

## D. Cases

已实际加载：`k8s_541`、`k8s_753`、`k8s_1287`、`k8s_1472`、`k8s_1602`。

## E. Evidence

Feature、Historical Issue 与 Test Plan 都有 `EvidenceRef`，分别绑定冻结 KEP raw README、GitHub issue raw JSON 和保存的 evaluation-reference Test Plan；focused test 解析每个 source path 与 URL。

## F. Annotation

```text
GROUND_TRUTH = PENDING
HUMAN_ANNOTATION_REQUIRED = YES
```

后续人工最小审核项：每个 case 的 change points、components、risk areas、historical issue IDs、coverage gaps 和 risk level。本 WP 未自动填充它们。

## G. Tests

```text
cd backend && uv run --group test pytest tests/unit/test_feature_risk_review_dataset.py tests/unit/test_feature_risk_review_contracts.py -v
PASS: 7 passed

cd backend && uv run --group dev ruff check app/core/feature_risk_review scripts/build_feature_risk_review_projection.py tests/unit/test_feature_risk_review_contracts.py
PASS

cd backend && python scripts/validate_feature_risk_review_dataset.py evaluation_assets/feature_risk_review_v1
PASS
```

pytest 仅报告现有 `.pytest_cache` 写入权限 warning。

## H. Files

- `backend/app/core/feature_risk_review/`：contracts 与 loader。
- `backend/scripts/build_feature_risk_review_projection.py`：无网络 projection builder。
- `backend/evaluation_assets/feature_risk_review_v1/normalized/`、`annotations/`：归一化数据和 PENDING annotations。
- `backend/tests/unit/test_feature_risk_review_contracts.py`：focused tests。

## I. Known Limitations

- `REAL_TEST_CASE_MAPPING = PARTIAL`，故所有 `test_cases` 均为空。
- Historical Issue 是 enhancement tracking issue，不代表 production incident；source severity 均为空。
- Annotation 尚未人工审核，不能作为 Ground Truth 使用。

## J. Truthful Boundary

```text
REAL_KUBERNETES_SOURCE = YES
TYPED_CONTRACTS = YES
NORMALIZED_DATASET = YES
GROUND_TRUTH = PENDING
RAG_INDEX_BUILT = NO
REAL_AGENT_EXECUTION = NO
THREE_AGENT_WORKFLOW = NOT_IMPLEMENTED
CITATION_TRACEABILITY = IMPLEMENTED
CITATION_CORRECTNESS = NOT_EVALUATED
PRODUCTION_CHANGE = NO
LOCALAGENT_WRITE = NONE
```
