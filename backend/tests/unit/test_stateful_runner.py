"""Stateful scenario runner：sequential / persistence / isolation / snapshots / binding。"""

# ruff: noqa: D101, D105, D415

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.evaluation.execution import ExecutionTargetRef
from app.core.evaluation.references import VersionRef
from app.core.evaluation.stateful_assertion import AssertionStatus, BlockReason
from app.core.evaluation.stateful_memory_dataset import load_stateful_memory_dataset
from app.core.evaluation.stateful_memory_dataset_v2 import load_stateful_memory_dataset_v2
from app.core.evaluation.stateful_projection import read_memory_projection
from app.registry.settings import settings
from app.services.evaluation.stateful_environment import StatefulEnvironmentError
from app.services.evaluation.stateful_runner import (
    ScenarioRunPlan,
    StatefulScenarioRunnerService,
)
from tests.unit.fixtures.stateful_runtime import (
    FixtureStatefulProvisioner,
    ScriptedStep,
    make_fake_persistence,
    stateful_lexical_score,
)

DATASET = load_stateful_memory_dataset("evaluation_assets/stateful_memory_v1/stateful_memory_dataset.v1.json")
V2_DATASET = load_stateful_memory_dataset_v2("evaluation_assets/stateful_memory_v2/stateful_memory_dataset.v2.json")
CREATED_AT = datetime(2026, 8, 29, tzinfo=timezone.utc)


def scenario(by_id: str):
    return next(s for s in DATASET.scenarios if s.scenario_id == by_id)


def v2_scenario(by_id: str):
    return next(s for s in V2_DATASET.scenarios if s.scenario_id == by_id)


def make_plan(scn, target_kind="FIXTURE"):
    return ScenarioRunPlan(
        dataset_id=DATASET.dataset_id,
        dataset_version=DATASET.version,
        dataset_digest=DATASET.content_digest,
        scenario=scn,
        target_ref=ExecutionTargetRef(
            target_id="scripted-memory",
            target_kind=target_kind,
            config_ref=VersionRef("scripted_config", "v1"),
        ),
        timeout=timedelta(seconds=30),
        created_at=CREATED_AT,
    )


def make_v2_plan(scn, target_kind="FIXTURE"):
    return ScenarioRunPlan(
        dataset_id=V2_DATASET.dataset_id,
        dataset_version=V2_DATASET.version,
        dataset_digest=V2_DATASET.content_digest,
        scenario=scn,
        target_ref=ExecutionTargetRef(
            target_id="scripted-memory",
            target_kind=target_kind,
            config_ref=VersionRef("scripted_config", "v1"),
        ),
        timeout=timedelta(seconds=30),
        created_at=CREATED_AT,
    )


def scripts_for(scn, *, lexical_wildcard=False):
    scripts = {}
    for step in scn.steps:
        exp_f = step.expected_formation
        if exp_f is not None:
            if exp_f.decision.value == "IGNORE":
                scripts[step.query] = ScriptedStep(query=step.query, operation="ignore")
            elif step.expected_lifecycle.value == "FORGET":
                scripts[step.query] = ScriptedStep(
                    query=step.query,
                    operation="forget",
                    logical_key=exp_f.predicate.predicate_id,
                )
            else:
                scripts[step.query] = ScriptedStep(
                    query=step.query,
                    operation="remember",
                    logical_key=exp_f.predicate.predicate_id,
                    value=step_value(scn, step),
                    canonical_text=_v2_database_correction_canonical_text(scn, step) if lexical_wildcard else None,
                )
        elif step.expected_retrieval is not None:
            scripts[step.query] = ScriptedStep(
                query=step.query,
                operation="retrieve",
                selected_ids=("*",),
                lexical_wildcard=lexical_wildcard,
                context_record_count=1,
                planning_injected=True,
            )
        else:
            scripts[step.query] = ScriptedStep(query=step.query, operation="retrieve", selected_ids=())
    return scripts


def _v2_database_correction_canonical_text(scn, step):
    """R3-C V2 harness 复现已审计的 database-correction formation canonical text。"""
    if scn.scenario_id != "database_correction":
        return None
    return {
        "r1": "项目数据库使用 SQLite",
        "r2": "项目数据库为 PostgreSQL",
    }.get(step.step_id)


def step_value(scn, step):
    predicate_id = step.expected_formation.predicate.predicate_id
    matching = [r for r in scn.expected_state if r.logical_key == predicate_id]
    if step.expected_lifecycle.value == "SUPERSEDE":
        active = [r for r in matching if r.status.value == "ACTIVE"]
        return active[0].value if active else None
    superseded = [r for r in matching if r.status.value == "SUPERSEDED"]
    if superseded:
        return superseded[0].value
    active = [r for r in matching if r.status.value == "ACTIVE"]
    return active[0].value if active else None


async def run_scenario(tmp_path, by_id):
    uow, persistence = make_fake_persistence()
    scn = scenario(by_id)
    provisioner = FixtureStatefulProvisioner(Path(tmp_path), scripts=scripts_for(scn))
    runner = StatefulScenarioRunnerService(persistence)
    plan = make_plan(scn)
    receipt = await runner.execute_scenario(
        uuid4(),
        plan,
        provisioner,
        lease=timedelta(seconds=60),
        worker_ref="test",
    )
    return uow, receipt, provisioner


async def run_v2_scenario(tmp_path, by_id):
    uow, persistence = make_fake_persistence()
    scn = v2_scenario(by_id)
    provisioner = FixtureStatefulProvisioner(Path(tmp_path), scripts=scripts_for(scn, lexical_wildcard=True))
    runner = StatefulScenarioRunnerService(persistence)
    receipt = await runner.execute_scenario(
        uuid4(),
        make_v2_plan(scn),
        provisioner,
        lease=timedelta(seconds=60),
        worker_ref="test",
    )
    return uow, receipt, provisioner


@pytest.mark.asyncio
async def test_scenario_sequential_ordered_execution(tmp_path):
    uow, receipt, _ = await run_scenario(tmp_path, "database_correction")
    assert [record.step.step_id for record in receipt.step_records] == ["r1", "r2", "r3"]
    assert all(record.outcome_kind.value == "SUCCESS" for record in receipt.step_records)
    # steps executed in dataset order and each attempt persisted as TERMINAL
    for record in receipt.step_records:
        attempt = await persistence_attempt(uow, record.attempt_id)
        assert attempt.status.value == "TERMINAL"


async def persistence_attempt(uow, attempt_id):
    return uow._attempts[str(attempt_id)]


@pytest.mark.asyncio
async def test_step_attempt_persistence_has_evidence_refs(tmp_path):
    uow, receipt, _ = await run_scenario(tmp_path, "database_correction")
    for record in receipt.step_records:
        attempt = await persistence_attempt(uow, record.attempt_id)
        kinds = {ref.kind for ref in attempt.outcome_evidence_refs}
        assert "stateful_journal_evidence" in kinds
        assert "stateful_state_snapshot" in kinds


@pytest.mark.asyncio
async def test_scenario_isolation_no_db_leakage(tmp_path):
    uow_a, receipt_a, prov_a = await run_scenario(tmp_path, "formation_stable_fact_remember")
    env_a = receipt_a.environment
    a_before = len(read_memory_projection(env_a.memory_db_path))
    # scenario B runs with its own fresh DB; must not touch A's DB
    uow_b, receipt_b, prov_b = await run_scenario(tmp_path, "lifecycle_supersede")
    env_b = receipt_b.environment
    assert env_a.memory_db_path != env_b.memory_db_path
    assert env_a.journal_db_path != env_b.journal_db_path
    # A's DB untouched by B's execution
    assert len(read_memory_projection(env_a.memory_db_path)) == a_before == 1
    # B's DB has B's own rows only
    b_rows = read_memory_projection(env_b.memory_db_path)
    assert len(b_rows) == 2
    assert all(row.agent_id == "core_router" and row.memory_scope == "direct" for row in b_rows)


@pytest.mark.asyncio
async def test_scripted_retrieval_respects_scope_for_scope_isolation(tmp_path):
    """Wildcard retrieval evidence must mirror LocalAgent's agent/scope eligibility."""
    _, receipt, _ = await run_scenario(tmp_path, "scope_isolation")
    selection = receipt.step_records[0].selection_evidence
    assert selection is not None
    assert selection.selected_memory_ids == (receipt.alias_binding["db"],)
    assert receipt.alias_binding["other"] not in selection.selected_memory_ids
    assert receipt.evaluation.scenario_outcome is AssertionStatus.PASS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario_id", "step_id", "selected_alias"),
    [
        ("retrieval_active_hit", "r1", "db"),
        ("retrieval_status_exclusion", "r1", "db"),
        ("retrieval_open_hit", "r1", "codename"),
        ("retrieval_unrelated_rejection", "r1", "db"),
        ("scope_isolation", "r1", "db"),
        ("safety_forgotten_not_injected", "r1", "db"),
        ("database_correction", "r3", "db_new"),
    ],
)
async def test_v2_wildcard_retrieval_matches_frozen_layer1_identity_contract(
    tmp_path, scenario_id, step_id, selected_alias
):
    """Wildcard only simulates eligible, score-positive identities for the focused seven."""
    _, receipt, _ = await run_v2_scenario(tmp_path, scenario_id)
    selection = next(record.selection_evidence for record in receipt.step_records if record.step.step_id == step_id)
    assert selection is not None
    assert selection.selected_memory_ids == (receipt.alias_binding[selected_alias],)
    assert receipt.evaluation.scenario_outcome is AssertionStatus.PASS


@pytest.mark.parametrize(
    ("query", "canonical_text", "logical_key", "payload", "expected_positive"),
    [
        ("项目数据库是什么？", "项目数据库使用 SQLite", "project.database", {"value": "SQLite"}, True),
        ("项目数据库是什么？", "北京的天气是晴天", None, {"value": "北京的天气是晴天"}, False),
        ("DATABASE", "database record", None, {}, True),
        ("database", "unrelated", "project.database", {}, True),
        ("sqlite", "unrelated", None, {"value": "SQLite"}, True),
    ],
)
def test_stateful_lexical_score_matches_frozen_localagent_match_sources(
    query, canonical_text, logical_key, payload, expected_positive
):
    assert (
        stateful_lexical_score(query, canonical_text=canonical_text, logical_key=logical_key, payload=payload) > 0
    ) is (expected_positive)


@pytest.mark.asyncio
async def test_step_level_pre_post_snapshots(tmp_path):
    uow, receipt, _ = await run_scenario(tmp_path, "lifecycle_supersede")
    record = receipt.step_records[0]
    assert len(record.pre_snapshot.records) == 0
    assert len(record.post_snapshot.records) == 1
    assert record.post_snapshot.records[0].status == "ACTIVE"
    final = receipt.final_snapshot
    assert len(final.records) == 2
    statuses = {r.status for r in final.records}
    assert statuses == {"ACTIVE", "SUPERSEDED"}


@pytest.mark.asyncio
async def test_alias_binding_maps_expected_aliases_to_runtime_ids(tmp_path):
    uow, receipt, _ = await run_scenario(tmp_path, "lifecycle_supersede")
    assert "db_old" in receipt.alias_binding
    assert "db_new" in receipt.alias_binding
    assert receipt.alias_binding["db_old"] != receipt.alias_binding["db_new"]


@pytest.mark.asyncio
async def test_no_evaluator_mutation_authority(tmp_path):
    uow, receipt, _ = await run_scenario(tmp_path, "database_correction")
    artifact = receipt.artifact
    # artifact actual state is the read-only final snapshot; DB unchanged after evaluation
    records_before = [r.memory_id for r in receipt.final_snapshot.records]
    # re-read the DB: still read-only, same rows
    assert [r.memory_id for r in read_memory_projection(receipt.environment.memory_db_path)] == records_before
    # artifact carries read-only projection, not a mutation
    assert artifact.private_evaluation_artifact is True
    assert len(artifact.state_diff) == 0


@pytest.mark.asyncio
async def test_artifact_contains_all_required_sections(tmp_path):
    uow, receipt, _ = await run_scenario(tmp_path, "database_correction")
    artifact = receipt.artifact
    assert artifact.dataset_id == "stateful_memory_v1"
    assert artifact.dataset_digest.startswith("sha256:")
    assert artifact.scenario_id == "database_correction"
    assert len(artifact.step_attempts) == 3
    assert artifact.runtime_evidence_refs
    assert len(artifact.snapshot_refs) == 7  # 3 pre + 3 post + 1 final
    assert artifact.expected_state
    assert artifact.actual_state
    assert artifact.assertion_results
    assert artifact.metric_aggregates
    assert artifact.scenario_outcome == "PASS"
    assert artifact.truthfulness_origin == "DETERMINISTIC_GROUND_TRUTH"
    assert artifact.regression_tags == []


@pytest.mark.asyncio
async def test_runtime_blocked_scenario_retains_blocked_artifact(tmp_path):
    uow, persistence = make_fake_persistence()
    # a REQUIRED formation scenario whose runtime unexpectedly blocks
    scn = scenario("formation_stable_fact_remember")
    scripts = {
        scn.steps[0].query: ScriptedStep(
            query=scn.steps[0].query, operation="fail", error_category="PLANNER_SCHEMA_INVALID"
        )
    }
    provisioner = FixtureStatefulProvisioner(Path(tmp_path), scripts=scripts)
    runner = StatefulScenarioRunnerService(persistence)
    plan = make_plan(scn)
    receipt = await runner.execute_scenario(uuid4(), plan, provisioner, lease=timedelta(seconds=60))
    assert receipt.evaluation.scenario_outcome is AssertionStatus.BLOCKED
    assert receipt.artifact.scenario_outcome == "BLOCKED"
    assert receipt.evaluation.runtime_block_rate.numerator >= 1
    # blocked artifact retained (not cleaned up)
    assert receipt.environment.memory_db_path.is_file()
    # blocked artifact retained (not cleaned up)
    assert receipt.environment.memory_db_path.is_file()


@pytest.mark.asyncio
async def test_binding_failure_is_infra_blocked_and_retained(tmp_path):
    uow, persistence = make_fake_persistence()

    class BrokenProvisioner(FixtureStatefulProvisioner):
        async def verify_bound(self, evidence) -> bool:
            return False

    scn = scenario("formation_stable_fact_remember")
    provisioner = BrokenProvisioner(Path(tmp_path), scripts=scripts_for(scn))
    runner = StatefulScenarioRunnerService(persistence)
    plan = make_plan(scn)
    receipt = await runner.execute_scenario(uuid4(), plan, provisioner, lease=timedelta(seconds=60))
    assert receipt.evaluation.scenario_outcome is AssertionStatus.BLOCKED
    infra = next(a for a in receipt.evaluation.assertions if a.assertion_id.endswith(".infra"))
    assert infra.blocked_by is BlockReason.EVALUATION_INFRASTRUCTURE
    assert receipt.artifact.retention_ref is None  # retained via preserve=True + memory db present
    assert receipt.environment.memory_db_path.is_file()


@pytest.mark.asyncio
async def test_fixture_seeded_state_is_visible_in_first_pre_snapshot(tmp_path):
    uow, persistence = make_fake_persistence()
    scn = scenario("retrieval_active_hit")
    provisioner = FixtureStatefulProvisioner(Path(tmp_path), scripts=scripts_for(scn))
    runner = StatefulScenarioRunnerService(persistence)
    plan = make_plan(scn)
    receipt = await runner.execute_scenario(uuid4(), plan, provisioner, lease=timedelta(seconds=60))
    # SEEDED fixture was applied before any invocation; pre snapshot already has the seed row
    record = receipt.step_records[0]
    assert len(record.pre_snapshot.records) == 1
    assert record.pre_snapshot.records[0].logical_key == "project.database"
    assert receipt.artifact.initial_state["kind"] == "SEEDED"
    assert receipt.artifact.initial_state["fixture_seeded"] is True


@pytest.mark.asyncio
async def test_scripted_runtime_never_leaks_across_scenarios(tmp_path):
    uow_a, receipt_a, prov_a = await run_scenario(tmp_path, "lifecycle_forget")
    uow_b, receipt_b, prov_b = await run_scenario(tmp_path, "lifecycle_supersede")
    # forgotten scenario keeps tombstone in its own DB
    statuses_a = {r.status for r in read_memory_projection(receipt_a.environment.memory_db_path)}
    assert statuses_a == {"FORGOTTEN"}
    # supersede scenario DB never saw the forget
    statuses_b = {r.status for r in read_memory_projection(receipt_b.environment.memory_db_path)}
    assert "FORGOTTEN" not in statuses_b


# ------------------------------------------------------------------ E1-R2 bounded journal settle


def _settle_evidence(tmp_path, monkeypatch):
    from datetime import UTC

    from app.core.evaluation.stateful_memory_dataset import StatefulMemoryStep
    from app.services.evaluation.stateful_environment import ScenarioEnvironmentEvidence

    monkeypatch.setattr(settings, "STATEFUL_JOURNAL_POLL_INTERVAL_MS", 50)
    monkeypatch.setattr(settings, "STATEFUL_JOURNAL_SETTLE_BUDGET_MS", 800)
    journal = tmp_path / "event_journal.db"
    step = StatefulMemoryStep.model_validate(
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
    )
    evidence = ScenarioEnvironmentEvidence(
        scenario_id="s",
        scenario_environment_id="env",
        scenario_token="token",
        work_dir=tmp_path,
        memory_db_path=tmp_path / "memory.db",
        journal_db_path=journal,
        target_instance_ref="ref",
        localagent_base_url=None,
        fixture_seeded=False,
        provisioned_at=datetime.now(timezone.utc),
    )
    _, persistence = make_fake_persistence()
    runner = StatefulScenarioRunnerService(persistence)
    return runner, evidence, step, journal


@pytest.mark.asyncio
async def test_journal_settle_polls_until_event_appears(tmp_path, monkeypatch):
    import asyncio
    import json as _json
    import sqlite3

    from tests.unit.fixtures.stateful_runtime import JOURNAL_DB_SCHEMA

    runner, evidence, step, journal = _settle_evidence(tmp_path, monkeypatch)
    con = sqlite3.connect(journal)
    con.executescript(JOURNAL_DB_SCHEMA)
    con.commit()
    con.close()
    run_id = "run-settle-1"

    async def delayed_write():
        await asyncio.sleep(0.025)  # lands before the first poll (50ms)
        con = sqlite3.connect(journal)
        con.execute(
            "INSERT INTO runtime_event_journal (event_id, run_id, sequence, event_type, safe_payload) VALUES (?,?,?,?,?)",
            (
                "e1",
                run_id,
                1,
                "MEMORY_FORMATION_COMPLETED",
                _json.dumps(
                    {
                        "schema_version": 1,
                        "formation_method": "llm",
                        "status": "SUCCEEDED",
                        "proposed_count": 1,
                        "accepted_count": 1,
                        "ignored_count": 0,
                        "persisted_count": 1,
                        "reused_count": 0,
                        "failed_count": 0,
                        "candidate_outcomes": "0|PERSISTED|OK|mem-1",
                    }
                ),
            ),
        )
        con.execute(
            "INSERT INTO runtime_event_journal (event_id, run_id, sequence, event_type, safe_payload) VALUES (?,?,?,?,?)",
            (
                "e2",
                run_id,
                2,
                "MEMORY_LIFECYCLE_RESOLVED",
                _json.dumps(
                    {
                        "schema_version": 1,
                        "memory_type": "SEMANTIC",
                        "operation": "INSERT",
                        "outcome": "OK",
                        "affected_count": 1,
                        "winner_memory_id": "mem-1",
                        "new_memory_id": "mem-1",
                        "candidate_outcome": "PERSISTED",
                        "affected_transitions": "NONE",
                    }
                ),
            ),
        )
        con.commit()
        con.close()

    task = asyncio.create_task(delayed_write())
    events, settle = await runner._capture_journal_with_settle(evidence, step, run_id)
    await task
    assert len(events.formation) == 1
    assert settle.stop_reason == "EXPECTED_EVIDENCE_OBSERVED"
    assert settle.poll_attempts >= 2
    assert settle.final_sequence_watermark >= settle.initial_sequence_watermark


@pytest.mark.asyncio
async def test_journal_settle_bounded_when_event_never_appears(tmp_path, monkeypatch):
    import sqlite3

    from tests.unit.fixtures.stateful_runtime import JOURNAL_DB_SCHEMA

    runner, evidence, step, journal = _settle_evidence(tmp_path, monkeypatch)
    con = sqlite3.connect(journal)
    con.executescript(JOURNAL_DB_SCHEMA)
    con.commit()
    con.close()
    run_id = "run-never"
    events, settle = await runner._capture_journal_with_settle(evidence, step, run_id)
    assert len(events.formation) == 0
    assert settle.stop_reason in {"STABLE_WATERMARK", "DEADLINE_REACHED"}
    # bounded: at most budget/poll + 2 polls
    assert settle.poll_attempts <= (800 // 20) + 2


# ------------------------------------------------------------------ E1-R3 V2 seeded canonical


@pytest.mark.asyncio
async def test_v2_seeded_canonical_text_written_not_legacy_fallback(tmp_path):
    v2_dataset = load_stateful_memory_dataset_v2(
        "evaluation_assets/stateful_memory_v2/stateful_memory_dataset.v2.json"
    )
    scn = next(s for s in v2_dataset.scenarios if s.scenario_id == "retrieval_active_hit")
    uow, persistence = make_fake_persistence()
    scripts = {
        scn.steps[0].query: ScriptedStep(
            query=scn.steps[0].query,
            operation="retrieve",
            selected_ids=("db",),
            context_record_count=1,
            planning_injected=True,
        )
    }
    provisioner = FixtureStatefulProvisioner(Path(tmp_path), scripts=scripts)
    runner = StatefulScenarioRunnerService(persistence)
    plan = ScenarioRunPlan(
        dataset_id=v2_dataset.dataset_id,
        dataset_version=v2_dataset.version,
        dataset_digest=v2_dataset.content_digest,
        scenario=scn,
        target_ref=ExecutionTargetRef(
            target_id="scripted-memory",
            target_kind="FIXTURE",
            config_ref=VersionRef("scripted_config", "v1"),
        ),
        timeout=timedelta(seconds=30),
        created_at=CREATED_AT,
    )
    receipt = await runner.execute_scenario(uuid4(), plan, provisioner, lease=timedelta(seconds=60))
    pre = receipt.step_records[0].pre_snapshot
    assert len(pre.records) == 1
    # V2 seed 写入 trimmed canonical_text，绝不用 <logical_key>: <value> fallback
    assert pre.records[0].canonical_text == "项目数据库使用 SQLite"
    assert pre.records[0].canonical_text != "project.database: SQLite"
    assert receipt.artifact.initial_state["fixture_seeded"] is True
