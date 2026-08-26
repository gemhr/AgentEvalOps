"""WP4 evaluation-only contracts, validator and FeatureRiskReviewEvaluator.

本模块是 Stage5-Phase4-WP4 的 lightweight evaluation owner，只做 evaluation-only
能力，不建设 generic evaluator framework / metric registry / plugin system / DSL。
它显式接收：

- RuntimePredictionArtifact（runtime 已落盘的 typed prediction）
- Human-reviewed EvaluationAnnotation + GroundTruthFieldStatus sidecar
- optional ManualAdjudication artifact

Runtime 永远不加载本模块；Evaluator 也永远不写 annotation 文件。Evaluator 是
deterministic / near-pure 的 application logic：不调用 LLM、不发网络请求、不读取
annotation loader。Ground Truth authority 始终是 human-reviewed artifact；digest
只作为 freeze evidence，不替代 authority。

Metrics:

- E2E_WORKFLOW_SUCCESS / REPORT_GENERATION_SUCCESS 使用固定 denominator 5。
- 集合指标保存 TP/FP/FN 与 precision/recall/f1；aggregate 通过 sum TP/FP/FN 再
  计算 micro P/R/F1，不平均 5 个 per-case F1。
- PRIORITY_CORRECTNESS / CITATION_COMPLETENESS / TOKEN_USAGE / COST 始终为
  NOT_EVALUATED。
- denominator=0 时 value=None 且状态为 METRIC_NOT_APPLICABLE，不得伪装为 0.0/1.0。
- 缺少 adjudication 返回 NOT_EVALUATED，不是 false/0/all FP。
"""

# ruff: noqa: D415

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import Field, StrictStr, model_validator

from app.core.feature_risk_review.agents import (
    DocumentAnalysisResult,
    RiskRetrievalResult,
    TestReviewResult,
)
from app.core.feature_risk_review.contracts import (
    AnnotationStatus,
    EvaluationAnnotation,
    FeatureRiskReviewReport,
    ReportCompleteness,
    RiskFinding,
    RiskLevel,
    _Contract,
)
from app.core.feature_risk_review.loader import load_evaluation_annotations
from app.core.feature_risk_review.workflow import (
    BranchStatus,
    FeatureRiskReviewWorkflowResult,
    WorkflowStatus,
)

_FROZEN_CASE_IDS: tuple[str, ...] = ("k8s_541", "k8s_753", "k8s_1287", "k8s_1472", "k8s_1602")

_EXPECTED_FIELDS: tuple[str, ...] = (
    "expected_change_points",
    "expected_components",
    "expected_risk_areas",
    "expected_historical_issue_ids",
    "expected_coverage_gaps",
    "expected_risk_level",
)

_ISSUE_SOURCE_TYPES: frozenset[str] = frozenset(
    {"github_enhancement_tracking_issue", "kubernetes_issue_snapshot"}
)

_MODEL_REF: str = "deepseek/deepseek-chat"
_MODEL_TEMPERATURE: float = 0.0
_MODEL_COMPATIBILITY_MODE: str = "json_text"
_TOP_K: int = 5

_MANIFEST_SCHEMA_VERSION = "feature-risk-review-freeze-manifest.v1"
_ANNOTATION_SCHEMA_VERSION = "feature-risk-review-annotations.v1"
_FIELD_STATUS_SCHEMA_VERSION = "feature-risk-review-field-status.v1"
_ADJUDICATION_SCHEMA_VERSION = "feature-risk-review-adjudications.v1"
_PREDICTION_SCHEMA_VERSION = "feature-risk-review-prediction.v1"
_SUMMARY_SCHEMA_VERSION = "feature-risk-review-evaluation-summary.v1"

_ISSUE_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class FieldEvaluationStatus(StrEnum):
    """evaluation-only sidecar 中单个 expected_* 字段的可评测状态。"""

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"


class GroundTruthState(StrEnum):
    """Ground Truth checkpoint 状态。"""

    ANNOTATION_READY = "ANNOTATION_READY"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    GROUND_TRUTH_READY = "GROUND_TRUTH_READY"


class MetricStatus(StrEnum):
    """field/case metric 的显式状态；不允许只靠 value=null 表达失败。"""

    EXECUTION_FAILED = "EXECUTION_FAILED"
    GROUND_TRUTH_MISSING = "GROUND_TRUTH_MISSING"
    METRIC_NOT_APPLICABLE = "METRIC_NOT_APPLICABLE"
    NOT_EVALUATED = "NOT_EVALUATED"
    EVALUATED = "EVALUATED"


class ExecutionClassification(StrEnum):
    """execution attempt 分类；ENVIRONMENT_FAILURE 不伪装成 business result。"""

    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    BUSINESS_RESULT = "BUSINESS_RESULT"


class CaseExecutionStatus(StrEnum):
    """per-case E2E execution 状态。"""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"


class MatchVerdict(StrEnum):
    """人工 1:1 text match verdict。"""

    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"


class CitationVerdict(StrEnum):
    """冻结的 citation rubric；UNVERIFIABLE 从 correctness denominator 排除。"""

    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNVERIFIABLE = "UNVERIFIABLE"


class AdjudicationMetric(StrEnum):
    """adjudication 覆盖的 metric。"""

    CHANGE_POINT = "change_point"
    RISK_AREA = "risk_area"
    COVERAGE_GAP = "coverage_gap"
    CITATION = "citation"


class FreezeStatus(StrEnum):
    """runtime freeze manifest 的冻结状态。"""

    FROZEN = "FROZEN"
    NOT_FROZEN = "NOT_FROZEN"


class EvaluationValidationError(ValueError):
    """evaluation/adjudication 输入违反 identity 或 1:1 约束时 fail closed。"""


class GroundTruthFieldStatus(_Contract):
    """单个 expected_* 字段的 evaluation-only sidecar 条目。"""

    field: StrictStr = Field(min_length=1)
    status: FieldEvaluationStatus
    reason: StrictStr = Field(default="", min_length=0)


class GroundTruthFieldStatusByCase(_Contract):
    """一个 Case 的 field-status sidecar 分组。"""

    case_id: StrictStr = Field(min_length=1)
    field_statuses: list[GroundTruthFieldStatus] = Field(default_factory=list)


class GroundTruthFieldStatusFile(_Contract):
    """annotations/field_status.v1.json 顶层 contract。"""

    schema_version: StrictStr = Field(min_length=1)
    note: StrictStr | None = None
    cases: list[GroundTruthFieldStatusByCase] = Field(default_factory=list)


class MetricValue(_Contract):
    """count/rate metric 的最小 typed result。"""

    status: MetricStatus
    numerator: int = 0
    denominator: int = 0
    value: float | None = None


class SetMetricValue(MetricValue):
    """集合指标：额外保存 TP/FP/FN 与 precision/recall/f1。"""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None


class RiskLevelEvaluation(_Contract):
    """per-case RiskLevel accuracy。"""

    status: MetricStatus
    predicted: RiskLevel | None = None
    expected: RiskLevel | None = None
    correct: bool | None = None


class CitationRubricCounts(_Contract):
    """citation rubric 的逐项计数。"""

    supported: int = 0
    partially_supported: int = 0
    unsupported: int = 0
    unverifiable: int = 0


class CitationEvaluation(_Contract):
    """per-case/aggregate citation correctness；traceability 不作为 numerator。"""

    status: MetricStatus
    numerator: int = 0
    denominator: int = 0
    value: float | None = None
    unverifiable_count: int = 0
    rubric_counts: CitationRubricCounts = Field(default_factory=CitationRubricCounts)


class E2eExecutionStatusCounts(_Contract):
    """E2E 各 execution 状态计数。"""

    success: int = 0
    partial: int = 0
    failed: int = 0
    environment_failure: int = 0


class BadCase(_Contract):
    """bad-case 最小记录；不建设平台。"""

    case_id: StrictStr = Field(min_length=1)
    metric: StrictStr = Field(min_length=1)
    prediction: StrictStr | None = None
    expected: StrictStr | None = None
    evidence_or_source: StrictStr | None = None
    note: StrictStr | None = None


class ManualAdjudication(_Contract):
    """人工 adjudication 最小记录；不需要的字段根据 metric 为 None。"""

    case_id: StrictStr = Field(min_length=1)
    metric: AdjudicationMetric
    prediction_id_or_index: StrictStr = Field(min_length=1)
    expected_id_or_index: StrictStr | None = None
    evidence_id: StrictStr | None = None
    verdict: StrictStr = Field(min_length=1)
    review_note: StrictStr | None = None
    reviewer: StrictStr = Field(min_length=1)
    reviewed_at: datetime


class EvaluationProvenance(_Contract):
    """per-case evaluation 的来源信息；path 由 runner 填充。"""

    prediction_artifact: StrictStr | None = None
    attempt: int = 0
    model_ref: StrictStr | None = None
    runtime_manifest_digest: StrictStr | None = None
    annotation_digest: StrictStr | None = None


class RuntimePredictionArtifact(_Contract):
    """runtime 落盘的 prediction artifact；不复制 annotation。"""

    schema_version: StrictStr = Field(min_length=1)
    case_id: StrictStr = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    execution_classification: ExecutionClassification
    model_ref: StrictStr = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    workflow: FeatureRiskReviewWorkflowResult | None = None
    report: FeatureRiskReviewReport | None = None
    report_markdown: StrictStr | None = None
    runtime_manifest_digest: StrictStr | None = None

    @model_validator(mode="after")
    def _validate_prediction_boundary(self) -> "RuntimePredictionArtifact":
        if self.schema_version != _PREDICTION_SCHEMA_VERSION:
            raise ValueError(f"unsupported prediction schema_version: {self.schema_version!r}")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.workflow is not None and self.workflow.case_id != self.case_id:
            raise ValueError("workflow case_id must match prediction case_id")
        if self.report is not None and self.report.case_id != self.case_id:
            raise ValueError("report case_id must match prediction case_id")
        if self.execution_classification == ExecutionClassification.ENVIRONMENT_FAILURE and (
            self.workflow is not None or self.report is not None or self.report_markdown is not None
        ):
            raise ValueError("ENVIRONMENT_FAILURE must not contain business output")
        return self


class GroundTruthStatusReport(_Contract):
    """validate-annotations 的 typed 输出。"""

    state: GroundTruthState
    total_cases: int
    reviewed_cases: int
    case_ids: list[StrictStr] = Field(default_factory=list)
    missing_case_ids: list[StrictStr] = Field(default_factory=list)
    unknown_case_ids: list[StrictStr] = Field(default_factory=list)
    issues: list[StrictStr] = Field(default_factory=list)


class PerCaseEvaluationResult(_Contract):
    """单个 Case 的最小 typed evaluation result。"""

    case_id: StrictStr = Field(min_length=1)
    execution_status: CaseExecutionStatus
    execution_classification: ExecutionClassification
    retry_eligible: bool = False
    workflow_status: WorkflowStatus | None = None
    report_generation: MetricValue
    report_completeness: ReportCompleteness | None = None
    report_markdown_generated: bool = False
    change_point: SetMetricValue
    component: SetMetricValue
    risk_area: SetMetricValue
    historical_evidence_at_5: SetMetricValue
    historical_issue_finding: SetMetricValue
    coverage_gap: SetMetricValue
    risk_level: RiskLevelEvaluation
    citation_correctness: CitationEvaluation
    priority_correctness: MetricValue
    citation_completeness: MetricValue
    top5_source_type_composition: dict[str, int] = Field(default_factory=dict)
    exact_empty_match: dict[str, bool] = Field(default_factory=dict)
    bad_cases: list[BadCase] = Field(default_factory=list)
    provenance: EvaluationProvenance | None = None


class FeatureRiskReviewAggregateSummary(_Contract):
    """aggregate summary；只从 per-case result 程序化 derive。"""

    summary_schema_version: StrictStr = Field(min_length=1)
    ground_truth_state: GroundTruthState
    total_cases: int
    per_case: list[PerCaseEvaluationResult] = Field(default_factory=list)
    e2e_workflow_success: MetricValue
    e2e_status_counts: E2eExecutionStatusCounts
    report_generation_success: MetricValue
    change_point: SetMetricValue
    component: SetMetricValue
    risk_area: SetMetricValue
    historical_evidence_at_5: SetMetricValue
    historical_issue_finding: SetMetricValue
    coverage_gap: SetMetricValue
    risk_level_accuracy: MetricValue
    citation_correctness: CitationEvaluation
    priority_correctness: MetricValue
    citation_completeness: MetricValue
    token_usage: MetricValue
    cost: MetricValue
    top5_source_type_composition_aggregate: dict[str, int] = Field(default_factory=dict)
    bad_cases: list[BadCase] = Field(default_factory=list)


class FileAuthorityRef(_Contract):
    """freeze manifest 中指向真实源码/语料的 authority ref；不复制 Prompt 内容。"""

    path: StrictStr = Field(min_length=1)
    commit: StrictStr = Field(min_length=1)
    sha256: StrictStr = Field(min_length=1)


class RuntimeFreezeManifest(_Contract):
    """runtime freeze manifest；GT 未 READY 时只能生成 NOT_FROZEN draft。"""

    schema_version: StrictStr = Field(min_length=1)
    freeze_status: FreezeStatus
    wp4_mode: StrictStr = Field(min_length=1)
    case_ids: list[StrictStr] = Field(default_factory=list)
    git_commit: StrictStr = Field(min_length=1)
    dataset_digest: StrictStr = Field(min_length=1)
    annotation_digest: StrictStr = Field(min_length=1)
    risk_level_rubric_authority: FileAuthorityRef
    model_ref: StrictStr = Field(min_length=1)
    temperature: float
    model_compatibility_mode: StrictStr = Field(min_length=1)
    top_k: int
    retrieval_corpus_digest: StrictStr = Field(min_length=1)
    agents_authority: FileAuthorityRef
    workflow_authority: FileAuthorityRef
    aggregator_authority: FileAuthorityRef
    renderer_authority: FileAuthorityRef
    risk_policy_authority: FileAuthorityRef
    priority_policy_authority: FileAuthorityRef
    lexical_scoring_authority: FileAuthorityRef
    source_boost_authority: FileAuthorityRef
    query_construction_authority: FileAuthorityRef
    built_at: datetime


def sha256_bytes(data: bytes) -> str:
    """返回 bytes 的 SHA-256 hex digest。"""
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    """返回文件 bytes 的 SHA-256 hex digest。"""
    return sha256_bytes(path.read_bytes())


def compute_annotation_digest(annotation_bytes: bytes, field_status_bytes: bytes) -> str:
    """基于冻结 annotation bytes 与 field-status bytes 的 deterministic digest。"""
    hasher = hashlib.sha256()
    hasher.update(b"annotation:")
    hasher.update(annotation_bytes)
    hasher.update(b"\nfield_status:")
    hasher.update(field_status_bytes)
    return hasher.hexdigest()


def normalize_component(value: str) -> str:
    """冻结的 component normalization：NFKC -> trim -> collapse whitespace -> casefold。

    不删除 punctuation、不做 substring / stemming / embedding / 自动 alias。
    """
    return " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold()


def precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float | None, float | None, float | None]:
    """确定性 P/R/F1；denominator=0 时对应值为 None。"""
    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    f1 = 2 * tp / (2 * tp + fp + fn) if recall is not None and (2 * tp + fp + fn) > 0 else None
    return precision, recall, f1


def build_set_metric(tp: int, fp: int, fn: int) -> SetMetricValue:
    """由 TP/FP/FN 构建集合指标；全空时 METRIC_NOT_APPLICABLE。"""
    precision, recall, f1 = precision_recall_f1(tp, fp, fn)
    status = MetricStatus.EVALUATED if (tp + fp + fn) > 0 else MetricStatus.METRIC_NOT_APPLICABLE
    value = f1 if f1 is not None else precision
    return SetMetricValue(
        status=status,
        numerator=tp,
        denominator=tp + fp + fn,
        value=value,
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def _raise_if(issues: list[str]) -> None:
    if issues:
        raise EvaluationValidationError("; ".join(issues))


def _resolve_index(raw: str, count: int, label: str) -> int:
    try:
        index = int(raw)
    except ValueError as exc:
        raise EvaluationValidationError(f"{label}: non-integer index {raw!r}") from exc
    if index < 0 or index >= count:
        raise EvaluationValidationError(f"{label}: index {index} out of range (count={count})")
    return index


def _text_match_build(
    adjudications: list[ManualAdjudication],
    prediction_count: int,
    expected_count: int,
) -> SetMetricValue:
    """HUMAN_ADJUDICATED_ONE_TO_ONE_MATCH；identity 与 1:1 违规 fail closed。"""
    if prediction_count == 0 or expected_count == 0:
        if adjudications:
            raise EvaluationValidationError("text match adjudication is invalid when one side is empty")
        return build_set_metric(0, prediction_count, expected_count)
    if not adjudications:
        return SetMetricValue(status=MetricStatus.NOT_EVALUATED)
    prediction_match: dict[int, int] = {}
    expected_match: dict[int, int] = {}
    no_match_pairs: set[tuple[int, int]] = set()
    seen: set[tuple[int, int, str]] = set()
    for adj in adjudications:
        prediction_index = _resolve_index(adj.prediction_id_or_index, prediction_count, f"{adj.metric} prediction")
        expected_raw = adj.expected_id_or_index
        if not expected_raw:
            raise EvaluationValidationError(f"{adj.metric}: expected_id_or_index is required for text match")
        expected_index = _resolve_index(expected_raw, expected_count, f"{adj.metric} expected")
        verdict = MatchVerdict(adj.verdict)
        key = (prediction_index, expected_index, verdict.value)
        if key in seen:
            raise EvaluationValidationError(f"{adj.metric}: duplicate adjudication row {key}")
        seen.add(key)
        if verdict == MatchVerdict.MATCH:
            if prediction_index in prediction_match:
                raise EvaluationValidationError(
                    f"{adj.metric}: prediction {prediction_index} matched more than once"
                )
            if expected_index in expected_match:
                raise EvaluationValidationError(
                    f"{adj.metric}: expected {expected_index} matched more than once"
                )
            prediction_match[prediction_index] = expected_index
            expected_match[expected_index] = prediction_index
        elif verdict == MatchVerdict.NO_MATCH:
            if prediction_match.get(prediction_index) == expected_index or expected_match.get(expected_index) == prediction_index:
                raise EvaluationValidationError(
                    f"{adj.metric}: pair ({prediction_index},{expected_index}) is both MATCH and NO_MATCH"
                )
            no_match_pairs.add((prediction_index, expected_index))
    unmatched_predictions = set(range(prediction_count)) - set(prediction_match)
    unmatched_expected = set(range(expected_count)) - set(expected_match)
    required_no_match_pairs = {
        (prediction_index, expected_index)
        for prediction_index in unmatched_predictions
        for expected_index in unmatched_expected
    }
    missing_pairs = sorted(required_no_match_pairs - no_match_pairs)
    if missing_pairs:
        raise EvaluationValidationError(
            f"incomplete text match adjudication; missing NO_MATCH verdicts for {missing_pairs}"
        )
    tp = len(prediction_match)
    fp = prediction_count - len(prediction_match)
    fn = expected_count - len(expected_match)
    return build_set_metric(tp, fp, fn)


def _citation_build(adjudications: list[ManualAdjudication], findings: list[RiskFinding]) -> CitationEvaluation:
    """RiskFinding <-> EvidenceRef pair rubric；identity/duplicate 违规 fail closed。"""
    expected_pairs = {
        (finding_index, ref.evidence_id)
        for finding_index, finding in enumerate(findings)
        for ref in finding.evidence_refs
    }
    if not expected_pairs:
        if adjudications:
            raise EvaluationValidationError("citation adjudication exists but prediction has no citation pair")
        return CitationEvaluation(status=MetricStatus.METRIC_NOT_APPLICABLE)
    if not adjudications:
        return CitationEvaluation(status=MetricStatus.NOT_EVALUATED)
    counts = {verdict: 0 for verdict in CitationVerdict}
    seen: set[tuple[int, str]] = set()
    for adj in adjudications:
        finding_index = _resolve_index(adj.prediction_id_or_index, len(findings), "citation finding")
        if not adj.evidence_id:
            raise EvaluationValidationError("citation adjudication requires evidence_id")
        finding = findings[finding_index]
        evidence_ids = {ref.evidence_id for ref in finding.evidence_refs}
        if adj.evidence_id not in evidence_ids:
            raise EvaluationValidationError(
                f"citation evidence_id={adj.evidence_id!r} not present in finding {finding_index} evidence_refs"
            )
        key = (finding_index, adj.evidence_id)
        if key in seen:
            raise EvaluationValidationError(f"citation: duplicate adjudication {key}")
        seen.add(key)
        counts[CitationVerdict(adj.verdict)] += 1
    missing_pairs = sorted(expected_pairs - seen)
    if missing_pairs:
        raise EvaluationValidationError(
            f"incomplete citation adjudication; missing verdicts for {missing_pairs}"
        )
    supported = counts[CitationVerdict.SUPPORTED]
    denominator = (
        supported
        + counts[CitationVerdict.PARTIALLY_SUPPORTED]
        + counts[CitationVerdict.UNSUPPORTED]
    )
    unverifiable = counts[CitationVerdict.UNVERIFIABLE]
    status = MetricStatus.EVALUATED if denominator > 0 else MetricStatus.METRIC_NOT_APPLICABLE
    return CitationEvaluation(
        status=status,
        numerator=supported,
        denominator=denominator,
        value=(supported / denominator) if denominator > 0 else None,
        unverifiable_count=unverifiable,
        rubric_counts=CitationRubricCounts(
            supported=counts[CitationVerdict.SUPPORTED],
            partially_supported=counts[CitationVerdict.PARTIALLY_SUPPORTED],
            unsupported=counts[CitationVerdict.UNSUPPORTED],
            unverifiable=counts[CitationVerdict.UNVERIFIABLE],
        ),
    )


def validate_manual_adjudications(
    adjudications: list[ManualAdjudication],
    prediction: RuntimePredictionArtifact,
    annotation: EvaluationAnnotation,
) -> list[str]:
    """验证 adjudication 的 case_id、metric 约束与 prediction/expected/evidence identity。

    返回 issue 列表；空列表表示通过。identity 校验复用权威 build 函数。
    """
    issues: list[str] = []
    for adj in adjudications:
        if adj.case_id != prediction.case_id:
            issues.append(
                f"adjudication case_id={adj.case_id!r} does not match prediction case_id={prediction.case_id!r}"
            )
        if adj.metric == AdjudicationMetric.CITATION:
            if not adj.evidence_id:
                issues.append("citation adjudication requires evidence_id")
            if adj.expected_id_or_index is not None:
                issues.append("citation adjudication must set expected_id_or_index to null")
            if adj.verdict not in {v.value for v in CitationVerdict}:
                issues.append(f"invalid citation verdict {adj.verdict!r}")
        else:
            if not adj.expected_id_or_index:
                issues.append(f"{adj.metric}: expected_id_or_index is required")
            if adj.evidence_id is not None:
                issues.append(f"{adj.metric}: evidence_id must be null for text match")
            if adj.verdict not in {v.value for v in MatchVerdict}:
                issues.append(f"{adj.metric}: invalid match verdict {adj.verdict!r}")
    if issues:
        return issues
    try:
        _text_match_build(
            [a for a in adjudications if a.metric == AdjudicationMetric.CHANGE_POINT],
            _document_count(prediction, "change_points"),
            len(annotation.expected_change_points),
        )
        _text_match_build(
            [a for a in adjudications if a.metric == AdjudicationMetric.RISK_AREA],
            _document_count(prediction, "potential_risk_areas"),
            len(annotation.expected_risk_areas),
        )
        _text_match_build(
            [a for a in adjudications if a.metric == AdjudicationMetric.COVERAGE_GAP],
            _test_gap_count(prediction),
            len(annotation.expected_coverage_gaps),
        )
        _citation_build(
            [a for a in adjudications if a.metric == AdjudicationMetric.CITATION],
            _accepted_findings(prediction),
        )
    except EvaluationValidationError as exc:
        issues.append(str(exc))
    return issues


def _document_analysis(prediction: RuntimePredictionArtifact) -> DocumentAnalysisResult | None:
    workflow = prediction.workflow
    if workflow is None or workflow.document_analysis.status != BranchStatus.SUCCESS:
        return None
    return workflow.document_analysis.value


def _risk_retrieval(prediction: RuntimePredictionArtifact) -> RiskRetrievalResult | None:
    workflow = prediction.workflow
    if workflow is None or workflow.risk_retrieval.status != BranchStatus.SUCCESS:
        return None
    return workflow.risk_retrieval.value


def _test_review(prediction: RuntimePredictionArtifact) -> TestReviewResult | None:
    workflow = prediction.workflow
    if workflow is None or workflow.test_review.status != BranchStatus.SUCCESS:
        return None
    return workflow.test_review.value


def _accepted_findings(prediction: RuntimePredictionArtifact) -> list[RiskFinding]:
    risk = _risk_retrieval(prediction)
    if risk is None:
        return []
    return [finding for finding in risk.agent_inferred_risk_findings if finding.evidence_refs]


def _document_count(prediction: RuntimePredictionArtifact, attribute: str) -> int:
    document = _document_analysis(prediction)
    if document is None:
        return 0
    return len(getattr(document, attribute))


def _test_gap_count(prediction: RuntimePredictionArtifact) -> int:
    test = _test_review(prediction)
    if test is None:
        return 0
    return len(test.potential_gaps)


def _execution_status(prediction: RuntimePredictionArtifact) -> CaseExecutionStatus:
    if prediction.execution_classification == ExecutionClassification.ENVIRONMENT_FAILURE:
        return CaseExecutionStatus.ENVIRONMENT_FAILURE
    if prediction.workflow is None:
        return CaseExecutionStatus.FAILED
    return CaseExecutionStatus(prediction.workflow.workflow_status.value)


def _field_status(
    field_statuses: list[GroundTruthFieldStatus], field: str
) -> FieldEvaluationStatus | None:
    for entry in field_statuses:
        if entry.field == field:
            return entry.status
    return None


def _field_gt_status(
    annotation: EvaluationAnnotation,
    field_statuses: list[GroundTruthFieldStatus],
    field: str,
) -> MetricStatus | None:
    """字段可评测时返回 None；否则返回应使用的 metric status。"""
    if annotation.annotation_status != AnnotationStatus.HUMAN_REVIEWED:
        return MetricStatus.GROUND_TRUTH_MISSING
    status = _field_status(field_statuses, field)
    if status is None or status == FieldEvaluationStatus.NOT_EVALUATED:
        return MetricStatus.NOT_EVALUATED
    return None


def _not_evaluated_metric() -> MetricValue:
    return MetricValue(status=MetricStatus.NOT_EVALUATED)


class FeatureRiskReviewEvaluator:
    """WP4 evaluation-only evaluator；接收 prediction + human GT + adjudication。"""

    def evaluate_case(
        self,
        *,
        prediction: RuntimePredictionArtifact,
        annotation: EvaluationAnnotation,
        field_statuses: list[GroundTruthFieldStatus],
        adjudications: list[ManualAdjudication] | None = None,
        prediction_artifact_path: str | None = None,
    ) -> PerCaseEvaluationResult:
        """计算单个 frozen Case 的 evaluation result；fail closed。"""
        if prediction.case_id != annotation.case_id:
            raise EvaluationValidationError(
                f"case_id mismatch: prediction={prediction.case_id!r} annotation={annotation.case_id!r}"
            )
        _raise_if(
            [
                *_validate_annotation_values(annotation),
                *_validate_field_status_entries(annotation, field_statuses),
            ]
        )
        case_adjudications = [
            adj for adj in (adjudications or []) if adj.case_id == prediction.case_id
        ]
        _raise_if(validate_manual_adjudications(case_adjudications, prediction, annotation))

        document = _document_analysis(prediction)
        risk = _risk_retrieval(prediction)
        test = _test_review(prediction)
        report = prediction.report
        execution_status = _execution_status(prediction)
        workflow_status = prediction.workflow.workflow_status if prediction.workflow else None

        if prediction.execution_classification == ExecutionClassification.ENVIRONMENT_FAILURE:
            report_generation = MetricValue(
                status=MetricStatus.EXECUTION_FAILED, numerator=0, denominator=1, value=0.0
            )
            report_markdown_generated = False
        else:
            generated = report is not None and prediction.report_markdown is not None
            report_generation = MetricValue(
                status=MetricStatus.EVALUATED,
                numerator=int(generated),
                denominator=1,
                value=float(generated),
            )
            report_markdown_generated = prediction.report_markdown is not None
        report_completeness = report.completeness if report is not None else None

        change_point = self._change_point_metric(
            prediction, annotation, field_statuses, case_adjudications, document
        )
        component = self._component_metric(annotation, field_statuses, document)
        risk_area = self._risk_area_metric(
            prediction, annotation, field_statuses, case_adjudications, document
        )
        historical_evidence, top5_composition = self._historical_evidence_metric(
            annotation, field_statuses, risk
        )
        historical_issue_finding = self._historical_issue_finding_metric(
            prediction, annotation, field_statuses, risk
        )
        coverage_gap = self._coverage_gap_metric(
            prediction, annotation, field_statuses, case_adjudications, test
        )
        risk_level = self._risk_level_metric(annotation, field_statuses, report)
        citation = self._citation_metric(annotation, case_adjudications, risk)

        exact_empty_match: dict[str, bool] = {}
        for key, metric in (
            ("change_point", change_point),
            ("component", component),
            ("risk_area", risk_area),
            ("historical_evidence_at_5", historical_evidence),
            ("historical_issue_finding", historical_issue_finding),
            ("coverage_gap", coverage_gap),
        ):
            if (
                metric.status in (MetricStatus.EVALUATED, MetricStatus.METRIC_NOT_APPLICABLE)
                and metric.tp == 0
                and metric.fp == 0
                and metric.fn == 0
            ):
                exact_empty_match[key] = True

        provenance = EvaluationProvenance(
            prediction_artifact=prediction_artifact_path,
            attempt=prediction.attempt,
            model_ref=prediction.model_ref,
            runtime_manifest_digest=prediction.runtime_manifest_digest,
        )

        return PerCaseEvaluationResult(
            case_id=prediction.case_id,
            execution_status=execution_status,
            execution_classification=prediction.execution_classification,
            retry_eligible=prediction.execution_classification
            == ExecutionClassification.ENVIRONMENT_FAILURE,
            workflow_status=workflow_status,
            report_generation=report_generation,
            report_completeness=report_completeness,
            report_markdown_generated=report_markdown_generated,
            change_point=change_point,
            component=component,
            risk_area=risk_area,
            historical_evidence_at_5=historical_evidence,
            historical_issue_finding=historical_issue_finding,
            coverage_gap=coverage_gap,
            risk_level=risk_level,
            citation_correctness=citation,
            priority_correctness=_not_evaluated_metric(),
            citation_completeness=_not_evaluated_metric(),
            top5_source_type_composition=top5_composition,
            exact_empty_match=exact_empty_match,
            bad_cases=[],
            provenance=provenance,
        )

    @staticmethod
    def _change_point_metric(
        prediction: RuntimePredictionArtifact,
        annotation: EvaluationAnnotation,
        field_statuses: list[GroundTruthFieldStatus],
        adjudications: list[ManualAdjudication],
        document: DocumentAnalysisResult | None,
    ) -> SetMetricValue:
        if document is None:
            return SetMetricValue(status=MetricStatus.EXECUTION_FAILED)
        gt_status = _field_gt_status(annotation, field_statuses, "expected_change_points")
        if gt_status is not None:
            return SetMetricValue(status=gt_status)
        return _text_match_build(
            [a for a in adjudications if a.metric == AdjudicationMetric.CHANGE_POINT],
            len(document.change_points),
            len(annotation.expected_change_points),
        )

    @staticmethod
    def _component_metric(
        annotation: EvaluationAnnotation,
        field_statuses: list[GroundTruthFieldStatus],
        document: DocumentAnalysisResult | None,
    ) -> SetMetricValue:
        if document is None:
            return SetMetricValue(status=MetricStatus.EXECUTION_FAILED)
        gt_status = _field_gt_status(annotation, field_statuses, "expected_components")
        if gt_status is not None:
            return SetMetricValue(status=gt_status)
        predicted = {normalize_component(value) for value in document.affected_components}
        expected = {normalize_component(value) for value in annotation.expected_components}
        tp = len(predicted & expected)
        fp = len(predicted - expected)
        fn = len(expected - predicted)
        return build_set_metric(tp, fp, fn)

    @staticmethod
    def _risk_area_metric(
        prediction: RuntimePredictionArtifact,
        annotation: EvaluationAnnotation,
        field_statuses: list[GroundTruthFieldStatus],
        adjudications: list[ManualAdjudication],
        document: DocumentAnalysisResult | None,
    ) -> SetMetricValue:
        if document is None:
            return SetMetricValue(status=MetricStatus.EXECUTION_FAILED)
        gt_status = _field_gt_status(annotation, field_statuses, "expected_risk_areas")
        if gt_status is not None:
            return SetMetricValue(status=gt_status)
        return _text_match_build(
            [a for a in adjudications if a.metric == AdjudicationMetric.RISK_AREA],
            len(document.potential_risk_areas),
            len(annotation.expected_risk_areas),
        )

    @staticmethod
    def _historical_evidence_metric(
        annotation: EvaluationAnnotation,
        field_statuses: list[GroundTruthFieldStatus],
        risk: RiskRetrievalResult | None,
    ) -> tuple[SetMetricValue, dict[str, int]]:
        composition: dict[str, int] = {}
        if risk is None:
            return SetMetricValue(status=MetricStatus.EXECUTION_FAILED), composition
        top5 = risk.retrieved_evidence[:5]
        composition = dict(Counter(fragment.evidence_ref.source_type for fragment in top5))
        gt_status = _field_gt_status(annotation, field_statuses, "expected_historical_issue_ids")
        if gt_status is not None:
            return SetMetricValue(status=gt_status), composition
        issue_refs = [
            fragment.evidence_ref
            for fragment in top5
            if fragment.evidence_ref.source_type in _ISSUE_SOURCE_TYPES
        ]
        predicted = {ref.source_id for ref in issue_refs}
        expected = set(annotation.expected_historical_issue_ids)
        tp = len(predicted & expected)
        fp = len(predicted - expected)
        fn = len(expected - predicted)
        return build_set_metric(tp, fp, fn), composition

    @staticmethod
    def _historical_issue_finding_metric(
        prediction: RuntimePredictionArtifact,
        annotation: EvaluationAnnotation,
        field_statuses: list[GroundTruthFieldStatus],
        risk: RiskRetrievalResult | None,
    ) -> SetMetricValue:
        if risk is None:
            return SetMetricValue(status=MetricStatus.EXECUTION_FAILED)
        gt_status = _field_gt_status(annotation, field_statuses, "expected_historical_issue_ids")
        if gt_status is not None:
            return SetMetricValue(status=gt_status)
        refs: set[str] = set()
        for finding in _accepted_findings(prediction):
            refs.update(finding.historical_issue_refs)
            refs.update(
                ref.source_id for ref in finding.evidence_refs if ref.source_type in _ISSUE_SOURCE_TYPES
            )
        expected = set(annotation.expected_historical_issue_ids)
        tp = len(refs & expected)
        fp = len(refs - expected)
        fn = len(expected - refs)
        return build_set_metric(tp, fp, fn)

    @staticmethod
    def _coverage_gap_metric(
        prediction: RuntimePredictionArtifact,
        annotation: EvaluationAnnotation,
        field_statuses: list[GroundTruthFieldStatus],
        adjudications: list[ManualAdjudication],
        test: TestReviewResult | None,
    ) -> SetMetricValue:
        if test is None:
            return SetMetricValue(status=MetricStatus.EXECUTION_FAILED)
        gt_status = _field_gt_status(annotation, field_statuses, "expected_coverage_gaps")
        if gt_status is not None:
            return SetMetricValue(status=gt_status)
        return _text_match_build(
            [a for a in adjudications if a.metric == AdjudicationMetric.COVERAGE_GAP],
            len(test.potential_gaps),
            len(annotation.expected_coverage_gaps),
        )

    @staticmethod
    def _risk_level_metric(
        annotation: EvaluationAnnotation,
        field_statuses: list[GroundTruthFieldStatus],
        report: FeatureRiskReviewReport | None,
    ) -> RiskLevelEvaluation:
        if report is None or report.risk_level is None:
            return RiskLevelEvaluation(status=MetricStatus.EXECUTION_FAILED)
        gt_status = _field_gt_status(annotation, field_statuses, "expected_risk_level")
        if gt_status is not None:
            return RiskLevelEvaluation(status=gt_status)
        if annotation.expected_risk_level is None:
            return RiskLevelEvaluation(status=MetricStatus.GROUND_TRUTH_MISSING)
        correct = report.risk_level == annotation.expected_risk_level
        return RiskLevelEvaluation(
            status=MetricStatus.EVALUATED,
            predicted=report.risk_level,
            expected=annotation.expected_risk_level,
            correct=correct,
        )

    @staticmethod
    def _citation_metric(
        annotation: EvaluationAnnotation,
        adjudications: list[ManualAdjudication],
        risk: RiskRetrievalResult | None,
    ) -> CitationEvaluation:
        if risk is None:
            return CitationEvaluation(status=MetricStatus.EXECUTION_FAILED)
        if annotation.annotation_status != AnnotationStatus.HUMAN_REVIEWED:
            return CitationEvaluation(status=MetricStatus.GROUND_TRUTH_MISSING)
        findings = [f for f in risk.agent_inferred_risk_findings if f.evidence_refs]
        return _citation_build(
            [a for a in adjudications if a.metric == AdjudicationMetric.CITATION],
            findings,
        )

    def evaluate(
        self,
        *,
        predictions: list[RuntimePredictionArtifact],
        annotations: list[EvaluationAnnotation],
        field_statuses: dict[str, list[GroundTruthFieldStatus]],
        adjudications: list[ManualAdjudication] | None = None,
        prediction_artifact_paths: dict[str, str] | None = None,
    ) -> FeatureRiskReviewAggregateSummary:
        """对 frozen 5-case 计算 aggregate summary；只从 per-case result derive。"""
        prediction_issues = validate_frozen_case_set([prediction.case_id for prediction in predictions])
        annotation_issues = validate_annotations(annotations, field_statuses)
        _raise_if([f"prediction eval set: {issue}" for issue in prediction_issues])
        _raise_if([f"Ground Truth: {issue}" for issue in annotation_issues])
        gt_report = detect_ground_truth_state(annotations, field_statuses)
        if gt_report.state != GroundTruthState.GROUND_TRUTH_READY:
            raise EvaluationValidationError(
                f"quality evaluation requires GROUND_TRUTH_READY; got {gt_report.state.value}"
            )
        unknown_adjudication_cases = sorted(
            {adj.case_id for adj in (adjudications or [])} - set(_FROZEN_CASE_IDS)
        )
        if unknown_adjudication_cases:
            raise EvaluationValidationError(
                f"adjudication references unknown case_id(s): {unknown_adjudication_cases}"
            )

        prediction_by_case = {prediction.case_id: prediction for prediction in predictions}
        annotation_by_case = {annotation.case_id: annotation for annotation in annotations}

        per_case = [
            self.evaluate_case(
                prediction=prediction_by_case[case_id],
                annotation=annotation_by_case[case_id],
                field_statuses=field_statuses.get(case_id, []),
                adjudications=adjudications,
                prediction_artifact_path=(
                    (prediction_artifact_paths or {}).get(case_id)
                ),
            )
            for case_id in _FROZEN_CASE_IDS
        ]

        counts = E2eExecutionStatusCounts(
            success=sum(int(pc.execution_status == CaseExecutionStatus.SUCCESS) for pc in per_case),
            partial=sum(int(pc.execution_status == CaseExecutionStatus.PARTIAL) for pc in per_case),
            failed=sum(int(pc.execution_status == CaseExecutionStatus.FAILED) for pc in per_case),
            environment_failure=sum(
                int(pc.execution_status == CaseExecutionStatus.ENVIRONMENT_FAILURE) for pc in per_case
            ),
        )
        e2e_workflow = MetricValue(
            status=MetricStatus.EVALUATED,
            numerator=counts.success,
            denominator=len(_FROZEN_CASE_IDS),
            value=counts.success / len(_FROZEN_CASE_IDS),
        )
        report_success = sum(pc.report_generation.numerator for pc in per_case)
        report_generation = MetricValue(
            status=MetricStatus.EVALUATED,
            numerator=report_success,
            denominator=len(_FROZEN_CASE_IDS),
            value=report_success / len(_FROZEN_CASE_IDS),
        )

        return FeatureRiskReviewAggregateSummary(
            summary_schema_version=_SUMMARY_SCHEMA_VERSION,
            ground_truth_state=gt_report.state,
            total_cases=len(_FROZEN_CASE_IDS),
            per_case=per_case,
            e2e_workflow_success=e2e_workflow,
            e2e_status_counts=counts,
            report_generation_success=report_generation,
            change_point=_aggregate_set_metric(per_case, lambda pc: pc.change_point),
            component=_aggregate_set_metric(per_case, lambda pc: pc.component),
            risk_area=_aggregate_set_metric(per_case, lambda pc: pc.risk_area),
            historical_evidence_at_5=_aggregate_set_metric(
                per_case, lambda pc: pc.historical_evidence_at_5
            ),
            historical_issue_finding=_aggregate_set_metric(
                per_case, lambda pc: pc.historical_issue_finding
            ),
            coverage_gap=_aggregate_set_metric(per_case, lambda pc: pc.coverage_gap),
            risk_level_accuracy=_aggregate_risk_level(per_case),
            citation_correctness=_aggregate_citation(per_case),
            priority_correctness=_not_evaluated_metric(),
            citation_completeness=_not_evaluated_metric(),
            token_usage=_not_evaluated_metric(),
            cost=_not_evaluated_metric(),
            top5_source_type_composition_aggregate=_aggregate_top5_composition(per_case),
            bad_cases=[bad_case for pc in per_case for bad_case in pc.bad_cases],
        )

def _aggregate_set_metric(
    per_case: list[PerCaseEvaluationResult],
    getter,
) -> SetMetricValue:
    statuses = [getter(pc).status for pc in per_case]
    if any(status in (MetricStatus.EVALUATED, MetricStatus.METRIC_NOT_APPLICABLE) for status in statuses):
        tp = sum(getter(pc).tp for pc in per_case)
        fp = sum(getter(pc).fp for pc in per_case)
        fn = sum(getter(pc).fn for pc in per_case)
        result = build_set_metric(tp, fp, fn)
        if not any(status == MetricStatus.EVALUATED for status in statuses):
            return result.model_copy(update={"status": MetricStatus.METRIC_NOT_APPLICABLE})
        return result
    if any(status == MetricStatus.NOT_EVALUATED for status in statuses):
        return SetMetricValue(status=MetricStatus.NOT_EVALUATED)
    if any(status == MetricStatus.GROUND_TRUTH_MISSING for status in statuses):
        return SetMetricValue(status=MetricStatus.GROUND_TRUTH_MISSING)
    return SetMetricValue(status=MetricStatus.EXECUTION_FAILED)


def _aggregate_risk_level(per_case: list[PerCaseEvaluationResult]) -> MetricValue:
    evaluated = [pc.risk_level for pc in per_case if pc.risk_level.status == MetricStatus.EVALUATED]
    if evaluated:
        correct = sum(1 for entry in evaluated if entry.correct)
        total = len(evaluated)
        return MetricValue(
            status=MetricStatus.EVALUATED,
            numerator=correct,
            denominator=total,
            value=correct / total,
        )
    if any(pc.risk_level.status == MetricStatus.NOT_EVALUATED for pc in per_case):
        return MetricValue(status=MetricStatus.NOT_EVALUATED)
    if any(pc.risk_level.status == MetricStatus.GROUND_TRUTH_MISSING for pc in per_case):
        return MetricValue(status=MetricStatus.GROUND_TRUTH_MISSING)
    return MetricValue(status=MetricStatus.EXECUTION_FAILED)


def _aggregate_citation(per_case: list[PerCaseEvaluationResult]) -> CitationEvaluation:
    evaluated = [pc.citation_correctness for pc in per_case if pc.citation_correctness.status in (MetricStatus.EVALUATED, MetricStatus.METRIC_NOT_APPLICABLE)]
    if evaluated:
        supported = sum(entry.numerator for entry in evaluated)
        denominator = sum(entry.denominator for entry in evaluated)
        unverifiable = sum(entry.unverifiable_count for entry in evaluated)
        rubric_counts = CitationRubricCounts(
            supported=sum(entry.rubric_counts.supported for entry in evaluated),
            partially_supported=sum(entry.rubric_counts.partially_supported for entry in evaluated),
            unsupported=sum(entry.rubric_counts.unsupported for entry in evaluated),
            unverifiable=sum(entry.rubric_counts.unverifiable for entry in evaluated),
        )
        status = MetricStatus.EVALUATED if denominator > 0 else MetricStatus.METRIC_NOT_APPLICABLE
        return CitationEvaluation(
            status=status,
            numerator=supported,
            denominator=denominator,
            value=(supported / denominator) if denominator > 0 else None,
            unverifiable_count=unverifiable,
            rubric_counts=rubric_counts,
        )
    if any(pc.citation_correctness.status == MetricStatus.NOT_EVALUATED for pc in per_case):
        return CitationEvaluation(status=MetricStatus.NOT_EVALUATED)
    if any(pc.citation_correctness.status == MetricStatus.GROUND_TRUTH_MISSING for pc in per_case):
        return CitationEvaluation(status=MetricStatus.GROUND_TRUTH_MISSING)
    return CitationEvaluation(status=MetricStatus.EXECUTION_FAILED)


def _aggregate_top5_composition(per_case: list[PerCaseEvaluationResult]) -> dict[str, int]:
    combined: Counter[str] = Counter()
    for pc in per_case:
        combined.update(pc.top5_source_type_composition)
    return dict(combined)


def validate_frozen_case_set(case_ids: list[str]) -> list[str]:
    """校验 final eval set 必须且只能是冻结的 5 个 Case。"""
    issues: list[str] = []
    counts = Counter(case_ids)
    for case_id, count in counts.items():
        if count > 1:
            issues.append(f"duplicate case_id in eval set: {case_id}")
    present = set(case_ids)
    frozen = set(_FROZEN_CASE_IDS)
    missing = sorted(frozen - present)
    unknown = sorted(present - frozen)
    if missing:
        issues.append(f"missing frozen case(s): {missing}")
    if unknown:
        issues.append(f"unknown case(s) in eval set: {unknown}")
    if not case_ids:
        issues.append("eval set is empty")
    return issues


def _validate_annotation_values(annotation: EvaluationAnnotation) -> list[str]:
    issues: list[str] = []
    for value in annotation.expected_change_points:
        if not value.strip():
            issues.append(f"{annotation.case_id}: empty expected_change_point string")
    for value in annotation.expected_components:
        if not value.strip():
            issues.append(f"{annotation.case_id}: empty expected_component string")
    for value in annotation.expected_risk_areas:
        if not value.strip():
            issues.append(f"{annotation.case_id}: empty expected_risk_area string")
    for value in annotation.expected_coverage_gaps:
        if not value.strip():
            issues.append(f"{annotation.case_id}: empty expected_coverage_gap string")
    for value in annotation.expected_historical_issue_ids:
        if not _ISSUE_ID_TOKEN_RE.match(value):
            issues.append(f"{annotation.case_id}: malformed expected historical issue id {value!r}")
    if annotation.annotation_status == AnnotationStatus.HUMAN_REVIEWED and not annotation.annotation_source.strip():
        issues.append(f"{annotation.case_id}: HUMAN_REVIEWED annotation requires annotation_source")
    if (
        annotation.annotation_status == AnnotationStatus.HUMAN_REVIEWED
        and annotation.annotation_source.strip().casefold() == "human_curated"
    ):
        issues.append(
            f"{annotation.case_id}: HUMAN_REVIEWED annotation requires a source note, not placeholder 'human_curated'"
        )
    return issues


def _validate_field_status_entries(
    annotation: EvaluationAnnotation, field_statuses: list[GroundTruthFieldStatus]
) -> list[str]:
    issues: list[str] = []
    field_counts = Counter(entry.field for entry in field_statuses)
    present = set(field_counts)
    for field, count in field_counts.items():
        if count > 1:
            issues.append(f"{annotation.case_id}: duplicate field status for {field}")
    for field in _EXPECTED_FIELDS:
        if field not in present:
            issues.append(f"{annotation.case_id}: missing field status for {field}")
    for entry in field_statuses:
        if entry.field not in _EXPECTED_FIELDS:
            issues.append(f"{annotation.case_id}: unknown field status field {entry.field!r}")
        if entry.status == FieldEvaluationStatus.NOT_EVALUATED and not entry.reason.strip():
            issues.append(f"{annotation.case_id}: NOT_EVALUATED field {entry.field} requires reason")
    return issues


def validate_annotations(
    annotations: list[EvaluationAnnotation],
    field_statuses: dict[str, list[GroundTruthFieldStatus]],
) -> list[str]:
    """严格 annotation validator；返回 issue 列表，空列表表示通过（fail closed）。"""
    issues = validate_frozen_case_set([annotation.case_id for annotation in annotations])
    known = set(annotation.case_id for annotation in annotations)
    for annotation in annotations:
        issues.extend(_validate_annotation_values(annotation))
        issues.extend(_validate_field_status_entries(annotation, field_statuses.get(annotation.case_id, [])))
    for case_id in field_statuses:
        if case_id not in known:
            issues.append(f"field status references unknown case_id: {case_id}")
    return issues


def detect_ground_truth_state(
    annotations: list[EvaluationAnnotation],
    field_statuses: dict[str, list[GroundTruthFieldStatus]],
) -> GroundTruthStatusReport:
    """检测 ANNOTATION_READY / HUMAN_REVIEW_REQUIRED / GROUND_TRUTH_READY。"""
    issues = validate_annotations(annotations, field_statuses)
    case_ids = [annotation.case_id for annotation in annotations]
    frozen = set(_FROZEN_CASE_IDS)
    present = set(case_ids)
    reviewed = [a.case_id for a in annotations if a.annotation_status == AnnotationStatus.HUMAN_REVIEWED]
    ready = (
        not issues
        and len(annotations) == len(_FROZEN_CASE_IDS)
        and present == frozen
        and len(reviewed) == len(_FROZEN_CASE_IDS)
    )
    if ready:
        state = GroundTruthState.GROUND_TRUTH_READY
    elif annotations:
        state = GroundTruthState.HUMAN_REVIEW_REQUIRED
    else:
        state = GroundTruthState.ANNOTATION_READY
    return GroundTruthStatusReport(
        state=state,
        total_cases=len(_FROZEN_CASE_IDS),
        reviewed_cases=len(reviewed),
        case_ids=sorted(case_ids),
        missing_case_ids=sorted(frozen - present),
        unknown_case_ids=sorted(present - frozen),
        issues=issues,
    )


def load_ground_truth_field_statuses(root: Path) -> dict[str, list[GroundTruthFieldStatus]]:
    """加载 annotations/field_status.v1.json，按 case_id 返回。"""
    path = root / "annotations" / "field_status.v1.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationValidationError(f"unable to load field status file: {path}") from exc
    file = GroundTruthFieldStatusFile.model_validate(payload)
    if file.schema_version != _FIELD_STATUS_SCHEMA_VERSION:
        raise EvaluationValidationError(
            f"unsupported field status schema_version: {file.schema_version!r}"
        )
    case_counts = Counter(case.case_id for case in file.cases)
    duplicates = sorted(case_id for case_id, count in case_counts.items() if count > 1)
    if duplicates:
        raise EvaluationValidationError(f"duplicate field-status case_id(s): {duplicates}")
    return {case.case_id: list(case.field_statuses) for case in file.cases}


def load_manual_adjudications(root: Path) -> list[ManualAdjudication]:
    """加载 experiments/wp4/adjudications/*.json 中全部 adjudication。"""
    directory = root / "experiments" / "wp4" / "adjudications"
    result: list[ManualAdjudication] = []
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvaluationValidationError(f"unable to load adjudication file: {path}") from exc
        if not isinstance(payload, dict):
            raise EvaluationValidationError(f"adjudication file must contain a JSON object: {path}")
        if payload.get("schema_version") != _ADJUDICATION_SCHEMA_VERSION:
            raise EvaluationValidationError(
                f"unsupported adjudication schema_version in {path}: {payload.get('schema_version')!r}"
            )
        file_case_id = payload.get("case_id")
        if not isinstance(file_case_id, str) or not file_case_id:
            raise EvaluationValidationError(f"adjudication file requires case_id: {path}")
        entries = payload.get("adjudications", [])
        if not isinstance(entries, list):
            raise EvaluationValidationError(f"adjudication file must contain adjudications list: {path}")
        loaded = [ManualAdjudication.model_validate(entry) for entry in entries]
        if any(entry.case_id != file_case_id for entry in loaded):
            raise EvaluationValidationError(
                f"adjudication entry case_id must match file case_id={file_case_id!r}: {path}"
            )
        result.extend(loaded)
    return result


def load_runtime_prediction_artifact(path: Path) -> RuntimePredictionArtifact:
    """加载 experiments/wp4/predictions/<case_id>.json 中的 typed prediction。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationValidationError(f"unable to load prediction artifact: {path}") from exc
    prediction = payload.get("prediction")
    if prediction is None:
        raise EvaluationValidationError(f"prediction artifact must contain prediction object: {path}")
    return RuntimePredictionArtifact.model_validate(prediction)


def render_evaluation_summary_markdown(summary: FeatureRiskReviewAggregateSummary) -> str:
    """确定性 summary Markdown 呈现；JSON typed authority 优先于本呈现。"""
    lines: list[str] = ["# WP4 Evaluation Summary", ""]
    lines.append(f"- Ground truth state: {summary.ground_truth_state.value}")
    lines.append(f"- Total cases: {summary.total_cases}")
    lines.append(f"- E2E workflow success: {summary.e2e_workflow_success.value:.3f}"
                 if summary.e2e_workflow_success.value is not None
                 else "- E2E workflow success: None")
    counts = summary.e2e_status_counts
    lines.append(
        f"- E2E status counts: SUCCESS={counts.success} PARTIAL={counts.partial} "
        f"FAILED={counts.failed} ENVIRONMENT_FAILURE={counts.environment_failure}"
    )
    lines.append(
        f"- Report generation success: {summary.report_generation_success.value:.3f}"
        if summary.report_generation_success.value is not None
        else "- Report generation success: None"
    )
    for label, metric in (
        ("Change point", summary.change_point),
        ("Component", summary.component),
        ("Risk area", summary.risk_area),
        ("Historical evidence @5", summary.historical_evidence_at_5),
        ("Historical issue finding", summary.historical_issue_finding),
        ("Coverage gap", summary.coverage_gap),
    ):
        lines.append(
            f"- {label}: status={metric.status.value} "
            f"TP={metric.tp} FP={metric.fp} FN={metric.fn} "
            f"P={_format(metric.precision)} R={_format(metric.recall)} F1={_format(metric.f1)}"
        )
    risk = summary.risk_level_accuracy
    lines.append(
        f"- Risk level accuracy: status={risk.status.value} "
        f"correct={risk.numerator}/{risk.denominator} value={_format(risk.value)}"
    )
    citation = summary.citation_correctness
    lines.append(
        f"- Citation correctness: status={citation.status.value} "
        f"numerator={citation.numerator} denominator={citation.denominator} "
        f"value={_format(citation.value)} unverifiable={citation.unverifiable_count} "
        f"rubric={citation.rubric_counts.model_dump(mode='json')}"
    )
    lines.append("- Priority correctness: NOT_EVALUATED")
    lines.append("- Citation completeness: NOT_EVALUATED")
    lines.append("- Token usage: NOT_EVALUATED")
    lines.append("- Cost: NOT_EVALUATED")
    if summary.top5_source_type_composition_aggregate:
        lines.append(
            f"- Top5 source-type composition: {json.dumps(summary.top5_source_type_composition_aggregate, sort_keys=True)}"
        )
    lines.append("")
    for pc in summary.per_case:
        lines.append(f"## {pc.case_id}")
        lines.append("")
        lines.append(f"- execution_status: {pc.execution_status.value}")
        lines.append(f"- execution_classification: {pc.execution_classification.value}")
        lines.append(f"- retry_eligible: {'yes' if pc.retry_eligible else 'no'}")
        lines.append(
            f"- report_generation: status={pc.report_generation.status.value} "
            f"numerator={pc.report_generation.numerator} denominator={pc.report_generation.denominator}"
        )
        if pc.report_completeness is not None:
            lines.append(f"- report_completeness: {pc.report_completeness.value}")
        for label, metric in (
            ("change_point", pc.change_point),
            ("component", pc.component),
            ("risk_area", pc.risk_area),
            ("historical_evidence_at_5", pc.historical_evidence_at_5),
            ("historical_issue_finding", pc.historical_issue_finding),
            ("coverage_gap", pc.coverage_gap),
        ):
            lines.append(
                f"- {label}: status={metric.status.value} "
                f"TP={metric.tp} FP={metric.fp} FN={metric.fn} "
                f"P={_format(metric.precision)} R={_format(metric.recall)} F1={_format(metric.f1)}"
            )
        risk_level = pc.risk_level
        lines.append(
            f"- risk_level: status={risk_level.status.value} "
            f"predicted={risk_level.predicted.value if risk_level.predicted else None} "
            f"expected={risk_level.expected.value if risk_level.expected else None} "
            f"correct={risk_level.correct}"
        )
        citation = pc.citation_correctness
        lines.append(
            f"- citation_correctness: status={citation.status.value} "
            f"numerator={citation.numerator} denominator={citation.denominator} "
            f"value={_format(citation.value)} unverifiable={citation.unverifiable_count}"
        )
        if pc.top5_source_type_composition:
            lines.append(
                f"- top5_source_type_composition: {json.dumps(pc.top5_source_type_composition, sort_keys=True)}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _format(value: float | None) -> str:
    return "None" if value is None else f"{value:.3f}"


def build_runtime_manifest(
    *,
    root: Path,
    backend_root: Path,
    git_commit: str,
    gt_state: GroundTruthState,
) -> RuntimeFreezeManifest:
    """构建 runtime freeze manifest；GT 未 READY 时 freeze_status=NOT_FROZEN。

    authority ref 只记录 file path / commit / sha256，不复制 Prompt 内容。
    """
    normalized_path = root / "normalized" / "cases.v1.json"
    manifest_path = root / "manifest.json"
    annotation_path = root / "annotations" / "annotations.v1.json"
    field_status_path = root / "annotations" / "field_status.v1.json"
    corpus_path = root / "retrieval" / "phase4_retrieval_corpus.v1.json"
    risk_level_rubric_path = root / "annotations" / "RISK_LEVEL_RUBRIC.v1.md"

    dataset_digest = _combined_digest([normalized_path, manifest_path])
    annotation_digest = compute_annotation_digest(
        annotation_path.read_bytes(), field_status_path.read_bytes()
    )

    def _authority(relative: str) -> FileAuthorityRef:
        path = backend_root / relative
        return FileAuthorityRef(path=relative, commit=git_commit, sha256=file_sha256(path))

    freeze_status = FreezeStatus.FROZEN if gt_state == GroundTruthState.GROUND_TRUTH_READY else FreezeStatus.NOT_FROZEN
    return RuntimeFreezeManifest(
        schema_version=_MANIFEST_SCHEMA_VERSION,
        freeze_status=freeze_status,
        wp4_mode="ONE_PASS_EVALUATION",
        case_ids=list(_FROZEN_CASE_IDS),
        git_commit=git_commit,
        dataset_digest=dataset_digest,
        annotation_digest=annotation_digest,
        risk_level_rubric_authority=FileAuthorityRef(
            path="evaluation_assets/feature_risk_review_v1/annotations/RISK_LEVEL_RUBRIC.v1.md",
            commit=git_commit,
            sha256=file_sha256(risk_level_rubric_path),
        ),
        model_ref=_MODEL_REF,
        temperature=_MODEL_TEMPERATURE,
        model_compatibility_mode=_MODEL_COMPATIBILITY_MODE,
        top_k=_TOP_K,
        retrieval_corpus_digest=file_sha256(corpus_path),
        agents_authority=_authority("app/core/feature_risk_review/agents.py"),
        workflow_authority=_authority("app/core/feature_risk_review/workflow.py"),
        aggregator_authority=_authority("app/core/feature_risk_review/aggregation.py"),
        renderer_authority=_authority("app/core/feature_risk_review/aggregation.py"),
        risk_policy_authority=_authority("app/core/feature_risk_review/aggregation.py"),
        priority_policy_authority=_authority("app/core/feature_risk_review/aggregation.py"),
        lexical_scoring_authority=_authority("app/adapters/feature_risk_review/retrieval.py"),
        source_boost_authority=_authority("app/adapters/feature_risk_review/retrieval.py"),
        query_construction_authority=_authority("app/core/feature_risk_review/agents.py"),
        built_at=datetime.now(timezone.utc),
    )


def _combined_digest(paths: list[Path]) -> str:
    hasher = hashlib.sha256()
    for path in paths:
        hasher.update(path.read_bytes())
        hasher.update(b"\x00")
    return hasher.hexdigest()


def is_retry_eligible(classification: ExecutionClassification) -> bool:
    """只允许 pre-business ENVIRONMENT_FAILURE 显式 retry；business result 不可自动 retry。"""
    return classification == ExecutionClassification.ENVIRONMENT_FAILURE


def annotation_digest_of(root: Path) -> str:
    """便捷 digest；基于冻结 annotation bytes 与 field-status bytes。"""
    return compute_annotation_digest(
        (root / "annotations" / "annotations.v1.json").read_bytes(),
        (root / "annotations" / "field_status.v1.json").read_bytes(),
    )


def load_all_for_evaluation(root: Path) -> dict[str, object]:
    """Runner 使用的统一加载入口（不包含 runtime case loader）。"""
    annotations = load_evaluation_annotations(root)
    field_statuses = load_ground_truth_field_statuses(root)
    adjudications = load_manual_adjudications(root)
    return {
        "annotations": annotations,
        "field_statuses": field_statuses,
        "adjudications": adjudications,
    }


__all__ = [
    "AdjudicationMetric",
    "BadCase",
    "CaseExecutionStatus",
    "CitationEvaluation",
    "CitationRubricCounts",
    "CitationVerdict",
    "E2eExecutionStatusCounts",
    "EvaluationProvenance",
    "EvaluationValidationError",
    "ExecutionClassification",
    "FeatureRiskReviewAggregateSummary",
    "FeatureRiskReviewEvaluator",
    "FieldEvaluationStatus",
    "FileAuthorityRef",
    "FreezeStatus",
    "GroundTruthFieldStatus",
    "GroundTruthFieldStatusByCase",
    "GroundTruthFieldStatusFile",
    "GroundTruthState",
    "GroundTruthStatusReport",
    "ManualAdjudication",
    "MatchVerdict",
    "MetricStatus",
    "MetricValue",
    "PerCaseEvaluationResult",
    "RiskLevelEvaluation",
    "RuntimeFreezeManifest",
    "RuntimePredictionArtifact",
    "SetMetricValue",
    "annotation_digest_of",
    "build_runtime_manifest",
    "build_set_metric",
    "compute_annotation_digest",
    "detect_ground_truth_state",
    "file_sha256",
    "is_retry_eligible",
    "load_all_for_evaluation",
    "load_ground_truth_field_statuses",
    "load_manual_adjudications",
    "load_runtime_prediction_artifact",
    "normalize_component",
    "precision_recall_f1",
    "render_evaluation_summary_markdown",
    "sha256_bytes",
    "validate_annotations",
    "validate_frozen_case_set",
    "validate_manual_adjudications",
]
