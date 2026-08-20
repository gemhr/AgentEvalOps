import asyncio
from datetime import timedelta

import httpx
import pytest

from app.adapters.evaluation import (
    LOCALAGENT_HTTP_CONFIG,
    LOCALAGENT_HTTP_TARGET_ID,
    LOCALAGENT_HTTP_TARGET_KIND,
    LOCALAGENT_HTTP_TARGET_VERSION,
    LocalAgentHttpExecutionTarget,
)
from app.core.evaluation import (
    CaseVersionRef,
    ExecutionRequest,
    ExecutionTargetRef,
    OutcomeKind,
    VersionRef,
)


ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
EXECUTE_URL = "http://localagent.test/api/runtime/execute"


def target_ref(**changes: object) -> ExecutionTargetRef:
    values: dict[str, object] = {
        "target_id": LOCALAGENT_HTTP_TARGET_ID,
        "target_kind": LOCALAGENT_HTTP_TARGET_KIND,
        "target_version_ref": LOCALAGENT_HTTP_TARGET_VERSION,
        "config_ref": LOCALAGENT_HTTP_CONFIG,
    }
    values.update(changes)
    return ExecutionTargetRef(**values)  # type: ignore[arg-type]


def request(**changes: object) -> ExecutionRequest:
    values: dict[str, object] = {
        "request_id": "request-1",
        "run_id": "22222222-2222-4222-8222-222222222222",
        "attempt_id": ATTEMPT_ID,
        "case_ref": CaseVersionRef("case-1", "v1"),
        "input_payload": {"agent_id": "core_router", "query": "hello"},
        "timeout": timedelta(seconds=30),
        "idempotency_key": "idempotency-1",
    }
    values.update(changes)
    return ExecutionRequest(**values)  # type: ignore[arg-type]


def response(payload: dict[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", EXECUTE_URL),
    )


class _FakeClient:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def post(self, url: str, *, json=None, timeout=None) -> httpx.Response:
        self.calls.append((url, json))
        return await self.result(url, json)

    async def aclose(self) -> None:
        return None


def make_target(client: _FakeClient, **ref_changes: object) -> LocalAgentHttpExecutionTarget:
    return LocalAgentHttpExecutionTarget(
        target_ref(**ref_changes),
        "http://localagent.test",
        client=client,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_success_maps_structured_terminal_fact_and_refs() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        return response(
            {
                "run_id": ATTEMPT_ID,
                "status": "SUCCEEDED",
                "stop_reason": "COMPLETED",
                "error_code": None,
                "safe_message": None,
            }
        )

    client = _FakeClient(result)
    outcome = await make_target(client).execute(request())

    assert outcome.kind is OutcomeKind.SUCCESS
    assert outcome.output_artifact_ref is not None
    assert outcome.output_artifact_ref.artifact_id == f"localagent-run://{ATTEMPT_ID}"
    assert outcome.evidence_refs[0].kind == "localagent_run"
    assert outcome.evidence_refs[0].identifier == ATTEMPT_ID
    assert len(client.calls) == 1
    assert client.calls[0][0] == EXECUTE_URL
    assert client.calls[0][1] is not None
    assert client.calls[0][1]["agent_id"] == "core_router"
    assert client.calls[0][1]["query"] == "hello"
    assert client.calls[0][1]["run_id"] == ATTEMPT_ID
    assert client.calls[0][1]["timeout_seconds"] == pytest.approx(30.0, abs=0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "stop_reason", "expected_kind", "expected_category"),
    [
        ("FAILED", "UNHANDLED_ERROR", OutcomeKind.FAILURE, "LOCALAGENT_RUNTIME_FAILURE"),
        ("FAILED", "DEADLINE_EXCEEDED", OutcomeKind.TIMEOUT, "LOCALAGENT_RUNTIME_TIMEOUT"),
        ("CANCELLED", "CLIENT_DISCONNECTED", OutcomeKind.CANCELLED, "LOCALAGENT_CLIENT_DISCONNECTED"),
    ],
)
async def test_structured_runtime_terminal_mapping(
    status: str,
    stop_reason: str,
    expected_kind: OutcomeKind,
    expected_category: str,
) -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        return response(
            {
                "run_id": ATTEMPT_ID,
                "status": status,
                "stop_reason": stop_reason,
                "error_code": "RUNTIME_TEST_ERROR",
                "safe_message": "bounded safe message",
            }
        )

    outcome = await make_target(_FakeClient(result)).execute(request())

    assert outcome.kind is expected_kind
    assert outcome.error_category == expected_category
    assert outcome.output_artifact_ref is None
    assert "RUNTIME_TEST_ERROR" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_ambiguous_reset_is_unknown_and_never_retried() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=httpx.Request("POST", EXECUTE_URL))

    client = _FakeClient(result)
    outcome = await make_target(client).execute(request())

    assert outcome.kind is OutcomeKind.OUTCOME_UNKNOWN
    assert outcome.error_category == "HTTP_AMBIGUOUS_TRANSPORT"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_evaluation_timeout_cleans_up_once_without_changing_root_cause() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        if url == EXECUTE_URL:
            await asyncio.sleep(60)
        return response({"run_id": ATTEMPT_ID, "status": "cancelled"})

    client = _FakeClient(result)
    outcome = await make_target(client).execute(request(timeout=timedelta(milliseconds=20)))

    assert outcome.kind is OutcomeKind.TIMEOUT
    assert outcome.error_category == "EVALUATION_TIMEOUT"
    assert outcome.metadata["remote_cancel_status"] == "cancelled"
    assert [url for url, _ in client.calls] == [
        EXECUTE_URL,
        f"http://localagent.test/api/runtime/runs/{ATTEMPT_ID}/cancel",
    ]


@pytest.mark.asyncio
async def test_cancelled_error_is_reraised_after_bounded_cleanup() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        if url == EXECUTE_URL:
            started.set()
            await release.wait()
        return response({"run_id": ATTEMPT_ID, "status": "cancelled"})

    client = _FakeClient(result)
    task = asyncio.create_task(make_target(client).execute(request()))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(client.calls) == 2


def test_invalid_payload_and_identity_fail_closed_before_http() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        raise AssertionError("HTTP must not be called")

    client = _FakeClient(result)
    target = make_target(client)
    with pytest.raises(ValueError, match="unsupported fields"):
        asyncio.run(
            target.execute(
                request(input_payload={"agent_id": "core_router", "query": "hello", "file_path": "x"})
            )
        )
    with pytest.raises(ValueError, match="unsupported LocalAgent target"):
        make_target(client, target_version_ref=VersionRef("localagent_http_execution_target", "v2"))
    assert client.calls == []
