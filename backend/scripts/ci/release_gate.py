"""AgentEvalOps CI Candidate Gate adapter.

Canonical mode reads two already-persisted EvaluationRuns from AgentEvalOps
PostgreSQL, then delegates comparison and release policy to the existing
``EvaluationComparisonService`` and ``RegressionReportService``. The JSON
written by this command is CI evidence only; it is not a second authority.

Exit contract: ``0`` = PASS, ``2`` = business gate FAIL, ``1`` = technical
error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import fields, is_dataclass
from functools import partial
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.evaluation.references import CaseVersionRef
from app.core.evaluation.immutable import json_compatible
from app.core.evaluation.report import RegressionReport, ReleaseDecision
from app.core.evaluation.production_evidence import (
    CIEvidenceSourceV1, EnvironmentType, SourceAuthenticity,
)
from app.core.evaluation.metric_policy import (
    MetricGateDecision, MetricGateStatus, MetricPolicy, MetricRule, MetricRuleKind, evaluate_metric_gate,
)
from app.core.evaluation.platform_metrics import MetricReportV1
from app.infrastructure.db.engine import engine as default_engine
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)

EXIT_PASS = 0
EXIT_GATE_FAIL = 2
EXIT_ERROR = 1
REPORT_SCHEMA_VERSION = 3
GATE_DATABASE_URL_ENV = "AGENTEVALOPS_GATE_DATABASE_URL"


def exit_code_for_decision(decision: ReleaseDecision) -> int:
    """Map the frozen ReleaseDecision to the stable process exit contract."""
    if decision is ReleaseDecision.PASS:
        return EXIT_PASS
    if decision is ReleaseDecision.FAIL:
        return EXIT_GATE_FAIL
    raise ValueError(f"unknown release decision: {decision!r}")


def serialize_report(
    report: RegressionReport,
    metrics: MetricReportV1 | None = None,
    gate: MetricGateDecision | None = None,
) -> dict[str, object]:
    """Serialize the existing RegressionReport without recomputing its truth."""
    payload: dict[str, object] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "authority": "RegressionReportService",
        "comparison_contract_version": report.comparison_contract_version,
        "comparison_digest": report.comparison_digest,
        "accepted_conditional_reasons": list(report.accepted_conditional_reasons),
        "computed_at": _json_value(report.computed_at),
        "project_id": str(report.project_id),
        "baseline_run_id": str(report.baseline_run_id),
        "candidate_run_id": str(report.candidate_run_id),
        "baseline_reference": _json_value(report.baseline_reference),
        "candidate_reference": _json_value(report.candidate_reference),
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
        "required_evidence_blockers": [
            {
                "case_id": item.case_id,
                "evaluator_id": item.evaluator_id,
                "reason_codes": list(item.reason_codes),
            }
            for item in report.incomplete_required_evidence
        ],
        "rows": [_json_value(item) for item in report.comparisons],
        "wp6_evidence_source": _json_value(ci_evidence_source_from_env()),
    }
    if metrics is not None:
        payload["metric_report"] = _json_value(metrics)
    if gate is not None:
        payload["metric_gate"] = _json_value(gate)
    return payload


def ci_evidence_source_from_env() -> CIEvidenceSourceV1:
    """Project controlled workflow identity; this wrapper never implies production."""
    workflow = os.environ.get("GITHUB_WORKFLOW")
    job = os.environ.get("GITHUB_JOB")
    run_id = os.environ.get("GITHUB_RUN_ID")
    revision = os.environ.get("GITHUB_SHA")
    controlled = (os.environ.get("GITHUB_ACTIONS") == "true" and bool(workflow and job and run_id and revision))
    return CIEvidenceSourceV1(
        workflow=workflow, job=job, run_id=run_id, source_revision=revision,
        environment_type=EnvironmentType.TEST if controlled else EnvironmentType.UNKNOWN,
        synthetic=True,
        # Runner environment variables identify context but are not an attestation.
        source_authenticity=SourceAuthenticity.DECLARED_SOURCE if controlled else SourceAuthenticity.UNKNOWN_SOURCE,
    )


def _json_value(value: object) -> object:
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="python"))
    if is_dataclass(value):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return json_compatible(value)


def write_report_artifact(
    path: str,
    report: RegressionReport,
    metrics: MetricReportV1 | None = None,
    gate: MetricGateDecision | None = None,
) -> Path:
    """Write ephemeral CI evidence (never durable evaluation authority)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(serialize_report(report, metrics, gate), indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def finalize(
    report_json_path: str | None,
    report: RegressionReport,
    metrics: MetricReportV1 | None = None,
    gate: MetricGateDecision | None = None,
) -> int:
    """Write evidence first, then map the existing decision to process status."""
    if report_json_path:
        write_report_artifact(report_json_path, report, metrics, gate)
    if gate is not None:
        if gate.status is MetricGateStatus.NOT_CONFIGURED:
            return EXIT_ERROR
        return EXIT_PASS if gate.status is MetricGateStatus.PASS else EXIT_GATE_FAIL
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
        "--report-json",
        default=None,
        metavar="PATH",
        help="Write ephemeral gate evidence to this path, including when the decision is FAIL.",
    )
    parser.add_argument(
        "--metric-policy-json", metavar="PATH", default=None,
        help="Optional explicit WP5 metric policy JSON; no production thresholds are built in.",
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


def _metric_policy(path: str | None) -> MetricPolicy | None:
    if path is None:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return MetricPolicy(
        policy_id=data["policy_id"], policy_version=data["policy_version"],
        rules=tuple(MetricRule(
            kind=MetricRuleKind(item["kind"]), metric_id=item["metric_id"],
            metric_version=item["metric_version"],
            metric_definition_digest=item["metric_definition_digest"], threshold=float(item["threshold"]),
        ) for item in data["rules"]),
        block_unavailable=data.get("block_unavailable", True),
        block_not_comparable=data.get("block_not_comparable", True),
    )


async def _run_persisted_gate(args: argparse.Namespace) -> int:
    """Compare persisted real runs and delegate policy to the existing service."""
    from app.services.evaluation.comparison import EvaluationComparisonService
    from app.services.evaluation.persistence import EvaluationPersistenceService
    from app.services.evaluation.report import RegressionReportService
    from app.services.evaluation.platform_metrics import PlatformMetricService

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
        metrics = await PlatformMetricService(persistence).build_report(comparison)
        gate = evaluate_metric_gate(report, metrics, _metric_policy(args.metric_policy_json))
        return finalize(args.report_json, report, metrics, gate)
    finally:
        if dispose_engine:
            await engine.dispose()


async def _run_gate(args: argparse.Namespace) -> int:
    """Run the canonical persisted gate."""
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
