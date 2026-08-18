"""Pure-logic unit tests for the closed-loop demo driver (no DB required).

Covers the deterministic scenario tables, the artifact-digest-driven evaluator,
and the console/JSON artifact renderers.  DB orchestration is covered by the
integration suite (``test_closed_loop_demo.py``).
"""

# ruff: noqa: D101, D102, D105, D415

import asyncio
from datetime import datetime, timezone
import json
import re
from uuid import UUID

import pytest

from app.core.evaluation import ArtifactRef, DatasetVersion, EvaluationVerdict
from app.core.evaluation.comparison import (
    AlignedResultComparison,
    ComparisonReason,
    EvaluationRunComparison,
    RegressionClassification,
    RunComparisonProvenance,
)
from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.report import RegressionReport, ReleaseDecision
from scripts.demo.closed_loop_demo import (
    CASE_RAG,
    CASE_REFS,
    CASE_ROUTING,
    CASE_TOOL_CONTRACT,
    DEMO_DATABASE_URL_ENV,
    DEMO_DATASET_ID,
    EVALUATOR_SPEC,
    DemoQualityEvaluator,
    DemoResult,
    _build_parser,
    _fixture_map,
    _resolve_dsn,
    _verdict_tables,
    render_console,
    report_payload,
)

NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)


def _input(case_id: str, digest: str) -> EvaluationInput:
    return EvaluationInput(
        case_ref=CASE_REFS[case_id],
        expected_output=None,
        assertion_specs=(),
        actual_artifact=ArtifactRef("demo-artifact", digest=digest),
    )


def _demo_result(decision: ReleaseDecision) -> DemoResult:
    """Minimal in-memory DemoResult with a valid Report/Comparison (no DB)."""
    if decision is ReleaseDecision.FAIL:
        tool_item = AlignedResultComparison(
            CASE_TOOL_CONTRACT, "v1", "demo-quality", "v1",
            classification=RegressionClassification.REGRESSION,
            reason=ComparisonReason.VERDICT_REGRESSED,
            baseline_score=1.0, candidate_score=0.0, score_delta=-1.0,
        )
    else:
        tool_item = AlignedResultComparison(
            CASE_TOOL_CONTRACT, "v1", "demo-quality", "v1",
            classification=RegressionClassification.UNCHANGED,
            reason=ComparisonReason.VERDICT_UNCHANGED,
            baseline_score=1.0, candidate_score=1.0, score_delta=0.0,
        )
    items = (
        AlignedResultComparison(
            CASE_ROUTING, "v1", "demo-quality", "v1",
            classification=RegressionClassification.UNCHANGED,
            reason=ComparisonReason.VERDICT_UNCHANGED,
        ),
        AlignedResultComparison(
            CASE_RAG, "v1", "demo-quality", "v1",
            classification=RegressionClassification.IMPROVEMENT,
            reason=ComparisonReason.VERDICT_IMPROVED,
            baseline_score=0.0, candidate_score=1.0, score_delta=1.0,
        ),
        tool_item,
    )
    provenance = RunComparisonProvenance(
        dataset_id=DEMO_DATASET_ID, dataset_version="v1", suite_id="suite", suite_version="v1",
        execution_target_id="demo-fixture-target", execution_target_kind="FIXTURE",
    )
    comparison = EvaluationRunComparison(
        project_id=UUID("00000000-0000-4000-a000-000000000001"),
        baseline_run_id=UUID("00000000-0000-4000-a000-000000000002"),
        candidate_run_id=UUID("00000000-0000-4000-a000-000000000003"),
        baseline_provenance=provenance,
        candidate_provenance=provenance,
        comparisons=items,
    )
    regressions = tuple(item for item in items if item.classification is RegressionClassification.REGRESSION)
    report = RegressionReport(
        project_id=comparison.project_id,
        baseline_run_id=comparison.baseline_run_id,
        candidate_run_id=comparison.candidate_run_id,
        baseline_provenance=provenance,
        candidate_provenance=provenance,
        critical_case_refs=(CASE_REFS[CASE_TOOL_CONTRACT],),
        comparisons=items,
        total_count=3,
        regression_count=len(regressions),
        improvement_count=1,
        unchanged_count=3 - len(regressions) - 1,
        not_comparable_count=0,
        regressions=regressions,
        critical_regressions=(tool_item,) if decision is ReleaseDecision.FAIL else (),
        critical_not_comparable=(),
        release_decision=decision,
    )
    dataset = DatasetVersion(
        DEMO_DATASET_ID, "v1", "Demo Agent Regression (SYNTHETIC)", NOW,
        case_version_refs=tuple(CASE_REFS[case_id] for case_id in CASE_REFS),
    )
    return DemoResult(
        scenario="fail" if decision is ReleaseDecision.FAIL else "pass",
        org_id=UUID("00000000-0000-4000-a000-000000000004"),
        project_id=comparison.project_id,
        trace_id=UUID("00000000-0000-4000-a000-000000000005"),
        dataset=dataset,
        baseline_run_id=comparison.baseline_run_id,
        candidate_run_id=comparison.candidate_run_id,
        comparison=comparison,
        report=report,
    )


def test_fail_scenario_verdict_tables_drive_critical_regression() -> None:
    tables = _verdict_tables("fail")
    assert tables["baseline"] == {
        CASE_ROUTING: EvaluationVerdict.PASS,
        CASE_RAG: EvaluationVerdict.FAIL,
        CASE_TOOL_CONTRACT: EvaluationVerdict.PASS,
    }
    assert tables["candidate"] == {
        CASE_ROUTING: EvaluationVerdict.PASS,
        CASE_RAG: EvaluationVerdict.PASS,
        CASE_TOOL_CONTRACT: EvaluationVerdict.FAIL,
    }


def test_pass_scenario_verdict_tables_keep_critical_case_pass() -> None:
    tables = _verdict_tables("pass")
    assert tables["candidate"][CASE_TOOL_CONTRACT] is EvaluationVerdict.PASS
    assert tables["candidate"][CASE_RAG] is EvaluationVerdict.PASS


def test_evaluator_judges_artifact_digest_not_run_identity() -> None:
    evaluator = DemoQualityEvaluator(EVALUATOR_SPEC, _verdict_tables("fail"))

    async def _run():
        baseline = await evaluator.evaluate(
            _input(CASE_TOOL_CONTRACT, "baseline:demo-tool-contract"), EvaluatorContext(EVALUATOR_SPEC)
        )
        candidate = await evaluator.evaluate(
            _input(CASE_TOOL_CONTRACT, "candidate:demo-tool-contract"), EvaluatorContext(EVALUATOR_SPEC)
        )
        improved = await evaluator.evaluate(
            _input(CASE_RAG, "candidate:demo-rag-grounding"), EvaluatorContext(EVALUATOR_SPEC)
        )
        return baseline, candidate, improved

    baseline, candidate, improved = asyncio.run(_run())
    assert baseline.verdict is EvaluationVerdict.PASS
    assert candidate.verdict is EvaluationVerdict.FAIL
    assert candidate.score == 0.0
    assert improved.verdict is EvaluationVerdict.PASS


def test_evaluator_fails_closed_on_unknown_digest() -> None:
    evaluator = DemoQualityEvaluator(EVALUATOR_SPEC, _verdict_tables("fail"))

    async def _run():
        return await evaluator.evaluate(
            _input(CASE_ROUTING, "production:demo-routing-critical"), EvaluatorContext(EVALUATOR_SPEC)
        )

    with pytest.raises(ValueError, match="unknown demo artifact digest"):
        asyncio.run(_run())


def test_fixture_map_encodes_run_marker_and_case_in_digest() -> None:
    fixtures = _fixture_map("baseline", (CASE_REFS[CASE_ROUTING],))
    fixture = fixtures[CASE_REFS[CASE_ROUTING]]
    assert fixture.kind.value == "SUCCESS"
    assert fixture.output_artifact_ref.digest == "baseline:demo-routing-critical"


def test_report_payload_is_demo_labeled_and_reflects_decision() -> None:
    payload = report_payload(_demo_result(ReleaseDecision.FAIL))
    assert payload["demo"] is True
    assert payload["synthetic"] is True
    assert payload["release_decision"] == "FAIL"
    assert payload["comparison"] == {"unchanged": 1, "improvements": 1, "regressions": 1, "not_comparable": 0}
    assert payload["critical_blockers"][0]["case_id"] == CASE_TOOL_CONTRACT

    pass_payload = report_payload(_demo_result(ReleaseDecision.PASS))
    assert pass_payload["release_decision"] == "PASS"
    assert pass_payload["critical_blockers"] == []


def test_console_render_exposes_decision_and_blockers() -> None:
    fail_text = render_console(_demo_result(ReleaseDecision.FAIL))
    assert "AgentEvalOps Closed-Loop Demo (SYNTHETIC)" in fail_text
    assert "Release Decision: FAIL" in fail_text
    assert f"{CASE_TOOL_CONTRACT}@v1 REGRESSION (critical)" in fail_text

    pass_text = render_console(_demo_result(ReleaseDecision.PASS))
    assert "Release Decision: PASS" in pass_text
    assert "critical blockers:" in pass_text.lower()


# ---------------------------------------------------------------------------
# P1-1 remediation regression tests: no plaintext DB credentials anywhere
# ---------------------------------------------------------------------------

# Matches an auth section with a password, e.g. "://user:password@" in a URL.
_AUTH_WITH_PASSWORD = re.compile(r"://[^/\s]*:[^/@\s]*@")


def test_cli_help_contains_no_plaintext_credentials() -> None:
    help_text = _build_parser().format_help()
    assert _AUTH_WITH_PASSWORD.search(help_text) is None
    assert "postgresql://" not in help_text
    assert DEMO_DATABASE_URL_ENV in help_text


def test_cli_parser_default_dsn_is_none() -> None:
    args = _build_parser().parse_args([])
    assert args.dsn is None


def test_dsn_resolution_explicit_flag_wins_over_env(monkeypatch) -> None:
    monkeypatch.setenv(DEMO_DATABASE_URL_ENV, "postgresql+asyncpg://env-user@localhost:5432/envdb")
    explicit = "postgresql+asyncpg://cli-user@localhost:5432/clidb"
    assert _resolve_dsn(explicit) == explicit


def test_dsn_resolution_uses_env_variable(monkeypatch) -> None:
    env_dsn = "postgresql+asyncpg://env-user@localhost:5432/envdb"
    monkeypatch.setenv(DEMO_DATABASE_URL_ENV, env_dsn)
    assert _resolve_dsn(None) == env_dsn


def test_dsn_resolution_falls_back_to_project_config(monkeypatch) -> None:
    monkeypatch.delenv(DEMO_DATABASE_URL_ENV, raising=False)
    assert _resolve_dsn(None) is None


def test_json_artifact_contains_no_dsn_or_credentials() -> None:
    payload_text = json.dumps(report_payload(_demo_result(ReleaseDecision.FAIL)))
    assert "postgresql" not in payload_text
    assert "@" not in payload_text
    assert "password" not in payload_text
