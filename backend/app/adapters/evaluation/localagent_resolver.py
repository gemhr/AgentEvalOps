"""Production resolver binding LOCALAGENT_HTTP ExecutionTargetRef to the adapter."""

from __future__ import annotations

from app.adapters.evaluation.http_localagent import (
    LOCALAGENT_HTTP_TARGET_ID,
    LOCALAGENT_HTTP_TARGET_KIND,
    LOCALAGENT_HTTP_TARGET_VERSION,
    LocalAgentHttpExecutionTarget,
)
from app.core.evaluation.execution import ExecutionTarget, ExecutionTargetRef
from app.registry.settings import settings


class LocalAgentHttpExecutionTargetResolver:
    """Resolve a persisted LOCALAGENT_HTTP ref to the concrete HTTP Target.

    The base URL is owned by ``Settings`` (read from ``LOCALAGENT_HTTP_BASE_URL``),
    not by ``ExecutionTargetRef.config_ref.opaque_value``. Unsupported target
    identity, version or config identity fail closed and never fall back to the
    Fixture target. The resolver itself does not open or own the HTTP client
    lifecycle; the adapter owns the client it constructs.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url if base_url is not None else settings.LOCALAGENT_HTTP_BASE_URL

    def resolve(self, target_ref: ExecutionTargetRef) -> ExecutionTarget:
        """Resolve ``target_ref``; unsupported identity raises (propagated)."""
        if target_ref.target_kind != LOCALAGENT_HTTP_TARGET_KIND:
            raise ValueError(f"unsupported execution target kind: {target_ref.target_kind}")
        if target_ref.target_id != LOCALAGENT_HTTP_TARGET_ID:
            raise ValueError(f"unsupported execution target id: {target_ref.target_id}")
        if target_ref.target_version_ref != LOCALAGENT_HTTP_TARGET_VERSION:
            raise ValueError(
                f"unsupported LOCALAGENT_HTTP target version: {target_ref.target_version_ref}"
            )
        return LocalAgentHttpExecutionTarget(target_ref, self._base_url)
