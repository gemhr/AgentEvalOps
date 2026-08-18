"""RegressionReportService 最小 Application tests。"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

from uuid import UUID

import pytest

from app.core.evaluation import (
    AlignedResultComparison,
    CaseVersionRef,
    ComparisonReason,
    EvaluationRunComparison,
    RegressionClassification,
    RegressionReport,
    RegressionReportContractError,
    ReleaseDecision,
    RunComparisonProvenance,
    VersionRef,
)
from app.services.evaluation import RegressionReportService

PROJECT_ID = UUID("10000000-0000-4000-a000-000000000001")
BASELINE_RUN_ID = UUID("20000000-0000-4000-a000-000000000001")
CANDIDATE_RUN_ID = UUID("20000000-0000-4000-a000-000000000002")


def provenance() -> RunComparisonProvenance:
    return RunComparisonProvenance(
        dataset_id="dataset",
        dataset_version="d1",
        suite_id="suite",
        suite_version="s1",
        execution_target_id="target",
        execution_target_kind="FIXTURE",
        target_version_ref=VersionRef("git", "abc"),
    )


def slot(
    case_id: str,
    classification: RegressionClassification,
    *,
    case_version: str = "v1",
    evaluator_id: str = "eval",
    score_regressed: bool | None = None,
) -> AlignedResultComparison:
    reason = {
        RegressionClassification.REGRESSION: ComparisonReason.VERDICT_REGRESSED,
        RegressionClassification.IMPROVEMENT: ComparisonReason.VERDICT_IMPROVED,
        RegressionClassification.UNCHANGED: ComparisonReason.VERDICT_UNCHANGED,
        RegressionClassification.NOT_COMPARABLE: ComparisonReason.CANDIDATE_MISSING,
    }[classification]
    return AlignedResultComparison(
        case_id=case_id,
        case_version=case_version,
        evaluator_id=evaluator_id,
        evaluator_version="v1",
        baseline_result_id="base-1",
        candidate_result_id="cand-1",
        classification=classification,
        reason=reason,
        score_regressed=score_regressed,
    )


def comparison(
    slots: tuple[AlignedResultComparison, ...],
) -> EvaluationRunComparison:
    return EvaluationRunComparison(
        project_id=PROJECT_ID,
        baseline_run_id=BASELINE_RUN_ID,
        candidate_run_id=CANDIDATE_RUN_ID,
        baseline_provenance=provenance(),
        candidate_provenance=provenance(),
        comparisons=slots,
    )


def build(
    comparison_obj: EvaluationRunComparison,
    critical_refs: tuple[CaseVersionRef, ...],
) -> RegressionReport:
    return RegressionReportService().build_report(comparison_obj, critical_refs)


def test_critical_regression_blocks_release() -> None:
    report = build(
        comparison((slot("case-a", RegressionClassification.REGRESSION),)),
        (CaseVersionRef("case-a", "v1"),),
    )
    assert report.release_decision is ReleaseDecision.FAIL
    assert report.regression_count == 1
    assert report.critical_regressions == (report.comparisons[0],)
    assert report.critical_not_comparable == ()


def test_non_critical_regression_is_report_only() -> None:
    report = build(comparison((slot("case-a", RegressionClassification.REGRESSION),)), ())
    assert report.release_decision is ReleaseDecision.PASS
    assert report.regression_count == 1
    assert report.regressions == (report.comparisons[0],)
    assert report.critical_regressions == ()


def test_critical_not_comparable_blocks_release() -> None:
    report = build(
        comparison((slot("case-a", RegressionClassification.NOT_COMPARABLE),)),
        (CaseVersionRef("case-a", "v1"),),
    )
    assert report.release_decision is ReleaseDecision.FAIL
    assert report.not_comparable_count == 1
    assert report.critical_not_comparable == (report.comparisons[0],)
    assert report.critical_regressions == ()


def test_non_critical_not_comparable_is_report_only() -> None:
    report = build(comparison((slot("case-a", RegressionClassification.NOT_COMPARABLE),)), ())
    assert report.release_decision is ReleaseDecision.PASS
    assert report.not_comparable_count == 1
    assert report.critical_not_comparable == ()


@pytest.mark.parametrize(
    "classification",
    [RegressionClassification.IMPROVEMENT, RegressionClassification.UNCHANGED],
)
def test_critical_improvement_and_unchanged_do_not_block(classification: RegressionClassification) -> None:
    report = build(
        comparison((slot("case-a", classification),)),
        (CaseVersionRef("case-a", "v1"),),
    )
    assert report.release_decision is ReleaseDecision.PASS
    assert report.critical_regressions == ()
    assert report.critical_not_comparable == ()


def test_score_only_regression_evidence_does_not_block() -> None:
    report = build(
        comparison((slot("case-a", RegressionClassification.UNCHANGED, score_regressed=True),)),
        (CaseVersionRef("case-a", "v1"),),
    )
    assert report.comparisons[0].classification is RegressionClassification.UNCHANGED
    assert report.comparisons[0].score_regressed is True
    assert report.release_decision is ReleaseDecision.PASS


def test_counts_and_comparisons_are_correct() -> None:
    slots = (
        slot("case-a", RegressionClassification.REGRESSION),
        slot("case-b", RegressionClassification.IMPROVEMENT),
        slot("case-c", RegressionClassification.UNCHANGED),
        slot("case-d", RegressionClassification.NOT_COMPARABLE),
    )
    report = build(comparison(slots), ())
    assert report.total_count == 4
    assert report.regression_count == 1
    assert report.improvement_count == 1
    assert report.unchanged_count == 1
    assert report.not_comparable_count == 1
    assert report.comparisons == slots
    assert report.regressions == (slots[0],)
    assert report.release_decision is ReleaseDecision.PASS


def test_duplicate_critical_refs_fail_closed() -> None:
    comparison_obj = comparison((slot("case-a", RegressionClassification.REGRESSION),))
    with pytest.raises(RegressionReportContractError, match="duplicate critical case ref"):
        build(comparison_obj, (CaseVersionRef("case-a", "v1"), CaseVersionRef("case-a", "v1")))


@pytest.mark.parametrize(
    "refs",
    [
        (CaseVersionRef("unknown-case", "v1"),),
        (CaseVersionRef("case-a", "v2"),),
    ],
)
def test_critical_ref_absent_or_wrong_version_fails_closed(
    refs: tuple[CaseVersionRef, ...],
) -> None:
    comparison_obj = comparison((slot("case-a", RegressionClassification.REGRESSION),))
    with pytest.raises(RegressionReportContractError, match="outside the comparison universe"):
        build(comparison_obj, refs)


def test_empty_comparison_policy() -> None:
    empty = comparison(())
    assert build(empty, ()).release_decision is ReleaseDecision.PASS
    with pytest.raises(RegressionReportContractError, match="empty comparison universe"):
        build(empty, (CaseVersionRef("case-a", "v1"),))


def test_critical_case_with_multiple_evaluator_slots_blocks() -> None:
    slots = (
        slot("case-b", RegressionClassification.REGRESSION),
        slot("case-a", RegressionClassification.UNCHANGED, evaluator_id="e1"),
        slot("case-a", RegressionClassification.REGRESSION, evaluator_id="e2"),
    )
    report = build(comparison(slots), (CaseVersionRef("case-a", "v1"),))
    assert report.release_decision is ReleaseDecision.FAIL
    # 子集保持 WP1 comparison 顺序：case-b 的 regression 在 case-a/e2 之前。
    assert [item.evaluator_id for item in report.regressions] == ["eval", "e2"]
    assert [item.evaluator_id for item in report.critical_regressions] == ["e2"]


def test_critical_case_refs_are_canonical_and_sorted() -> None:
    report = build(
        comparison(
            (
                slot("case-b", RegressionClassification.UNCHANGED),
                slot("case-a", RegressionClassification.UNCHANGED),
            )
        ),
        (CaseVersionRef("case-b", "v1"), CaseVersionRef("case-a", "v1")),
    )
    assert report.critical_case_refs == (
        CaseVersionRef("case-a", "v1"),
        CaseVersionRef("case-b", "v1"),
    )
