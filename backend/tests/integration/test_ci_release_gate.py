"""CI Release Gate CLI E2E on real PostgreSQL (subprocess).

Runs the actual ``scripts.ci.release_gate`` CLI as a subprocess against the
current-head test database and asserts the full process exit contract:

    pass -> ReleaseDecision.PASS -> exit 0 -> artifact decision PASS
    fail -> ReleaseDecision.FAIL  -> exit 2 -> artifact decision FAIL
    invalid scenario -> exit 1 (technical error, no artifact)
"""

# ruff: noqa: D101, D102, D105, D415

import json
import subprocess
import sys
from pathlib import Path

from app.registry.settings import settings

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_GATE_CMD = [sys.executable, "-m", "scripts.ci.release_gate"]


def _run_gate(tmp_path: Path, scenario: str) -> tuple[subprocess.CompletedProcess, Path]:
    report = tmp_path / "release-gate.json"
    result = subprocess.run(
        [
            *_GATE_CMD,
            "--scenario",
            scenario,
            "--report-json",
            str(report),
            "--dsn",
            settings.DATABASE_URL,
        ],
        cwd=_BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return result, report


def test_pass_scenario_exits_zero_with_pass_artifact(tmp_path: Path) -> None:
    result, report = _run_gate(tmp_path, "pass")

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["demo"] is True and payload["synthetic"] is True
    assert payload["release_decision"] == "PASS"
    assert payload["critical_blockers"] == []
    assert payload["comparison_counts"]["regressions"] == 0


def test_fail_scenario_exits_two_with_fail_artifact(tmp_path: Path) -> None:
    result, report = _run_gate(tmp_path, "fail")

    assert result.returncode == 2, result.stderr
    # A business gate FAIL is not a crash: no traceback, no error line on stderr.
    assert result.stderr.strip() == ""
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["release_decision"] == "FAIL"
    assert len(payload["critical_blockers"]) == 1
    assert payload["critical_blockers"][0]["classification"] == "REGRESSION"
    assert payload["critical_blockers"][0]["case_id"] == "demo-tool-contract"
    assert payload["comparison_counts"] == {
        "total": 3,
        "unchanged": 1,
        "improvements": 1,
        "regressions": 1,
        "not_comparable": 0,
    }


def test_technical_error_exits_one_without_artifact(tmp_path: Path) -> None:
    report = tmp_path / "release-gate.json"
    result = subprocess.run(
        [
            *_GATE_CMD,
            "--scenario",
            "bogus",
            "--report-json",
            str(report),
            "--dsn",
            settings.DATABASE_URL,
        ],
        cwd=_BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1
    assert "release-gate error" in result.stderr
    assert "Traceback" not in result.stderr
    assert not report.exists()
