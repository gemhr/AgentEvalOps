"""Runtime-neutral online trace normalization domain."""

from app.core.online.entities import (
    FAILURE_OUTCOMES,
    GenericOutcome,
    NormalizedOnlineSpan,
    NormalizedOnlineTrace,
    TraceEvidenceCandidate,
    summarize_outcomes,
    trace_evidence_ref,
)

__all__ = [
    "FAILURE_OUTCOMES",
    "GenericOutcome",
    "NormalizedOnlineSpan",
    "NormalizedOnlineTrace",
    "TraceEvidenceCandidate",
    "summarize_outcomes",
    "trace_evidence_ref",
]
