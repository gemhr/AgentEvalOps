"""Focused matrix tests for LocalAgentHttpExecutionTarget (WP1 gaps).

These tests intentionally avoid duplicating the 8 critical contract tests in
``test_localagent_http_execution_target.py``. They cover payload validation
edges, identity fail-closed, HTTP status mapping, malformed protocol, remote
cancellation, transport timeout capping, no-retry on 5xx, cleanup failure
root-cause preservation and Trace independence.
"""

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
CANCEL_URL = f"http://localagent.test/api/runtime/runs/{ATTEMPT_ID}/cancel"


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


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=httpx.Request("POST", EXECUTE_URL))


def raw_response(content: bytes, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, content=content, request=httpx.Request("POST", EXECUTE_URL))


class _FakeClient:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def post(self, url: str, *, json=None, timeout=None) -> httpx.Response:
        self.calls.append((url, json))
        value = self.result(url, json)
        if hasattr(value, "__await__"):
            return await value
        return value

    async def aclose(self) -> None:
        return None


def make_target(client: _FakeClient, **ref_changes: object) -> LocalAgentHttpExecutionTarget:
    return LocalAgentHttpExecutionTarget(
        target_ref(**ref_changes),
        "http://localagent.test",
        client=client,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Payload validation edges                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "not-an-object",
        ["agent_id", "query"],
        12345,
    ],
)
async def test_non_object_payload_fails_closed_before_http(payload: object) -> None:
    async def result(url: str, json: object) -> httpx.Response:
        raise AssertionError("HTTP must not be called for invalid payload")

    client = _FakeClient(result)
    with pytest.raises(ValueError, match="must be a JSON object"):
        await make_target(client).execute(request(input_payload=payload))
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "query"),
    [
        (123, "hello"),
        ("core_router", 456),
        (None, "hello"),
    ],
    ids=["int-agent", "int-query", "none-agent"],
)
async def test_non_string_payload_fields_fail_closed_before_http(agent_id: object, query: object) -> None:
    async def result(url: str, json: object) -> httpx.Response:
        raise AssertionError("HTTP must not be called for non-string fields")

    client = _FakeClient(result)
    with pytest.raises(TypeError, match="must be strings"):
        await make_target(client).execute(
            request(input_payload={"agent_id": agent_id, "query": query})
        )
    assert client.calls == []


# --------------------------------------------------------------------------- #
# Identity fail-closed (no HTTP)                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalid_attempt_id_fails_closed_before_http() -> None:
    async def result(url: str, json: object) -> httpx.Response:
        raise AssertionError("HTTP must not be called for invalid attempt_id")

    client = _FakeClient(result)
    with pytest.raises(ValueError, match="canonical UUID"):
        await make_target(client).execute(request(attempt_id="not-a-uuid"))
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"target_kind": "FIXTURE"},
        {"target_id": "other-target"},
        {"config_ref": VersionRef("localagent_http_config", "other")},
        {"target_version_ref": VersionRef("localagent_http_execution_target", "v2")},
    ],
    ids=["wrong-kind", "wrong-id", "wrong-config", "wrong-version"],
)
async def test_unsupported_target_identity_fails_closed_in_constructor(change: dict[str, object]) -> None:
    async def result(url: str, json: object) -> httpx.Response:
        raise AssertionError("HTTP must not be called")

    client = _FakeClient(result)
    with pytest.raises(ValueError, match="unsupported"):
        make_target(client, **change)
    assert client.calls == []


# --------------------------------------------------------------------------- #
# HTTP status mapping                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_category"),
    [
        (400, "HTTP_CLIENT_FAILURE"),
        (422, "HTTP_CLIENT_FAILURE"),
        (500, "HTTP_SERVER_FAILURE"),
        (503, "HTTP_SERVER_FAILURE"),
    ],
)
async def test_http_status_mapping(status_code: int, expected_category: str) -> None:
    client = _FakeClient(lambda url, json: json_response({}, status_code))
    outcome = await make_target(client).execute(request())

    assert outcome.kind is OutcomeKind.FAILURE
    assert outcome.error_category == expected_category
    assert outcome.output_artifact_ref is None
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_unexpected_non_200_protocol_is_outcome_unknown() -> None:
    client = _FakeClient(lambda url, json: json_response({}, status_code=302))
    outcome = await make_target(client).execute(request())

    assert outcome.kind is OutcomeKind.OUTCOME_UNKNOWN
    assert outcome.error_category == "PROTOCOL_MALFORMED"
    assert len(client.calls) == 1


# --------------------------------------------------------------------------- #
# Malformed protocol                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalid_json_response_is_protocol_malformed() -> None:
    client = _FakeClient(lambda url, json: raw_response(b"not-json-at-all"))
    outcome = await make_target(client).execute(request())

    assert outcome.kind is OutcomeKind.OUTCOME_UNKNOWN
    assert outcome.error_category == "PROTOCOL_MALFORMED"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_run_id_mismatch_is_protocol_malformed() -> None:
    client = _FakeClient(
        lambda url, json: json_response(
            {
                "run_id": "99999999-9999-4999-8999-999999999999",
                "status": "SUCCEEDED",
                "stop_reason": "COMPLETED",
                "error_code": None,
                "safe_message": None,
            }
        )
    )
    outcome = await make_target(client).execute(request())

    assert outcome.kind is OutcomeKind.OUTCOME_UNKNOWN
    assert outcome.error_category == "PROTOCOL_MALFORMED"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_inconsistent_success_terminal_is_protocol_malformed() -> None:
    client = _FakeClient(
        lambda url, json: json_response(
            {
                "run_id": ATTEMPT_ID,
                "status": "SUCCEEDED",
                "stop_reason": "COMPLETED",
                "error_code": "SOME_ERROR",
                "safe_message": None,
            }
        )
    )
    outcome = await make_target(client).execute(request())

    assert outcome.kind is OutcomeKind.OUTCOME_UNKNOWN
    assert outcome.error_category == "PROTOCOL_MALFORMED"
    assert len(client.calls) == 1


# --------------------------------------------------------------------------- #
# Remote cancellation terminal mapping                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_remote_user_cancelled_maps_to_remote_cancelled() -> None:
    client = _FakeClient(
        lambda url, json: json_response(
            {
                "run_id": ATTEMPT_ID,
                "status": "CANCELLED",
                "stop_reason": "USER_CANCELLED",
                "error_code": "RUN_CANCELLED",
                "safe_message": "user cancelled",
            }
        )
    )
    outcome = await make_target(client).execute(request())

    assert outcome.kind is OutcomeKind.CANCELLED
    assert outcome.error_category == "LOCALAGENT_REMOTE_CANCELLED"
    assert outcome.output_artifact_ref is None
    assert len(client.calls) == 1


# --------------------------------------------------------------------------- #
# Transport timeout + provider timeout cap                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_http_transport_timeout_maps_to_http_transport_timeout() -> None:
    async def result(url: str, json: object) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timeout", request=httpx.Request("POST", EXECUTE_URL))

    client = _FakeClient(result)
    outcome = await make_target(client).execute(request())

    assert outcome.kind is OutcomeKind.TIMEOUT
    assert outcome.error_category == "HTTP_TRANSPORT_TIMEOUT"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_provider_timeout_capped_at_3600_seconds() -> None:
    async def result(url: str, json: object) -> httpx.Response:
        return json_response(
            {
                "run_id": ATTEMPT_ID,
                "status": "SUCCEEDED",
                "stop_reason": "COMPLETED",
                "error_code": None,
                "safe_message": None,
            }
        )

    client = _FakeClient(result)
    await make_target(client).execute(request(timeout=timedelta(seconds=7200)))

    sent = client.calls[0][1]
    assert sent is not None
    assert sent["timeout_seconds"] == pytest.approx(3600.0, abs=0.01)
    assert sent["timeout_seconds"] <= 3600.0


# --------------------------------------------------------------------------- #
# No automatic retry                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_5xx_failure_is_not_retried() -> None:
    client = _FakeClient(lambda url, json: json_response({}, status_code=503))
    outcome = await make_target(client).execute(request())

    assert outcome.kind is OutcomeKind.FAILURE
    assert outcome.error_category == "HTTP_SERVER_FAILURE"
    assert len(client.calls) == 1


# --------------------------------------------------------------------------- #
# Cleanup failure preserves timeout root cause                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_evaluation_timeout_cleanup_failure_preserves_root_cause() -> None:
    async def result(url: str, json: object) -> httpx.Response:
        if url == EXECUTE_URL:
            await asyncio.sleep(60)
        raise httpx.ConnectError("cleanup connection refused", request=httpx.Request("POST", CANCEL_URL))

    client = _FakeClient(result)
    outcome = await make_target(client).execute(request(timeout=timedelta(milliseconds=20)))

    assert outcome.kind is OutcomeKind.TIMEOUT
    assert outcome.error_category == "EVALUATION_TIMEOUT"
    assert outcome.metadata["remote_cancel_attempted"] is True
    assert outcome.metadata["remote_cancel_status"] == "failed"
    assert "remote_cancel_error" in outcome.metadata
    assert [url for url, _ in client.calls] == [EXECUTE_URL, CANCEL_URL]


# --------------------------------------------------------------------------- #
# Trace independence                                                          #
# --------------------------------------------------------------------------- #


def test_adapter_has_no_trace_dependency() -> None:
    import importlib

    module = importlib.import_module("app.adapters.evaluation.http_localagent")
    source = module.__doc__ or ""
    for obj_name in ("TraceEvidenceCandidate", "trace_evidence_ref", "AgentEvalOpsTraceExporter"):
        assert not hasattr(module, obj_name), f"{obj_name} must not be referenced"
    for token in ("TraceExportEnvelope", "trace_evidence_ref", "LocalAgentTraceEnvelopeInV1"):
        assert token not in source


@pytest.mark.asyncio
async def test_success_completes_without_trace_evidence() -> None:
    async def result(url: str, json: object) -> httpx.Response:
        return json_response(
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
    assert outcome.evidence_refs[0].kind == "localagent_run"
    assert all(ref.kind != "trace" for ref in outcome.evidence_refs)
