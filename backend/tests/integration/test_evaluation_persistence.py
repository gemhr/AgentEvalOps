"""真实 PostgreSQL 上的 WP3 ownership、race 与 transaction tests。"""

# ruff: noqa: D415

import asyncio
from dataclasses import replace
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
from app.infrastructure.db.models import EvaluationResultModel, EvaluationRunModel, ExecutionAttemptModel, ProjectModel
from app.infrastructure.db.repositories import evaluation_persistence_repo as persistence_repo
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
    PostgresExecutionAttemptRepository,
)
from app.services.evaluation import EvaluationPersistenceService
from tests.integration.conftest import TEST_ORG_ID, TEST_PROJECT_ID

NOW = datetime.now(timezone.utc)


def service() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory))


async def seed_run(project_id: UUID = TEST_PROJECT_ID):
    ref = CaseVersionRef("case", "v1")
    case = CaseVersion("case", "v1", "case", {"q": 1}, NOW)
    dataset = DatasetVersion("dataset", "d1", "dataset", NOW, case_version_refs=(ref,))
    spec = EvaluatorSpec(
        "eval", "e1", EvaluatorKind.DETERMINISTIC, VersionRef("cfg", "1"),
        ScoreDirection.HIGHER_IS_BETTER, config_snapshot={"threshold_source": "frozen"},
        score_range=(0, 1), prompt_ref=VersionRef("prompt", "p1"),
    )
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
        finished_at=NOW, output_artifact_ref=ArtifactRef("artifact", "sha256:abc", "application/json"),
    )
    terminal = await service().record_outcome(project_id, attempt.attempt_id, claimed.claim_token, outcome)
    return run, terminal, claimed.claim_token


async def terminal_failure(project_id: UUID = TEST_PROJECT_ID):
    run, (attempt,) = await seed_run(project_id)
    claimed = await service().claim_attempt(project_id, attempt.attempt_id, lease=timedelta(minutes=5))
    running = await service().start_attempt(project_id, attempt.attempt_id, claimed.claim_token)
    terminal = await service().record_outcome(
        project_id,
        attempt.attempt_id,
        claimed.claim_token,
        ExecutionOutcome(
            running.execution_request.request_id,
            OutcomeKind.FAILURE,
            NOW,
            NOW,
            error_category="TARGET",
            reason="failed",
        ),
    )
    return run, terminal


def result_for(run, attempt, *, result_id=None):
    evaluator = run.suite_snapshot["evaluators"][0]
    config_ref = evaluator["config_ref"]
    prompt_ref = evaluator["prompt_ref"]
    return EvaluationResult(
        result_id=str(result_id or uuid4()), run_id=str(run.run_id), attempt_id=str(attempt.attempt_id),
        dataset_id=str(run.dataset_snapshot["dataset_id"]), dataset_version=run.dataset_ref.opaque_value,
        case_id=attempt.case_ref.case_id, case_version=attempt.case_ref.version,
        suite_id=str(run.suite_snapshot["suite_id"]), suite_version=run.suite_ref.opaque_value,
        evaluator_id=str(evaluator["evaluator_id"]), evaluator_version=str(evaluator["evaluator_version"]),
        config_ref=VersionRef(str(config_ref["kind"]), str(config_ref["opaque_value"])),
        prompt_ref=None if prompt_ref is None else VersionRef(str(prompt_ref["kind"]), str(prompt_ref["opaque_value"])),
        execution_target_id=attempt.execution_target_ref.target_id,
        execution_request_id=attempt.execution_request.request_id, verdict=EvaluationVerdict.FAIL,
        reason="wrong answer", provenance_completeness=ProvenanceCompleteness.COMPLETE,
        target_version_ref=attempt.execution_target_ref.target_version_ref,
        output_artifact_ref=attempt.output_artifact_ref, score=0.0, created_at=NOW,
    )


def _row_snapshot(row) -> dict[str, object]:
    """复制 ORM row 的全部持久化列，避免 identity map 掩盖数据库变化。"""
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _sorted_snapshots(rows) -> list[dict[str, object]]:
    """按 UUID identity 稳定保存一组 authoritative rows。"""
    return sorted((_row_snapshot(row) for row in rows), key=lambda item: str(item["id"]))


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
@pytest.mark.parametrize(
    "outcome_kind",
    [OutcomeKind.FAILURE, OutcomeKind.TIMEOUT, OutcomeKind.CANCELLED, OutcomeKind.OUTCOME_UNKNOWN],
)
async def test_non_success_outcome_persists_sql_null_artifact(db_session, outcome_kind):
    _, (attempt,) = await seed_run()
    claimed = await service().claim_attempt(TEST_PROJECT_ID, attempt.attempt_id, lease=timedelta(minutes=5))
    running = await service().start_attempt(TEST_PROJECT_ID, attempt.attempt_id, claimed.claim_token)
    error_category = f"{outcome_kind.value}_ERROR"
    reason = f"observed {outcome_kind.value.lower()}"

    terminal = await service().record_outcome(
        TEST_PROJECT_ID,
        attempt.attempt_id,
        claimed.claim_token,
        ExecutionOutcome(
            running.execution_request.request_id,
            outcome_kind,
            NOW,
            NOW,
            error_category=error_category,
            reason=reason,
        ),
    )

    assert terminal.status is AttemptStatus.TERMINAL
    assert terminal.execution_outcome_kind is outcome_kind
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(ExecutionAttemptModel).where(ExecutionAttemptModel.id == attempt.attempt_id)
            )
        ).scalar_one()
        artifact_is_sql_null = (
            await session.execute(
                select(ExecutionAttemptModel.output_artifact_ref.is_(None)).where(
                    ExecutionAttemptModel.id == attempt.attempt_id
                )
            )
        ).scalar_one()
        assert row.status == AttemptStatus.TERMINAL.value
        assert row.execution_outcome_kind == outcome_kind.value
        assert row.error_category == error_category
        assert row.reason == reason
        assert artifact_is_sql_null is True


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
@pytest.mark.parametrize(
    ("constraint_name", "intent_matches", "expected_exception"),
    [
        ("uq_evaluation_attempts_direct_retry", True, RetryAlreadyCreated),
        ("uq_evaluation_attempts_case_number", True, RetryAlreadyCreated),
        ("uq_evaluation_attempts_direct_retry", False, IntegrityError),
        ("uq_evaluation_attempts_case_number", False, IntegrityError),
        ("uq_unrelated_constraint", True, IntegrityError),
    ],
    ids=[
        "direct-uq-matching-intent",
        "case-number-uq-matching-intent",
        "direct-uq-mismatched-intent",
        "case-number-uq-mismatched-intent",
        "unknown-constraint",
    ],
)
async def test_retry_conflict_mapping_is_exact_and_savepoint_recovers_session(
    db_session,
    monkeypatch,
    constraint_name,
    intent_matches,
    expected_exception,
):
    run, source = await terminal_failure()
    winner = await service().retry_attempt(TEST_PROJECT_ID, source.attempt_id)
    candidate = source.build_retry(attempt_id=uuid4(), request_id=str(uuid4()), created_at=NOW)
    if not intent_matches:
        candidate = replace(
            candidate,
            execution_target_ref=ExecutionTargetRef(
                "different-target",
                candidate.execution_target_ref.target_kind,
                candidate.execution_target_ref.target_version_ref,
                config_ref=candidate.execution_target_ref.config_ref,
            ),
        )

    monkeypatch.setattr(persistence_repo, "_retry_constraint_name", lambda _: constraint_name)
    async with async_session_factory() as session:
        repository = PostgresExecutionAttemptRepository(session)
        with pytest.raises(expected_exception):
            await repository.create_retry(candidate)

        rows = (
            await session.execute(
                select(ExecutionAttemptModel).where(ExecutionAttemptModel.run_id == run.run_id)
            )
        ).scalars().all()
        assert len(rows) == 2
        assert {row.id for row in rows} == {source.attempt_id, winner.attempt_id}
        stored_source = next(row for row in rows if row.id == source.attempt_id)
        stored_child = next(row for row in rows if row.id == winner.attempt_id)
        assert stored_source.execution_outcome_kind == OutcomeKind.FAILURE.value
        assert stored_child.retry_of_attempt_id == source.attempt_id


@pytest.mark.asyncio
async def test_legal_retry_preserves_other_successful_attempt_result(db_session):
    refs = (CaseVersionRef("retry-case", "v1"), CaseVersionRef("success-case", "v1"))
    cases = {ref: CaseVersion(ref.case_id, ref.version, ref.case_id, {"case": ref.case_id}, NOW) for ref in refs}
    dataset = DatasetVersion("dataset", "d1", "dataset", NOW, case_version_refs=refs)
    spec = EvaluatorSpec(
        "eval", "e1", EvaluatorKind.DETERMINISTIC, VersionRef("cfg", "1"),
        ScoreDirection.HIGHER_IS_BETTER, prompt_ref=VersionRef("prompt", "p1"),
    )
    suite = EvaluationSuiteVersion("suite", "s1", refs, (spec,), EvaluationPolicy(), NOW)
    run, attempts = await service().create_run(
        project_id=TEST_PROJECT_ID, dataset=dataset, suite=suite, cases=cases,
        target=ExecutionTargetRef("target", "FIXTURE", VersionRef("git", "abc")),
        timeout=timedelta(seconds=30),
    )
    retry_source = next(item for item in attempts if item.case_ref.case_id == "retry-case")
    successful = next(item for item in attempts if item.case_ref.case_id == "success-case")
    retry_claim = await service().claim_attempt(TEST_PROJECT_ID, retry_source.attempt_id, lease=timedelta(minutes=5))
    retry_running = await service().start_attempt(TEST_PROJECT_ID, retry_source.attempt_id, retry_claim.claim_token)
    await service().record_outcome(
        TEST_PROJECT_ID, retry_source.attempt_id, retry_claim.claim_token,
        ExecutionOutcome(
            retry_running.execution_request.request_id, OutcomeKind.FAILURE, NOW, NOW,
            error_category="TARGET", reason="retryable",
        ),
    )
    success_claim = await service().claim_attempt(TEST_PROJECT_ID, successful.attempt_id, lease=timedelta(minutes=5))
    success_running = await service().start_attempt(TEST_PROJECT_ID, successful.attempt_id, success_claim.claim_token)
    successful = await service().record_outcome(
        TEST_PROJECT_ID, successful.attempt_id, success_claim.claim_token,
        ExecutionOutcome(
            success_running.execution_request.request_id, OutcomeKind.SUCCESS, NOW, NOW,
            ArtifactRef("preserved", "sha256:preserved"),
        ),
    )
    result = result_for(run, successful)
    await service().finalize_result(TEST_PROJECT_ID, successful.attempt_id, success_claim.claim_token, result)
    async with async_session_factory() as session:
        successful_before = (
            await session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.id == successful.attempt_id))
        ).scalar_one()
        source_before = (
            await session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.id == retry_source.attempt_id))
        ).scalar_one()
        result_before = (
            await session.execute(select(EvaluationResultModel).where(EvaluationResultModel.id == UUID(result.result_id)))
        ).scalar_one()
        successful_snapshot = _row_snapshot(successful_before)
        source_snapshot = _row_snapshot(source_before)
        result_snapshot = _row_snapshot(result_before)
    child = await service().retry_attempt(TEST_PROJECT_ID, retry_source.attempt_id)
    async with async_session_factory() as session:
        successful_row = (
            await session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.id == successful.attempt_id))
        ).scalar_one()
        source_row = (
            await session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.id == retry_source.attempt_id))
        ).scalar_one()
        result_row = (
            await session.execute(select(EvaluationResultModel).where(EvaluationResultModel.id == UUID(result.result_id)))
        ).scalar_one()
        child_row = (
            await session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.id == child.attempt_id))
        ).scalar_one()
        assert _row_snapshot(successful_row) == successful_snapshot
        assert _row_snapshot(source_row) == source_snapshot
        assert _row_snapshot(result_row) == result_snapshot
        assert child_row.id != successful_row.id
        assert child_row.id != source_row.id
        assert child_row.execution_request_id != source_row.execution_request_id
        assert child_row.retry_of_attempt_id == source_row.id
        assert child_row.attempt_no == source_row.attempt_no + 1
        assert child_row.idempotency_key == source_row.idempotency_key
        assert child_row.status == "PENDING"


@pytest.mark.asyncio
async def test_cross_project_operations_fail_closed(db_session):
    run, (attempt,) = await seed_run()
    foreign = uuid4()
    async with async_session_factory() as session:
        run_before = (
            await session.execute(select(EvaluationRunModel).where(EvaluationRunModel.id == run.run_id))
        ).scalar_one()
        attempt_before = (
            await session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.id == attempt.attempt_id))
        ).scalar_one()
        results_before = (
            await session.execute(
                select(EvaluationResultModel).where(EvaluationResultModel.run_id == run.run_id)
            )
        ).scalars().all()
        children_before = (
            await session.execute(
                select(ExecutionAttemptModel).where(
                    ExecutionAttemptModel.run_id == run.run_id,
                    ExecutionAttemptModel.retry_of_attempt_id == attempt.attempt_id,
                )
            )
        ).scalars().all()
        run_snapshot = _row_snapshot(run_before)
        attempt_snapshot = _row_snapshot(attempt_before)
        result_snapshots = _sorted_snapshots(results_before)
        child_snapshots = _sorted_snapshots(children_before)
        assert result_snapshots == []
        assert child_snapshots == []

    async with PostgresEvaluationPersistenceUnitOfWork(async_session_factory) as uow:
        assert await uow.runs.get_run(foreign, run.run_id) is None
        assert await uow.attempts.get_attempt(foreign, attempt.attempt_id) is None
        assert await uow.attempts.list_attempts(foreign, run.run_id) == ()
    assert not (await service().claim_attempt(foreign, attempt.attempt_id, lease=timedelta(minutes=5))).claimed
    with pytest.raises(AttemptClaimLost):
        await service().start_attempt(foreign, attempt.attempt_id, uuid4())
    with pytest.raises(EvaluationEntityNotFound):
        await service().record_outcome(
            foreign, attempt.attempt_id, uuid4(),
            ExecutionOutcome("foreign-request", OutcomeKind.FAILURE, NOW, NOW, error_category="X", reason="foreign"),
        )
    with pytest.raises(EvaluationEntityNotFound):
        await service().retry_attempt(foreign, attempt.attempt_id)
    with pytest.raises(AttemptClaimLost):
        await service().reconcile_stale(foreign, attempt.attempt_id, reason="foreign")
    with pytest.raises(EvaluationEntityNotFound):
        await service().finalize_result(foreign, attempt.attempt_id, uuid4(), result_for(run, attempt))
    async with async_session_factory() as session:
        run_after = (
            await session.execute(select(EvaluationRunModel).where(EvaluationRunModel.id == run.run_id))
        ).scalar_one()
        attempt_after = (
            await session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.id == attempt.attempt_id))
        ).scalar_one()
        results_after = (
            await session.execute(
                select(EvaluationResultModel).where(EvaluationResultModel.run_id == run.run_id)
            )
        ).scalars().all()
        children_after = (
            await session.execute(
                select(ExecutionAttemptModel).where(
                    ExecutionAttemptModel.run_id == run.run_id,
                    ExecutionAttemptModel.retry_of_attempt_id == attempt.attempt_id,
                )
            )
        ).scalars().all()
        assert _row_snapshot(run_after) == run_snapshot
        assert _row_snapshot(attempt_after) == attempt_snapshot
        assert _sorted_snapshots(results_after) == result_snapshots
        assert _sorted_snapshots(children_after) == child_snapshots


@pytest.mark.asyncio
async def test_cross_project_result_ownership_is_rejected_by_database(db_session):
    run, attempt, _ = await terminal_success()
    foreign = uuid4()
    db_session.add(ProjectModel(id=foreign, org_id=TEST_ORG_ID, name="Foreign", description="", created_at=NOW))
    await db_session.commit()
    result = result_for(run, attempt)
    db_session.add(
        EvaluationResultModel(
            id=UUID(result.result_id), project_id=foreign, run_id=run.run_id, attempt_id=attempt.attempt_id,
            dataset_id=result.dataset_id, dataset_version=result.dataset_version,
            case_id=result.case_id, case_version=result.case_version,
            suite_id=result.suite_id, suite_version=result.suite_version,
            evaluator_id=result.evaluator_id, evaluator_version=result.evaluator_version,
            config_ref_kind=result.config_ref.kind, config_ref_value=result.config_ref.opaque_value,
            prompt_ref_kind=result.prompt_ref.kind, prompt_ref_value=result.prompt_ref.opaque_value,
            execution_target_id=result.execution_target_id,
            target_version_kind=result.target_version_ref.kind,
            target_version_value=result.target_version_ref.opaque_value,
            execution_request_id=result.execution_request_id, verdict=result.verdict.value,
            reason=result.reason, provenance_completeness=result.provenance_completeness.value,
            output_artifact_ref={"artifact_id": result.output_artifact_ref.artifact_id},
            evidence_refs=[], metadata_json={}, created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    assert (
        await db_session.execute(select(EvaluationResultModel).where(EvaluationResultModel.id == UUID(result.result_id)))
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"config_ref": VersionRef("cfg", "other")}, "config"),
        ({"prompt_ref": VersionRef("prompt", "other")}, "prompt"),
        ({"target_version_ref": VersionRef("git", "other")}, "target version"),
        ({"output_artifact_ref": ArtifactRef("other")}, "artifact"),
    ],
)
async def test_result_provenance_mismatch_is_rejected_before_insert(db_session, change, message):
    run, attempt, token = await terminal_success()
    with pytest.raises(ValueError, match=message):
        await service().finalize_result(
            TEST_PROJECT_ID, attempt.attempt_id, token, replace(result_for(run, attempt), **change)
        )
    async with async_session_factory() as session:
        assert (await session.execute(select(EvaluationResultModel))).scalars().all() == []


@pytest.mark.asyncio
async def test_complete_matching_result_provenance_inserts(db_session):
    run, attempt, token = await terminal_success()
    result = result_for(run, attempt)
    await service().finalize_result(TEST_PROJECT_ID, attempt.attempt_id, token, result)
    async with async_session_factory() as session:
        persisted = (
            await session.execute(select(EvaluationResultModel).where(EvaluationResultModel.id == UUID(result.result_id)))
        ).scalar_one()
        assert persisted.config_ref_value == "1"
        assert persisted.prompt_ref_value == "p1"
        assert persisted.target_version_value == "abc"
        assert persisted.output_artifact_ref["artifact_id"] == "artifact"


@pytest.mark.asyncio
async def test_create_run_attempt_failure_rolls_back_run(db_session, monkeypatch):
    refs = (CaseVersionRef("case-a", "v1"), CaseVersionRef("case-b", "v1"))
    cases = {ref: CaseVersion(ref.case_id, ref.version, ref.case_id, {"case": ref.case_id}, NOW) for ref in refs}
    dataset = DatasetVersion("rollback-dataset", "d1", "dataset", NOW, case_version_refs=refs)
    spec = EvaluatorSpec(
        "eval", "e1", EvaluatorKind.DETERMINISTIC, VersionRef("cfg", "1"), ScoreDirection.NOT_APPLICABLE,
    )
    suite = EvaluationSuiteVersion("rollback-suite", "s1", refs, (spec,), EvaluationPolicy(), NOW)
    candidate_run = uuid4()
    duplicate_request = uuid4()
    generated = iter((candidate_run, uuid4(), duplicate_request, uuid4(), uuid4(), duplicate_request, uuid4()))
    monkeypatch.setattr("app.services.evaluation.persistence.uuid4", lambda: next(generated))
    with pytest.raises(IntegrityError):
        await service().create_run(
            project_id=TEST_PROJECT_ID, dataset=dataset, suite=suite, cases=cases,
            target=ExecutionTargetRef("target", "FIXTURE"), timeout=timedelta(seconds=30),
        )
    async with async_session_factory() as session:
        assert (await session.execute(select(EvaluationRunModel).where(EvaluationRunModel.id == candidate_run))).scalar_one_or_none() is None
        assert (
            await session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.run_id == candidate_run))
        ).scalars().all() == []
