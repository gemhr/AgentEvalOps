"""Minimal, non-executing pytest JUnit receipt parser for WP6."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime

from app.core.evaluation.production_evidence import (
    Availability,
    PhaseSummaryV1,
    ProcessStatus,
    TestEvidenceV1,
    WorktreeState,
)
from app.services.evaluation.production_evidence import CapturedTestReceipt, _captured_test_receipt

_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./:=@+-]{1,240}$")
_SECRET = re.compile(r"(?i)(token|password|secret|api[_-]?key|credential|cookie)=|://[^/\s:]+:[^/@\s]+@")
_SAFE_TEST_ID = re.compile(r"^[A-Za-z0-9_./:[\]-]{1,240}$")


def _safe_command(argv: tuple[str, ...]) -> tuple[str, ...]:
    if not argv or any(not _SAFE_ARG.fullmatch(item) or _SECRET.search(item) for item in argv):
        raise ValueError("COMMAND_FIELD_REJECTED")
    return argv


def capture_junit_receipt(
    junit_xml: bytes,
    *,
    command_argv: tuple[str, ...],
    cwd_alias: str,
    scope: str,
    process_exit_code: int | None,
    started_at: datetime | None,
    finished_at: datetime | None,
    source_revision: str | None,
    worktree_state: WorktreeState,
    working_tree_content_digest: str | None = None,
    selected_test_ids: tuple[str, ...] = (),
    dependency_versions: tuple[tuple[str, str], ...] = (),
    limitations: tuple[str, ...] = (),
) -> CapturedTestReceipt:
    """Project aggregate/phase facts; retains no raw JUnit messages or output."""
    if any(not _SAFE_TEST_ID.fullmatch(item) or _SECRET.search(item) for item in selected_test_ids):
        raise ValueError("TEST_ID_FIELD_REJECTED")
    try:
        root = ET.fromstring(junit_xml)
    except ET.ParseError:
        raise ValueError("JUNIT_XML_INVALID") from None
    cases = root.findall(".//testcase")
    failed = errors = skipped = 0
    xfail: int | None = None
    phase_counts: dict[str, dict[str, int]] = {
        key: {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "unknown": 0}
        for key in ("collection", "setup", "call", "teardown")
    }
    calls: dict[str, str] = {}
    child_counts = {"errors": 0, "failures": 0, "skipped": 0, "tests": 0}
    for case in cases:
        logical_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        child_counts["tests"] += 1
        outcomes = [(kind, element) for kind in ("failure", "error", "skipped")
                    for element in case.findall(kind)]
        if not outcomes:
            calls.setdefault(logical_id, "PASSED")
            continue
        for kind, element in outcomes:
            text = " ".join((element.get("type", ""), element.get("message", ""), element.text or "")).lower()
            phase = next((name for name in ("collection", "setup", "call", "teardown") if name in text), None)
            if kind == "failure":
                failed += 1
                child_counts["failures"] += 1
                if phase:
                    phase_counts[phase]["failed"] += 1
                if phase == "call":
                    calls[logical_id] = "FAILED"
            elif kind == "error":
                errors += 1
                child_counts["errors"] += 1
                if phase:
                    phase_counts[phase]["errors"] += 1
                else:
                    phase_counts["call"]["unknown"] += 1
                if phase == "call":
                    calls[logical_id] = "ERROR"
            else:
                skipped += 1
                child_counts["skipped"] += 1
                if phase:
                    phase_counts[phase]["skipped"] += 1
                skip_type = element.get("type", "").lower()
                if "pytest.xfail" in skip_type or "xfail" in skip_type:
                    xfail = (xfail or 0) + 1
                    calls[logical_id] = "XFAIL"
                elif phase == "call":
                    calls[logical_id] = "SKIPPED"
                elif phase is None:
                    calls.setdefault(logical_id, "UNKNOWN")

    for logical_id in {f"{case.get('classname', '')}::{case.get('name', '')}" for case in cases}:
        outcome = calls.setdefault(logical_id, "UNKNOWN")
        if outcome == "PASSED":
            phase_counts["call"]["passed"] += 1
        elif outcome == "UNKNOWN":
            phase_counts["call"]["unknown"] += 1

    suite_conflict = False
    for suite in root.iter("testsuite"):
        for key in child_counts:
            raw = suite.get(key)
            if raw is None:
                continue
            try:
                declared = int(raw)
            except ValueError:
                suite_conflict = True
                continue
            if declared > child_counts[key]:
                suite_conflict = True
    passed = sum(phase == "PASSED" for phase in calls.values())
    if process_exit_code is None:
        status = ProcessStatus.UNKNOWN
    elif process_exit_code != 0 or failed or errors or not cases or suite_conflict:
        status = ProcessStatus.FAILED
    else:
        status = ProcessStatus.SUCCEEDED
    if not cases:
        phase_counts["call"]["unknown"] += 1
    phase_summaries = tuple(PhaseSummaryV1(phase=name, **counts) for name, counts in phase_counts.items())
    receipt = TestEvidenceV1(
        command_argv=_safe_command(command_argv), cwd_alias=cwd_alias, scope=scope,
        selected_test_ids=selected_test_ids,
        selection_digest=hashlib.sha256("\n".join(selected_test_ids).encode("utf-8")).hexdigest()
        if selected_test_ids else None,
        body_assertions=f"{passed} passed", passed=passed, failed=failed, errors=errors, skipped=skipped,
        xfail=xfail, xpass=None, phases=phase_summaries, process_exit_code=process_exit_code,
        process_status=status, started_at=started_at, finished_at=finished_at,
        source_revision=source_revision, working_tree_content_digest=working_tree_content_digest,
        worktree_state=worktree_state,
        dependency_versions=dependency_versions,
        dependency_health=Availability.UNMEASURED,
        raw_result_digest=hashlib.sha256(junit_xml).hexdigest(), limitations=limitations,
    )
    return _captured_test_receipt(receipt)
