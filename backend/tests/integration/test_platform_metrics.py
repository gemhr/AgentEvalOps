"""真实 PostgreSQL 上的 WP5 只读 MetricReport 闭环。"""

# ruff: noqa: D102, D103, D415

import pytest

from app.core.evaluation.metric_policy import MetricGateStatus, evaluate_metric_gate
from app.core.evaluation.references import CaseVersionRef
from app.core.evaluation.results import EvaluationVerdict
from app.services.evaluation.comparison import EvaluationComparisonService
from app.services.evaluation.platform_metrics import PlatformMetricService
from app.services.evaluation.report import RegressionReportService
from tests.integration.conftest import TEST_PROJECT_ID
from tests.integration.test_evaluation_comparison import (
    complete_run_with_verdicts, seed_two_case_run, service,
)


@pytest.mark.asyncio
async def test_postgres_metric_report_paired_gate_read_only(db_session) -> None:
    baseline, baseline_attempts = await seed_two_case_run(TEST_PROJECT_ID)
    candidate, candidate_attempts = await seed_two_case_run(TEST_PROJECT_ID)
    await complete_run_with_verdicts(
        TEST_PROJECT_ID, baseline, baseline_attempts,
        {"case-a": EvaluationVerdict.PASS, "case-b": EvaluationVerdict.FAIL},
        {"case-a": 1.0, "case-b": 0.0},
    )
    await complete_run_with_verdicts(
        TEST_PROJECT_ID, candidate, candidate_attempts,
        {"case-a": EvaluationVerdict.FAIL, "case-b": EvaluationVerdict.PASS},
        {"case-a": 0.0, "case-b": 1.0},
    )
    persistence = service()
    comparison = await EvaluationComparisonService(persistence).compare_runs(
        TEST_PROJECT_ID, baseline.run_id, candidate.run_id,
    )
    before = (
        await persistence.list_results(TEST_PROJECT_ID, baseline.run_id),
        await persistence.list_results(TEST_PROJECT_ID, candidate.run_id),
    )
    metrics = await PlatformMetricService(persistence).build_report(comparison)
    after = (
        await persistence.list_results(TEST_PROJECT_ID, baseline.run_id),
        await persistence.list_results(TEST_PROJECT_ID, candidate.run_id),
    )
    assert before == after
    quality = next(item for item in metrics.candidate if item.definition.metric_id == "eval")
    assert quality.coverage.expected_count == 2
    assert quality.coverage.valid_count == 2
    assert quality.coverage.coverage == 1
    paired = next(item for item in metrics.paired if item.definition.metric_id == "eval")
    assert paired.valid_count == 2
    assert paired.paired_delta == 0
    regression = RegressionReportService().build_report(comparison, (CaseVersionRef("case-a", "v1"),))
    assert evaluate_metric_gate(regression, metrics, None).status is MetricGateStatus.FAIL
    assert comparison.semantic_digest == metrics.comparison_digest
