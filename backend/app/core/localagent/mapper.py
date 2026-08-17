"""Compatibility mapper: LocalAgent envelope -> legacy Trace/Span write model.

The legacy ``traces``/``spans`` rows are a server write model ONLY.  All
LocalAgent contract truth lives in the compatibility sidecar.  The mapper
therefore uses code-owned display placeholders (never the producer
payload as the semantic Owner) and keeps raw ``Span.error`` NULL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.core.localagent.entities import LocalAgentTraceEnvelopeInV1
from app.registry.constants import SpanKind, SpanStatusCode, TraceStatus

# Code-owned display placeholder for the legacy trace container name.
TRACE_DISPLAY_NAME = "localagent.trace"
# Code-owned display prefix for legacy span names; derived only from the
# validated operation vocabulary, never from arbitrary producer text.
SPAN_DISPLAY_NAME_PREFIX = "localagent:"

# Legacy span status is a display derivation only: CANCELLED/TIMED_OUT map
# to the closest non-OK terminal legacy value.  The exact frozen status is
# preserved in the compatibility sidecar.
_LOCALAGENT_TO_LEGACY_SPAN_STATUS = {
    "OK": SpanStatusCode.OK,
    "ERROR": SpanStatusCode.ERROR,
    "CANCELLED": SpanStatusCode.ERROR,
    "TIMED_OUT": SpanStatusCode.ERROR,
}


def legacy_trace_row(
    envelope: LocalAgentTraceEnvelopeInV1,
    *,
    internal_trace_uuid: UUID,
    project_id: UUID,
) -> dict[str, Any]:
    """Build the legacy ``traces`` insert values (code-owned display)."""
    return {
        "trace_id": internal_trace_uuid,
        "project_id": project_id,
        "name": TRACE_DISPLAY_NAME,
        "status": TraceStatus.COMPLETED.value,
        "input": None,
        "output": None,
        "metadata": {},
        "started_at": envelope.started_at,
        "ended_at": envelope.completed_at,
        "session_id": None,
        "user_id": None,
        "tags": [],
        "environment": None,
        "release": None,
        "created_at": datetime.now(timezone.utc),
    }


def legacy_span_row(
    envelope: LocalAgentTraceEnvelopeInV1,
    *,
    internal_span_uuid: UUID,
    internal_trace_uuid: UUID,
    internal_parent_uuid: UUID | None,
) -> dict[str, Any]:
    """Build the legacy ``spans`` insert values.

    ``Span.error`` is always NULL for LocalAgent compatibility writes and
    ``Span.kind`` is the code-owned ``OTHER`` placeholder; the sidecar is
    the authoritative source for operation/component/status/error.
    """
    return {
        "span_id": internal_span_uuid,
        "trace_id": internal_trace_uuid,
        "parent_span_id": internal_parent_uuid,
        "name": f"{SPAN_DISPLAY_NAME_PREFIX}{envelope.operation}",
        "kind": SpanKind.OTHER.value,
        "status": _LOCALAGENT_TO_LEGACY_SPAN_STATUS[envelope.status].value,
        "input": None,
        "output": None,
        "model": None,
        "token_usage": None,
        "metadata": {},
        "started_at": envelope.started_at,
        "ended_at": envelope.completed_at,
        "error": None,
        "completion_start_time": None,
        "model_parameters": None,
        "cost": None,
    }
