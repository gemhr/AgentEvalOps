"""真实 PostgreSQL 上的 WP1 Regression Comparison 闭环 tests。"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.core.evaluation import (
    ArtifactRef,
    CaseVersionRef,
    DatasetVersion,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationSuiteVersion,
    EvaluationVerdict,
    EvaluatorKind,
    EvaluatorSpec,
    ExecutionOutcome,
    ExecutionTargetRef,
    OutcomeKind,
    ProvenanceCompleteness,
    RegressionClassification,
    RunStatus,
    ScoreDirection,
    TestCaseVersion as CaseVersion,
    VersionRef,
)
from app.core.evaluation.comparison import RunsNotComparable
from app.core.evaluation.run_attempts import EvaluationEntityNotFound
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.models import ProjectModel
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)
from app.services.evaluation import EvaluationComparisonService, EvaluationPersistenceService
from tests.integration.conftest import TEST_ORG_ID, TEST_PROJECT_ID

NOW = datetime.now(timezone.utc)


def service() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory))


async def seed_two_case_run(
    project_id: UUID,
    *,
    dataset_id: str = "dataset",
    suite_id: str = "suite",
    target_version: VersionRef = VersionRef("git", "abc"),
):
    ref_a = CaseVersionRef("case-a", "v1")
    ref_b = CaseVersionRef("case-b", "v1")
    case_a = CaseVersion("case-a", "v1", "case-a", {"q": 1}, NOW)
    case_b = CaseVersion("case-b", "v1", "case-b", {"q": 2}, NOW)
    dataset = DatasetVersion(dataset_id, "d1", "dataset", NOW, case_version_refs=(ref_a, ref_b))
    spec = EvaluatorSpec(
        "eval",
        "e1",
        EvaluatorKind.DETERMINISTIC,
        VersionRef("cfg", "1"),
        ScoreDirection.HIGHER_IS_BETTER,
        config_snapshot={"threshold": 0.5},
        score_range=(0.0, 1.0),
        prompt_ref=VersionRef("prompt", "p1"),
    )
    suite = EvaluationSuiteVersion(suite_id, "s1", (ref_a, ref_b), (spec,), EvaluationPolicy(), NOW)
    return await service().create_run(
        project_id=project_id,
        dataset=dataset,
        suite=suite,
        cases={ref_a: case_a, ref_b: case_b},
        target=ExecutionTargetRef("target", "FIXTURE", target_version),
        timeout=timedelta(seconds=30),
    )


def result_for(
    run,
    attempt,
    *,
    verdict: EvaluationVerdict,
    score: float,
) -> EvaluationResult:
    evaluator = run.suite_snapshot["evaluators"][0]
    config_ref = evaluator["config_ref"]
    prompt_ref = evaluator["prompt_ref"]
    return EvaluationResult(
        result_id=str(uuid4()),
        run_id=str(run.run_id),
        attempt_id=str(attempt.attempt_id),
        dataset_id=str(run.dataset_snapshot["dataset_id"]),
        dataset_version=run.dataset_ref.opaque_value,
        case_id=attempt.case_ref.case_id,
        case_version=attempt.case_ref.version,
        suite_id=str(run.suite_snapshot["suite_id"]),
        suite_version=run.suite_ref.opaque_value,
        evaluator_id=str(evaluator["evaluator_id"]),
        evaluator_version=str(evaluator["evaluator_version"]),
        config_ref=VersionRef(str(config_ref["kind"]), str(config_ref["opaque_value"])),
        prompt_ref=None if prompt_ref is None else VersionRef(str(prompt_ref["kind"]), str(prompt_ref["opaque_value"])),
        execution_target_id=attempt.execution_target_ref.target_id,
        execution_request_id=attempt.execution_request.request_id,
        verdict=verdict,
        reason="evaluated",
        provenance_completeness=ProvenanceCompleteness.COMPLETE,
        target_version_ref=attempt.execution_target_ref.target_version_ref,
        output_artifact_ref=attempt.output_artifact_ref,
        score=score,
        created_at=NOW,
    )


async def complete_run_with_verdicts(
    project_id: UUID,
    run,
    attempts,
    verdicts: dict[str, EvaluationVerdict],
    scores: dict[str, float],
):
    """把 Run 推进到 COMPLETED：每个 attempt SUCCESS + 写入指定 verdict/score 的 Result。"""
    svc = service()
    for attempt in attempts:
        claimed = await svc.claim_attempt(project_id, attempt.attempt_id, lease=timedelta(minutes=5))
        assert claimed.claimed and claimed.claim_token
        running = await svc.start_attempt(project_id, attempt.attempt_id, claimed.claim_token)
        outcome = ExecutionOutcome(
            request_id=running.execution_request.request_id,
            kind=OutcomeKind.SUCCESS,
            started_at=NOW,
            finished_at=NOW,
            output_artifact_ref=ArtifactRef("artifact", "sha256:abc", "application/json"),
        )
        terminal = await svc.record_outcome(project_id, attempt.attempt_id, claimed.claim_token, outcome)
        result = result_for(
            run,
            terminal,
            verdict=verdicts[attempt.case_ref.case_id],
            score=scores[attempt.case_ref.case_id],
        )
        await svc.finalize_result(project_id, terminal.attempt_id, claimed.claim_token, result)
    status = await svc.finish_run(project_id, run.run_id)
    assert status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_real_postgres_comparison_loop(db_session) -> None:
    baseline_run, baseline_attempts = await seed_two_case_run(TEST_PROJECT_ID)
    candidate_run, candidate_attempts = await seed_two_case_run(
        TEST_PROJECT_ID,
        target_version=VersionRef("git", "def"),
    )
    await complete_run_with_verdicts(
        TEST_PROJECT_ID,
        baseline_run,
        baseline_attempts,
        {"case-a": EvaluationVerdict.PASS, "case-b": EvaluationVerdict.FAIL},
        {"case-a": 1.0, "case-b": 0.0},
    )
    await complete_run_with_verdicts(
        TEST_PROJECT_ID,
        candidate_run,
        candidate_attempts,
        {"case-a": EvaluationVerdict.FAIL, "case-b": EvaluationVerdict.PASS},
        {"case-a": 0.0, "case-b": 1.0},
    )
    comparison = await EvaluationComparisonService(service()).compare_runs(
        TEST_PROJECT_ID,
        baseline_run.run_id,
        candidate_run.run_id,
    )
    by_key = {(item.case_id, item.evaluator_id): item for item in comparison.comparisons}
    assert len(comparison.comparisons) == 2
    assert by_key[("case-a", "eval")].classification is RegressionClassification.REGRESSION
    assert by_key[("case-b", "eval")].classification is RegressionClassification.IMPROVEMENT
    # Baseline 与 Candidate 的 attempt_id 必然不同；alignment 成功即证明 attempt_id 不参与键。
    assert by_key[("case-a", "eval")].baseline_result_id != by_key[("case-a", "eval")].candidate_result_id
    # 版本差异保留为 provenance。
    assert comparison.baseline_provenance.target_version_ref == VersionRef("git", "abc")
    assert comparison.candidate_provenance.target_version_ref == VersionRef("git", "def")


@pytest.mark.asyncio
async def test_cross_project_comparison_fails_closed(db_session) -> None:
    baseline_run, baseline_attempts = await seed_two_case_run(TEST_PROJECT_ID)
    await complete_run_with_verdicts(
        TEST_PROJECT_ID,
        baseline_run,
        baseline_attempts,
        {"case-a": EvaluationVerdict.PASS, "case-b": EvaluationVerdict.FAIL},
        {"case-a": 1.0, "case-b": 0.0},
    )
    other_project_id = uuid4()
    db_session.add(
        ProjectModel(
            id=other_project_id,
            org_id=TEST_ORG_ID,
            name="Other Project",
            description="",
            created_at=NOW,
        )
    )
    await db_session.commit()
    other_run, _ = await seed_two_case_run(other_project_id)
    svc = EvaluationComparisonService(service())
    with pytest.raises(EvaluationEntityNotFound, match="run not found"):
        await svc.compare_runs(TEST_PROJECT_ID, baseline_run.run_id, other_run.run_id)


@pytest.mark.asyncio
async def test_dataset_suite_mismatch_fails_closed(db_session) -> None:
    baseline_run, baseline_attempts = await seed_two_case_run(TEST_PROJECT_ID)
    candidate_run, candidate_attempts = await seed_two_case_run(
        TEST_PROJECT_ID,
        suite_id="suite-other",
    )
    await complete_run_with_verdicts(
        TEST_PROJECT_ID,
        baseline_run,
        baseline_attempts,
        {"case-a": EvaluationVerdict.PASS, "case-b": EvaluationVerdict.FAIL},
        {"case-a": 1.0, "case-b": 0.0},
    )
    await complete_run_with_verdicts(
        TEST_PROJECT_ID,
        candidate_run,
        candidate_attempts,
        {"case-a": EvaluationVerdict.PASS, "case-b": EvaluationVerdict.FAIL},
        {"case-a": 1.0, "case-b": 0.0},
    )
    svc = EvaluationComparisonService(service())
    with pytest.raises(RunsNotComparable, match="suite identity mismatch"):
        await svc.compare_runs(TEST_PROJECT_ID, baseline_run.run_id, candidate_run.run_id)
