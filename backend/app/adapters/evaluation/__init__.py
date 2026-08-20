"""Minimal adapters for Evaluation ports."""

from app.adapters.evaluation.fixture import FixtureExecution, FixtureExecutionTarget
from app.adapters.evaluation.http_localagent import (
    LOCALAGENT_HTTP_CONFIG,
    LOCALAGENT_HTTP_TARGET_ID,
    LOCALAGENT_HTTP_TARGET_KIND,
    LOCALAGENT_HTTP_TARGET_VERSION,
    LocalAgentHttpExecutionTarget,
    RuntimeExecuteResponse,
)
from app.adapters.evaluation.localagent_resolver import (
    LocalAgentHttpExecutionTargetResolver,
)

__all__ = [
    "FixtureExecution",
    "FixtureExecutionTarget",
    "LOCALAGENT_HTTP_CONFIG",
    "LOCALAGENT_HTTP_TARGET_ID",
    "LOCALAGENT_HTTP_TARGET_KIND",
    "LOCALAGENT_HTTP_TARGET_VERSION",
    "LocalAgentHttpExecutionTarget",
    "LocalAgentHttpExecutionTargetResolver",
    "RuntimeExecuteResponse",
]
