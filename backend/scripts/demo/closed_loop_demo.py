"""AgentEvalOps closed-loop demo driver (DEMO / SYNTHETIC).

One documented command walks the full Stage-4 closed loop on a fresh
environment:

    Production-like SYNTHETIC failing Trace
      -> TraceFeedbackService (caller-sanitized input)
      -> TestCaseVersion / DatasetVersion (in-memory)
      -> create_run / execute_attempt (Fixture target + deterministic evaluator)
      -> persisted EvaluationResults (with the Trace EvidenceRef)
      -> EvaluationComparisonService (baseline vs candidate)
      -> RegressionReportService -> ReleaseDecision

This module is DEMO / ONBOARDING hardening, not evaluation architecture.
It only orchestrates the frozen Phase1-4 owners:

- classification owner = EvaluationComparisonService
- release decision owner = RegressionReportService
- criticality owner = caller (the driver declares ``critical_case_refs``)
- evaluation execution owner = EvaluationPersistenceService + EvaluationLoopService

It adds no business truth, no schema, no API and no Celery task.  The
baseline/candidate difference is genuinely produced by the ExecutionTarget
output (artifact digest) and consumed by a deterministic evaluator; nothing
is hand-built into EvaluationResult rows.

Usage (from ``backend/``):

    uv run python -m scripts.demo.closed_loop_demo --scenario fail
    uv run python -m scripts.demo.closed_loop_demo --scenario pass --json-output demo-report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.evaluation import FixtureExecution, FixtureExecutionTarget
from app.core.evaluation import (
    ArtifactRef,
    CapabilityRequirement,
    CaseVersionRef,
    DatasetVersion,
    EvaluationPolicy,
    EvaluationSuiteVersion,
    EvaluationVerdict,
    EvaluatorKind,
    EvaluatorSpec,
    ExecutionTargetRef,
    OutcomeKind,
    PolicyDisposition,
    ScoreDirection,
    TestCaseVersion,
    VersionRef,
)
from app.core.evaluation.comparison import AlignedResultComparison, EvaluationRunComparison
from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.feedback import TraceFeedbackCommand
from app.core.evaluation.report import RegressionReport
from app.core.evaluation.results import EvaluationResultDraft
from app.core.evaluation.run_attempts import EvaluationRun, ExecutionAttempt, RunStatus
from app.core.online.entities import trace_evidence_ref
from app.core.traces.entities import Span, Trace
from app.infrastructure.db.engine import engine as default_engine
from app.infrastructure.db.models import OrganizationModel, ProjectModel
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)
from app.infrastructure.db.repositories.trace_repo import TraceRepository
from app.registry.constants import SpanKind, SpanStatusCode, TraceStatus
from app.services.evaluation import (
    EvaluationComparisonService,
    EvaluationLoopResult,
    EvaluationLoopService,
    EvaluationPersistenceService,
    RegressionReportService,
    ResolvedEvaluator,
    TraceFeedbackService,
)

DEMO_DATASET_ID = "demo-agent-regression"
DEMO_SUITE_ID = "demo-agent-regression-suite"
DEMO_TARGET_ID = "demo-fixture-target"

# Runtime DSN source: explicit --dsn > this env var > project database configuration.
DEMO_DATABASE_URL_ENV = "AGENTEVALOPS_DEMO_DATABASE_URL"

CASE_ROUTING = "demo-routing-critical"
CASE_RAG = "demo-rag-grounding"
CASE_TOOL_CONTRACT = "demo-tool-contract"
CASE_IDS = (CASE_ROUTING, CASE_RAG, CASE_TOOL_CONTRACT)

SCENARIOS = ("fail", "pass")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _case_ref(case_id: str) -> CaseVersionRef:
    return CaseVersionRef(case_id, "v1")


CASE_REFS = {case_id: _case_ref(case_id) for case_id in CASE_IDS}


def _evaluator_spec() -> EvaluatorSpec:
    return EvaluatorSpec(
        evaluator_id="demo-quality",
        evaluator_version="v1",
        evaluator_kind=EvaluatorKind.DETERMINISTIC,
        config_ref=VersionRef("DEMO-CONFIG", "v1"),
        score_direction=ScoreDirection.HIGHER_IS_BETTER,
        config_snapshot={"demo": True, "driver": "artifact-digest"},
        score_range=(0.0, 1.0),
        comparison_tolerance=0.0,
        prompt_ref=VersionRef("DEMO-PROMPT", "v1"),
        required=True,
    )


EVALUATOR_SPEC = _evaluator_spec()

TARGET_REF = ExecutionTargetRef(
    target_id=DEMO_TARGET_ID,
    target_kind="FIXTURE",
    target_version_ref=VersionRef("DEMO", "v1"),
    capabilities=("DEMO-EXECUTION",),
    config_ref=VersionRef("DEMO-CONFIG", "v1"),
)


def _verdict_tables(scenario: str) -> dict[str, dict[str, EvaluationVerdict]]:
    """Deterministic per-run verdict tables for the three demo cases.

    Baseline is healthy except ``demo-rag-grounding`` (FAIL) so the candidate
    improvement is visible.  The FAIL scenario regresses the critical
    ``demo-tool-contract`` case (baseline PASS -> candidate FAIL) which drives
    ReleaseDecision.FAIL; the PASS scenario keeps it PASS.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown demo scenario: {scenario!r}")
    baseline = {case_id: EvaluationVerdict.PASS for case_id in CASE_IDS}
    baseline[CASE_RAG] = EvaluationVerdict.FAIL
    candidate = {case_id: EvaluationVerdict.PASS for case_id in CASE_IDS}
    if scenario == "fail":
        candidate[CASE_TOOL_CONTRACT] = EvaluationVerdict.FAIL
    return {"baseline": baseline, "candidate": candidate}


def _fixture_map(run_marker: str, case_refs: tuple[CaseVersionRef, ...]) -> dict[CaseVersionRef, FixtureExecution]:
    """One deterministic SUCCESS fixture per case; the artifact digest encodes the run.

    ``baseline:<case_id>`` vs ``candidate:<case_id>`` is the only run-varying
    signal the evaluator is allowed to consume (target output, not run ids).
    """
    now = _now()
    return {
        ref: FixtureExecution(
            OutcomeKind.SUCCESS,
            now,
            now,
            output_artifact_ref=ArtifactRef(
                artifact_id=f"demo-artifact-{ref.case_id}",
                digest=f"{run_marker}:{ref.case_id}",
                media_type="application/json",
                metadata={"demo": True, "synthetic": True},
            ),
        )
        for ref in case_refs
    }


class DemoQualityEvaluator:
    """Deterministic demo evaluator that judges the target's output artifact.

    Implements the frozen ``Evaluator`` port; verdicts derive from the artifact
    digest produced by the ExecutionTarget, never from run identity.  No LLM,
    no API key, no network — 100% deterministic on a fresh environment.
    """

    def __init__(self, spec: EvaluatorSpec, verdict_tables: Mapping[str, Mapping[str, EvaluationVerdict]]) -> None:
        self._spec = spec
        self._tables = verdict_tables

    async def evaluate(
        self,
        evaluation_input: EvaluationInput,
        context: EvaluatorContext,
    ) -> EvaluationResultDraft:
        """Judge the target output artifact digest and return a deterministic draft."""
        artifact = evaluation_input.actual_artifact
        digest = artifact.digest if artifact is not None else None
        run_marker = digest.split(":", 1)[0] if digest and ":" in digest else None
        table = self._tables.get(run_marker) if run_marker is not None else None
        if table is None:
            raise ValueError(f"unknown demo artifact digest: {digest!r}")
        verdict = table[evaluation_input.case_ref.case_id]
        return EvaluationResultDraft(
            evaluator_id=self._spec.evaluator_id,
            evaluator_version=self._spec.evaluator_version,
            config_ref=self._spec.config_ref,
            verdict=verdict,
            reason=f"demo:{run_marker}:{evaluation_input.case_ref.case_id}",
            score=1.0 if verdict is EvaluationVerdict.PASS else 0.0,
            prompt_ref=self._spec.prompt_ref,
        )


class DemoEvaluatorResolver:
    """Resolves every EvaluatorSpec to the shared demo evaluator."""

    def __init__(self, evaluator: DemoQualityEvaluator) -> None:
        self._evaluator = evaluator

    def resolve(self, spec: EvaluatorSpec) -> ResolvedEvaluator:
        """Bind the shared demo evaluator to the spec's frozen identity."""
        return ResolvedEvaluator(spec.evaluator_id, spec.evaluator_version, self._evaluator)


class DemoTargetResolver:
    """Resolves the persisted target ref to a run-specific FixtureExecutionTarget."""

    def __init__(self, target: FixtureExecutionTarget) -> None:
        self._target = target

    def resolve(self, target_ref: ExecutionTargetRef) -> FixtureExecutionTarget:
        """Return the run-specific fixture target for the persisted ref."""
        return self._target


@dataclass(frozen=True, slots=True)
class DemoResult:
    """Typed outcome of a closed-loop demo run (all data is SYNTHETIC)."""

    scenario: str
    org_id: UUID
    project_id: UUID
    trace_id: UUID
    dataset: DatasetVersion
    baseline_run_id: UUID
    candidate_run_id: UUID
    comparison: EvaluationRunComparison
    report: RegressionReport


class DemoError(RuntimeError):
    """Closed-loop demo orchestration failure (not a gate decision)."""


async def ensure_demo_project(session: AsyncSession, *, now: datetime | None = None) -> tuple[UUID, UUID]:
    """Create an isolated DEMO org + project with fresh UUIDs (never touches real data)."""
    created_at = now or _now()
    org_id = uuid4()
    project_id = uuid4()
    session.add(
        OrganizationModel(id=org_id, name=f"Demo Org {org_id.hex[:8]} (SYNTHETIC)", created_at=created_at)
    )
    session.add(
        ProjectModel(
            id=project_id,
            org_id=org_id,
            name="Demo Project (SYNTHETIC closed-loop demo)",
            description="Created by scripts/demo/closed_loop_demo.py; all data is synthetic.",
            created_at=created_at,
        )
    )
    await session.commit()
    return org_id, project_id


async def seed_failing_trace(
    session: AsyncSession,
    *,
    project_id: UUID,
    now: datetime | None = None,
) -> UUID:
    """Persist one deterministic failing Trace under the demo project.

    Uses the same LocalAgent-compatible legacy path the Phase4 feedback test
    exercises: ``TraceRepository.upsert_trace`` computes the normalized
    projection so the Trace is a valid failing candidate.
    """
    created_at = now or _now()
    trace_id = uuid4()
    started = created_at - timedelta(hours=1)
    trace = Trace(
        trace_id=trace_id,
        project_id=project_id,
        name="demo-tool-contract-failure",
        status=TraceStatus.ERROR,
        input={"demo": True, "synthetic": True, "tool": "contract", "payload": "already-sanitized"},
        output={"demo": True, "synthetic": True, "error": "schema_mismatch"},
        metadata={"demo": True, "synthetic": True},
        started_at=started,
        ended_at=started + timedelta(seconds=1),
        tags=("DEMO", "SYNTHETIC"),
        environment="demo",
        release="demo-v1",
        spans=[
            Span(
                span_id=uuid4(),
                trace_id=trace_id,
                parent_span_id=None,
                name="tool.contract.call",
                kind=SpanKind.TOOL,
                status=SpanStatusCode.ERROR,
                started_at=started,
                ended_at=started + timedelta(seconds=1),
                input={"demo": True, "synthetic": True},
                output={"demo": True, "synthetic": True, "error": "schema_mismatch"},
            )
        ],
    )
    await TraceRepository(session).upsert_trace(trace)
    await session.commit()
    return trace_id


async def _build_cases(
    session: AsyncSession,
    *,
    project_id: UUID,
    trace_id: UUID,
    scenario: str,
    now: datetime,
) -> tuple[TestCaseVersion, TestCaseVersion, TestCaseVersion, DatasetVersion]:
    """Case C comes from the real Trace feedback path; A and B are direct demo fixtures."""
    feedback = TraceFeedbackService(session=session)
    feedback_case, feedback_dataset = await feedback.create_feedback_case(
        TraceFeedbackCommand(
            project_id=project_id,
            trace_id=trace_id,
            dataset_id=DEMO_DATASET_ID,
            dataset_version="v1",
            dataset_name="Demo Agent Regression (SYNTHETIC)",
            case_id=CASE_TOOL_CONTRACT,
            case_version="v1",
            case_name="demo: tool contract compliance",
            input_payload={"demo": True, "synthetic": True, "tool": "contract", "expected": {"schema": "v1"}},
            expected_output={"schema": "v1"},
            base_case_refs=(CASE_REFS[CASE_ROUTING], CASE_REFS[CASE_RAG]),
            tags=("DEMO", "SYNTHETIC"),
            metadata={"demo": True, "synthetic": True, "scenario": scenario},
        )
    )
    case_routing = TestCaseVersion(
        case_id=CASE_ROUTING,
        version="v1",
        name="demo: routing critical path",
        input_payload={"demo": True, "synthetic": True, "scenario": "routing", "instruction": "route to the sales agent"},
        created_at=now,
        expected_output={"route": "sales-agent"},
        tags=("DEMO", "SYNTHETIC"),
        metadata={"demo": True, "synthetic": True, "case": "A"},
    )
    case_rag = TestCaseVersion(
        case_id=CASE_RAG,
        version="v1",
        name="demo: RAG grounding quality",
        input_payload={"demo": True, "synthetic": True, "scenario": "rag", "instruction": "answer from provided docs only"},
        created_at=now,
        expected_output={"grounded": True},
        tags=("DEMO", "SYNTHETIC"),
        metadata={"demo": True, "synthetic": True, "case": "B"},
    )
    return case_routing, case_rag, feedback_case, feedback_dataset


def _suite(scenario: str, *, now: datetime) -> EvaluationSuiteVersion:
    return EvaluationSuiteVersion(
        suite_id=DEMO_SUITE_ID,
        version="v1",
        case_selection=tuple(CASE_REFS[case_id] for case_id in CASE_IDS),
        evaluator_specs=(EVALUATOR_SPEC,),
        evaluation_policy=EvaluationPolicy(
            required_result_missing=PolicyDisposition.FAIL,
            evaluator_error=PolicyDisposition.FAIL,
            evaluator_inconclusive=PolicyDisposition.INCONCLUSIVE,
        ),
        created_at=now,
        target_capability_requirements=(CapabilityRequirement("DEMO-EXECUTION"),),
        metadata={"demo": True, "synthetic": True, "scenario": scenario},
    )


async def _execute_run(
    persistence: EvaluationPersistenceService,
    loop: EvaluationLoopService,
    *,
    project_id: UUID,
    run: EvaluationRun,
    attempts: tuple[ExecutionAttempt, ...],
    cases: Mapping[CaseVersionRef, TestCaseVersion],
) -> None:
    """Drive every attempt through the real loop, then require a COMPLETED run.

    ``RUN_NOT_READY`` is expected for every attempt except the last: the run
    can only finish once all attempts are terminal and required slots are full.
    """
    accepted = {
        EvaluationLoopResult.PROGRESSED,
        EvaluationLoopResult.ALREADY_COMPLETE,
        EvaluationLoopResult.RUN_NOT_READY,
    }
    for attempt in attempts:
        outcome = await loop.execute_attempt(
            project_id,
            attempt.attempt_id,
            cases[attempt.case_ref],
            lease=timedelta(minutes=10),
            worker_ref="closed-loop-demo",
            task_ref="scripts.demo.closed_loop_demo",
        )
        if outcome not in accepted:
            raise DemoError(f"demo attempt {attempt.attempt_id} ended with {outcome.value}")
    stored = await persistence.get_run(project_id, run.run_id)
    if stored.status is not RunStatus.COMPLETED:
        raise DemoError(f"demo run {run.run_id} did not COMPLETE (status={stored.status.value})")


def _default_uow_factory() -> PostgresEvaluationPersistenceUnitOfWork:
    """UoW over the app-configured async engine (used when no factory is passed)."""
    from app.infrastructure.db.engine import async_session_factory

    return PostgresEvaluationPersistenceUnitOfWork(async_session_factory)


async def run_closed_loop_demo(
    session: AsyncSession,
    *,
    uow_factory: Callable[[], PostgresEvaluationPersistenceUnitOfWork] | None = None,
    scenario: str = "fail",
) -> DemoResult:
    """Orchestrate the full closed loop on the given session (DEMO / SYNTHETIC data)."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown demo scenario: {scenario!r}")

    uow_factory = uow_factory or _default_uow_factory

    now = _now()
    org_id, project_id = await ensure_demo_project(session, now=now)
    trace_id = await seed_failing_trace(session, project_id=project_id, now=now)

    case_routing, case_rag, feedback_case, dataset = await _build_cases(
        session, project_id=project_id, trace_id=trace_id, scenario=scenario, now=now
    )
    cases = {
        CASE_REFS[CASE_ROUTING]: case_routing,
        CASE_REFS[CASE_RAG]: case_rag,
        CASE_REFS[CASE_TOOL_CONTRACT]: feedback_case,
    }

    persistence = EvaluationPersistenceService(uow_factory)
    suite = _suite(scenario, now=now)
    baseline_run, baseline_attempts = await persistence.create_run(
        project_id=project_id,
        dataset=dataset,
        suite=suite,
        cases=cases,
        target=TARGET_REF,
        timeout=timedelta(seconds=30),
    )
    candidate_run, candidate_attempts = await persistence.create_run(
        project_id=project_id,
        dataset=dataset,
        suite=suite,
        cases=cases,
        target=TARGET_REF,
        timeout=timedelta(seconds=30),
    )

    evaluator = DemoQualityEvaluator(EVALUATOR_SPEC, _verdict_tables(scenario))
    baseline_target = FixtureExecutionTarget(TARGET_REF, _fixture_map("baseline", suite.case_selection))
    candidate_target = FixtureExecutionTarget(TARGET_REF, _fixture_map("candidate", suite.case_selection))
    baseline_loop = EvaluationLoopService(
        persistence, DemoTargetResolver(baseline_target), DemoEvaluatorResolver(evaluator)
    )
    candidate_loop = EvaluationLoopService(
        persistence, DemoTargetResolver(candidate_target), DemoEvaluatorResolver(evaluator)
    )

    await _execute_run(
        persistence, baseline_loop, project_id=project_id, run=baseline_run, attempts=baseline_attempts, cases=cases
    )
    await _execute_run(
        persistence, candidate_loop, project_id=project_id, run=candidate_run, attempts=candidate_attempts, cases=cases
    )

    comparison = await EvaluationComparisonService(persistence).compare_runs(
        project_id, baseline_run.run_id, candidate_run.run_id
    )
    report = RegressionReportService().build_report(comparison, critical_case_refs=(CASE_REFS[CASE_TOOL_CONTRACT],))
    return DemoResult(
        scenario=scenario,
        org_id=org_id,
        project_id=project_id,
        trace_id=trace_id,
        dataset=dataset,
        baseline_run_id=baseline_run.run_id,
        candidate_run_id=candidate_run.run_id,
        comparison=comparison,
        report=report,
    )


async def cleanup_demo_data(session: AsyncSession, *, org_id: UUID, project_id: UUID) -> None:
    """Delete only the demo project (cascades traces + eval rows) and the demo org."""
    await session.execute(delete(ProjectModel).where(ProjectModel.id == project_id))
    await session.execute(delete(OrganizationModel).where(OrganizationModel.id == org_id))
    await session.commit()


def _classification_row(item: AlignedResultComparison) -> str:
    score = "" if item.score_delta is None else f"  (score_delta={item.score_delta:+.1f})"
    return f"  {item.case_id}@{item.case_version}  {item.classification.value}{score}"


def render_console(result: DemoResult) -> str:
    """Human-readable console output; the gate decision always comes from the Report."""
    report = result.report
    lines = [
        "=" * 56,
        "AgentEvalOps Closed-Loop Demo (SYNTHETIC)",
        "=" * 56,
        f"Scenario: {result.scenario}",
        f"Project:  {result.project_id}",
        f"Dataset:  {result.dataset.dataset_id} @ {result.dataset.version}",
        "",
        "Trace feedback chain:",
        f"  failing Trace {result.trace_id} -> TraceFeedbackService -> TestCaseVersion",
        f"  Trace EvidenceRef: kind=trace identifier={result.trace_id}",
        "  EvidenceRef propagated to EvaluationResult: yes",
        "",
        f"Baseline Run:  {result.baseline_run_id}",
        f"Candidate Run: {result.candidate_run_id}",
        "",
        "Comparison:",
        f"- unchanged: {report.unchanged_count}",
        f"- improvements: {report.improvement_count}",
        f"- regressions: {report.regression_count}",
        f"- not comparable: {report.not_comparable_count}",
    ]
    for item in report.comparisons:
        lines.append(_classification_row(item))
    lines.append("")
    lines.append("Critical blockers:")
    if report.critical_regressions or report.critical_not_comparable:
        for item in (*report.critical_regressions, *report.critical_not_comparable):
            lines.append(f"  {item.case_id}@{item.case_version} {item.classification.value} (critical)")
    else:
        lines.append("  none")
    lines.append("")
    lines.append(f"Release Decision: {report.release_decision.value}")
    lines.append("=" * 56)
    return "\n".join(lines)


def report_payload(result: DemoResult) -> dict[str, object]:
    """JSON-serializable artifact payload (no secrets, no production claims)."""
    report = result.report
    return {
        "demo": True,
        "synthetic": True,
        "scenario": result.scenario,
        "project_id": str(result.project_id),
        "dataset_id": result.dataset.dataset_id,
        "dataset_version": result.dataset.version,
        "trace_id": str(result.trace_id),
        "trace_evidence_ref": {
            "kind": "trace",
            "identifier": str(result.trace_id),
        },
        "baseline_run_id": str(result.baseline_run_id),
        "candidate_run_id": str(result.candidate_run_id),
        "comparison": {
            "unchanged": report.unchanged_count,
            "improvements": report.improvement_count,
            "regressions": report.regression_count,
            "not_comparable": report.not_comparable_count,
        },
        "critical_case_refs": [
            {"case_id": ref.case_id, "version": ref.version} for ref in report.critical_case_refs
        ],
        "critical_blockers": [
            {
                "case_id": item.case_id,
                "case_version": item.case_version,
                "classification": item.classification.value,
                "reason": item.reason.value,
            }
            for item in (*report.critical_regressions, *report.critical_not_comparable)
        ],
        "release_decision": report.release_decision.value,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="closed_loop_demo",
        description="AgentEvalOps closed-loop demo (SYNTHETIC data; exit 0 = demo orchestration succeeded).",
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        default="fail",
        help="'fail' regresses the critical case -> ReleaseDecision.FAIL (default); 'pass' -> PASS.",
    )
    parser.add_argument("--json-output", default=None, metavar="PATH", help="write the JSON artifact to this path")
    parser.add_argument(
        "--dsn",
        default=None,
        metavar="DATABASE_URL",
        help=(
            "PostgreSQL DSN (postgresql+asyncpg://...). Defaults to the "
            f"{DEMO_DATABASE_URL_ENV} environment variable, then to the project "
            "database configuration (POSTGRES_* / .env)."
        ),
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete only the demo org/project rows after the run (never touches other data)",
    )
    return parser


def _resolve_dsn(cli_dsn: str | None) -> str | None:
    """Resolve the DSN at runtime: explicit --dsn > env var > project config.

    Returns ``None`` when no explicit DSN is available so the caller reuses the
    project database configuration.  Credentials only ever come from runtime
    environment/args — never from argparse defaults, help text, docs or the
    Makefile.
    """
    if cli_dsn:
        return cli_dsn
    return os.environ.get(DEMO_DATABASE_URL_ENV) or None


async def main(argv: list[str] | None = None) -> int:
    """CLI entry: always exits 0 when the demo orchestration succeeds.

    A ReleaseDecision.FAIL is the intended demo outcome and therefore does NOT
    map to a non-zero exit — that mapping belongs to the WP2 CI release gate.
    """
    args = _build_parser().parse_args(argv)

    dsn = _resolve_dsn(args.dsn)
    if dsn is not None:
        engine = create_async_engine(dsn)
        dispose_engine = True
    else:
        engine = default_engine
        dispose_engine = False
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    uow_factory = partial(PostgresEvaluationPersistenceUnitOfWork, session_factory)

    async with session_factory() as session:
        result = await run_closed_loop_demo(session, uow_factory=uow_factory, scenario=args.scenario)
        print(render_console(result))
        if args.json_output is not None:
            path = Path(args.json_output)
            path.write_text(json.dumps(report_payload(result), indent=2) + "\n", encoding="utf-8")
            print(f"\nJSON artifact written to {path}")
        if args.cleanup:
            await cleanup_demo_data(session, org_id=result.org_id, project_id=result.project_id)
            print(f"Cleaned up demo project {result.project_id} (only demo-owned rows)")
    if dispose_engine:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
