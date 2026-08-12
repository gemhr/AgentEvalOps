"""Minimal Evaluation Loop 的真实 PostgreSQL application integration。"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.adapters.evaluation import FixtureExecution, FixtureExecutionTarget
from app.core.evaluation import (
    ArtifactRef,
    CapabilityRequirement,
    CaseVersionRef,
    DatasetVersion,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationResultDraft,
    EvaluationSuiteVersion,
    EvaluationVerdict,
    EvaluatorKind,
    EvaluatorSpec,
    ExecutionOutcome,
    ExecutionTargetRef,
    OutcomeKind,
    PolicyDisposition,
    ProvenanceCompleteness,
    ScoreDirection,
    TestCaseVersion as CaseVersion,
    VersionRef,
)
from app.core.evaluation.run_attempts import AttemptStatus, EvaluationEntityNotFound, RunStatus
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import PostgresEvaluationPersistenceUnitOfWork
from app.services.evaluation import (
    EvaluationLoopResult,
    EvaluationLoopService,
    EvaluationPersistenceService,
    ResolvedEvaluator,
)

from .conftest import TEST_PROJECT_ID

NOW = datetime(2026, 8, 12, 14, tzinfo=timezone.utc)


@dataclass
class UowProbe:
    active: int = 0
    entered: int = 0
    current_task_active: ContextVar[int] = ContextVar("wp4_uow_active", default=0)


class TrackingUow:
    """只为 integration 证明 external call 期间没有活跃 persistence UoW。"""

    def __init__(self, probe: UowProbe):
        self._probe = probe
        self._delegate = PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
        self._context_token = None

    async def __aenter__(self):
        self._probe.active += 1
        self._probe.entered += 1
        self._context_token = self._probe.current_task_active.set(self._probe.current_task_active.get() + 1)
        entered = await self._delegate.__aenter__()
        self.runs = entered.runs
        self.attempts = entered.attempts
        self.results = entered.results
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            await self._delegate.__aexit__(exc_type, exc, tb)
        finally:
            self._probe.active -= 1
            self._probe.current_task_active.reset(self._context_token)

    async def commit(self):
        await self._delegate.commit()

    async def rollback(self):
        await self._delegate.rollback()


class ProbeTarget:
    def __init__(self, delegate: FixtureExecutionTarget, probe: UowProbe):
        self._delegate = delegate
        self._probe = probe
        self.calls = 0
        self.resolved_refs: list[ExecutionTargetRef] = []

    @property
    def target_ref(self):
        return self._delegate.target_ref

    async def execute(self, request):
        assert self._probe.current_task_active.get() == 0
        self.calls += 1
        return await self._delegate.execute(request)


class TargetResolver:
    def __init__(self, target: ProbeTarget):
        self.target = target

    def resolve(self, target_ref):
        self.target.resolved_refs.append(target_ref)
        return self.target


class ProbeEvaluator:
    def __init__(
        self,
        spec: EvaluatorSpec,
        probe: UowProbe,
        *,
        verdict: EvaluationVerdict = EvaluationVerdict.PASS,
        barrier: EvaluatorBarrier | None = None,
    ):
        self.spec = spec
        self.probe = probe
        self.verdict = verdict
        self.barrier = barrier
        self.calls = 0

    async def evaluate(self, evaluation_input, context):
        assert self.probe.current_task_active.get() == 0
        self.calls += 1
        if self.barrier is not None:
            await self.barrier.wait()
        return EvaluationResultDraft(
            self.spec.evaluator_id,
            self.spec.evaluator_version,
            self.spec.config_ref,
            self.verdict,
            "integration evaluator",
            score=0.0 if self.verdict is EvaluationVerdict.FAIL else 1.0,
            prompt_ref=self.spec.prompt_ref,
        )


class EvaluatorResolver:
    def __init__(self, evaluators: dict[str, ProbeEvaluator]):
        self.evaluators = evaluators

    def resolve(self, spec):
        return ResolvedEvaluator(spec.evaluator_id, spec.evaluator_version, self.evaluators[spec.evaluator_id])


class EvaluatorBarrier:
    def __init__(self, parties: int):
        self.parties = parties
        self.arrived = 0
        self.event = asyncio.Event()

    async def wait(self):
        self.arrived += 1
        if self.arrived >= self.parties:
            self.event.set()
        await asyncio.wait_for(self.event.wait(), timeout=5)


def make_spec(evaluator_id: str, *, required: bool = True) -> EvaluatorSpec:
    return EvaluatorSpec(
        evaluator_id,
        "v1",
        EvaluatorKind.DETERMINISTIC,
        VersionRef("config", f"{evaluator_id}-config"),
        ScoreDirection.HIGHER_IS_BETTER,
        score_range=(0.0, 1.0),
        prompt_ref=VersionRef("prompt", f"{evaluator_id}-prompt"),
        required=required,
    )


def persistence(probe: UowProbe) -> EvaluationPersistenceService:
    return EvaluationPersistenceService(lambda: TrackingUow(probe))


async def seed(
    probe: UowProbe,
    *,
    specs: tuple[EvaluatorSpec, ...],
    case_id: str = "case-a",
    target_ref: ExecutionTargetRef | None = None,
    target_requirements: tuple[CapabilityRequirement, ...] = (),
) -> tuple[EvaluationPersistenceService, object, object, CaseVersion, ExecutionTargetRef]:
    case_ref = CaseVersionRef(case_id, "v1")
    case = CaseVersion(case_id, "v1", case_id, {"question": case_id}, NOW, expected_output={"answer": 42})
    dataset = DatasetVersion("dataset", "d1", "dataset", NOW, case_version_refs=(case_ref,))
    suite = EvaluationSuiteVersion(
        "suite",
        "s1",
        (case_ref,),
        specs,
        EvaluationPolicy(
            evaluator_error=PolicyDisposition.FAIL,
            evaluator_inconclusive=PolicyDisposition.INCONCLUSIVE,
        ),
        NOW,
        target_requirements,
    )
    target_ref = target_ref or ExecutionTargetRef("fixture-target", "FIXTURE", VersionRef("fixture-set", "v1"))
    service = persistence(probe)
    run, (attempt,) = await service.create_run(
        project_id=TEST_PROJECT_ID,
        dataset=dataset,
        suite=suite,
        cases={case_ref: case},
        target=target_ref,
        timeout=timedelta(seconds=30),
    )
    return service, run, attempt, case, target_ref


def loop_for(
    service: EvaluationPersistenceService,
    probe: UowProbe,
    target_ref: ExecutionTargetRef,
    case_ref: CaseVersionRef,
    specs: tuple[EvaluatorSpec, ...],
    *,
    outcome: OutcomeKind = OutcomeKind.SUCCESS,
    evaluators: dict[str, ProbeEvaluator] | None = None,
) -> tuple[EvaluationLoopService, ProbeTarget, dict[str, ProbeEvaluator]]:
    fixture = FixtureExecution(
        outcome,
        NOW,
        NOW,
        output_artifact_ref=ArtifactRef("artifact", "sha256:artifact") if outcome is OutcomeKind.SUCCESS else None,
        error_category=None if outcome is OutcomeKind.SUCCESS else "TARGET",
        reason=None if outcome is OutcomeKind.SUCCESS else outcome.value,
    )
    target = ProbeTarget(FixtureExecutionTarget(target_ref, {case_ref: fixture}), probe)
    evaluators = evaluators or {value.evaluator_id: ProbeEvaluator(value, probe) for value in specs}
    return (
        EvaluationLoopService(
            service,
            TargetResolver(target),
            EvaluatorResolver(evaluators),
            uuid_factory=uuid4,
            clock=lambda: NOW,
        ),
        target,
        evaluators,
    )


def result_for(run, attempt, spec: EvaluatorSpec) -> EvaluationResult:
    return EvaluationResult(
        result_id=str(uuid4()),
        run_id=str(run.run_id),
        attempt_id=str(attempt.attempt_id),
        dataset_id="dataset",
        dataset_version="d1",
        case_id=attempt.case_ref.case_id,
        case_version=attempt.case_ref.version,
        suite_id="suite",
        suite_version="s1",
        evaluator_id=spec.evaluator_id,
        evaluator_version=spec.evaluator_version,
        config_ref=spec.config_ref,
        prompt_ref=spec.prompt_ref,
        execution_target_id=attempt.execution_target_ref.target_id,
        target_version_ref=attempt.execution_target_ref.target_version_ref,
        execution_request_id=attempt.execution_request.request_id,
        verdict=EvaluationVerdict.PASS,
        reason="pre-existing",
        provenance_completeness=ProvenanceCompleteness.COMPLETE,
        output_artifact_ref=attempt.output_artifact_ref,
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_full_success_two_evaluators_and_external_calls_have_no_open_uow(db_session):
    specs = (make_spec("required"), make_spec("optional", required=False))
    probe = UowProbe()
    service, run, attempt, case, target_ref = await seed(probe, specs=specs)
    loop, target, evaluators = loop_for(service, probe, target_ref, attempt.case_ref, specs)

    assert await loop.execute_attempt(
        TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=5)
    ) is EvaluationLoopResult.PROGRESSED

    stored_attempt = await service.get_attempt(TEST_PROJECT_ID, attempt.attempt_id)
    stored_run = await service.get_run(TEST_PROJECT_ID, run.run_id)
    results = await service.list_results(TEST_PROJECT_ID, run.run_id, attempt.attempt_id)
    assert stored_attempt.status is AttemptStatus.TERMINAL
    assert stored_attempt.execution_outcome_kind is OutcomeKind.SUCCESS
    assert stored_run.status is RunStatus.COMPLETED
    assert [(item.evaluator_id, item.evaluator_version) for item in results] == [
        ("required", "v1"),
        ("optional", "v1"),
    ]
    assert target.calls == 1
    assert [evaluators[name].calls for name in ("required", "optional")] == [1, 1]
    assert probe.active == 0 and probe.entered > 0


@pytest.mark.asyncio
async def test_capability_and_config_target_projection_completes_postgres_loop(db_session):
    specs = (make_spec("eval"),)
    authoritative = ExecutionTargetRef(
        "fixture-target",
        "FIXTURE",
        VersionRef("fixture-set", "v1"),
        ("TEXT",),
        VersionRef("target-config", "v1"),
    )
    probe = UowProbe()
    service, run, attempt, case, target_ref = await seed(
        probe,
        specs=specs,
        case_id="case-target-projections",
        target_ref=authoritative,
        target_requirements=(CapabilityRequirement("TEXT"),),
    )
    loop, target, _ = loop_for(service, probe, target_ref, attempt.case_ref, specs)

    assert await loop.execute_attempt(
        TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=5)
    ) is EvaluationLoopResult.PROGRESSED
    stored_attempt = await service.get_attempt(TEST_PROJECT_ID, attempt.attempt_id)
    stored_run = await service.get_run(TEST_PROJECT_ID, run.run_id)
    results = await service.list_results(TEST_PROJECT_ID, run.run_id, attempt.attempt_id)
    assert target.resolved_refs == [authoritative]
    assert target.target_ref == authoritative
    assert stored_attempt.status is AttemptStatus.TERMINAL
    assert stored_attempt.execution_outcome_kind is OutcomeKind.SUCCESS
    assert stored_run.status is RunStatus.COMPLETED
    assert len(results) == 1 and results[0].verdict is EvaluationVerdict.PASS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_status"),
    [
        (OutcomeKind.FAILURE, RunStatus.FAILED),
        (OutcomeKind.TIMEOUT, RunStatus.FAILED),
        (OutcomeKind.CANCELLED, RunStatus.FAILED),
        (OutcomeKind.OUTCOME_UNKNOWN, RunStatus.OUTCOME_UNKNOWN),
    ],
)
async def test_confirmed_non_success_has_no_results_and_finishes_run(db_session, kind, expected_status):
    specs = (make_spec("eval"),)
    probe = UowProbe()
    service, run, attempt, case, target_ref = await seed(probe, specs=specs, case_id=f"case-{kind.value.lower()}")
    loop, target, evaluators = loop_for(service, probe, target_ref, attempt.case_ref, specs, outcome=kind)

    assert await loop.execute_attempt(
        TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=5)
    ) is EvaluationLoopResult.PROGRESSED
    assert (await service.get_run(TEST_PROJECT_ID, run.run_id)).status is expected_status
    assert (await service.get_attempt(TEST_PROJECT_ID, attempt.attempt_id)).execution_outcome_kind is kind
    assert await service.list_results(TEST_PROJECT_ID, run.run_id, attempt.attempt_id) == ()
    assert target.calls == 1 and evaluators["eval"].calls == 0


@pytest.mark.asyncio
async def test_terminal_success_reentry_preserves_existing_slot_and_only_fills_missing(db_session):
    specs = (make_spec("existing"), make_spec("missing"))
    probe = UowProbe()
    service, run, attempt, case, target_ref = await seed(probe, specs=specs)
    claim = await service.claim_attempt(TEST_PROJECT_ID, attempt.attempt_id, lease=timedelta(minutes=5))
    running = await service.start_attempt(TEST_PROJECT_ID, attempt.attempt_id, claim.claim_token)
    terminal = await service.record_outcome(
        TEST_PROJECT_ID,
        attempt.attempt_id,
        claim.claim_token,
        ExecutionOutcome(
            running.execution_request.request_id,
            OutcomeKind.SUCCESS,
            NOW,
            NOW,
            ArtifactRef("artifact", "sha256:artifact"),
        ),
    )
    await service.finalize_result(
        TEST_PROJECT_ID,
        attempt.attempt_id,
        claim.claim_token,
        result_for(run, terminal, specs[0]),
    )
    loop, target, evaluators = loop_for(service, probe, target_ref, attempt.case_ref, specs)

    assert await loop.execute_attempt(
        TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=5)
    ) is EvaluationLoopResult.PROGRESSED
    results = await service.list_results(TEST_PROJECT_ID, run.run_id, attempt.attempt_id)
    assert [item.evaluator_id for item in results] == ["existing", "missing"]
    assert target.calls == 0
    assert evaluators["existing"].calls == 0 and evaluators["missing"].calls == 1
    assert (await service.get_run(TEST_PROJECT_ID, run.run_id)).status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_concurrent_duplicate_delivery_claims_and_executes_target_once(db_session):
    specs = (make_spec("eval"),)
    probe = UowProbe()
    service, _, attempt, case, target_ref = await seed(probe, specs=specs)
    loop, target, _ = loop_for(service, probe, target_ref, attempt.case_ref, specs)

    outcomes = await asyncio.gather(
        loop.execute_attempt(TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=5)),
        loop.execute_attempt(TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=5)),
    )
    assert EvaluationLoopResult.NOT_CLAIMED in outcomes
    assert target.calls == 1


@pytest.mark.asyncio
async def test_concurrent_terminal_resume_duplicate_result_converges_to_one_row(db_session):
    specs = (make_spec("eval"),)
    probe = UowProbe()
    service, run, attempt, case, target_ref = await seed(probe, specs=specs)
    claim = await service.claim_attempt(TEST_PROJECT_ID, attempt.attempt_id, lease=timedelta(minutes=5))
    running = await service.start_attempt(TEST_PROJECT_ID, attempt.attempt_id, claim.claim_token)
    await service.record_outcome(
        TEST_PROJECT_ID,
        attempt.attempt_id,
        claim.claim_token,
        ExecutionOutcome(
            running.execution_request.request_id,
            OutcomeKind.SUCCESS,
            NOW,
            NOW,
            ArtifactRef("artifact", "sha256:artifact"),
        ),
    )
    barrier = EvaluatorBarrier(2)
    evaluator = ProbeEvaluator(specs[0], probe, barrier=barrier)
    loop, target, _ = loop_for(
        service,
        probe,
        target_ref,
        attempt.case_ref,
        specs,
        evaluators={"eval": evaluator},
    )

    outcomes = await asyncio.gather(
        loop.execute_attempt(TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=5)),
        loop.execute_attempt(TEST_PROJECT_ID, attempt.attempt_id, case, lease=timedelta(minutes=5)),
    )
    assert set(outcomes) <= {EvaluationLoopResult.PROGRESSED, EvaluationLoopResult.ALREADY_COMPLETE}
    assert len(await service.list_results(TEST_PROJECT_ID, run.run_id, attempt.attempt_id)) == 1
    assert evaluator.calls == 2 and target.calls == 0


@pytest.mark.asyncio
async def test_cross_project_and_query_facade_fail_closed(db_session):
    specs = (make_spec("eval"),)
    probe = UowProbe()
    service, run, attempt, case, target_ref = await seed(probe, specs=specs)
    loop, target, evaluators = loop_for(service, probe, target_ref, attempt.case_ref, specs)
    foreign = uuid4()

    with pytest.raises(EvaluationEntityNotFound):
        await loop.execute_attempt(foreign, attempt.attempt_id, case, lease=timedelta(minutes=5))
    with pytest.raises(EvaluationEntityNotFound):
        await service.get_run(foreign, run.run_id)
    with pytest.raises(EvaluationEntityNotFound):
        await service.get_attempt(foreign, attempt.attempt_id)
    assert await service.list_attempts(foreign, run.run_id) == ()
    assert await service.list_results(foreign, run.run_id, attempt.attempt_id) == ()
    assert target.calls == 0 and evaluators["eval"].calls == 0
