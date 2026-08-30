"""Stateful Memory hard gate：deterministic 100% PASS / leakage zero / invariant zero。"""

# ruff: noqa: D101, D105, D415

from datetime import UTC, datetime
from dataclasses import replace

import pytest

from app.core.evaluation.stateful_assertion import AssertionStatus
from app.core.evaluation.stateful_evaluators import (
    RetrievalSelectionEvidence,
    ScenarioEvaluationEvidence,
    evaluate_scenario,
)
from app.core.evaluation.stateful_gate import EvaluationLayer, evaluate_hard_gate
from app.core.evaluation.stateful_journal import (
    FormationEvent,
    JournalEvents,
    LifecycleEvent,
)
from app.core.evaluation.stateful_memory_dataset import (
    StatefulMemoryScenario,
    StatefulMemoryStep,
)
from app.core.evaluation.stateful_projection import (
    CanonicalMemoryRecord,
    MemoryStateSnapshot,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def rec(memory_id, *, status="ACTIVE", logical_key="project.database", value="SQLite"):
    payload = {} if status == "FORGOTTEN" else {"value": value}
    return CanonicalMemoryRecord(
        memory_id=memory_id,
        agent_id="core_router",
        memory_scope="direct",
        memory_type="SEMANTIC",
        logical_key=logical_key,
        status=status,
        canonical_text="[FORGOTTEN]" if status == "FORGOTTEN" else f"{logical_key}: {value}",
        payload=payload,
        canonical_value={} if status == "FORGOTTEN" else value,
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-01T00:00:00+00:00",
        superseded_by_memory_id=None,
        origin_run_id="run-1",
        formation_method="llm",
    )


def snap(records):
    return MemoryStateSnapshot(snapshot_id="s", db_path="iso://m.db", captured_at=NOW, records=records)


def make_scenario(steps, expected_state, *, scenario_id="gate_scn"):
    return StatefulMemoryScenario.model_validate(
        {
            "scenario_id": scenario_id,
            "description": "gate",
            "truthfulness_origin": "DETERMINISTIC_GROUND_TRUTH",
            "tags": [],
            "initial_state": {"kind": "EMPTY"},
            "steps": [s.model_dump() for s in steps],
            "expected_state": expected_state,
        }
    )


def make_scenario_origin(
    steps, expected_state, *, origin, scenario_id="gate_scn", required=True, det_denominator=True, regression_tags=None
):
    return StatefulMemoryScenario.model_validate(
        {
            "scenario_id": scenario_id,
            "description": "gate",
            "truthfulness_origin": origin,
            "tags": [],
            "regression_tags": regression_tags or [],
            "initial_state": {"kind": "EMPTY"},
            "steps": [s.model_dump() for s in steps],
            "expected_state": expected_state,
            "required": required,
            "deterministic_denominator": det_denominator,
        }
    )


def make_step(**overrides):
    data = {
        "step_id": "r1",
        "agent_id": "core_router",
        "memory_scope": "direct",
        "query": "项目数据库使用 SQLite",
        "expected_formation": {
            "decision": "REMEMBER",
            "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
        },
        "expected_lifecycle": "INSERT",
    }
    data.update(overrides)
    return StatefulMemoryStep.model_validate(data)


def passing_evaluation():
    scn = make_scenario(
        [make_step()],
        [
            {
                "alias": "db",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "SQLite",
            }
        ],
    )
    post = snap((rec("mem-1"),))
    ev = ScenarioEvaluationEvidence(
        scenario=scn,
        journal_by_step={
            "r1": JournalEvents(
                "run-1",
                (FormationEvent("run-1", "f1", "llm", "OK", None, 1, 1, 0, 1, 0, 0, "NONE"),),
                (LifecycleEvent("run-1", "l1", "SEMANTIC", "INSERT", "OK", None, 1, None, "mem-1", None, "NONE"),),
                (),
            )
        },
        snapshots_by_step={"r1": (snap(()), post)},
        final_snapshot=post,
        outcome_kind_by_step={"r1": "SUCCESS"},
        run_id_by_step={"r1": "run-1"},
    )
    return evaluate_scenario(ev)


def leaking_evaluation():
    scn = make_scenario(
        [
            make_step(
                expected_formation=None,
                expected_lifecycle=None,
                expected_retrieval={"expected_selected": ["db"], "expected_excluded": [], "k": 5},
                query="当前项目数据库是什么？",
            )
        ],
        [],
    )
    ev = ScenarioEvaluationEvidence(
        scenario=scn,
        selection_by_step={
            "r1": RetrievalSelectionEvidence(
                step_id="r1",
                run_id="run-1",
                retrieval_status="COMPLETE",
                selected_count=1,
                context_record_count=1,
                planning_injected=True,
                direct_entry_supplied=False,
                registered_selected_count=1,
                open_selected_count=0,
                selected_memory_ids=("mem-forgotten",),
            )
        },
        final_snapshot=snap((rec("mem-forgotten", status="FORGOTTEN"),)),
    )
    return evaluate_scenario(ev)


def invariant_violation_evaluation():
    scn = make_scenario(
        [make_step()],
        [
            {
                "alias": "db",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "SQLite",
            }
        ],
    )
    post = snap((rec("mem-1"), rec("mem-2", logical_key="project.database", value="SQLite")))
    ev = ScenarioEvaluationEvidence(
        scenario=scn,
        journal_by_step={
            "r1": JournalEvents(
                "run-1",
                (FormationEvent("run-1", "f1", "llm", "OK", None, 1, 1, 0, 1, 0, 0, "NONE"),),
                (LifecycleEvent("run-1", "l1", "SEMANTIC", "INSERT", "OK", None, 1, None, "mem-1", None, "NONE"),),
                (),
            )
        },
        snapshots_by_step={"r1": (snap(()), post)},
        final_snapshot=post,
        outcome_kind_by_step={"r1": "SUCCESS"},
        run_id_by_step={"r1": "run-1"},
    )
    return evaluate_scenario(ev)


def blocked_evaluation():
    scn = make_scenario([make_step()], [])
    ev = ScenarioEvaluationEvidence(
        scenario=scn,
        journal_by_step={"r1": JournalEvents("run-1", (), (), ())},
        outcome_kind_by_step={"r1": "FAILURE"},
    )
    return evaluate_scenario(ev)


def test_gate_passes_with_all_deterministic_scenarios_passing():
    evaluation = passing_evaluation()
    assert evaluation.scenario_outcome is AssertionStatus.PASS
    assert evaluation.deterministic_gate_eligible is True
    result = evaluate_hard_gate([evaluation], layer=EvaluationLayer.LAYER_1_DETERMINISTIC, artifacts_retained=True)
    assert result.passed is True
    assert result.deterministic_gate_required_passed == 1


def test_gate_fails_on_forgotten_leakage():
    evaluation = leaking_evaluation()
    result = evaluate_hard_gate([evaluation], layer=EvaluationLayer.LAYER_1_DETERMINISTIC, artifacts_retained=True)
    assert result.passed is False
    assert result.forgotten_leakage_failures == 1
    assert any("forgotten" in reason for reason in result.reasons)


def test_gate_fails_on_keyed_active_invariant_violation():
    evaluation = invariant_violation_evaluation()
    result = evaluate_hard_gate([evaluation], layer=EvaluationLayer.LAYER_1_DETERMINISTIC, artifacts_retained=True)
    assert result.passed is False
    assert result.keyed_active_invariant_violations == 1


def test_gate_fails_when_evidence_failure_present():
    evaluation = blocked_evaluation()
    result = evaluate_hard_gate([evaluation], layer=EvaluationLayer.LAYER_1_DETERMINISTIC, artifacts_retained=True)
    assert result.passed is False
    assert any("evidence/provision" in reason for reason in result.reasons)


def test_gate_fails_when_artifacts_not_retained():
    evaluation = passing_evaluation()
    result = evaluate_hard_gate([evaluation], layer=EvaluationLayer.LAYER_1_DETERMINISTIC, artifacts_retained=False)
    assert result.passed is False
    assert any("retained" in reason for reason in result.reasons)


def test_gate_deterministic_100_percent_requirement():
    good = passing_evaluation()
    bad = leaking_evaluation()
    result = evaluate_hard_gate(
        [good, bad],
        layer=EvaluationLayer.LAYER_1_DETERMINISTIC,
        artifacts_retained=True,
    )
    assert result.passed is False
    assert result.deterministic_gate_required_passed == 1
    assert result.deterministic_gate_required_total == 2


# ------------------------------------------------------------------ E1-R2 gate membership


def _eval_for_origin(origin, *, required=True, det_denominator=True, scenario_id="s"):
    scn = make_scenario_origin(
        [make_step()],
        [
            {
                "alias": "db",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "SQLite",
            }
        ],
        origin=origin,
        scenario_id=scenario_id,
        required=required,
        det_denominator=det_denominator,
        regression_tags=["FIXED_REGRESSION"] if origin == "REAL_BAD_CASE" else None,
    )
    post = snap((rec("mem-1"),))
    ev = ScenarioEvaluationEvidence(
        scenario=scn,
        journal_by_step={
            "r1": JournalEvents(
                "run-1",
                (FormationEvent("run-1", "f1", "llm", "OK", None, 1, 1, 0, 1, 0, 0, "NONE"),),
                (LifecycleEvent("run-1", "l1", "SEMANTIC", "INSERT", "OK", None, 1, None, "mem-1", None, "NONE"),),
                (),
            )
        },
        snapshots_by_step={"r1": (snap(()), post)},
        final_snapshot=post,
        outcome_kind_by_step={"r1": "SUCCESS"},
        run_id_by_step={"r1": "run-1"},
    )
    return evaluate_scenario(ev)


def test_gate_membership_deterministic_origin_eligible():
    evaluation = _eval_for_origin("DETERMINISTIC_GROUND_TRUTH")
    assert evaluation.deterministic_gate_eligible is True


def test_gate_membership_human_reviewed_not_eligible():
    evaluation = _eval_for_origin("HUMAN_REVIEWED")
    assert evaluation.deterministic_gate_eligible is False


def test_gate_membership_real_bad_case_not_eligible():
    evaluation = _eval_for_origin("REAL_BAD_CASE")
    assert evaluation.deterministic_gate_eligible is False


def test_gate_membership_synthetic_not_eligible_by_default():
    evaluation = _eval_for_origin("SYNTHETIC_CASE")
    assert evaluation.deterministic_gate_eligible is False


def test_gate_derives_membership_not_caller_list():
    good = _eval_for_origin("DETERMINISTIC_GROUND_TRUTH", scenario_id="d")
    human = _eval_for_origin("HUMAN_REVIEWED", scenario_id="h")
    result = evaluate_hard_gate(
        [good, human],
        layer=EvaluationLayer.LAYER_1_DETERMINISTIC,
        artifacts_retained=True,
    )
    # only the deterministic-origin scenario is in the gate set; it PASSes
    assert result.deterministic_gate_required_total == 1


def test_execution_required_total_excludes_non_required_scenarios():
    required = passing_evaluation()
    optional = passing_evaluation()
    optional = replace(optional, scenario_id="optional", required=False)

    result = evaluate_hard_gate(
        [required, optional],
        layer=EvaluationLayer.LAYER_2_REAL_MODEL,
        artifacts_retained=True,
    )

    assert result.execution_required_total == 1
    assert result.deterministic_gate_required_passed == 1
    assert result.passed is True


def test_gate_layer2_does_not_fail_on_real_model_nonpass():
    bad = leaking_evaluation()  # scenario FAIL (leakage)
    result = evaluate_hard_gate(
        [bad],
        layer=EvaluationLayer.LAYER_2_REAL_MODEL,
        artifacts_retained=True,
    )
    # Layer 2: real-model behavioral non-PASS is report-only, not a deterministic threshold failure
    assert result.passed is False  # but actual leakage FAIL still fails the correctness gate
    assert any("forgotten" in reason for reason in result.reasons)
    assert not any("100% PASS" in reason for reason in result.reasons)


def test_gate_does_not_claim_zero_leakage_when_na():
    passing = passing_evaluation()
    result = evaluate_hard_gate(
        [passing],
        layer=EvaluationLayer.LAYER_2_REAL_MODEL,
        artifacts_retained=True,
    )
    assert result.forgotten_leakage_not_evaluable is True
    assert result.forgotten_leakage_failures == 0
    assert not any("zero" in reason.lower() for reason in result.reasons)
    assert not any("no leakage" in reason.lower() for reason in result.reasons)
