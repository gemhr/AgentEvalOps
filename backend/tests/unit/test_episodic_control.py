"""WP6-E control expansion / fixture expansion / v3 wire mapping 契约测试。"""

# ruff: noqa: D101, D105, D415

import json

import pytest
from pydantic import ValidationError

from app.adapters.evaluation.episodic_http_target import (
    EpisodicV3Response,
    EpisodicV3TargetError,
    validate_run_uuid,
)
from app.core.evaluation.episodic_evidence import EpisodicEvidenceError
from app.core.evaluation.episodic_dataset import (
    EpisodicEvaluationControl,
    EpisodicEvaluationControlDeclaration,
    validate_episodic_dataset,
)
from app.services.evaluation.episodic_runner import (
    build_episodic_evaluation_control,
    build_episodic_fixture_wire,
)
from tests.unit.episodic_fixtures import load_dataset, scenario_by_case

DATASET = load_dataset()


def _run(scenario, run_id: str):
    return next(r for r in scenario.runs if r.run_id == run_id)


def test_fixture_wire_matches_target_dto_shape():
    scenario = scenario_by_case(DATASET, "E09")
    fixture = scenario.initial_fixture
    assert fixture is not None
    wire = build_episodic_fixture_wire(fixture)
    assert wire["fixture_ref"] == fixture.fixture_ref == "foreign_scope_episode"
    assert wire["agent_id"] == fixture.agent_id == "ops_router"
    assert wire["memory_scope"] == fixture.memory_scope == "orchestration"
    assert wire["origin_run_id"] == fixture.origin_run_id
    assert wire["situation"]
    assert wire["goal"]
    assert isinstance(wire["observations"], list) and wire["observations"]
    assert isinstance(wire["result"], dict) and wire["result"]["terminal_status"]
    assert "canonical_text" not in wire
    # Dataset loader 结构性拒绝 caller canonical_text（extra=forbid）
    payload = DATASET.model_dump(mode="json")
    for scenario_payload in payload["scenarios"]:
        if scenario_payload.get("case_code") == "E09":
            scenario_payload["initial_fixture"]["canonical_text"] = "caller-owned prose"
    with pytest.raises(ValidationError, match="Extra inputs"):
        validate_episodic_dataset(payload)


def test_e04_replay_symbolic_to_actual_uuid_mapping():
    scenario = scenario_by_case(DATASET, "E04")
    run = _run(scenario, "run_a")
    control = run.evaluation_control
    assert control is not None
    assert control.replay_run_id == "run_a"  # symbolic，dataset 侧
    actual_uuid = "123e4567-e89b-12d3-a456-426614174000"
    wire = build_episodic_evaluation_control(run, scenario, actual_runtime_run_id=actual_uuid)
    # 必须发送 actual UUID，不是 "run_a"
    assert wire["replay_run_id"] == actual_uuid
    assert wire["replay_run_id"] != "run_a"
    assert "REPLAY_EPISODIC_FORMATION_OBSERVER" in wire["capabilities"]
    assert "fixture" not in wire


def test_e09_fixture_control_expansion():
    scenario = scenario_by_case(DATASET, "E09")
    run = _run(scenario, "run_a")
    wire = build_episodic_evaluation_control(
        run, scenario, actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174001"
    )
    assert "INSTALL_EPISODIC_FIXTURE" in wire["capabilities"]
    assert "CAPTURE_EPISODIC_PIPELINE" in wire["capabilities"]
    assert wire["fixture"]["fixture_ref"] == "foreign_scope_episode"


def test_e02_e10_failed_run_control_maps():
    for case in ("E02", "E10"):
        scenario = scenario_by_case(DATASET, case)
        run = _run(scenario, "run_a")
        wire = build_episodic_evaluation_control(
            run, scenario, actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174002"
        )
        assert "DETERMINISTIC_FAILED_RUN" in wire["capabilities"]
        assert "fixture" not in wire
        assert "replay_run_id" not in wire


def test_e08_success_control_maps():
    scenario = scenario_by_case(DATASET, "E08")
    run_a = _run(scenario, "run_a")
    run_b = _run(scenario, "run_b")
    wire_a = build_episodic_evaluation_control(
        run_a, scenario, actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174003"
    )
    wire_b = build_episodic_evaluation_control(
        run_b, scenario, actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174004"
    )
    assert "DETERMINISTIC_EPISODIC_SUCCESS_RUN" in wire_a["capabilities"]
    assert "DETERMINISTIC_EPISODIC_SUCCESS_RUN" not in wire_b["capabilities"]
    assert "CAPTURE_EPISODIC_PIPELINE" in wire_b["capabilities"]
    # 禁止在控制中发送 plan/step/prompt（Dataset 无该字段，structural）
    assert "plan" not in wire_a
    assert "steps" not in wire_a


def test_no_control_run_emits_empty_control():
    scenario = scenario_by_case(DATASET, "E01")
    run = _run(scenario, "run_a")
    assert run.evaluation_control is None
    wire = build_episodic_evaluation_control(
        run, scenario, actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174005"
    )
    assert wire == {}


def test_capture_control_on_retrieval_runs():
    for case in ("E07", "E08", "E10", "E11", "E12"):
        scenario = scenario_by_case(DATASET, case)
        run_b = _run(scenario, "run_b")
        wire = build_episodic_evaluation_control(
            run_b, scenario, actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174006"
        )
        assert "CAPTURE_EPISODIC_PIPELINE" in wire["capabilities"]


def test_invalid_mapping_fails_closed():
    with pytest.raises(EpisodicV3TargetError):
        validate_run_uuid("run_a")
    with pytest.raises(EpisodicV3TargetError):
        validate_run_uuid("123e4567-e89b-12d3-a456")  # non-canonical


def test_evaluation_control_declaration_fail_closed():
    with pytest.raises(ValidationError):
        EpisodicEvaluationControlDeclaration(
            schema_version="episodic-evaluation-control.v1",
            capabilities=["EXECUTE_PYTHON"],
        )
    with pytest.raises(ValidationError):
        EpisodicEvaluationControlDeclaration(
            schema_version="episodic-evaluation-control.v1",
            capabilities=["DETERMINISTIC_FAILED_RUN", "REPLAY_EPISODIC_FORMATION_OBSERVER"],
            replay_run_id="x",
        )
    with pytest.raises(ValidationError):
        EpisodicEvaluationControlDeclaration(
            schema_version="episodic-evaluation-control.v1",
            capabilities=["REPLAY_EPISODIC_FORMATION_OBSERVER"],
        )


def test_v3_response_strict_parse():
    from tests.unit.episodic_fixtures import (
        capture_wire,
        runtime_receipt_wire,
        selection_item_wire,
        selection_wire,
        supplied_wire,
        injected_wire,
        v3_response_wire,
    )

    run_id = "123e4567-e89b-12d3-a456-426614174007"
    wire = v3_response_wire(
        run_id=run_id,
        formation_receipts=[
            {
                "run_id": run_id,
                "outcome": "CREATED",
                "memory_id": "episode-aaaabbbbccccdddd",
                "lesson_status": "ABSENT",
                "safe_reason": None,
            }
        ],
        capture=capture_wire(
            run_id=run_id,
            selection=selection_wire(
                candidate_count=1,
                items=[selection_item_wire("episode-aaaabbbbccccdddd", 1, 5, True)],
            ),
            supplied=supplied_wire(["episode-aaaabbbbccccdddd"]),
            injected=[injected_wire("PLANNING", ["episode-aaaabbbbccccdddd"])],
        ),
        runtime_receipt=runtime_receipt_wire(run_id, formed_memory_id="episode-aaaabbbbccccdddd"),
    )
    parsed = EpisodicV3Response.from_wire(wire)
    assert parsed.run_id == run_id
    assert parsed.formation_receipts[0].outcome == "CREATED"
    assert parsed.episodic_capture is not None
    assert parsed.episodic_capture.selection.selected_count == 1
    assert parsed.episodic_capture.supplied.record_count == 1
    assert parsed.episodic_capture.injected[0].target == "PLANNING"
    assert parsed.runtime_receipt.terminal_status == "SUCCEEDED"


def test_v3_response_rejects_malformed():
    with pytest.raises(EpisodicV3TargetError):
        EpisodicV3Response.from_wire({"protocol_version": "wrong"})
    with pytest.raises(EpisodicV3TargetError):
        EpisodicV3Response.from_wire([])
    # supplied record_count 与 ids 不一致 -> strict reject
    from tests.unit.episodic_fixtures import v3_response_wire

    wire = v3_response_wire(run_id="123e4567-e89b-12d3-a456-426614174008")
    wire["episodic_capture"] = {
        "schema_version": "episodic-evaluation-capture.v1",
        "run_id": "123e4567-e89b-12d3-a456-426614174008",
        "capture_outcome": "COMPLETE",
        "selection": {"candidate_count": 0, "selected": []},
        "supplied": {"episodic_memory_ids": [], "record_count": 2},
        "injected": [],
    }
    with pytest.raises((EpisodicV3TargetError, EpisodicEvidenceError)):
        EpisodicV3Response.from_wire(wire)
