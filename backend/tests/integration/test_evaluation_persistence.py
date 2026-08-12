"""真实 PostgreSQL 上的 WP3 ownership、race 与 transaction tests。"""

# ruff: noqa: D415

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.evaluation import (
    ArtifactRef, CaseVersionRef, DatasetVersion, EvaluationPolicy, EvaluationResult, EvaluationSuiteVersion,
    EvaluationVerdict, EvaluatorKind, EvaluatorSpec, ExecutionOutcome, ExecutionTargetRef, OutcomeKind,
    ProvenanceCompleteness, ScoreDirection, TestCaseVersion as CaseVersion, VersionRef,
)
from app.core.evaluation.run_attempts import (
    AttemptClaimLost, AttemptStatus, EvaluationEntityNotFound, ResultAlreadyFinalized, RetryAlreadyCreated,
)
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.models import EvaluationResultModel, EvaluationRunModel, ExecutionAttemptModel
from app.infrastructure.db.repositories.evaluation_persistence_repo import PostgresEvaluationPersistenceUnitOfWork
from app.services.evaluation import EvaluationPersistenceService
from tests.integration.conftest import TEST_PROJECT_ID

NOW = datetime.now(timezone.utc)


def service() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory))


async def seed_run(project_id: UUID = TEST_PROJECT_ID):
    ref = CaseVersionRef("case", "v1")
    case = CaseVersion("case", "v1", "case", {"q": 1}, NOW)
    dataset = DatasetVersion("dataset", "d1", "dataset", NOW, case_version_refs=(ref,))
    spec = EvaluatorSpec("eval", "e1", EvaluatorKind.DETERMINISTIC, VersionRef("cfg", "1"), ScoreDirection.HIGHER_IS_BETTER, score_range=(0, 1))
    suite = EvaluationSuiteVersion("suite", "s1", (ref,), (spec,), EvaluationPolicy(), NOW)
    return await service().create_run(
        project_id=project_id, dataset=dataset, suite=suite, cases={ref: case},
        target=ExecutionTargetRef("target", "FIXTURE", VersionRef("git", "abc")), timeout=timedelta(seconds=30),
    )


async def terminal_success(project_id: UUID = TEST_PROJECT_ID):
    run, (attempt,) = await seed_run(project_id)
    claimed = await service().claim_attempt(project_id, attempt.attempt_id, lease=timedelta(minutes=5))
    assert claimed.claimed and claimed.claim_token
    running = await service().start_attempt(project_id, attempt.attempt_id, claimed.claim_token)
    outcome = ExecutionOutcome(
        request_id=running.execution_request.request_id, kind=OutcomeKind.SUCCESS, started_at=NOW,
        finished_at=NOW, output_artifact_ref=ArtifactRef("artifact"),
    )
    terminal = await service().record_outcome(project_id, attempt.attempt_id, claimed.claim_token, outcome)
    return run, terminal, claimed.claim_token


def result_for(run, attempt, *, result_id=None):
    return EvaluationResult(
        result_id=str(result_id or uuid4()), run_id=str(run.run_id), attempt_id=str(attempt.attempt_id),
        dataset_id="dataset", dataset_version="d1", case_id="case", case_version="v1",
        suite_id="suite", suite_version="s1", evaluator_id="eval", evaluator_version="e1",
        config_ref=VersionRef("cfg", "1"), execution_target_id="target",
        execution_request_id=attempt.execution_request.request_id, verdict=EvaluationVerdict.FAIL,
        reason="wrong answer", provenance_completeness=ProvenanceCompleteness.COMPLETE,
        target_version_ref=VersionRef("git", "abc"), score=0.0, created_at=NOW,
    )


@pytest.mark.asyncio
async def test_atomic_claim_race_has_exactly_one_owner(db_session):
    _, (attempt,) = await seed_run()
    results = await asyncio.gather(
        service().claim_attempt(TEST_PROJECT_ID, attempt.attempt_id, lease=timedelta(minutes=5), worker_ref="a"),
        service().claim_attempt(TEST_PROJECT_ID, attempt.attempt_id, lease=timedelta(minutes=5), worker_ref="b"),
    )
    assert sum(item.claimed for item in results) == 1
    assert sum(not item.claimed for item in results) == 1


@pytest.mark.asyncio
async def test_wrong_token_fails_start_outcome_and_result(db_session):
    run, (attempt,) = await seed_run()
    claimed = await service().claim_attempt(TEST_PROJECT_ID, attempt.attempt_id, lease=timedelta(minutes=5))
    wrong = uuid4()
    with pytest.raises(AttemptClaimLost):
        await service().start_attempt(TEST_PROJECT_ID, attempt.attempt_id, wrong)
    running = await service().start_attempt(TEST_PROJECT_ID, attempt.attempt_id, claimed.claim_token)
    outcome = ExecutionOutcome(running.execution_request.request_id, OutcomeKind.SUCCESS, NOW, NOW, ArtifactRef("artifact"))
    with pytest.raises(AttemptClaimLost):
        await service().record_outcome(TEST_PROJECT_ID, attempt.attempt_id, wrong, outcome)
    terminal = await service().record_outcome(TEST_PROJECT_ID, attempt.attempt_id, claimed.claim_token, outcome)
    with pytest.raises(AttemptClaimLost):
        await service().finalize_result(TEST_PROJECT_ID, attempt.attempt_id, wrong, result_for(run, terminal))


@pytest.mark.asyncio
async def test_stale_reconcile_and_late_worker_race_only_one_terminal_write(db_session):
    _, (attempt,) = await seed_run()
    claimed = await service().claim_attempt(TEST_PROJECT_ID, attempt.attempt_id, lease=timedelta(minutes=5))
    running = await service().start_attempt(TEST_PROJECT_ID, attempt.attempt_id, claimed.claim_token)
    async with async_session_factory() as session:
        await session.execute(update(ExecutionAttemptModel).where(ExecutionAttemptModel.id == attempt.attempt_id).values(lease_expires_at=func.current_timestamp() - timedelta(seconds=1)))
        await session.commit()
    outcome = ExecutionOutcome(running.execution_request.request_id, OutcomeKind.SUCCESS, NOW, NOW, ArtifactRef("artifact"))
    writes = await asyncio.gather(
        service().record_outcome(TEST_PROJECT_ID, attempt.attempt_id, claimed.claim_token, outcome),
        service().reconcile_stale(TEST_PROJECT_ID, attempt.attempt_id, reason="lease expired"),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in writes) == 1
    assert sum(isinstance(item, AttemptClaimLost) for item in writes) == 1


@pytest.mark.asyncio
async def test_duplicate_result_race_is_insert_only(db_session):
    run, attempt, token = await terminal_success()
    values = [result_for(run, attempt), result_for(run, attempt)]
    writes = await asyncio.gather(
        service().finalize_result(TEST_PROJECT_ID, attempt.attempt_id, token, values[0]),
        service().finalize_result(TEST_PROJECT_ID, attempt.attempt_id, token, values[1]),
        return_exceptions=True,
    )
    assert sum(item is None for item in writes) == 1
    assert sum(isinstance(item, ResultAlreadyFinalized) for item in writes) == 1
    async with async_session_factory() as session:
        rows = (await session.execute(select(EvaluationResultModel))).scalars().all()
        assert len(rows) == 1 and rows[0].verdict == "FAIL"


@pytest.mark.asyncio
async def test_retry_race_creates_one_child_and_preserves_source(db_session):
    run, (attempt,) = await seed_run()
    claimed = await service().claim_attempt(TEST_PROJECT_ID, attempt.attempt_id, lease=timedelta(minutes=5))
    running = await service().start_attempt(TEST_PROJECT_ID, attempt.attempt_id, claimed.claim_token)
    failed = ExecutionOutcome(running.execution_request.request_id, OutcomeKind.FAILURE, NOW, NOW, error_category="TARGET", reason="failed")
    await service().record_outcome(TEST_PROJECT_ID, attempt.attempt_id, claimed.claim_token, failed)
    retries = await asyncio.gather(
        service().retry_attempt(TEST_PROJECT_ID, attempt.attempt_id),
        service().retry_attempt(TEST_PROJECT_ID, attempt.attempt_id), return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in retries) == 1
    assert sum(isinstance(item, RetryAlreadyCreated) for item in retries) == 1
    async with async_session_factory() as session:
        rows = (await session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.run_id == run.run_id))).scalars().all()
        assert len(rows) == 2
        source = next(row for row in rows if row.id == attempt.attempt_id)
        child = next(row for row in rows if row.id != attempt.attempt_id)
        assert source.execution_outcome_kind == "FAILURE" and child.retry_of_attempt_id == source.id


@pytest.mark.asyncio
async def test_cross_project_operations_fail_closed(db_session):
    run, (attempt,) = await seed_run()
    foreign = uuid4()
    assert not (await service().claim_attempt(foreign, attempt.attempt_id, lease=timedelta(minutes=5))).claimed
    with pytest.raises(AttemptClaimLost):
        await service().start_attempt(foreign, attempt.attempt_id, uuid4())
    with pytest.raises(EvaluationEntityNotFound):
        await service().retry_attempt(foreign, attempt.attempt_id)
    async with async_session_factory() as session:
        row = (await session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.id == attempt.attempt_id))).scalar_one()
        persisted_run = (await session.execute(select(EvaluationRunModel).where(EvaluationRunModel.id == run.run_id))).scalar_one()
        assert row.status == "PENDING" and persisted_run.status == "PENDING"


@pytest.mark.asyncio
async def test_create_run_attempt_failure_rolls_back_run(db_session):
    run, (attempt,) = await seed_run()
    # 验证数据库约束拒绝同 Run/Case/#1，并确认显式事务 rollback 不留下新 Run。
    candidate_run = uuid4()
    async with async_session_factory() as session:
        async with session.begin():
            original = (await session.execute(select(EvaluationRunModel).where(EvaluationRunModel.id == run.run_id))).scalar_one()
            session.add(EvaluationRunModel(
                id=candidate_run, project_id=original.project_id, dataset_id=original.dataset_id,
                dataset_version=original.dataset_version, suite_id=original.suite_id, suite_version=original.suite_version,
                execution_target_id=original.execution_target_id, execution_target_kind=original.execution_target_kind,
                target_version_kind=original.target_version_kind, target_version_value=original.target_version_value,
                dataset_snapshot=original.dataset_snapshot, suite_snapshot=original.suite_snapshot,
                execution_target_snapshot=original.execution_target_snapshot, status="PENDING", metadata_json={}, created_at=NOW,
            ))
            source = (await session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.id == attempt.attempt_id))).scalar_one()
            common = dict(
                project_id=source.project_id, run_id=candidate_run, case_id=source.case_id, case_version=source.case_version,
                attempt_no=1, execution_target_id=source.execution_target_id,
                execution_target_kind=source.execution_target_kind, target_version_kind=source.target_version_kind,
                target_version_value=source.target_version_value, target_config_kind=source.target_config_kind,
                target_config_value=source.target_config_value, execution_request_id="duplicate-request",
                idempotency_key=source.idempotency_key, request_snapshot=source.request_snapshot,
                status="PENDING", created_at=NOW, outcome_evidence_refs=[], outcome_metadata={},
            )
            session.add_all([ExecutionAttemptModel(id=uuid4(), **common), ExecutionAttemptModel(id=uuid4(), **common)])
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
    async with async_session_factory() as session:
        assert (await session.execute(select(EvaluationRunModel).where(EvaluationRunModel.id == candidate_run))).scalar_one_or_none() is None
