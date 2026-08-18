"""Runtime-neutral normalized online trace and span entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping
from uuid import UUID

from app.core.evaluation.references import EvidenceRef

if TYPE_CHECKING:
    from app.core.traces.entities import Trace


class GenericOutcome(StrEnum):
    """Lossless minimum outcome vocabulary shared by online producers."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


FAILURE_OUTCOMES = frozenset(
    {
        GenericOutcome.FAILURE,
        GenericOutcome.CANCELLED,
        GenericOutcome.TIMEOUT,
    }
)

_OUTCOME_PRECEDENCE = {
    GenericOutcome.SUCCESS: 0,
    GenericOutcome.UNKNOWN: 1,
    GenericOutcome.CANCELLED: 2,
    GenericOutcome.TIMEOUT: 3,
    GenericOutcome.FAILURE: 4,
}
_Scalar = str | int | float | bool


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


def _freeze_attributes(attributes: Mapping[str, _Scalar]) -> Mapping[str, _Scalar]:
    frozen = dict(attributes)
    for key, value in frozen.items():
        _require_non_empty(key, "attribute key")
        if not isinstance(value, (str, int, float, bool)):
            raise TypeError("normalized attributes must contain scalar values")
    return MappingProxyType(frozen)


def _validate_duration(duration_ms: Decimal | None) -> None:
    if duration_ms is None:
        return
    if not isinstance(duration_ms, Decimal) or not duration_ms.is_finite() or duration_ms < 0:
        raise ValueError("duration_ms must be a finite, non-negative Decimal")


@dataclass(frozen=True, slots=True)
class NormalizedOnlineTrace:
    """Canonical trace-level projection independent of any producer contract."""

    project_id: UUID
    trace_id: UUID
    source_kind: str
    outcome: GenericOutcome
    source_contract_identity: str | None = None
    source_contract_version: int | None = None
    subject_version_ref: str | None = None

    def __post_init__(self) -> None:
        """Validate source provenance without coupling to a producer contract."""
        _require_non_empty(self.source_kind, "source_kind")
        if self.source_contract_identity is not None:
            _require_non_empty(self.source_contract_identity, "source_contract_identity")
        if self.source_contract_version is not None:
            if isinstance(self.source_contract_version, bool) or self.source_contract_version < 0:
                raise ValueError("source_contract_version must be a non-negative integer")
        if self.subject_version_ref is not None:
            _require_non_empty(self.subject_version_ref, "subject_version_ref")


@dataclass(frozen=True, slots=True)
class NormalizedOnlineSpan:
    """Canonical completed or partially observed span projection."""

    project_id: UUID
    trace_id: UUID
    span_id: UUID
    parent_span_id: UUID | None
    operation: str
    component: str | None
    outcome: GenericOutcome
    error_code: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_ms: Decimal | None
    attributes: Mapping[str, _Scalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate span timing and freeze the safe scalar attributes."""
        _require_non_empty(self.operation, "operation")
        if self.component is not None:
            _require_non_empty(self.component, "component")
        if self.error_code is not None:
            _require_non_empty(self.error_code, "error_code")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        _validate_duration(self.duration_ms)
        object.__setattr__(self, "attributes", _freeze_attributes(self.attributes))


def summarize_outcomes(
    outcomes: list[GenericOutcome] | tuple[GenericOutcome, ...],
    *,
    default: GenericOutcome = GenericOutcome.UNKNOWN,
) -> GenericOutcome:
    """Return a deterministic, monotonic summary for observed child outcomes."""
    if not outcomes:
        return default
    return max(outcomes, key=_OUTCOME_PRECEDENCE.__getitem__)


def trace_evidence_ref(trace_id: UUID) -> EvidenceRef:
    """Build the frozen identity-only evidence reference for a trace.

    The identifier is the global ``trace_id`` UUID string; tenant safety is
    enforced by the caller's project context plus a project-scoped resolver,
    never by encoding the project into the reference.
    """
    return EvidenceRef(kind="trace", identifier=str(trace_id))


@dataclass(frozen=True, slots=True)
class TraceEvidenceCandidate:
    """A failing trace selected as a candidate for offline evaluation.

    This is a query-time value DTO owned by the Online Core.  It is not a
    database entity, not an Evaluation Dataset/TestCase fact, and is never
    persisted.  The evidence reference stays identity-only: no input,
    output, error, attribute or span payload is copied.
    """

    project_id: UUID
    trace_id: UUID
    occurred_at: datetime
    evidence_ref: EvidenceRef = field(init=False, compare=False)
    source_kind: str | None = None
    normalized_outcome: GenericOutcome | None = None

    def __post_init__(self) -> None:
        """Derive the identity-only evidence reference from the trace id."""
        object.__setattr__(self, "evidence_ref", trace_evidence_ref(self.trace_id))

    @classmethod
    def from_trace(cls, trace: Trace) -> "TraceEvidenceCandidate":
        """Convert a resolved domain Trace into a candidate value DTO."""
        return cls(
            project_id=trace.project_id,
            trace_id=trace.trace_id,
            occurred_at=trace.started_at,
            source_kind=trace.normalized_source_kind,
            normalized_outcome=trace.normalized_outcome,
        )
