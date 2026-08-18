"""Trace-to-Dataset feedback 的 Application Service。

职责：验证 Trace 是当前 project 的 failing candidate，接受 caller 提供的
sanitized input，构造 immutable ``TestCaseVersion``（携带 Trace EvidenceRef）
与新的 ``DatasetVersion``（NEW_VERSION）。不读 Trace payload、不做
sanitization / expected-output / criticality 推断，并且绝不自动触发
Evaluation —— Evaluation 是 caller 显式的 ``create_run`` / ``execute_attempt``
动作。
"""

# ruff: noqa: D415

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.evaluation.catalog import DatasetVersion, TestCaseVersion
from app.core.evaluation.feedback import TraceFeedbackCandidateError, TraceFeedbackCommand, TraceFeedbackError
from app.core.evaluation.references import CaseVersionRef
from app.core.online.entities import FAILURE_OUTCOMES, TraceEvidenceCandidate
from app.core.traces.entities import Trace
from app.registry.exceptions import NotFoundError
from app.services.trace_service import TraceService


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_failing_trace(trace: Trace) -> bool:
    """WP1 frozen failing rule over resolved normalized facts (no new SQL).

    Uses the shared ``FAILURE_OUTCOMES`` set so the semantics cannot drift
    from the repository predicate.  UNKNOWN/SUCCESS are never failures.
    """
    if trace.normalized_outcome in FAILURE_OUTCOMES:
        return True
    return any(span.normalized_outcome in FAILURE_OUTCOMES for span in trace.spans)


class TraceFeedbackService:
    """Failing Trace → caller-confirmed TestCaseVersion + new DatasetVersion。

    The produced catalog facts are in-memory only; nothing is persisted and
    no EvaluationRun/Attempt/Result is created by this service.
    """

    def __init__(
        self,
        session: AsyncSession | None = None,
        *,
        trace_service: TraceService | None = None,
    ) -> None:
        if trace_service is None:
            if session is None:
                raise TypeError("TraceFeedbackService requires a session or an injected TraceService")
            trace_service = TraceService(session)
        self._traces = trace_service

    async def create_feedback_case(
        self,
        command: TraceFeedbackCommand,
    ) -> tuple[TestCaseVersion, DatasetVersion]:
        """Validate the failing candidate, then build immutable catalog facts."""
        trace = await self._require_failing_candidate(command.project_id, command.trace_id)
        candidate = TraceEvidenceCandidate.from_trace(trace)

        created_at = _now()
        test_case = TestCaseVersion(
            case_id=command.case_id,
            version=command.case_version,
            name=command.case_name or f"trace-feedback:{command.case_id}",
            input_payload=command.input_payload,
            created_at=created_at,
            expected_output=command.expected_output,
            assertion_specs=command.assertion_specs,
            evidence_refs=(candidate.evidence_ref,),
            tags=command.tags,
            metadata=command.metadata,
        )

        new_ref = CaseVersionRef(command.case_id, command.case_version)
        dataset = DatasetVersion(
            dataset_id=command.dataset_id,
            version=command.dataset_version,
            name=command.dataset_name or f"dataset:{command.dataset_id}",
            created_at=created_at,
            parent_version=command.parent_dataset_version,
            case_version_refs=command.base_case_refs + (new_ref,),
            tags=(),
            metadata={},
        )
        return test_case, dataset

    async def _require_failing_candidate(self, project_id: UUID, trace_id: UUID) -> Trace:
        """Project-scoped failing-trace validation; fails closed on any miss."""
        try:
            detail = await self._traces.get_trace(trace_id, project_id)
        except NotFoundError as exc:
            raise TraceFeedbackCandidateError(
                f"trace {trace_id} is not accessible in project {project_id}"
            ) from exc
        if not is_failing_trace(detail.trace):
            raise TraceFeedbackCandidateError(f"trace {trace_id} is not a failing candidate")
        return detail.trace
