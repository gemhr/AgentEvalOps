"""WP6-E metrics 与 Layer1 gate 契约测试。"""

# ruff: noqa: D101, D105, D415

from dataclasses import replace

from app.core.evaluation.episodic_assertion import EpisodicAssertionGroup, EpisodicBlockReason, EpisodicFailureTaxonomy
from app.core.evaluation.episodic_evaluators import evaluate_episodic_scenario
from app.core.evaluation.episodic_evidence import (
    EpisodicCaptureEvidence,
    EpisodicFixtureReceiptEvidence,
    EpisodicRunEvidence,
    RunExecutionStatus,
)
from app.core.evaluation.episodic_gate import evaluate_episodic_layer1_gate
from app.core.evaluation.episodic_identity import EpisodicIdentityResolver
from app.core.evaluation.episodic_metrics import (
    EPISODE_INJECTION_SUCCESS_RATE_METRIC,
    EPISODE_SCOPE_LEAKAGE_RATE_METRIC,
    EXPECTED_EPISODE_RECALL_AT_K_METRIC,
    FABRICATED_FACT_RATE_METRIC,
    FORMATION_PRECISION_METRIC,
    FORMATION_RECALL_METRIC,
    HIT_AT_K_METRIC,
    INSTRUCTION_ELEVATION_VIOLATION_RATE_METRIC,
    STATEFUL_EPISODIC_SCENARIO_SUCCESS_RATE_METRIC,
    TRIVIAL_REJECTION_RATE_METRIC,
    IRRELEVANT_EPISODE_SELECTION_RATE_METRIC,
    build_episodic_scenario_success_aggregate,
)
from app.core.evaluation.episodic_projection import EpisodicProjectionRecord
from app.core.evaluation.stateful_assertion import AssertionStatus
from tests.unit.test_episodic_evaluators import (
    _cross_run_evidence,
    _evidence,
    _formation,
    _projection_for_run,
    _run_evidence,
)
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


def _capture_for(run_id, mem_a, *, score=5, selected=True, injected_targets=("PLANNING",)):
    return EpisodicCaptureEvidence.from_wire(
        capture_wire(
            run_id=run_id,
            selection=selection_wire(candidate_count=1, items=[selection_item_wire(mem_a, 1, score, selected)]),
            supplied=supplied_wire([mem_a] if selected else []),
            injected=[injected_wire(target, [mem_a]) for target in injected_targets],
        )
    )


def _e07_evaluation(*, miss: bool = False, blocked_capture: bool = False):
    scenario = scenario_by_case(DATASET, "E07")
    run_a = scenario.runs[0]
    run_b = scenario.runs[1]
    mem_a = "episode-a"
    mem_b = "episode-b"
    run_a_record = _run_evidence(scenario, run_a, mem_a, actual_run_id="123e4567-e89b-12d3-a456-426614174400")
    if blocked_capture:
        run_b_record = _run_evidence(scenario, run_b, mem_b, actual_run_id="123e4567-e89b-12d3-a456-426614174401")
    else:
        run_b_record = _run_evidence(
            scenario,
            run_b,
            mem_b,
            actual_run_id="123e4567-e89b-12d3-a456-426614174401",
            capture=_capture_for(
                "123e4567-e89b-12d3-a456-426614174401",
                mem_a,
                score=0 if miss else 5,
                selected=not miss,
            ),
        )
    evidence = _evidence(
        scenario,
        [run_a_record, run_b_record],
        projections=[_projection_for_run(scenario, run_a, mem_a), _projection_for_run(scenario, run_b, mem_b)],
    )
    return evaluate_episodic_scenario(evidence)


def test_metrics_denominators_pass_hit():
    evaluation = _e07_evaluation()
    assert evaluation.scenario_outcome is AssertionStatus.PASS
    recall = evaluation.metrics[EXPECTED_EPISODE_RECALL_AT_K_METRIC]
    assert recall.evaluable_denominator > 0
    assert recall.value == 1.0
    hit = evaluation.metrics[HIT_AT_K_METRIC]
    assert hit.value == 1.0


def test_metrics_denominators_miss():
    evaluation = _e07_evaluation(miss=True)
    recall = evaluation.metrics[EXPECTED_EPISODE_RECALL_AT_K_METRIC]
    assert recall.evaluable_denominator > 0
    assert recall.value == 0.0


def test_irrelevant_episode_selection_rate_is_zero_best_failure_metric():
    assert IRRELEVANT_EPISODE_SELECTION_RATE_METRIC == "irrelevant_episode_selection_rate"
    scenario = scenario_by_case(DATASET, "E08")
    run_id_b = "123e4567-e89b-12d3-a456-426614174421"
    mem_a = "episode-e08"
    rejected = evaluate_episodic_scenario(
        _cross_run_evidence(
            scenario,
            mem_a=mem_a,
            mem_b="episode-b",
            run_b_capture=EpisodicCaptureEvidence.from_wire(
                capture_wire(
                    run_id=run_id_b,
                    selection=selection_wire(
                        candidate_count=1,
                        items=[selection_item_wire(mem_a, 1, 0, False, "NO_LEXICAL_MATCH")],
                    ),
                    supplied=supplied_wire([]),
                    injected=[],
                )
            ),
        )
    )
    metric = rejected.metrics[IRRELEVANT_EPISODE_SELECTION_RATE_METRIC]
    assert metric.value == 0.0

    incorrectly_selected = evaluate_episodic_scenario(
        _cross_run_evidence(
            scenario,
            mem_a=mem_a,
            mem_b="episode-b",
            run_b_capture=EpisodicCaptureEvidence.from_wire(
                capture_wire(
                    run_id="123e4567-e89b-12d3-a456-426614174422",
                    selection=selection_wire(candidate_count=1, items=[selection_item_wire(mem_a, 1, 0, True)]),
                    supplied=supplied_wire([mem_a]),
                    injected=[injected_wire("PLANNING", [mem_a])],
                )
            ),
        )
    )
    assert incorrectly_selected.metrics[IRRELEVANT_EPISODE_SELECTION_RATE_METRIC].value > 0.0


def test_blocked_excluded_from_quality_denominator():
    evaluation = _e07_evaluation(blocked_capture=True)
    assert evaluation.scenario_outcome is AssertionStatus.BLOCKED
    recall = evaluation.metrics[EXPECTED_EPISODE_RECALL_AT_K_METRIC]
    # 无 capture -> 没有可 evaluable 的 identity assertion -> NOT_EVALUABLE（value None）
    assert recall.evaluable_denominator == 0
    assert recall.value is None
    # blocked 通过 runtime block / eval infra rate 独立报告
    infra_rate = evaluation.metrics["evaluation_infra_failure_rate"]
    assert infra_rate.numerator >= 1


def test_formation_precision_recall():
    scenario = scenario_by_case(DATASET, "E01")
    run = scenario.runs[0]
    record = _run_evidence(scenario, run, "episode-e01", actual_run_id="123e4567-e89b-12d3-a456-426614174402")
    evaluation = evaluate_episodic_scenario(
        _evidence(scenario, [record], projections=[_projection_for_run(scenario, run, "episode-e01")])
    )
    assert evaluation.scenario_outcome is AssertionStatus.PASS
    precision = evaluation.metrics[FORMATION_PRECISION_METRIC]
    recall = evaluation.metrics[FORMATION_RECALL_METRIC]
    assert precision.value == 1.0
    assert recall.value == 1.0


def test_fabricated_fact_rate_zero_best():
    scenario = scenario_by_case(DATASET, "E05")
    run = scenario.runs[0]
    memory = "episode-e05"
    record = _run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174403")
    projection = _projection_for_run(
        scenario, run, memory, situation="检查配置并核对备份策略，调用了 tool_not_invoked"
    )
    evaluation = evaluate_episodic_scenario(_evidence(scenario, [record], projections=[projection]))
    rate = evaluation.metrics[FABRICATED_FACT_RATE_METRIC]
    # fabricated fact 是 0-best rate：失败断言在 numerator
    assert rate.value == 1.0
    assert evaluation.scenario_outcome is AssertionStatus.FAIL


def test_scope_leakage_rate_zero_best():
    scenario = scenario_by_case(DATASET, "E09")
    run = scenario.runs[0]
    foreign = "episode-foreign"
    mem_a = "episode-a"
    run_id = "123e4567-e89b-12d3-a456-426614174404"
    record = EpisodicRunEvidence(
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
        capture=_capture_for(run_id, foreign, score=5, selected=True),  # 泄漏
    )
    evaluation = evaluate_episodic_scenario(
        _evidence(
            scenario,
            [record],
            projections=[_projection_for_run(scenario, run, mem_a)],
        )
    )
    rate = evaluation.metrics[EPISODE_SCOPE_LEAKAGE_RATE_METRIC]
    assert rate.value == 1.0  # 0-best：泄漏 = 1.0


def test_injection_metric_uses_injected_not_selected():
    scenario = scenario_by_case(DATASET, "E11")
    run_a = scenario.runs[0]
    run_b = scenario.runs[1]
    mem_a = "episode-a"
    run_a_record = _run_evidence(scenario, run_a, mem_a, actual_run_id="123e4567-e89b-12d3-a456-426614174405")
    # selected=1 但 actual injected=0 -> injection 失败
    run_b_record = _run_evidence(
        scenario,
        run_b,
        "episode-b",
        actual_run_id="123e4567-e89b-12d3-a456-426614174406",
        capture=EpisodicCaptureEvidence.from_wire(
            capture_wire(
                run_id="123e4567-e89b-12d3-a456-426614174406",
                selection=selection_wire(candidate_count=1, items=[selection_item_wire(mem_a, 1, 5, True)]),
                supplied=supplied_wire([mem_a]),
                injected=[],
            )
        ),
    )
    evaluation = evaluate_episodic_scenario(
        _evidence(
            scenario,
            [run_a_record, run_b_record],
            projections=[
                _projection_for_run(scenario, run_a, mem_a),
                _projection_for_run(scenario, run_b, "episode-b"),
            ],
        )
    )
    injection = evaluation.metrics[EPISODE_INJECTION_SUCCESS_RATE_METRIC]
    assert injection.value == 0.0  # 使用 actual injected evidence，不是 selected count


def test_empty_denominator_not_evaluable():
    scenario = scenario_by_case(DATASET, "E12")
    evaluation = (
        evaluate_episodic_scenario(
            type(
                "E",
                (),
                {
                    "scenario": scenario,
                    "run_evidence_by_dataset_run_id": {},
                    "identity_map": None,
                    "final_projection": (),
                    "evaluation_layer": AssertionStatus.PASS,
                },
            )()
        )
        if False
        else None
    )
    # 无 E12 证据时 injection metric 无 evaluable unit -> value None
    from app.core.evaluation.episodic_evidence import EpisodicScenarioEvaluationEvidence
    from app.core.evaluation.stateful_assertion import EvaluationLayer

    evidence = EpisodicScenarioEvaluationEvidence(
        scenario=scenario, evaluation_layer=EvaluationLayer.LAYER_1_DETERMINISTIC
    )
    evaluation = evaluate_episodic_scenario(evidence)
    assert evaluation.scenario_outcome is AssertionStatus.BLOCKED
    for name in (
        EXPECTED_EPISODE_RECALL_AT_K_METRIC,
        HIT_AT_K_METRIC,
        EPISODE_INJECTION_SUCCESS_RATE_METRIC,
    ):
        metric = evaluation.metrics[name]
        if metric.evaluable_denominator == 0:
            assert metric.value is None, name


def test_scenario_success_aggregate():
    aggregate = build_episodic_scenario_success_aggregate(
        [AssertionStatus.PASS, AssertionStatus.PASS, AssertionStatus.FAIL, AssertionStatus.BLOCKED]
    )
    assert aggregate.metric_name == STATEFUL_EPISODIC_SCENARIO_SUCCESS_RATE_METRIC
    assert aggregate.passed == 2
    assert aggregate.failed == 1
    assert aggregate.blocked == 1
    assert aggregate.evaluable_denominator == 3
    assert aggregate.value == 2 / 3  # BLOCKED 不进 denominator


# ---------------------------------------------------------------------------
# Layer1 gate
# ---------------------------------------------------------------------------


def test_gate_all_pass():
    gate = evaluate_episodic_layer1_gate([_e07_evaluation()])
    assert gate.passed is True
    assert gate.deterministic_gate_required_total == 1
    assert gate.deterministic_gate_required_passed == 1


def test_gate_one_p0_violation_fails():
    scenario = scenario_by_case(DATASET, "E06")
    run = scenario.runs[0]
    memory = "episode-e06"
    record = _run_evidence(scenario, run, memory, actual_run_id="123e4567-e89b-12d3-a456-426614174407")
    projection = _projection_for_run(scenario, run, memory, situation="api_key=FAKE_EPISODIC_SECRET_SENTINEL_001 泄露")
    evaluation = evaluate_episodic_scenario(_evidence(scenario, [record], projections=[projection]))
    gate = evaluate_episodic_layer1_gate([evaluation])
    assert gate.passed is False
    assert gate.privacy_violations == 1
    assert gate.p0_violations == 1


def test_gate_required_fail_fails():
    gate = evaluate_episodic_layer1_gate([_e07_evaluation(miss=True)])
    assert gate.passed is False
    assert gate.deterministic_gate_required_passed == 0


def test_gate_required_blocked_not_pass():
    gate = evaluate_episodic_layer1_gate([_e07_evaluation(blocked_capture=True)])
    assert gate.passed is False
    assert gate.required_blocked_total == 1


def test_gate_evidence_failure_not_silently_excluded():
    gate = evaluate_episodic_layer1_gate([_e07_evaluation(blocked_capture=True)], artifacts_retained=False)
    assert gate.passed is False
    assert any("not retained" in reason for reason in gate.reasons)


def test_gate_layer2_limitation_does_not_pollute_layer1():
    # Layer2 expected limitation 属于未来；Layer1 identity capture REQUIRED，不适用。
    # 此处验证 Layer1 gate 对 blocked evidence_capture 严格非 PASS。
    gate = evaluate_episodic_layer1_gate([_e07_evaluation(blocked_capture=True)])
    assert gate.passed is False
    assert any("blocked" in reason for reason in gate.reasons)


def test_experiment_aggregate_preserves_zero_best_failure_rate_semantics():
    """Experiment 聚合必须保留 0-best failure-rate 语义（failed / (p+f)）。

    冻结语义：scope leakage / instruction elevation / fabricated fact 是 0-best，
    value = failed / (passed + failed)。聚合时不得重算成 accuracy（passed/denom）。
    """
    from app.core.evaluation.episodic_metrics import (
        build_episodic_experiment_metrics,
    )
    from app.core.evaluation.stateful_metrics import MetricAggregate

    # 模拟两个 scenario：scope 断言 1 PASS（无泄漏），trust 断言 1 FAIL（1 次 elevation）。
    scenario_metrics = [
        {
            EPISODE_SCOPE_LEAKAGE_RATE_METRIC: MetricAggregate(
                metric_name=EPISODE_SCOPE_LEAKAGE_RATE_METRIC,
                passed=1,
                failed=0,
                blocked=0,
                not_applicable=0,
                evaluable_denominator=1,
                value=0.0,
            ),
            INSTRUCTION_ELEVATION_VIOLATION_RATE_METRIC: MetricAggregate(
                metric_name=INSTRUCTION_ELEVATION_VIOLATION_RATE_METRIC,
                passed=0,
                failed=1,
                blocked=0,
                not_applicable=0,
                evaluable_denominator=1,
                value=1.0,
            ),
            EPISODE_INJECTION_SUCCESS_RATE_METRIC: MetricAggregate(
                metric_name=EPISODE_INJECTION_SUCCESS_RATE_METRIC,
                passed=2,
                failed=1,
                blocked=0,
                not_applicable=0,
                evaluable_denominator=3,
                value=2 / 3,
            ),
        },
        {
            EPISODE_SCOPE_LEAKAGE_RATE_METRIC: MetricAggregate(
                metric_name=EPISODE_SCOPE_LEAKAGE_RATE_METRIC,
                passed=0,
                failed=0,
                blocked=0,
                not_applicable=0,
                evaluable_denominator=0,
                value=None,
            ),
        },
    ]
    aggregated = build_episodic_experiment_metrics(scenario_metrics)
    scope = aggregated[EPISODE_SCOPE_LEAKAGE_RATE_METRIC]
    assert isinstance(scope, MetricAggregate)
    assert scope.value == 0.0  # failed/(p+f) = 0/1
    elevation = aggregated[INSTRUCTION_ELEVATION_VIOLATION_RATE_METRIC]
    assert isinstance(elevation, MetricAggregate)
    assert elevation.value == 1.0  # failed/(p+f) = 1/1
    # accuracy 语义 metric 仍按 passed/(p+f)
    injection = aggregated[EPISODE_INJECTION_SUCCESS_RATE_METRIC]
    assert isinstance(injection, MetricAggregate)
    assert abs(injection.value - 2 / 3) < 1e-9  # passed/(p+f) = 2/3
