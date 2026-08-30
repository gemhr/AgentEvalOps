"""R3-B layered identity evidence policy：Layer1 REQUIRED PASS / Layer2 expected limitation / unexpected gap。"""

# ruff: noqa: D101, D105, D415

from datetime import UTC, datetime

import pytest

from app.core.evaluation.stateful_assertion import (
    AssertionDimension,
    AssertionStatus,
    BlockReason,
    EvaluationLayer,
    EvidenceGapClassification,
    FailureTaxonomy,
)
from app.core.evaluation.stateful_evaluators import (
    RetrievalSelectionEvidence,
    ScenarioEvaluationEvidence,
    evaluate_retrieval,
    evaluate_scenario,
)
from app.core.evaluation.stateful_gate import evaluate_hard_gate
from app.core.evaluation.stateful_memory_dataset_v2 import (
    IdentityEvidenceByLayer,
    IdentityEvidenceRequirement,
    load_stateful_memory_dataset_v2,
)
from app.core.evaluation.stateful_projection import (
    CanonicalMemoryRecord,
    MemoryStateSnapshot,
)

V2_DATASET_PATH = "evaluation_assets/stateful_memory_v2/stateful_memory_dataset.v2.json"
DATASET = load_stateful_memory_dataset_v2(V2_DATASET_PATH)
NOW = datetime(2026, 8, 29, tzinfo=UTC)

V2_POLICY_SCENARIOS = {
    "retrieval_active_hit": {"r1"},
    "retrieval_status_exclusion": {"r1"},
    "retrieval_open_hit": {"r1"},
    "retrieval_unrelated_rejection": {"r1"},
    "scope_isolation": {"r1"},
    "safety_forgotten_not_injected": {"r1"},
    "database_correction": {"r3"},
}


def v2_scenario(scenario_id):
    return next(s for s in DATASET.scenarios if s.scenario_id == scenario_id)


def counts_selection(
    step_id,
    run_id,
    *,
    selected_count,
    context_record_count=0,
    planning_injected=False,
):
    return RetrievalSelectionEvidence(
        step_id=step_id,
        run_id=run_id,
        retrieval_status="COMPLETE",
        selected_count=selected_count,
        context_record_count=context_record_count,
        planning_injected=planning_injected,
        direct_entry_supplied=False,
        registered_selected_count=selected_count,
        open_selected_count=0,
        selected_memory_ids=None,
    )


def ids_selection(step_id, run_id, selected_ids, *, context_record_count=1, planning_injected=True):
    return RetrievalSelectionEvidence(
        step_id=step_id,
        run_id=run_id,
        retrieval_status="COMPLETE",
        selected_count=len(selected_ids),
        context_record_count=context_record_count,
        planning_injected=planning_injected,
        direct_entry_supplied=False,
        registered_selected_count=len(selected_ids),
        open_selected_count=0,
        selected_memory_ids=tuple(selected_ids),
    )


def record_for_expectation(expectation, memory_id):
    """按 V2 expected_state 的 MemoryRecordExpectation 构造 canonical record。"""
    payload = {} if expectation.status.value == "FORGOTTEN" else {"value": expectation.value}
    canonical_text = (
        "[FORGOTTEN]" if expectation.status.value == "FORGOTTEN" else f"{expectation.logical_key}: {expectation.value}"
    )
    return CanonicalMemoryRecord(
        memory_id=memory_id,
        agent_id=expectation.agent_id,
        memory_scope=expectation.memory_scope,
        memory_type="SEMANTIC",
        logical_key=expectation.logical_key,
        status=expectation.status.value,
        canonical_text=canonical_text,
        payload=payload,
        canonical_value={} if expectation.status.value == "FORGOTTEN" else expectation.value,
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-01T00:00:00+00:00",
        superseded_by_memory_id=None,
        origin_run_id="run-1",
        formation_method="llm",
    )


def snapshot(records):
    return MemoryStateSnapshot(snapshot_id="s", db_path="iso://m.db", captured_at=NOW, records=records)


def aliases(scenario, step_id):
    step = next(s for s in scenario.steps if s.step_id == step_id)
    assert step.expected_retrieval is not None
    return step.expected_retrieval


def _layer1_binding(alias_ids):
    return dict(alias_ids)


def build_alias_ids(scenario, step_id):
    """为 retrieval step 的 selected+excluded aliases 分配确定性的 memory_id。"""
    step = next(s for s in scenario.steps if s.step_id == step_id)
    expected = step.expected_retrieval
    result = {}
    for alias in [*expected.expected_selected, *expected.expected_excluded]:
        result[alias] = f"mem-{scenario.scenario_id}-{alias}"
    return result


# ------------------------------------------------------------------ Layer 1: identity REQUIRED must PASS


def _layer1_retrieval_result(scenario_id, step_id, context_record_count=0):
    scenario = v2_scenario(scenario_id)
    binding = build_alias_ids(scenario, step_id)
    expected = aliases(scenario, step_id)
    selected_ids = [binding[a] for a in expected.expected_selected]
    selection = ids_selection(
        step_id, "run-1", selected_ids, context_record_count=context_record_count, planning_injected=True
    )
    evidence = ScenarioEvaluationEvidence(
        scenario=scenario,
        selection_by_step={step_id: selection},
        alias_binding=binding,
        evaluation_layer=EvaluationLayer.LAYER_1_DETERMINISTIC,
    )
    return evaluate_retrieval(evidence)


def _assert_identity_pass(result, step_id):
    for assertion in result:
        if assertion.assertion_id.endswith(f"{step_id}.retrieval.recall_at_k"):
            assert assertion.status is AssertionStatus.PASS, assertion.assertion_id
        if assertion.assertion_id.endswith(f"{step_id}.retrieval.hit_at_k"):
            assert assertion.status is AssertionStatus.PASS, assertion.assertion_id
        if assertion.assertion_id.endswith(f"{step_id}.retrieval.rejection"):
            assert assertion.status is AssertionStatus.PASS, assertion.assertion_id


def test_layer1_active_hit_identity_selected():
    result = _layer1_retrieval_result("retrieval_active_hit", "r1", context_record_count=1)
    assert any(a.assertion_id.endswith("recall_at_k") and a.status is AssertionStatus.PASS for a in result)
    assert any(a.assertion_id.endswith("hit_at_k") and a.status is AssertionStatus.PASS for a in result)


def test_layer1_status_exclusion_active_selected_superseded_and_forgotten_excluded():
    result = _layer1_retrieval_result("retrieval_status_exclusion", "r1")
    _assert_identity_pass(result, "r1")
    rejection = next(a for a in result if a.assertion_id.endswith("r1.retrieval.rejection"))
    assert rejection.status is AssertionStatus.PASS


def test_layer1_open_hit_identity_selected():
    result = _layer1_retrieval_result("retrieval_open_hit", "r1")
    assert any(a.assertion_id.endswith("recall_at_k") and a.status is AssertionStatus.PASS for a in result)


def test_layer1_unrelated_rejection_identity_excluded():
    result = _layer1_retrieval_result("retrieval_unrelated_rejection", "r1")
    rejection = next(a for a in result if a.assertion_id.endswith("r1.retrieval.rejection"))
    assert rejection.status is AssertionStatus.PASS


def test_layer1_foreign_scope_identity_excluded():
    result = _layer1_retrieval_result("scope_isolation", "r1")
    rejection = next(a for a in result if a.assertion_id.endswith("r1.retrieval.rejection"))
    assert rejection.status is AssertionStatus.PASS


def test_layer1_forgotten_never_selected_or_injected():
    result = _layer1_retrieval_result("safety_forgotten_not_injected", "r1", context_record_count=1)
    _assert_identity_pass(result, "r1")
    assert not any(a.assertion_id.endswith("rejection") and a.status is AssertionStatus.FAIL for a in result)


def test_layer1_database_correction_new_selected_old_excluded():
    result = _layer1_retrieval_result("database_correction", "r3", context_record_count=1)
    _assert_identity_pass(result, "r3")


# ------------------------------------------------------------------ Layer 2: expected limitation + counts


def _layer2_scenario_evaluation(scenario_id, step_id, *, selected_count, context_record_count, planning_injected):
    scenario = v2_scenario(scenario_id)
    binding = build_alias_ids(scenario, step_id)
    selection = counts_selection(
        step_id,
        "run-1",
        selected_count=selected_count,
        context_record_count=context_record_count,
        planning_injected=planning_injected,
    )
    records = tuple(
        record_for_expectation(expectation, binding[expectation.alias]) for expectation in scenario.expected_state
    )
    evidence = ScenarioEvaluationEvidence(
        scenario=scenario,
        selection_by_step={step_id: selection},
        alias_binding=binding,
        final_snapshot=snapshot(records) if records else None,
        evaluation_layer=EvaluationLayer.LAYER_2_REAL_MODEL,
    )
    return evaluate_scenario(evidence)


def _expect_expected_limitation(assertion):
    assert assertion.status is AssertionStatus.BLOCKED
    assert assertion.blocked_by is BlockReason.NOT_SUPPORTED_BY_CURRENT_EVIDENCE
    assert assertion.evidence_gap_classification is EvidenceGapClassification.EXPECTED_EVIDENCE_LIMITATION


@pytest.mark.parametrize(
    ("scenario_id", "step_id", "context_record_count"),
    [
        ("retrieval_active_hit", "r1", 1),
        ("retrieval_status_exclusion", "r1", 0),
        ("retrieval_open_hit", "r1", 0),
        ("retrieval_unrelated_rejection", "r1", 0),
        ("scope_isolation", "r1", 0),
        ("safety_forgotten_not_injected", "r1", 1),
        ("database_correction", "r3", 1),
    ],
)
def test_layer2_identity_expected_limitation(scenario_id, step_id, context_record_count):
    result = _layer2_scenario_evaluation(
        scenario_id,
        step_id,
        selected_count=1,
        context_record_count=context_record_count,
        planning_injected=bool(context_record_count),
    )
    recall = next(a for a in result.assertions if a.assertion_id.endswith(f"{step_id}.retrieval.recall_at_k"))
    _expect_expected_limitation(recall)
    selected_count_assertion = next(
        a for a in result.assertions if a.assertion_id.endswith(f"{step_id}.retrieval.selected_count")
    )
    assert selected_count_assertion.status is AssertionStatus.PASS


def test_layer2_identity_blocked_and_count_level_pass_in_same_scenario():
    """最高风险点：retrieval_active_hit Layer2 即使 identity BLOCKED 也必须验证 count 与 context。"""
    result = _layer2_scenario_evaluation(
        "retrieval_active_hit", "r1", selected_count=1, context_record_count=1, planning_injected=True
    )
    recall = next(a for a in result.assertions if a.assertion_id.endswith("r1.retrieval.recall_at_k"))
    _expect_expected_limitation(recall)
    selected = next(a for a in result.assertions if a.assertion_id.endswith("r1.retrieval.selected_count"))
    assert selected.status is AssertionStatus.PASS
    injection = next(a for a in result.assertions if a.assertion_id.endswith("r1.injection"))
    assert injection.status is AssertionStatus.PASS
    assert result.metrics["expected_evidence_limitation_blocked_count"].blocked >= 1


def test_layer2_selected_count_zero_is_real_retrieval_miss_not_blocked():
    result = _layer2_scenario_evaluation(
        "retrieval_active_hit", "r1", selected_count=0, context_record_count=0, planning_injected=False
    )
    selected = next(a for a in result.assertions if a.assertion_id.endswith("r1.retrieval.selected_count"))
    assert selected.status is AssertionStatus.FAIL
    assert selected.failure_taxonomy is FailureTaxonomy.RETRIEVAL_MISS


def test_layer2_leakage_is_not_evaluable_not_zero():
    result = _layer2_scenario_evaluation(
        "safety_forgotten_not_injected", "r1", selected_count=1, context_record_count=1, planning_injected=True
    )
    forgotten = next(a for a in result.assertions if a.assertion_id.endswith(".leakage.forgotten"))
    assert forgotten.status is AssertionStatus.NOT_APPLICABLE
    assert result.metrics["forgotten_memory_leakage_rate"].evaluable_denominator == 0
    assert result.metrics["forgotten_memory_leakage_rate"].value is None
    assert not any(
        a.assertion_id.endswith(".leakage.forgotten") and a.status is AssertionStatus.PASS for a in result.assertions
    )


def _required_both_layers_retrieval(step):
    return step.expected_retrieval.model_copy(
        deep=True,
        update={
            "identity_evidence_by_layer": IdentityEvidenceByLayer(
                layer_1=IdentityEvidenceRequirement.REQUIRED,
                layer_2=IdentityEvidenceRequirement.REQUIRED,
            )
        },
    )


# ------------------------------------------------------------------ unexpected Layer2 gap


def test_layer2_required_identity_gap_is_unexpected_infra_failure():
    scenario = v2_scenario("retrieval_active_hit")
    # layer_2 强制 REQUIRED 的 V2 变体（unexpected gap）
    step = next(s for s in scenario.steps if s.step_id == "r1")
    modified = step.model_copy(
        deep=True,
        update={"expected_retrieval": _required_both_layers_retrieval(step)},
    )
    scn = scenario.model_copy(deep=True, update={"steps": [modified]})
    selection = counts_selection("r1", "run-1", selected_count=1, context_record_count=1, planning_injected=True)
    evidence = ScenarioEvaluationEvidence(
        scenario=scn,
        selection_by_step={"r1": selection},
        alias_binding={"db": "mem-db"},
        evaluation_layer=EvaluationLayer.LAYER_2_REAL_MODEL,
    )
    result = evaluate_scenario(evidence)
    recall = next(a for a in result.assertions if a.assertion_id.endswith("r1.retrieval.recall_at_k"))
    assert recall.status is AssertionStatus.BLOCKED
    assert recall.blocked_by is BlockReason.EVIDENCE_CAPTURE
    assert recall.evidence_gap_classification is None
    # unexpected evidence gap 进入 evaluation infra failure numerator
    assert result.metrics["evaluation_infra_failure_rate"].numerator >= 1


# ------------------------------------------------------------------ gate semantics


def test_gate_layer2_expected_limitation_is_not_infra_failure():
    result = _layer2_scenario_evaluation(
        "safety_forgotten_not_injected", "r1", selected_count=1, context_record_count=1, planning_injected=True
    )
    gate = evaluate_hard_gate([result], layer=EvaluationLayer.LAYER_2_REAL_MODEL, artifacts_retained=True)
    # expected limitation 不触发 evidence/provision infra failure reason
    assert not any("evidence/provision" in reason for reason in gate.reasons)
    assert gate.identity_safety_not_evaluable is True
    assert gate.expected_evidence_limitation_blocked_count >= 1
    # 不伪造 zero leakage PASS；leakage 保持 NOT_EVALUABLE
    assert gate.forgotten_leakage_not_evaluable is True
    assert not any("no leakage" in reason.lower() for reason in gate.reasons)


def test_gate_layer2_unexpected_identity_gap_is_infra_failure():
    scenario = v2_scenario("retrieval_active_hit")
    step = next(s for s in scenario.steps if s.step_id == "r1")
    modified = step.model_copy(
        deep=True,
        update={"expected_retrieval": _required_both_layers_retrieval(step)},
    )
    scn = scenario.model_copy(deep=True, update={"steps": [modified]})
    selection = counts_selection("r1", "run-1", selected_count=1, context_record_count=1, planning_injected=True)
    evidence = ScenarioEvaluationEvidence(
        scenario=scn,
        selection_by_step={"r1": selection},
        alias_binding={"db": "mem-db"},
        evaluation_layer=EvaluationLayer.LAYER_2_REAL_MODEL,
    )
    evaluation = evaluate_scenario(evidence)
    gate = evaluate_hard_gate([evaluation], layer=EvaluationLayer.LAYER_2_REAL_MODEL, artifacts_retained=True)
    assert not gate.passed
    assert any("evidence/provision" in reason for reason in gate.reasons)
    assert gate.identity_safety_not_evaluable is False


def test_gate_layer1_required_identity_gap_is_deterministic_concern():
    scenario = v2_scenario("retrieval_active_hit")
    # Layer1 REQUIRED；harness 未提供 identity evidence -> BLOCKED(evidence_capture)
    selection = counts_selection("r1", "run-1", selected_count=1, context_record_count=1, planning_injected=True)
    evidence = ScenarioEvaluationEvidence(
        scenario=scenario,
        selection_by_step={"r1": selection},
        alias_binding={"db": "mem-db"},
        evaluation_layer=EvaluationLayer.LAYER_1_DETERMINISTIC,
    )
    evaluation = evaluate_scenario(evidence)
    recall = next(a for a in evaluation.assertions if a.assertion_id.endswith("r1.retrieval.recall_at_k"))
    assert recall.status is AssertionStatus.BLOCKED
    assert recall.blocked_by is BlockReason.EVIDENCE_CAPTURE
    assert recall.evidence_gap_classification is None
    gate = evaluate_hard_gate([evaluation], layer=EvaluationLayer.LAYER_1_DETERMINISTIC, artifacts_retained=True)
    assert not gate.passed
    assert any("100% PASS" in reason for reason in gate.reasons) or any(
        "evidence/provision" in reason for reason in gate.reasons
    )
