"""PostgreSQL-backed Evaluation Run/Attempt/Result repositories 与 UoW。"""

# ruff: noqa: D102, D105, D415

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta
from types import TracebackType
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.evaluation.execution import ExecutionOutcome, ExecutionRequest, ExecutionTargetRef, OutcomeKind
from app.core.evaluation.immutable import FrozenDict
from app.core.evaluation.references import ArtifactRef, CaseVersionRef, EvidenceRef, VersionRef
from app.core.evaluation.results import EvaluationResult, EvaluationVerdict, ProvenanceCompleteness
from app.core.evaluation.run_attempts import (
    AttemptStatus,
    EvaluationRun,
    ExecutionAttempt,
    ResultAlreadyFinalized,
    RetryAlreadyCreated,
    RunStatus,
)
from app.infrastructure.db.models import EvaluationResultModel, EvaluationRunModel, ExecutionAttemptModel


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _artifact(value: ArtifactRef | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"artifact_id": value.artifact_id, "digest": value.digest, "media_type": value.media_type, "metadata": _plain(value.metadata)}


def _artifact_from(value: dict[str, Any] | None) -> ArtifactRef | None:
    return None if value is None else ArtifactRef(**value)


def _evidence(value: EvidenceRef) -> dict[str, object]:
    return {
        "kind": value.kind, "identifier": value.identifier, "media_type": value.media_type,
        "schema_version": value.schema_version, "metadata": _plain(value.metadata),
    }


def _evidence_from(value: dict[str, Any]) -> EvidenceRef:
    return EvidenceRef(**value)


def _version(kind: str | None, value: str | None) -> VersionRef | None:
    return None if kind is None else VersionRef(kind, value or "")


def _target(row: ExecutionAttemptModel | EvaluationRunModel) -> ExecutionTargetRef:
    return ExecutionTargetRef(
        target_id=row.execution_target_id,
        target_kind=row.execution_target_kind,
        target_version_ref=_version(row.target_version_kind, row.target_version_value),
        capabilities=tuple(row.execution_target_snapshot.get("capabilities", ())) if isinstance(row, EvaluationRunModel) else (),
        config_ref=None if isinstance(row, EvaluationRunModel) else _version(row.target_config_kind, row.target_config_value),
    )


def _run_from(row: EvaluationRunModel) -> EvaluationRun:
    return EvaluationRun(
        run_id=row.id, project_id=row.project_id,
        dataset_ref=VersionRef("DATASET", row.dataset_version), suite_ref=VersionRef("SUITE", row.suite_version),
        execution_target_ref=_target(row), dataset_snapshot=row.dataset_snapshot, suite_snapshot=row.suite_snapshot,
        execution_target_snapshot=row.execution_target_snapshot, subject_ref=row.subject_ref,
        status=RunStatus(row.status), status_reason=row.status_reason, metadata=row.metadata_json,
        created_at=row.created_at, started_at=row.started_at, finished_at=row.finished_at,
    )


def _attempt_from(row: ExecutionAttemptModel) -> ExecutionAttempt:
    snapshot = row.request_snapshot
    request = ExecutionRequest(
        request_id=row.execution_request_id, run_id=str(row.run_id), attempt_id=str(row.id),
        case_ref=CaseVersionRef(row.case_id, row.case_version), input_payload=snapshot["input_payload"],
        timeout=timedelta(seconds=float(snapshot["timeout_seconds"])), idempotency_key=row.idempotency_key,
        execution_metadata=snapshot.get("execution_metadata", {}),
    )
    return ExecutionAttempt(
        attempt_id=row.id, project_id=row.project_id, run_id=row.run_id, case_ref=request.case_ref,
        attempt_no=row.attempt_no, retry_of_attempt_id=row.retry_of_attempt_id, execution_target_ref=_target(row),
        execution_request=request, request_snapshot=snapshot, status=AttemptStatus(row.status), claim_token=row.claim_token,
        worker_ref=row.worker_ref, task_ref=row.task_ref, created_at=row.created_at, claimed_at=row.claimed_at,
        started_at=row.started_at, finished_at=row.finished_at, lease_expires_at=row.lease_expires_at,
        execution_outcome_kind=OutcomeKind(row.execution_outcome_kind) if row.execution_outcome_kind else None,
        output_artifact_ref=_artifact_from(row.output_artifact_ref),
        outcome_evidence_refs=tuple(_evidence_from(item) for item in row.outcome_evidence_refs),
        outcome_metadata=row.outcome_metadata, error_category=row.error_category, reason=row.reason,
    )


def _attempt_model(value: ExecutionAttempt) -> ExecutionAttemptModel:
    target = value.execution_target_ref
    return ExecutionAttemptModel(
        id=value.attempt_id, project_id=value.project_id, run_id=value.run_id,
        case_id=value.case_ref.case_id, case_version=value.case_ref.version, attempt_no=value.attempt_no,
        retry_of_attempt_id=value.retry_of_attempt_id, execution_target_id=target.target_id,
        execution_target_kind=target.target_kind,
        target_version_kind=target.target_version_ref.kind if target.target_version_ref else None,
        target_version_value=target.target_version_ref.opaque_value if target.target_version_ref else None,
        target_config_kind=target.config_ref.kind if target.config_ref else None,
        target_config_value=target.config_ref.opaque_value if target.config_ref else None,
        execution_request_id=value.execution_request.request_id,
        idempotency_key=value.execution_request.idempotency_key, request_snapshot=_plain(value.request_snapshot),
        status=value.status.value, claim_token=value.claim_token, worker_ref=value.worker_ref, task_ref=value.task_ref,
        created_at=value.created_at, claimed_at=value.claimed_at, started_at=value.started_at,
        finished_at=value.finished_at, lease_expires_at=value.lease_expires_at,
        execution_outcome_kind=value.execution_outcome_kind.value if value.execution_outcome_kind else None,
        output_artifact_ref=_artifact(value.output_artifact_ref),
        outcome_evidence_refs=[_evidence(item) for item in value.outcome_evidence_refs],
        outcome_metadata=_plain(value.outcome_metadata), error_category=value.error_category, reason=value.reason,
    )


class PostgresEvaluationRunRepository:
    """EvaluationRun SQL primitives。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_run_with_attempts(self, run: EvaluationRun, attempts: tuple[ExecutionAttempt, ...]) -> None:
        target = run.execution_target_ref
        self._session.add(
            EvaluationRunModel(
                id=run.run_id, project_id=run.project_id,
                dataset_id=str(run.dataset_snapshot["dataset_id"]), dataset_version=run.dataset_ref.opaque_value,
                suite_id=str(run.suite_snapshot["suite_id"]), suite_version=run.suite_ref.opaque_value,
                execution_target_id=target.target_id, execution_target_kind=target.target_kind,
                target_version_kind=target.target_version_ref.kind if target.target_version_ref else None,
                target_version_value=target.target_version_ref.opaque_value if target.target_version_ref else None,
                dataset_snapshot=_plain(run.dataset_snapshot), suite_snapshot=_plain(run.suite_snapshot),
                execution_target_snapshot=_plain(run.execution_target_snapshot), subject_ref=_plain(run.subject_ref),
                status=run.status.value, status_reason=run.status_reason, metadata_json=_plain(run.metadata),
                created_at=run.created_at, started_at=run.started_at, finished_at=run.finished_at,
            )
        )
        self._session.add_all([_attempt_model(item) for item in attempts])
        await self._session.flush()

    async def get_run(self, project_id: UUID, run_id: UUID) -> EvaluationRun | None:
        row = (await self._session.execute(select(EvaluationRunModel).where(EvaluationRunModel.project_id == project_id, EvaluationRunModel.id == run_id))).scalar_one_or_none()
        return None if row is None else _run_from(row)

    async def lock_run(self, project_id: UUID, run_id: UUID) -> EvaluationRun | None:
        row = (await self._session.execute(select(EvaluationRunModel).where(EvaluationRunModel.project_id == project_id, EvaluationRunModel.id == run_id).with_for_update())).scalar_one_or_none()
        return None if row is None else _run_from(row)

    async def set_running_if_pending(self, project_id: UUID, run_id: UUID) -> bool:
        result = await self._session.execute(
            update(EvaluationRunModel).where(EvaluationRunModel.project_id == project_id, EvaluationRunModel.id == run_id, EvaluationRunModel.status == RunStatus.PENDING.value)
            .values(status=RunStatus.RUNNING.value, started_at=func.current_timestamp()).returning(EvaluationRunModel.id)
        )
        return result.scalar_one_or_none() is not None

    async def finish_run(self, project_id: UUID, run_id: UUID, status: RunStatus, reason: str | None) -> bool:
        result = await self._session.execute(
            update(EvaluationRunModel).where(
                EvaluationRunModel.project_id == project_id, EvaluationRunModel.id == run_id,
                EvaluationRunModel.status.in_([RunStatus.PENDING.value, RunStatus.RUNNING.value]),
            ).values(status=status.value, status_reason=reason, finished_at=func.current_timestamp()).returning(EvaluationRunModel.id)
        )
        return result.scalar_one_or_none() is not None


class PostgresExecutionAttemptRepository:
    """PostgreSQL CAS-backed Attempt repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_attempt(self, project_id: UUID, attempt_id: UUID) -> ExecutionAttempt | None:
        row = (await self._session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.project_id == project_id, ExecutionAttemptModel.id == attempt_id))).scalar_one_or_none()
        return None if row is None else _attempt_from(row)

    async def list_attempts(self, project_id: UUID, run_id: UUID) -> tuple[ExecutionAttempt, ...]:
        rows = (await self._session.execute(select(ExecutionAttemptModel).where(ExecutionAttemptModel.project_id == project_id, ExecutionAttemptModel.run_id == run_id).order_by(ExecutionAttemptModel.case_id, ExecutionAttemptModel.case_version, ExecutionAttemptModel.attempt_no))).scalars().all()
        return tuple(_attempt_from(row) for row in rows)

    async def list_latest_attempts(self, project_id: UUID, run_id: UUID) -> tuple[ExecutionAttempt, ...]:
        latest = select(
            ExecutionAttemptModel.case_id.label("case_id"), ExecutionAttemptModel.case_version.label("case_version"),
            func.max(ExecutionAttemptModel.attempt_no).label("attempt_no"),
        ).where(ExecutionAttemptModel.project_id == project_id, ExecutionAttemptModel.run_id == run_id).group_by(ExecutionAttemptModel.case_id, ExecutionAttemptModel.case_version).subquery()
        rows = (await self._session.execute(select(ExecutionAttemptModel).join(
            latest, and_(ExecutionAttemptModel.case_id == latest.c.case_id, ExecutionAttemptModel.case_version == latest.c.case_version, ExecutionAttemptModel.attempt_no == latest.c.attempt_no)
        ).where(ExecutionAttemptModel.project_id == project_id, ExecutionAttemptModel.run_id == run_id))).scalars().all()
        return tuple(_attempt_from(row) for row in rows)

    async def claim_attempt(self, project_id: UUID, attempt_id: UUID, token: UUID, lease: timedelta, worker_ref: str | None, task_ref: str | None) -> ExecutionAttempt | None:
        now = func.current_timestamp()
        row = (await self._session.execute(
            update(ExecutionAttemptModel).where(
                ExecutionAttemptModel.project_id == project_id, ExecutionAttemptModel.id == attempt_id,
                ExecutionAttemptModel.status == AttemptStatus.PENDING.value, ExecutionAttemptModel.claim_token.is_(None),
            ).values(status=AttemptStatus.CLAIMED.value, claim_token=token, claimed_at=now,
                     lease_expires_at=now + lease, worker_ref=worker_ref, task_ref=task_ref)
            .returning(ExecutionAttemptModel)
        )).scalar_one_or_none()
        return None if row is None else _attempt_from(row)

    async def mark_running(self, project_id: UUID, attempt_id: UUID, token: UUID) -> ExecutionAttempt | None:
        row = (await self._session.execute(
            update(ExecutionAttemptModel).where(
                ExecutionAttemptModel.project_id == project_id, ExecutionAttemptModel.id == attempt_id,
                ExecutionAttemptModel.status == AttemptStatus.CLAIMED.value, ExecutionAttemptModel.claim_token == token,
                ExecutionAttemptModel.lease_expires_at > func.current_timestamp(),
            ).values(status=AttemptStatus.RUNNING.value, started_at=func.current_timestamp()).returning(ExecutionAttemptModel)
        )).scalar_one_or_none()
        return None if row is None else _attempt_from(row)

    async def record_outcome(self, project_id: UUID, attempt_id: UUID, token: UUID, outcome: ExecutionOutcome) -> ExecutionAttempt | None:
        row = (await self._session.execute(
            update(ExecutionAttemptModel).where(
                ExecutionAttemptModel.project_id == project_id, ExecutionAttemptModel.id == attempt_id,
                ExecutionAttemptModel.status == AttemptStatus.RUNNING.value, ExecutionAttemptModel.claim_token == token,
            ).values(
                status=AttemptStatus.TERMINAL.value, execution_outcome_kind=outcome.kind.value,
                output_artifact_ref=_artifact(outcome.output_artifact_ref),
                outcome_evidence_refs=[_evidence(item) for item in outcome.evidence_refs],
                outcome_metadata=_plain(outcome.metadata), error_category=outcome.error_category,
                reason=outcome.reason, finished_at=func.current_timestamp(),
            ).returning(ExecutionAttemptModel)
        )).scalar_one_or_none()
        return None if row is None else _attempt_from(row)

    async def create_retry(self, attempt: ExecutionAttempt) -> None:
        self._session.add(_attempt_model(attempt))
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "uq_evaluation_attempts_direct_retry" in str(exc.orig):
                raise RetryAlreadyCreated("direct retry child already exists") from exc
            raise

    async def list_stale_candidates(self, project_id: UUID, run_id: UUID | None = None) -> tuple[ExecutionAttempt, ...]:
        stmt = select(ExecutionAttemptModel).where(
            ExecutionAttemptModel.project_id == project_id,
            ExecutionAttemptModel.status.in_([AttemptStatus.CLAIMED.value, AttemptStatus.RUNNING.value]),
            ExecutionAttemptModel.lease_expires_at <= func.current_timestamp(),
        )
        if run_id is not None:
            stmt = stmt.where(ExecutionAttemptModel.run_id == run_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return tuple(_attempt_from(row) for row in rows)

    async def reconcile_stale(self, project_id: UUID, attempt_id: UUID, reason: str) -> ExecutionAttempt | None:
        row = (await self._session.execute(
            update(ExecutionAttemptModel).where(
                ExecutionAttemptModel.project_id == project_id, ExecutionAttemptModel.id == attempt_id,
                ExecutionAttemptModel.status.in_([AttemptStatus.CLAIMED.value, AttemptStatus.RUNNING.value]),
                ExecutionAttemptModel.lease_expires_at <= func.current_timestamp(),
            ).values(
                status=AttemptStatus.TERMINAL.value, execution_outcome_kind=OutcomeKind.OUTCOME_UNKNOWN.value,
                output_artifact_ref=None, error_category="STALE_EXECUTION", reason=reason,
                finished_at=func.current_timestamp(),
            ).returning(ExecutionAttemptModel)
        )).scalar_one_or_none()
        return None if row is None else _attempt_from(row)


class PostgresEvaluationResultRepository:
    """Append-only Result repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_result(self, project_id: UUID, result_id: UUID) -> EvaluationResult | None:
        row = (await self._session.execute(select(EvaluationResultModel).where(EvaluationResultModel.project_id == project_id, EvaluationResultModel.id == result_id))).scalar_one_or_none()
        return None if row is None else self._from(row)

    async def list_results(self, project_id: UUID, run_id: UUID, attempt_id: UUID | None = None) -> tuple[EvaluationResult, ...]:
        stmt = select(EvaluationResultModel).where(EvaluationResultModel.project_id == project_id, EvaluationResultModel.run_id == run_id)
        if attempt_id is not None:
            stmt = stmt.where(EvaluationResultModel.attempt_id == attempt_id)
        rows = (await self._session.execute(stmt.order_by(EvaluationResultModel.created_at))).scalars().all()
        return tuple(self._from(row) for row in rows)

    async def list_finalized_slots(self, project_id: UUID, run_id: UUID, attempt_id: UUID) -> frozenset[tuple[str, str, str, str]]:
        rows = (await self._session.execute(select(
            EvaluationResultModel.case_id, EvaluationResultModel.case_version,
            EvaluationResultModel.evaluator_id, EvaluationResultModel.evaluator_version,
        ).where(EvaluationResultModel.project_id == project_id, EvaluationResultModel.run_id == run_id, EvaluationResultModel.attempt_id == attempt_id))).all()
        return frozenset(tuple(row) for row in rows)

    async def insert_final_result(self, project_id: UUID, result: EvaluationResult, claim_token: UUID) -> None:
        owned = (await self._session.execute(select(ExecutionAttemptModel.id).where(
            ExecutionAttemptModel.project_id == project_id, ExecutionAttemptModel.id == UUID(result.attempt_id),
            ExecutionAttemptModel.run_id == UUID(result.run_id), ExecutionAttemptModel.claim_token == claim_token,
            ExecutionAttemptModel.status == AttemptStatus.TERMINAL.value,
            ExecutionAttemptModel.execution_outcome_kind == OutcomeKind.SUCCESS.value,
        ))).scalar_one_or_none()
        if owned is None:
            from app.core.evaluation.run_attempts import AttemptClaimLost
            raise AttemptClaimLost("result insert is not owned by terminal SUCCESS claimant")
        self._session.add(EvaluationResultModel(
            id=UUID(result.result_id), project_id=project_id, run_id=UUID(result.run_id), attempt_id=UUID(result.attempt_id),
            dataset_id=result.dataset_id, dataset_version=result.dataset_version, case_id=result.case_id,
            case_version=result.case_version, suite_id=result.suite_id, suite_version=result.suite_version,
            evaluator_id=result.evaluator_id, evaluator_version=result.evaluator_version,
            config_ref_kind=result.config_ref.kind, config_ref_value=result.config_ref.opaque_value,
            prompt_ref_kind=result.prompt_ref.kind if result.prompt_ref else None,
            prompt_ref_value=result.prompt_ref.opaque_value if result.prompt_ref else None,
            execution_target_id=result.execution_target_id,
            target_version_kind=result.target_version_ref.kind if result.target_version_ref else None,
            target_version_value=result.target_version_ref.opaque_value if result.target_version_ref else None,
            execution_request_id=result.execution_request_id, verdict=result.verdict.value, reason=result.reason,
            provenance_completeness=result.provenance_completeness.value,
            output_artifact_ref=_artifact(result.output_artifact_ref), score=result.score,
            evidence_refs=[_evidence(item) for item in result.evidence_refs], metadata_json=_plain(result.metadata),
            created_at=result.created_at,
        ))
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "uq_evaluation_results_logical_slot" in str(exc.orig):
                raise ResultAlreadyFinalized("logical result slot already finalized") from exc
            raise

    @staticmethod
    def _from(row: EvaluationResultModel) -> EvaluationResult:
        return EvaluationResult(
            result_id=str(row.id), run_id=str(row.run_id), attempt_id=str(row.attempt_id),
            dataset_id=row.dataset_id, dataset_version=row.dataset_version, case_id=row.case_id,
            case_version=row.case_version, suite_id=row.suite_id, suite_version=row.suite_version,
            evaluator_id=row.evaluator_id, evaluator_version=row.evaluator_version,
            config_ref=VersionRef(row.config_ref_kind, row.config_ref_value),
            prompt_ref=_version(row.prompt_ref_kind, row.prompt_ref_value),
            execution_target_id=row.execution_target_id,
            target_version_ref=_version(row.target_version_kind, row.target_version_value),
            execution_request_id=row.execution_request_id, verdict=EvaluationVerdict(row.verdict), reason=row.reason,
            provenance_completeness=ProvenanceCompleteness(row.provenance_completeness),
            output_artifact_ref=_artifact_from(row.output_artifact_ref), score=row.score,
            evidence_refs=tuple(_evidence_from(item) for item in row.evidence_refs), metadata=row.metadata_json,
            created_at=row.created_at,
        )


class PostgresEvaluationPersistenceUnitOfWork:
    """Own-session UoW；Repository 永不 commit。"""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> PostgresEvaluationPersistenceUnitOfWork:
        self._session = self._session_factory()
        self.runs = PostgresEvaluationRunRepository(self._session)
        self.attempts = PostgresExecutionAttemptRepository(self._session)
        self.results = PostgresEvaluationResultRepository(self._session)
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        if self._session is None:
            return
        if exc_type is not None:
            await self._session.rollback()
        await self._session.close()

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        await self._session.commit()

    async def rollback(self) -> None:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        await self._session.rollback()
