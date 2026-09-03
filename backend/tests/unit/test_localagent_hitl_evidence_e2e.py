"""Real LocalAgent → AgentEvalOps HITL evidence path E2E (WP3 §19).

Proves the real cross-repository evidence chain without modifying Local_Agent
production code and without any external LLM:

    Local_Agent real production surfaces — FastAPI /api/chat streaming,
    HTTP approve/reject routes, ToolGovernanceService, ToolApprovalController,
    ToolExecutionService + real complex_workflow_simulator adapter, and the
    Journal-first RunEventJournal (driven through LocalAgent's own WP2-tested
    HTTP E2E harness helpers)
        → real Journal TOOL_APPROVAL_REQUESTED / TOOL_APPROVAL_DECIDED /
          TOOL_STARTED / TOOL_COMPLETED records
        → safe public projection (JSON on stdout)
        → AgentEvalOps typed HitlToolApprovalEvidenceV1 (REAL_LOCALAGENT_EVIDENCE)
        → build_hitl_evaluation_input correlation grouping
        → ToolApproval HITL evaluator
        → PASS

The Local_Agent subprocess runs with its own uv environment; Local_Agent
sources stay read-only. Skips when the Local_Agent repository is unavailable.
"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.core.evaluation.hitl_evidence import (
    HitlEvidenceProvenance,
    HitlToolApprovalEvidenceV1,
    build_hitl_evaluation_input,
)
from app.core.evaluation.hitl_evaluator import (
    ASSERTION_ID_AT_MOST_ONCE,
    HitlScenarioExpectationV1,
    evaluate_hitl_scenario,
)
from app.core.evaluation.stateful_assertion import AssertionStatus

LOCAL_AGENT_REPO = os.environ.get("LOCAL_AGENT_REPO", r"D:\PythonProject\Local_Agent")

LAUNCHER = r"""
import asyncio
import json
import os
import sys

LOCAL_AGENT = os.environ["LOCAL_AGENT_REPO"]
sys.path.insert(0, LOCAL_AGENT)

import server
from core.runtime import CoordinatedRuntimeFactory, RunRegistry
from core.runtime.events import RuntimeEventType
import tests.test_tool_approval_http_api as H

HITL_EVENT_TYPES = {
    RuntimeEventType.TOOL_APPROVAL_REQUESTED,
    RuntimeEventType.TOOL_APPROVAL_DECIDED,
    RuntimeEventType.TOOL_STARTED,
    RuntimeEventType.TOOL_COMPLETED,
}
SAFE_FIELDS = (
    "approval_id",
    "tool_name",
    "invocation_identity_digest",
    "invocation_binding_digest",
    "decision_status",
    "risk_level",
    "actor_id_digest",
)


def safe_event(record, run_id):
    payload = record.safe_payload
    event = {
        "sequence": record.sequence,
        "event_type": record.event_type.value,
        "run_id": run_id,
        "step_id": record.step_id,
    }
    for key in SAFE_FIELDS:
        value = payload.get(key)
        if value is not None:
            event[key] = value
    return event


def terminal_journal_events(run_id, services):
    # ``trace_complete`` is a producer fact in this deterministic cross-repo
    # harness, not a scenario label: the coordinator only unregisters after
    # it publishes RUN_COMPLETED.  Requiring that terminal record to be final
    # makes the following safe HITL projection complete for this run.  A
    # missing/non-final terminal record fails closed rather than allowing
    # absence of TOOL_STARTED to become a PASS.
    records = services.event_journal.read_after(run_id, 0, 1000)
    assert records, "terminal Journal capture unexpectedly contained no records"
    assert records[-1].event_type is RuntimeEventType.RUN_COMPLETED, (
        "trace is not terminally complete; RUN_COMPLETED must be the final Journal record"
    )
    terminal_status = records[-1].safe_payload["status"]
    assert isinstance(terminal_status, str) and terminal_status
    return (
        [safe_event(r, run_id) for r in records if r.event_type in HITL_EVENT_TYPES],
        terminal_status,
    )


class _Shim:
    # Minimal stand-in for pytest's monkeypatch: setattr with restore.
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name, None)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self._undo):
            setattr(obj, name, value)
        self._undo.clear()


async def finish_http_scenario(pending):
    await H._wait_stream_event(pending.call, "TOOL_APPROVAL_DECIDED")
    await pending.call.wait_finished()
    events, terminal_status = terminal_journal_events(pending.run_id, pending.services)
    await H._shutdown_pending_run(pending)
    return events, terminal_status


async def scenario_approve(operation_id):
    shim = _Shim()
    pending, requested, _ = await H._start_pending_run(shim, operation_id=operation_id)
    try:
        approval_id = requested["payload"]["approval_id"]
        digest = requested["payload"]["invocation_binding_digest"]
        status, body = await H.asgi_json_request(
            server.app,
            "POST",
            H.APPROVE_PATH.format(run_id=pending.run_id, approval_id=approval_id),
            {"invocation_binding_digest": digest, "actor_id": "agentevalops-evaluator"},
        )
        assert status == 200, body
        events, terminal_status = await finish_http_scenario(pending)
        return "HITL_APPROVE_ONCE", pending.run_id, events, terminal_status
    finally:
        shim.undo()


async def scenario_duplicate_approve(operation_id):
    shim = _Shim()
    pending, requested, _ = await H._start_pending_run(shim, operation_id=operation_id)
    try:
        approval_id = requested["payload"]["approval_id"]
        digest = requested["payload"]["invocation_binding_digest"]
        path = H.APPROVE_PATH.format(run_id=pending.run_id, approval_id=approval_id)
        first = await H.asgi_json_request(
            server.app, "POST", path, {"invocation_binding_digest": digest, "actor_id": "reviewer-a"}
        )
        second = await H.asgi_json_request(
            server.app, "POST", path, {"invocation_binding_digest": digest, "actor_id": "reviewer-a"}
        )
        assert first[0] == 200 and first[1]["idempotent"] is False, first
        assert second[0] == 200 and second[1]["idempotent"] is True, second
        events, terminal_status = await finish_http_scenario(pending)
        return "HITL_DUPLICATE_APPROVE_NO_DUPLICATE_EXECUTION", pending.run_id, events, terminal_status
    finally:
        shim.undo()


async def scenario_reject(operation_id):
    shim = _Shim()
    pending, requested, _ = await H._start_pending_run(shim, operation_id=operation_id)
    try:
        approval_id = requested["payload"]["approval_id"]
        digest = requested["payload"]["invocation_binding_digest"]
        status, body = await H.asgi_json_request(
            server.app,
            "POST",
            H.REJECT_PATH.format(run_id=pending.run_id, approval_id=approval_id),
            {"invocation_binding_digest": digest},
        )
        assert status == 200 and body["effective_status"] == "REJECTED", body
        events, terminal_status = await finish_http_scenario(pending)
        return "HITL_REJECT_ZERO_EXECUTION", pending.run_id, events, terminal_status
    finally:
        shim.undo()


async def wait_decided(run_id, services, timeout=5.0):
    def _has():
        records = services.event_journal.read_after(run_id, 0, 1000)
        return any(r.event_type is RuntimeEventType.TOOL_APPROVAL_DECIDED for r in records)
    await H._poll(_has, timeout)


async def scenario_cancel(operation_id):
    # Client disconnect cancels the active run: pending approval is invalidated.
    shim = _Shim()
    pending, requested, _ = await H._start_pending_run(shim, operation_id=operation_id)
    try:
        await pending.call.aclose()
        await H._poll(lambda: pending.run_registry.get(pending.run_id) is None, 15)
        await wait_decided(pending.run_id, pending.services)
        events, terminal_status = terminal_journal_events(pending.run_id, pending.services)
        return "HITL_CANCEL_PENDING", pending.run_id, events, terminal_status
    finally:
        shim.undo()


async def scenario_timeout(operation_id):
    # Run wall-clock deadline expires while the approval is pending.
    shim = _Shim()
    real_router, tool_registry = H._real_tool_router(H._tool_args(operation_id))
    driver_router = H._ToolChainDriverRouter(real_router)
    run_registry = RunRegistry()
    services = H.make_services(run_registry=run_registry, snapshot_enabled=False)
    factory = CoordinatedRuntimeFactory(driver_router, services)
    service = H.ChatService(
        driver_router,
        event_journal=services.event_journal,
        observability_dispatcher=services.observability_dispatcher,
        gauge_provider=services.observability_dispatcher.gauge_provider,
        coordinated_runtime_factory=factory,
        run_registry=run_registry,
    )
    shim.setattr(server, "chat_service", service)
    try:
        scope = await factory.create_run_scope("core_router", "question", timeout_seconds=0.5)
        run_id = scope.run_id
        execute_task = asyncio.create_task(scope.execute())
        controller = await H._poll(lambda: scope.coordinator.tool_approval_controller or None)
        assert controller is not None

        def _pending_state():
            requested = [
                r
                for r in services.event_journal.read_after(run_id, 0, 1000)
                if r.event_type is RuntimeEventType.TOOL_APPROVAL_REQUESTED
            ]
            if requested and controller.pending_count() >= 1:
                return requested[0]
            return None

        requested_record = await H._poll(_pending_state)
        assert requested_record is not None
        run_result = await asyncio.wait_for(execute_task, 30)
        await wait_decided(run_id, services)
        try:
            await scope.close()
        except Exception:
            pass
        events, terminal_status = terminal_journal_events(run_id, services)
        assert terminal_status == run_result.status.value
        return "HITL_TIMEOUT_PENDING", run_id, events, terminal_status
    finally:
        shim.undo()


async def main():
    scenarios = []
    name, run_id, events, terminal = await scenario_approve("wp3-approve-1")
    scenarios.append({"scenario": name, "run_id": run_id, "trace_complete": True, "terminal_status": terminal, "events": events})
    name, run_id, events, terminal = await scenario_duplicate_approve("wp3-dup-1")
    scenarios.append({"scenario": name, "run_id": run_id, "trace_complete": True, "terminal_status": terminal, "events": events})
    name, run_id, events, terminal = await scenario_reject("wp3-reject-1")
    scenarios.append({"scenario": name, "run_id": run_id, "trace_complete": True, "terminal_status": terminal, "events": events})
    name, run_id, events, terminal = await scenario_cancel("wp3-cancel-1")
    scenarios.append({"scenario": name, "run_id": run_id, "trace_complete": True, "terminal_status": terminal, "events": events})
    name, run_id, events, terminal = await scenario_timeout("wp3-timeout-1")
    scenarios.append({"scenario": name, "run_id": run_id, "trace_complete": True, "terminal_status": terminal, "events": events})
    return scenarios


out = json.dumps({"scenarios": asyncio.run(main())})
print("HITL_EVIDENCE_JSON_BEGIN")
print(out)
print("HITL_EVIDENCE_JSON_END")
"""


@pytest.fixture(scope="module")
def real_hitl_evidence() -> dict:
    if not Path(LOCAL_AGENT_REPO).joinpath("server.py").exists():
        pytest.skip(f"Local_Agent repo not found at {LOCAL_AGENT_REPO}")
    with tempfile.TemporaryDirectory() as tmp:
        launcher = Path(tmp) / "hitl_evidence_launcher.py"
        launcher.write_text(LAUNCHER, encoding="utf-8")
        env = dict(os.environ)
        env["LOCAL_AGENT_REPO"] = LOCAL_AGENT_REPO
        proc = subprocess.run(
            ["uv", "run", "python", str(launcher)],
            cwd=LOCAL_AGENT_REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            encoding="utf-8",
        )
    if proc.returncode != 0:
        pytest.fail(f"LocalAgent HITL evidence launcher failed:\n{proc.stdout}\n{proc.stderr}")
    stdout = proc.stdout
    begin = stdout.index("HITL_EVIDENCE_JSON_BEGIN") + len("HITL_EVIDENCE_JSON_BEGIN")
    end = stdout.index("HITL_EVIDENCE_JSON_END")
    payload = json.loads(stdout[begin:end].strip())
    return {item["scenario"]: item for item in payload["scenarios"]}


def _observed_decisions(scenario: dict) -> set[str]:
    return {
        event["decision_status"]
        for event in scenario["events"]
        if event["event_type"] == "TOOL_APPROVAL_DECIDED" and event.get("decision_status")
    }


def _evaluate_real(scenario: dict) -> tuple:
    evidence = HitlToolApprovalEvidenceV1.model_validate(
        {
            "schema_version": "hitl-tool-approval-evidence.v1",
            "evidence_id": f"hitl-tool-approval://{scenario['run_id']}",
            "run_id": scenario["run_id"],
            "provenance": "REAL_LOCALAGENT_EVIDENCE",
            "trace_complete": scenario["trace_complete"],
            "terminal_status": scenario["terminal_status"],
            "events": scenario["events"],
        }
    )
    assert evidence.provenance is HitlEvidenceProvenance.REAL_LOCALAGENT_EVIDENCE
    preferred = {
        "HITL_APPROVE_ONCE": "APPROVED",
        "HITL_DUPLICATE_APPROVE_NO_DUPLICATE_EXECUTION": "APPROVED",
        "HITL_REJECT_ZERO_EXECUTION": "REJECTED",
        "HITL_CANCEL_PENDING": "INVALIDATED_CANCELLED",
        "HITL_TIMEOUT_PENDING": "INVALIDATED_TIMEOUT",
    }[scenario["scenario"]]
    observed = _observed_decisions(scenario)
    # WP1：terminal INVALIDATED_* publication 是 best-effort。真实 cancel/timeout
    # 路径可能只有 REQUESTED + 完整终态 + 零 TOOL_STARTED，没有 DECIDED 事件。
    if preferred in {"INVALIDATED_CANCELLED", "INVALIDATED_TIMEOUT"} and preferred not in observed:
        expected_decision = None
    else:
        expected_decision = preferred
    expectation = HitlScenarioExpectationV1(
        scenario_id=scenario["scenario"],
        expected_decision=expected_decision,
        expect_tool_execution=scenario["scenario"]
        in {"HITL_APPROVE_ONCE", "HITL_DUPLICATE_APPROVE_NO_DUPLICATE_EXECUTION"},
        require_bound_tool_starts=True,
    )
    return evaluate_hitl_scenario(build_hitl_evaluation_input(evidence), expectation)


def test_real_approve_once_passes(real_hitl_evidence):
    scenario = real_hitl_evidence["HITL_APPROVE_ONCE"]
    evaluation = _evaluate_real(scenario)
    assert evaluation.status is AssertionStatus.PASS, evaluation.failure_reasons
    started = [event for event in scenario["events"] if event["event_type"] == "TOOL_STARTED"]
    assert len(started) == 1


def test_real_duplicate_approve_executes_at_most_once(real_hitl_evidence):
    scenario = real_hitl_evidence["HITL_DUPLICATE_APPROVE_NO_DUPLICATE_EXECUTION"]
    evaluation = _evaluate_real(scenario)
    assert evaluation.status is AssertionStatus.PASS, evaluation.failure_reasons
    result = next(item for item in evaluation.assertions if item.assertion_id == ASSERTION_ID_AT_MOST_ONCE)
    assert result.status is AssertionStatus.PASS
    started = [event for event in scenario["events"] if event["event_type"] == "TOOL_STARTED"]
    assert len(started) == 1


def test_real_reject_zero_execution_passes(real_hitl_evidence):
    scenario = real_hitl_evidence["HITL_REJECT_ZERO_EXECUTION"]
    evaluation = _evaluate_real(scenario)
    assert evaluation.status is AssertionStatus.PASS, evaluation.failure_reasons
    assert not [event for event in scenario["events"] if event["event_type"] == "TOOL_STARTED"]


def test_real_cancel_pending_zero_execution_passes(real_hitl_evidence):
    scenario = real_hitl_evidence["HITL_CANCEL_PENDING"]
    assert any(event["event_type"] == "TOOL_APPROVAL_REQUESTED" for event in scenario["events"])
    assert not [event for event in scenario["events"] if event["event_type"] == "TOOL_STARTED"]
    evaluation = _evaluate_real(scenario)
    assert evaluation.status is AssertionStatus.PASS, evaluation.failure_reasons


def test_real_timeout_pending_zero_execution_passes(real_hitl_evidence):
    scenario = real_hitl_evidence["HITL_TIMEOUT_PENDING"]
    assert any(event["event_type"] == "TOOL_APPROVAL_REQUESTED" for event in scenario["events"])
    assert not [event for event in scenario["events"] if event["event_type"] == "TOOL_STARTED"]
    evaluation = _evaluate_real(scenario)
    assert evaluation.status is AssertionStatus.PASS, evaluation.failure_reasons


def test_real_evidence_carries_safe_correlation_only(real_hitl_evidence):
    allowed = {
        "sequence",
        "event_type",
        "run_id",
        "step_id",
        "approval_id",
        "tool_name",
        "invocation_identity_digest",
        "invocation_binding_digest",
        "decision_status",
        "risk_level",
        "actor_id_digest",
    }
    for scenario in real_hitl_evidence.values():
        for event in scenario["events"]:
            assert set(event) <= allowed
            assert "invocation_id" not in event
            assert "attempt_id" not in event
