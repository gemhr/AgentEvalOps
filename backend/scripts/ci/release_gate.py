"""AgentEvalOps CI Candidate Gate adapter.

Canonical mode reads two already-persisted EvaluationRuns from AgentEvalOps
PostgreSQL, then delegates comparison and release policy to the existing
``EvaluationComparisonService`` and ``RegressionReportService``. The JSON
written by this command is CI evidence only; it is not a second authority.

``--synthetic --scenario`` is retained solely for fast unit/integration
fixtures. The canonical workflow does not use that path.

Exit contract: ``0`` = PASS, ``2`` = business gate FAIL, ``1`` = technical
error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from functools import partial
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.evaluation.references import CaseVersionRef
from app.core.evaluation.report import RegressionReport, ReleaseDecision
from app.infrastructure.db.engine import engine as default_engine
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)

EXIT_PASS = 0
EXIT_GATE_FAIL = 2
EXIT_ERROR = 1
REPORT_SCHEMA_VERSION = 1
GATE_DATABASE_URL_ENV = "AGENTEVALOPS_GATE_DATABASE_URL"
SYNTHETIC_SCENARIOS = ("fail", "pass")


def exit_code_for_decision(decision: ReleaseDecision) -> int:
    """Map the frozen ReleaseDecision to the stable process exit contract."""
    if decision is ReleaseDecision.PASS:
        return EXIT_PASS
    if decision is ReleaseDecision.FAIL:
        return EXIT_GATE_FAIL
    raise ValueError(f"unknown release decision: {decision!r}")


def serialize_report(
    report: RegressionReport,
    *,
    scenario: str | None = None,
    synthetic: bool = False,
) -> dict[str, object]:
    """Serialize the existing RegressionReport without recomputing its truth."""
    payload: dict[str, object] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "authority": "RegressionReportService",
        "demo": synthetic,
        "synthetic": synthetic,
        "project_id": str(report.project_id),
        "baseline_run_id": str(report.baseline_run_id),
        "candidate_run_id": str(report.candidate_run_id),
        "release_decision": report.release_decision.value,
        "comparison_counts": {
            "total": report.total_count,
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
    }
    if scenario is not None:
        payload["scenario"] = scenario
    return payload


def write_report_artifact(
    path: str,
    report: RegressionReport,
    *,
    scenario: str | None = None,
    synthetic: bool = False,
) -> Path:
    """Write ephemeral CI evidence (never durable evaluation authority)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(serialize_report(report, scenario=scenario, synthetic=synthetic), indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def finalize(
    report_json_path: str | None,
    report: RegressionReport,
    *,
    scenario: str | None = None,
    synthetic: bool = False,
) -> int:
    """Write evidence first, then map the existing decision to process status."""
    if report_json_path:
        write_report_artifact(report_json_path, report, scenario=scenario, synthetic=synthetic)
    return exit_code_for_decision(report.release_decision)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_gate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "0 = Release Gate PASS\n"
            "2 = Release Gate FAIL\n"
            "1 = execution / configuration / contract error"
        ),
        description=(
            "AgentEvalOps Candidate Gate. Canonical mode consumes persisted "
            "baseline/candidate EvaluationRuns. Exit contract: 0 = Release Gate PASS, "
            "2 = Release Gate FAIL (business block), 1 = execution / configuration / "
            "contract error."
        ),
    )
    parser.add_argument("--project-id", type=UUID, help="Project UUID owning both persisted EvaluationRuns.")
    parser.add_argument("--baseline-run-id", type=UUID, help="Persisted COMPLETED baseline EvaluationRun UUID.")
    parser.add_argument("--candidate-run-id", type=UUID, help="Persisted COMPLETED candidate EvaluationRun UUID.")
    parser.add_argument(
        "--critical-case",
        action="append",
        default=[],
        metavar="CASE_ID@VERSION",
        help="Caller-supplied critical case reference; repeat for multiple cases.",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Use synthetic fixture mode (tests only; never production authority).",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        metavar="{pass,fail}",
        help="Synthetic fixture scenario; requires --synthetic and is not a production input.",
    )
    parser.add_argument(
        "--report-json",
        default=None,
        metavar="PATH",
        help="Write ephemeral gate evidence to this path, including when the decision is FAIL.",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        metavar="DATABASE_URL",
        help=(
            "PostgreSQL DSN (postgresql+asyncpg://...). Defaults to the "
            f"{GATE_DATABASE_URL_ENV} environment variable, then project settings."
        ),
    )
    return parser


def _resolve_dsn(explicit: str | None) -> str | None:
    """Resolve an optional isolated DSN without printing it."""
    return explicit or os.environ.get(GATE_DATABASE_URL_ENV)


def _critical_case_refs(values: list[str]) -> tuple[CaseVersionRef, ...]:
    """Parse refs for the existing caller-supplied criticality contract."""
    refs: list[CaseVersionRef] = []
    for value in values:
        case_id, separator, version = value.rpartition("@")
        if not separator or not case_id.strip() or not version.strip():
            raise ValueError(f"invalid --critical-case {value!r}; expected CASE_ID@VERSION")
        refs.append(CaseVersionRef(case_id.strip(), version.strip()))
    if not refs:
        raise ValueError("at least one --critical-case is required for the canonical gate")
    return tuple(refs)


def _engine_for_dsn(dsn: str | None):
    if dsn is None:
        return default_engine, False
    return create_async_engine(dsn), True


async def _run_persisted_gate(args: argparse.Namespace) -> int:
    """Compare persisted real runs and delegate policy to the existing service."""
    from app.services.evaluation.comparison import EvaluationComparisonService
    from app.services.evaluation.persistence import EvaluationPersistenceService
    from app.services.evaluation.report import RegressionReportService

    if args.project_id is None or args.baseline_run_id is None or args.candidate_run_id is None:
        raise ValueError("canonical gate requires --project-id, --baseline-run-id and --candidate-run-id")
    engine, dispose_engine = _engine_for_dsn(_resolve_dsn(args.dsn))
    try:
        session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        uow_factory = partial(PostgresEvaluationPersistenceUnitOfWork, session_factory)
        persistence = EvaluationPersistenceService(uow_factory)
        comparison = await EvaluationComparisonService(persistence).compare_runs(
            args.project_id, args.baseline_run_id, args.candidate_run_id
        )
        report = RegressionReportService().build_report(comparison, _critical_case_refs(args.critical_case))
        return finalize(args.report_json, report, scenario="real", synthetic=False)
    finally:
        if dispose_engine:
            await engine.dispose()


async def _run_synthetic_gate(args: argparse.Namespace) -> int:
    """Run the legacy fixture only when explicitly requested by tests."""
    # Avoid an incidental network fetch while importing the test-only demo.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    from scripts.demo.closed_loop_demo import run_closed_loop_demo

    engine, dispose_engine = _engine_for_dsn(_resolve_dsn(args.dsn))
    try:
        session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        uow_factory = partial(PostgresEvaluationPersistenceUnitOfWork, session_factory)
        async with session_factory() as session:
            result = await run_closed_loop_demo(session, uow_factory=uow_factory, scenario=args.scenario)
        return finalize(args.report_json, result.report, scenario=result.scenario, synthetic=True)
    finally:
        if dispose_engine:
            await engine.dispose()


async def _run_gate(args: argparse.Namespace) -> int:
    """Dispatch canonical persisted mode or explicit test fixture mode."""
    if args.synthetic:
        if args.scenario not in SYNTHETIC_SCENARIOS:
            raise ValueError("--synthetic requires --scenario pass or fail")
        return await _run_synthetic_gate(args)
    if args.scenario is not None:
        raise ValueError("--scenario is only available with explicit --synthetic")
    return await _run_persisted_gate(args)


def main(argv: list[str] | None = None) -> int:
    """Parse, execute, and return a stable gate status without leaking secrets."""
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run_gate(args))
    except Exception as exc:
        print(f"release-gate error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
