"""WP7-E V2 governance dataset/evaluator regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.evaluation.multi_agent_memory_governance import Observation, Verdict, evaluate_scenario, load_dataset


DATA = Path(__file__).parents[2] / "evaluation_assets/multi_agent_memory_governance_v2/dataset.json"


def _observation(
    *, decision: str = "ALLOW", project: tuple[int, int, int, int] = (0, 0, 0, 0), mutation: dict | None = None
) -> Observation:
    body = {
        "authorization": {"decision": decision},
        "private_retrieval": {"candidate_count": 0, "selected_count": 0, "supplied_count": 0, "injected_count": 0},
        "project_retrieval": dict(
            zip(("candidate_count", "selected_count", "supplied_count", "injected_count"), project, strict=True)
        ),
        "mutation": mutation,
        "promotion": None,
        "specialist_formation": [],
        "invocation_visibility": [],
    }
    return Observation.from_response(body)


def test_v2_loader_accepts_immutable_lineage_and_stateful_runs() -> None:
    dataset = load_dataset(DATA)
    assert dataset.parent_dataset_id == "multi_agent_memory_governance_v1"
    assert [run.run_id for run in dataset.scenarios[3].runs] == ["run_a", "run_b"]


def test_loader_fails_closed_for_unknown_nested_field(tmp_path: Path) -> None:
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    payload["scenarios"][0]["runs"][0]["unexpected"] = True
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_dataset(path)


def test_g04_requires_prior_write_and_actual_selected_supplied_injected() -> None:
    prior = _observation(mutation={"before_count": 0, "affected_count": 1, "after_count": 1, "outcome": "CREATED"})
    recalled = _observation(project=(1, 1, 1, 1))
    assert evaluate_scenario("G04", [prior, recalled]) is Verdict.PASS
    assert evaluate_scenario("G04", [prior, _observation(project=(1, 1, 0, 1))]) is Verdict.FAIL


def test_g05_and_g06_require_target_existence() -> None:
    absent = _observation(mutation={"before_count": 0, "affected_count": 0, "after_count": 0, "outcome": "DENIED"})
    assert evaluate_scenario("G05", [absent, _observation()]) is Verdict.BLOCKED
    prior = _observation(mutation={"before_count": 0, "affected_count": 1, "after_count": 1, "outcome": "CREATED"})
    assert evaluate_scenario("G05", [prior, _observation()]) is Verdict.PASS
    assert evaluate_scenario("G06", [prior, _observation(decision="DENY")]) is Verdict.PASS


def test_g12_requires_injection_and_user_content_trust() -> None:
    prior = _observation(mutation={"before_count": 0, "affected_count": 1, "after_count": 1, "outcome": "CREATED"})
    current = _observation(project=(1, 1, 1, 1))
    current.project_retrieval.context_sources = [
        {"source_type": "project_memory_retrieval", "trust_role": "user_content"}
    ]
    assert evaluate_scenario("G12", [prior, current]) is Verdict.PASS
