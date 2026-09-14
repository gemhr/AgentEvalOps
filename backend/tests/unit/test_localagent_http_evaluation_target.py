"""LocalAgentHttpExecutionTarget —— RAG evaluation protocol consumer focused tests."""

from __future__ import annotations

from datetime import timedelta
import hashlib

import httpx
import pytest

from app.adapters.evaluation import (
    LOCALAGENT_HTTP_EVALUATION_V2_CONFIG,
    LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION,
    LOCALAGENT_HTTP_TARGET_ID,
    LOCALAGENT_HTTP_TARGET_KIND,
    LocalAgentHttpExecutionTarget,
)
from app.core.evaluation import (
    CaseVersionRef,
    ExecutionRequest,
    ExecutionTargetRef,
    OutcomeKind,
)
from tests.unit.test_rag_artifact import artifact_payload

ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
EVALUATION_V2_URL = "http://localagent.test/api/runtime/evaluation-execute/v2"


def target_ref_v2(**changes: object) -> ExecutionTargetRef:
    values: dict[str, object] = {
        "target_id": LOCALAGENT_HTTP_TARGET_ID,
        "target_kind": LOCALAGENT_HTTP_TARGET_KIND,
        "target_version_ref": LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION,
        "config_ref": LOCALAGENT_HTTP_EVALUATION_V2_CONFIG,
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


def evaluation_v2_body(
    *,
    status: str = "SUCCEEDED",
    final_status: str = "COMPLETE",
    final_error_code: str | None = None,
    final_evidence: dict[str, object] | None = None,
    artifacts: list[dict[str, object]] | None = None,
    **changes: object,
) -> dict[str, object]:
    content = "answer-v2"
    evidence = final_evidence
    if evidence is None and final_status == "COMPLETE":
        evidence = {
            "schema_version": "final-answer-evidence.v1",
            "evidence_id": f"final-answer://{ATTEMPT_ID}",
            "run_id": ATTEMPT_ID,
            "attempt_id": ATTEMPT_ID,
            "media_type": "text/plain; charset=utf-8",
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content": content,
        }
    if final_status == "FAILED" and final_error_code is None:
        final_error_code = "FINAL_ANSWER_RUNTIME_NOT_SUCCEEDED"
    body = {
        "protocol_version": "localagent-evaluation-execute.v2",
        "run_id": ATTEMPT_ID,
        "status": status,
        "stop_reason": changes.pop("stop_reason", "COMPLETED"),
        "error_code": changes.pop("error_code", None),
        "safe_message": changes.pop("safe_message", None),
        "capture_status": changes.pop("capture_status", "COMPLETE"),
        "capture_error_code": changes.pop("capture_error_code", None),
        "rag_evaluation_artifacts": artifacts if artifacts is not None else [],
        "final_answer_capture_status": final_status,
        "final_answer_capture_error_code": final_error_code,
        "final_answer_evidence": evidence,
    }
    body.update(changes)
    return body


def response(payload: dict[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", EVALUATION_V2_URL),
    )


class _FakeClient:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object] | None, dict[str, str] | None]] = []

    async def post(self, url: str, *, json=None, timeout=None, headers=None) -> httpx.Response:
        self.calls.append((url, json, headers))
        return await self.result(url, json)

    async def aclose(self) -> None:
        return None


def make_target_v2(client: _FakeClient, **ref_changes: object) -> LocalAgentHttpExecutionTarget:
    return LocalAgentHttpExecutionTarget(
        target_ref_v2(**ref_changes),
        "http://localagent.test",
        bearer_token="test-service-token",
        client=client,  # type: ignore[arg-type]
    )


def test_bearer_token_is_required() -> None:
    client = _FakeClient(lambda url, payload: response(evaluation_v2_body()))
    with pytest.raises(ValueError, match="bearer_token must be a non-empty string"):
        LocalAgentHttpExecutionTarget(
            target_ref_v2(), "http://localagent.test", bearer_token=" ", client=client  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_v2_endpoint_maps_final_answer_and_rag_evidence() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        return response(evaluation_v2_body(artifacts=[artifact_payload()]))

    client = _FakeClient(result)
    outcome = await make_target_v2(client).execute(request())

    assert client.calls[0][0] == EVALUATION_V2_URL
    assert client.calls[0][2] == {"Authorization": "Bearer test-service-token"}
    assert outcome.kind is OutcomeKind.SUCCESS
    assert [ref.kind for ref in outcome.evidence_refs] == [
        "localagent_run",
        "rag_evaluation_artifact",
        "final_answer",
    ]
    final_ref = outcome.evidence_refs[-1]
    assert final_ref.identifier == f"final-answer://{ATTEMPT_ID}"
    assert final_ref.metadata["payload"]["content"] == "answer-v2"
    assert outcome.metadata["final_answer_capture_status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_v2_runtime_failure_keeps_terminal_without_final_answer_evidence() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        return response(
            evaluation_v2_body(
                status="FAILED",
                stop_reason="UNHANDLED_ERROR",
                final_status="FAILED",
                artifacts=[artifact_payload()],
            )
        )

    outcome = await make_target_v2(_FakeClient(result)).execute(request())

    assert outcome.kind is OutcomeKind.FAILURE
    assert [ref.kind for ref in outcome.evidence_refs] == [
        "localagent_run",
        "rag_evaluation_artifact",
    ]
    assert outcome.metadata["final_answer_capture_status"] == "FAILED"


@pytest.mark.asyncio
async def test_v2_malformed_final_answer_fails_closed() -> None:
    invalid = evaluation_v2_body()
    invalid["final_answer_evidence"]["content_sha256"] = "0" * 64  # type: ignore[index]

    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        return response(invalid)

    outcome = await make_target_v2(_FakeClient(result)).execute(request())

    assert outcome.kind is OutcomeKind.OUTCOME_UNKNOWN
    assert outcome.error_category == "PROTOCOL_MALFORMED"
