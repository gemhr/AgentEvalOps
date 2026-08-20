"""LocalAgentHttpExecutionTarget —— RAG evaluation protocol consumer focused tests."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.adapters.evaluation import (
    LOCALAGENT_HTTP_EVALUATION_CONFIG,
    LOCALAGENT_HTTP_EVALUATION_TARGET_VERSION,
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
EVALUATION_URL = "http://localagent.test/api/runtime/evaluation-execute/v1"


def target_ref(**changes: object) -> ExecutionTargetRef:
    values: dict[str, object] = {
        "target_id": LOCALAGENT_HTTP_TARGET_ID,
        "target_kind": LOCALAGENT_HTTP_TARGET_KIND,
        "target_version_ref": LOCALAGENT_HTTP_EVALUATION_TARGET_VERSION,
        "config_ref": LOCALAGENT_HTTP_EVALUATION_CONFIG,
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


def evaluation_body(
    *,
    status: str = "SUCCEEDED",
    stop_reason: str = "COMPLETED",
    capture_status: str = "COMPLETE",
    capture_error_code: str | None = None,
    artifacts: list[dict[str, object]] | None = None,
    run_id: str = ATTEMPT_ID,
) -> dict[str, object]:
    return {
        "protocol_version": "localagent-rag-evaluation-execute.v1",
        "run_id": run_id,
        "status": status,
        "stop_reason": stop_reason,
        "error_code": None,
        "safe_message": None,
        "capture_status": capture_status,
        "capture_error_code": capture_error_code,
        "rag_evaluation_artifacts": artifacts if artifacts is not None else [],
    }


def response(payload: dict[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", EVALUATION_URL),
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
async def test_evaluation_endpoint_used_and_success_complete_maps_artifacts() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        return response(evaluation_body(artifacts=[artifact_payload()]))

    client = _FakeClient(result)
    outcome = await make_target(client).execute(request())

    assert len(client.calls) == 1
    assert client.calls[0][0] == EVALUATION_URL
    assert client.calls[0][1]["run_id"] == ATTEMPT_ID

    assert outcome.kind is OutcomeKind.SUCCESS
    assert outcome.output_artifact_ref is not None
    assert outcome.output_artifact_ref.artifact_id == f"localagent-run://{ATTEMPT_ID}"
    kinds = [ref.kind for ref in outcome.evidence_refs]
    assert kinds[0] == "localagent_run"
    assert kinds[1] == "rag_evaluation_artifact"
    artifact_ref = outcome.evidence_refs[1]
    assert artifact_ref.identifier == f"rag-eval://{ATTEMPT_ID}/r1"
    assert artifact_ref.metadata["capture_status"] == "COMPLETE"
    assert artifact_ref.metadata["payload"]["artifact_id"] == f"rag-eval://{ATTEMPT_ID}/r1"
    assert outcome.metadata["rag_evaluation_capture_status"] == "COMPLETE"
    assert "rag_evaluation_capture_error_code" not in outcome.metadata


@pytest.mark.asyncio
async def test_runtime_failed_capture_complete_keeps_artifacts_without_output_artifact() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        return response(
            evaluation_body(
                status="FAILED",
                stop_reason="UNHANDLED_ERROR",
                capture_status="COMPLETE",
                artifacts=[artifact_payload()],
            )
        )

    outcome = await make_target(_FakeClient(result)).execute(request())

    assert outcome.kind is OutcomeKind.FAILURE
    assert outcome.error_category == "LOCALAGENT_RUNTIME_FAILURE"
    assert outcome.output_artifact_ref is None
    kinds = [ref.kind for ref in outcome.evidence_refs]
    assert kinds[0] == "localagent_run"
    assert kinds[1] == "rag_evaluation_artifact"
    assert outcome.metadata["rag_evaluation_capture_status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_succeeded_capture_failed_keeps_succeeded_terminal() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        return response(
            evaluation_body(
                status="SUCCEEDED",
                capture_status="FAILED",
                capture_error_code="RAG_EVALUATION_QUERY_LIMIT_EXCEEDED",
                artifacts=[],
            )
        )

    outcome = await make_target(_FakeClient(result)).execute(request())

    assert outcome.kind is OutcomeKind.SUCCESS
    assert outcome.output_artifact_ref is not None
    assert [ref.kind for ref in outcome.evidence_refs] == ["localagent_run"]
    assert outcome.metadata["rag_evaluation_capture_status"] == "FAILED"
    assert outcome.metadata["rag_evaluation_capture_error_code"] == "RAG_EVALUATION_QUERY_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_partial_capture_preserves_valid_artifacts() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        return response(
            evaluation_body(
                status="SUCCEEDED",
                capture_status="PARTIAL",
                capture_error_code="RAG_EVALUATION_DUPLICATE_RETRIEVAL_ID",
                artifacts=[artifact_payload()],
            )
        )

    outcome = await make_target(_FakeClient(result)).execute(request())

    assert outcome.kind is OutcomeKind.SUCCESS
    kinds = [ref.kind for ref in outcome.evidence_refs]
    assert kinds[1] == "rag_evaluation_artifact"
    assert outcome.metadata["rag_evaluation_capture_status"] == "PARTIAL"
    assert outcome.metadata["rag_evaluation_capture_error_code"] == "RAG_EVALUATION_DUPLICATE_RETRIEVAL_ID"


@pytest.mark.asyncio
async def test_artifact_malformed_under_complete_is_protocol_malformed_unknown() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        bad = artifact_payload(retrieval_status="BOGUS")
        return response(evaluation_body(artifacts=[bad]))

    outcome = await make_target(_FakeClient(result)).execute(request())

    assert outcome.kind is OutcomeKind.OUTCOME_UNKNOWN
    assert outcome.error_category == "PROTOCOL_MALFORMED"
    assert outcome.output_artifact_ref is None


@pytest.mark.asyncio
async def test_artifact_run_id_mismatch_under_complete_is_protocol_malformed() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        wrong = artifact_payload(run_id="22222222-2222-4222-8222-222222222222")
        return response(evaluation_body(artifacts=[wrong]))

    outcome = await make_target(_FakeClient(result)).execute(request())

    assert outcome.kind is OutcomeKind.OUTCOME_UNKNOWN
    assert outcome.error_category == "PROTOCOL_MALFORMED"


@pytest.mark.asyncio
async def test_artifact_malformed_under_partial_keeps_terminal_drops_artifacts() -> None:
    async def result(url: str, payload: dict[str, object] | None) -> httpx.Response:
        bad = artifact_payload(retrieval_status="BOGUS")
        return response(
            evaluation_body(
                status="SUCCEEDED",
                capture_status="PARTIAL",
                capture_error_code="RAG_EVALUATION_PROJECTION_FAILED",
                artifacts=[bad],
            )
        )

    outcome = await make_target(_FakeClient(result)).execute(request())

    # PARTIAL 下保留真实 execution outcome，仅丢弃无法解析的 artifact。
    assert outcome.kind is OutcomeKind.SUCCESS
    assert outcome.output_artifact_ref is not None
    assert [ref.kind for ref in outcome.evidence_refs] == ["localagent_run"]
    assert outcome.metadata["rag_evaluation_capture_status"] == "PARTIAL"
    assert outcome.metadata["rag_evaluation_capture_error_code"] == "RAG_EVALUATION_PROJECTION_FAILED"
