"""Persistence adapter for legacy evaluation compatibility projections."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.evals.entities import EvalRun, SessionScore, TraceScore
from app.core.evaluation.results import EvaluationVerdict
from app.infrastructure.db.models import EvalRunModel, SessionScoreModel, TraceScoreModel


class PostgresEvaluationProjectionRepository:
    """Insert-only persistence for canonical-to-legacy projection rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_run(self, run: EvalRun, canonical_run_id: UUID) -> None:
        """Insert one linked compatibility Run row."""
        self._session.add(
            EvalRunModel(
                id=run.id,
                project_id=run.project_id,
                name=run.name,
                target_type=run.target_type,
                metric_names=run.metric_names,
                filters=run.filters,
                sampling_rate=run.sampling_rate,
                model=run.model,
                status=run.status,
                total_targets=run.total_targets,
                evaluated_count=run.evaluated_count,
                failed_count=run.failed_count,
                error_message=run.error_message,
                monitor_id=run.monitor_id,
                canonical_run_id=canonical_run_id,
                created_at=run.created_at,
                completed_at=run.completed_at,
            )
        )
        await self._session.flush()

    async def insert_trace_score(
        self,
        score: TraceScore,
        canonical_result_id: UUID,
        canonical_verdict: EvaluationVerdict,
    ) -> None:
        """Insert one linked trace score without discarding its verdict."""
        if await self._session.scalar(select(TraceScoreModel.id).where(
            TraceScoreModel.project_id == score.project_id,
            TraceScoreModel.canonical_result_id == canonical_result_id,
        )) is not None:
            return
        self._session.add(
            TraceScoreModel(
                id=score.id,
                trace_id=score.trace_id,
                project_id=score.project_id,
                name=score.name,
                data_type=score.data_type,
                value=score.value,
                source=score.source,
                status=score.status,
                eval_run_id=score.eval_run_id,
                canonical_result_id=canonical_result_id,
                canonical_verdict=canonical_verdict.value,
                author_user_id=score.author_user_id,
                reason=score.reason,
                environment=score.environment,
                config_id=score.config_id,
                metadata_=score.metadata,
                created_at=score.created_at,
                updated_at=score.updated_at,
            )
        )
        await self._session.flush()

    async def insert_session_score(
        self,
        score: SessionScore,
        canonical_result_id: UUID,
        canonical_verdict: EvaluationVerdict,
    ) -> None:
        """Insert one linked session score without discarding its verdict."""
        if await self._session.scalar(select(SessionScoreModel.id).where(
            SessionScoreModel.project_id == score.project_id,
            SessionScoreModel.canonical_result_id == canonical_result_id,
        )) is not None:
            return
        self._session.add(
            SessionScoreModel(
                id=score.id,
                session_id=score.session_id,
                project_id=score.project_id,
                name=score.name,
                data_type=score.data_type,
                value=score.value,
                source=score.source,
                status=score.status,
                eval_run_id=score.eval_run_id,
                canonical_result_id=canonical_result_id,
                canonical_verdict=canonical_verdict.value,
                author_user_id=score.author_user_id,
                reason=score.reason,
                environment=score.environment,
                config_id=score.config_id,
                metadata_=score.metadata,
                created_at=score.created_at,
                updated_at=score.updated_at,
            )
        )
        await self._session.flush()
