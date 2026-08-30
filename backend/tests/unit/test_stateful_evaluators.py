"""Stateful Memory evaluators：formation/predicate/lifecycle/final-state/invariant/retrieval/ranking/injection/leakage/generation + denominator 契约。"""

# ruff: noqa: D101, D105, D415

from datetime import UTC, datetime

import pytest

from app.core.evaluation.stateful_assertion import (
    AssertionDimension,
    AssertionStatus,
    BlockReason,
    FailureTaxonomy,
)
from app.core.evaluation.stateful_evaluators import (
    FORGOTTEN_LEAKAGE_RATE_METRIC,
    SCOPE_LEAKAGE_RATE_METRIC,
    RetrievalEvidenceSource,
    RetrievalSelectionEvidence,
    ScenarioEvaluationEvidence,
    build_alias_binding,
    evaluate_scenario,
)
from app.core.evaluation.stateful_journal import (
    FormationEvent,
    JournalEvents,
    LifecycleEvent,
    RetrievalEvent,
)
from app.core.evaluation.stateful_memory_dataset import (
    FormationDecision,
    LifecycleOperation,
    PredicateClassification,
    StatefulMemoryScenario,
)
from app.core.evaluation.stateful_projection import (
    CanonicalMemoryRecord,
    MemoryStateSnapshot,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def record(
    memory_id,
    *,
    status="ACTIVE",
    logical_key=None,
    value="SQLite",
    origin_run_id="run-1",
    superseded_by_memory_id=None,
    created_at="2026-08-01T00:00:00+00:00",
    updated_at="2026-08-01T00:00:00+00:00",
    agent_id="core_router",
    memory_scope="direct",
    canonical_text=None,
):
    if status == "FORGOTTEN":
        canonical_text = canonical_text or "[FORGOTTEN]"
        payload: dict[str, object] = {}
        value = {}
    else:
        payload = {"value": value}
        canonical_text = canonical_text or f"{logical_key}: {value}"
    return CanonicalMemoryRecord(
        memory_id=memory_id,
        agent_id=agent_id,
        memory_scope=memory_scope,
        memory_type="SEMANTIC",
        logical_key=logical_key,
        status=status,
        canonical_text=canonical_text,
        payload=payload,
        canonical_value=value,
        created_at=created_at,
        updated_at=updated_at,
        superseded_by_memory_id=superseded_by_memory_id,
        origin_run_id=origin_run_id,
        formation_method="llm",
    )


def snapshot(records):
    return MemoryStateSnapshot(snapshot_id="snap", db_path="isolated://memory.db", captured_at=NOW, records=records)


def formation(run_id, *, persisted=1, accepted=1, ignored=0, reused=0, outcomes="1|ACCEPTED|mem-1"):
    return FormationEvent(
        run_id, f"ev-{run_id}-f", "llm", "OK", None, 1, accepted, ignored, persisted, reused, 0, outcomes
    )


def lifecycle(run_id, op, *, outcome="OK", winner=None, new=None):
    return LifecycleEvent(run_id, f"ev-{run_id}-l", "SEMANTIC", op, outcome, None, 1, winner, new, "ACCEPTED", "NONE")


def retrieval(
    run_id, *, selected_count=1, context_record_count=1, planning_injected=True, direct_entry_supplied=False
):
    return RetrievalEvent(
        run_id,
        f"ev-{run_id}-r",
        "lexical",
        "deterministic",
        "COMPLETE",
        None,
        1,
        selected_count,
        selected_count,
        context_record_count,
        0,
        0,
        selected_count,
        0,
        planning_injected,
        direct_entry_supplied,
    )


def journal(run_id, *, formation_events=(), lifecycle_events=(), retrieval_events=()):
    return JournalEvents(run_id, formation_events, lifecycle_events, retrieval_events)


def selection(
    step_id, run_id, *, selected_ids, context_record_count=1, planning_injected=True, direct_entry_supplied=False
):
    return RetrievalSelectionEvidence(
        step_id=step_id,
        run_id=run_id,
        retrieval_status="COMPLETE",
        selected_count=len(selected_ids),
        context_record_count=context_record_count,
        planning_injected=planning_injected,
        direct_entry_supplied=direct_entry_supplied,
        registered_selected_count=len(selected_ids),
        open_selected_count=0,
        selected_memory_ids=selected_ids,
    )


def evidence(scenario, **kwargs):
    defaults = {
        "scenario": scenario,
        "journal_by_step": {},
        "snapshots_by_step": {},
        "final_snapshot": None,
        "outcome_kind_by_step": {},
        "run_id_by_step": {},
        "selection_by_step": {},
        "alias_binding": {},
    }
    defaults.update(kwargs)
    return ScenarioEvaluationEvidence(**defaults)


def scenario(**overrides):
    data = {
        "scenario_id": "scn",
        "description": "t",
        "truthfulness_origin": "DETERMINISTIC_GROUND_TRUTH",
        "tags": [],
        "initial_state": {"kind": "EMPTY"},
        "steps": [],
        "expected_state": [],
    }
    data.update(overrides)
    return StatefulMemoryScenario.model_validate(data)


def step(**overrides):
    data = {
        "step_id": "r1",
        "agent_id": "core_router",
        "memory_scope": "direct",
        "query": "q",
    }
    data.update(overrides)
    from app.core.evaluation.stateful_memory_dataset import StatefulMemoryStep

    return StatefulMemoryStep.model_validate(data)


def _assertion(ev, suffix):
    return next(a for a in ev.assertions if a.assertion_id.endswith(suffix))


# ---------------------------------------------------------------- formation


def test_formation_remember_precision_recall():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            ),
            step(
                step_id="r2",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            ),
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal("run-1", formation_events=(formation("run-1"),)),
            "r2": journal("run-2", formation_events=(formation("run-2"),)),
        },
        outcome_kind_by_step={"r1": "SUCCESS", "r2": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    assert result.metrics["formation_decision_precision_remember"].value == 1.0
    assert result.metrics["formation_decision_recall_remember"].value == 1.0


def test_formation_ignore_metrics_and_false_negative():
    scn = scenario(
        steps=[
            step(step_id="r1", expected_formation={"decision": "IGNORE"}),
            step(
                step_id="r2",
                expected_formation={"decision": "REMEMBER", "predicate": {"classification": "OPEN"}},
                expected_lifecycle="INSERT",
            ),
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal(
                "run-1",
                formation_events=(
                    formation("run-1", persisted=0, accepted=0, ignored=1, outcomes="1|IGNORED|POLICY"),
                ),
            ),
            "r2": journal(
                "run-2",
                formation_events=(
                    formation("run-2", persisted=0, accepted=0, ignored=1, outcomes="1|IGNORED|POLICY"),
                ),
            ),
        },
        outcome_kind_by_step={"r1": "SUCCESS", "r2": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    # IGNORE precision = correct IGNORE / actual IGNORE decisions. r2 was actually
    # IGNOREd while expected REMEMBER -> it is a false positive for the IGNORE class.
    assert result.metrics["formation_decision_precision_ignore"].value == 0.5
    assert result.metrics["formation_decision_recall_ignore"].value == 1.0
    assert result.metrics["formation_decision_recall_remember"].value == 0.0
    fail = next(a for a in result.assertions if a.assertion_id.endswith("r2.formation"))
    assert fail.failure_taxonomy is FailureTaxonomy.FORMATION_FALSE_NEGATIVE


def test_formation_runtime_blocked_is_not_false_negative():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            )
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={"r1": journal("run-1")},
        outcome_kind_by_step={"r1": "FAILURE"},
    )
    result = evaluate_scenario(ev)
    formation_assertion = next(a for a in result.assertions if a.assertion_id.endswith("r1.formation"))
    assert formation_assertion.status is AssertionStatus.BLOCKED
    assert formation_assertion.blocked_by is BlockReason.RUNTIME
    assert result.metrics["formation_decision_recall_remember"].value is None
    assert result.metrics["formation_decision_recall_remember"].evaluable_denominator == 0


# ---------------------------------------------------------------- predicate


def test_predicate_registered_classification_and_id():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            ),
            step(
                step_id="r2",
                expected_formation={"decision": "REMEMBER", "predicate": {"classification": "OPEN"}},
                expected_lifecycle="INSERT",
            ),
        ]
    )
    post1 = snapshot((record("mem-1", logical_key="project.database", origin_run_id="run-1"),))
    post2 = snapshot((record("mem-2", logical_key=None, value="Nebula", origin_run_id="run-2"),))
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal("run-1", formation_events=(formation("run-1"),)),
            "r2": journal("run-2", formation_events=(formation("run-2"),)),
        },
        snapshots_by_step={"r1": (snapshot(()), post1), "r2": (snapshot(()), post2)},
        run_id_by_step={"r1": "run-1", "r2": "run-2"},
        outcome_kind_by_step={"r1": "SUCCESS", "r2": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    assert result.metrics["registered_vs_open_accuracy"].value == 1.0
    assert result.metrics["registered_predicate_id_accuracy"].value == 1.0


def test_predicate_id_drift_is_classification_error():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            )
        ]
    )
    post1 = snapshot((record("mem-1", logical_key="project_database", origin_run_id="run-1"),))
    ev = evidence(
        scn,
        journal_by_step={"r1": journal("run-1", formation_events=(formation("run-1"),))},
        snapshots_by_step={"r1": (snapshot(()), post1)},
        run_id_by_step={"r1": "run-1"},
        outcome_kind_by_step={"r1": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    fail = next(a for a in result.assertions if a.assertion_id.endswith("r1.predicate"))
    assert fail.status is AssertionStatus.FAIL
    assert fail.failure_taxonomy is FailureTaxonomy.PREDICATE_CLASSIFICATION_ERROR


def test_predicate_blocked_when_formation_did_not_occur():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            )
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal("run-1", formation_events=(formation("run-1", persisted=0, accepted=0, ignored=1),))
        },
        outcome_kind_by_step={"r1": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    predicate_assertion = next(a for a in result.assertions if a.assertion_id.endswith("r1.predicate"))
    assert predicate_assertion.status is AssertionStatus.BLOCKED
    assert predicate_assertion.blocked_by is BlockReason.PREREQUISITE


# ---------------------------------------------------------------- lifecycle


@pytest.mark.parametrize(
    ("expected_op", "actual_op", "actual_outcome", "should_pass"),
    [
        ("INSERT", "INSERT", "OK", True),
        ("NO_CHANGE", "NO_CHANGE", "OK", True),
        ("SUPERSEDE", "SUPERSEDE", "OK", True),
        ("FORGET", "FORGET", "OK", True),
        ("INSERT", "NO_CHANGE", "OK", False),
        ("FORGET", "FORGET", "NOT_FOUND", False),
    ],
)
def test_lifecycle_exact_operations(expected_op, actual_op, actual_outcome, should_pass):
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle=expected_op,
            )
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal(
                "run-1",
                formation_events=(formation("run-1"),),
                lifecycle_events=(lifecycle("run-1", actual_op, outcome=actual_outcome),),
            )
        },
        outcome_kind_by_step={"r1": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    assertion = _assertion(result, "r1.lifecycle")
    if should_pass:
        assert assertion.status is AssertionStatus.PASS
    else:
        assert assertion.status is AssertionStatus.FAIL
        assert assertion.failure_taxonomy is FailureTaxonomy.LIFECYCLE_OPERATION_MISMATCH


def test_lifecycle_expected_outcomes_not_found_and_already_forgotten():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="NOT_FOUND",
            ),
            step(
                step_id="r2",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="ALREADY_FORGOTTEN",
            ),
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal(
                "run-1",
                formation_events=(formation("run-1"),),
                lifecycle_events=(lifecycle("run-1", "FORGET", outcome="NOT_FOUND"),),
            ),
            "r2": journal(
                "run-2",
                formation_events=(formation("run-2"),),
                lifecycle_events=(lifecycle("run-2", "FORGET", outcome="ALREADY_FORGOTTEN"),),
            ),
        },
        outcome_kind_by_step={"r1": "SUCCESS", "r2": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    assert _assertion(result, "r1.lifecycle").status is AssertionStatus.PASS
    assert _assertion(result, "r2.lifecycle").status is AssertionStatus.PASS


def test_lifecycle_policy_ignored_passes_when_no_lifecycle_event():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={"decision": "IGNORE"},
                expected_lifecycle="POLICY_IGNORED",
                required=False,
            )
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal(
                "run-1",
                formation_events=(
                    formation("run-1", persisted=0, accepted=0, ignored=1, outcomes="1|IGNORED|POLICY"),
                ),
            )
        },
        outcome_kind_by_step={"r1": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    assert _assertion(result, "r1.lifecycle").status is AssertionStatus.PASS


# ---------------------------------------------------------------- final state


def test_final_state_exact_and_extra_active_row():
    scn = scenario(
        steps=[step(step_id="r1")],
        expected_state=[
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
    good = evidence(scn, final_snapshot=snapshot((record("mem-1", logical_key="project.database"),)))
    result = evaluate_scenario(good)
    assert _assertion(result, "final_state").status is AssertionStatus.PASS

    bad = evidence(
        scn,
        final_snapshot=snapshot(
            (
                record("mem-1", logical_key="project.database"),
                record("mem-2", logical_key="project.package_manager", value="uv"),
            )
        ),
    )
    result = evaluate_scenario(bad)
    final = _assertion(result, "final_state")
    assert final.status is AssertionStatus.FAIL
    assert final.failure_taxonomy is FailureTaxonomy.FINAL_STATE_MISMATCH


# ---------------------------------------------------------------- invariant


def test_no_change_invariant_keeps_winner():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="NO_CHANGE",
            )
        ]
    )
    pre = snapshot((record("mem-1", logical_key="project.database"),))
    post = snapshot((record("mem-1", logical_key="project.database"),))
    ev = evidence(scn, snapshots_by_step={"r1": (pre, post)}, final_snapshot=post)
    result = evaluate_scenario(ev)
    assert _assertion(result, "no_change_no_new_row").status is AssertionStatus.PASS
    assert _assertion(result, "no_change_keeps_winner").status is AssertionStatus.PASS


def test_no_change_new_row_invariant_violation():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="NO_CHANGE",
            )
        ]
    )
    pre = snapshot((record("mem-1", logical_key="project.database"),))
    post = snapshot(
        (
            record("mem-1", logical_key="project.database"),
            record("mem-2", logical_key="project.database", value="SQLite"),
        )
    )
    ev = evidence(scn, snapshots_by_step={"r1": (pre, post)}, final_snapshot=post)
    result = evaluate_scenario(ev)
    violation = _assertion(result, "no_change_no_new_row")
    assert violation.status is AssertionStatus.FAIL
    assert violation.failure_taxonomy is FailureTaxonomy.INVARIANT_VIOLATION


def test_supersede_direct_to_latest_invariant():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="SUPERSEDE",
            )
        ]
    )
    good_post = snapshot(
        (
            record("mem-old", status="SUPERSEDED", logical_key="project.database", superseded_by_memory_id="mem-new"),
            record("mem-new", logical_key="project.database", value="PostgreSQL"),
        )
    )
    result = evaluate_scenario(
        evidence(scn, snapshots_by_step={"r1": (snapshot(()), good_post)}, final_snapshot=good_post)
    )
    assert _assertion(result, "supersede_direct_to_latest").status is AssertionStatus.PASS

    bad_post = snapshot(
        (
            record(
                "mem-old", status="SUPERSEDED", logical_key="project.database", superseded_by_memory_id="mem-other"
            ),
            record("mem-new", logical_key="project.database", value="PostgreSQL"),
        )
    )
    result = evaluate_scenario(
        evidence(scn, snapshots_by_step={"r1": (snapshot(()), bad_post)}, final_snapshot=bad_post)
    )
    violation = _assertion(result, "supersede_direct_to_latest")
    assert violation.status is AssertionStatus.FAIL


def test_forget_redaction_invariant():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="FORGET",
            )
        ]
    )
    post = snapshot((record("mem-1", status="FORGOTTEN", logical_key="project.database"),))
    result = evaluate_scenario(evidence(scn, snapshots_by_step={"r1": (snapshot(()), post)}, final_snapshot=post))
    assert _assertion(result, "forget_redaction").status is AssertionStatus.PASS


def test_keyed_active_invariant_violation_detected():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            )
        ]
    )
    post = snapshot(
        (
            record("mem-1", logical_key="project.database"),
            record("mem-2", logical_key="project.database", value="SQLite"),
        )
    )
    result = evaluate_scenario(evidence(scn, snapshots_by_step={"r1": (snapshot(()), post)}, final_snapshot=post))
    violation = _assertion(result, "keyed_active_le_1")
    assert violation.status is AssertionStatus.FAIL
    assert violation.failure_taxonomy is FailureTaxonomy.INVARIANT_VIOLATION


# ---------------------------------------------------------------- retrieval


def test_retrieval_recall_hit_rejection_with_selection_ids():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_retrieval={"expected_selected": ["db"], "expected_excluded": ["forgone"], "k": 5},
            )
        ]
    )
    ev = evidence(
        scn,
        selection_by_step={"r1": selection("r1", "run-1", selected_ids=("mem-db",))},
        alias_binding={"db": "mem-db", "forgone": "mem-forgotten"},
    )
    result = evaluate_scenario(ev)
    assert _assertion(result, "recall_at_k").status is AssertionStatus.PASS
    assert _assertion(result, "hit_at_k").status is AssertionStatus.PASS
    assert _assertion(result, "rejection").status is AssertionStatus.PASS


def test_retrieval_miss_fails():
    scn = scenario(
        steps=[step(step_id="r1", expected_retrieval={"expected_selected": ["db"], "expected_excluded": [], "k": 5})]
    )
    ev = evidence(
        scn,
        selection_by_step={"r1": selection("r1", "run-1", selected_ids=())},
        alias_binding={"db": "mem-db"},
    )
    result = evaluate_scenario(ev)
    miss = _assertion(result, "recall_at_k")
    assert miss.status is AssertionStatus.FAIL
    assert miss.failure_taxonomy is FailureTaxonomy.RETRIEVAL_MISS


def test_retrieval_irrelevant_rejection_fails_on_excluded_selection():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_retrieval={"expected_selected": ["db"], "expected_excluded": ["unrelated"], "k": 5},
            )
        ]
    )
    ev = evidence(
        scn,
        selection_by_step={"r1": selection("r1", "run-1", selected_ids=("mem-db", "mem-unrelated"))},
        alias_binding={"db": "mem-db", "unrelated": "mem-unrelated"},
    )
    result = evaluate_scenario(ev)
    rejection = _assertion(result, "rejection")
    assert rejection.status is AssertionStatus.FAIL
    assert rejection.failure_taxonomy is FailureTaxonomy.IRRELEVANT_RETRIEVAL


def test_retrieval_counts_only_evidence_blocks_identity_assertions():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_retrieval={"expected_selected": ["db"], "expected_excluded": ["forgone"], "k": 5},
            )
        ]
    )
    ev = evidence(
        scn,
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
                selected_memory_ids=None,
            )
        },
        alias_binding={"db": "mem-db", "forgone": "mem-forgotten"},
    )
    result = evaluate_scenario(ev)
    assert _assertion(result, "selected_count").status is AssertionStatus.PASS
    recall = _assertion(result, "recall_at_k")
    assert recall.status is AssertionStatus.BLOCKED
    assert recall.blocked_by is BlockReason.NOT_SUPPORTED_BY_CURRENT_EVIDENCE
    assert _assertion(result, "rejection").status is AssertionStatus.BLOCKED


# ---------------------------------------------------------------- ranking


def test_ranking_single_relevant_no_ndcg():
    scn = scenario(
        steps=[
            step(
                step_id="r1", expected_retrieval={"expected_selected": ["db"], "expected_ranked_order": ["db"], "k": 5}
            )
        ]
    )
    ev = evidence(
        scn,
        selection_by_step={"r1": selection("r1", "run-1", selected_ids=("mem-db",))},
        alias_binding={"db": "mem-db"},
    )
    result = evaluate_scenario(ev)
    ranking = next(a for a in result.assertions if a.dimension is AssertionDimension.RANKING)
    assert ranking.status is AssertionStatus.NOT_APPLICABLE
    assert not any(a.assertion_id.endswith("ndcg_at_k") for a in result.assertions)


def test_ranking_multi_ordered_mrr_ndcg():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_retrieval={
                    "expected_selected": ["db", "pm"],
                    "expected_ranked_order": ["db", "pm"],
                    "k": 5,
                },
            )
        ]
    )
    ev = evidence(
        scn,
        selection_by_step={"r1": selection("r1", "run-1", selected_ids=("mem-db", "mem-pm"))},
        alias_binding={"db": "mem-db", "pm": "mem-pm"},
    )
    result = evaluate_scenario(ev)
    assert _assertion(result, "mrr").status is AssertionStatus.PASS
    assert _assertion(result, "ndcg_at_k").status is AssertionStatus.PASS


# ---------------------------------------------------------------- injection


def test_injection_pass_requires_planning_injected_and_count():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_retrieval={"expected_selected": ["db"], "expected_excluded": [], "k": 5},
                expected_injection={"planner_context_record_count": 1},
            )
        ]
    )
    ev = evidence(
        scn,
        selection_by_step={
            "r1": selection("r1", "run-1", selected_ids=("mem-db",), context_record_count=1, planning_injected=True)
        },
        alias_binding={"db": "mem-db"},
    )
    result = evaluate_scenario(ev)
    assert _assertion(result, "injection").status is AssertionStatus.PASS


def test_injection_fails_when_planning_not_injected():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_retrieval={"expected_selected": ["db"], "expected_excluded": [], "k": 5},
                expected_injection={"planner_context_record_count": 1},
            )
        ]
    )
    ev = evidence(
        scn,
        selection_by_step={"r1": selection("r1", "run-1", selected_ids=("mem-db",), planning_injected=False)},
        alias_binding={"db": "mem-db"},
    )
    result = evaluate_scenario(ev)
    injection = _assertion(result, "injection")
    assert injection.status is AssertionStatus.FAIL
    assert injection.failure_taxonomy is FailureTaxonomy.CONTEXT_INJECTION_MISS


def test_direct_entry_supplied_is_not_injection_pass():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_retrieval={"expected_selected": ["db"], "expected_excluded": [], "k": 5},
                expected_injection={"planning_injected_expected": False},
            )
        ]
    )
    ev = evidence(
        scn,
        selection_by_step={
            "r1": selection(
                "r1", "run-1", selected_ids=("mem-db",), planning_injected=False, direct_entry_supplied=True
            )
        },
        alias_binding={"db": "mem-db"},
    )
    result = evaluate_scenario(ev)
    injection = _assertion(result, "injection")
    assert injection.status is AssertionStatus.BLOCKED
    assert injection.blocked_by is BlockReason.NOT_SUPPORTED_BY_CURRENT_EVIDENCE


# ---------------------------------------------------------------- leakage


def test_forgotten_leakage_gate():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_retrieval={"expected_selected": ["db"], "expected_excluded": ["forgone"], "k": 5},
            )
        ]
    )
    good = evidence(
        scn,
        selection_by_step={"r1": selection("r1", "run-1", selected_ids=("mem-db",))},
        alias_binding={"db": "mem-db", "forgone": "mem-forgotten"},
        final_snapshot=snapshot(
            (
                record("mem-db", logical_key="project.database", value="PostgreSQL"),
                record("mem-forgotten", status="FORGOTTEN", logical_key="project.legacy_database"),
            )
        ),
    )
    result = evaluate_scenario(good)
    assert result.metrics[FORGOTTEN_LEAKAGE_RATE_METRIC].failed == 0

    leak = evidence(
        scn,
        selection_by_step={"r1": selection("r1", "run-1", selected_ids=("mem-db", "mem-forgotten"))},
        alias_binding={"db": "mem-db", "forgone": "mem-forgotten"},
        final_snapshot=snapshot(
            (
                record("mem-db", logical_key="project.database", value="PostgreSQL"),
                record("mem-forgotten", status="FORGOTTEN", logical_key="project.legacy_database"),
            )
        ),
    )
    result = evaluate_scenario(leak)
    leakage_assertion = next(a for a in result.assertions if a.assertion_id.endswith(".leakage.forgotten"))
    assert leakage_assertion.status is AssertionStatus.FAIL
    assert leakage_assertion.failure_taxonomy is FailureTaxonomy.FORGOTTEN_LEAKAGE
    assert result.metrics[FORGOTTEN_LEAKAGE_RATE_METRIC].failed == 1


def test_scope_leakage_gate():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                agent_id="core_router",
                memory_scope="direct",
                expected_retrieval={"expected_selected": ["db"], "expected_excluded": [], "k": 5},
            )
        ]
    )
    ev = evidence(
        scn,
        selection_by_step={"r1": selection("r1", "run-1", selected_ids=("mem-other",))},
        alias_binding={"db": "mem-db"},
        final_snapshot=snapshot((record("mem-other", logical_key=None, value="部署在裸金属", memory_scope="team"),)),
    )
    result = evaluate_scenario(ev)
    leakage_assertion = next(a for a in result.assertions if a.assertion_id.endswith(".leakage.scope"))
    assert leakage_assertion.status is AssertionStatus.FAIL
    assert leakage_assertion.failure_taxonomy is FailureTaxonomy.SCOPE_LEAKAGE
    assert result.metrics[SCOPE_LEAKAGE_RATE_METRIC].failed == 1


def test_leakage_not_counted_when_no_exposure():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_retrieval={"expected_selected": ["db"], "expected_excluded": [], "k": 5},
            )
        ]
    )
    ev = evidence(
        scn,
        selection_by_step={
            "r1": RetrievalSelectionEvidence(
                step_id="r1",
                run_id="run-1",
                retrieval_status="COMPLETE",
                selected_count=0,
                context_record_count=0,
                planning_injected=False,
                direct_entry_supplied=False,
                registered_selected_count=0,
                open_selected_count=0,
                selected_memory_ids=None,
            )
        },
        alias_binding={"db": "mem-db"},
        final_snapshot=snapshot((record("mem-db", logical_key="project.database"),)),
    )
    result = evaluate_scenario(ev)
    forgotten = next(a for a in result.assertions if a.assertion_id.endswith(".leakage.forgotten"))
    assert forgotten.status is AssertionStatus.NOT_APPLICABLE
    assert result.metrics[FORGOTTEN_LEAKAGE_RATE_METRIC].evaluable_denominator == 0


# ---------------------------------------------------------------- generation


def test_generation_exact_match():
    scn = scenario(
        steps=[step(step_id="r1")],
        generation_expectation={"kind": "EXACT", "expected_value": "PostgreSQL", "adjudication": "HUMAN_REVIEWED"},
    )
    good = evidence(scn, final_answer_text="PostgreSQL")
    result = evaluate_scenario(good)
    assert _assertion(result, "generation").status is AssertionStatus.PASS

    bad = evidence(scn, final_answer_text="MySQL")
    result = evaluate_scenario(bad)
    generation = _assertion(result, "generation")
    assert generation.status is AssertionStatus.FAIL
    assert generation.failure_taxonomy is FailureTaxonomy.GENERATION_USE_FAILURE


# ---------------------------------------------------------------- denominator / scenario outcome


def test_blocked_excluded_from_quality_denominator_but_rates_retained():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            ),
            step(
                step_id="r2",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            ),
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal("run-1", formation_events=(formation("run-1"),)),
            "r2": journal("run-2"),
        },
        outcome_kind_by_step={"r1": "SUCCESS", "r2": "FAILURE"},
    )
    result = evaluate_scenario(ev)
    recall = result.metrics["formation_decision_recall_remember"]
    assert recall.evaluable_denominator == 1
    assert recall.value == 1.0
    assert result.metrics["runtime_block_rate"].numerator >= 1
    assert result.runtime_block_rate.numerator >= 1
    # infra failures（evidence capture）是独立轨道；本条只验证 quality denominator 排除 BLOCKED
    assert result.metrics["evaluation_infra_failure_rate"].numerator >= 0


def test_evidence_capture_failure_is_infra_rate_not_runtime_rate():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            )
        ]
    )
    ev = evidence(scn, outcome_kind_by_step={"r1": "SUCCESS"}, journal_by_step={"r1": journal("run-1")})
    result = evaluate_scenario(ev)
    formation_assertion = next(a for a in result.assertions if a.assertion_id.endswith("r1.formation"))
    assert formation_assertion.blocked_by is BlockReason.EVIDENCE_CAPTURE
    assert result.runtime_block_rate.numerator == 0
    assert result.evaluation_infra_failure_rate.numerator >= 1


def test_scenario_outcome_required_fail_is_fail():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            )
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal("run-1", formation_events=(formation("run-1", persisted=0, accepted=0, ignored=1),))
        },
        outcome_kind_by_step={"r1": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    assert result.scenario_outcome is AssertionStatus.FAIL


def test_scenario_outcome_required_blocked_is_blocked():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            )
        ]
    )
    ev = evidence(scn, journal_by_step={"r1": journal("run-1")}, outcome_kind_by_step={"r1": "FAILURE"})
    result = evaluate_scenario(ev)
    assert result.scenario_outcome is AssertionStatus.BLOCKED


def test_optional_failure_does_not_block_scenario():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            )
        ],
        expected_state=[
            {
                "alias": "db",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "SQLite",
            }
        ],
        generation_expectation={"kind": "EXACT", "expected_value": "PostgreSQL", "adjudication": "HUMAN_REVIEWED"},
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal(
                "run-1",
                formation_events=(formation("run-1"),),
                lifecycle_events=(lifecycle("run-1", "INSERT"),),
            )
        },
        snapshots_by_step={"r1": (snapshot(()), snapshot((record("mem-1", logical_key="project.database"),)))},
        final_snapshot=snapshot((record("mem-1", logical_key="project.database"),)),
        run_id_by_step={"r1": "run-1"},
        outcome_kind_by_step={"r1": "SUCCESS"},
        final_answer_text="MySQL",
    )
    result = evaluate_scenario(ev)
    assert result.scenario_outcome is AssertionStatus.PASS
    generation = _assertion(result, "generation")
    assert generation.status is AssertionStatus.FAIL


def test_alias_binding_matches_canonical_identity():
    records = (
        record("mem-1", logical_key="project.database"),
        record("mem-2", status="SUPERSEDED", logical_key="project.database", superseded_by_memory_id="mem-1"),
    )
    expected = [
        {
            "alias": "db_new",
            "agent_id": "core_router",
            "memory_scope": "direct",
            "logical_key": "project.database",
            "status": "ACTIVE",
            "value": "SQLite",
        },
        {
            "alias": "db_old",
            "agent_id": "core_router",
            "memory_scope": "direct",
            "logical_key": "project.database",
            "status": "SUPERSEDED",
            "value": "SQLite",
            "superseded_by_alias": "db_new",
        },
    ]
    from app.core.evaluation.stateful_memory_dataset import MemoryRecordExpectation

    binding = build_alias_binding(
        [MemoryRecordExpectation.model_validate(item) for item in expected], [snapshot(records)]
    )
    assert binding == {"db_new": "mem-1", "db_old": "mem-2"}


# ------------------------------------------------------------------ E1-R2 formation semantics


def test_formation_expected_remember_accepted_zero_is_false_negative():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            )
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal("run-1", formation_events=(formation("run-1", persisted=0, accepted=0, outcomes="NONE"),))
        },
        outcome_kind_by_step={"r1": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    f = _assertion(result, "r1.formation")
    assert f.status is AssertionStatus.FAIL
    assert f.failure_taxonomy is FailureTaxonomy.FORMATION_FALSE_NEGATIVE


def test_formation_reused_count_is_remember():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="NO_CHANGE",
            )
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal(
                "run-1",
                formation_events=(
                    formation("run-1", persisted=0, accepted=1, reused=1, outcomes="0|REUSED|OK|mem-1"),
                ),
                lifecycle_events=(lifecycle("run-1", "NO_CHANGE"),),
            )
        },
        snapshots_by_step={"r1": (snapshot(()), snapshot((record("mem-1", logical_key="project.database"),)))},
        final_snapshot=snapshot((record("mem-1", logical_key="project.database"),)),
        run_id_by_step={"r1": "run-1"},
        outcome_kind_by_step={"r1": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    assert _assertion(result, "r1.formation").status is AssertionStatus.PASS


def test_formation_no_change_persisted_zero_is_valid_remember():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="NO_CHANGE",
            )
        ]
    )
    ev = evidence(
        scn,
        journal_by_step={
            "r1": journal(
                "run-1",
                formation_events=(
                    formation("run-1", persisted=0, accepted=1, outcomes="0|NO_CHANGE|NO_CHANGE|mem-1"),
                ),
                lifecycle_events=(lifecycle("run-1", "NO_CHANGE"),),
            )
        },
        snapshots_by_step={"r1": (snapshot(()), snapshot((record("mem-1", logical_key="project.database"),)))},
        final_snapshot=snapshot((record("mem-1", logical_key="project.database"),)),
        run_id_by_step={"r1": "run-1"},
        outcome_kind_by_step={"r1": "SUCCESS"},
    )
    result = evaluate_scenario(ev)
    assert _assertion(result, "r1.formation").status is AssertionStatus.PASS
    assert _assertion(result, "r1.lifecycle").status is AssertionStatus.PASS


def test_formation_runtime_stopped_before_formation_is_blocked_runtime():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                expected_formation={
                    "decision": "REMEMBER",
                    "predicate": {"classification": "REGISTERED", "predicate_id": "project.database"},
                },
                expected_lifecycle="INSERT",
            )
        ]
    )
    ev = evidence(scn, journal_by_step={"r1": journal("run-1")}, outcome_kind_by_step={"r1": "FAILURE"})
    result = evaluate_scenario(ev)
    f = _assertion(result, "r1.formation")
    assert f.status is AssertionStatus.BLOCKED
    assert f.blocked_by is BlockReason.RUNTIME


# ------------------------------------------------------------------ E1-R2 scope isolation


def test_scope_isolation_seeded_expected_foreign_scope_row_allowed():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                agent_id="core_router",
                memory_scope="direct",
                expected_formation=None,
                expected_lifecycle=None,
            )
        ],
        expected_state=[
            {
                "alias": "other",
                "agent_id": "core_router",
                "memory_scope": "team",
                "logical_key": None,
                "status": "ACTIVE",
                "value": "部署在裸金属",
            },
        ],
        initial_state={
            "kind": "SEEDED",
            "records": [
                {
                    "alias": "other",
                    "agent_id": "core_router",
                    "memory_scope": "team",
                    "logical_key": None,
                    "status": "ACTIVE",
                    "value": "部署在裸金属",
                },
            ],
        },
    )
    final = snapshot(
        (record("mem-other", agent_id="core_router", memory_scope="team", logical_key=None, value="部署在裸金属"),)
    )
    ev = evidence(scn, final_snapshot=final, snapshots_by_step={"r1": (snapshot(()), final)})
    result = evaluate_scenario(ev)
    assert _assertion(result, "scope_isolation").status is AssertionStatus.PASS


def test_scope_isolation_unexpected_foreign_scope_row_fails():
    scn = scenario(steps=[step(step_id="r1")], expected_state=[])
    final = snapshot((record("mem-foreign", agent_id="evil-agent", memory_scope="direct"),))
    ev = evidence(scn, final_snapshot=final)
    result = evaluate_scenario(ev)
    violation = _assertion(result, "scope_isolation")
    assert violation.status is AssertionStatus.FAIL
    assert violation.failure_taxonomy is FailureTaxonomy.INVARIANT_VIOLATION


def test_scope_isolation_expected_foreign_row_mutated_fails():
    scn = scenario(
        steps=[step(step_id="r1")],
        initial_state={
            "kind": "SEEDED",
            "records": [
                {
                    "alias": "other",
                    "agent_id": "core_router",
                    "memory_scope": "team",
                    "logical_key": None,
                    "status": "ACTIVE",
                    "value": "部署在裸金属",
                },
            ],
        },
        expected_state=[
            {
                "alias": "other",
                "agent_id": "core_router",
                "memory_scope": "team",
                "logical_key": None,
                "status": "ACTIVE",
                "value": "部署在裸金属",
            },
        ],
    )
    # expected team row mutated to a different logical_key -> extra ACTIVE row mismatch
    final = snapshot(
        (record("mem-other", agent_id="core_router", memory_scope="team", logical_key=None, value="被篡改"),)
    )
    ev = evidence(scn, final_snapshot=final)
    result = evaluate_scenario(ev)
    assert _assertion(result, "final_state").status is AssertionStatus.FAIL


def test_scope_leakage_selected_foreign_identity():
    scn = scenario(
        steps=[
            step(
                step_id="r1",
                agent_id="core_router",
                memory_scope="direct",
                expected_retrieval={"expected_selected": ["db"], "expected_excluded": [], "k": 5},
            )
        ],
        expected_state=[],
    )
    ev = evidence(
        scn,
        selection_by_step={"r1": selection("r1", "run-1", selected_ids=("mem-team",))},
        alias_binding={"db": "mem-db"},
        final_snapshot=snapshot(
            (record("mem-team", agent_id="core_router", memory_scope="team", logical_key=None, value="x"),)
        ),
    )
    result = evaluate_scenario(ev)
    leakage = next(a for a in result.assertions if a.assertion_id.endswith(".leakage.scope"))
    assert leakage.status is AssertionStatus.FAIL
    assert leakage.failure_taxonomy is FailureTaxonomy.SCOPE_LEAKAGE


# ------------------------------------------------------------------ E1-R2 failure taxonomy


def test_scenario_rollup_does_not_fabricate_final_state_taxonomy():
    scn = scenario(
        steps=[step(step_id="r1", expected_retrieval={"expected_selected": ["db"], "expected_excluded": [], "k": 5})],
        expected_state=[
            {
                "alias": "db",
                "agent_id": "core_router",
                "memory_scope": "direct",
                "logical_key": "project.database",
                "status": "ACTIVE",
                "value": "SQLite",
            },
        ],
    )
    ev = evidence(
        scn,
        selection_by_step={"r1": selection("r1", "run-1", selected_ids=())},
        alias_binding={"db": "mem-db"},
        final_snapshot=snapshot((record("mem-db", logical_key="project.database"),)),
    )
    result = evaluate_scenario(ev)
    # retrieval FAIL, final state PASS
    assert _assertion(result, "final_state").status is AssertionStatus.PASS
    outcome = _assertion(result, "outcome")
    assert outcome.status is AssertionStatus.FAIL
    assert outcome.failure_taxonomy is None
    assert "RETRIEVAL_MISS" in outcome.actual_evidence["child_failure_taxonomies"]
    assert "FINAL_STATE_MISMATCH" not in outcome.actual_evidence["child_failure_taxonomies"]
    assert "FINAL_STATE_MISMATCH" not in result.failure_taxonomies
    assert "RETRIEVAL_MISS" in result.failure_taxonomies
