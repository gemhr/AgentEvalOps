"""LocalAgent Coordinated Runtime 的 HTTP ExecutionTarget。"""

# ruff: noqa: D415

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, StrictStr

from app.core.evaluation.execution import (
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionTargetRef,
    OutcomeKind,
)
from app.core.evaluation.references import ArtifactRef, EvidenceRef, VersionRef


LOCALAGENT_HTTP_TARGET_ID = "localagent-coordinated-http"
LOCALAGENT_HTTP_TARGET_KIND = "LOCALAGENT_HTTP"
LOCALAGENT_HTTP_TARGET_VERSION = VersionRef(
    kind="localagent_http_execution_target",
    opaque_value="v1",
)
LOCALAGENT_HTTP_CONFIG = VersionRef(
    kind="localagent_http_config",
    opaque_value="localagent-coordinated-v1",
)

_MAX_PROVIDER_TIMEOUT_SECONDS = 3600.0
_CLEANUP_TIMEOUT_SECONDS = 1.0
_MAX_REASON_LENGTH = 500
_EXECUTE_PATH = "/api/runtime/execute"
_CANCEL_PATH = "/api/runtime/runs/{run_id}/cancel"

_STOP_REASONS = frozenset(
    {
        "COMPLETED",
        "UNHANDLED_ERROR",
        "DEADLINE_EXCEEDED",
        "USER_CANCELLED",
        "CLIENT_DISCONNECTED",
        "SYSTEM_SHUTDOWN",
        "MAX_STEPS_REACHED",
        "NO_ACTION",
        "REPEATED_ACTION",
        "BUDGET_EXHAUSTED",
        "PLANNING_FAILED",
    }
)
_CANCELLED_STOP_REASONS = frozenset(
    {"USER_CANCELLED", "CLIENT_DISCONNECTED", "SYSTEM_SHUTDOWN"}
)


class RuntimeExecuteResponse(BaseModel):
    """LocalAgent structured terminal response wire DTO。"""

    model_config = ConfigDict(extra="forbid")

    run_id: StrictStr
    status: Literal["SUCCEEDED", "FAILED", "CANCELLED"]
    stop_reason: StrictStr
    error_code: StrictStr | None
    safe_message: StrictStr | None


class _RuntimeDeadlineExceeded(Exception):
    """HTTP invocation exceeded the Evaluation outer deadline。"""


class _RemoteCleanupError(Exception):
    """Remote cancellation cleanup failed in a controlled way。"""


def _safe_text(*values: object) -> str:
    """把受控 provider 字段压缩为 bounded、无换行的 reason。"""
    parts = [" ".join(str(value).split()) for value in values if value is not None]
    text = "; ".join(part for part in parts if part)
    return text[:_MAX_REASON_LENGTH] or "LocalAgent execution failed"


def _now() -> datetime:
    return datetime.now(UTC)


class LocalAgentHttpExecutionTarget:
    """通过 LocalAgent structured Coordinated endpoint 执行一个 Attempt。"""

    __slots__ = ("_target_ref", "_base_url", "_client", "_owns_client")

    def __init__(
        self,
        target_ref: ExecutionTargetRef,
        base_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._validate_target_ref(target_ref)
        self._base_url = self._validate_base_url(base_url)
        self._target_ref = target_ref
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    @property
    def target_ref(self) -> ExecutionTargetRef:
        """返回 immutable Target identity/version/config snapshot。"""
        return self._target_ref

    async def aclose(self) -> None:
        """关闭由 Adapter 自己创建的 client；注入 client 由调用方负责。"""
        if self._owns_client:
            await self._client.aclose()

    async def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        """执行一次 LocalAgent HTTP request 并返回唯一 terminal observation。"""
        started_at = _now()
        run_id, payload = self._validate_request(request)
        timeout_seconds = request.timeout.total_seconds()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        post_may_be_accepted = False

        try:
            remaining = self._remaining(deadline)
            if remaining <= 0:
                return self._timeout_outcome(
                    request,
                    started_at,
                    run_id,
                    "EVALUATION_TIMEOUT",
                    "Evaluation outer deadline expired before HTTP POST",
                    evidence=False,
                )

            provider_timeout = min(remaining, _MAX_PROVIDER_TIMEOUT_SECONDS)
            post_may_be_accepted = True
            response = await self._post_execute(
                payload=payload,
                run_id=run_id,
                timeout_seconds=provider_timeout,
                remaining=remaining,
            )
        except _RuntimeDeadlineExceeded:
            cleanup_status = await self._best_effort_cancel(run_id)
            return self._timeout_outcome(
                request,
                started_at,
                run_id,
                "EVALUATION_TIMEOUT",
                _safe_text(
                    "Evaluation outer deadline expired",
                    f"remote_cancel={cleanup_status}",
                ),
                evidence=post_may_be_accepted,
                cleanup_status=cleanup_status,
            )
        except asyncio.CancelledError:
            if post_may_be_accepted:
                try:
                    await self._best_effort_cancel(run_id)
                except asyncio.CancelledError:
                    pass
            raise
        except httpx.ConnectError:
            return self._failure_outcome(
                request,
                started_at,
                run_id,
                "HTTP_CONNECTION_FAILURE",
                "LocalAgent connection was not established",
                evidence=False,
            )
        except httpx.TimeoutException as error:
            evidence = not isinstance(error, (httpx.ConnectTimeout, httpx.PoolTimeout))
            cleanup_status = await self._best_effort_cancel(run_id) if evidence else None
            return self._timeout_outcome(
                request,
                started_at,
                run_id,
                "HTTP_TRANSPORT_TIMEOUT",
                f"HTTP transport timeout ({type(error).__name__})",
                evidence=evidence,
                cleanup_status=cleanup_status,
            )
        except (httpx.NetworkError, httpx.ProtocolError, httpx.HTTPError) as error:
            return self._failure_outcome(
                request,
                started_at,
                run_id,
                "HTTP_AMBIGUOUS_TRANSPORT",
                f"HTTP transport failed after request dispatch ({type(error).__name__})",
                kind=OutcomeKind.OUTCOME_UNKNOWN,
                evidence=True,
            )

        return self._map_response(request, started_at, run_id, response)

    async def _post_execute(
        self,
        *,
        payload: dict[str, str | float],
        run_id: str,
        timeout_seconds: float,
        remaining: float,
    ) -> httpx.Response:
        """发送唯一一次 execution POST，覆盖完整 outer deadline。"""
        timeout = httpx.Timeout(
            connect=remaining,
            read=remaining,
            write=remaining,
            pool=remaining,
        )
        try:
            async with asyncio.timeout(remaining):
                return await self._client.post(
                    self._url(_EXECUTE_PATH),
                    json={
                        "agent_id": payload["agent_id"],
                        "query": payload["query"],
                        "run_id": run_id,
                        "timeout_seconds": timeout_seconds,
                    },
                    timeout=timeout,
                )
        except TimeoutError as error:
            raise _RuntimeDeadlineExceeded from error

    async def _best_effort_cancel(self, run_id: str) -> str:
        """以 bounded、最多一次的 request 执行 remote cleanup。"""
        task = asyncio.create_task(self._cancel_remote(run_id))
        try:
            return await asyncio.wait_for(asyncio.shield(task), _CLEANUP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return "timeout"
        except _RemoteCleanupError as error:
            return str(error)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _cancel_remote(self, run_id: str) -> str:
        """调用现有 cancel endpoint，只返回 bounded status。"""
        try:
            response = await self._client.post(
                self._url(_CANCEL_PATH.format(run_id=run_id)),
                timeout=httpx.Timeout(_CLEANUP_TIMEOUT_SECONDS),
            )
        except Exception as error:
            raise _RemoteCleanupError(type(error).__name__) from None

        if response.status_code < 200 or response.status_code >= 300:
            raise _RemoteCleanupError(f"http_{response.status_code}")
        try:
            value = response.json()
        except ValueError:
            raise _RemoteCleanupError("malformed_response") from None
        if not isinstance(value, Mapping) or value.get("run_id") != run_id:
            raise _RemoteCleanupError("run_id_mismatch")
        status = value.get("status")
        if status not in {"cancelled", "already_cancelled", "inactive"}:
            raise _RemoteCleanupError("invalid_status")
        return str(status)

    def _map_response(
        self,
        request: ExecutionRequest,
        started_at: datetime,
        run_id: str,
        response: httpx.Response,
    ) -> ExecutionOutcome:
        """严格解析 provider response，再映射 Runtime terminal fact。"""
        if 400 <= response.status_code <= 499:
            return self._failure_outcome(
                request,
                started_at,
                run_id,
                "HTTP_CLIENT_FAILURE",
                f"LocalAgent returned HTTP {response.status_code}",
                evidence=True,
            )
        if 500 <= response.status_code <= 599:
            return self._failure_outcome(
                request,
                started_at,
                run_id,
                "HTTP_SERVER_FAILURE",
                f"LocalAgent returned HTTP {response.status_code}",
                evidence=True,
            )
        if response.status_code != 200:
            return self._failure_outcome(
                request,
                started_at,
                run_id,
                "PROTOCOL_MALFORMED",
                f"Unexpected HTTP status {response.status_code}",
                kind=OutcomeKind.OUTCOME_UNKNOWN,
                evidence=True,
            )

        try:
            wire = RuntimeExecuteResponse.model_validate(response.json())
        except (TypeError, ValueError):
            return self._failure_outcome(
                request,
                started_at,
                run_id,
                "PROTOCOL_MALFORMED",
                "LocalAgent structured response is invalid",
                kind=OutcomeKind.OUTCOME_UNKNOWN,
                evidence=True,
            )

        if wire.run_id != run_id or wire.stop_reason not in _STOP_REASONS:
            return self._failure_outcome(
                request,
                started_at,
                run_id,
                "PROTOCOL_MALFORMED",
                "LocalAgent structured response identity or enum is invalid",
                kind=OutcomeKind.OUTCOME_UNKNOWN,
                evidence=True,
            )

        if wire.status == "SUCCEEDED":
            if wire.stop_reason != "COMPLETED" or wire.error_code is not None:
                return self._failure_outcome(
                    request,
                    started_at,
                    run_id,
                    "PROTOCOL_MALFORMED",
                    "LocalAgent success terminal fact is inconsistent",
                    kind=OutcomeKind.OUTCOME_UNKNOWN,
                    evidence=True,
                )
            return ExecutionOutcome(
                request_id=request.request_id,
                kind=OutcomeKind.SUCCESS,
                started_at=started_at,
                finished_at=_now(),
                output_artifact_ref=ArtifactRef(
                    artifact_id=f"localagent-run://{run_id}",
                    media_type="application/vnd.localagent.execution-ref+json",
                ),
                evidence_refs=(self._run_evidence(run_id),),
                metadata=self._metadata(request, run_id, wire),
            )

        if wire.status == "FAILED":
            if wire.stop_reason in _CANCELLED_STOP_REASONS | {"COMPLETED"}:
                return self._failure_outcome(
                    request,
                    started_at,
                    run_id,
                    "PROTOCOL_MALFORMED",
                    "LocalAgent failure terminal fact is inconsistent",
                    kind=OutcomeKind.OUTCOME_UNKNOWN,
                    evidence=True,
                    wire=wire,
                )
            if wire.stop_reason == "DEADLINE_EXCEEDED":
                return self._timeout_outcome(
                    request,
                    started_at,
                    run_id,
                    "LOCALAGENT_RUNTIME_TIMEOUT",
                    _safe_text(wire.error_code, wire.safe_message, wire.stop_reason),
                    evidence=True,
                    wire=wire,
                )
            return self._failure_outcome(
                request,
                started_at,
                run_id,
                "LOCALAGENT_RUNTIME_FAILURE",
                _safe_text(wire.error_code, wire.safe_message, wire.stop_reason),
                evidence=True,
                wire=wire,
            )

        if wire.stop_reason not in _CANCELLED_STOP_REASONS:
            return self._failure_outcome(
                request,
                started_at,
                run_id,
                "PROTOCOL_MALFORMED",
                "LocalAgent cancellation terminal fact is inconsistent",
                kind=OutcomeKind.OUTCOME_UNKNOWN,
                evidence=True,
                wire=wire,
            )
        category = (
            "LOCALAGENT_CLIENT_DISCONNECTED"
            if wire.stop_reason == "CLIENT_DISCONNECTED"
            else "LOCALAGENT_REMOTE_CANCELLED"
        )
        return self._failure_outcome(
            request,
            started_at,
            run_id,
            category,
            _safe_text(wire.error_code, wire.safe_message, wire.stop_reason),
            kind=OutcomeKind.CANCELLED,
            evidence=True,
            wire=wire,
        )

    def _validate_request(self, request: ExecutionRequest) -> tuple[str, dict[str, str]]:
        if not isinstance(request.input_payload, Mapping):
            raise ValueError("LocalAgent input_payload must be a JSON object")
        if set(request.input_payload) != {"agent_id", "query"}:
            raise ValueError("LocalAgent input_payload has unsupported fields")
        agent_id = request.input_payload["agent_id"]
        query = request.input_payload["query"]
        if not isinstance(agent_id, str) or not isinstance(query, str):
            raise TypeError("LocalAgent input_payload fields must be strings")
        try:
            run_id = str(UUID(str(request.attempt_id)))
        except (AttributeError, ValueError, TypeError) as error:
            raise ValueError("attempt_id must be a canonical UUID for LocalAgent") from error
        if str(request.attempt_id) != run_id:
            raise ValueError("attempt_id must use canonical UUID representation")
        return run_id, {"agent_id": agent_id, "query": query}

    @staticmethod
    def _validate_target_ref(target_ref: ExecutionTargetRef) -> None:
        expected = ExecutionTargetRef(
            target_id=LOCALAGENT_HTTP_TARGET_ID,
            target_kind=LOCALAGENT_HTTP_TARGET_KIND,
            target_version_ref=LOCALAGENT_HTTP_TARGET_VERSION,
            config_ref=LOCALAGENT_HTTP_CONFIG,
        )
        if (
            target_ref.target_id != expected.target_id
            or target_ref.target_kind != expected.target_kind
            or target_ref.target_version_ref != expected.target_version_ref
            or target_ref.config_ref != expected.config_ref
        ):
            raise ValueError("unsupported LocalAgent target identity/version/config")

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        url = httpx.URL(base_url.strip())
        if url.scheme not in {"http", "https"} or not url.host or url.query or url.fragment:
            raise ValueError("base_url must be an absolute HTTP(S) URL without query or fragment")
        return str(url).rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - asyncio.get_running_loop().time())

    @staticmethod
    def _run_evidence(run_id: str) -> EvidenceRef:
        return EvidenceRef(kind="localagent_run", identifier=run_id, schema_version="v1")

    @staticmethod
    def _metadata(
        request: ExecutionRequest,
        run_id: str,
        wire: RuntimeExecuteResponse | None = None,
        **extra: object,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "idempotency_key": request.idempotency_key,
            "localagent_run_id": run_id,
        }
        if wire is not None:
            metadata.update(
                {
                    "provider_status": wire.status,
                    "provider_stop_reason": wire.stop_reason,
                }
            )
        metadata.update(extra)
        return metadata

    def _failure_outcome(
        self,
        request: ExecutionRequest,
        started_at: datetime,
        run_id: str,
        error_category: str,
        reason: str,
        *,
        kind: OutcomeKind = OutcomeKind.FAILURE,
        evidence: bool,
        wire: RuntimeExecuteResponse | None = None,
        **extra: object,
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            request_id=request.request_id,
            kind=kind,
            started_at=started_at,
            finished_at=_now(),
            evidence_refs=(self._run_evidence(run_id),) if evidence else (),
            error_category=error_category,
            reason=_safe_text(reason),
            metadata=self._metadata(request, run_id, wire, **extra),
        )

    def _timeout_outcome(
        self,
        request: ExecutionRequest,
        started_at: datetime,
        run_id: str,
        error_category: str,
        reason: str,
        *,
        evidence: bool,
        cleanup_status: str | None = None,
        wire: RuntimeExecuteResponse | None = None,
    ) -> ExecutionOutcome:
        extra = {"remote_cancel_attempted": cleanup_status is not None}
        if cleanup_status is not None:
            if cleanup_status in {"cancelled", "already_cancelled", "inactive"}:
                extra["remote_cancel_status"] = cleanup_status
            else:
                extra["remote_cancel_status"] = "failed"
                extra["remote_cancel_error"] = cleanup_status
        return self._failure_outcome(
            request,
            started_at,
            run_id,
            error_category,
            reason,
            kind=OutcomeKind.TIMEOUT,
            evidence=evidence,
            wire=wire,
            **extra,
        )
