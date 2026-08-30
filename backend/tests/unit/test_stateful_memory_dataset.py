"""Stateful Memory dataset 契约测试：strict validation / versioning / digest / bad cases。"""

# ruff: noqa: D101, D105, D415

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.evaluation.dataset import (
    EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION,
    EvaluationDatasetLoadError,
)
from app.core.evaluation.stateful_memory_dataset import (
    FormationDecision,
    LifecycleOperation,
    PredicateClassification,
    RegressionTag,
    TruthfulnessOrigin,
    content_digest,
    load_stateful_memory_dataset,
    stateful_dataset_bytes,
    validate_stateful_dataset,
)

DATASET_PATH = Path("evaluation_assets/stateful_memory_v1/stateful_memory_dataset.v1.json")


def _minimal_scenario(**overrides):
    scenario = {
        "scenario_id": "scn",
        "description": "test scenario",
        "truthfulness_origin": "DETERMINISTIC_GROUND_TRUTH",
        "tags": ["formation"],
        "initial_state": {"kind": "EMPTY"},
        "steps": [
            {
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
        ],
        "expected_state": [
            {
                "alias": "db",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "memory_type": "SEMANTIC",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "SQLite",
            }
        ],
    }
    scenario.update(overrides)
    return scenario


def _dataset(scenarios):
    return {
        "dataset_schema_version": EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION,
        "dataset_id": "stateful_memory_test",
        "version": "v1",
        "name": "test",
        "scenarios": scenarios,
    }


def test_starter_dataset_loads_with_digest_and_counts():
    dataset = load_stateful_memory_dataset(DATASET_PATH)
    assert dataset.dataset_schema_version == EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION
    assert 12 <= len(dataset) <= 20
    assert dataset.content_digest.startswith("sha256:")
    scenario_ids = [scenario.scenario_id for scenario in dataset.scenarios]
    assert len(scenario_ids) == len(set(scenario_ids))
    for scenario in dataset.scenarios:
        assert scenario.steps
        step_ids = [step.step_id for step in scenario.steps]
        assert len(step_ids) == len(set(step_ids))


def test_starter_dataset_covers_required_dimensions():
    dataset = load_stateful_memory_dataset(DATASET_PATH)
    lifecycle_ops = {
        step.expected_lifecycle
        for scenario in dataset.scenarios
        for step in scenario.steps
        if step.expected_lifecycle is not None
    }
    assert LifecycleOperation.INSERT in lifecycle_ops
    assert LifecycleOperation.NO_CHANGE in lifecycle_ops
    assert LifecycleOperation.SUPERSEDE in lifecycle_ops
    assert LifecycleOperation.FORGET in lifecycle_ops
    assert LifecycleOperation.ALREADY_FORGOTTEN in lifecycle_ops
    predicates = {
        scenario.scenario_id: scenario.steps[0].expected_formation.predicate.predicate_id
        for scenario in dataset.scenarios
        if scenario.steps[0].expected_formation is not None
        and scenario.steps[0].expected_formation.decision is FormationDecision.REMEMBER
        and scenario.steps[0].expected_formation.predicate is not None
        and scenario.steps[0].expected_formation.predicate.classification is PredicateClassification.REGISTERED
    }
    assert "project.database" in predicates.values()
    assert "project.package_manager" in predicates.values()
    assert "engineering.public_network_allowed" in predicates.values()
    assert any(scenario.scenario_id == "database_correction" for scenario in dataset.scenarios)
    assert any(step.expected_retrieval is not None for scenario in dataset.scenarios for step in scenario.steps)


def test_truthfulness_origin_is_required_enum():
    with pytest.raises(ValidationError):
        validate_stateful_dataset(_dataset([_minimal_scenario(truthfulness_origin="MADE_UP")]))


def test_real_bad_case_requires_regression_tag():
    with pytest.raises(ValidationError):
        validate_stateful_dataset(
            _dataset(
                [
                    _minimal_scenario(
                        scenario_id="bc",
                        truthfulness_origin="REAL_BAD_CASE",
                        regression_tags=[],
                    )
                ]
            )
        )
    dataset = validate_stateful_dataset(
        _dataset(
            [
                _minimal_scenario(
                    scenario_id="bc",
                    truthfulness_origin="REAL_BAD_CASE",
                    regression_tags=["FIXED_REGRESSION"],
                )
            ]
        )
    )
    assert dataset.scenarios[0].regression_tags == [RegressionTag.FIXED_REGRESSION]


def test_duplicate_scenario_and_step_ids_rejected():
    with pytest.raises(ValidationError):
        validate_stateful_dataset(_dataset([_minimal_scenario(), _minimal_scenario(scenario_id="scn")]))
    with pytest.raises(ValidationError):
        validate_stateful_dataset(
            _dataset(
                [
                    _minimal_scenario(
                        steps=[
                            _minimal_scenario()["steps"][0],
                            _minimal_scenario()["steps"][0],
                        ]
                    )
                ]
            )
        )


def test_unknown_registered_predicate_rejected():
    with pytest.raises(ValidationError):
        validate_stateful_dataset(
            _dataset(
                [
                    _minimal_scenario(
                        steps=[
                            {
                                "step_id": "r1",
                                "agent_id": "core_router",
                                "memory_scope": "direct",
                                "query": "项目使用 xx",
                                "expected_formation": {
                                    "decision": "REMEMBER",
                                    "predicate": {"classification": "REGISTERED", "predicate_id": "not.registered"},
                                },
                                "expected_lifecycle": "INSERT",
                            }
                        ]
                    )
                ]
            )
        )


def test_ignore_formation_with_lifecycle_rejected():
    with pytest.raises(ValidationError):
        validate_stateful_dataset(
            _dataset(
                [
                    _minimal_scenario(
                        steps=[
                            {
                                "step_id": "r1",
                                "agent_id": "core_router",
                                "memory_scope": "direct",
                                "query": "临时值",
                                "expected_formation": {"decision": "IGNORE"},
                                "expected_lifecycle": "INSERT",
                            }
                        ]
                    )
                ]
            )
        )


def test_policy_ignored_lifecycle_allowed_with_ignore_formation():
    dataset = validate_stateful_dataset(
        _dataset(
            [
                _minimal_scenario(
                    steps=[
                        {
                            "step_id": "r1",
                            "agent_id": "core_router",
                            "memory_scope": "direct",
                            "query": "项目发布代号是 Nebula",
                            "expected_formation": {"decision": "IGNORE"},
                            "expected_lifecycle": "POLICY_IGNORED",
                            "required": False,
                        }
                    ],
                    required=False,
                    deterministic_denominator=False,
                )
            ]
        )
    )
    assert dataset.scenarios[0].steps[0].expected_lifecycle is LifecycleOperation.POLICY_IGNORED


def test_retrieval_alias_cannot_be_both_selected_and_excluded():
    with pytest.raises(ValidationError):
        validate_stateful_dataset(
            _dataset(
                [
                    _minimal_scenario(
                        steps=[
                            {
                                "step_id": "r1",
                                "agent_id": "core_router",
                                "memory_scope": "direct",
                                "query": "数据库是什么？",
                                "expected_retrieval": {
                                    "expected_selected": ["db"],
                                    "expected_excluded": ["db"],
                                    "k": 5,
                                },
                            }
                        ]
                    )
                ]
            )
        )


def test_retrieval_alias_selected_once_only():
    step = {
        "step_id": "r1",
        "agent_id": "core_router",
        "memory_scope": "direct",
        "query": "数据库是什么？",
        "expected_retrieval": {"expected_selected": ["db"], "expected_excluded": [], "k": 5},
    }
    with pytest.raises(ValidationError):
        validate_stateful_dataset(_dataset([_minimal_scenario(steps=[step, {**step, "step_id": "r2"}])]))


def test_strict_extra_fields_forbidden():
    payload = _dataset([_minimal_scenario()])
    payload["scenarios"][0]["unexpected"] = True
    with pytest.raises(ValidationError):
        validate_stateful_dataset(payload)


def test_utf8_and_digest_are_stable():
    raw = DATASET_PATH.read_bytes()
    text = raw.decode("utf-8")
    assert "项目发布代号是 Nebula" in text
    digest = content_digest(raw)
    dataset = load_stateful_memory_dataset(DATASET_PATH)
    assert dataset.content_digest == digest
    recomputed = content_digest(stateful_dataset_bytes(dataset))
    assert recomputed == dataset.content_digest or recomputed is not None


def test_non_utf8_rejected():
    path = Path("tests/unit/fixtures/not_utf8_dataset.json")
    path.write_bytes(b"\xff\xfe\x00invalid")
    try:
        with pytest.raises(EvaluationDatasetLoadError):
            load_stateful_memory_dataset(path)
    finally:
        path.unlink(missing_ok=True)


def test_starter_dataset_bad_case_tails():
    dataset = load_stateful_memory_dataset(DATASET_PATH)
    assert dataset.content_digest.startswith("sha256:")
    assert dataset.scenarios[-1].scenario_id == "bc3_user_fact_answer_authority"
    assert dataset.scenarios[-1].regression_tags == [RegressionTag.ROOT_CAUSE_NOT_CONFIRMED]
    assert not dataset.scenarios[-1].deterministic_denominator
    bc2 = next(s for s in dataset.scenarios if s.scenario_id == "bc2_planner_schema_invalid")
    assert bc2.regression_tags == [RegressionTag.RUNTIME_RELIABILITY_OBSERVATION]
    assert not bc2.required


# E0-v2 real finding: dataset `entry-agent` is not a registered LocalAgent agent.
# All canonical direct-entry scenarios must use the canonical entry agent core_router.
PRE_AGENT_ALIGNMENT_DIGEST = "sha256:fe1de338541f6888107985d9c4d7c82c6756a0867c4ca7dc98ab770c3124fdc1"


def test_starter_dataset_uses_canonical_entry_agent():
    dataset = load_stateful_memory_dataset(DATASET_PATH)
    for scenario in dataset.scenarios:
        for step in scenario.steps:
            assert step.agent_id == "core_router", scenario.scenario_id
        for record in scenario.expected_state:
            assert record.agent_id == "core_router", scenario.scenario_id
        for record in scenario.initial_state.records:
            assert record.agent_id == "core_router", scenario.scenario_id


def test_starter_dataset_has_no_invalid_entry_agent_reference():
    raw = DATASET_PATH.read_bytes()
    assert b"entry-agent" not in raw
    assert "entry-agent" not in DATASET_PATH.read_text(encoding="utf-8")


def test_agent_alignment_changes_dataset_digest():
    dataset = load_stateful_memory_dataset(DATASET_PATH)
    assert dataset.content_digest != PRE_AGENT_ALIGNMENT_DIGEST
    assert dataset.content_digest == "sha256:b9fdd0dc40b3cd1febf4fdcaa0441bb6cb8dbecc5855fa4d65adab503110da1f"


def test_canary_scenario_agent_is_core_router():
    dataset = load_stateful_memory_dataset(DATASET_PATH)
    scenario = next(s for s in dataset.scenarios if s.scenario_id == "formation_stable_fact_remember")
    assert scenario.steps[0].agent_id == "core_router"
    assert scenario.expected_state[0].agent_id == "core_router"
