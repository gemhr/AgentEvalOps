"""Unit tests for the CI Release Gate adapter (no DB required).

Covers the exit-code contract, the artifact serializer (truth source = the
frozen RegressionReport), the credential boundary, the "scenario never decides
the exit" invariant, and the workflow static gate.
"""

# ruff: noqa: D101, D102, D105, D415

import json
import re
from datetime import datetime, timezone
from pathlib import Path
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
    EXIT_ERROR,
    EXIT_GATE_FAIL,
    EXIT_PASS,
    _build_parser,
    exit_code_for_decision,
    finalize,
    main,
    serialize_report,
)

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
    code = finalize(str(report_path), _report(ReleaseDecision.PASS), scenario="pass")
    assert code == EXIT_PASS
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["release_decision"] == "PASS"


def test_finalize_fail_writes_artifact_before_exit_two(tmp_path: Path) -> None:
    report_path = tmp_path / "release-gate.json"
    code = finalize(str(report_path), _report(ReleaseDecision.FAIL), scenario="fail")
    assert code == EXIT_GATE_FAIL
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["release_decision"] == "FAIL"
    assert len(payload["critical_blockers"]) == 1
    assert payload["critical_blockers"][0]["classification"] == "REGRESSION"


def test_exit_comes_from_report_decision_not_scenario_label() -> None:
    # A "pass" scenario label with a FAIL report must still exit 2, and vice versa:
    # the scenario only labels the artifact; the exit code comes from the decision.
    assert finalize(None, _report(ReleaseDecision.FAIL), scenario="pass") == EXIT_GATE_FAIL
    assert finalize(None, _report(ReleaseDecision.PASS), scenario="fail") == EXIT_PASS


def test_serialize_report_matches_report_truth() -> None:
    report = _report(ReleaseDecision.FAIL)
    payload = serialize_report(report, scenario="fail")
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
    payload_text = json.dumps(serialize_report(_report(ReleaseDecision.FAIL), scenario="fail"))
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


def test_invalid_scenario_is_technical_error_not_gate_fail() -> None:
    assert main(["--scenario", "bogus"]) == EXIT_ERROR


def test_workflow_static_gate() -> None:
    text = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "workflow_dispatch" in text
    assert "workflow_call" in text
    assert "scenario" in text
    assert "POSTGRES_HOST_AUTH_METHOD: trust" in text
    assert "pandaprobe_ci" in text
    assert "alembic upgrade head" in text
    assert "scripts.ci.release_gate" in text
    assert "continue-on-error" not in text
    assert "|| true" not in text
    assert "if: always()" in text
    assert "upload-artifact" in text
    assert _AUTH_WITH_SECRET.search(text) is None
