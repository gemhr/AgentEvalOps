"""LocalAgent evaluation-execute v3 的 HTTP execution target adapter。

只访问 isolated ``/api/runtime/evaluation-execute/v3``，发送 strict typed
``episodic-evaluation-control.v1`` 控制并严格解析 private evaluation projection。
不读写 production API / event / ranking / context / persistence；不发送 plan /
steps / tool / prompt / model output / callable / status override。
"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import httpx

from app.core.evaluation.episodic_evidence import (
    EVALUATION_EXECUTE_V3_PROTOCOL_VERSION,
    EpisodicCaptureEvidence,
    EpisodicEvidenceError,
    EpisodicFixtureReceiptEvidence,
    EpisodicFormationReceiptEvidence,
    EpisodicRuntimeReceiptEvidence,
)
from app.core.evaluation.execution import ExecutionOutcome, ExecutionRequest, ExecutionTargetRef, OutcomeKind

_EXECUTE_V3_PATH = "/api/runtime/evaluation-execute/v3"


class EpisodicV3TargetError(ValueError):
    """v3 target 请求/响应失败（EVALUATION_INFRA）。"""


@dataclass(frozen=True, slots=True)
class EpisodicV3Response:
    """v3 响应的严格 typed 投影（无 episode 正文）。"""

    protocol_version: str
    run_id: str
    status: str
    stop_reason: str
    error_code: str | None
    safe_message: str | None
    evaluation_control_status: str
    evaluation_error_code: str | None
    capture_status: str
    capture_error_code: str | None
    episodic_capture: EpisodicCaptureEvidence | None
    runtime_receipt: EpisodicRuntimeReceiptEvidence | None
    formation_receipts: tuple[EpisodicFormationReceiptEvidence, ...]
    fixture_receipts: tuple[EpisodicFixtureReceiptEvidence, ...]
    replay_receipts: tuple[EpisodicFormationReceiptEvidence, ...]

    @classmethod
    def from_wire(cls, value: object) -> "EpisodicV3Response":
        """Strictly parse ``value`` into a typed DTO (fail closed)."""
        if not isinstance(value, Mapping):
            raise EpisodicV3TargetError("v3 response must be a JSON object")
        try:
            protocol_version = value["protocol_version"]
            if protocol_version != EVALUATION_EXECUTE_V3_PROTOCOL_VERSION:
                raise EpisodicV3TargetError(f"unsupported v3 protocol_version: {protocol_version!r}")
            run_id = value["run_id"]
            capture = (
                EpisodicCaptureEvidence.from_wire(value["episodic_capture"])
                if value.get("episodic_capture") is not None
                else None
            )
            runtime_receipt = (
                EpisodicRuntimeReceiptEvidence.from_wire(value["runtime_receipt"], "runtime_receipt")
                if value.get("runtime_receipt") is not None
                else None
            )
            formation_receipts = tuple(
                EpisodicFormationReceiptEvidence.from_wire(item, f"formation_receipts[{index}]")
                for index, item in enumerate(value.get("formation_receipts") or [])
            )
            fixture_receipts = tuple(
                EpisodicFixtureReceiptEvidence.from_wire(item, f"fixture_receipts[{index}]")
                for index, item in enumerate(value.get("fixture_receipts") or [])
            )
            replay_receipts = tuple(
                EpisodicFormationReceiptEvidence.from_wire(item, f"replay_receipts[{index}]")
                for index, item in enumerate(value.get("replay_receipts") or [])
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, EpisodicV3TargetError):
                raise
            raise EpisodicV3TargetError(f"v3 response is malformed: {exc}") from exc
        return cls(
            protocol_version=protocol_version,
            run_id=run_id,
            status=value["status"],
            stop_reason=value["stop_reason"],
            error_code=value.get("error_code"),
            safe_message=value.get("safe_message"),
            evaluation_control_status=value["evaluation_control_status"],
            evaluation_error_code=value.get("evaluation_error_code"),
            capture_status=value["capture_status"],
            capture_error_code=value.get("capture_error_code"),
            episodic_capture=capture,
            runtime_receipt=runtime_receipt,
            formation_receipts=formation_receipts,
            fixture_receipts=fixture_receipts,
            replay_receipts=replay_receipts,
        )


@dataclass(frozen=True, slots=True)
class EpisodicV3ExecutionResult:
    """一次 v3 execution 的完整结果（outcome + typed response）。"""

    outcome: ExecutionOutcome
    response: EpisodicV3Response


class EpisodicHttpEvaluationV3Target:
    """向 isolated v3 endpoint 执行一次 run 并返回 typed response。"""

    __slots__ = ("_target_ref", "_base_url", "_client", "_owns_client")

    def __init__(
        self,
        target_ref: ExecutionTargetRef,
        base_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._target_ref = target_ref
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(trust_env=False)
        self._owns_client = client is None

    @property
    def target_ref(self) -> ExecutionTargetRef:
        """Return the computed property value."""
        return self._target_ref

    async def aclose(self) -> None:
        """Close the underlying HTTP client if owned by this target."""
        if self._owns_client:
            await self._client.aclose()

    async def execute_v3(
        self,
        *,
        request: ExecutionRequest,
        run_id: str,
        evaluation_control: Mapping[str, object],
    ) -> EpisodicV3ExecutionResult:
        """Execute one Run through the isolated v3 evaluation endpoint."""
        payload: dict[str, object] = {
            "agent_id": request.input_payload["agent_id"],
            "query": request.input_payload["query"],
            "run_id": run_id,
            "timeout_seconds": request.timeout.total_seconds(),
        }
        if evaluation_control:
            payload["evaluation_control"] = evaluation_control
        started_at = datetime.now(UTC)
        try:
            async with asyncio.timeout(request.timeout.total_seconds()):
                response = await self._client.post(
                    f"{self._base_url}{_EXECUTE_V3_PATH}",
                    json=payload,
                )
        except TimeoutError as exc:
            raise EpisodicV3TargetError("v3 execution timed out") from exc
        except httpx.HTTPError as exc:
            raise EpisodicV3TargetError(f"v3 HTTP transport failure: {type(exc).__name__}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise EpisodicV3TargetError(f"v3 execution returned HTTP {response.status_code}")
        try:
            wire = response.json()
        except ValueError as exc:
            raise EpisodicV3TargetError("v3 response is not valid JSON") from exc
        try:
            typed = EpisodicV3Response.from_wire(wire)
        except (EpisodicV3TargetError, EpisodicEvidenceError) as exc:
            raise EpisodicV3TargetError(f"v3 response parsing failed: {exc}") from exc
        if typed.run_id != run_id:
            raise EpisodicV3TargetError("v3 response run_id mismatch")
        finished_at = datetime.now(UTC)
        outcome = ExecutionOutcome(
            request_id=request.request_id,
            kind=(
                OutcomeKind.SUCCESS
                if typed.status == "SUCCEEDED"
                else OutcomeKind.FAILURE
                if typed.status == "FAILED"
                else OutcomeKind.CANCELLED
            ),
            started_at=started_at,
            finished_at=finished_at,
            output_artifact_ref=(
                f"localagent-episodic-v3://{typed.run_id}"
                if typed.status == "SUCCEEDED"
                else None
            ),
            error_category=(
                None if typed.status == "SUCCEEDED" else typed.error_code or typed.stop_reason or "V3_RUNTIME_FAILURE"
            ),
            reason=None if typed.status == "SUCCEEDED" else (typed.safe_message or typed.stop_reason),
            metadata={
                "target_run_id": typed.run_id,
                "provider_status": typed.status,
                "provider_stop_reason": typed.stop_reason,
                "evaluation_control_status": typed.evaluation_control_status,
                "evaluation_error_code": typed.evaluation_error_code,
                "capture_status": typed.capture_status,
                "capture_error_code": typed.capture_error_code,
            },
        )
        return EpisodicV3ExecutionResult(outcome=outcome, response=typed)


def validate_run_uuid(value: str) -> str:
    """校验 run_id 是 canonical UUID（LocalAgent v3 要求）。"""
    try:
        parsed = str(UUID(value))
    except (AttributeError, ValueError, TypeError) as exc:
        raise EpisodicV3TargetError(f"run_id must be a canonical UUID: {value!r}") from exc
    if parsed != value:
        raise EpisodicV3TargetError("run_id must use canonical UUID representation")
    return parsed


__all__ = [
    "EpisodicHttpEvaluationV3Target",
    "EpisodicV3ExecutionResult",
    "EpisodicV3Response",
    "EpisodicV3TargetError",
    "validate_run_uuid",
]
