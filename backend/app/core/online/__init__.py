"""Runtime-neutral online trace normalization domain."""

from app.core.online.entities import (
    FAILURE_OUTCOMES,
    GenericOutcome,
    NormalizedOnlineSpan,
    NormalizedOnlineTrace,
    summarize_outcomes,
)

__all__ = [
    "FAILURE_OUTCOMES",
    "GenericOutcome",
    "NormalizedOnlineSpan",
    "NormalizedOnlineTrace",
    "summarize_outcomes",
]
