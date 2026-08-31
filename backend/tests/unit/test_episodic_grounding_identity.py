"""WP6-E grounding identity authority + persisted fidelity 契约测试（68 Gate frozen）。

覆盖 WP6-E-D-G5-R1 §22-24：

11. expected release_list/SUCCEEDED + runtime task-release_list/SUCCEEDED -> PASS
12. same identity 但 actual FAILED -> FAIL
13. display name=执行专业任务 不影响 canonical identity PASS
14. canonical runtime identity 缺失 -> grounding FAIL（BLOCKED 若 evidence 缺失）
15. terminal mismatch 仍 FAIL
16. delivery mismatch 仍 FAIL
17. persisted status mismatch 仍 FAIL
18. fabricated extra step 仍进入 FABRICATED_FACT
19. E02 truthful failed step PASS
20. E10 truthful failed step PASS
+ 二十三：旧错误回归（release_list vs 执行专业任务 -> identity PASS，不是
  EPISODE_GROUNDING_MISMATCH）
+ 二十四：核心能力回归（E04/E07/E08/E09/E10）
"""

# ruff: noqa: D101, D105, D415

import dataclasses

from app.core.evaluation.episodic_assertion import EpisodicFailureTaxonomy
from app.core.evaluation.episodic_evaluators import evaluate_episodic_scenario
from app.core.evaluation.episodic_evidence import EpisodicRunEvidence
from app.core.evaluation.stateful_assertion import AssertionStatus
from tests.unit.episodic_fixtures import load_dataset, scenario_by_case
from tests.unit.test_episodic_evaluators import (
    _evidence,
    _grounding_fidelity_assertion,
    _grounding_identity_assertions,
    _journal_step_facts_for_run,
    _projection_for_run,
    _run_evidence,
)

DATASET = load_dataset()


def _identity_status(evaluation, prefix: str) -> list[AssertionStatus]:
    return [item.status for item in _grounding_identity_assertions(evaluation, prefix)]


def _fidelity_status(evaluation, prefix: str) -> AssertionStatus:
    return _grounding_fidelity_assertion(evaluation, prefix).status


def test_11_expected_succeeded_runtime_succeeded_pass():
    scenario = scenario_by_case(DATASET, "E01")
    run = scenario.runs[0]
    memory = "episode-e01"
    records = [_run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174600")]
    evaluation = evaluate_episodic_scenario(
        _evidence(scenario, records, projections=[_projection_for_run(scenario, run, memory)])
    )
    assert _identity_status(evaluation, f"{scenario.scenario_id}.run_a") == [AssertionStatus.PASS] * 2
    assert _fidelity_status(evaluation, f"{scenario.scenario_id}.run_a") is AssertionStatus.PASS
    assert evaluation.scenario_outcome is AssertionStatus.PASS


def test_12_same_identity_but_actual_failed_fails():
    scenario = scenario_by_case(DATASET, "E01")
    run = scenario.runs[0]
    memory = "episode-e01"
    record = _run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174601")
    # canonical runtime task-release_list = FAILED（与 Dataset expectation 冲突）
    record = dataclasses.replace(
        record,
        step_facts=_journal_step_facts_for_run(
            run, record.actual_runtime_run_id, status_override={"task-release_list": "FAILED"}
        ),
    )
    evaluation = evaluate_episodic_scenario(
        _evidence(scenario, [record], projections=[_projection_for_run(scenario, run, memory)])
    )
    identity = _grounding_identity_assertions(evaluation, f"{scenario.scenario_id}.run_a")
    assert any(item.status is AssertionStatus.FAIL for item in identity)
    assert any(item.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_GROUNDING_MISMATCH for item in identity)
    assert evaluation.scenario_outcome is AssertionStatus.FAIL


def test_13_display_name_does_not_affect_canonical_identity_pass():
    scenario = scenario_by_case(DATASET, "E01")
    run = scenario.runs[0]
    memory = "episode-e01"
    records = [_run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174602")]
    # projection observation 使用 display name 执行专业任务（EpisodeObservation.name 不是
    # identity），canonical identity 完全来自 journal step facts。
    projection = _projection_for_run(scenario, run, memory)
    evaluation = evaluate_episodic_scenario(_evidence(scenario, records, projections=[projection]))
    assert _identity_status(evaluation, f"{scenario.scenario_id}.run_a") == [AssertionStatus.PASS] * 2
    assert _fidelity_status(evaluation, f"{scenario.scenario_id}.run_a") is AssertionStatus.PASS


def test_14_canonical_runtime_identity_missing_grounding_fails():
    scenario = scenario_by_case(DATASET, "E01")
    run = scenario.runs[0]
    memory = "episode-e01"
    # 无 step_facts、无 runtime_receipt step facts -> identity evidence 缺失 -> BLOCKED
    record = _run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174603")
    record = dataclasses.replace(record, step_facts=None)
    evaluation = evaluate_episodic_scenario(
        _evidence(scenario, [record], projections=[_projection_for_run(scenario, run, memory)], auto_step_facts=False)
    )
    identity = _grounding_identity_assertions(evaluation, f"{scenario.scenario_id}.run_a")
    assert identity
    assert all(item.status is AssertionStatus.BLOCKED for item in identity)
    assert evaluation.scenario_outcome is AssertionStatus.BLOCKED


def test_15_terminal_mismatch_still_fails():
    scenario = scenario_by_case(DATASET, "E01")
    run = scenario.runs[0]
    memory = "episode-e01"
    record = _run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174604")
    # 真实 runtime terminal FAILED 但 Dataset 期望 SUCCEEDED -> fidelity FAIL
    record = dataclasses.replace(
        record,
        runtime_receipt=dataclasses.replace(
            record.runtime_receipt, terminal_status="FAILED", delivery_status="NOT_DELIVERED"
        )
        if record.runtime_receipt is not None
        else None,
    )
    projection = _projection_for_run(scenario, run, memory, terminal_status="FAILED", delivery_status="NOT_DELIVERED")
    evaluation = evaluate_episodic_scenario(_evidence(scenario, [record], projections=[projection]))
    fidelity = _grounding_fidelity_assertion(evaluation, f"{scenario.scenario_id}.run_a")
    assert fidelity.status is AssertionStatus.FAIL
    assert fidelity.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_GROUNDING_MISMATCH


def test_16_delivery_mismatch_still_fails():
    scenario = scenario_by_case(DATASET, "E01")
    run = scenario.runs[0]
    memory = "episode-e01"
    record = _run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174605")
    record = dataclasses.replace(
        record,
        runtime_receipt=dataclasses.replace(record.runtime_receipt, delivery_status="NOT_DELIVERED")
        if record.runtime_receipt is not None
        else None,
    )
    projection = _projection_for_run(scenario, run, memory, delivery_status="NOT_DELIVERED")
    evaluation = evaluate_episodic_scenario(_evidence(scenario, [record], projections=[projection]))
    fidelity = _grounding_fidelity_assertion(evaluation, f"{scenario.scenario_id}.run_a")
    assert fidelity.status is AssertionStatus.FAIL
    assert fidelity.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_GROUNDING_MISMATCH


def test_17_persisted_status_mismatch_still_fails():
    scenario = scenario_by_case(DATASET, "E01")
    run = scenario.runs[0]
    memory = "episode-e01"
    records = [_run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174606")]
    # persisted Episode 把真实 SUCCEEDED 写成 FAILED -> fidelity FAIL
    from app.core.evaluation.episodic_projection import EpisodicObservationProjection

    projection = _projection_for_run(scenario, run, memory)
    projection = dataclasses.replace(
        projection,
        observations=tuple(
            EpisodicObservationProjection(item.observation_type, item.name, "FAILED", item.safe_error_code)
            for item in projection.observations
        ),
    )
    evaluation = evaluate_episodic_scenario(_evidence(scenario, records, projections=[projection]))
    # runtime terminal 真实 SUCCEEDED；persisted observation 把它写成 FAILED。
    # fidelity 检查会看到 result.terminal_status 仍 SUCCEEDED（与 runtime 一致），
    # 但 observation status FAILED 表示虚构/失真 -> 必须 FAIL。
    fidelity = _grounding_fidelity_assertion(evaluation, f"{scenario.scenario_id}.run_a")
    # persisted observation status 失真必须被检测；当前 fidelity 检查要求 observation
    # 是真实投影，所以 FAIL。
    assert fidelity.status is AssertionStatus.FAIL
    assert fidelity.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_GROUNDING_MISMATCH


def test_18_fabricated_extra_step_enters_fabricated_fact():
    scenario = scenario_by_case(DATASET, "E05")
    run = scenario.runs[0]
    memory = "episode-e05"
    records = [_run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174607")]
    projection = _projection_for_run(
        scenario, run, memory, situation="检查配置并核对备份策略，调用了 tool_not_invoked"
    )
    evaluation = evaluate_episodic_scenario(_evidence(scenario, records, projections=[projection]))
    fidelity = _grounding_fidelity_assertion(evaluation, f"{scenario.scenario_id}.run_a")
    assert fidelity.status is AssertionStatus.FAIL
    assert fidelity.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_FABRICATED_FACT
    assert evaluation.scenario_outcome is AssertionStatus.FAIL


def test_19_e02_truthful_failed_step_pass():
    scenario = scenario_by_case(DATASET, "E02")
    run = scenario.runs[0]
    memory = "episode-e02"
    records = [_run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174608")]
    # E02 run A 真实 FAILED / NOT_DELIVERED；Dataset 期望 replication_check=FAILED。
    # projection 与 step_facts 都按真实失败构造 -> identity PASS + fidelity PASS。
    projection = _projection_for_run(scenario, run, memory, terminal_status="FAILED", delivery_status="NOT_DELIVERED")
    evaluation = evaluate_episodic_scenario(_evidence(scenario, records, projections=[projection]))
    assert _identity_status(evaluation, f"{scenario.scenario_id}.run_a") == [AssertionStatus.PASS]
    assert _fidelity_status(evaluation, f"{scenario.scenario_id}.run_a") is AssertionStatus.PASS
    assert evaluation.scenario_outcome is AssertionStatus.PASS


def test_20_e10_truthful_failed_step_pass():
    from app.core.evaluation.episodic_evidence import (
        EpisodicFormationReceiptEvidence,
        RunExecutionStatus,
    )

    scenario = scenario_by_case(DATASET, "E10")
    run_a = scenario.runs[0]
    run_b = scenario.runs[1]
    failed = "episode-failed"
    mem_b = "episode-b"
    run_a_record = EpisodicRunEvidence(
        scenario_id=scenario.scenario_id,
        case_code="E10",
        dataset_run_id="run_a",
        actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174609",
        execution_status=RunExecutionStatus.EXECUTED,
        terminal_status="FAILED",
        delivery_status="NOT_DELIVERED",
        formation_receipt=EpisodicFormationReceiptEvidence(
            run_id="123e4567-e89b-12d3-a456-426614174609",
            outcome="CREATED",
            memory_id=failed,
            lesson_status="ABSENT",
        ),
    )
    run_a_record = dataclasses.replace(
        run_a_record,
        step_facts=_journal_step_facts_for_run(run_a, run_a_record.actual_runtime_run_id),
    )
    run_b_record = _run_evidence(scenario, run_b, mem_b, actual_run_id="123e4567-e89b-12d3-a456-426614174610")
    projections = [
        _projection_for_run(scenario, run_a, failed, terminal_status="FAILED", delivery_status="NOT_DELIVERED"),
        _projection_for_run(scenario, run_b, mem_b),
    ]
    evaluation = evaluate_episodic_scenario(_evidence(scenario, [run_a_record, run_b_record], projections))
    # run_a 的 grounding identity 必须 PASS（真实失败被忠实记录）
    assert _identity_status(evaluation, f"{scenario.scenario_id}.run_a") == [AssertionStatus.PASS]
    assert _fidelity_status(evaluation, f"{scenario.scenario_id}.run_a") is AssertionStatus.PASS


# ---------------------------------------------------------------------------
# 二十三：negative regression（旧错误不再可能）
# ---------------------------------------------------------------------------


def test_negative_regression_display_name_not_identity():
    scenario = scenario_by_case(DATASET, "E01")
    run = scenario.runs[0]
    memory = "episode-e01"
    records = [_run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174611")]
    # 旧错误：expected release_list vs persisted display 执行专业任务 -> 直接 FAIL。
    # 新契约：identity 来自 journal task-release_list=SUCCEEDED -> identity PASS。
    projection = _projection_for_run(scenario, run, memory)
    evaluation = evaluate_episodic_scenario(_evidence(scenario, records, projections=[projection]))
    identity = _grounding_identity_assertions(evaluation, f"{scenario.scenario_id}.run_a")
    assert identity and all(item.status is AssertionStatus.PASS for item in identity)
    for item in identity:
        assert item.failure_taxonomy is None
    assert not any(
        item.failure_taxonomy is EpisodicFailureTaxonomy.EPISODE_GROUNDING_MISMATCH for item in evaluation.assertions
    )


# ---------------------------------------------------------------------------
# 二十四：核心能力回归
# ---------------------------------------------------------------------------


def test_24_core_capabilities_regression():
    """E04/E07/E08/E09/E10 核心能力不得回退（WP6-E-D-G5-R1 §24）。

    构造与既有 test_episodic_evaluators / test_episodic_metrics_gate 相同的通过路径，
    断言 scenario outcome PASS。
    """
    from app.core.evaluation.episodic_evidence import (
        EpisodicCaptureEvidence,
        EpisodicFixtureReceiptEvidence,
        EpisodicFormationReceiptEvidence,
        RunExecutionStatus,
    )
    from tests.unit.episodic_fixtures import (
        capture_wire,
        injected_wire,
        selection_item_wire,
        selection_wire,
        supplied_wire,
    )

    def _capture(run_id, mem_a, *, score=5, selected=True):
        return EpisodicCaptureEvidence.from_wire(
            capture_wire(
                run_id=run_id,
                selection=selection_wire(candidate_count=1, items=[selection_item_wire(mem_a, 1, score, selected)]),
                supplied=supplied_wire([mem_a] if selected else []),
                injected=[injected_wire("PLANNING", [mem_a])] if selected else [],
            )
        )

    def _empty_capture(run_id):
        return EpisodicCaptureEvidence.from_wire(
            capture_wire(
                run_id=run_id,
                selection=selection_wire(candidate_count=0, items=[]),
                supplied=supplied_wire([]),
                injected=[],
            )
        )

    # E04 idempotency
    scenario = scenario_by_case(DATASET, "E04")
    run = scenario.runs[0]
    evaluation = evaluate_episodic_scenario(
        _evidence(
            scenario,
            [
                _run_evidence(
                    scenario, run, "episode-e04", actual_run_id="123e4567-e89b-12d3-a456-426614174620", replay=True
                )
            ],
            projections=[_projection_for_run(scenario, run, "episode-e04")],
        )
    )
    assert evaluation.scenario_outcome is AssertionStatus.PASS, "E04"

    # E07 retrieval hit
    scenario = scenario_by_case(DATASET, "E07")
    run_a, run_b = scenario.runs
    mem_a = "episode-a"
    run_b_id = "123e4567-e89b-12d3-a456-426614174621"
    evaluation = evaluate_episodic_scenario(
        _evidence(
            scenario,
            [
                _run_evidence(scenario, run_a, mem_a, actual_run_id="123e4567-e89b-12d3-a456-426614174622"),
                _run_evidence(scenario, run_b, "episode-b", actual_run_id=run_b_id, capture=_capture(run_b_id, mem_a)),
            ],
            projections=[
                _projection_for_run(scenario, run_a, mem_a),
                _projection_for_run(scenario, run_b, "episode-b"),
            ],
        )
    )
    assert evaluation.scenario_outcome is AssertionStatus.PASS, "E07"

    # E08 zero-score rejection
    scenario = scenario_by_case(DATASET, "E08")
    run_a, run_b = scenario.runs
    mem_a = "episode-a"
    run_b_id = "123e4567-e89b-12d3-a456-426614174623"
    evaluation = evaluate_episodic_scenario(
        _evidence(
            scenario,
            [
                _run_evidence(scenario, run_a, mem_a, actual_run_id="123e4567-e89b-12d3-a456-426614174624"),
                _run_evidence(
                    scenario,
                    run_b,
                    "episode-b",
                    actual_run_id=run_b_id,
                    capture=_capture(run_b_id, mem_a, score=0, selected=False),
                ),
            ],
            projections=[
                _projection_for_run(scenario, run_a, mem_a),
                _projection_for_run(scenario, run_b, "episode-b"),
            ],
        )
    )
    assert evaluation.scenario_outcome is AssertionStatus.PASS, "E08"

    # E09 scope isolation
    scenario = scenario_by_case(DATASET, "E09")
    run = scenario.runs[0]
    foreign = "episode-foreign"
    mem_a = "episode-a"
    run_id = "123e4567-e89b-12d3-a456-426614174625"
    record = EpisodicRunEvidence(
        scenario_id=scenario.scenario_id,
        case_code="E09",
        dataset_run_id="run_a",
        actual_runtime_run_id=run_id,
        execution_status=RunExecutionStatus.EXECUTED,
        terminal_status="SUCCEEDED",
        delivery_status="DELIVERED",
        formation_receipt=EpisodicFormationReceiptEvidence(
            run_id=run_id, outcome="CREATED", memory_id=mem_a, lesson_status="ABSENT"
        ),
        fixture_receipt=EpisodicFixtureReceiptEvidence(
            fixture_ref="foreign_scope_episode",
            memory_id=foreign,
            origin_run_id="fixture-origin",
            origin_kind="DATASET_CONTROLLED_INITIAL_FIXTURE",
            memory_scope="orchestration",
        ),
        capture=_empty_capture(run_id),
    )
    projection = _projection_for_run(scenario, run, mem_a)
    from app.core.evaluation.episodic_projection import EpisodicObservationProjection

    foreign_projection = dataclasses.replace(
        projection,
        memory_id=foreign,
        origin_run_id="fixture-origin",
        agent_id="ops_router",
        memory_scope="orchestration",
        observations=(EpisodicObservationProjection("STEP", "deploy_probe", "SUCCEEDED"),),
    )
    evaluation = evaluate_episodic_scenario(
        _evidence(scenario, [record], projections=[projection, foreign_projection])
    )
    assert evaluation.scenario_outcome is AssertionStatus.PASS, "E09"

    # E10 failed episode retrieval
    scenario = scenario_by_case(DATASET, "E10")
    run_a, run_b = scenario.runs
    failed = "episode-failed"
    run_b_id = "123e4567-e89b-12d3-a456-426614174626"
    run_a_record = EpisodicRunEvidence(
        scenario_id=scenario.scenario_id,
        case_code="E10",
        dataset_run_id="run_a",
        actual_runtime_run_id="123e4567-e89b-12d3-a456-426614174627",
        execution_status=RunExecutionStatus.EXECUTED,
        terminal_status="FAILED",
        delivery_status="NOT_DELIVERED",
        formation_receipt=EpisodicFormationReceiptEvidence(
            run_id="123e4567-e89b-12d3-a456-426614174627", outcome="CREATED", memory_id=failed, lesson_status="ABSENT"
        ),
    )
    run_a_record = dataclasses.replace(
        run_a_record, step_facts=_journal_step_facts_for_run(run_a, run_a_record.actual_runtime_run_id)
    )
    evaluation = evaluate_episodic_scenario(
        _evidence(
            scenario,
            [
                run_a_record,
                _run_evidence(
                    scenario, run_b, "episode-b", actual_run_id=run_b_id, capture=_capture(run_b_id, failed)
                ),
            ],
            projections=[
                _projection_for_run(
                    scenario, run_a, failed, terminal_status="FAILED", delivery_status="NOT_DELIVERED"
                ),
                _projection_for_run(scenario, run_b, "episode-b"),
            ],
        )
    )
    assert evaluation.scenario_outcome is AssertionStatus.PASS, "E10"
