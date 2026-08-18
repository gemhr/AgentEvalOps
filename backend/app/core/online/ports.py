"""Minimal application seam for normalized online trace persistence."""

from __future__ import annotations

from typing import Protocol

from app.core.online.entities import NormalizedOnlineSpan, NormalizedOnlineTrace


class TraceIngestPort(Protocol):
    """Persist normalized facts without exposing producer-specific DTOs."""

    async def persist_normalized(
        self,
        trace: NormalizedOnlineTrace,
        span: NormalizedOnlineSpan | None = None,
    ) -> None:
        """Persist one normalized trace projection and an optional child span."""
