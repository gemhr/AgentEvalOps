"""Stage5-Phase7-WP3 HITL Tool Approval evaluator unit tests.

覆盖任务 §30 mandatory matrix：happy approve PASS、reject/cancel/timeout zero
execution PASS、duplicate execution FAIL、execution before approval FAIL、
reject/cancel/timeout then execution FAIL、missing required evidence BLOCKED、
correlation mismatch FAIL，以及 §31/§32 cross-run / cross-approval isolation、
§33 evidence completeness、EvidenceRef roundtrip 与 artifact record。
"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.evaluation.hitl_artifact import build_hitl_evaluation_record
from app.core.evaluation.hitl_evidence import (
    HITL_TOOL_APPROVAL_EVIDENCE_KIND,
    HitlEvidenceContractError,
    HitlEvidenceProvenance,
    HitlRuntimeEventV1,
    HitlToolApprovalEvidenceV1,
    build_hitl_evaluation_input,
    build_hitl_evidence_ref,
    hitl_evidence_from_ref,
)
from app.core.evaluation.hitl_evaluator import (
    ASSERTION_ID_AT_MOST_ONCE,
    ASSERTION_ID_BOUND_TOOL_STARTS,
    ASSERTION_ID_CANCEL_SAFETY,
    ASSERTION_ID_CORRELATION,
    ASSERTION_ID_DECISION_OBSERVED,
    ASSERTION_ID_EXPECTED_EXECUTION,
    ASSERTION_ID_NO_EXECUTION_BEFORE_APPROVAL,
    ASSERTION_ID_REJECT_PREVENTS_EXECUTION,
    ASSERTION_ID_REQUESTED,
    ASSERTION_ID_TIMEOUT_SAFETY,
    HitlScenarioExpectationV1,
    evaluate_hitl_scenario,
)
from app.core.evaluation.references import EvidenceRef
from app.core.evaluation.stateful_assertion import AssertionStatus, BlockReason

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "hitl_tool_approval.v1.json"


def _load_scenarios() -> dict[str, tuple[HitlScenarioExpectationV1, HitlToolApprovalEvidenceV1]]:
    raw = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    scenarios: dict[str, tuple[HitlScenarioExpectationV1, HitlToolApprovalEvidenceV1]] = {}
    for entry in raw["scenarios"]:
        expectation = HitlScenarioExpectationV1.model_validate(entry["expectation"])
        evidence = HitlToolApprovalEvidenceV1.model_validate(entry["evidence"])
        scenarios[entry["scenario_id"]] = (expectation, evidence)
    return scenarios


def _scenario(scenario_id: str) -> tuple[HitlScenarioExpectationV1, HitlToolApprovalEvidenceV1]:
    return _load_scenarios()[scenario_id]


def _evaluate(scenario_id: str):
    expectation, evidence = _scenario(scenario_id)
    return evaluate_hitl_scenario(build_hitl_evaluation_input(evidence), expectation)


def _assertion_result(evaluation, assertion_id: str):
    return next(item for item in evaluation.assertions if item.assertion_id == assertion_id)


# --- §17 provenance marking ---------------------------------------------------


def test_bad_case_fixtures_are_marked_hypothetical():
    scenarios = _load_scenarios()
    raw = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    by_id = {entry["scenario_id"]: entry for entry in raw["scenarios"]}
    for scenario_id, (_, evidence) in scenarios.items():
        if scenario_id.startswith("BAD_CASE_"):
            assert evidence.provenance is HitlEvidenceProvenance.HYPOTHETICAL_BAD_CASE_FIXTURE
            assert by_id[scenario_id]["provenance"] == "HYPOTHETICAL_BAD_CASE_FIXTURE"
            assert "not a real production incident" in by_id[scenario_id]["description"]
        else:
            assert evidence.provenance in (
                HitlEvidenceProvenance.DETERMINISTIC_TEST_EVIDENCE,
                HitlEvidenceProvenance.REAL_LOCALAGENT_EVIDENCE,
            )


# --- §30 positive scenarios ---------------------------------------------------


@pytest.mark.parametrize(
    "scenario_id",
    [
        "HITL_APPROVE_ONCE",
        "HITL_REJECT_ZERO_EXECUTION",
        "HITL_CANCEL_PENDING",
        "HITL_TIMEOUT_PENDING",
        "HITL_DUPLICATE_APPROVE_NO_DUPLICATE_EXECUTION",
        "HITL_MULTI_APPROVAL_ISOLATION",
    ],
)
def test_positive_scenarios_pass(scenario_id: str):
    evaluation = _evaluate(scenario_id)
    assert evaluation.status is AssertionStatus.PASS, evaluation.failure_reasons


def test_approve_once_assertions_detail():
    evaluation = _evaluate("HITL_APPROVE_ONCE")
    assert _assertion_result(evaluation, ASSERTION_ID_REQUESTED).status is AssertionStatus.PASS
    assert _assertion_result(evaluation, ASSERTION_ID_NO_EXECUTION_BEFORE_APPROVAL).status is AssertionStatus.PASS
    assert _assertion_result(evaluation, ASSERTION_ID_AT_MOST_ONCE).status is AssertionStatus.PASS
    assert _assertion_result(evaluation, ASSERTION_ID_EXPECTED_EXECUTION).status is AssertionStatus.PASS


def test_reject_scenario_assertions_detail():
    evaluation = _evaluate("HITL_REJECT_ZERO_EXECUTION")
    assert _assertion_result(evaluation, ASSERTION_ID_REJECT_PREVENTS_EXECUTION).status is AssertionStatus.PASS
    assert _assertion_result(evaluation, ASSERTION_ID_EXPECTED_EXECUTION).status is AssertionStatus.NOT_APPLICABLE
    assert _assertion_result(evaluation, ASSERTION_ID_BOUND_TOOL_STARTS).status is AssertionStatus.PASS


# --- §30 / §28 bad cases ------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario_id", "failing_assertion"),
    [
        ("BAD_CASE_1_EXECUTION_BEFORE_APPROVAL", ASSERTION_ID_NO_EXECUTION_BEFORE_APPROVAL),
        ("BAD_CASE_2_REJECTED_THEN_EXECUTION", ASSERTION_ID_REJECT_PREVENTS_EXECUTION),
        ("BAD_CASE_3_DUPLICATE_EXECUTION", ASSERTION_ID_AT_MOST_ONCE),
        ("BAD_CASE_4_CANCELLED_THEN_EXECUTION", ASSERTION_ID_CANCEL_SAFETY),
        ("BAD_CASE_5_TIMEOUT_THEN_EXECUTION", ASSERTION_ID_TIMEOUT_SAFETY),
        ("BAD_CASE_6_CORRELATION_MISMATCH", ASSERTION_ID_CORRELATION),
    ],
)
def test_bad_cases_fail(scenario_id: str, failing_assertion: str):
    evaluation = _evaluate(scenario_id)
    assert evaluation.status is AssertionStatus.FAIL, evaluation.failure_reasons
    result = _assertion_result(evaluation, failing_assertion)
    assert result.status is AssertionStatus.FAIL
    assert result.failure_taxonomy is not None


def test_bad_case_1_reason_explains_sequence_ordering():
    evaluation = _evaluate("BAD_CASE_1_EXECUTION_BEFORE_APPROVAL")
    reason = _assertion_result(evaluation, ASSERTION_ID_NO_EXECUTION_BEFORE_APPROVAL).reason
    assert "TOOL_STARTED sequence 1" in reason
    assert "TOOL_APPROVAL_DECIDED(APPROVED) sequence 2" in reason


# --- §22 / §33 missing evidence / BLOCKED semantics ----------------------------


def test_missing_approval_evidence_fails_when_trace_complete():
    expectation, _ = _scenario("HITL_APPROVE_ONCE")
    evidence = HitlToolApprovalEvidenceV1(
        schema_version="hitl-tool-approval-evidence.v1",
        evidence_id="hitl-tool-approval://run-hitl-empty",
        run_id="run-hitl-empty",
        provenance=HitlEvidenceProvenance.DETERMINISTIC_TEST_EVIDENCE,
        trace_complete=True,
        terminal_status="SUCCEEDED",
        events=(),
    )
    evaluation = evaluate_hitl_scenario(build_hitl_evaluation_input(evidence), expectation)
    assert evaluation.status is AssertionStatus.FAIL
    assert _assertion_result(evaluation, ASSERTION_ID_REQUESTED).status is AssertionStatus.FAIL


def test_missing_approval_evidence_blocked_when_trace_incomplete():
    expectation, _ = _scenario("HITL_APPROVE_ONCE")
    evidence = HitlToolApprovalEvidenceV1(
        schema_version="hitl-tool-approval-evidence.v1",
        evidence_id="hitl-tool-approval://run-hitl-empty-partial",
        run_id="run-hitl-empty-partial",
        provenance=HitlEvidenceProvenance.DETERMINISTIC_TEST_EVIDENCE,
        trace_complete=False,
        terminal_status=None,
        events=(),
    )
    evaluation = evaluate_hitl_scenario(build_hitl_evaluation_input(evidence), expectation)
    assert evaluation.status is AssertionStatus.BLOCKED
    requested = _assertion_result(evaluation, ASSERTION_ID_REQUESTED)
    assert requested.status is AssertionStatus.BLOCKED
    assert requested.blocked_by is BlockReason.EVIDENCE_CAPTURE


def test_incomplete_trace_blocks_missing_execution_semantics():
    evaluation = _evaluate("HITL_INCOMPLETE_TRACE_PENDING")
    assert evaluation.status is AssertionStatus.BLOCKED
    assert evaluation.failure_reasons
    for item in evaluation.assertions:
        if item.status is AssertionStatus.BLOCKED:
            assert item.blocked_by is BlockReason.EVIDENCE_CAPTURE


def test_reject_zero_execution_blocked_on_incomplete_trace():
    """缺少 evidence 时，“没有观察到 TOOL_STARTED” 绝不能自动变成 zero-execution PASS。"""
    expectation, evidence = _scenario("HITL_REJECT_ZERO_EXECUTION")
    incomplete = evidence.model_copy(update={"trace_complete": False})
    evaluation = evaluate_hitl_scenario(build_hitl_evaluation_input(incomplete), expectation)
    reject = _assertion_result(evaluation, ASSERTION_ID_REJECT_PREVENTS_EXECUTION)
    assert reject.status is AssertionStatus.BLOCKED
    assert evaluation.status is AssertionStatus.BLOCKED


def test_approve_with_missing_execution_never_passes():
    """APPROVE scenario 预期本身是执行；即使 trace 完整，缺失执行也不得判 PASS。"""
    expectation, evidence = _scenario("HITL_APPROVE_ONCE")
    trimmed = evidence.model_copy(
        update={"events": tuple(event for event in evidence.events if event.event_type != "TOOL_STARTED")}
    )
    evaluation = evaluate_hitl_scenario(build_hitl_evaluation_input(trimmed), expectation)
    assert evaluation.status is AssertionStatus.FAIL
    expected = _assertion_result(evaluation, ASSERTION_ID_EXPECTED_EXECUTION)
    assert expected.status is AssertionStatus.FAIL


# --- §31 / §32 isolation -------------------------------------------------------


def test_cross_run_isolation():
    """Run A 的 approval 不能关联 Run B 的 TOOL_STARTED。"""
    expectation_a, evidence_a = _scenario("HITL_INCOMPLETE_TRACE_PENDING")
    run_b_start = HitlRuntimeEventV1(
        sequence=0,
        event_type="TOOL_STARTED",
        run_id="run-hitl-other-run",
        step_id="answer",
        tool_name="complex_workflow_simulator",
        invocation_identity_digest="a" * 64,
    )
    evidence_b = HitlToolApprovalEvidenceV1(
        schema_version="hitl-tool-approval-evidence.v1",
        evidence_id="hitl-tool-approval://run-hitl-other-run",
        run_id="run-hitl-other-run",
        provenance=HitlEvidenceProvenance.DETERMINISTIC_TEST_EVIDENCE,
        trace_complete=True,
        terminal_status="SUCCEEDED",
        events=(run_b_start,),
    )
    input_a = build_hitl_evaluation_input(evidence_a)
    input_b = build_hitl_evaluation_input(evidence_b)
    assert all(event.run_id == "run-hitl-incomplete" for event in evidence_a.events)
    assert input_a.run_id != input_b.run_id
    assert not input_b.lifecycles
    assert len(input_b.unbound_tool_started) == 1
    # Run B 的执行事件与 Run A 的 lifecycle 无关：跨 envelope 不合并。
    assert input_a.lifecycles[0].tool_started == []
    evaluation_a = evaluate_hitl_scenario(input_a, expectation_a)
    assert evaluation_a.status is AssertionStatus.BLOCKED

    # 把 Run B 的事件混入 Run A 的 envelope 必须被 identity 校验拒绝。
    with pytest.raises(ValidationError):
        HitlToolApprovalEvidenceV1(
            schema_version="hitl-tool-approval-evidence.v1",
            evidence_id="hitl-tool-approval://run-hitl-incomplete",
            run_id="run-hitl-incomplete",
            provenance=HitlEvidenceProvenance.DETERMINISTIC_TEST_EVIDENCE,
            trace_complete=False,
            terminal_status=None,
            events=(*evidence_a.events, run_b_start),
        )


def test_cross_approval_isolation():
    """同一 Run 内多个 approval：每个 lifecycle 只吸收自己的 tool event。"""
    _, evidence = _scenario("HITL_MULTI_APPROVAL_ISOLATION")
    input_value = build_hitl_evaluation_input(evidence)
    assert len(input_value.lifecycles) == 2
    first, second = input_value.lifecycles
    assert first.approval_id != second.approval_id
    assert first.invocation_identity_digest != second.invocation_identity_digest
    assert [event.sequence for event in first.tool_started] == [2]
    assert [event.sequence for event in second.tool_started] == [6]
    assert first.invocation_binding_digest != second.invocation_binding_digest


# --- fail-closed evidence validation -------------------------------------------


def test_event_missing_correlation_fields_is_rejected():
    with pytest.raises(ValidationError):
        HitlRuntimeEventV1(
            sequence=0,
            event_type="TOOL_APPROVAL_REQUESTED",
            run_id="run-hitl-malformed",
            approval_id="approval-malformed-1",
            tool_name="complex_workflow_simulator",
        )


def test_event_extra_field_is_rejected():
    with pytest.raises(ValidationError):
        HitlRuntimeEventV1(
            sequence=0,
            event_type="TOOL_STARTED",
            run_id="run-hitl-malformed",
            tool_name="complex_workflow_simulator",
            invocation_identity_digest="a" * 64,
            invocation_id="raw-invocation-identity",  # type: ignore[arg-type]
        )


def test_evidence_sequence_ordering_is_enforced():
    events = [
        HitlRuntimeEventV1(
            sequence=1,
            event_type="TOOL_STARTED",
            run_id="run-hitl-order",
            tool_name="complex_workflow_simulator",
            invocation_identity_digest="a" * 64,
        ),
        HitlRuntimeEventV1(
            sequence=1,
            event_type="TOOL_COMPLETED",
            run_id="run-hitl-order",
            tool_name="complex_workflow_simulator",
            invocation_identity_digest="a" * 64,
        ),
    ]
    with pytest.raises(ValidationError):
        HitlToolApprovalEvidenceV1(
            schema_version="hitl-tool-approval-evidence.v1",
            evidence_id="hitl-tool-approval://run-hitl-order",
            run_id="run-hitl-order",
            provenance=HitlEvidenceProvenance.DETERMINISTIC_TEST_EVIDENCE,
            trace_complete=True,
            terminal_status=None,
            events=(events[0], events[1]),
        )


def test_evidence_id_run_identity_is_enforced():
    with pytest.raises(ValidationError):
        HitlToolApprovalEvidenceV1(
            schema_version="hitl-tool-approval-evidence.v1",
            evidence_id="hitl-tool-approval://run-hitl-other",
            run_id="run-hitl-order",
            provenance=HitlEvidenceProvenance.DETERMINISTIC_TEST_EVIDENCE,
            trace_complete=True,
            terminal_status=None,
            events=(),
        )


def test_evidence_ref_roundtrip():
    _, evidence = _scenario("HITL_APPROVE_ONCE")
    ref = build_hitl_evidence_ref(evidence)
    assert ref.kind == HITL_TOOL_APPROVAL_EVIDENCE_KIND
    restored = hitl_evidence_from_ref(ref, expected_run_id=evidence.run_id)
    assert restored == evidence
    with pytest.raises(HitlEvidenceContractError):
        hitl_evidence_from_ref(ref, expected_run_id="run-hitl-mismatch")
    foreign = EvidenceRef(kind="other_kind", identifier="x", schema_version="v1")
    with pytest.raises(HitlEvidenceContractError):
        hitl_evidence_from_ref(foreign)


def test_evidence_ref_with_conflicting_payload_is_rejected():
    _, evidence = _scenario("HITL_APPROVE_ONCE")
    ref = build_hitl_evidence_ref(evidence)
    tampered = EvidenceRef(
        kind=ref.kind,
        identifier=ref.identifier,
        media_type=ref.media_type,
        schema_version=ref.schema_version,
        metadata={"payload": {**ref.metadata["payload"], "trace_complete": not evidence.trace_complete}},
    )
    restored = hitl_evidence_from_ref(tampered, expected_run_id=evidence.run_id)
    assert restored.trace_complete is not evidence.trace_complete
    broken = EvidenceRef(
        kind=ref.kind,
        identifier=ref.identifier,
        media_type=ref.media_type,
        schema_version=ref.schema_version,
        metadata={"payload": {"schema_version": "unknown"}},
    )
    with pytest.raises(HitlEvidenceContractError):
        hitl_evidence_from_ref(broken)


# --- artifact record ------------------------------------------------------------


def test_evaluation_record_for_pass_scenario():
    expectation, evidence = _scenario("HITL_APPROVE_ONCE")
    input_value = build_hitl_evaluation_input(evidence)
    evaluation = evaluate_hitl_scenario(input_value, expectation)
    record = build_hitl_evaluation_record(evaluation, input_value)
    payload = record.model_dump(mode="json")
    assert record.aggregate_status is AssertionStatus.PASS
    assert record.scenario_id == "HITL_APPROVE_ONCE"
    assert record.evidence_provenance is HitlEvidenceProvenance.DETERMINISTIC_TEST_EVIDENCE
    assert record.trace_complete is True
    assert record.failure_reasons == []
    assert len(record.lifecycles) == 1
    lifecycle = record.lifecycles[0]
    assert lifecycle.decision_status == "APPROVED"
    assert lifecycle.tool_started_sequences == [2]
    assert payload["schema_version"] == "hitl-tool-approval-evaluation.v1"


def test_evaluation_record_exposes_failure_evidence():
    expectation, evidence = _scenario("BAD_CASE_1_EXECUTION_BEFORE_APPROVAL")
    input_value = build_hitl_evaluation_input(evidence)
    evaluation = evaluate_hitl_scenario(input_value, expectation)
    record = build_hitl_evaluation_record(evaluation, input_value)
    assert record.aggregate_status is AssertionStatus.FAIL
    assert any("TOOL_STARTED sequence 1" in reason for reason in record.failure_reasons)
    assert any(
        item["status"] == "FAIL" and item["assertion_id"] == ASSERTION_ID_NO_EXECUTION_BEFORE_APPROVAL
        for item in record.assertions
    )


def test_evaluation_record_reports_unbound_and_ambiguous_correlation():
    _, evidence = _scenario("BAD_CASE_6_CORRELATION_MISMATCH")
    input_value = build_hitl_evaluation_input(evidence)
    expectation = HitlScenarioExpectationV1(
        scenario_id="BAD_CASE_6_CORRELATION_MISMATCH",
        expected_decision="APPROVED",
        expect_tool_execution=True,
    )
    record = build_hitl_evaluation_record(evaluate_hitl_scenario(input_value, expectation), input_value)
    assert record.ambiguous_identity_digests == ["a" * 64]
    evaluation = evaluate_hitl_scenario(input_value, expectation)
    assert _assertion_result(evaluation, ASSERTION_ID_DECISION_OBSERVED).status is AssertionStatus.PASS
