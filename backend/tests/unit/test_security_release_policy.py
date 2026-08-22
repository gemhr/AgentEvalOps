"""WP7 Security Release Policy —— 纯确定性 Release Gate 单元测试。

覆盖任务书 §49–§72：absolute / comparative gate、INCONCLUSIVE / NOT_EVALUATED /
NOT_MAPPED / coverage debt、comparability warnings、improvement 不抵消、
determinism / fresh reload、零重跑、CI adapter 复用、policy version。
"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

import random
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.core.evaluation import (
    SECURITY_EVALUATOR_ID,
    SECURITY_RELEASE_POLICY_REF,
    ArtifactRef,
    EvaluationResult,
    EvaluationVerdict,
    ProvenanceCompleteness,
    SecurityCaseFacts,
    SecurityCaseStatus,
    SecurityRunSummary,
    SecurityTransitionClass,
    VersionRef,
    build_security_run_summary,
)
from app.core.evaluation.comparison import ComparisonReason
from app.core.evaluation.security_projection import (
    SecurityComparisonProjection,
    SecuritySlotProjection,
    UnmappedSecurityCaseRef,
)
from app.core.evaluation.security_release import (
    SecurityBlockingReason,
    SecurityReleaseAssessment,
    SecurityReleasePolicyError,
    evaluate_security_release,
)
from scripts.ci.release_gate import EXIT_GATE_FAIL, EXIT_PASS, exit_code_for_decision

NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
RUN_ID = UUID("22000000-0000-4000-a000-000000000004")
BASELINE_RUN_ID = UUID("22000000-0000-4000-a000-000000000005")
CONFIG_REF = VersionRef("judge_config", "v1")

_SCORE_BY_VERDICT = {
    EvaluationVerdict.PASS: 1.0,
    EvaluationVerdict.FAIL: 0.0,
    EvaluationVerdict.INCONCLUSIVE: None,
}


def fact(
    case_id: str,
    *,
    kind: str = "ATTACK",
    severity: str = "HIGH",
    attack_type: str = "DIRECT_INSTRUCTION_OVERRIDE",
    source: str = "USER_INPUT",
) -> SecurityCaseFacts:
    return SecurityCaseFacts(
        case_id=case_id,
        case_version="v2",
        case_kind=kind,
        attack_type=attack_type if kind == "ATTACK" else None,
        attack_source=source if kind == "ATTACK" else None,
        severity=severity if kind == "ATTACK" else None,
    )


def make_result(
    run_id: UUID,
    case_id: str,
    verdict: EvaluationVerdict,
    *,
    case_kind: str = "ATTACK",
    severity: str = "HIGH",
) -> EvaluationResult:
    return EvaluationResult(
        result_id=str(uuid4()),
        run_id=str(run_id),
        attempt_id=str(uuid4()),
        dataset_id="prompt-injection-regression-v2",
        dataset_version="d1",
        case_id=case_id,
        case_version="v2",
        suite_id="sec-suite",
        suite_version="s1",
        evaluator_id=SECURITY_EVALUATOR_ID,
        evaluator_version="v1",
        config_ref=CONFIG_REF,
        execution_target_id="target-x",
        execution_request_id=str(uuid4()),
        verdict=verdict,
        reason="summary",
        provenance_completeness=ProvenanceCompleteness.COMPLETE,
        target_version_ref=VersionRef("fixture", "v1"),
        output_artifact_ref=ArtifactRef(f"localagent-run://{uuid4()}"),
        score=_SCORE_BY_VERDICT.get(verdict),
        created_at=NOW,
        metadata={
            "evaluator": {
                "source_status": "SECURITY_EVALUATED",
                "security": {
                    "case_kind": case_kind,
                    "attack_type": None,
                    "attack_source": None,
                    "severity": severity,
                    "expected_behaviors": [],
                    "behavior_findings": [],
                },
            }
        },
    )


def build_summary(
    results: list[EvaluationResult],
    *,
    facts: list[SecurityCaseFacts] | None = None,
    unmapped: tuple[UnmappedSecurityCaseRef, ...] = (),
    run_id: UUID = RUN_ID,
) -> SecurityRunSummary:
    return build_security_run_summary(
        run_id=run_id,
        dataset_id="prompt-injection-regression-v2",
        dataset_version="v2",
        suite_id="sec-suite",
        suite_version="s1",
        execution_target_id="target-x",
        execution_target_kind="LOCALAGENT_HTTP",
        facts=facts if facts is not None else [fact("c1")],
        results=results,
        unmapped=list(unmapped),
    )


def slot(
    case_id: str,
    classification: SecurityTransitionClass,
    *,
    detail: str = "",
    warnings: tuple[str, ...] = (),
) -> SecuritySlotProjection:
    return SecuritySlotProjection(
        case_id=case_id,
        case_version="v2",
        evaluator_id=SECURITY_EVALUATOR_ID,
        evaluator_version="v1",
        classification=classification,
        detail=detail,
        warnings=warnings,
    )


def projection(
    *,
    regressions: tuple[SecuritySlotProjection, ...] = (),
    benign: tuple[SecuritySlotProjection, ...] = (),
    certainty: tuple[SecuritySlotProjection, ...] = (),
    newly: tuple[SecuritySlotProjection, ...] = (),
    improvements: tuple[SecuritySlotProjection, ...] = (),
    eval_improvements: tuple[SecuritySlotProjection, ...] = (),
    warnings: tuple[tuple[str, tuple[str, ...]], ...] = (),
    dataset_version_changed: bool = False,
) -> SecurityComparisonProjection:
    slots = (
        *regressions,
        *benign,
        *certainty,
        *newly,
        *improvements,
        *eval_improvements,
    )
    return SecurityComparisonProjection(
        project_id=UUID("20000000-0000-4000-a000-000000000002"),
        baseline_run_id=BASELINE_RUN_ID,
        candidate_run_id=RUN_ID,
        dataset_version_changed=dataset_version_changed,
        suite_version_changed=False,
        slots=slots,
        security_regressions=regressions,
        benign_regressions=benign,
        security_improvements=improvements,
        certainty_regressions=certainty,
        newly_identified_failures=newly,
        evaluation_improvements=eval_improvements,
        unchanged_count=0,
        not_comparable_count=0,
        comparability_warnings=warnings,
    )


def reasons_map(assessment: SecurityReleaseAssessment) -> dict[str, tuple[str, ...]]:
    return {reason: cases for reason, cases in assessment.blocking_reasons}


# --------------------------------------------------------------------------- #
# Absolute Candidate Gate                                                     #
# --------------------------------------------------------------------------- #


def test_absolute_attack_fail_blocks() -> None:
    facts = [fact("sec-attack", severity="HIGH")]
    results = [make_result(RUN_ID, "sec-attack", EvaluationVerdict.FAIL, severity="HIGH")]
    assessment = evaluate_security_release(candidate_summary=build_summary(results, facts=facts))
    assert assessment.decision.value == "FAIL"
    assert assessment.blocking_case_ids == ("sec-attack",)
    rm = reasons_map(assessment)
    assert rm[SecurityBlockingReason.SECURITY_BEHAVIOR_FAILURE.value] == ("sec-attack",)


def test_absolute_critical_failure_blocks() -> None:
    facts = [fact("sec-critical", severity="CRITICAL")]
    results = [make_result(RUN_ID, "sec-critical", EvaluationVerdict.FAIL, severity="CRITICAL")]
    summary = build_summary(results, facts=facts)
    assessment = evaluate_security_release(candidate_summary=summary)
    assert assessment.decision.value == "FAIL"
    assert assessment.critical_case_ids == ("sec-critical",)
    rm = reasons_map(assessment)
    assert rm[SecurityBlockingReason.SECURITY_BEHAVIOR_FAILURE.value] == ("sec-critical",)


def test_absolute_benign_failure_blocks() -> None:
    facts = [fact("sec-attack-pass"), fact("sec-benign-fail", kind="BENIGN_CONTROL")]
    results = [
        make_result(RUN_ID, "sec-attack-pass", EvaluationVerdict.PASS),
        make_result(RUN_ID, "sec-benign-fail", EvaluationVerdict.FAIL, case_kind="BENIGN_CONTROL"),
    ]
    assessment = evaluate_security_release(candidate_summary=build_summary(results, facts=facts))
    assert assessment.decision.value == "FAIL"
    assert "sec-benign-fail" in assessment.blocking_case_ids
    rm = reasons_map(assessment)
    assert rm[SecurityBlockingReason.SECURITY_BEHAVIOR_FAILURE.value] == ("sec-benign-fail",)


def test_low_and_medium_fail_block() -> None:
    for severity in ("LOW", "MEDIUM"):
        facts = [fact("sec-x", severity=severity)]
        results = [make_result(RUN_ID, "sec-x", EvaluationVerdict.FAIL, severity=severity)]
        assessment = evaluate_security_release(candidate_summary=build_summary(results, facts=facts))
        assert assessment.decision.value == "FAIL", severity


def test_absolute_inconclusive_blocks_with_evidence_insufficient() -> None:
    facts = [fact("sec-inc", severity="CRITICAL")]
    results = [make_result(RUN_ID, "sec-inc", EvaluationVerdict.INCONCLUSIVE, severity="CRITICAL")]
    assessment = evaluate_security_release(candidate_summary=build_summary(results, facts=facts))
    assert assessment.decision.value == "FAIL"
    rm = reasons_map(assessment)
    assert SecurityBlockingReason.SECURITY_EVALUATION_INCONCLUSIVE.value in rm
    assert SecurityBlockingReason.SECURITY_BEHAVIOR_FAILURE.value not in rm
    assert assessment.critical_case_ids == ("sec-inc",)


def test_high_inconclusive_blocks() -> None:
    facts = [fact("sec-inc", severity="HIGH")]
    results = [make_result(RUN_ID, "sec-inc", EvaluationVerdict.INCONCLUSIVE, severity="HIGH")]
    assessment = evaluate_security_release(candidate_summary=build_summary(results, facts=facts))
    assert assessment.decision.value == "FAIL"


def test_not_evaluated_blocks() -> None:
    facts = [fact("sec-ne")]
    assessment = evaluate_security_release(candidate_summary=build_summary([], facts=facts))
    assert assessment.decision.value == "FAIL"
    rm = reasons_map(assessment)
    assert SecurityBlockingReason.SECURITY_NOT_EVALUATED.value in rm
    assert SecurityBlockingReason.SECURITY_BEHAVIOR_FAILURE.value not in rm


def test_single_run_all_pass_allows_with_comparison_unavailable_warning() -> None:
    facts = [fact("sec-pass"), fact("sec-pass2")]
    results = [
        make_result(RUN_ID, "sec-pass", EvaluationVerdict.PASS),
        make_result(RUN_ID, "sec-pass2", EvaluationVerdict.PASS),
    ]
    assessment = evaluate_security_release(candidate_summary=build_summary(results, facts=facts))
    assert assessment.decision.value == "PASS"
    assert assessment.comparability_warnings == (("comparison_unavailable", ("sec-pass", "sec-pass2")),)


# --------------------------------------------------------------------------- #
# NOT_MAPPED / coverage debt                                                  #
# --------------------------------------------------------------------------- #


def test_known_not_mapped_allowed_when_frozen_in_baseline() -> None:
    unmapped = (
        UnmappedSecurityCaseRef("sec-gap-1", "v2", "tool_output_boundary_unsupported"),
        UnmappedSecurityCaseRef("sec-gap-2", "v2", "agent_message_boundary_unsupported"),
    )
    facts = [
        fact("sec-pass"),
        fact("sec-pass2"),
        fact("sec-gap-1", source="TOOL_OUTPUT"),
        fact("sec-gap-2", source="AGENT_MESSAGE"),
    ]
    results = [
        make_result(RUN_ID, "sec-pass", EvaluationVerdict.PASS),
        make_result(RUN_ID, "sec-pass2", EvaluationVerdict.PASS),
    ]
    baseline = build_summary(results, facts=facts, unmapped=unmapped)
    candidate = build_summary(results, facts=facts, unmapped=unmapped)
    assessment = evaluate_security_release(candidate_summary=candidate, baseline_summary=baseline)
    assert assessment.decision.value == "PASS"
    gap_map = {gap.category: gap.cases for gap in assessment.known_contract_gaps}
    assert gap_map == {
        "tool_output_boundary_unsupported": ("sec-gap-1",),
        "agent_message_boundary_unsupported": ("sec-gap-2",),
    }
    assert assessment.blocking_case_ids == ()


def test_new_not_mapped_blocks() -> None:
    baseline_facts = [fact("sec-a"), fact("sec-b")]
    baseline_results = [
        make_result(BASELINE_RUN_ID, "sec-a", EvaluationVerdict.PASS),
        make_result(BASELINE_RUN_ID, "sec-b", EvaluationVerdict.PASS),
    ]
    baseline = build_summary(baseline_results, facts=baseline_facts)
    candidate_facts = [fact("sec-a"), fact("sec-b")]
    candidate_results = [make_result(RUN_ID, "sec-a", EvaluationVerdict.PASS)]
    candidate = build_summary(
        candidate_results,
        facts=candidate_facts,
        unmapped=(UnmappedSecurityCaseRef("sec-b", "v2", "tool_output_boundary_unsupported"),),
    )
    assessment = evaluate_security_release(candidate_summary=candidate, baseline_summary=baseline)
    assert assessment.decision.value == "FAIL"
    rm = reasons_map(assessment)
    assert rm[SecurityBlockingReason.SECURITY_NEW_CONTRACT_GAP.value] == ("sec-b",)


def test_new_not_mapped_blocks_when_no_baseline() -> None:
    facts = [fact("sec-b")]
    candidate = build_summary(
        [],
        facts=facts,
        unmapped=(UnmappedSecurityCaseRef("sec-b", "v2", "tool_output_boundary_unsupported"),),
    )
    assessment = evaluate_security_release(candidate_summary=candidate)
    assert assessment.decision.value == "FAIL"
    rm = reasons_map(assessment)
    assert rm[SecurityBlockingReason.SECURITY_NEW_CONTRACT_GAP.value] == ("sec-b",)


def test_coverage_improvement_recognized() -> None:
    gap_facts = [fact("sec-gap", source="TOOL_OUTPUT")]
    baseline = build_summary(
        [],
        facts=gap_facts,
        unmapped=(UnmappedSecurityCaseRef("sec-gap", "v2", "tool_output_boundary_unsupported"),),
    )
    mapped_facts = [fact("sec-gap", source="TOOL_OUTPUT"), fact("sec-pass")]
    results = [
        make_result(RUN_ID, "sec-gap", EvaluationVerdict.PASS),
        make_result(RUN_ID, "sec-pass", EvaluationVerdict.PASS),
    ]
    candidate = build_summary(results, facts=mapped_facts)
    assessment = evaluate_security_release(candidate_summary=candidate, baseline_summary=baseline)
    assert assessment.decision.value == "PASS"
    assert assessment.coverage_improvement_case_ids == ("sec-gap",)
    assert assessment.known_contract_gaps == ()


# --------------------------------------------------------------------------- #
# Comparative Regression Gate                                                 #
# --------------------------------------------------------------------------- #


def test_security_regression_blocks() -> None:
    facts = [fact("sec-attack", severity="HIGH")]
    results = [make_result(RUN_ID, "sec-attack", EvaluationVerdict.FAIL)]
    summary = build_summary(results, facts=facts)
    comp = projection(regressions=(slot("sec-attack", SecurityTransitionClass.SECURITY_REGRESSION),))
    assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
    assert assessment.decision.value == "FAIL"
    rm = reasons_map(assessment)
    assert rm[SecurityBlockingReason.SECURITY_BEHAVIOR_FAILURE.value] == ("sec-attack",)


def test_over_refusal_regression_blocks() -> None:
    facts = [fact("sec-benign", kind="BENIGN_CONTROL")]
    results = [make_result(RUN_ID, "sec-benign", EvaluationVerdict.FAIL, case_kind="BENIGN_CONTROL")]
    summary = build_summary(results, facts=facts)
    comp = projection(benign=(slot("sec-benign", SecurityTransitionClass.OVER_REFUSAL_REGRESSION),))
    assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
    assert assessment.decision.value == "FAIL"
    rm = reasons_map(assessment)
    assert rm[SecurityBlockingReason.SECURITY_OVER_REFUSAL_REGRESSION.value] == ("sec-benign",)


def test_certainty_regression_blocks() -> None:
    facts = [fact("sec-attack", severity="HIGH")]
    results = [make_result(RUN_ID, "sec-attack", EvaluationVerdict.INCONCLUSIVE)]
    summary = build_summary(results, facts=facts)
    comp = projection(certainty=(slot("sec-attack", SecurityTransitionClass.CERTAINTY_REGRESSION),))
    assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
    assert assessment.decision.value == "FAIL"
    rm = reasons_map(assessment)
    assert rm[SecurityBlockingReason.SECURITY_CERTAINTY_REGRESSION.value] == ("sec-attack",)


def test_newly_identified_failure_blocks_with_specific_reason() -> None:
    facts = [fact("sec-attack", severity="HIGH")]
    results = [make_result(RUN_ID, "sec-attack", EvaluationVerdict.FAIL)]
    summary = build_summary(results, facts=facts)
    comp = projection(newly=(slot("sec-attack", SecurityTransitionClass.NEWLY_IDENTIFIED_FAILURE),))
    assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
    assert assessment.decision.value == "FAIL"
    rm = reasons_map(assessment)
    assert rm[SecurityBlockingReason.SECURITY_NEWLY_IDENTIFIED_FAILURE.value] == ("sec-attack",)
    assert SecurityBlockingReason.SECURITY_BEHAVIOR_FAILURE.value in rm


def test_security_improvement_allows_when_no_blocker() -> None:
    facts = [fact("sec-attack", severity="HIGH"), fact("sec-attack2", severity="HIGH")]
    results = [
        make_result(RUN_ID, "sec-attack", EvaluationVerdict.PASS),
        make_result(RUN_ID, "sec-attack2", EvaluationVerdict.PASS),
    ]
    summary = build_summary(results, facts=facts)
    comp = projection(improvements=(slot("sec-attack", SecurityTransitionClass.SECURITY_IMPROVEMENT),))
    assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
    assert assessment.decision.value == "PASS"
    assert assessment.improvement_case_ids == ("sec-attack",)


def test_evaluation_improvement_recognized() -> None:
    facts = [fact("sec-attack", severity="HIGH"), fact("sec-attack2", severity="HIGH")]
    results = [
        make_result(RUN_ID, "sec-attack", EvaluationVerdict.PASS),
        make_result(RUN_ID, "sec-attack2", EvaluationVerdict.PASS),
    ]
    summary = build_summary(results, facts=facts)
    comp = projection(eval_improvements=(slot("sec-attack2", SecurityTransitionClass.EVALUATION_IMPROVEMENT),))
    assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
    assert assessment.decision.value == "PASS"
    assert assessment.improvement_case_ids == ("sec-attack2",)


def test_improvement_does_not_offset_failure() -> None:
    facts = [
        fact("sec-attack", severity="HIGH"),
        fact("sec-attack2", severity="HIGH"),
        fact("sec-attack3", severity="HIGH"),
        fact("sec-attack4", severity="HIGH"),
    ]
    results = [
        make_result(RUN_ID, "sec-attack", EvaluationVerdict.PASS),
        make_result(RUN_ID, "sec-attack2", EvaluationVerdict.PASS),
        make_result(RUN_ID, "sec-attack3", EvaluationVerdict.PASS),
        make_result(RUN_ID, "sec-attack4", EvaluationVerdict.FAIL),
    ]
    summary = build_summary(results, facts=facts)
    comp = projection(
        regressions=(slot("sec-attack4", SecurityTransitionClass.SECURITY_REGRESSION),),
        improvements=(
            slot("sec-attack", SecurityTransitionClass.SECURITY_IMPROVEMENT),
            slot("sec-attack2", SecurityTransitionClass.SECURITY_IMPROVEMENT),
            slot("sec-attack3", SecurityTransitionClass.SECURITY_IMPROVEMENT),
        ),
    )
    assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
    assert assessment.decision.value == "FAIL"
    assert assessment.blocking_case_ids == ("sec-attack4",)
    assert assessment.improvement_case_ids == ("sec-attack", "sec-attack2", "sec-attack3")


# --------------------------------------------------------------------------- #
# Comparability warnings                                                      #
# --------------------------------------------------------------------------- #


def test_prompt_version_mismatch_allows_with_warning() -> None:
    facts = [fact("sec-attack"), fact("sec-attack2")]
    results = [
        make_result(RUN_ID, "sec-attack", EvaluationVerdict.PASS),
        make_result(RUN_ID, "sec-attack2", EvaluationVerdict.PASS),
    ]
    summary = build_summary(results, facts=facts)
    comp = projection(warnings=(("judge_prompt_changed", ("sec-attack", "sec-attack2")),))
    assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
    assert assessment.decision.value == "PASS"
    assert ("judge_prompt_changed", ("sec-attack", "sec-attack2")) in assessment.comparability_warnings


def test_dataset_version_mismatch_allows_with_warning() -> None:
    facts = [fact("sec-attack"), fact("sec-attack2")]
    results = [
        make_result(RUN_ID, "sec-attack", EvaluationVerdict.PASS),
        make_result(RUN_ID, "sec-attack2", EvaluationVerdict.PASS),
    ]
    summary = build_summary(results, facts=facts)
    comp = projection(
        warnings=(("dataset_version_changed", ("sec-attack", "sec-attack2")),),
        dataset_version_changed=True,
    )
    assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
    assert assessment.decision.value == "PASS"
    assert ("dataset_version_changed", ("sec-attack", "sec-attack2")) in assessment.comparability_warnings


def test_judge_model_changed_warning_preserved() -> None:
    facts = [fact("sec-attack")]
    results = [make_result(RUN_ID, "sec-attack", EvaluationVerdict.PASS)]
    summary = build_summary(results, facts=facts)
    comp = projection(warnings=(("judge_model_changed", ("sec-attack",)),))
    assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
    assert assessment.decision.value == "PASS"
    assert ("judge_model_changed", ("sec-attack",)) in assessment.comparability_warnings


def test_not_comparable_result_is_not_security_fail() -> None:
    facts = [fact("sec-attack")]
    results = [make_result(RUN_ID, "sec-attack", EvaluationVerdict.PASS)]
    summary = build_summary(results, facts=facts)
    comp = projection(warnings=(("judge_prompt_changed", ("sec-attack",)),))
    assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
    assert assessment.decision.value == "PASS"


# --------------------------------------------------------------------------- #
# Determinism / fresh reload / zero re-evaluation                             #
# --------------------------------------------------------------------------- #


def test_stable_decision_under_input_order_shuffle() -> None:
    facts = [fact("sec-attack"), fact("sec-benign", kind="BENIGN_CONTROL"), fact("sec-pass")]
    results = [
        make_result(RUN_ID, "sec-attack", EvaluationVerdict.FAIL),
        make_result(RUN_ID, "sec-benign", EvaluationVerdict.PASS, case_kind="BENIGN_CONTROL"),
        make_result(RUN_ID, "sec-pass", EvaluationVerdict.PASS),
    ]
    rng = random.Random(7)
    first = None
    for _ in range(5):
        shuffled = list(results)
        rng.shuffle(shuffled)
        summary = build_summary(shuffled, facts=facts)
        comp = projection(regressions=(slot("sec-attack", SecurityTransitionClass.SECURITY_REGRESSION),))
        assessment = evaluate_security_release(candidate_summary=summary, comparison_projection=comp)
        if first is None:
            first = assessment
        else:
            assert assessment == first


def test_fresh_reload_identical_decision() -> None:
    facts = [fact("sec-attack"), fact("sec-pass")]
    results = [
        make_result(RUN_ID, "sec-attack", EvaluationVerdict.FAIL),
        make_result(RUN_ID, "sec-pass", EvaluationVerdict.PASS),
    ]
    baseline_facts = [fact("sec-attack"), fact("sec-pass")]
    baseline_results = [
        make_result(BASELINE_RUN_ID, "sec-attack", EvaluationVerdict.PASS),
        make_result(BASELINE_RUN_ID, "sec-pass", EvaluationVerdict.PASS),
    ]
    baseline = build_summary(baseline_results, facts=baseline_facts, run_id=BASELINE_RUN_ID)
    candidate = build_summary(results, facts=facts)
    comp = projection(
        regressions=(slot("sec-attack", SecurityTransitionClass.SECURITY_REGRESSION),),
        improvements=(slot("sec-pass", SecurityTransitionClass.SECURITY_IMPROVEMENT),),
    )
    first = evaluate_security_release(candidate_summary=candidate, comparison_projection=comp, baseline_summary=baseline)
    reloaded = evaluate_security_release(candidate_summary=candidate, comparison_projection=comp, baseline_summary=baseline)
    assert reloaded == first
    assert reloaded.decision.value == "FAIL"


def test_zero_reevaluation_no_judge_agent_calls() -> None:
    facts = [fact("sec-attack")]
    results = [make_result(RUN_ID, "sec-attack", EvaluationVerdict.PASS)]
    candidate = build_summary(results, facts=facts)
    assessment = evaluate_security_release(candidate_summary=candidate)
    assert assessment.decision.value == "PASS"
    assert not assessment.blocking_case_ids


# --------------------------------------------------------------------------- #
# CI adapter reuse / policy version / contract errors                         #
# --------------------------------------------------------------------------- #


def test_ci_exit_adapter_reuse_allow() -> None:
    facts = [fact("sec-attack")]
    results = [make_result(RUN_ID, "sec-attack", EvaluationVerdict.PASS)]
    candidate = build_summary(results, facts=facts)
    assessment = evaluate_security_release(candidate_summary=candidate)
    assert assessment.decision.value == "PASS"
    assert exit_code_for_decision(assessment.decision) == EXIT_PASS


def test_ci_exit_adapter_reuse_block() -> None:
    facts = [fact("sec-attack")]
    results = [make_result(RUN_ID, "sec-attack", EvaluationVerdict.FAIL)]
    candidate = build_summary(results, facts=facts)
    assessment = evaluate_security_release(candidate_summary=candidate)
    assert assessment.decision.value == "FAIL"
    assert exit_code_for_decision(assessment.decision) == EXIT_GATE_FAIL


def test_policy_ref_is_frozen_v1_identity() -> None:
    facts = [fact("sec-pass")]
    results = [make_result(RUN_ID, "sec-pass", EvaluationVerdict.PASS)]
    candidate = build_summary(results, facts=facts)
    assessment = evaluate_security_release(candidate_summary=candidate)
    assert assessment.policy_ref == SECURITY_RELEASE_POLICY_REF == "security-release-policy.v1"


def test_unsupported_policy_ref_fails_closed() -> None:
    with pytest.raises(SecurityReleasePolicyError, match="unsupported security release policy ref"):
        evaluate_security_release(
            candidate_summary=build_summary([], facts=[fact("sec-pass")]),
            policy_ref="security-release-policy.v2",
        )


def test_wrong_input_types_fail_closed() -> None:
    with pytest.raises(SecurityReleasePolicyError):
        evaluate_security_release(candidate_summary=None)  # type: ignore[arg-type]