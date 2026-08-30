"""Stateful Memory baseline：compatible / incompatible / deltas。"""

# ruff: noqa: D101, D105, D415

import pytest

from app.core.evaluation.stateful_artifact import StatefulScenarioAggregateV1
from app.core.evaluation.stateful_baseline import (
    BaselineCompatibility,
    OutcomeDelta,
    compare_stateful_baseline,
)


def artifact(
    scenario_id,
    *,
    dataset_digest="sha256:abc",
    dataset_version="v1",
    target_version_ref=None,
    config_ref=None,
    scenario_outcome="PASS",
    assertions=None,
    metrics=None,
    evaluation_implementation_ref=None,
):
    return StatefulScenarioAggregateV1(
        evaluation_run_id="run-1",
        dataset_id="stateful_memory_v1",
        dataset_version=dataset_version,
        dataset_digest=dataset_digest,
        target_id="localagent",
        target_kind="LOCALAGENT_HTTP",
        target_version_ref=target_version_ref or {"kind": "t", "opaque_value": "v2"},
        config_ref=config_ref or {"kind": "c", "opaque_value": "cfg"},
        scenario_id=scenario_id,
        truthfulness_origin="DETERMINISTIC_GROUND_TRUTH",
        regression_tags=[],
        required=True,
        deterministic_denominator=True,
        initial_state={"kind": "EMPTY"},
        step_attempts=[],
        runtime_evidence_refs=[],
        snapshot_refs=[],
        expected_state=[],
        actual_state=[],
        state_diff=[],
        assertion_results=assertions or [],
        metric_aggregates=metrics or {},
        failure_taxonomies=[],
        scenario_outcome=scenario_outcome,
        scenario_outcome_assertion={"status": scenario_outcome, "assertion_id": "outcome"},
        evaluation_implementation_ref=evaluation_implementation_ref,
        private_evaluation_artifact=True,
    )


def test_baseline_compatible_comparison_reports_deltas():
    baseline = [
        artifact(
            "scn_a",
            scenario_outcome="PASS",
            assertions=[
                {"assertion_id": "scn_a.r1.formation", "status": "PASS"},
            ],
        ),
        artifact(
            "scn_b",
            scenario_outcome="FAIL",
            assertions=[{"assertion_id": "scn_b.final_state", "status": "FAIL"}],
        ),
    ]
    candidate = [
        artifact(
            "scn_a",
            scenario_outcome="FAIL",
            assertions=[{"assertion_id": "scn_a.r1.formation", "status": "FAIL"}],
        ),
        artifact(
            "scn_b",
            scenario_outcome="PASS",
            assertions=[{"assertion_id": "scn_b.final_state", "status": "PASS"}],
        ),
    ]
    comparison = compare_stateful_baseline(baseline, candidate)
    assert comparison.compatibility is BaselineCompatibility.COMPATIBLE
    by_id = {item.scenario_id: item for item in comparison.comparisons}
    assert by_id["scn_a"].outcome_delta is OutcomeDelta.NEW_FAILURE
    assert by_id["scn_b"].outcome_delta is OutcomeDelta.FIXED_FAILURE
    assert comparison.new_failures == ("scn_a.r1.formation",)
    assert comparison.fixed_failures == ("scn_b.final_state",)


def test_baseline_persistent_failures():
    baseline = [
        artifact(
            "scn_a",
            scenario_outcome="FAIL",
            assertions=[{"assertion_id": "scn_a.invariant", "status": "FAIL"}],
        )
    ]
    candidate = [
        artifact(
            "scn_a",
            scenario_outcome="FAIL",
            assertions=[{"assertion_id": "scn_a.invariant", "status": "FAIL"}],
        )
    ]
    comparison = compare_stateful_baseline(baseline, candidate)
    assert comparison.persistent_failures == ("scn_a.invariant",)
    assert comparison.new_failures == ()
    assert comparison.fixed_failures == ()


def test_baseline_blocked_deltas():
    baseline = [
        artifact(
            "scn_a",
            scenario_outcome="BLOCKED",
            assertions=[{"assertion_id": "scn_a.retrieval", "status": "BLOCKED"}],
        )
    ]
    candidate = [
        artifact(
            "scn_a",
            scenario_outcome="PASS",
            assertions=[{"assertion_id": "scn_a.retrieval", "status": "PASS"}],
        )
    ]
    comparison = compare_stateful_baseline(baseline, candidate)
    assert comparison.resolved_blocked == ("scn_a.retrieval",)
    assert comparison.new_blocked == ()
    assert comparison.comparisons[0].outcome_delta is OutcomeDelta.RESOLVED_BLOCKED


def test_baseline_metric_deltas():
    baseline = [
        artifact(
            "scn_a",
            scenario_outcome="PASS",
            metrics={
                "formation_decision_recall_remember": {
                    "passed": 1,
                    "failed": 0,
                    "blocked": 0,
                    "not_applicable": 0,
                    "evaluable_denominator": 1,
                    "value": 1.0,
                }
            },
        )
    ]
    candidate = [
        artifact(
            "scn_a",
            scenario_outcome="PASS",
            metrics={
                "formation_decision_recall_remember": {
                    "passed": 0,
                    "failed": 1,
                    "blocked": 0,
                    "not_applicable": 0,
                    "evaluable_denominator": 1,
                    "value": 0.0,
                }
            },
        )
    ]
    comparison = compare_stateful_baseline(baseline, candidate)
    delta = comparison.comparisons[0].metric_deltas["formation_decision_recall_remember"]
    assert delta == pytest.approx(-1.0)


def test_baseline_incompatible_rejected_no_numeric_delta():
    baseline = [artifact("scn_a", dataset_version="v1")]
    candidate = [artifact("scn_a", dataset_version="v2")]
    comparison = compare_stateful_baseline(baseline, candidate)
    assert comparison.compatibility is BaselineCompatibility.BASELINE_INCOMPATIBLE
    assert comparison.incompatibility_reason is not None
    assert comparison.comparisons == ()


def test_baseline_incompatible_on_target_contract_change():
    baseline = [artifact("scn_a", target_version_ref={"kind": "t", "opaque_value": "v2"})]
    candidate = [artifact("scn_a", target_version_ref={"kind": "t", "opaque_value": "v3"})]
    comparison = compare_stateful_baseline(baseline, candidate)
    assert comparison.compatibility is BaselineCompatibility.BASELINE_INCOMPATIBLE


def test_baseline_incompatible_when_one_bundle_artifact_has_different_provenance():
    baseline = [artifact("scn_a"), artifact("scn_b", dataset_digest="sha256:different")]
    candidate = [artifact("scn_a"), artifact("scn_b")]
    comparison = compare_stateful_baseline(baseline, candidate)
    assert comparison.compatibility is BaselineCompatibility.BASELINE_INCOMPATIBLE


# E0-v2 real finding: dataset agent alignment changed the dataset digest, so any
# artifact produced against the OLD digest must be BASELINE_INCOMPATIBLE with the
# new dataset, and no numeric delta may be produced.
PRE_AGENT_ALIGNMENT_DIGEST = "sha256:fe1de338541f6888107985d9c4d7c82c6756a0867c4ca7dc98ab770c3124fdc1"
POST_AGENT_ALIGNMENT_DIGEST = "sha256:b9fdd0dc40b3cd1febf4fdcaa0441bb6cb8dbecc5855fa4d65adab503110da1f"


def test_baseline_old_digest_artifact_vs_new_dataset_rejected():
    old_artifact = artifact("scn_a", dataset_digest=PRE_AGENT_ALIGNMENT_DIGEST)
    new_artifact = artifact("scn_a", dataset_digest=POST_AGENT_ALIGNMENT_DIGEST)
    comparison = compare_stateful_baseline([old_artifact], [new_artifact])
    assert comparison.compatibility is BaselineCompatibility.BASELINE_INCOMPATIBLE
    assert comparison.comparisons == ()
    assert comparison.incompatibility_reason is not None
    assert comparison.comparisons == ()


def test_baseline_incompatible_when_implementation_ref_differs():
    baseline = [artifact("scn_a", evaluation_implementation_ref="HEAD_A:sha256:aaaa")]
    candidate = [artifact("scn_a", evaluation_implementation_ref="HEAD_B:sha256:bbbb")]
    comparison = compare_stateful_baseline(baseline, candidate)
    assert comparison.compatibility is BaselineCompatibility.BASELINE_INCOMPATIBLE
    assert comparison.comparisons == ()


def test_baseline_incompatible_when_implementation_ref_absent():
    baseline = [artifact("scn_a")]
    candidate = [artifact("scn_a", evaluation_implementation_ref="HEAD:sha256:abc")]
    comparison = compare_stateful_baseline(baseline, candidate)
    assert comparison.compatibility is BaselineCompatibility.BASELINE_INCOMPATIBLE


def test_baseline_empty_lists_rejected():
    with pytest.raises(ValueError):
        compare_stateful_baseline([], [])


# ------------------------------------------------------------------ E1-R3 V1/V2 baseline


def test_v1_v2_dataset_baseline_incompatible_no_numeric_delta():
    baseline = [
        artifact(
            "scn_a",
            dataset_digest="sha256:b9fdd0dc40b3cd1febf4fdcaa0441bb6cb8dbecc5855fa4d65adab503110da1f",
            dataset_version="v1",
            metrics={"formation_decision_recall_remember": {"value": 1.0, "evaluable_denominator": 1}},
        )
    ]
    candidate = [
        artifact(
            "scn_a",
            dataset_digest="sha256:9538c08c85573c7d19caac0b5beb8187a27fa2b317d0cf08a1770a6952726e12",
            dataset_version="v2",
            metrics={"formation_decision_recall_remember": {"value": 0.5, "evaluable_denominator": 1}},
        )
    ]
    comparison = compare_stateful_baseline(baseline, candidate)
    assert comparison.compatibility is BaselineCompatibility.BASELINE_INCOMPATIBLE
    assert comparison.incompatibility_reason is not None
    assert comparison.comparisons == ()
    assert comparison.new_failures == ()
    assert comparison.fixed_failures == ()


def test_v1_v2_actual_dataset_artifacts_baseline_incompatible():
    from app.core.evaluation.stateful_assertion import AssertionStatus
    from app.core.evaluation.stateful_memory_dataset import load_stateful_memory_dataset
    from app.core.evaluation.stateful_memory_dataset_v2 import load_stateful_memory_dataset_v2

    v1 = load_stateful_memory_dataset("evaluation_assets/stateful_memory_v1/stateful_memory_dataset.v1.json")
    v2 = load_stateful_memory_dataset_v2("evaluation_assets/stateful_memory_v2/stateful_memory_dataset.v2.json")
    baseline = [
        artifact(
            "retrieval_active_hit",
            dataset_digest=v1.content_digest,
            dataset_version=v1.version,
            scenario_outcome=AssertionStatus.FAIL.value,
        )
    ]
    candidate = [
        artifact(
            "retrieval_active_hit",
            dataset_digest=v2.content_digest,
            dataset_version=v2.version,
            scenario_outcome=AssertionStatus.PASS.value,
        )
    ]
    comparison = compare_stateful_baseline(baseline, candidate)
    assert comparison.compatibility is BaselineCompatibility.BASELINE_INCOMPATIBLE
    assert comparison.comparisons == ()
