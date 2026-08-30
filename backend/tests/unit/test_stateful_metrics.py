"""Metric denominator 契约：PASS+FAIL 为 evaluable_denominator，BLOCKED/NA 单列。"""

# ruff: noqa: D101, D105, D415

import pytest

from app.core.evaluation.stateful_assertion import (
    AssertionDimension,
    AssertionStatus,
    BlockReason,
    EvidenceGapClassification,
    FailureTaxonomy,
    MemoryAssertion,
)
from app.core.evaluation.stateful_metrics import (
    EXPECTED_EVIDENCE_LIMITATION_BLOCKED_COUNT_METRIC,
    build_evaluation_infra_failure_rate,
    build_expected_evidence_limitation_blocked_aggregate,
    build_failure_rate_aggregate,
    build_metric_aggregate,
    build_runtime_block_rate,
    expected_evidence_limitation_blocked_count,
    scenario_success_assertion,
    status_counts,
)


def assertion(assertion_id, status, *, dimension=AssertionDimension.FORMATION, required=True, blocked_by=None):
    blocked_by = (
        blocked_by
        if blocked_by is not None
        else (
            BlockReason.RUNTIME
            if status is AssertionStatus.BLOCKED and assertion_id.endswith("rt")
            else (BlockReason.EVIDENCE_CAPTURE if status is AssertionStatus.BLOCKED else None)
        )
    )
    return MemoryAssertion(
        assertion_id=assertion_id,
        dimension=dimension,
        status=status,
        expected=None,
        actual_evidence=None,
        failure_taxonomy=(FailureTaxonomy.FORMATION_FALSE_NEGATIVE if status is AssertionStatus.FAIL else None),
        blocked_by=blocked_by,
        reason="ok" if status is AssertionStatus.PASS else None,
        required=required,
    )


def test_metric_aggregate_denominator_is_pass_plus_fail():
    assertions = [
        assertion("a", AssertionStatus.PASS),
        assertion("b", AssertionStatus.FAIL),
        assertion("c", AssertionStatus.BLOCKED),
        assertion("d", AssertionStatus.NOT_APPLICABLE),
    ]
    aggregate = build_metric_aggregate("m", assertions)
    assert aggregate.passed == 1
    assert aggregate.failed == 1
    assert aggregate.blocked == 1
    assert aggregate.not_applicable == 1
    assert aggregate.evaluable_denominator == 2
    assert aggregate.value == 0.5


def test_metric_aggregate_no_evaluable_is_none():
    aggregate = build_metric_aggregate("m", [assertion("a", AssertionStatus.BLOCKED)])
    assert aggregate.evaluable_denominator == 0
    assert aggregate.value is None


def test_runtime_block_rate_retained_separately():
    assertions = [
        assertion("a.rt", AssertionStatus.BLOCKED),
        assertion("b", AssertionStatus.PASS),
        assertion("c.ev", AssertionStatus.BLOCKED),
        assertion("d", AssertionStatus.NOT_APPLICABLE),
    ]
    rate = build_runtime_block_rate(assertions)
    # required applicable = a.rt, b, c.ev (d NA excluded)
    assert rate.denominator == 3
    assert rate.numerator == 1  # only runtime-blocked
    assert rate.value == pytest.approx(1 / 3)


def test_evaluation_infra_failure_rate_separate_from_runtime():
    assertions = [
        assertion("a.rt", AssertionStatus.BLOCKED),
        assertion("c.ev", AssertionStatus.BLOCKED),
        assertion("b", AssertionStatus.PASS),
    ]
    infra = build_evaluation_infra_failure_rate(assertions)
    assert infra.numerator == 1  # only evidence_capture blocked
    assert infra.value == pytest.approx(1 / 3)
    runtime = build_runtime_block_rate(assertions)
    assert runtime.numerator == 1


def test_optional_failures_not_in_required_denominator():
    assertions = [
        assertion("a", AssertionStatus.FAIL, required=False),
        assertion("b", AssertionStatus.PASS),
    ]
    rate = build_runtime_block_rate(assertions)
    assert rate.denominator == 1
    assert rate.numerator == 0
    assert rate.value == 0.0


def test_scenario_success_assertion_rules():
    pass_assertion = scenario_success_assertion("scn.outcome", 5, 0, 0, 1)
    assert pass_assertion.status is AssertionStatus.PASS

    fail_assertion = scenario_success_assertion("scn.outcome", 4, 1, 0, 0, child_failure_taxonomies=["RETRIEVAL_MISS"])
    assert fail_assertion.status is AssertionStatus.FAIL
    # E1-R2: scenario roll-up must not fabricate FINAL_STATE_MISMATCH
    assert fail_assertion.failure_taxonomy is None
    assert list(fail_assertion.actual_evidence["child_failure_taxonomies"]) == ["RETRIEVAL_MISS"]

    blocked_assertion = scenario_success_assertion("scn.outcome", 4, 0, 1, 0)
    assert blocked_assertion.status is AssertionStatus.BLOCKED
    assert blocked_assertion.blocked_by is BlockReason.PREREQUISITE


def test_status_counts_includes_all():
    assertions = [
        assertion("a", AssertionStatus.PASS),
        assertion("b", AssertionStatus.FAIL),
        assertion("c.rt", AssertionStatus.BLOCKED),
        assertion("d", AssertionStatus.NOT_APPLICABLE),
    ]
    counts = status_counts(assertions)
    assert counts == {
        "PASS": 1,
        "FAIL": 1,
        "BLOCKED": 1,
        "NOT_APPLICABLE": 1,
    }


# ------------------------------------------------------------------ E1-R2 rate formulas


def test_runtime_block_rate_exact_formula():
    # E1-v1: 3 runtime blocks over 138 applicable -> 0.9783 (BUG, inverted).
    # E1-R2: rate = numerator / denominator = 3 / 138.
    assertions = []
    for i in range(135):
        assertions.append(assertion(f"pass_{i}", AssertionStatus.PASS))
    for i in range(3):
        assertions.append(assertion(f"block_{i}.rt", AssertionStatus.BLOCKED))
    rate = build_runtime_block_rate(assertions)
    assert rate.numerator == 3
    assert rate.denominator == 138
    assert rate.value == pytest.approx(3 / 138)
    assert rate.value == pytest.approx(0.021739130434782608)


def test_evaluation_infra_failure_rate_exact_formula():
    assertions = []
    for i in range(119):
        assertions.append(assertion(f"pass_{i}", AssertionStatus.PASS))
    for i in range(19):
        assertions.append(assertion(f"ev_{i}", AssertionStatus.BLOCKED))
    rate = build_evaluation_infra_failure_rate(assertions)
    assert rate.numerator == 19
    assert rate.denominator == 138
    assert rate.value == pytest.approx(19 / 138)
    assert rate.value == pytest.approx(0.13768115942028985)


def test_rate_zero_numerator_is_zero():
    rate = build_runtime_block_rate([assertion("a", AssertionStatus.PASS)])
    assert rate.numerator == 0
    assert rate.denominator == 1
    assert rate.value == 0.0


def test_rate_zero_denominator_is_na():
    rate = build_runtime_block_rate([assertion("a", AssertionStatus.NOT_APPLICABLE)])
    assert rate.denominator == 0
    assert rate.value is None
    assert rate.as_dict()["kind"] == "ratio"


# ------------------------------------------------------------------ E1-R2 leakage NA


def test_leakage_na_zero_denominator_is_none():
    aggregate = build_metric_aggregate(
        "forgotten_memory_leakage_rate",
        [assertion("x", AssertionStatus.NOT_APPLICABLE, dimension=AssertionDimension.LEAKAGE)],
    )
    assert aggregate.evaluable_denominator == 0
    assert aggregate.value is None
    assert aggregate.as_dict()["value"] is None


@pytest.mark.parametrize(
    ("passed", "failed", "expected"),
    [
        (2, 0, 0.0),
        (6, 1, 1 / 7),
        (0, 2, 1.0),
        (0, 0, None),
    ],
)
def test_failure_rate_aggregate_uses_failed_over_evaluable(passed, failed, expected):
    assertions = [assertion(f"pass_{index}", AssertionStatus.PASS) for index in range(passed)]
    assertions.extend(assertion(f"fail_{index}", AssertionStatus.FAIL) for index in range(failed))
    aggregate = build_failure_rate_aggregate("forgotten_memory_leakage_rate", assertions)
    assert aggregate.passed == passed
    assert aggregate.failed == failed
    assert aggregate.evaluable_denominator == passed + failed
    assert aggregate.value == (pytest.approx(expected) if expected is not None else None)


# ------------------------------------------------------------------ E1-R3 expected limitation


def _expected_limitation_assertion(assertion_id):
    return MemoryAssertion(
        assertion_id=assertion_id,
        dimension=AssertionDimension.RETRIEVAL,
        status=AssertionStatus.BLOCKED,
        expected=["db"],
        blocked_by=BlockReason.NOT_SUPPORTED_BY_CURRENT_EVIDENCE,
        evidence_gap_classification=EvidenceGapClassification.EXPECTED_EVIDENCE_LIMITATION,
        reason="identity-level evidence not supported by current runtime journal",
        required=True,
    )


def test_infra_rate_excludes_expected_limitation_blocks():
    assertions = [
        assertion("a", AssertionStatus.PASS),
        _expected_limitation_assertion("a.r1.retrieval.recall_at_k"),
        _expected_limitation_assertion("a.r1.retrieval.rejection"),
        assertion("b.ev", AssertionStatus.BLOCKED),
    ]
    rate = build_evaluation_infra_failure_rate(assertions)
    # expected limitation + NOT_SUPPORTED 不计入 numerator；只有 evidence_capture 计
    assert rate.numerator == 1
    assert rate.denominator == 4
    assert rate.value == pytest.approx(0.25)


def test_infra_rate_excludes_plain_not_supported_blocks():
    assertions = [
        assertion("a", AssertionStatus.PASS),
        MemoryAssertion(
            assertion_id="a.r1.retrieval.recall_at_k",
            dimension=AssertionDimension.RETRIEVAL,
            status=AssertionStatus.BLOCKED,
            expected=["db"],
            blocked_by=BlockReason.NOT_SUPPORTED_BY_CURRENT_EVIDENCE,
            required=True,
        ),
        assertion("b.ev", AssertionStatus.BLOCKED),
    ]
    rate = build_evaluation_infra_failure_rate(assertions)
    assert rate.numerator == 1  # 只有 evidence_capture


def test_expected_limitation_blocked_count_aggregate():
    assertions = [
        assertion("a", AssertionStatus.PASS),
        _expected_limitation_assertion("a.recall"),
        _expected_limitation_assertion("a.rejection"),
        assertion("b.ev", AssertionStatus.BLOCKED),
    ]
    assert expected_evidence_limitation_blocked_count(assertions) == 2
    aggregate = build_expected_evidence_limitation_blocked_aggregate(assertions)
    assert aggregate.metric_name == EXPECTED_EVIDENCE_LIMITATION_BLOCKED_COUNT_METRIC
    assert aggregate.blocked == 2
    assert aggregate.evaluable_denominator == 0
    assert aggregate.value is None
    # 序列化进既有 metric_aggregates 体系（不新建第二套 report framework）
    assert aggregate.as_dict()["blocked"] == 2
