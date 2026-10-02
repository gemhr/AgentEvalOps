"""Unit tests for the CI Release Gate adapter (no DB required).

Covers the exit-code contract, the artifact serializer (truth source = the
frozen RegressionReport), the credential boundary, and the workflow static gate.
"""

# ruff: noqa: D101, D102, D105, D415

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.core.evaluation.comparison import (
    AlignedResultComparison,
    ComparisonReason,
    RegressionClassification,
    RunComparisonProvenance,
)
from app.core.evaluation.report import RegressionReport, ReleaseDecision
from app.core.evaluation.references import CaseVersionRef
from scripts.ci.release_gate import (
    EXIT_GATE_FAIL,
    EXIT_PASS,
    _build_parser,
    ci_evidence_source_from_env,
    exit_code_for_decision,
    finalize,
    main,
    serialize_report,
)
from app.core.evaluation.metric_policy import MetricGateStatus

NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)
CASE_ROUTING = "demo-routing-critical"
CASE_RAG = "demo-rag-grounding"
CASE_TOOL_CONTRACT = "demo-tool-contract"
_CRITICAL_REF = CaseVersionRef(CASE_TOOL_CONTRACT, "v1")

# Matches an auth section with a password, e.g. "://user:password@" in a URL.
_AUTH_WITH_SECRET = re.compile(r"://[^/\s]*:[^/@\s]*@")

_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[3] / ".github" / "workflows" / "evaluation-release-gate.yml"
)
_RELEASE_WORKFLOW_PATH = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "release.yml"


def _report(decision: ReleaseDecision) -> RegressionReport:
    """Minimal in-memory RegressionReport matching the demo's 3-case universe."""
    if decision is ReleaseDecision.FAIL:
        tool_item = AlignedResultComparison(
            CASE_TOOL_CONTRACT,
            "v1",
            "demo-quality",
            "v1",
            classification=RegressionClassification.REGRESSION,
            reason=ComparisonReason.VERDICT_REGRESSED,
        )
    else:
        tool_item = AlignedResultComparison(
            CASE_TOOL_CONTRACT,
            "v1",
            "demo-quality",
            "v1",
            classification=RegressionClassification.UNCHANGED,
            reason=ComparisonReason.VERDICT_UNCHANGED,
        )
    items = (
        AlignedResultComparison(
            CASE_ROUTING,
            "v1",
            "demo-quality",
            "v1",
            classification=RegressionClassification.UNCHANGED,
            reason=ComparisonReason.VERDICT_UNCHANGED,
        ),
        AlignedResultComparison(
            CASE_RAG,
            "v1",
            "demo-quality",
            "v1",
            classification=RegressionClassification.IMPROVEMENT,
            reason=ComparisonReason.VERDICT_IMPROVED,
        ),
        tool_item,
    )
    regressions = tuple(
        item for item in items if item.classification is RegressionClassification.REGRESSION
    )
    provenance = RunComparisonProvenance(
        dataset_id="demo-agent-regression",
        dataset_version="v1",
        suite_id="demo-agent-regression-suite",
        suite_version="v1",
        execution_target_id="demo-fixture-target",
        execution_target_kind="FIXTURE",
    )
    return RegressionReport(
        project_id=UUID("00000000-0000-4000-a000-000000000001"),
        baseline_run_id=UUID("00000000-0000-4000-a000-000000000002"),
        candidate_run_id=UUID("00000000-0000-4000-a000-000000000003"),
        baseline_provenance=provenance,
        candidate_provenance=provenance,
        critical_case_refs=(_CRITICAL_REF,),
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


def test_exit_code_for_decision_pass_is_zero() -> None:
    assert exit_code_for_decision(ReleaseDecision.PASS) == EXIT_PASS


def test_exit_code_for_decision_fail_is_two() -> None:
    assert exit_code_for_decision(ReleaseDecision.FAIL) == EXIT_GATE_FAIL


def test_exit_code_for_decision_unknown_raises_never_defaults_pass() -> None:
    with pytest.raises(ValueError, match="unknown release decision"):
        exit_code_for_decision(object())  # type: ignore[arg-type]


def test_finalize_pass_writes_artifact_then_exits_zero(tmp_path: Path) -> None:
    report_path = tmp_path / "release-gate.json"
    code = finalize(str(report_path), _report(ReleaseDecision.PASS))
    assert code == EXIT_PASS
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["release_decision"] == "PASS"


def test_finalize_fail_writes_artifact_before_exit_two(tmp_path: Path) -> None:
    report_path = tmp_path / "release-gate.json"
    code = finalize(str(report_path), _report(ReleaseDecision.FAIL))
    assert code == EXIT_GATE_FAIL
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["release_decision"] == "FAIL"
    assert len(payload["critical_blockers"]) == 1
    assert payload["critical_blockers"][0]["classification"] == "REGRESSION"


def test_exit_comes_from_report_decision() -> None:
    assert finalize(None, _report(ReleaseDecision.FAIL)) == EXIT_GATE_FAIL
    assert finalize(None, _report(ReleaseDecision.PASS)) == EXIT_PASS


def test_missing_metric_policy_remains_exit_one() -> None:
    gate = SimpleNamespace(status=MetricGateStatus.NOT_CONFIGURED)
    assert finalize(None, _report(ReleaseDecision.PASS), None, gate) == 1


def test_ci_evidence_source_never_promotes_synthetic_test_to_production(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKFLOW", "test workflow")
    monkeypatch.setenv("GITHUB_JOB", "test-job")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    source = ci_evidence_source_from_env()
    assert source.environment_type.value == "TEST"
    assert source.synthetic is True
    assert source.source_authenticity.value == "DECLARED_SOURCE"
    monkeypatch.delenv("GITHUB_SHA")
    unbound = ci_evidence_source_from_env()
    assert unbound.environment_type.value == "UNKNOWN"
    assert unbound.source_authenticity.value == "UNKNOWN_SOURCE"


def test_serialize_report_matches_report_truth() -> None:
    report = _report(ReleaseDecision.FAIL)
    payload = serialize_report(report)
    assert payload["release_decision"] == report.release_decision.value
    counts = payload["comparison_counts"]
    assert counts == {
        "total": report.total_count,
        "unchanged": report.unchanged_count,
        "improvements": report.improvement_count,
        "regressions": report.regression_count,
        "not_comparable": report.not_comparable_count,
    }
    assert payload["critical_case_refs"] == [{"case_id": "demo-tool-contract", "version": "v1"}]
    assert len(payload["critical_blockers"]) == len(report.critical_regressions)


def test_serialize_report_contains_no_credentials() -> None:
    payload_text = json.dumps(serialize_report(_report(ReleaseDecision.FAIL)))
    assert "postgresql" not in payload_text
    assert "@" not in payload_text
    assert "password" not in payload_text


def test_cli_help_documents_exit_contract_without_credentials() -> None:
    help_text = _build_parser().format_help()
    assert "0 = Release Gate PASS" in help_text
    assert "2 = Release Gate FAIL" in help_text
    assert "1 = execution / configuration / contract error" in help_text
    assert _AUTH_WITH_SECRET.search(help_text) is None
    assert "postgresql://" not in help_text


def test_workflow_static_gate() -> None:
    text = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "workflow_dispatch" in text
    assert "workflow_call" in text
    assert "POSTGRES_HOST_AUTH_METHOD: trust" in text
    assert "- 5433:5432" in text
    assert "- 6380:6379" in text
    assert "POSTGRES_PORT: \"5433\"" in text
    assert "POSTGRES_DB: pandaprobe_test_db" in text
    assert "AGENTEVALOPS_GATE_DATABASE_URL: postgresql+asyncpg://postgres@localhost:5433/pandaprobe_test_db" in text
    assert "CREATE DATABASE pandaprobe_test_db" in text
    psql_create_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("psql ")]
    assert len(psql_create_lines) == 2
    assert all("-h localhost -p 5433" in line and "-d postgres" in line for line in psql_create_lines)
    assert "5432:5432" not in text
    assert "6379:6379" not in text
    fixture_text = Path(__file__).resolve().parents[2].joinpath("tests", "conftest.py").read_text(encoding="utf-8")
    assert 'os.environ["POSTGRES_PORT"] = "5433"' in fixture_text
    assert 'os.environ["POSTGRES_DB"] = "pandaprobe_test_db"' in fixture_text
    assert 'os.environ["REDIS_PORT"] = "6380"' in fixture_text
    service_pg_port = re.search(r"-\s*(\d+):5432", text).group(1)
    service_redis_port = re.search(r"-\s*(\d+):6379", text).group(1)
    gate_pg = re.search(r"AGENTEVALOPS_GATE_DATABASE_URL: postgresql\+asyncpg://[^@]+@localhost:(\d+)/([^\s]+)", text)
    workflow_pg_port = re.search(r'POSTGRES_PORT: "(\d+)"', text).group(1)
    workflow_pg_db = re.search(r"^      POSTGRES_DB: ([A-Za-z0-9_]+)$", text, re.MULTILINE).group(1)
    test_pg_port = re.search(r'os\.environ\["POSTGRES_PORT"\] = "(\d+)"', fixture_text).group(1)
    test_pg_db = re.search(r'os\.environ\["POSTGRES_DB"\] = "([A-Za-z0-9_]+)"', fixture_text).group(1)
    test_redis_port = re.search(r'os\.environ\["REDIS_PORT"\] = "(\d+)"', fixture_text).group(1)
    assert gate_pg is not None
    assert service_pg_port == workflow_pg_port == test_pg_port == gate_pg.group(1)
    assert workflow_pg_db == test_pg_db == gate_pg.group(2) == "pandaprobe_test_db"
    assert service_redis_port == test_redis_port == "6380"
    assert re.search(r'^      REDIS_DB: "0"$', text, re.MULTILINE)
    assert "redis://localhost:6380/1" in text
    assert "redis://localhost:6380/0" not in text
    assert text.count("LOCAL_AGENT_DATABASE_URL: postgresql+asyncpg://postgres@localhost:5433/localagent_ci") == 2
    assert "LOCALAGENT_E2E_DATABASE_URL: postgresql+asyncpg://postgres@localhost:5433/localagent_ci" in text
    assert text.count("LOCAL_AGENT_REDIS_URL: redis://localhost:6380/1") == 2
    assert "LOCALAGENT_E2E_REDIS_URL: redis://localhost:6380/1" in text
    assert "repository: gemhr/Local_Agent" in text
    assert "uv sync --frozen" in text
    assert "LOCAL_AGENT_DATABASE_URL" in text
    assert "LOCALAGENT_E2E_JWT_PUBLIC_KEY" in text
    assert "LOCALAGENT_E2E_SERVICE_TOKEN" in text
    assert "AGENTEVALOPS_RUN_LOCALAGENT_PRODUCTION_E2E" in text
    assert (
        "tests/integration/test_localagent_production_e2e.py::"
        "test_real_known_bad_candidate_fails_canonical_gate"
    ) in text
    assert "AGENTEVALOPS_CANDIDATE_GATE_RUN_IDS_PATH" in text
    assert "AGENTEVALOPS_CANDIDATE_GATE_REPORT_DIR" in text
    assert "scripts.ci.release_gate" in text
    assert "--metric-policy-json" in Path(__file__).resolve().parents[3].joinpath(
        "backend", "tests", "integration", "test_localagent_production_e2e.py"
    ).read_text(encoding="utf-8")
    assert "TEST FIXTURE ONLY" in Path(__file__).resolve().parents[3].joinpath(
        "backend", "tests", "integration", "test_localagent_production_e2e.py"
    ).read_text(encoding="utf-8")
    assert "alembic upgrade head" in text
    assert "--synthetic" not in text
    assert "scenario" not in text
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "if: always()" in text
    assert "upload-artifact" in text
    assert "if-no-files-found: error" in text
    assert _AUTH_WITH_SECRET.search(text) is None

    release_text = _RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "candidate-gate:" in release_text
    assert "uses: ./.github/workflows/evaluation-release-gate.yml" in release_text
    assert "secrets: inherit" in release_text
    assert re.search(r"build:\s+needs:\s+candidate-gate", release_text)
