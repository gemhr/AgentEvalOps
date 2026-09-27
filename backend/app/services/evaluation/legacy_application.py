"""Canonical application path for the existing trace/session evaluation APIs."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select

from app.core.evaluation.catalog import DatasetVersion, EvaluationPolicy, EvaluationSuiteVersion, TestCaseVersion
from app.core.evaluation.references import CaseVersionRef
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.models import EvalRunModel, EvaluationRunModel, ExecutionAttemptModel
from app.infrastructure.db.repositories.evaluation_persistence_repo import PostgresEvaluationPersistenceUnitOfWork
from app.infrastructure.db.repositories.evaluation_projection_repo import PostgresEvaluationProjectionRepository
from app.infrastructure.db.repositories.trace_repo import TraceRepository
from app.services.evaluation.legacy_metrics import LegacyExecutionTargetResolver, LegacyMetricResolver, legacy_target_ref, metric_spec
from app.services.evaluation.loop import EvaluationLoopService
from app.services.evaluation.persistence import EvaluationPersistenceService
from app.registry.constants import EvaluationStatus, ScoreDataType, ScoreSource, ScoreStatus
from app.core.evals.entities import EvalRun, PreparedRun, SessionScore, TraceScore
from app.core.evaluation.run_attempts import AttemptClaimLost, AttemptStatus, RetryAlreadyCreated, RunNotFinishable, RunStatus
from app.core.traces.entities import Trace


def _persistence() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory))


def persistence_service() -> EvaluationPersistenceService:
    """Expose the canonical persistence application boundary to API adapters."""
    return _persistence()


async def create_canonical_run(session, prepared: PreparedRun):
    """Materialize the current trace/session inputs and create durable attempts."""
    trace_repo = TraceRepository(session)
    cases: dict[CaseVersionRef, TestCaseVersion] = {}
    target_ids: list[str]
    if prepared.target_type == "SESSION":
        target_ids = list(prepared.target_ids)
    else:
        target_ids = list(prepared.target_ids)

    for target_id in target_ids:
        if prepared.target_type == "SESSION":
            traces: list[Trace] = []
            offset = 0
            while True:
                rows, total = await trace_repo.list_traces(
                    prepared.project_id, limit=500, offset=offset, session_id=target_id,
                    sort_order="ASC",
                )
                if not rows:
                    break
                for row in rows:
                    trace = await trace_repo.get_trace(row.trace_id, prepared.project_id)
                    if trace is not None:
                        traces.append(trace)
                offset += len(rows)
                if offset >= total:
                    break
            if not traces:
                continue
            case_id = target_id
            payload = {"scope": "SESSION", "session_id": target_id,
                       "traces": [trace.model_dump(mode="json") for trace in traces]}
            name = f"session {target_id}"
        else:
            trace = await trace_repo.get_trace(UUID(target_id), prepared.project_id)
            if trace is None:
                continue
            case_id = str(trace.trace_id)
            payload = {"scope": "TRACE", "trace": trace.model_dump(mode="json")}
            name = trace.name
        ref = CaseVersionRef(case_id, f"legacy-snapshot-{prepared.run.id}")
        cases[ref] = TestCaseVersion(
            case_id=ref.case_id,
            version=ref.version,
            name=name,
            input_payload=payload,
            created_at=prepared.run.created_at,
            metadata={"legacy_scope": prepared.target_type},
        )
    if not cases:
        raise ValueError("no durable evaluation inputs were found")

    dataset = DatasetVersion(
        dataset_id=f"legacy-{prepared.target_type.lower()}-evaluation",
        version="v1",
        name="Legacy evaluation input snapshot",
        created_at=prepared.run.created_at,
        case_version_refs=tuple(cases),
    )
    specs = tuple(
        metric_spec(
            name, scope=prepared.target_type, model=prepared.run.model,
            signal_weights=(prepared.run.filters or {}).get("signal_weights"),
        )
        for name in prepared.run.metric_names
    )
    suite = EvaluationSuiteVersion(
        suite_id=f"legacy-{prepared.target_type.lower()}-suite",
        version="v1",
        case_selection=tuple(cases),
        evaluator_specs=specs,
        evaluation_policy=EvaluationPolicy(),
        created_at=prepared.run.created_at,
    )
    canonical, attempts = await _persistence().create_run(
        project_id=prepared.project_id,
        dataset=dataset,
        suite=suite,
        cases=cases,
        target=legacy_target_ref(),
        timeout=timedelta(seconds=3300),
        metadata={"legacy_projection": {
            "target_type": prepared.target_type,
            "name": prepared.run.name,
            "filters": prepared.run.filters or {},
            "sampling_rate": prepared.run.sampling_rate,
            "model": prepared.run.model,
            "metric_names": list(prepared.run.metric_names),
            "monitor_id": None if prepared.run.monitor_id is None else str(prepared.run.monitor_id),
        }},
        run_id=prepared.run.id,
    )
    prepared.run.total_targets = len(attempts)
    async with async_session_factory() as projection_session:
        await PostgresEvaluationProjectionRepository(projection_session).insert_run(
            prepared.run, canonical.run_id
        )
        await projection_session.commit()
    return canonical, attempts


async def execute_canonical_attempt(project_id: UUID, attempt_id: UUID, *, task_ref: str | None, worker_ref: str | None) -> str:
    """Claim and execute one attempt, then coordinate the Run terminal decision."""
    persistence = _persistence()
    loop = EvaluationLoopService(persistence, LegacyExecutionTargetResolver(), LegacyMetricResolver())
    attempt = await persistence.get_attempt(project_id, attempt_id)
    run = await persistence.get_run(project_id, attempt.run_id)
    if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.OUTCOME_UNKNOWN}:
        await project_canonical_state(project_id, run.run_id)
        return run.status.value
    result = await loop.execute_attempt(
        project_id, attempt_id, lease=timedelta(minutes=56), worker_ref=worker_ref,
        task_ref=task_ref, finalize_run=False,
    )
    attempt = await persistence.get_attempt(project_id, attempt_id)
    run = await persistence.get_run(project_id, attempt.run_id)
    try:
        await persistence.coordinate_run(project_id, run.run_id)
    except RunNotFinishable:
        pass
    await project_canonical_state(project_id, run.run_id)
    return result.value


async def project_canonical_state(project_id: UUID, run_id: UUID) -> None:
    """Refresh the one-way compatibility view from canonical Run/Attempts/Results."""
    persistence = _persistence()
    async with async_session_factory() as session:
        # Serialize projection writes with canonical terminalization/retry, then
        # reload truth after obtaining the run-row lock.
        locked = await session.scalar(
            select(EvaluationRunModel.id).where(
                EvaluationRunModel.project_id == project_id,
                EvaluationRunModel.id == run_id,
            ).with_for_update()
        )
        if locked is None:
            return
        run = await persistence.get_run(project_id, run_id)
        attempts = await persistence.list_attempts(project_id, run_id)
        results = await persistence.list_results(project_id, run_id)
        latest: dict[tuple[str, str], object] = {}
        for attempt in attempts:
            key = (attempt.case_ref.case_id, attempt.case_ref.version)
            if key not in latest or latest[key].attempt_no < attempt.attempt_no:
                latest[key] = attempt
        status = {
            RunStatus.PENDING: EvaluationStatus.PENDING,
            RunStatus.RUNNING: EvaluationStatus.RUNNING,
            RunStatus.COMPLETED: EvaluationStatus.COMPLETED,
            RunStatus.FAILED: EvaluationStatus.FAILED,
            RunStatus.OUTCOME_UNKNOWN: EvaluationStatus.OUTCOME_UNKNOWN,
        }[run.status]
        metadata = dict(run.metadata)
        target_type = metadata.get("legacy_projection", {}).get("target_type", "TRACE")
        legacy = await session.get(EvalRunModel, run_id)
        if legacy is None:
            projection = metadata.get("legacy_projection", {})
            legacy_run = EvalRun(
                id=run_id,
                project_id=project_id,
                name=projection.get("name"),
                target_type=projection.get("target_type", "TRACE"),
                metric_names=list(projection.get("metric_names", [])),
                filters=dict(projection.get("filters", {})),
                sampling_rate=float(projection.get("sampling_rate", 1.0)),
                model=projection.get("model"),
                monitor_id=None if projection.get("monitor_id") is None else UUID(projection["monitor_id"]),
                status=status,
                total_targets=len(latest),
                evaluated_count=0,
                created_at=run.created_at,
            )
            await PostgresEvaluationProjectionRepository(session).insert_run(legacy_run, run_id)
            legacy = await session.get(EvalRunModel, run_id)
            if legacy is None:
                raise RuntimeError("canonical Run projection insert did not persist")
        legacy.status = status
        legacy.total_targets = len(latest)
        legacy.evaluated_count = sum(item.status is AttemptStatus.TERMINAL for item in latest.values())
        legacy.failed_count = sum(
            item.status is AttemptStatus.TERMINAL and item.execution_outcome_kind.value != "SUCCESS"
            for item in latest.values()
        )
        legacy.error_message = run.status_reason
        legacy.completed_at = run.finished_at
        projector = PostgresEvaluationProjectionRepository(session)
        for result in results:
            score_value = None if result.score is None else str(round(result.score, 4))
            score_status = ScoreStatus.FAILED if result.verdict.value == "ERROR" else ScoreStatus.SUCCESS
            if target_type == "SESSION":
                score = SessionScore(
                    id=UUID(result.result_id), session_id=result.case_id, project_id=project_id,
                    name=result.evaluator_id, data_type=ScoreDataType.NUMERIC, value=score_value,
                    source=ScoreSource.AUTOMATED, status=score_status, eval_run_id=run_id,
                    reason=result.reason, metadata=dict(result.metadata), created_at=result.created_at,
                    updated_at=result.created_at,
                )
                await projector.insert_session_score(score, UUID(result.result_id), result.verdict)
            else:
                score = TraceScore(
                    id=UUID(result.result_id), trace_id=UUID(result.case_id), project_id=project_id,
                    name=result.evaluator_id, data_type=ScoreDataType.NUMERIC, value=score_value,
                    source=ScoreSource.AUTOMATED, status=score_status, eval_run_id=run_id,
                    reason=result.reason, metadata=dict(result.metadata), created_at=result.created_at,
                    updated_at=result.created_at,
                )
                await projector.insert_trace_score(score, UUID(result.result_id), result.verdict)
        await session.commit()


async def reconcile_stale_attempt(project_id: UUID, attempt_id: UUID, reason: str) -> str:
    """Reconcile one expired lease as UNKNOWN and refresh its projection."""
    persistence = _persistence()
    attempt = await persistence.reconcile_stale(project_id, attempt_id, reason=reason)
    await project_canonical_state(project_id, attempt.run_id)
    return attempt.execution_outcome_kind.value


async def retry_canonical_attempts(
    project_id: UUID, source_attempt_ids: list[UUID], *, unknown_retry_reason: str | None = None
):
    """Create durable child Attempts for API-authorized retry requests."""
    persistence = _persistence()
    children = []
    for source_id in source_attempt_ids:
        try:
            child = await persistence.retry_attempt(
                project_id,
                source_id,
                allow_unknown_retry=bool(unknown_retry_reason),
                reason=unknown_retry_reason,
            )
        except RetryAlreadyCreated:
            source = await persistence.get_attempt(project_id, source_id)
            attempts = await persistence.list_attempts(project_id, source.run_id)
            child = next(item for item in attempts if item.retry_of_attempt_id == source_id)
        except ValueError as exc:
            from app.registry.exceptions import ValidationError

            raise ValidationError(str(exc)) from exc
        children.append(child)
    return tuple(children)


async def dispatch_pending_attempts(limit: int = 500) -> int:
    """Re-enqueue only PENDING attempts; duplicate deliveries are claim fenced."""
    from app.infrastructure.queue.tasks import execute_evaluation_attempt

    async with async_session_factory() as session:
        rows = (await session.execute(
            select(ExecutionAttemptModel.project_id, ExecutionAttemptModel.id)
            .where(ExecutionAttemptModel.status == AttemptStatus.PENDING.value)
            .order_by(ExecutionAttemptModel.created_at)
            .limit(limit)
        )).all()
    for project_id, attempt_id in rows:
        execute_evaluation_attempt.apply_async(args=[str(project_id), str(attempt_id)])
    return len(rows)


async def reconcile_expired_attempts(limit: int = 500) -> int:
    """Convert expired active leases to UNKNOWN and schedule coordinator work."""
    from sqlalchemy import func

    from app.infrastructure.queue.tasks import execute_evaluation_attempt

    async with async_session_factory() as session:
        rows = (await session.execute(
            select(ExecutionAttemptModel.project_id, ExecutionAttemptModel.id)
            .where(
                ExecutionAttemptModel.status.in_(["CLAIMED", "RUNNING"]),
                ExecutionAttemptModel.lease_expires_at <= func.current_timestamp(),
            )
            .order_by(ExecutionAttemptModel.lease_expires_at)
            .limit(limit)
        )).all()
    persistence = _persistence()
    for project_id, attempt_id in rows:
        try:
            await persistence.reconcile_stale(project_id, attempt_id, reason="active lease expired")
        except AttemptClaimLost:
            continue
        execute_evaluation_attempt.apply_async(args=[str(project_id), str(attempt_id)])
    return len(rows)
