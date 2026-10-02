"""WP5 指标身份、覆盖、配对及门禁反例。"""

# ruff: noqa: D102, D103, D415

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.core.evaluation.metric_policy import (
    MetricGateStatus, MetricPolicy, MetricRule, MetricRuleKind, evaluate_metric_gate,
)
from app.core.evaluation.platform_metrics import (
    DEFERRED_METRICS, MetricDefinitionV1, MetricDirection, MetricFamily, MetricIdentityCollision,
    MetricObservation, MetricState, MetricValueType, aggregate_metric, validate_definitions,
)
from app.core.evaluation.results import EvaluationVerdict
from app.core.evaluation.execution import OutcomeKind
from app.services.evaluation.comparison import EvaluationComparisonService
from app.services.evaluation.platform_metrics import build_metric_report
from app.services.evaluation.report import RegressionReportService
from scripts.ci.release_gate import serialize_report, finalize
from tests.unit.test_evaluation_comparison import (
    FakePersistence, PROJECT_ID, make_result, make_run, spec,
)


def definition(k: int = 5) -> MetricDefinitionV1:
    return MetricDefinitionV1(
        metric_id="recall", metric_version="1", family=MetricFamily.AGENT_QUALITY,
        input_contract="stage11.wp1.v1", value_type=MetricValueType.SCALAR,
        direction=MetricDirection.HIGHER_IS_BETTER, unit="ratio", value_range=(0, 1),
        grain="MACRO_CASE", parameters=(("k", str(k)),), eligibility_rule="golden",
        denominator_rule="valid", missing_rule="null", aggregation="MEAN",
        required_source="WP1_RESULT",
    )


def test_identity_coverage_zero_and_percentile() -> None:
    with pytest.raises(MetricIdentityCollision, match="METRIC_IDENTITY_COLLISION"):
        validate_definitions((definition(5), definition(10)))
    assert definition(5).metric_definition_digest != definition(10).metric_definition_digest
    observations = (
        MetricObservation("a", MetricState.VALID, 0, observed=True),
        MetricObservation("b", MetricState.MISSING),
        MetricObservation("c", MetricState.ERROR, observed=True),
        MetricObservation("d", MetricState.INCONCLUSIVE, observed=True),
    )
    aggregate = aggregate_metric(definition(), observations, scope="candidate")
    assert aggregate.value == 0
    assert aggregate.coverage.expected_count == 4
    assert aggregate.coverage.valid_count == 1
    assert aggregate.coverage.coverage == 0.25
    assert aggregate.coverage.error_count == aggregate.coverage.inconclusive_count == 1
    assert aggregate_metric(definition(), (MetricObservation("a", MetricState.MISSING),), scope="candidate").value is None
    assert aggregate_metric(definition(), (), scope="candidate").coverage.coverage is None
    percentile = replace(definition(), metric_id="p50", aggregation="P50")
    assert aggregate_metric(percentile, tuple(MetricObservation(str(i), MetricState.VALID, i) for i in range(1, 5)),
                            scope="candidate").value == 2
    shrink = aggregate_metric(definition(), tuple(
        MetricObservation(str(i), MetricState.VALID, 1.0) if i < 60
        else MetricObservation(str(i), MetricState.MISSING) for i in range(100)
    ), scope="candidate")
    assert shrink.value == 1.0
    assert shrink.coverage.expected_count == 100
    assert shrink.coverage.coverage == 0.6
    with pytest.raises(ValueError, match="unsupported metric group"):
        aggregate_metric(definition(), observations, scope="candidate", group=(("run_id", "x"),))
    with pytest.raises(ValueError, match="unit/currency mismatch"):
        aggregate_metric(replace(definition(), metric_id="cost", unit="USD", aggregation="SUM",
                                 value_type=MetricValueType.COST),
                         (MetricObservation("a", MetricState.VALID, 1, unit="EUR"),), scope="candidate")


@pytest.mark.asyncio
async def test_report_pair_coverage_digest_and_unconfigured_gate() -> None:
    baseline_id, candidate_id = uuid4(), uuid4()
    runs = {baseline_id: make_run(baseline_id), candidate_id: make_run(candidate_id)}
    results = {
        baseline_id: (make_result(run_id=baseline_id, case_id="case-a", case_version="v1",
                                  evaluator_id="eval", evaluator_version="v1", verdict=EvaluationVerdict.PASS,
                                  score=0.0),),
        candidate_id: (),
    }
    persistence = FakePersistence(runs, results)
    comparison = await EvaluationComparisonService(persistence).compare_runs(PROJECT_ID, baseline_id, candidate_id)
    report = build_metric_report(comparison, persistence.attempts[baseline_id],
                                 persistence.attempts[candidate_id], persistence.results[baseline_id],
                                 persistence.results[candidate_id])
    quality = next(item for item in report.candidate if item.definition.metric_id == "eval")
    assert quality.value is None
    assert quality.coverage.expected_count == 1
    assert quality.coverage.missing_count == 1
    assert next(item for item in report.baseline if item.definition.metric_id == "eval").value == 0
    assert next(item for item in report.candidate if item.definition.metric_id == "missing_result_rate").value == 1
    assert next(item for item in report.paired if item.definition.metric_id == "eval").paired_delta is None
    assert replace(report, computed_at=report.computed_at + timedelta(days=1)).semantic_digest == report.semantic_digest
    assert replace(report, baseline=tuple(reversed(report.baseline))).semantic_digest == report.semantic_digest
    regression = RegressionReportService().build_report(comparison, ())
    gate = evaluate_metric_gate(regression, report, None)
    assert gate.status is MetricGateStatus.FAIL  # WP3 required blocker 优先。
    ci = serialize_report(regression, report, gate)
    assert ci["metric_report"]["semantic_digest"] == report.semantic_digest
    assert ci["metric_report"]["candidate"][0]["coverage"]["expected_count"] == 1
    assert ci["metric_gate"]["reasons"] == ["WP3_CRITICAL_OR_REQUIRED_BLOCKER"]
    assert finalize(None, regression, report, gate) == 2
    assert DEFERRED_METRICS["tool_side_effect_committed_rate"] == "DEFERRED_PRODUCER"
    assert DEFERRED_METRICS["model_cost"] == "DEFERRED_PRODUCER"
    assert all(item.definition.metric_id not in DEFERRED_METRICS
               for item in (*report.baseline, *report.candidate))
    assert all("overall_score" not in item.definition.metric_id for item in report.candidate)


@pytest.mark.asyncio
async def test_no_policy_is_not_configured_and_partial_coverage_blocks_threshold() -> None:
    baseline_id, candidate_id = uuid4(), uuid4()
    runs = {baseline_id: make_run(baseline_id), candidate_id: make_run(candidate_id)}
    results = {run_id: (make_result(
        run_id=run_id, case_id="case-a", case_version="v1", evaluator_id="eval",
        evaluator_version="v1", verdict=EvaluationVerdict.PASS, score=0.9,
    ),) for run_id in runs}
    persistence = FakePersistence(runs, results)
    comparison = await EvaluationComparisonService(persistence).compare_runs(PROJECT_ID, baseline_id, candidate_id)
    report = build_metric_report(comparison, persistence.attempts[baseline_id],
                                 persistence.attempts[candidate_id], persistence.results[baseline_id],
                                 persistence.results[candidate_id])
    regression = RegressionReportService().build_report(comparison, ())
    assert evaluate_metric_gate(regression, report, None).status is MetricGateStatus.NOT_CONFIGURED
    quality = next(item for item in report.candidate if item.definition.metric_id == "eval")
    policy = MetricPolicy("test-fixture", "1", (MetricRule(
        MetricRuleKind.ABSOLUTE_MIN, "eval", quality.definition.metric_version,
        quality.definition.metric_definition_digest, 0.8,
    ),))
    assert evaluate_metric_gate(regression, report, policy).status is MetricGateStatus.PASS
    partial = replace(quality, coverage=replace(quality.coverage, coverage=0.6, valid_count=60,
                                                missing_count=40, observed_count=60,
                                                expected_count=100, eligible_count=100,
                                                value_denominator=60))
    report = replace(report, candidate=tuple(partial if item is quality else item for item in report.candidate))
    assert evaluate_metric_gate(regression, report, policy).status is MetricGateStatus.FAIL
    paired = next(item for item in report.paired if item.definition.metric_id == "eval")
    regression_policy = MetricPolicy("test-fixture", "2", (MetricRule(
        MetricRuleKind.MAX_REGRESSION_DELTA, "eval", paired.definition.metric_version,
        paired.definition.metric_definition_digest, 0.1,
    ),))
    report = replace(report, paired=tuple(replace(paired, paired_delta=-0.2) if item is paired else item
                                          for item in report.paired))
    assert evaluate_metric_gate(regression, report, regression_policy).status is MetricGateStatus.FAIL


@pytest.mark.asyncio
async def test_completion_uses_expected_objects_and_wp1_retry_history() -> None:
    baseline_id, candidate_id = uuid4(), uuid4()
    run = make_run(baseline_id)
    selected = tuple({"case_id": f"case-{letter}", "version": "v1"} for letter in "abcdefg")
    runs = {
        baseline_id: replace(run, suite_snapshot={**dict(run.suite_snapshot), "selected_cases": selected}),
        candidate_id: replace(make_run(candidate_id),
                              suite_snapshot={**dict(run.suite_snapshot), "selected_cases": selected}),
    }
    persistence = FakePersistence(runs, {baseline_id: (), candidate_id: ()})
    states = dict(zip("abcde", (OutcomeKind.SUCCESS, OutcomeKind.FAILURE, OutcomeKind.TIMEOUT,
                                OutcomeKind.CANCELLED, OutcomeKind.OUTCOME_UNKNOWN), strict=True))
    for run_id in runs:
        altered = []
        for attempt in persistence.attempts[run_id]:
            letter = attempt.case_ref.case_id[-1]
            if letter == "f":
                continue
            if letter in states:
                attempt = replace(attempt, execution_outcome_kind=states[letter])
            altered.append(attempt)
            if letter == "g":
                failed = replace(attempt, execution_outcome_kind=OutcomeKind.FAILURE)
                altered[-1] = failed
                altered.append(failed.build_retry(attempt_id=uuid4(), request_id=str(uuid4()),
                                                  created_at=failed.created_at))
        persistence.attempts[run_id] = tuple(altered)

    async def latest(project_id, run_id):
        by_case = {}
        for attempt in persistence.attempts[run_id]:
            by_case[attempt.case_ref.case_id] = attempt
        return tuple(by_case.values())

    persistence.list_latest_attempts = latest
    comparison = await EvaluationComparisonService(persistence).compare_runs(PROJECT_ID, baseline_id, candidate_id)
    report = build_metric_report(comparison, persistence.attempts[baseline_id],
                                 persistence.attempts[candidate_id], (), ())
    metrics = {item.definition.metric_id: item for item in report.candidate}
    for name in ("successful_completion_rate", "timeout_rate", "cancelled_rate",
                 "outcome_unknown_rate", "execution_attempt_retry_rate"):
        assert metrics[name].denominator == 7
        assert metrics[name].numerator == 1
        assert metrics[name].value == 1 / 7
    assert metrics["terminalization_rate"].numerator == 5
    assert metrics["outcome_unknown_rate"].numerator == 1


@pytest.mark.asyncio
async def test_new_removed_case_mix_stays_out_of_paired_delta() -> None:
    baseline_id, candidate_id = uuid4(), uuid4()
    runs = {baseline_id: make_run(baseline_id), candidate_id: make_run(candidate_id)}
    def result(run_id, case_id, score):
        return make_result(run_id=run_id, case_id=case_id, case_version="v1",
                           evaluator_id="eval", evaluator_version="v1", verdict=EvaluationVerdict.PASS,
                           score=score)
    persistence = FakePersistence(runs, {
        baseline_id: (result(baseline_id, "case-a", 0.1), result(baseline_id, "case-b", 1.0)),
        candidate_id: (result(candidate_id, "case-a", 0.2), result(candidate_id, "case-c", 0.0)),
    })
    comparison = await EvaluationComparisonService(persistence).compare_runs(PROJECT_ID, baseline_id, candidate_id)
    report = build_metric_report(comparison, persistence.attempts[baseline_id],
                                 persistence.attempts[candidate_id], persistence.results[baseline_id],
                                 persistence.results[candidate_id])
    paired = next(item for item in report.paired if item.definition.metric_id == "eval")
    assert paired.valid_count == 1
    assert paired.paired_delta == pytest.approx(0.1)
    assert dict(paired.exclusion_reasons)["NEW_OR_REMOVED"] == 2
    assert next(item for item in report.baseline if item.definition.metric_id == "eval").value == 0.55
    assert next(item for item in report.candidate if item.definition.metric_id == "eval").value == 0.1


@pytest.mark.asyncio
async def test_metric_version_drift_excludes_paired_delta() -> None:
    baseline_id, candidate_id = uuid4(), uuid4()
    runs = {baseline_id: make_run(baseline_id),
            candidate_id: make_run(candidate_id, specs=(spec("eval", evaluator_version="v2"),))}
    persistence = FakePersistence(runs, {
        baseline_id: (make_result(run_id=baseline_id, case_id="case-a", case_version="v1",
                                  evaluator_id="eval", evaluator_version="v1", verdict=EvaluationVerdict.PASS,
                                  score=0.2),),
        candidate_id: (make_result(run_id=candidate_id, case_id="case-a", case_version="v1",
                                   evaluator_id="eval", evaluator_version="v2", verdict=EvaluationVerdict.PASS,
                                   score=0.9),),
    })
    comparison = await EvaluationComparisonService(persistence).compare_runs(PROJECT_ID, baseline_id, candidate_id)
    report = build_metric_report(comparison, persistence.attempts[baseline_id],
                                 persistence.attempts[candidate_id], persistence.results[baseline_id],
                                 persistence.results[candidate_id])
    paired = next(item for item in report.paired if item.definition.metric_id == "eval")
    assert paired.paired_delta is None
    assert dict(paired.exclusion_reasons)["METRIC_NOT_COMPARABLE"] == 1


@pytest.mark.asyncio
async def test_execution_grain_does_not_multiply_by_evaluator_count() -> None:
    baseline_id, candidate_id = uuid4(), uuid4()
    runs = {run_id: make_run(run_id, specs=(spec("a"), spec("b")))
            for run_id in (baseline_id, candidate_id)}
    results = {run_id: tuple(make_result(
        run_id=run_id, case_id="case-a", case_version="v1", evaluator_id=evaluator_id,
        evaluator_version="v1", verdict=EvaluationVerdict.PASS, score=1.0,
    ) for evaluator_id in ("a", "b")) for run_id in runs}
    persistence = FakePersistence(runs, results)
    comparison = await EvaluationComparisonService(persistence).compare_runs(PROJECT_ID, baseline_id, candidate_id)
    report = build_metric_report(comparison, persistence.attempts[baseline_id],
                                 persistence.attempts[candidate_id], persistence.results[baseline_id],
                                 persistence.results[candidate_id])
    absolute = {item.definition.metric_id: item for item in report.candidate}
    paired = {item.definition.metric_id: item for item in report.paired}
    assert absolute["successful_completion_rate"].denominator == 1
    assert absolute["evaluator_error_rate"].denominator == 2
    assert paired["successful_completion_rate"].expected_count == 1
    assert paired["evaluator_error_rate"].expected_count == 2


@pytest.mark.asyncio
async def test_required_process_evidence_unavailable_does_not_count_as_quality_zero() -> None:
    baseline_id, candidate_id = uuid4(), uuid4()
    required = replace(spec("eval"), required_evidence_kinds=("process_trajectory",),
                       config_snapshot={"process_evidence_requirements": {
                           "schema_version": "process-evidence-requirements.v1",
                           "trajectory_schema_version": "stage11.wp4.v1", "requirements": [],
                       }})
    runs = {run_id: make_run(run_id, specs=(required,)) for run_id in (baseline_id, candidate_id)}
    results = {run_id: (make_result(run_id=run_id, case_id="case-a", case_version="v1",
                                    evaluator_id="eval", evaluator_version="v1",
                                    verdict=EvaluationVerdict.PASS, score=1.0),) for run_id in runs}
    persistence = FakePersistence(runs, results)
    comparison = await EvaluationComparisonService(persistence).compare_runs(PROJECT_ID, baseline_id, candidate_id)
    report = build_metric_report(comparison, persistence.attempts[baseline_id],
                                 persistence.attempts[candidate_id], persistence.results[baseline_id],
                                 persistence.results[candidate_id])
    absolute = {item.definition.metric_id: item for item in report.candidate}
    assert absolute["required_evidence_availability_rate"].value == 0
    assert absolute["eval"].value is None
    assert absolute["eval"].coverage.unavailable_count == 1
