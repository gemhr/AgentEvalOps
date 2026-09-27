"""Canonical evaluation facts exposed through legacy compatibility rows."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.core.evals.entities import EvalRun, SessionScore, TraceScore
from app.core.evaluation.results import EvaluationVerdict
from app.registry.constants import ScoreSource


class EvaluationProjectionRepository(Protocol):
    """Insert-only persistence operations for compatibility projections."""

    async def insert_run(self, run: EvalRun, canonical_run_id: UUID) -> None:
        """Insert the legacy Run row and its canonical link."""
        ...

    async def insert_trace_score(
        self, score: TraceScore, canonical_result_id: UUID, canonical_verdict: EvaluationVerdict
    ) -> None:
        """Insert an automated trace score projection."""
        ...

    async def insert_session_score(
        self, score: SessionScore, canonical_result_id: UUID, canonical_verdict: EvaluationVerdict
    ) -> None:
        """Insert an automated session score projection."""
        ...


class LegacyEvaluationProjectionService:
    """Create one-way legacy read-model rows from canonical identities."""

    def __init__(self, repository: EvaluationProjectionRepository) -> None:
        self._repository = repository

    async def project_run(self, run: EvalRun, canonical_run_id: UUID) -> None:
        """Persist the compatibility row associated with a canonical Run."""
        await self._repository.insert_run(run, canonical_run_id)

    async def project_trace_result(
        self,
        score: TraceScore,
        canonical_result_id: UUID,
        canonical_verdict: EvaluationVerdict,
    ) -> None:
        """Persist an automated TraceScore projection, never an annotation."""
        self._require_automated(score.source)
        await self._repository.insert_trace_score(score, canonical_result_id, canonical_verdict)

    async def project_session_result(
        self,
        score: SessionScore,
        canonical_result_id: UUID,
        canonical_verdict: EvaluationVerdict,
    ) -> None:
        """Persist an automated SessionScore projection, never an annotation."""
        self._require_automated(score.source)
        await self._repository.insert_session_score(score, canonical_result_id, canonical_verdict)

    @staticmethod
    def _require_automated(source: ScoreSource) -> None:
        if source is not ScoreSource.AUTOMATED:
            raise ValueError("canonical EvaluationResult projections must use AUTOMATED source")
