"""Closed-loop demo driver E2E on real PostgreSQL.

Proves the WP1 deliverable: one call to ``run_closed_loop_demo`` walks
SYNTHETIC failing Trace -> TraceFeedbackService -> TestCaseVersion/DatasetVersion
-> create_run/execute_attempt -> persisted Results (with Trace EvidenceRef)
-> EvaluationComparisonService -> RegressionReportService -> ReleaseDecision.
The gate decision must come from the frozen Report owner, and demo data must be
isolated in its own org/project with fresh UUIDs (repeatable).
"""

# ruff: noqa: D101, D102, D105, D415

from functools import partial

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.evaluation.comparison import RegressionClassification
from app.core.evaluation.report import ReleaseDecision
from app.core.evaluation.run_attempts import RunStatus
from app.core.online.entities import trace_evidence_ref
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.models import EvaluationRunModel, ProjectModel
from app.infrastructure.db.repositories.evaluation_persistence_repo import PostgresEvaluationPersistenceUnitOfWork
from app.services.evaluation import EvaluationPersistenceService
from scripts.demo.closed_loop_demo import (
    CASE_ROUTING,
    CASE_RAG,
    CASE_TOOL_CONTRACT,
    DemoResult,
    run_closed_loop_demo,
)

pytestmark = pytest.mark.asyncio


def _uow_factory() -> PostgresEvaluationPersistenceUnitOfWork:
    return PostgresEvaluationPersistenceUnitOfWork(async_session_factory)


def _persistence() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(partial(PostgresEvaluationPersistenceUnitOfWork, async_session_factory))


async def _count_rows(session: AsyncSession, model) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return result.scalar_one()


async def _classifications(result: DemoResult) -> dict[str, RegressionClassification]:
    return {item.case_id: item.classification for item in result.report.comparisons}


async def _case_c_result_evidence(session: AsyncSession, result: DemoResult) -> bool:
    """True if the Trace EvidenceRef reached the persisted result for the feedback case."""
    persistence = _persistence()
    for run_id in (result.baseline_run_id, result.candidate_run_id):
        for item in await persistence.list_results(result.project_id, run_id):
            if item.case_id == CASE_TOOL_CONTRACT:
                if trace_evidence_ref(result.trace_id) in item.evidence_refs:
                    return True
    return False


async def test_fail_scenario_gate_is_fail_with_critical_regression(db_session: AsyncSession) -> None:
    result = await run_closed_loop_demo(db_session, uow_factory=_uow_factory, scenario="fail")

    classifications = await _classifications(result)
    assert classifications == {
        CASE_ROUTING: RegressionClassification.UNCHANGED,
        CASE_RAG: RegressionClassification.IMPROVEMENT,
        CASE_TOOL_CONTRACT: RegressionClassification.REGRESSION,
    }
    assert result.report.unchanged_count == 1
    assert result.report.improvement_count == 1
    assert result.report.regression_count == 1
    assert result.report.not_comparable_count == 0
    assert [(item.case_id, item.case_version) for item in result.report.critical_regressions] == [
        (CASE_TOOL_CONTRACT, "v1")
    ]
    assert result.report.critical_not_comparable == ()
    assert result.report.release_decision is ReleaseDecision.FAIL

    # Both runs genuinely persisted and COMPLETED through the real loop.
    persistence = _persistence()
    for run_id in (result.baseline_run_id, result.candidate_run_id):
        assert (await persistence.get_run(result.project_id, run_id)).status is RunStatus.COMPLETED

    # Phase4 evidence propagation survived the round-trip.
    assert await _case_c_result_evidence(db_session, result) is True


async def test_pass_scenario_gate_is_pass_without_regression(db_session: AsyncSession) -> None:
    result = await run_closed_loop_demo(db_session, uow_factory=_uow_factory, scenario="pass")

    classifications = await _classifications(result)
    assert classifications == {
        CASE_ROUTING: RegressionClassification.UNCHANGED,
        CASE_RAG: RegressionClassification.IMPROVEMENT,
        CASE_TOOL_CONTRACT: RegressionClassification.UNCHANGED,
    }
    assert result.report.regression_count == 0
    assert result.report.critical_regressions == ()
    assert result.report.release_decision is ReleaseDecision.PASS


async def test_demo_data_is_isolated_and_repeatable(db_session: AsyncSession) -> None:
    before = await _count_rows(db_session, EvaluationRunModel)

    first = await run_closed_loop_demo(db_session, uow_factory=_uow_factory, scenario="fail")
    second = await run_closed_loop_demo(db_session, uow_factory=_uow_factory, scenario="pass")

    # Every run creates its own org/project/run UUIDs; no fixed-PK collisions.
    assert first.project_id != second.project_id
    assert first.org_id != second.org_id
    assert first.baseline_run_id != second.baseline_run_id
    assert first.candidate_run_id != second.candidate_run_id

    # Only the two demo projects own the new rows (isolation, not global state).
    demo_project_ids = {first.project_id, second.project_id}
    rows = (await db_session.execute(select(EvaluationRunModel.project_id))).scalars().all()
    assert len(rows) == before + 4
    assert set(rows) == demo_project_ids
    demo_projects = (
        await db_session.execute(select(ProjectModel).where(ProjectModel.id.in_(demo_project_ids)))
    ).scalars().all()
    assert {item.id for item in demo_projects} == demo_project_ids
    assert {item.org_id for item in demo_projects} == {first.org_id, second.org_id}

    # Deterministic gate decisions across repeats.
    assert first.report.release_decision is ReleaseDecision.FAIL
    assert second.report.release_decision is ReleaseDecision.PASS
