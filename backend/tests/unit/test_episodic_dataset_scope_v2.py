"""WP6 G6-R1 scope-corrected Episodic Dataset V2 lineage regression."""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.evaluation.episodic_dataset import (
    EPISODIC_DATASET_SCHEMA_V2,
    EPISODIC_DATASET_V2_ID,
    EPISODIC_DATASET_V2_VERSION,
    FROZEN_EPISODIC_SCENARIOS,
    load_episodic_dataset,
    validate_episodic_dataset,
)
from app.core.evaluation.episodic_evaluators import evaluate_episodic_scenario
from app.core.evaluation.stateful_assertion import AssertionStatus
from app.core.evaluation.stateful_journal import JournalStepFacts
from tests.unit.test_episodic_evaluators import _evidence, _projection_for_run, _run_evidence

V1_PATH = Path("evaluation_assets/stateful_episodic_v1/stateful_episodic_dataset.v1.json")
V2_PATH = Path("evaluation_assets/stateful_episodic_v2/stateful_episodic_dataset.v2.json")
V1_DIGEST = "sha256:d87ccfe28e414b90b8df10ffe3b1107b24f70dacf98e2996bb93f4950b105a2f"
V2_DIGEST = "sha256:678ecc706c6d0e199c057e7c45d94816679f9c9d9d6a9cc47b704e1b5f589e62"


def test_v1_is_unchanged_and_v2_is_a_distinct_strict_lineage() -> None:
    v1 = load_episodic_dataset(V1_PATH)
    v2 = load_episodic_dataset(V2_PATH)

    assert v1.content_digest == V1_DIGEST
    assert v2.content_digest == V2_DIGEST
    assert (v2.dataset_schema_version, v2.dataset_id, v2.version) == (
        EPISODIC_DATASET_SCHEMA_V2,
        EPISODIC_DATASET_V2_ID,
        EPISODIC_DATASET_V2_VERSION,
    )
    assert [(item.case_code, item.scenario_id.removeprefix("e").split("_", 1)[1]) for item in v2.scenarios] == list(
        FROZEN_EPISODIC_SCENARIOS
    )


def test_v2_audits_every_grounding_run_and_keeps_no_planner_identity_gate() -> None:
    v2 = load_episodic_dataset(V2_PATH)
    grounding_runs = [run for scenario in v2.scenarios for run in scenario.runs if run.expected_grounding is not None]

    assert len(grounding_runs) == 16
    assert all(not run.expected_grounding.required_observed_step_statuses for run in grounding_runs)
    assert all(run.expected_grounding.require_runtime_step_facts for run in grounding_runs)


def test_v2_preserves_primary_claim_scenarios() -> None:
    v2 = load_episodic_dataset(V2_PATH)
    scenarios = {item.case_code: item for item in v2.scenarios}

    assert scenarios["E04"].assertion_groups.idempotency is not None
    assert scenarios["E05"].runs[0].expected_grounding.require_runtime_step_facts is True
    assert scenarios["E07"].runs[1].expected_retrieval is not None
    assert scenarios["E08"].runs[1].expected_retrieval.expected_selected_count == 0


def test_v2_rejects_reintroduced_predefined_planner_task_identity() -> None:
    v2 = load_episodic_dataset(V2_PATH)
    payload = copy.deepcopy(v2.model_dump(mode="json", exclude={"content_digest"}))
    payload["scenarios"][0]["runs"][0]["expected_grounding"]["required_observed_step_statuses"] = [
        {"step_ref": "release_list", "expected_status": "SUCCEEDED"}
    ]

    with pytest.raises(ValidationError, match="must not predefine Planner task identities"):
        validate_episodic_dataset(payload)


def test_v2_rejects_missing_runtime_evidence_requirement() -> None:
    v2 = load_episodic_dataset(V2_PATH)
    payload = copy.deepcopy(v2.model_dump(mode="json", exclude={"content_digest"}))
    payload["scenarios"][0]["runs"][0]["expected_grounding"]["require_runtime_step_facts"] = False

    with pytest.raises(ValidationError, match="requires runtime step evidence"):
        validate_episodic_dataset(payload)


def test_v2_blocks_when_actual_runtime_step_evidence_is_missing() -> None:
    scenario = next(item for item in load_episodic_dataset(V2_PATH).scenarios if item.case_code == "E05")
    run = scenario.runs[0]
    record = _run_evidence(scenario, run, "episode-e05-v2", actual_run_id="123e4567-e89b-12d3-a456-426614174701")
    record = dataclasses.replace(
        record,
        step_facts=JournalStepFacts(run_id=record.actual_runtime_run_id, facts=()),
    )
    evaluation = evaluate_episodic_scenario(
        _evidence(
            scenario,
            [record],
            projections=[_projection_for_run(scenario, run, "episode-e05-v2")],
            auto_step_facts=False,
        )
    )

    runtime_evidence = next(item for item in evaluation.assertions if item.assertion_id.endswith(".runtime_evidence"))
    assert runtime_evidence.status is AssertionStatus.BLOCKED
    assert evaluation.scenario_outcome is AssertionStatus.BLOCKED
