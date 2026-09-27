"""RegressionReportService 最小 Application tests。"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

from uuid import UUID

import pytest

from app.core.evaluation import (
    AlignedResultComparison,
    AttemptAvailability,
    CaseVersionRef,
    ComparisonCompatibility,
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
    candidate_attempt_outcome: AttemptAvailability = AttemptAvailability.SUCCESS,
    candidate_result_id: str | None = "cand-1",
    compatibility: ComparisonCompatibility = ComparisonCompatibility.COMPARABLE,
    reason_codes: tuple[str, ...] = (),
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
        candidate_result_id=candidate_result_id,
        classification=classification,
        reason=reason,
        score_regressed=score_regressed,
        compatibility=compatibility,
        reason_codes=reason_codes,
        candidate_attempt_outcome=candidate_attempt_outcome,
        baseline_attempt_outcome=AttemptAvailability.SUCCESS,
        candidate_required=True,
        baseline_required=True,
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
    "required_slot",
    [
        slot("case-a", RegressionClassification.NOT_COMPARABLE, candidate_attempt_outcome=AttemptAvailability.OUTCOME_UNKNOWN),
        slot("case-a", RegressionClassification.NOT_COMPARABLE, candidate_result_id=None),
        slot("case-a", RegressionClassification.NOT_COMPARABLE, reason_codes=("EVALUATOR_ERROR",)),
        slot(
            "case-a",
            RegressionClassification.NOT_COMPARABLE,
            compatibility=ComparisonCompatibility.CONDITIONALLY_COMPARABLE,
            reason_codes=("TARGET_BINDING_DRIFT",),
        ),
    ],
)
def test_required_incomplete_evidence_fails_release(required_slot: AlignedResultComparison) -> None:
    report = build(comparison((required_slot,)), ())
    assert report.release_decision is ReleaseDecision.FAIL
    assert report.incomplete_required_evidence == (required_slot,)


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
    assert build(empty, ()).release_decision is ReleaseDecision.FAIL
    from dataclasses import replace
    optional_only = replace(slot("case-a", RegressionClassification.UNCHANGED), candidate_required=False)
    assert build(comparison((optional_only,)), ()).release_decision is ReleaseDecision.FAIL
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


@pytest.mark.parametrize("code", ("LEGACY_INSUFFICIENT_PROVENANCE", "INSUFFICIENT_SUBJECT_PROVENANCE", "INSUFFICIENT_EVALUATOR_PROVENANCE", "REQUIRED_EVIDENCE_MISSING", "JUDGE_BINDING_CHANGED"))
def test_required_provenance_gap_blocks_with_valid_critical_case(code):
    rows = (slot("required", RegressionClassification.NOT_COMPARABLE, reason_codes=(code,), compatibility=ComparisonCompatibility.INCOMPARABLE),
            slot("critical", RegressionClassification.UNCHANGED))
    report = build(comparison(rows), (CaseVersionRef("critical", "v1"),))
    assert report.release_decision is ReleaseDecision.FAIL
    assert report.incomplete_required_evidence == (rows[0],)


def test_critical_refs_preserve_both_versions_and_ci_acceptance():
    from dataclasses import replace
    from datetime import datetime, timezone
    from scripts.ci.release_gate import serialize_report

    row = replace(slot("case-a", RegressionClassification.REGRESSION), baseline_case_version="v1", candidate_case_version="v2", case_version="v2")
    now = datetime.now(timezone.utc)
    value = replace(comparison((row,)), accepted_conditional_reasons=("CASE_VERSION_LABEL_DRIFT",), computed_at=now)
    for version in ("v1", "v2"):
        report = build(value, (CaseVersionRef("case-a", version),))
        assert report.critical_regressions == (row,)
        assert report.release_decision is ReleaseDecision.FAIL
        payload = serialize_report(report)
        assert payload["accepted_conditional_reasons"] == ["CASE_VERSION_LABEL_DRIFT"]
        assert payload["computed_at"] == now.isoformat()
    with pytest.raises(RegressionReportContractError):
        build(value, (CaseVersionRef("case-a", "v3"),))
    removed = replace(row, candidate_case_version=None, candidate_required=False, classification=RegressionClassification.NOT_COMPARABLE)
    assert build(comparison((removed,)), (CaseVersionRef("case-a", "v1"),)).release_decision is ReleaseDecision.FAIL
