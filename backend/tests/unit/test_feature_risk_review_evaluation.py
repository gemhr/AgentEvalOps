"""WP4 focused tests: lightweight evaluation infrastructure (evaluation-only).

覆盖 Architecture Decision 的 26 项 focused tests：runtime/evaluator 边界、
Ground Truth state、field status、deterministic metrics、manual adjudication
validation、aggregate 派生与 truthful boundary。不调用模型。
"""

# ruff: noqa: D415

from __future__ import annotations

import importlib.util
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.feature_risk_review import (
    AnnotationStatus,
    BranchResult,
    BranchStatus,
    CoverageState,
    DocumentAnalysisResult,
    EvaluationAnnotation,
    EvidenceRef,
    FeatureChangePoint,
    FeatureRiskReviewReport,
    FeatureRiskReviewWorkflowResult,
    Priority,
    ReportCompleteness,
    RiskFinding,
    RiskLevel,
    RiskRetrievalResult,
    WorkflowStatus,
    load_evaluation_annotations,
    load_feature_risk_review_cases,
)
from app.core.feature_risk_review.agents import TestReviewResult as _TestReviewResult
from app.core.feature_risk_review.evaluation import (
    AdjudicationMetric,
    CaseExecutionStatus,
    CitationVerdict,
    ExecutionClassification,
    FeatureRiskReviewEvaluator,
    FieldEvaluationStatus,
    FreezeStatus,
    GroundTruthFieldStatus,
    GroundTruthState,
    ManualAdjudication,
    MatchVerdict,
    MetricStatus,
    RuntimePredictionArtifact,
    build_runtime_manifest,
    build_set_metric,
    compute_annotation_digest,
    detect_ground_truth_state,
    is_retry_eligible,
    load_ground_truth_field_statuses,
    load_manual_adjudications,
    load_runtime_prediction_artifact,
    normalize_component,
    precision_recall_f1,
    validate_annotations,
    validate_frozen_case_set,
    validate_manual_adjudications,
)

ASSET_ROOT = Path(__file__).resolve().parents[2] / "evaluation_assets" / "feature_risk_review_v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FROZEN_CASE_IDS = ("k8s_541", "k8s_753", "k8s_1287", "k8s_1472", "k8s_1602")
BUILDER = PROJECT_ROOT / "scripts" / "build_feature_risk_review_projection.py"
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

_ALL_FIELDS = (
    "expected_change_points",
    "expected_components",
    "expected_risk_areas",
    "expected_historical_issue_ids",
    "expected_coverage_gaps",
    "expected_risk_level",
)


def _now() -> datetime:
    return _EPOCH


def ev(evidence_id: str = "e1", source_type: str = "kubernetes_issue_snapshot", source_id: str = "541") -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        source_type=source_type,
        source_id=source_id,
        source_path="retrieval/enrichment/issue.json",
        source_url=f"https://github.com/kubernetes/kubernetes/issues/{source_id}",
        section="body",
    )


def fragment(ref: EvidenceRef):
    from app.core.feature_risk_review.ports import RetrievedKnowledgeFragment

    return RetrievedKnowledgeFragment(source_fragment="fragment", evidence_ref=ref)


def make_document(
    case_id: str = "k8s_541",
    change_points: list[FeatureChangePoint] | None = None,
    components: list[str] | None = None,
    risk_areas: list[str] | None = None,
) -> DocumentAnalysisResult:
    return DocumentAnalysisResult(
        case_id=case_id,
        feature_summary="summary",
        change_points=change_points
        or [FeatureChangePoint(description="cp0", affected_components=["client-go"])],
        affected_components=components or ["client-go"],
        potential_risk_areas=risk_areas or ["risk area a"],
        uncertainty=None,
    )


def make_finding(
    description: str = "finding",
    evidence_ids: tuple[str, ...] = ("e1",),
    issue_refs: tuple[str, ...] = (),
    risk_area: str = "risk area a",
    evidence_source_ids: dict[str, str] | None = None,
) -> RiskFinding:
    source_ids = evidence_source_ids or {}
    return RiskFinding(
        description=description,
        affected_components=["client-go"],
        risk_area=risk_area,
        historical_issue_refs=list(issue_refs),
        evidence_refs=[ev(evidence_id, source_id=source_ids.get(evidence_id, "541")) for evidence_id in evidence_ids],
        uncertainty=None,
    )


def make_risk(
    case_id: str = "k8s_541",
    findings: list[RiskFinding] | None = None,
    evidence: list[EvidenceRef] | None = None,
) -> RiskRetrievalResult:
    return RiskRetrievalResult(
        case_id=case_id,
        retrieved_historical_issues=[],
        retrieved_evidence=[fragment(ref) for ref in (evidence or [])],
        agent_inferred_risk_findings=list(findings or []),
        filtered_invalid_findings=[],
        uncertainty=None,
    )


def make_test(case_id: str = "k8s_541", gaps: list[str] | None = None) -> _TestReviewResult:
    return _TestReviewResult(
        case_id=case_id,
        coverage_state=CoverageState.PLAN_ONLY,
        test_plans=[],
        test_cases=[],
        coverage_assessment="assessment",
        potential_gaps=list(gaps or []),
        recommended_missing_cases=[],
        evidence_refs=[],
        uncertainty=None,
    )


def make_workflow(
    case_id: str = "k8s_541",
    *,
    document: DocumentAnalysisResult | None = None,
    risk: RiskRetrievalResult | None = None,
    test: _TestReviewResult | None = None,
    fail_document: bool = False,
    fail_risk: bool = False,
    fail_test: bool = False,
) -> FeatureRiskReviewWorkflowResult:
    from app.core.feature_risk_review.workflow import BranchFailure

    def _branch(branch: str, ok: bool, value: object) -> BranchResult:
        if ok:
            return BranchResult(branch=branch, status=BranchStatus.SUCCESS, value=value)
        return BranchResult(
            branch=branch,
            status=BranchStatus.FAILED,
            failure=BranchFailure(branch=branch, error_type="X", message=f"{branch} failed"),
        )

    branches = {
        "document_analysis": _branch(
            "document_analysis", not fail_document, document or make_document(case_id)
        ),
        "risk_retrieval": _branch("risk_retrieval", not fail_risk, risk or make_risk(case_id)),
        "test_review": _branch("test_review", not fail_test, test or make_test(case_id)),
    }
    if fail_document or (fail_risk and fail_test):
        status = WorkflowStatus.FAILED
    elif fail_risk or fail_test:
        status = WorkflowStatus.PARTIAL
    else:
        status = WorkflowStatus.SUCCESS
    return FeatureRiskReviewWorkflowResult(case_id=case_id, workflow_status=status, **branches)


def make_report(
    case_id: str = "k8s_541",
    risk_level: RiskLevel | None = RiskLevel.HIGH,
    completeness: ReportCompleteness = ReportCompleteness.FULL,
) -> FeatureRiskReviewReport:
    return FeatureRiskReviewReport(
        case_id=case_id,
        feature_summary="summary",
        change_points=[],
        high_risk_scenarios=[],
        historical_issues=[],
        existing_coverage=[],
        existing_test_cases=[],
        coverage_state=None,
        coverage_assessment=None,
        potential_gaps=[],
        missing_cases=[],
        risk_level=risk_level,
        priority=Priority.SCHEDULE_REVIEW,
        completeness=completeness,
        unavailable_sections=[],
        uncertainties=[],
        evidence_refs=[],
        uncertainty=None,
    )


def make_prediction(
    case_id: str = "k8s_541",
    *,
    classification: ExecutionClassification = ExecutionClassification.BUSINESS_RESULT,
    workflow: FeatureRiskReviewWorkflowResult | None = None,
    report: FeatureRiskReviewReport | None = None,
    markdown: str | None = None,
    attempt: int = 1,
) -> RuntimePredictionArtifact:
    return RuntimePredictionArtifact(
        schema_version="feature-risk-review-prediction.v1",
        case_id=case_id,
        attempt=attempt,
        execution_classification=classification,
        model_ref="deepseek/deepseek-chat",
        started_at=_now(),
        finished_at=_now(),
        workflow=workflow,
        report=report,
        report_markdown=markdown,
        runtime_manifest_digest="deadbeef",
    )


def make_annotation(
    case_id: str = "k8s_541",
    *,
    status: AnnotationStatus = AnnotationStatus.HUMAN_REVIEWED,
    change_points: tuple[str, ...] = ("cp0",),
    components: tuple[str, ...] = ("client-go",),
    risk_areas: tuple[str, ...] = ("risk area a",),
    issue_ids: tuple[str, ...] = ("541",),
    gaps: tuple[str, ...] = ("gap0",),
    risk_level: RiskLevel | None = RiskLevel.HIGH,
    source: str = "kep README + issue 541",
) -> EvaluationAnnotation:
    return EvaluationAnnotation(
        case_id=case_id,
        annotation_status=status,
        expected_change_points=list(change_points),
        expected_components=list(components),
        expected_risk_areas=list(risk_areas),
        expected_historical_issue_ids=list(issue_ids),
        expected_coverage_gaps=list(gaps),
        expected_risk_level=risk_level,
        annotation_source=source,
    )


def make_field_statuses(
    case_id: str = "k8s_541",
    *,
    status: FieldEvaluationStatus = FieldEvaluationStatus.EVALUATED,
) -> list[GroundTruthFieldStatus]:
    return [
        GroundTruthFieldStatus(field=field, status=status, reason="reviewed from source")
        for field in _ALL_FIELDS
    ]


def _adjudication(
    case_id: str = "k8s_541",
    *,
    metric: AdjudicationMetric = AdjudicationMetric.CHANGE_POINT,
    prediction_index: str = "0",
    expected_index: str | None = "0",
    evidence_id: str | None = None,
    verdict: str = "MATCH",
) -> ManualAdjudication:
    return ManualAdjudication(
        case_id=case_id,
        metric=metric,
        prediction_id_or_index=prediction_index,
        expected_id_or_index=expected_index,
        evidence_id=evidence_id,
        verdict=verdict,
        review_note="note",
        reviewer="human",
        reviewed_at=_now(),
    )


def _make_full_dataset(
    *,
    reviewed: bool = True,
) -> tuple[list[RuntimePredictionArtifact], list[EvaluationAnnotation], dict[str, list[GroundTruthFieldStatus]]]:
    predictions = []
    annotations = []
    field_statuses: dict[str, list[GroundTruthFieldStatus]] = {}
    for case_id in FROZEN_CASE_IDS:
        workflow = make_workflow(case_id)
        report = make_report(case_id)
        predictions.append(make_prediction(case_id, workflow=workflow, report=report, markdown="# r"))
        status = AnnotationStatus.HUMAN_REVIEWED if reviewed else AnnotationStatus.PENDING
        annotations.append(make_annotation(case_id, status=status))
        field_statuses[case_id] = make_field_statuses(case_id)
    return predictions, annotations, field_statuses


# 1. Runtime does not read annotations


def test_runtime_case_assembly_works_without_annotations(tmp_path: Path) -> None:
    copied = tmp_path / "dataset"
    shutil.copytree(ASSET_ROOT, copied)
    shutil.rmtree(copied / "annotations")

    cases = load_feature_risk_review_cases(copied)
    assert {case.feature_document.case_id for case in cases} == set(FROZEN_CASE_IDS)
    dumped = cases[0].model_dump(mode="json")
    assert "expected_change_points" not in dumped


# 2 + 3. Evaluator requires HUMAN_REVIEWED GT; PENDING cannot evaluate


@pytest.mark.parametrize("case_id", FROZEN_CASE_IDS)
def test_pending_annotation_cannot_evaluate_quality_metrics(case_id: str) -> None:
    prediction = make_prediction(
        case_id, workflow=make_workflow(case_id), report=make_report(case_id), markdown="# r"
    )
    annotation = make_annotation(case_id, status=AnnotationStatus.PENDING)
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction,
        annotation=annotation,
        field_statuses=make_field_statuses(case_id),
    )
    assert result.risk_level.status == MetricStatus.GROUND_TRUTH_MISSING
    assert result.change_point.status == MetricStatus.GROUND_TRUTH_MISSING
    assert result.component.status == MetricStatus.GROUND_TRUTH_MISSING
    assert result.historical_evidence_at_5.status == MetricStatus.GROUND_TRUTH_MISSING
    assert result.historical_issue_finding.status == MetricStatus.GROUND_TRUTH_MISSING
    assert result.coverage_gap.status == MetricStatus.GROUND_TRUTH_MISSING
    assert result.citation_correctness.status == MetricStatus.GROUND_TRUTH_MISSING
    assert result.priority_correctness.status == MetricStatus.NOT_EVALUATED


def test_pending_annotation_does_not_produce_zero_accuracy() -> None:
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541"), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541", status=AnnotationStatus.PENDING)
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction,
        annotation=annotation,
        field_statuses=make_field_statuses("k8s_541"),
    )
    assert result.risk_level.correct is None
    assert result.risk_level.status == MetricStatus.GROUND_TRUTH_MISSING
    assert not any(
        metric.status == MetricStatus.EVALUATED and metric.value == 0.0
        for metric in (result.change_point, result.component, result.historical_issue_finding)
    )


# 4. HUMAN_REVIEWED survives rebuild + validator + evaluator


def test_human_reviewed_annotation_survives_rebuild_validator_evaluator(tmp_path: Path) -> None:
    copied = tmp_path / "dataset"
    shutil.copytree(ASSET_ROOT, copied)
    annotation_path = copied / "annotations" / "annotations.v1.json"
    original_bytes = annotation_path.read_bytes()

    payload = json.loads(annotation_path.read_text(encoding="utf-8"))
    payload["annotations"][0]["annotation_status"] = "HUMAN_REVIEWED"
    payload["annotations"][0]["expected_risk_level"] = "HIGH"
    payload["annotations"][0]["annotation_source"] = "raw KEP README and evaluation reference"
    annotation_path.write_text(json.dumps(payload), encoding="utf-8")

    spec = importlib.util.spec_from_file_location("feature_risk_projection_builder", BUILDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build(copied)

    annotations = [
        EvaluationAnnotation.model_validate(a)
        for a in json.loads(annotation_path.read_text(encoding="utf-8"))["annotations"]
    ]
    reviewed = annotations[0]
    assert reviewed.annotation_status == AnnotationStatus.HUMAN_REVIEWED
    before = reviewed.model_dump(mode="json")

    evaluator = FeatureRiskReviewEvaluator()
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541"), report=make_report("k8s_541"), markdown="# r"
    )
    result = evaluator.evaluate_case(
        prediction=prediction,
        annotation=reviewed,
        field_statuses=make_field_statuses("k8s_541"),
    )
    assert result.risk_level.status == MetricStatus.EVALUATED
    assert reviewed.model_dump(mode="json") == before
    assert reviewed.annotation_status == AnnotationStatus.HUMAN_REVIEWED
    assert annotation_path.read_bytes() != original_bytes  # deliberate file was edited once

    validator_issues = validate_annotations(
        annotations,
        {case_id: make_field_statuses(case_id) for case_id in FROZEN_CASE_IDS},
    )
    assert validator_issues == []
    assert reviewed.annotation_status == AnnotationStatus.HUMAN_REVIEWED


# 5. RiskLevel exact numerator/denominator


def test_risk_level_aggregate_uses_exact_numerator_denominator() -> None:
    predictions, annotations, field_statuses = _make_full_dataset()
    predictions[0] = make_prediction(
        "k8s_541",
        workflow=make_workflow("k8s_541"),
        report=make_report("k8s_541", risk_level=RiskLevel.MEDIUM),
        markdown="# r",
    )
    predictions[1] = make_prediction(
        "k8s_753",
        workflow=make_workflow("k8s_753"),
        report=make_report("k8s_753", risk_level=RiskLevel.HIGH),
        markdown="# r",
    )
    predictions[2] = make_prediction(
        "k8s_1287",
        workflow=make_workflow("k8s_1287"),
        report=make_report("k8s_1287", risk_level=RiskLevel.LOW),
        markdown="# r",
    )
    annotations[0] = make_annotation("k8s_541", risk_level=RiskLevel.HIGH)
    annotations[1] = make_annotation("k8s_753", risk_level=RiskLevel.HIGH)
    annotations[2] = make_annotation("k8s_1287", risk_level=RiskLevel.LOW)
    for case_id in ("k8s_1472", "k8s_1602"):
        field_statuses[case_id] = [
            GroundTruthFieldStatus(
                field=field,
                status=(
                    FieldEvaluationStatus.NOT_EVALUATED
                    if field == "expected_risk_level"
                    else FieldEvaluationStatus.EVALUATED
                ),
                reason="risk level cannot be reliably annotated" if field == "expected_risk_level" else "reviewed",
            )
            for field in _ALL_FIELDS
        ]
    summary = FeatureRiskReviewEvaluator().evaluate(
        predictions=predictions,
        annotations=annotations,
        field_statuses=field_statuses,
    )
    metric = summary.risk_level_accuracy
    assert metric.status == MetricStatus.EVALUATED
    assert metric.numerator == 2
    assert metric.denominator == 3
    assert metric.value == pytest.approx(2 / 3)


# 6. Historical issue P/R/F1


def test_historical_issue_finding_precision_recall_f1() -> None:
    findings = [
        make_finding(evidence_ids=("e1",), issue_refs=("541",)),
        make_finding(evidence_ids=("e2",), evidence_source_ids={"e2": "99999"}),
    ]
    risk = make_risk(
        "k8s_541", findings=findings, evidence=[ev("e1"), ev("e2", source_id="99999")]
    )
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541", risk=risk), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541", issue_ids=("541", "116415"))
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
    )
    metric = result.historical_issue_finding
    assert metric.status == MetricStatus.EVALUATED
    assert metric.tp == 1
    assert metric.fp == 1
    assert metric.fn == 1
    assert metric.precision == pytest.approx(0.5)
    assert metric.recall == pytest.approx(0.5)
    assert metric.f1 == pytest.approx(0.5)


# 7. expected issue empty denominator semantics


def test_empty_expected_issue_set_with_nonempty_prediction() -> None:
    findings = [make_finding(evidence_ids=("e1",), issue_refs=("541",))]
    risk = make_risk("k8s_541", findings=findings, evidence=[ev("e1")])
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541", risk=risk), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541", issue_ids=())
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
    )
    metric = result.historical_issue_finding
    assert metric.fp == 1
    assert metric.precision == 0.0
    assert metric.recall is None
    assert metric.f1 is None
    assert metric.status == MetricStatus.EVALUATED


def test_both_issue_sets_empty_is_exact_empty_match_not_1_0() -> None:
    risk = make_risk("k8s_541", findings=[], evidence=[])
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541", risk=risk), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541", issue_ids=())
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
    )
    metric = result.historical_issue_finding
    assert metric.status == MetricStatus.METRIC_NOT_APPLICABLE
    assert metric.precision is None
    assert metric.recall is None
    assert metric.f1 is None
    assert result.exact_empty_match["historical_issue_finding"] is True


# 8. field NOT_EVALUATED


def test_field_not_evaluated_is_excluded_and_preserved() -> None:
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541"), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541")
    field_statuses = make_field_statuses("k8s_541", status=FieldEvaluationStatus.NOT_EVALUATED)
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=field_statuses
    )
    assert result.change_point.status == MetricStatus.NOT_EVALUATED
    assert result.risk_level.status == MetricStatus.NOT_EVALUATED


# 9. execution status != quality correctness


def test_execution_failure_not_counted_as_incorrect_risk_level() -> None:
    workflow = make_workflow("k8s_541", fail_document=True)
    prediction = make_prediction("k8s_541", workflow=workflow)
    annotation = make_annotation("k8s_541", risk_level=RiskLevel.HIGH)
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
    )
    assert result.execution_status == CaseExecutionStatus.FAILED
    assert result.risk_level.status == MetricStatus.EXECUTION_FAILED
    assert result.risk_level.correct is None
    assert result.change_point.status == MetricStatus.EXECUTION_FAILED


# 10. prediction/annotation case mismatch fail closed


def test_case_id_mismatch_fails_closed() -> None:
    prediction = make_prediction("k8s_541", workflow=make_workflow("k8s_541"))
    annotation = make_annotation("k8s_753")
    with pytest.raises(Exception, match="case_id mismatch"):
        FeatureRiskReviewEvaluator().evaluate_case(
            prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
        )


# 11. evaluator cannot mutate annotation


def test_evaluator_does_not_mutate_annotation() -> None:
    annotation = make_annotation("k8s_541")
    before = annotation.model_dump(mode="json")
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541"), report=make_report("k8s_541"), markdown="# r"
    )
    FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
    )
    assert annotation.model_dump(mode="json") == before
    assert annotation.annotation_status == AnnotationStatus.HUMAN_REVIEWED


# 12 + 13. per-case preserved; aggregate derived only from per-case


def test_aggregate_is_derived_only_from_per_case_results() -> None:
    predictions, annotations, field_statuses = _make_full_dataset()
    predictions[1] = make_prediction(
        "k8s_753",
        workflow=make_workflow("k8s_753", fail_risk=True),
        report=make_report("k8s_753", completeness=ReportCompleteness.PARTIAL_TEST_UNAVAILABLE),
        markdown="# r",
    )
    summary = FeatureRiskReviewEvaluator().evaluate(
        predictions=predictions, annotations=annotations, field_statuses=field_statuses
    )
    assert [pc.case_id for pc in summary.per_case] == list(FROZEN_CASE_IDS)
    assert summary.total_cases == 5
    assert summary.change_point.tp == sum(pc.change_point.tp for pc in summary.per_case)
    assert summary.change_point.fp == sum(pc.change_point.fp for pc in summary.per_case)
    assert summary.change_point.fn == sum(pc.change_point.fn for pc in summary.per_case)
    assert summary.e2e_status_counts.success == sum(
        int(pc.execution_status == CaseExecutionStatus.SUCCESS) for pc in summary.per_case
    )
    assert summary.e2e_workflow_success.numerator == summary.e2e_status_counts.success
    assert summary.e2e_workflow_success.denominator == 5
    assert summary.e2e_workflow_success.value == pytest.approx(summary.e2e_status_counts.success / 5)


# 14. evaluator does not modify runtime artifacts


def test_evaluator_does_not_mutate_prediction() -> None:
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541"), report=make_report("k8s_541"), markdown="# r"
    )
    before = prediction.model_dump(mode="json")
    annotation = make_annotation("k8s_541")
    FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
    )
    assert prediction.model_dump(mode="json") == before


# 15. Priority NOT_EVALUATED


def test_priority_correctness_remains_not_evaluated() -> None:
    predictions, annotations, field_statuses = _make_full_dataset()
    summary = FeatureRiskReviewEvaluator().evaluate(
        predictions=predictions, annotations=annotations, field_statuses=field_statuses
    )
    assert summary.priority_correctness.status == MetricStatus.NOT_EVALUATED
    for pc in summary.per_case:
        assert pc.priority_correctness.status == MetricStatus.NOT_EVALUATED


# 16. citation traceability not correctness


def test_citation_requires_adjudication_not_evidence_presence() -> None:
    findings = [make_finding(evidence_ids=("e1",))]
    risk = make_risk("k8s_541", findings=findings, evidence=[ev("e1")])
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541", risk=risk), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541")
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
    )
    assert result.citation_correctness.status == MetricStatus.NOT_EVALUATED


def test_citation_numerator_counts_only_supported() -> None:
    findings = [make_finding(evidence_ids=("e1",))]
    risk = make_risk("k8s_541", findings=findings, evidence=[ev("e1")])
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541", risk=risk), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541")
    adjudications = [
        _adjudication(
            metric=AdjudicationMetric.CITATION,
            prediction_index="0",
            expected_index=None,
            evidence_id="e1",
            verdict="SUPPORTED",
        )
    ]
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction,
        annotation=annotation,
        field_statuses=make_field_statuses("k8s_541"),
        adjudications=adjudications,
    )
    metric = result.citation_correctness
    assert metric.status == MetricStatus.EVALUATED
    assert metric.numerator == 1
    assert metric.denominator == 1
    assert metric.value == 1.0


# 17. citation adjudication strict validation


def test_citation_adjudication_rejects_unknown_evidence_id() -> None:
    findings = [make_finding(evidence_ids=("e1",))]
    risk = make_risk("k8s_541", findings=findings, evidence=[ev("e1")])
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541", risk=risk), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541")
    adjudications = [
        _adjudication(
            metric=AdjudicationMetric.CITATION,
            prediction_index="0",
            expected_index=None,
            evidence_id="missing-evidence",
            verdict="SUPPORTED",
        )
    ]
    with pytest.raises(Exception, match="evidence_id"):
        FeatureRiskReviewEvaluator().evaluate_case(
            prediction=prediction,
            annotation=annotation,
            field_statuses=make_field_statuses("k8s_541"),
            adjudications=adjudications,
        )


def test_citation_adjudication_rejects_invalid_rubric_and_duplicate() -> None:
    findings = [make_finding(evidence_ids=("e1",))]
    risk = make_risk("k8s_541", findings=findings, evidence=[ev("e1")])
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541", risk=risk), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541")
    invalid_rubric_issues = validate_manual_adjudications(
        [
            _adjudication(
                metric=AdjudicationMetric.CITATION,
                prediction_index="0",
                expected_index=None,
                evidence_id="e1",
                verdict="NOT_A_RUBRIC",
            )
        ],
        prediction,
        annotation,
    )
    assert any("invalid citation verdict" in issue for issue in invalid_rubric_issues)
    duplicates = [
        _adjudication(
            metric=AdjudicationMetric.CITATION,
            prediction_index="0",
            expected_index=None,
            evidence_id="e1",
            verdict="SUPPORTED",
        ),
        _adjudication(
            metric=AdjudicationMetric.CITATION,
            prediction_index="0",
            expected_index=None,
            evidence_id="e1",
            verdict="UNSUPPORTED",
        ),
    ]
    with pytest.raises(Exception, match="duplicate"):
        FeatureRiskReviewEvaluator().evaluate_case(
            prediction=prediction,
            annotation=annotation,
            field_statuses=make_field_statuses("k8s_541"),
            adjudications=duplicates,
        )


# 18. manual text matching 1:1 validation


def test_text_match_rejects_non_one_to_one_matches() -> None:
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541"), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541", change_points=("cp0", "cp1"))
    duplicate = [
        _adjudication(prediction_index="0", expected_index="0", verdict="MATCH"),
        _adjudication(prediction_index="0", expected_index="1", verdict="MATCH"),
    ]
    with pytest.raises(Exception, match="matched more than once"):
        FeatureRiskReviewEvaluator().evaluate_case(
            prediction=prediction,
            annotation=annotation,
            field_statuses=make_field_statuses("k8s_541"),
            adjudications=duplicate,
        )


def test_text_match_rejects_out_of_range_index() -> None:
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541"), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541", change_points=("cp0",))
    issues = validate_manual_adjudications(
        [_adjudication(prediction_index="5", expected_index="0", verdict="MATCH")],
        prediction,
        annotation,
    )
    assert any("out of range" in issue for issue in issues)


# 19. missing adjudication -> NOT_EVALUATED


def test_missing_adjudication_is_not_evaluated_not_all_false() -> None:
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541"), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541")
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
    )
    assert result.change_point.status == MetricStatus.NOT_EVALUATED
    assert result.risk_area.status == MetricStatus.NOT_EVALUATED
    assert result.coverage_gap.status == MetricStatus.EVALUATED
    assert result.coverage_gap.fn == 1  # prediction 为空时无需语义 adjudication
    assert result.change_point.fp == 0


# 20. exact frozen 5-case set


def test_frozen_case_set_must_be_exactly_the_five() -> None:
    assert validate_frozen_case_set(list(FROZEN_CASE_IDS)) == []
    assert validate_frozen_case_set(["k8s_541", "k8s_753", "k8s_1287", "k8s_1472"]) != []
    assert validate_frozen_case_set([*FROZEN_CASE_IDS, "k8s_9999"]) != []
    assert validate_frozen_case_set(["k8s_541", "k8s_541", "k8s_753", "k8s_1287", "k8s_1472", "k8s_1602"]) != []


def test_annotation_validator_rejects_duplicate_missing_unknown() -> None:
    annotations = [make_annotation(case_id) for case_id in FROZEN_CASE_IDS]
    field_statuses = {case_id: make_field_statuses(case_id) for case_id in FROZEN_CASE_IDS}
    assert validate_annotations(annotations, field_statuses) == []

    duplicate = [make_annotation("k8s_541"), *annotations]
    assert any("duplicate" in issue for issue in validate_annotations(duplicate, field_statuses))

    missing = annotations[1:]
    assert any("missing" in issue for issue in validate_annotations(missing, field_statuses))

    unknown = [make_annotation("k8s_9999"), *annotations[1:]]
    assert any("unknown" in issue for issue in validate_annotations(unknown, field_statuses))


def test_field_status_missing_or_unknown_is_rejected() -> None:
    annotations = [make_annotation(case_id) for case_id in FROZEN_CASE_IDS]
    field_statuses = {case_id: make_field_statuses(case_id) for case_id in FROZEN_CASE_IDS}
    field_statuses["k8s_541"] = field_statuses["k8s_541"][1:]
    assert any("missing field status" in issue for issue in validate_annotations(annotations, field_statuses))

    field_statuses["k8s_541"] = [
        *make_field_statuses("k8s_541"),
        GroundTruthFieldStatus(field="expected_priority", status=FieldEvaluationStatus.EVALUATED, reason="x"),
    ]
    assert any("unknown field status" in issue for issue in validate_annotations(annotations, field_statuses))


def test_duplicate_field_status_is_rejected() -> None:
    annotations = [make_annotation(case_id) for case_id in FROZEN_CASE_IDS]
    field_statuses = {case_id: make_field_statuses(case_id) for case_id in FROZEN_CASE_IDS}
    field_statuses["k8s_541"].append(field_statuses["k8s_541"][0])
    assert any("duplicate field status" in issue for issue in validate_annotations(annotations, field_statuses))


def test_field_status_loader_rejects_duplicate_case_identity(tmp_path: Path) -> None:
    copied = tmp_path / "dataset"
    shutil.copytree(ASSET_ROOT, copied)
    path = copied / "annotations" / "field_status.v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cases"].append(payload["cases"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception, match="duplicate field-status case_id"):
        load_ground_truth_field_statuses(copied)


def test_not_evaluated_field_requires_reason() -> None:
    annotations = [make_annotation(case_id) for case_id in FROZEN_CASE_IDS]
    field_statuses = {case_id: make_field_statuses(case_id) for case_id in FROZEN_CASE_IDS}
    field_statuses["k8s_541"] = [
        GroundTruthFieldStatus(field=field, status=FieldEvaluationStatus.NOT_EVALUATED, reason="")
        for field in _ALL_FIELDS
    ]
    assert any("requires reason" in issue for issue in validate_annotations(annotations, field_statuses))


# 21 + 22. environment failure retry eligibility vs business result


def test_environment_failure_is_retry_eligible_and_not_a_business_result() -> None:
    prediction = make_prediction(
        "k8s_541",
        classification=ExecutionClassification.ENVIRONMENT_FAILURE,
        workflow=None,
        report=None,
    )
    annotation = make_annotation("k8s_541")
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
    )
    assert result.execution_status == CaseExecutionStatus.ENVIRONMENT_FAILURE
    assert result.retry_eligible is True
    assert is_retry_eligible(ExecutionClassification.ENVIRONMENT_FAILURE) is True
    assert result.risk_level.status == MetricStatus.EXECUTION_FAILED
    assert result.report_generation.status == MetricStatus.EXECUTION_FAILED


def test_business_result_is_not_retry_eligible() -> None:
    for fail_document, fail_risk, fail_test in ((True, False, False), (False, True, False), (False, False, True)):
        workflow = make_workflow("k8s_541", fail_document=fail_document, fail_risk=fail_risk, fail_test=fail_test)
        prediction = make_prediction(
            "k8s_541", workflow=workflow, report=make_report("k8s_541"), markdown="# r"
        )
        annotation = make_annotation("k8s_541")
        result = FeatureRiskReviewEvaluator().evaluate_case(
            prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
        )
        assert result.retry_eligible is False
        assert result.execution_status in (CaseExecutionStatus.FAILED, CaseExecutionStatus.PARTIAL)
    assert is_retry_eligible(ExecutionClassification.BUSINESS_RESULT) is False


# 23. denominator 0 -> NOT_APPLICABLE


def test_zero_denominator_is_not_applicable_without_value() -> None:
    metric = build_set_metric(0, 0, 0)
    assert metric.status == MetricStatus.METRIC_NOT_APPLICABLE
    assert metric.value is None
    precision, recall, f1 = precision_recall_f1(0, 0, 0)
    assert precision is None and recall is None and f1 is None


# 24. citation UNVERIFIABLE excluded denominator


def test_citation_unverifiable_excluded_using_distinct_evidence() -> None:
    findings = [make_finding(evidence_ids=("e1", "e2"))]
    risk = make_risk("k8s_541", findings=findings, evidence=[ev("e1"), ev("e2")])
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541", risk=risk), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541")
    adjudications = [
        _adjudication(
            metric=AdjudicationMetric.CITATION,
            prediction_index="0",
            expected_index=None,
            evidence_id="e1",
            verdict="SUPPORTED",
        ),
        _adjudication(
            metric=AdjudicationMetric.CITATION,
            prediction_index="0",
            expected_index=None,
            evidence_id="e2",
            verdict="UNVERIFIABLE",
        ),
    ]
    metric = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction,
        annotation=annotation,
        field_statuses=make_field_statuses("k8s_541"),
        adjudications=adjudications,
    ).citation_correctness
    assert metric.numerator == 1
    assert metric.denominator == 1
    assert metric.value == 1.0
    assert metric.unverifiable_count == 1


def test_incomplete_citation_adjudication_fails_closed() -> None:
    findings = [make_finding(evidence_ids=("e1", "e2"))]
    risk = make_risk("k8s_541", findings=findings, evidence=[ev("e1"), ev("e2")])
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541", risk=risk), report=make_report("k8s_541"), markdown="# r"
    )
    with pytest.raises(Exception, match="incomplete citation adjudication"):
        FeatureRiskReviewEvaluator().evaluate_case(
            prediction=prediction,
            annotation=make_annotation("k8s_541"),
            field_statuses=make_field_statuses("k8s_541"),
            adjudications=[
                _adjudication(
                    metric=AdjudicationMetric.CITATION,
                    prediction_index="0",
                    expected_index=None,
                    evidence_id="e1",
                    verdict="SUPPORTED",
                )
            ],
        )


# 25. aggregate TP/FP/FN consistency with stored value


def test_aggregate_metric_value_consistent_with_counts() -> None:
    predictions, annotations, field_statuses = _make_full_dataset()
    summary = FeatureRiskReviewEvaluator().evaluate(
        predictions=predictions, annotations=annotations, field_statuses=field_statuses
    )
    tp = summary.change_point.tp
    fp = summary.change_point.fp
    fn = summary.change_point.fn
    assert summary.change_point.numerator == tp
    assert summary.change_point.denominator == tp + fp + fn
    expected = build_set_metric(tp, fp, fn)
    assert summary.change_point.precision == expected.precision
    assert summary.change_point.recall == expected.recall
    assert summary.change_point.f1 == expected.f1


# 26. Ground Truth state after the authorized human review


def test_ground_truth_state_is_currently_ground_truth_ready() -> None:
    annotations = [
        EvaluationAnnotation.model_validate(a)
        for a in json.loads(
            (ASSET_ROOT / "annotations" / "annotations.v1.json").read_text(encoding="utf-8")
        )["annotations"]
    ]
    field_statuses = load_ground_truth_field_statuses(ASSET_ROOT)
    report = detect_ground_truth_state(annotations, field_statuses)
    assert report.state == GroundTruthState.GROUND_TRUTH_READY
    assert report.reviewed_cases == len(FROZEN_CASE_IDS)
    assert report.issues == []


def test_ground_truth_ready_requires_five_reviewed_and_valid_field_status() -> None:
    annotations = [make_annotation(case_id) for case_id in FROZEN_CASE_IDS]
    field_statuses = {case_id: make_field_statuses(case_id) for case_id in FROZEN_CASE_IDS}
    report = detect_ground_truth_state(annotations, field_statuses)
    assert report.state == GroundTruthState.GROUND_TRUTH_READY

    partial = annotations[:-1]
    assert detect_ground_truth_state(partial, field_statuses).state == GroundTruthState.HUMAN_REVIEW_REQUIRED


# Historical evidence @5: KEP not in issue denominator; composition preserved


def test_historical_evidence_at5_excludes_kep_sections() -> None:
    evidence = [
        ev("snap1", source_type="kubernetes_issue_snapshot", source_id="116415"),
        ev("snap2", source_type="kubernetes_issue_snapshot", source_id="122760"),
        ev("track", source_type="github_enhancement_tracking_issue", source_id="541"),
        ev("kep1", source_type="kubernetes_enhancement_proposal", source_id="1287"),
        ev("kep2", source_type="kubernetes_enhancement_proposal", source_id="753"),
    ]
    risk = make_risk("k8s_541", findings=[], evidence=evidence)
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541", risk=risk), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541", issue_ids=("541", "116415"))
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
    )
    metric = result.historical_evidence_at_5
    assert metric.tp == 2
    assert metric.fp == 1
    assert metric.fn == 0
    assert result.top5_source_type_composition == {
        "kubernetes_issue_snapshot": 2,
        "github_enhancement_tracking_issue": 1,
        "kubernetes_enhancement_proposal": 2,
    }


# Component normalization is deterministic and frozen


def test_component_normalization_is_frozen_and_deterministic() -> None:
    assert normalize_component("  client-go  ") == "client-go"
    assert normalize_component("Client-Go") == "client-go"
    assert normalize_component("client  \t go") == "client go"
    assert normalize_component("client-go") == normalize_component("client-go")


def test_component_metric_uses_normalized_set_matching() -> None:
    document = make_document(components=["client-go", "kubelet"])
    prediction = make_prediction(
        "k8s_541", workflow=make_workflow("k8s_541", document=document), report=make_report("k8s_541"), markdown="# r"
    )
    annotation = make_annotation("k8s_541", components=("Client-Go", "kube-apiserver"))
    result = FeatureRiskReviewEvaluator().evaluate_case(
        prediction=prediction, annotation=annotation, field_statuses=make_field_statuses("k8s_541")
    )
    metric = result.component
    assert metric.tp == 1
    assert metric.fp == 1
    assert metric.fn == 1
    assert metric.precision == pytest.approx(0.5)
    assert metric.recall == pytest.approx(0.5)


# Manifest freeze refuses when GT pending


def test_manifest_is_not_frozen_when_ground_truth_pending(tmp_path: Path) -> None:
    copied = tmp_path / "dataset"
    shutil.copytree(ASSET_ROOT, copied)
    annotations = [
        EvaluationAnnotation.model_validate(a)
        for a in json.loads(
            (copied / "annotations" / "annotations.v1.json").read_text(encoding="utf-8")
        )["annotations"]
    ]
    annotations[0] = annotations[0].model_copy(update={"annotation_status": AnnotationStatus.PENDING})
    field_statuses = load_ground_truth_field_statuses(copied)
    state = detect_ground_truth_state(annotations, field_statuses).state
    assert state == GroundTruthState.HUMAN_REVIEW_REQUIRED
    manifest = build_runtime_manifest(
        root=copied, backend_root=PROJECT_ROOT, git_commit="fakecommit", gt_state=state
    )
    assert manifest.freeze_status == FreezeStatus.NOT_FROZEN
    assert manifest.model_ref == "deepseek/deepseek-chat"
    assert manifest.temperature == 0.0
    assert manifest.top_k == 5
    assert manifest.case_ids == list(FROZEN_CASE_IDS)
    assert len(manifest.annotation_digest) == 64
    assert manifest.risk_level_rubric_authority.path.endswith("RISK_LEVEL_RUBRIC.v1.md")
    assert len(manifest.risk_level_rubric_authority.sha256) == 64


def test_annotation_digest_is_deterministic() -> None:
    a = b"annotation bytes"
    b = b"field status bytes"
    assert compute_annotation_digest(a, b) == compute_annotation_digest(a, b)
    assert compute_annotation_digest(a, b) != compute_annotation_digest(b, a)


# Manual adjudication template is structural only; no fake prediction artifacts


def test_real_adjudications_load_and_pass_full_contract_validation() -> None:
    # A. adjudication.template remains a structural (placeholder) template
    template = ASSET_ROOT / "experiments" / "wp4" / "adjudications" / "adjudication.template"
    payload = json.loads(template.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "feature-risk-review-adjudications.v1"
    assert payload["adjudications"]  # structural template exists

    adjudication_dir = ASSET_ROOT / "experiments" / "wp4" / "adjudications"

    # B. exactly the four normal adjudications exist for the successful cases
    assert {p.stem for p in adjudication_dir.glob("k8s_*.json")} == {
        "k8s_541",
        "k8s_753",
        "k8s_1472",
        "k8s_1602",
    }
    loaded = load_manual_adjudications(ASSET_ROOT)
    assert {a.case_id for a in loaded} == {"k8s_541", "k8s_753", "k8s_1472", "k8s_1602"}

    annotations = load_evaluation_annotations(ASSET_ROOT)
    ann_by_case = {a.case_id: a for a in annotations}

    # B-G: canonical typed loader + identity + row contract + evaluator 1:1/citation completeness
    for case_id in ("k8s_541", "k8s_753", "k8s_1472", "k8s_1602"):
        prediction = load_runtime_prediction_artifact(
            ASSET_ROOT / "experiments" / "wp4" / "predictions" / f"{case_id}.json"
        )
        case_adjs = [a for a in loaded if a.case_id == case_id]
        assert case_adjs
        for a in case_adjs:
            # C. identity: every entry belongs to this case
            assert a.case_id == case_id
            # D. reviewer / reviewed_at / review_note satisfy the contract
            assert a.reviewer.strip()
            assert a.reviewed_at is not None
            assert a.review_note and a.review_note.strip()
        # E+F. 1:1 text-match and citation-pair completeness are enforced fail-closed
        assert validate_manual_adjudications(case_adjs, prediction, ann_by_case[case_id]) == []
        assert load_manual_adjudications(ASSET_ROOT) == loaded

    # G. k8s_1287 workflow FAILED -> must NOT have a normal adjudication
    assert not (adjudication_dir / "k8s_1287.json").exists()
    assert not any(a.case_id == "k8s_1287" for a in loaded)


def test_adjudication_loader_rejects_file_and_entry_case_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "dataset"
    directory = root / "experiments" / "wp4" / "adjudications"
    directory.mkdir(parents=True)
    payload = {
        "schema_version": "feature-risk-review-adjudications.v1",
        "case_id": "k8s_541",
        "adjudications": [
            _adjudication(case_id="k8s_753").model_dump(mode="json")
        ],
    }
    (directory / "k8s_541.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception, match="must match file case_id"):
        load_manual_adjudications(root)


# Report generation success with fixed denominator 5


def test_report_generation_uses_fixed_denominator_5() -> None:
    predictions, annotations, field_statuses = _make_full_dataset()
    predictions[2] = make_prediction("k8s_1287", workflow=make_workflow("k8s_1287"))
    summary = FeatureRiskReviewEvaluator().evaluate(
        predictions=predictions, annotations=annotations, field_statuses=field_statuses
    )
    assert summary.report_generation_success.denominator == 5
    assert summary.report_generation_success.numerator == 4
    assert summary.report_generation_success.value == pytest.approx(0.8)


def test_partial_workflow_with_partial_report_counts_as_report_success() -> None:
    predictions, annotations, field_statuses = _make_full_dataset()
    predictions[1] = make_prediction(
        "k8s_753",
        workflow=make_workflow("k8s_753", fail_test=True),
        report=make_report("k8s_753", completeness=ReportCompleteness.PARTIAL_TEST_UNAVAILABLE),
        markdown="# r",
    )
    summary = FeatureRiskReviewEvaluator().evaluate(
        predictions=predictions, annotations=annotations, field_statuses=field_statuses
    )
    assert summary.report_generation_success.numerator == 5
    for pc in summary.per_case:
        if pc.case_id == "k8s_753":
            assert pc.execution_status == CaseExecutionStatus.PARTIAL
            assert pc.report_completeness == ReportCompleteness.PARTIAL_TEST_UNAVAILABLE


def test_evaluate_requires_frozen_five_case_coverage() -> None:
    predictions, annotations, field_statuses = _make_full_dataset()
    predictions = predictions[:-1]
    annotations = annotations[:-1]
    field_statuses.pop("k8s_1602")
    with pytest.raises(Exception, match="missing frozen case"):
        FeatureRiskReviewEvaluator().evaluate(
            predictions=predictions, annotations=annotations, field_statuses=field_statuses
        )


def test_evaluate_rejects_pending_ground_truth() -> None:
    predictions, annotations, field_statuses = _make_full_dataset(reviewed=False)
    with pytest.raises(Exception, match="requires GROUND_TRUTH_READY"):
        FeatureRiskReviewEvaluator().evaluate(
            predictions=predictions, annotations=annotations, field_statuses=field_statuses
        )


def test_evaluate_rejects_duplicate_prediction_identity() -> None:
    predictions, annotations, field_statuses = _make_full_dataset()
    predictions.append(predictions[0])
    with pytest.raises(Exception, match="duplicate case_id"):
        FeatureRiskReviewEvaluator().evaluate(
            predictions=predictions, annotations=annotations, field_statuses=field_statuses
        )


def test_incomplete_text_adjudication_fails_closed() -> None:
    document = make_document(
        change_points=[
            FeatureChangePoint(description="cp0", affected_components=["client-go"]),
            FeatureChangePoint(description="cp1", affected_components=["kubelet"]),
        ]
    )
    prediction = make_prediction(
        "k8s_541",
        workflow=make_workflow("k8s_541", document=document),
        report=make_report("k8s_541"),
        markdown="# r",
    )
    annotation = make_annotation("k8s_541", change_points=("cp0", "cp1"))
    with pytest.raises(Exception, match="incomplete text match adjudication"):
        FeatureRiskReviewEvaluator().evaluate_case(
            prediction=prediction,
            annotation=annotation,
            field_statuses=make_field_statuses("k8s_541"),
            adjudications=[_adjudication(prediction_index="0", expected_index="0", verdict="MATCH")],
        )


def test_environment_failure_contract_rejects_business_output() -> None:
    with pytest.raises(Exception, match="must not contain business output"):
        make_prediction(
            "k8s_541",
            classification=ExecutionClassification.ENVIRONMENT_FAILURE,
            workflow=make_workflow("k8s_541"),
        )
