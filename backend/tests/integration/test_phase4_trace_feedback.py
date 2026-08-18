"""Phase4-WP1 Trace→Dataset feedback E2E on real PostgreSQL.

Proves: failing Trace → TraceFeedbackService → TestCaseVersion/DatasetVersion
(in-memory, no DB rows) → explicit create_run → execute_attempt →
EvaluationResult carrying the original Trace EvidenceRef; cross-project
feedback fails closed; existing DatasetVersion stays immutable (NEW_VERSION).
"""

# ruff: noqa: D101, D102, D105, D415

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.evaluation import FixtureExecution, FixtureExecutionTarget
from app.core.evaluation import (
    ArtifactRef,
    CaseVersionRef,
    DatasetVersion,
    EvaluationPolicy,
    EvaluationResultDraft,
    EvaluationSuiteVersion,
    EvaluationVerdict,
    EvaluatorKind,
    EvaluatorSpec,
    ExecutionTargetRef,
    OutcomeKind,
    PolicyDisposition,
    ScoreDirection,
    VersionRef,
)
from app.core.evaluation.feedback import TraceFeedbackCandidateError, TraceFeedbackCommand
from app.core.online.entities import trace_evidence_ref
from app.core.traces.entities import Span, Trace
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.models import EvaluationRunModel, OrganizationModel, ProjectModel
from app.infrastructure.db.repositories.evaluation_persistence_repo import PostgresEvaluationPersistenceUnitOfWork
from app.infrastructure.db.repositories.trace_repo import TraceRepository
from app.registry.constants import SpanKind, SpanStatusCode, TraceStatus
from app.services.evaluation import (
    EvaluationLoopResult,
    EvaluationLoopService,
    EvaluationPersistenceService,
    ResolvedEvaluator,
    TraceFeedbackService,
)

from .conftest import TEST_PROJECT_ID

pytestmark = pytest.mark.asyncio

PROJECT_B_ORG_ID = UUID("00000000-0000-4000-a000-00000000000b")
PROJECT_B_ID = UUID("00000000-0000-4000-a000-00000000000c")

NOW = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)


class SimpleEvaluator:
    """Deterministic evaluator that always PASSes and returns evidence-free drafts."""

    def __init__(self, spec: EvaluatorSpec) -> None:
        self.spec = spec

    async def evaluate(self, evaluation_input, context) -> EvaluationResultDraft:
        return EvaluationResultDraft(
            self.spec.evaluator_id,
            self.spec.evaluator_version,
            self.spec.config_ref,
            EvaluationVerdict.PASS,
            "feedback e2e",
            score=1.0,
            prompt_ref=self.spec.prompt_ref,
        )


class EvaluatorResolver:
    def __init__(self, evaluator: SimpleEvaluator) -> None:
        self.evaluator = evaluator

    def resolve(self, spec: EvaluatorSpec) -> ResolvedEvaluator:
        return ResolvedEvaluator(spec.evaluator_id, spec.evaluator_version, self.evaluator)


class TargetResolver:
    def __init__(self, target: FixtureExecutionTarget) -> None:
        self.target = target

    def resolve(self, target_ref: ExecutionTargetRef) -> FixtureExecutionTarget:
        return self.target


def _spec() -> EvaluatorSpec:
    return EvaluatorSpec(
        "deterministic-passes",
        "v1",
        EvaluatorKind.DETERMINISTIC,
        VersionRef("config", "feedback-config"),
        ScoreDirection.HIGHER_IS_BETTER,
        score_range=(0.0, 1.0),
        prompt_ref=VersionRef("prompt", "feedback-prompt"),
    )


async def _seed_failing_trace(session: AsyncSession, *, project_id: UUID = TEST_PROJECT_ID) -> UUID:
    trace_id = uuid4()
    started = NOW - timedelta(hours=1)
    trace = Trace(
        trace_id=trace_id,
        project_id=project_id,
        name="failed-trace",
        status=TraceStatus.ERROR,
        input={"secret": "production-input"},
        output={"secret": "production-output"},
        started_at=started,
        ended_at=started + timedelta(seconds=1),
        spans=[
            Span(
                span_id=uuid4(),
                trace_id=trace_id,
                parent_span_id=None,
                name="failed.operation",
                kind=SpanKind.OTHER,
                status=SpanStatusCode.ERROR,
                started_at=started,
                ended_at=started + timedelta(seconds=1),
            )
        ],
    )
    await TraceRepository(session).upsert_trace(trace)
    await session.commit()
    return trace_id


async def _create_project_b(session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    session.add(OrganizationModel(id=PROJECT_B_ORG_ID, name="Project B Org", created_at=now))
    session.add(
        ProjectModel(
            id=PROJECT_B_ID,
            org_id=PROJECT_B_ORG_ID,
            name="Project B",
            description="",
            created_at=now,
        )
    )
    await session.commit()


async def _run_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(EvaluationRunModel))
    return result.scalar_one()


async def test_feedback_to_evaluation_result_preserves_trace_evidence(db_session: AsyncSession) -> None:
    trace_id = await _seed_failing_trace(db_session)

    service = TraceFeedbackService(session=db_session)
    command = TraceFeedbackCommand(
        project_id=TEST_PROJECT_ID,
        trace_id=trace_id,
        dataset_id="dataset",
        dataset_version="v2",
        parent_dataset_version="v1",
        case_id="case-f",
        case_version="v1",
        input_payload={"sanitized_input": True},
        expected_output={"answer": 42},
        tags=("sanitized", "production-private"),
        metadata={"source": "feedback"},
        base_case_refs=(CaseVersionRef("case-a", "v1"),),
    )
    test_case, dataset = await service.create_feedback_case(command)

    # Feedback itself never persists and never triggers evaluation.
    assert test_case.evidence_refs == (trace_evidence_ref(trace_id),)
    assert test_case.input_payload == {"sanitized_input": True}
    assert dataset.parent_version == "v1"
    assert {ref.case_id for ref in dataset.case_version_refs} == {"case-a", "case-f"}
    assert await _run_count(db_session) == 0

    # Explicit evaluation handoff: create_run → execute_attempt.
    case_ref = CaseVersionRef("case-f", "v1")
    suite = EvaluationSuiteVersion(
        "suite",
        "s1",
        (case_ref,),
        (spec := _spec(),),
        EvaluationPolicy(
            evaluator_error=PolicyDisposition.FAIL,
            evaluator_inconclusive=PolicyDisposition.INCONCLUSIVE,
        ),
        NOW,
    )
    target_ref = ExecutionTargetRef("fixture-target", "FIXTURE", VersionRef("fixture-set", "v1"))
    persistence = EvaluationPersistenceService(
        lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
    )
    run, (attempt,) = await persistence.create_run(
        project_id=TEST_PROJECT_ID,
        dataset=dataset,
        suite=suite,
        cases={case_ref: test_case},
        target=target_ref,
        timeout=timedelta(seconds=30),
    )
    fixture = FixtureExecution(
        OutcomeKind.SUCCESS,
        NOW,
        NOW,
        output_artifact_ref=ArtifactRef("artifact", "sha256:feedback-artifact"),
    )
    target = FixtureExecutionTarget(target_ref, {case_ref: fixture})
    loop = EvaluationLoopService(
        persistence,
        TargetResolver(target),
        EvaluatorResolver(SimpleEvaluator(spec)),
    )
    assert (
        await loop.execute_attempt(TEST_PROJECT_ID, attempt.attempt_id, test_case, lease=timedelta(minutes=5))
        is EvaluationLoopResult.PROGRESSED
    )

    results = await persistence.list_results(TEST_PROJECT_ID, run.run_id, attempt.attempt_id)
    assert len(results) == 1
    assert trace_evidence_ref(trace_id) in results[0].evidence_refs


async def test_cross_project_feedback_fails_closed(db_session: AsyncSession) -> None:
    trace_id = await _seed_failing_trace(db_session)
    await _create_project_b(db_session)

    service = TraceFeedbackService(session=db_session)
    command = TraceFeedbackCommand(
        project_id=PROJECT_B_ID,
        trace_id=trace_id,
        dataset_id="dataset",
        dataset_version="v1",
        case_id="case-f",
        case_version="v1",
        input_payload={"sanitized_input": True},
    )
    with pytest.raises(TraceFeedbackCandidateError):
        await service.create_feedback_case(command)
    assert await _run_count(db_session) == 0


async def test_feedback_on_non_failing_trace_fails_closed(db_session: AsyncSession) -> None:
    trace_id = uuid4()
    started = NOW - timedelta(hours=1)
    await TraceRepository(db_session).upsert_trace(
        Trace(
            trace_id=trace_id,
            project_id=TEST_PROJECT_ID,
            name="success-trace",
            status=TraceStatus.COMPLETED,
            started_at=started,
            ended_at=started + timedelta(seconds=1),
            spans=[
                Span(
                    span_id=uuid4(),
                    trace_id=trace_id,
                    parent_span_id=None,
                    name="ok.operation",
                    kind=SpanKind.OTHER,
                    status=SpanStatusCode.OK,
                    started_at=started,
                    ended_at=started + timedelta(seconds=1),
                )
            ],
        )
    )
    await db_session.commit()

    service = TraceFeedbackService(session=db_session)
    command = TraceFeedbackCommand(
        project_id=TEST_PROJECT_ID,
        trace_id=trace_id,
        dataset_id="dataset",
        dataset_version="v1",
        case_id="case-f",
        case_version="v1",
        input_payload={"sanitized_input": True},
    )
    with pytest.raises(TraceFeedbackCandidateError):
        await service.create_feedback_case(command)
    assert await _run_count(db_session) == 0
