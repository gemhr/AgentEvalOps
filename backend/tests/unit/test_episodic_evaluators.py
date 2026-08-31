"""WP6-E 13 类 evaluator 契约测试（E01-E12 核心断言 + negative 路径）。"""

# ruff: noqa: D101, D105, D415

from __future__ import annotations

import dataclasses

from app.core.evaluation.episodic_assertion import EpisodicAssertionGroup, EpisodicBlockReason, EpisodicFailureTaxonomy
from app.core.evaluation.episodic_evaluators import evaluate_episodic_scenario
from app.core.evaluation.episodic_evidence import (
    EpisodicCaptureEvidence,
    EpisodicContextSourceType,
    EpisodicContextTrustLevel,
    EpisodicEvidenceError,
    EpisodicFixtureReceiptEvidence,
    EpisodicFormationReceiptEvidence,
    EpisodicRunEvidence,
    EpisodicScenarioEvaluationEvidence,
    RunExecutionStatus,
)
from app.core.evaluation.episodic_identity import EpisodicIdentityResolver
from app.core.evaluation.episodic_projection import (
    EpisodicObservationProjection,
    EpisodicProjectionRecord,
    EpisodicResultProjection,
)
from app.core.evaluation.stateful_assertion import AssertionStatus, EvaluationLayer
from tests.unit.episodic_fixtures import (
    capture_wire,
    injected_wire,
    load_dataset,
    scenario_by_case,
    selection_item_wire,
    selection_wire,
    supplied_wire,
)

DATASET = load_dataset()
RESOLVER = EpisodicIdentityResolver()

#: compiler-owned human-readable display label（68 Gate）；不是 canonical step identity。
_DISPLAY_STEP_NAME = "执行专业任务"

#: 显式未设置 step_facts 的 sentinel（区别于「未提供」的 None 自动补全）。
_STEP_FACTS_UNSET = object()


def _journal_step_facts_for_run(run, actual_run_id: str, *, status_override: dict[str, str] | None = None):
    """按 run 的 expected grounding 构造 canonical Journal RuntimeEvent.step_id facts。

    status_override 可把 canonical step 改成与 expectation 不同的 status（真实 FAIL）。
    """
    from app.core.evaluation.stateful_journal import JournalStepFact, JournalStepFacts

    grounding = run.expected_grounding
    if grounding is None:
        return JournalStepFacts(run_id=actual_run_id, facts=())
    facts = []
    for item in grounding.required_observed_step_statuses:
        canonical = f"task-{item.step_ref}"
        status = status_override.get(canonical) if status_override else None
        if status is None:
            status = item.expected_status.value
        facts.append(JournalStepFact(actual_run_id, f"evt-{item.step_ref}", "STEP_STARTED", canonical, None))
        facts.append(JournalStepFact(actual_run_id, f"evt-{item.step_ref}-c", "STEP_COMPLETED", canonical, status))
    return JournalStepFacts(run_id=actual_run_id, facts=tuple(facts))


def _formation(run_id: str, outcome: str, memory_id: str | None):
    return EpisodicFormationReceiptEvidence(
        run_id=run_id, outcome=outcome, memory_id=memory_id, lesson_status="ABSENT"
    )


def _projection(
    memory_id: str,
    *,
    origin_run_id: str,
    situation: str,
    observations: tuple[EpisodicObservationProjection, ...] | None = None,
    terminal_status: str = "SUCCEEDED",
    delivery_status: str = "DELIVERED",
    memory_scope: str = "direct",
    agent_id: str = "core_router",
    lesson: str | None = None,
) -> EpisodicProjectionRecord:
    return EpisodicProjectionRecord(
        memory_id=memory_id,
        memory_type="EPISODIC",
        status="ACTIVE",
        agent_id=agent_id,
        memory_scope=memory_scope,
        origin_run_id=origin_run_id,
        logical_key=None,
        canonical_text=f"Situation: {situation}\nGoal: {situation}\nResult: {terminal_status}",
        payload_schema_version=1,
        situation_text=situation,
        goal_text=situation,
        goal_authority="RUNTIME_OBSERVED_PLAN_GOAL",
        observations=observations or (EpisodicObservationProjection("STEP", "work", "SUCCEEDED"),),
        result=EpisodicResultProjection(terminal_status, "COMPLETED", delivery_status),
        lesson=lesson,
        created_at="2026-01-01T00:00:00+00:00",
        formation_method="EPISODIC_V1",
    )


def _projection_for_run(scenario, run, memory_id: str, **overrides) -> EpisodicProjectionRecord:
    """按 run 的 expected grounding 构造匹配 projection。

    Episode observation 是 human-readable factual observation：name 是 display name
    （compiler-owned ``执行专业任务``），不是 canonical Runtime step_id。Runtime
    identity/status 由 journal/runtime receipt 提供（``step_facts``）。
    """
    grounding = run.expected_grounding
    observations = (
        tuple(
            EpisodicObservationProjection("STEP", _DISPLAY_STEP_NAME, item.expected_status.value)
            for item in grounding.required_observed_step_statuses
        )
        if grounding is not None
        else (EpisodicObservationProjection("STEP", "work", "SUCCEEDED"),)
    )
    terminal = grounding.expected_terminal_status.value if grounding is not None else "SUCCEEDED"
    delivery = grounding.expected_delivery_status.value if grounding is not None else "DELIVERED"
    situation = overrides.pop("situation", run.user_request)
    return _projection(
        memory_id,
        origin_run_id=run.run_id,
        situation=situation,
        observations=overrides.pop("observations", observations),
        terminal_status=overrides.pop("terminal_status", terminal),
        delivery_status=overrides.pop("delivery_status", delivery),
        **overrides,
    )


def _run_evidence(scenario, run, memory_id: str, *, actual_run_id: str, capture=None, replay=False, **overrides):
    step_facts = overrides.pop("step_facts", _STEP_FACTS_UNSET)
    record = EpisodicRunEvidence(
        scenario_id=scenario.scenario_id,
        case_code=scenario.case_code,
        dataset_run_id=run.run_id,
        actual_runtime_run_id=actual_run_id,
        execution_status=RunExecutionStatus.EXECUTED,
        terminal_status="SUCCEEDED",
        delivery_status="DELIVERED",
        formation_receipt=_formation(actual_run_id, "CREATED", memory_id),
        replay_receipt=_formation(actual_run_id, "REUSED", memory_id) if replay else None,
        capture=capture,
    )
    if step_facts is not _STEP_FACTS_UNSET:
        record = dataclasses.replace(record, step_facts=step_facts)
    if overrides:
        record = dataclasses.replace(record, **overrides)
    return record


def _evidence(scenario, run_records, projections=(), *, auto_step_facts: bool = True):
    # 未显式提供 canonical step facts 时，按 run 的 expected grounding 自动构造
    # Journal RuntimeEvent.step_id facts（与真实 runner 的 journal 采集一致）。
    # ``auto_step_facts=False`` 保留缺失 evidence（identity -> BLOCKED）。
    enriched: list[EpisodicRunEvidence] = []
    run_by_id = {run.run_id: run for run in scenario.runs}
    for record in run_records:
        run = run_by_id.get(record.dataset_run_id)
        if (
            run is not None
            and auto_step_facts
            and record.step_facts is None
            and record.execution_status is RunExecutionStatus.EXECUTED
        ):
            record = dataclasses.replace(
                record,
                step_facts=_journal_step_facts_for_run(run, record.actual_runtime_run_id),
            )
        enriched.append(record)
    identity_map = RESOLVER.resolve(
        scenario,
        formation_receipt_by_run_id={
            record.dataset_run_id: record.formation_receipt
            for record in enriched
            if record.formation_receipt is not None
        },
        fixture_receipt_by_ref={
            record.fixture_receipt.fixture_ref: record.fixture_receipt
            for record in enriched
            if record.fixture_receipt is not None
        },
    )
    return EpisodicScenarioEvaluationEvidence(
        scenario=scenario,
        run_evidence_by_dataset_run_id={record.dataset_run_id: record for record in enriched},
        identity_map=identity_map,
        final_projection=tuple(projections),
        evaluation_layer=EvaluationLayer.LAYER_1_DETERMINISTIC,
    )


def _assertion(evaluation, assertion_id: str):
    return next(item for item in evaluation.assertions if item.assertion_id == assertion_id)


def _grounding_identity_assertions(evaluation, prefix: str):
    """返回该 run 的 runtime identity grounding assertions（.grounding.identity.step.*）。"""
    return [
        item
        for item in evaluation.assertions
        if item.assertion_id.startswith(prefix) and ".grounding.identity.step." in item.assertion_id
    ]


def _grounding_fidelity_assertion(evaluation, prefix: str):
    """返回该 run 的 persisted observation fidelity assertion（.grounding.fidelity）。"""
    return _assertion(evaluation, f"{prefix}.grounding.fidelity")


def test_e01_successful_formation_pass():
    scenario = scenario_by_case(DATASET, "E01")
    memory = "episode-e01"
    run = scenario.runs[0]
    run_records = [_run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174300")]
    evaluation = evaluate_episodic_scenario(
        _evidence(scenario, run_records, projections=[_projection_for_run(scenario, run, memory)])
    )
    assert evaluation.scenario_outcome is AssertionStatus.PASS
    assert _assertion(evaluation, f"{scenario.scenario_id}.run_a.formation").status is AssertionStatus.PASS
    assert _assertion(evaluation, f"{scenario.scenario_id}.run_a.structure").status is AssertionStatus.PASS
    for item in _grounding_identity_assertions(evaluation, f"{scenario.scenario_id}.run_a"):
        assert item.status is AssertionStatus.PASS
    assert _grounding_fidelity_assertion(evaluation, f"{scenario.scenario_id}.run_a").status is AssertionStatus.PASS
    assert _assertion(evaluation, f"{scenario.scenario_id}.persistence").status is AssertionStatus.PASS


def test_e02_failed_run_truthful_episode_pass():
    scenario = scenario_by_case(DATASET, "E02")
    memory = "episode-e02"
    run = scenario.runs[0]
    run_records = [_run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174301")]
    projection = _projection_for_run(scenario, run, memory)
    evaluation = evaluate_episodic_scenario(_evidence(scenario, run_records, projections=[projection]))
    assert evaluation.scenario_outcome is AssertionStatus.PASS
    for item in _grounding_identity_assertions(evaluation, f"{scenario.scenario_id}.run_a"):
        assert item.status is AssertionStatus.PASS
    assert _grounding_fidelity_assertion(evaluation, f"{scenario.scenario_id}.run_a").status is AssertionStatus.PASS


def test_e03_trivial_rejection_pass():
    scenario = scenario_by_case(DATASET, "E03")
    run_records = [
        EpisodicRunEvidence(
            scenario_id=scenario.scenario_id,
            case_code="E03",
            dataset_run_id="run_a",
            actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174302",
            execution_status=RunExecutionStatus.EXECUTED,
            terminal_status="SUCCEEDED",
            delivery_status="DELIVERED",
            formation_receipt=_formation("123e4567-e89b-12d3-a456-426614174302", "SKIPPED", None),
        )
    ]
    evaluation = evaluate_episodic_scenario(_evidence(scenario, run_records))
    assert evaluation.scenario_outcome is AssertionStatus.PASS
    assert _assertion(evaluation, f"{scenario.scenario_id}.run_a.eligibility").status is AssertionStatus.PASS
    assert _assertion(evaluation, f"{scenario.scenario_id}.run_a.formation").status is AssertionStatus.PASS


def test_e04_idempotency_pass():
    scenario = scenario_by_case(DATASET, "E04")
    memory = "episode-e04"
    run = scenario.runs[0]
    run_records = [
        _run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174303", replay=True)
    ]
    evaluation = evaluate_episodic_scenario(
        _evidence(scenario, run_records, projections=[_projection_for_run(scenario, run, memory)])
    )
    assert evaluation.scenario_outcome is AssertionStatus.PASS
    assert _assertion(evaluation, f"{scenario.scenario_id}.idempotency").status is AssertionStatus.PASS


def test_e04_idempotency_second_row_violation():
    scenario = scenario_by_case(DATASET, "E04")
    memory = "episode-e04"
    run = scenario.runs[0]
    record = _run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174304", replay=True)
    # replay 产生不同 memory_id -> 第二行
    from dataclasses import replace

    record = replace(record, replay_receipt=_formation(record.actual_runtime_run_id, "CREATED", "episode-e04-second"))
    evaluation = evaluate_episodic_scenario(
        _evidence(scenario, [record], projections=[_projection_for_run(scenario, run, memory)])
    )
    idempotency = _assertion(evaluation, f"{scenario.scenario_id}.idempotency")
    assert idempotency.status is AssertionStatus.FAIL
    assert idempotency.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_IDEMPOTENCY_VIOLATION


def test_e05_fabricated_fact_detection():
    scenario = scenario_by_case(DATASET, "E05")
    memory = "episode-e05"
    run = scenario.runs[0]
    run_records = [_run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174305")]
    projection = _projection_for_run(
        scenario, run, memory, situation="检查配置并核对备份策略，调用了 tool_not_invoked"
    )
    evaluation = evaluate_episodic_scenario(_evidence(scenario, run_records, projections=[projection]))
    grounding = _grounding_fidelity_assertion(evaluation, f"{scenario.scenario_id}.run_a")
    assert grounding.status is AssertionStatus.FAIL
    assert grounding.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_FABRICATED_FACT
    assert evaluation.scenario_outcome is AssertionStatus.FAIL


def test_e06_privacy_leak_detection():
    scenario = scenario_by_case(DATASET, "E06")
    memory = "episode-e06"
    run = scenario.runs[0]
    run_records = [_run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174306")]
    projection = _projection_for_run(
        scenario, run, memory, situation="api_key=FAKE_EPISODIC_SECRET_SENTINEL_001 需要轮换"
    )
    evaluation = evaluate_episodic_scenario(_evidence(scenario, run_records, projections=[projection]))
    privacy = _assertion(evaluation, f"{scenario.scenario_id}.run_a.privacy")
    assert privacy.status is AssertionStatus.FAIL
    assert privacy.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_PRIVACY_VIOLATION
    assert evaluation.scenario_outcome is AssertionStatus.FAIL


def _cross_run_evidence(scenario, *, mem_a, mem_b, run_b_capture, failed_a: bool = False):
    run_a = scenario.runs[0]
    run_b = scenario.runs[1]
    run_a_record = _run_evidence(scenario, run_a, mem_a, actual_run_id="123e4567-e89b-12d3-a456-426614174310")
    run_b_record = _run_evidence(
        scenario, run_b, mem_b, actual_run_id="123e4567-e89b-12d3-a456-426614174311", capture=run_b_capture
    )
    projections = [
        _projection_for_run(scenario, run_a, mem_a),
        _projection_for_run(scenario, run_b, mem_b),
    ]
    return _evidence(scenario, [run_a_record, run_b_record], projections)


def test_e07_relevant_retrieval_hit_pass():
    scenario = scenario_by_case(DATASET, "E07")
    run_id_b = "123e4567-e89b-12d3-a456-426614174312"
    mem_a = "episode-a"
    mem_b = "episode-b"
    capture = EpisodicCaptureEvidence.from_wire(
        capture_wire(
            run_id=run_id_b,
            selection=selection_wire(candidate_count=1, items=[selection_item_wire(mem_a, 1, 5, True)]),
            supplied=supplied_wire([mem_a]),
            injected=[injected_wire("PLANNING", [mem_a])],
        )
    )
    evaluation = evaluate_episodic_scenario(
        _cross_run_evidence(scenario, mem_a=mem_a, mem_b=mem_b, run_b_capture=capture)
    )
    assert evaluation.scenario_outcome is AssertionStatus.PASS
    assert _assertion(evaluation, f"{scenario.scenario_id}.run_b.retrieval.identity").status is AssertionStatus.PASS
    assert _assertion(evaluation, f"{scenario.scenario_id}.run_b.retrieval.hit").status is AssertionStatus.PASS


def test_e07_retrieval_miss_when_expected_not_selected():
    scenario = scenario_by_case(DATASET, "E07")
    run_id_b = "123e4567-e89b-12d3-a456-426614174313"
    mem_a = "episode-a"
    capture = EpisodicCaptureEvidence.from_wire(
        capture_wire(
            run_id=run_id_b,
            selection=selection_wire(
                candidate_count=1, items=[selection_item_wire(mem_a, 1, 0, False, "NO_LEXICAL_MATCH")]
            ),
            supplied=supplied_wire([]),
            injected=[],
        )
    )
    evaluation = evaluate_episodic_scenario(
        _cross_run_evidence(scenario, mem_a=mem_a, mem_b="episode-b", run_b_capture=capture)
    )
    identity = _assertion(evaluation, f"{scenario.scenario_id}.run_b.retrieval.identity")
    assert identity.status is AssertionStatus.FAIL
    assert identity.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_RETRIEVAL_MISS


def test_e08_exact_zero_score_rejection_pass():
    scenario = scenario_by_case(DATASET, "E08")
    run_id_b = "123e4567-e89b-12d3-a456-426614174314"
    mem_a = "episode-a"
    capture = EpisodicCaptureEvidence.from_wire(
        capture_wire(
            run_id=run_id_b,
            selection=selection_wire(
                candidate_count=1, items=[selection_item_wire(mem_a, 1, 0, False, "NO_LEXICAL_MATCH")]
            ),
            supplied=supplied_wire([]),
            injected=[],
        )
    )
    evaluation = evaluate_episodic_scenario(
        _cross_run_evidence(scenario, mem_a=mem_a, mem_b="episode-b", run_b_capture=capture)
    )
    assert evaluation.scenario_outcome is AssertionStatus.PASS
    score = _assertion(evaluation, f"{scenario.scenario_id}.run_b.retrieval.score.run_a_episode")
    assert score.status is AssertionStatus.PASS
    assert (
        _assertion(evaluation, f"{scenario.scenario_id}.run_b.ranking.zero_score_exclusion").status
        is AssertionStatus.PASS
    )


def test_e08_zero_score_rejection_violation():
    scenario = scenario_by_case(DATASET, "E08")
    run_id_b = "123e4567-e89b-12d3-a456-426614174315"
    mem_a = "episode-a"
    capture = EpisodicCaptureEvidence.from_wire(
        capture_wire(
            run_id=run_id_b,
            selection=selection_wire(candidate_count=1, items=[selection_item_wire(mem_a, 1, 0, True)]),
            supplied=supplied_wire([mem_a]),
            injected=[injected_wire("PLANNING", [mem_a])],
        )
    )
    evaluation = evaluate_episodic_scenario(
        _cross_run_evidence(scenario, mem_a=mem_a, mem_b="episode-b", run_b_capture=capture)
    )
    assert evaluation.scenario_outcome is AssertionStatus.FAIL
    assert (
        _assertion(evaluation, f"{scenario.scenario_id}.run_b.ranking.zero_score_exclusion").status
        is AssertionStatus.FAIL
    )


def test_e09_scope_leakage_detection():
    scenario = scenario_by_case(DATASET, "E09")
    foreign = "episode-foreign"
    mem_a = "episode-a"
    run = scenario.runs[0]
    run_id = "123e4567-e89b-12d3-a456-426614174316"
    run_record = EpisodicRunEvidence(
        scenario_id=scenario.scenario_id,
        case_code="E09",
        dataset_run_id="run_a",
        actual_runtime_run_id=run_id,
        execution_status=RunExecutionStatus.EXECUTED,
        terminal_status="SUCCEEDED",
        delivery_status="DELIVERED",
        formation_receipt=_formation(run_id, "CREATED", mem_a),
        fixture_receipt=EpisodicFixtureReceiptEvidence(
            fixture_ref="foreign_scope_episode",
            memory_id=foreign,
            origin_run_id="fixture-origin",
            origin_kind="DATASET_CONTROLLED_INITIAL_FIXTURE",
            memory_scope="orchestration",
        ),
        capture=EpisodicCaptureEvidence.from_wire(
            capture_wire(
                run_id=run_id,
                selection=selection_wire(candidate_count=0, items=[selection_item_wire(foreign, 1, 5, True)]),
                supplied=supplied_wire([foreign]),
                injected=[injected_wire("PLANNING", [foreign])],
            )
        ),
    )
    projection = _projection_for_run(scenario, run, mem_a)
    foreign_projection = _projection(
        foreign, origin_run_id="fixture-origin", situation=run.user_request, memory_scope="orchestration"
    )
    evaluation = evaluate_episodic_scenario(
        _evidence(scenario, [run_record], projections=[projection, foreign_projection])
    )
    scope = _assertion(evaluation, f"{scenario.scenario_id}.run_a.scope_isolation")
    assert scope.status is AssertionStatus.FAIL
    assert scope.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_SCOPE_LEAKAGE
    assert evaluation.scenario_outcome is AssertionStatus.FAIL


def test_e10_failed_episode_retrieval_pass():
    scenario = scenario_by_case(DATASET, "E10")
    run_a = scenario.runs[0]
    run_b = scenario.runs[1]
    failed = "episode-failed"
    mem_b = "episode-b"
    run_id_b = "123e4567-e89b-12d3-a456-426614174317"
    run_a_record = EpisodicRunEvidence(
        scenario_id=scenario.scenario_id,
        case_code="E10",
        dataset_run_id="run_a",
        actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174318",
        execution_status=RunExecutionStatus.EXECUTED,
        terminal_status="FAILED",
        delivery_status="NOT_DELIVERED",
        formation_receipt=_formation("123e4567-e89b-12d3-a456-426614174318", "CREATED", failed),
    )
    run_b_record = _run_evidence(
        scenario,
        run_b,
        mem_b,
        actual_run_id=run_id_b,
        capture=EpisodicCaptureEvidence.from_wire(
            capture_wire(
                run_id=run_id_b,
                selection=selection_wire(candidate_count=1, items=[selection_item_wire(failed, 1, 6, True)]),
                supplied=supplied_wire([failed]),
                injected=[injected_wire("PLANNING", [failed])],
            )
        ),
    )
    projections = [
        _projection_for_run(scenario, run_a, failed),
        _projection_for_run(scenario, run_b, mem_b),
    ]
    evaluation = evaluate_episodic_scenario(_evidence(scenario, [run_a_record, run_b_record], projections))
    assert evaluation.scenario_outcome is AssertionStatus.PASS
    assert _assertion(evaluation, f"{scenario.scenario_id}.run_b.retrieval.identity").status is AssertionStatus.PASS


def test_e11_injection_miss_detection():
    scenario = scenario_by_case(DATASET, "E11")
    mem_a = "episode-a"
    run_id_b = "123e4567-e89b-12d3-a456-426614174319"
    capture = EpisodicCaptureEvidence.from_wire(
        capture_wire(
            run_id=run_id_b,
            selection=selection_wire(candidate_count=1, items=[selection_item_wire(mem_a, 1, 5, True)]),
            supplied=supplied_wire([mem_a]),
            injected=[],
        )
    )
    evaluation = evaluate_episodic_scenario(
        _cross_run_evidence(scenario, mem_a=mem_a, mem_b="episode-b", run_b_capture=capture)
    )
    assert _assertion(evaluation, f"{scenario.scenario_id}.run_b.injection.selected").status is AssertionStatus.PASS
    context = _assertion(evaluation, f"{scenario.scenario_id}.run_b.injection.context_record_count")
    assert context.status is AssertionStatus.FAIL
    assert context.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_CONTEXT_INJECTION_MISS
    assert evaluation.scenario_outcome is AssertionStatus.FAIL


def test_injection_count_is_evaluated_per_planning_target():
    """PLANNING=1 与 DIRECT_ENTRY=1 不得被错误聚合为 2。"""
    scenario = scenario_by_case(DATASET, "E11")
    mem_a = "episode-a"
    run_id_b = "123e4567-e89b-12d3-a456-426614174420"
    capture = EpisodicCaptureEvidence.from_wire(
        capture_wire(
            run_id=run_id_b,
            selection=selection_wire(candidate_count=1, items=[selection_item_wire(mem_a, 1, 5, True)]),
            supplied=supplied_wire([mem_a]),
            injected=[injected_wire("PLANNING", [mem_a]), injected_wire("DIRECT_ENTRY", [mem_a])],
        )
    )
    evaluation = evaluate_episodic_scenario(
        _cross_run_evidence(scenario, mem_a=mem_a, mem_b="episode-b", run_b_capture=capture)
    )
    assert (
        _assertion(evaluation, f"{scenario.scenario_id}.run_b.injection.context_record_count").status
        is AssertionStatus.PASS
    )


def test_wire_enums_are_typed_and_unknown_values_fail_closed():
    wire = injected_wire("PLANNING", ["episode-a"])
    wire["source_type"] = "episodic_memory_retrieval"
    wire["trust_level"] = "user_content"
    parsed = EpisodicCaptureEvidence.from_wire(
        capture_wire(run_id="run", selection=None, supplied=None, injected=[wire])
    ).injected[0]
    assert parsed.source_type is EpisodicContextSourceType.EPISODIC_MEMORY_RETRIEVAL
    assert parsed.trust_level is EpisodicContextTrustLevel.USER_CONTENT

    for field, invalid in (("source_type", "unknown_source"), ("trust_level", "unknown_trust")):
        invalid_wire = injected_wire("PLANNING", ["episode-a"])
        invalid_wire[field] = invalid
        try:
            EpisodicCaptureEvidence.from_wire(
                capture_wire(run_id="run", selection=None, supplied=None, injected=[invalid_wire])
            )
        except EpisodicEvidenceError:
            continue
        raise AssertionError(f"unknown {field} must fail closed")


def test_e12_instruction_elevation_detection():
    scenario = scenario_by_case(DATASET, "E12")
    mem_a = "episode-a"
    run_id_b = "123e4567-e89b-12d3-a456-426614174320"
    capture = EpisodicCaptureEvidence.from_wire(
        capture_wire(
            run_id=run_id_b,
            selection=selection_wire(candidate_count=1, items=[selection_item_wire(mem_a, 1, 5, True)]),
            supplied=supplied_wire([mem_a]),
            injected=[
                {
                    "target": "PLANNING",
                    "episodic_memory_ids": [mem_a],
                    "context_record_count": 1,
                    "source_type": "EPISODIC_MEMORY_RETRIEVAL",
                    "trust_level": "SYSTEM_CONTENT",
                }
            ],
        )
    )
    evaluation = evaluate_episodic_scenario(
        _cross_run_evidence(scenario, mem_a=mem_a, mem_b="episode-b", run_b_capture=capture)
    )
    trust = _assertion(evaluation, f"{scenario.scenario_id}.run_b.trust_boundary")
    assert trust.status is AssertionStatus.FAIL
    assert trust.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_INSTRUCTION_ELEVATION
    assert evaluation.scenario_outcome is AssertionStatus.FAIL


def test_identity_evidence_missing_blocks_not_fail():
    scenario = scenario_by_case(DATASET, "E07")
    run_a = scenario.runs[0]
    run_b = scenario.runs[1]
    run_a_record = _run_evidence(scenario, run_a, "episode-a", actual_run_id="123e4567-e89b-12d3-a456-426614174321")
    run_b_record = _run_evidence(scenario, run_b, "episode-b", actual_run_id="123e4567-e89b-12d3-a456-426614174322")
    # run_b 缺 capture -> BLOCKED / EVIDENCE_CAPTURE
    evaluation = evaluate_episodic_scenario(
        _evidence(
            scenario,
            [run_a_record, run_b_record],
            projections=[
                _projection_for_run(scenario, run_a, "episode-a"),
                _projection_for_run(scenario, run_b, "episode-b"),
            ],
        )
    )
    evidence_assertion = _assertion(evaluation, f"{scenario.scenario_id}.run_b.retrieval.evidence")
    assert evidence_assertion.status is AssertionStatus.BLOCKED
    assert evidence_assertion.blocked_by is EpisodicBlockReason.EVIDENCE_CAPTURE
    assert evaluation.scenario_outcome is AssertionStatus.BLOCKED


def test_all_scenarios_without_evidence_blocked():
    for case in ("E01", "E02", "E03", "E04", "E05", "E06", "E07", "E08", "E09", "E10", "E11", "E12"):
        scenario = scenario_by_case(DATASET, case)
        evaluation = evaluate_episodic_scenario(
            EpisodicScenarioEvaluationEvidence(
                scenario=scenario, evaluation_layer=EvaluationLayer.LAYER_1_DETERMINISTIC
            )
        )
        assert evaluation.scenario_outcome is AssertionStatus.BLOCKED, case
        blocked = [item for item in evaluation.assertions if item.status is AssertionStatus.BLOCKED]
        assert blocked, case
        for item in blocked:
            assert item.blocked_by in {
                EpisodicBlockReason.EVIDENCE_CAPTURE,
                EpisodicBlockReason.PREREQUISITE,
            }, (case, item.assertion_id)
