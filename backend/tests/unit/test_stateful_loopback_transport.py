"""Loopback LocalAgent transport bypasses hostile process proxies."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.adapters.evaluation.http_localagent import (
    LOCALAGENT_HTTP_CONFIG,
    LOCALAGENT_HTTP_TARGET_ID,
    LOCALAGENT_HTTP_TARGET_KIND,
    LOCALAGENT_HTTP_TARGET_VERSION,
    LocalAgentHttpExecutionTarget,
)
from app.core.evaluation.execution import ExecutionRequest, ExecutionTargetRef, OutcomeKind
from app.core.evaluation.references import CaseVersionRef
from app.services.evaluation.stateful_environment import (
    MAX_STARTUP_DIAGNOSTIC_BYTES,
    LocalAgentSubprocessProvisioner,
    ScenarioEnvironmentEvidence,
    STARTUP_DIAGNOSTIC_FILE,
    StatefulEnvironmentError,
)

_PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
_ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"


class _LiveProcess:
    returncode = None


class _CleanupProcess:
    returncode = None

    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 1

    async def wait(self) -> int:
        return int(self.returncode or 0)


async def _loopback_server():
    requests: list[tuple[str, str]] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        header = await reader.readuntil(b"\r\n\r\n")
        request_line = header.split(b"\r\n", 1)[0].decode("ascii")
        method, path, _ = request_line.split(" ", 2)
        content_length = next(
            (
                int(line.split(b":", 1)[1].strip())
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ),
            0,
        )
        if content_length:
            await reader.readexactly(content_length)
        requests.append((method, path))
        if path == "/health":
            payload = b'{"status":"ok"}'
        else:
            payload = json.dumps(
                {
                    "run_id": _ATTEMPT_ID,
                    "status": "SUCCEEDED",
                    "stop_reason": "COMPLETED",
                    "error_code": None,
                    "safe_message": None,
                }
            ).encode("utf-8")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(payload)).encode("ascii")
            + b"\r\nConnection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}", requests


def _hostile_proxy(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    before = {name: os.environ.get(name) for name in _PROXY_ENV_NAMES}
    for name in _PROXY_ENV_NAMES:
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    return before


def _target_ref() -> ExecutionTargetRef:
    return ExecutionTargetRef(
        target_id=LOCALAGENT_HTTP_TARGET_ID,
        target_kind=LOCALAGENT_HTTP_TARGET_KIND,
        target_version_ref=LOCALAGENT_HTTP_TARGET_VERSION,
        config_ref=LOCALAGENT_HTTP_CONFIG,
    )


def _request() -> ExecutionRequest:
    return ExecutionRequest(
        request_id="request-1",
        run_id="22222222-2222-4222-8222-222222222222",
        attempt_id=_ATTEMPT_ID,
        case_ref=CaseVersionRef("case-1", "v1"),
        input_payload={"agent_id": "core_router", "query": "health-free execution"},
        timeout=timedelta(seconds=5),
        idempotency_key="idempotency-1",
    )


def test_hostile_proxy_does_not_intercept_loopback_health(monkeypatch, tmp_path):
    """Health client is dedicated and does not depend on NO_PROXY."""
    original = _hostile_proxy(monkeypatch)
    interpreter = tmp_path / "localagent-python.exe"
    interpreter.write_bytes(b"stub")

    async def run() -> None:
        server, base_url, requests = await _loopback_server()
        try:
            provisioner = LocalAgentSubprocessProvisioner(
                localagent_repo=tmp_path,
                base_work_dir=tmp_path / "work",
                localagent_python_executable=interpreter,
                health_timeout_seconds=1.0,
                health_poll_seconds=0.01,
            )
            await provisioner._wait_healthy(base_url, _LiveProcess())
            assert requests == [("GET", "/health")]
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
    assert {name: os.environ.get(name) for name in _PROXY_ENV_NAMES} == {
        name: "http://127.0.0.1:9" for name in _PROXY_ENV_NAMES
    }
    assert original  # explicitly retain the pre-test snapshot; monkeypatch restores it after the test


def test_hostile_proxy_does_not_intercept_loopback_execution(monkeypatch):
    """Production ExecutionTarget POST uses its own trust_env=False client."""
    _hostile_proxy(monkeypatch)

    async def run() -> None:
        server, base_url, requests = await _loopback_server()
        target = LocalAgentHttpExecutionTarget(_target_ref(), base_url)
        try:
            outcome = await target.execute(_request())
            assert outcome.kind is OutcomeKind.SUCCESS
            assert requests == [("POST", "/api/runtime/execute")]
            assert target._client._trust_env is False
        finally:
            await target.aclose()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_cleanup_still_terminates_process_without_global_proxy_mutation(tmp_path):
    """Client construction change leaves cleanup able to terminate its process."""
    interpreter = tmp_path / "localagent-python.exe"
    interpreter.write_bytes(b"stub")
    provisioner = LocalAgentSubprocessProvisioner(
        localagent_repo=tmp_path,
        base_work_dir=tmp_path / "work",
        localagent_python_executable=interpreter,
    )
    process = _CleanupProcess()
    token = "scenario-token"
    provisioner._processes[token] = process  # test-only state setup
    evidence = ScenarioEnvironmentEvidence(
        scenario_id="canary",
        scenario_environment_id="env-canary",
        scenario_token=token,
        work_dir=tmp_path / "work",
        memory_db_path=tmp_path / "work" / "memory.db",
        journal_db_path=tmp_path / "work" / "journal.db",
        target_instance_ref="localagent-process-test",
        localagent_base_url="http://127.0.0.1:1",
        fixture_seeded=False,
        provisioned_at=datetime.now(UTC),
    )

    asyncio.run(provisioner.cleanup(evidence, preserve=True))

    assert process.terminated is True
    assert token not in provisioner._processes


def test_failed_health_check_cleans_up_registered_subprocess(monkeypatch, tmp_path):
    """Health failure is cleaned up before provision can expose an environment."""
    interpreter = tmp_path / "localagent-python.exe"
    interpreter.write_bytes(b"stub")
    process = _CleanupProcess()

    async def fake_exec(*args, **kwargs):
        return process

    async def failed_health(self, base_url, process=None, **kwargs):
        raise RuntimeError("health unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(LocalAgentSubprocessProvisioner, "_wait_healthy", failed_health)
    provisioner = LocalAgentSubprocessProvisioner(
        localagent_repo=tmp_path,
        base_work_dir=tmp_path / "work",
        localagent_python_executable=interpreter,
    )

    scenario = type("Scenario", (), {"scenario_id": "failed-health"})()
    with pytest.raises(RuntimeError, match="health unavailable"):
        asyncio.run(provisioner.provision(scenario))

    assert process.terminated is True
    assert provisioner._processes == {}


def test_early_exit_persists_bounded_redacted_startup_diagnostic(monkeypatch, tmp_path):
    """Early exit retains a bounded private diagnostic without raw credentials."""
    interpreter = tmp_path / "localagent-python.exe"
    interpreter.write_bytes(b"stub")
    secret = "sk-secret-value"
    monkeypatch.setenv("LOCAL_AGENT_REMOTE_API_KEY", secret)

    class _ExitedProcess:
        returncode = 3

        async def wait(self) -> int:
            return 3

        def terminate(self) -> None:
            raise AssertionError("exited process must not be terminated")

        def kill(self) -> None:
            raise AssertionError("exited process must not be killed")

    async def fake_exec(*args, **kwargs):
        kwargs["stdout"].write(b"startup complete\n" + b"x" * (MAX_STARTUP_DIAGNOSTIC_BYTES + 20))
        kwargs["stderr"].write(
            f"Authorization: Bearer abc\napi_key={secret}\nhttp://user:password@proxy:7892\nstartup error\n".encode()
        )
        return _ExitedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    provisioner = LocalAgentSubprocessProvisioner(
        localagent_repo=tmp_path,
        base_work_dir=tmp_path / "work",
        localagent_python_executable=interpreter,
        health_timeout_seconds=1.0,
    )
    scenario = type("Scenario", (), {"scenario_id": "early-exit"})()

    with pytest.raises(StatefulEnvironmentError) as raised:
        asyncio.run(provisioner.provision(scenario))

    diagnostic = raised.value.startup_diagnostic
    assert diagnostic is not None
    assert diagnostic.process_exit_code == 3
    assert diagnostic.startup_failure_phase == "PROVISIONING_PROCESS_EXIT"
    assert len(diagnostic.stdout_tail.encode("utf-8")) <= MAX_STARTUP_DIAGNOSTIC_BYTES
    assert "startup error" in diagnostic.stderr_tail
    assert secret not in diagnostic.stderr_tail
    assert "Bearer abc" not in diagnostic.stderr_tail
    assert "user:password" not in diagnostic.stderr_tail
    artifact = next((tmp_path / "work").glob(f"*/{STARTUP_DIAGNOSTIC_FILE}"))
    payload = artifact.read_text(encoding="utf-8")
    assert secret not in payload
    assert "Bearer abc" not in payload
