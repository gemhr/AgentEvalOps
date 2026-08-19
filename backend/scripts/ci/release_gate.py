"""AgentEvalOps CI Release Gate adapter (DEMO / SYNTHETIC).

Stage4-Phase5-WP2.  This module is a THIN ADAPTER ONLY: it consumes the frozen
owners through the WP1 closed-loop orchestration and maps the decision to a
stable process exit contract:

    0  = Release Gate PASS
    2  = Release Gate FAIL (business gate block)
    1  = CLI / execution / configuration / contract error

It never re-computes classification, critical blockers, criticality or the
release decision (those owners stay with EvaluationComparisonService and
RegressionReportService), and the ``--scenario`` argument only controls the
synthetic candidate behavior — it never decides the exit code directly.

The gate data is DEMO / SYNTHETIC: this proves the exit contract and the CI job
status mapping, not protection of a real production release.

Usage (from ``backend/``):

    uv run python -m scripts.ci.release_gate --scenario pass
    uv run python -m scripts.ci.release_gate --scenario fail --report-json artifacts/release-gate.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from functools import partial
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.evaluation.report import RegressionReport, ReleaseDecision
from app.infrastructure.db.engine import engine as default_engine
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)
from scripts.demo.closed_loop_demo import (
    DEMO_DATABASE_URL_ENV,
    SCENARIOS,
    DemoResult,
    _resolve_dsn,
    run_closed_loop_demo,
)

EXIT_PASS = 0
EXIT_GATE_FAIL = 2
EXIT_ERROR = 1

REPORT_SCHEMA_VERSION = 1


def exit_code_for_decision(decision: ReleaseDecision) -> int:
    """Map the frozen ReleaseDecision to the exit contract; unknown never defaults to PASS."""
    if decision is ReleaseDecision.PASS:
        return EXIT_PASS
    if decision is ReleaseDecision.FAIL:
        return EXIT_GATE_FAIL
    raise ValueError(f"unknown release decision: {decision!r}")


def serialize_report(report: RegressionReport, *, scenario: str) -> dict[str, object]:
    """Pure serialization of the frozen RegressionReport into the gate artifact.

    Every field is copied from the report — no re-computation of counts,
    blockers, criticality or the decision.
    """
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "demo": True,
        "synthetic": True,
        "scenario": scenario,
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


def write_report_artifact(path: str, report: RegressionReport, *, scenario: str) -> Path:
    """Write the gate report artifact (FILE OUTPUT, not durable business truth)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(serialize_report(report, scenario=scenario), indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def finalize(report_json_path: str | None, report: RegressionReport, *, scenario: str) -> int:
    """Write the artifact first (even on FAIL), then map the decision to the exit code."""
    if report_json_path:
        write_report_artifact(report_json_path, report, scenario=scenario)
    return exit_code_for_decision(report.release_decision)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_gate",
        description=(
            "AgentEvalOps CI Release Gate (DEMO / SYNTHETIC gate; not a production "
            "deployment gate). Exit contract: 0 = Release Gate PASS, 2 = Release Gate "
            "FAIL (business block), 1 = execution / configuration / contract error."
        ),
    )
    parser.add_argument(
        "--scenario",
        default="pass",
        metavar="{pass,fail}",
        help="Synthetic gate scenario: 'pass' -> gate PASS (exit 0); 'fail' -> gate FAIL (exit 2).",
    )
    parser.add_argument(
        "--report-json",
        default=None,
        metavar="PATH",
        help="Write the gate report artifact (JSON) to this path before returning the exit code (written even on FAIL).",
    )
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
    return parser


async def _run_gate(args: argparse.Namespace) -> int:
    """Run the full closed loop and return the exit code derived from the real decision."""
    dsn = _resolve_dsn(args.dsn)
    if dsn is not None:
        engine = create_async_engine(dsn)
        dispose_engine = True
    else:
        engine = default_engine
        dispose_engine = False
    try:
        session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        uow_factory = partial(PostgresEvaluationPersistenceUnitOfWork, session_factory)
        async with session_factory() as session:
            result: DemoResult = await run_closed_loop_demo(session, uow_factory=uow_factory, scenario=args.scenario)
        return finalize(args.report_json, result.report, scenario=result.scenario)
    finally:
        if dispose_engine:
            await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """CLI entry: parse, validate, run the gate, map the real decision to the exit code."""
    args = _build_parser().parse_args(argv)
    if args.scenario not in SCENARIOS:
        print(
            f"release-gate error: invalid scenario {args.scenario!r} (expected one of {', '.join(SCENARIOS)})",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        return asyncio.run(_run_gate(args))
    except Exception as exc:
        print(f"release-gate error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
