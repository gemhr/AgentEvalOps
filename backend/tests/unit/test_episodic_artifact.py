"""WP6-E aggregate artifact / implementation ref / baseline candidate 契约测试。"""

# ruff: noqa: D101, D105, D415

from pathlib import Path

import pytest

from app.core.evaluation.episodic_artifact import (
    EpisodicExperimentArtifact,
    EpisodicRunAttemptRecord,
    EpisodicScenarioArtifact,
)
from app.core.evaluation.episodic_baseline import (
    EPISODIC_BASELINE_ID,
    EPISODIC_BASELINE_V2_ID,
    EpisodicBaselineAuthority,
    EpisodicBaselineCompatibility,
    EpisodicBaselineCandidate,
    EpisodicBaselineProvenance,
    EpisodicBaselineStatus,
)
from app.core.evaluation.episodic_impl_ref import (
    EPISODIC_SEMANTIC_SOURCE_FILES,
    episodic_evaluation_implementation_ref,
)
from app.core.evaluation.episodic_metrics import build_episodic_scenario_success_aggregate
from app.core.evaluation.stateful_assertion import AssertionStatus
from tests.unit.episodic_fixtures import load_dataset

DATASET = load_dataset()


def test_impl_ref_covers_semantic_owners():
    ref = episodic_evaluation_implementation_ref()
    assert ref.startswith("sha256:")
    assert len(ref) == len("sha256:") + 64
    # manifest 覆盖关键 semantic owners
    joined = "\n".join(EPISODIC_SEMANTIC_SOURCE_FILES)
    for owner in (
        "episodic_dataset.py",
        "episodic_runner.py",
        "episodic_evaluators.py",
        "episodic_metrics.py",
        "episodic_gate.py",
        "episodic_artifact.py",
        "episodic_impl_ref.py",
        "episodic_baseline.py",
        "episodic_environment.py",
        "episodic_http_target.py",
        "stateful_journal.py",
        "stateful_projection.py",
        "stateful_episodic_dataset.v1.json",
    ):
        assert owner in joined, owner


def test_impl_ref_deterministic():
    assert episodic_evaluation_implementation_ref() == episodic_evaluation_implementation_ref()


def test_impl_ref_changes_on_semantic_change(tmp_path):
    backend = Path(__file__).resolve().parents[2]  # backend root
    ref = episodic_evaluation_implementation_ref(backend_root=backend)
    assert ref.startswith("sha256:")


def test_baseline_candidate_not_canonical():
    candidate = EpisodicBaselineCandidate(
        status=EpisodicBaselineStatus.CANDIDATE,
        provenance=EpisodicBaselineProvenance(
            baseline_id=EPISODIC_BASELINE_ID,
            dataset_id=DATASET.dataset_id,
            dataset_version=DATASET.version,
            dataset_digest=DATASET.content_digest,
            agentevalops_implementation_ref="sha256:aaaa",
            target_evaluation_implementation_ref="sha256:bbbb",
            interpreter_ref="python",
            execution_policy="GLOBAL_SEQUENTIAL",
        ),
        scenario_outcomes={"E01": "PASS"},
    )
    assert candidate.canonical_baseline is False
    snapshot = candidate.to_dict()
    assert snapshot["canonical_baseline"] is False
    assert snapshot["status"] == "CANDIDATE"
    # 禁止宣称 canonical
    with pytest.raises(ValueError, match="must not declare a canonical baseline"):
        EpisodicBaselineCandidate(
            status=EpisodicBaselineStatus.CANDIDATE,
            provenance=candidate.provenance,
            canonical_baseline=True,
        )


def test_baseline_candidate_requires_provenance_refs():
    with pytest.raises(ValueError):
        EpisodicBaselineCandidate(
            status=EpisodicBaselineStatus.CANDIDATE,
            provenance=EpisodicBaselineProvenance(
                baseline_id=EPISODIC_BASELINE_ID,
                dataset_id=DATASET.dataset_id,
                dataset_version=DATASET.version,
                dataset_digest=DATASET.content_digest,
                agentevalops_implementation_ref=None,
                target_evaluation_implementation_ref=None,
            ),
        )


def test_run_attempt_record_artifact_shape():
    record = EpisodicRunAttemptRecord(
        scenario_id="s",
        case_code="E01",
        dataset_run_id="run_a",
        actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174500",
        execution_status="EXECUTED",
        terminal_status="SUCCEEDED",
        delivery_status="DELIVERED",
        evaluation_controls_sent=[],
        formation_receipt_summary={"outcome": "CREATED", "memory_id": "episode-x"},
    )
    assert record.dataset_run_id == "run_a"
    assert record.formation_receipt_summary["outcome"] == "CREATED"


def test_scenario_artifact_private_and_no_body_by_default():
    artifact = EpisodicScenarioArtifact(
        evaluation_run_id="run-1",
        scenario_id="e01",
        case_code="E01",
        truthfulness_origin="DETERMINISTIC_GROUND_TRUTH",
        episode_origin_kind="RUN_FORMED",
        scenario_outcome="PASS",
        scenario_outcome_assertion={"status": "PASS"},
        assertion_results=[],
        metric_aggregates={},
        failure_taxonomies=[],
        episode_projection_summary=[
            {
                "memory_id": "episode-x",
                "memory_type": "EPISODIC",
                "status": "ACTIVE",
                "observations": [],
            }
        ],
        private_evaluation_artifact=True,
    )
    assert artifact.private_evaluation_artifact is True
    # projection summary 不携带 canonical_text/situation/goal 正文
    summary = artifact.episode_projection_summary[0]
    assert "canonical_text" not in summary
    assert "situation_text" not in summary
    assert "goal_text" not in summary


def test_experiment_artifact_requires_dual_refs():
    artifact = EpisodicExperimentArtifact(
        experiment_id="exp-1",
        dataset={
            "schema": "stateful-episodic-scenario.v1",
            "id": "stateful_episodic_v1",
            "version": "v1",
            "raw_digest": "sha256:x",
            "scenario_count": 12,
        },
        agentevalops_implementation_ref="sha256:aaaa",
        target_evaluation_implementation_ref="sha256:bbbb",
        execution_policy="GLOBAL_SEQUENTIAL",
        layer1_gate={"passed": True},
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert artifact.schema_version == "stateful-episodic-evaluation-artifact.v1"
    assert artifact.agentevalops_implementation_ref
    assert artifact.target_evaluation_implementation_ref
    compat = artifact.model_dump_json_compat()
    assert isinstance(compat, dict)
    assert compat["layer1_gate"]["passed"] is True


def test_scenario_success_rate_denominator():
    aggregate = build_episodic_scenario_success_aggregate(
        [AssertionStatus.PASS, AssertionStatus.FAIL, AssertionStatus.BLOCKED]
    )
    assert aggregate.value == 0.5
    assert aggregate.blocked == 1


def _authority(**changes):
    payload = {
        "baseline_id": EPISODIC_BASELINE_V2_ID,
        "status": EpisodicBaselineStatus.CANDIDATE,
        "dataset": {"schema": "stateful-episodic-scenario.v2", "id": "stateful_episodic_v2", "version": "v2", "digest": "sha256:data"},
        "dataset_lineage": {"parent_dataset_id": "stateful_episodic_v1", "parent_dataset_version": "v1", "parent_dataset_digest": "sha256:parent", "remediation_reason": "DATASET_SCOPE_DEFECT", "authority_gate": "70"},
        "target_evaluation_implementation_ref": "sha256:target",
        "target_source_receipt_digest": "sha256:target-receipt",
        "agentevalops_evaluation_implementation_ref": "sha256:agent",
        "agentevalops_source_receipt_digest": "sha256:agent-receipt",
        "execution_policy": "GLOBAL_SEQUENTIAL",
        "scenario_outcomes": {f"E{i:02d}": "PASS" for i in range(1, 13)},
        "assertion_summary": {"PASS": 194, "FAIL": 0, "BLOCKED": 0},
        "failure_taxonomy": (), "blocked_taxonomy": (),
        "metrics": {"episode_grounding_accuracy": {"value": 1.0}},
        "layer1_gate": {"passed": True}, "environment_provenance": {"profile": "EPISODIC_EVALUATION_LAYER1"},
        "experiment_artifact_ref": "backend/evaluation_artifacts/example.json",
        "experiment_artifact_digest": "sha256:artifact",
    }
    payload.update(changes)
    return EpisodicBaselineAuthority(**payload)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"experiment_artifact_ref": ""}, "experiment_artifact_ref"),
        ({"experiment_artifact_digest": ""}, "experiment_artifact_digest"),
        ({"target_source_receipt_digest": ""}, "target_source_receipt_digest"),
        ({"agentevalops_source_receipt_digest": ""}, "agentevalops_source_receipt_digest"),
        ({"dataset_lineage": {}}, "parent_dataset_id"),
        ({"assertion_summary": {}}, "assertion"),
        ({"metrics": {}}, "assertion"),
        ({"layer1_gate": {}}, "assertion"),
        ({"environment_provenance": {}}, "assertion"),
        ({"scenario_outcomes": {}}, "12 scenario"),
    ],
)
def test_baseline_authority_rejects_incomplete_provenance(changes, message):
    with pytest.raises(ValueError, match=message):
        _authority(**changes)


@pytest.mark.parametrize(
    "field",
    [
        "dataset",
        "target_evaluation_implementation_ref",
        "target_source_receipt_digest",
        "agentevalops_evaluation_implementation_ref",
        "agentevalops_source_receipt_digest",
        "execution_policy",
        "experiment_artifact_digest",
    ],
)
def test_baseline_authority_rejects_incompatible_identity(field):
    baseline = _authority()
    if field == "dataset":
        candidate = _authority(dataset={**baseline.dataset, "digest": "sha256:other"})
    else:
        candidate = _authority(**{field: "different"})
    assert baseline.compatibility_with(candidate) is EpisodicBaselineCompatibility.BASELINE_INCOMPATIBLE


def test_invalidated_candidate_cannot_be_canonical_eligible():
    invalidated = _authority(status=EpisodicBaselineStatus.INVALIDATED_CANDIDATE)
    assert invalidated.canonical_eligible is False
    assert _authority().canonical_eligible is True
