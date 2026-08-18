"""Focused tests for the runtime-neutral online normalization boundary."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.localagent.entities import LocalAgentTraceEnvelopeInV1
from app.core.localagent.mapper import normalized_span, normalized_trace
from app.core.online.entities import (
    GenericOutcome,
    NormalizedOnlineSpan,
    summarize_outcomes,
)
from app.core.traces.entities import Span, Trace
from app.infrastructure.db.repositories.trace_repo import _legacy_normalized_span, _legacy_normalized_trace
from app.registry.constants import SpanKind, SpanStatusCode, TraceStatus


def _envelope(*, status: str = "OK", attributes: dict | None = None) -> LocalAgentTraceEnvelopeInV1:
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return LocalAgentTraceEnvelopeInV1(
        contract_identity="localagent.runtime.trace_export",
        contract_version=1,
        contract_fingerprint="a" * 64,
        run_id="run-1",
        trace_id="trace-1",
        span_id="span-1",
        parent_span_id=None,
        step_id="step-1",
        operation="vendor.custom.operation",
        component="worker",
        started_at=started_at,
        completed_at=started_at.replace(second=1),
        duration_ms=Decimal("1000.5"),
        status=status,
        error_code="E_CUSTOM" if status != "OK" else None,
        attributes=attributes or {},
    )


def test_generic_module_has_no_localagent_dependency() -> None:
    source = Path(__file__).parents[2] / "app" / "core" / "online" / "entities.py"
    assert "app.core.localagent" not in source.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("OK", GenericOutcome.SUCCESS),
        ("ERROR", GenericOutcome.FAILURE),
        ("CANCELLED", GenericOutcome.CANCELLED),
        ("TIMED_OUT", GenericOutcome.TIMEOUT),
    ],
)
def test_localagent_outcomes_are_lossless(status: str, expected: GenericOutcome) -> None:
    envelope = _envelope(status=status)
    trace = normalized_trace(envelope, internal_trace_uuid=uuid4(), project_id=uuid4())
    span = normalized_span(
        envelope,
        internal_trace_uuid=trace.trace_id,
        internal_span_uuid=uuid4(),
        internal_parent_uuid=None,
        project_id=trace.project_id,
    )
    assert trace.outcome == expected
    assert span.outcome == expected


def test_localagent_contract_version_is_provenance_not_subject_version() -> None:
    envelope = _envelope(attributes={"plan_version": 3, "execution_kind": "AGENT"})
    trace_id = uuid4()
    project_id = uuid4()
    trace = normalized_trace(envelope, internal_trace_uuid=trace_id, project_id=project_id)
    span = normalized_span(
        envelope,
        internal_trace_uuid=trace_id,
        internal_span_uuid=uuid4(),
        internal_parent_uuid=None,
        project_id=project_id,
    )
    assert trace.source_contract_identity == "localagent.runtime.trace_export"
    assert trace.source_contract_version == 1
    assert trace.subject_version_ref is None
    assert dict(span.attributes) == {"plan_version": 3}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (SpanStatusCode.OK, GenericOutcome.SUCCESS),
        (SpanStatusCode.ERROR, GenericOutcome.FAILURE),
        (SpanStatusCode.UNSET, GenericOutcome.UNKNOWN),
    ],
)
def test_legacy_span_mapping(status: SpanStatusCode, expected: GenericOutcome) -> None:
    project_id = uuid4()
    trace_id = uuid4()
    span = Span(
        span_id=uuid4(),
        trace_id=trace_id,
        name="legacy.operation",
        kind=SpanKind.OTHER,
        status=status,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ended_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
    )
    trace = Trace(
        trace_id=trace_id,
        project_id=project_id,
        name="legacy.trace",
        status=TraceStatus.COMPLETED,
        started_at=span.started_at,
        ended_at=span.ended_at,
    )
    normalized = _legacy_normalized_span(trace, span)
    assert normalized.operation == "legacy.operation"
    assert normalized.component is None
    assert normalized.outcome == expected
    assert normalized.duration_ms == Decimal("1000")


def test_legacy_trace_does_not_fabricate_subject_version() -> None:
    project_id = uuid4()
    trace = Trace(
        trace_id=uuid4(),
        project_id=project_id,
        name="legacy.trace",
        status=TraceStatus.COMPLETED,
        environment="production",
        release="release-1",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    normalized = _legacy_normalized_trace(trace)
    assert normalized.source_contract_identity is None
    assert normalized.source_contract_version is None
    assert normalized.subject_version_ref is None


def test_trace_summary_preserves_failure_against_later_success() -> None:
    assert summarize_outcomes(
        [GenericOutcome.FAILURE, GenericOutcome.SUCCESS]
    ) == GenericOutcome.FAILURE
    assert summarize_outcomes(
        [GenericOutcome.CANCELLED, GenericOutcome.SUCCESS]
    ) == GenericOutcome.CANCELLED
    assert summarize_outcomes([GenericOutcome.UNKNOWN]) == GenericOutcome.UNKNOWN


def test_generic_operation_is_open_string() -> None:
    span = NormalizedOnlineSpan(
        project_id=uuid4(),
        trace_id=uuid4(),
        span_id=uuid4(),
        parent_span_id=None,
        operation="producer-specific/operation:v2",
        component=None,
        outcome=GenericOutcome.UNKNOWN,
        error_code=None,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ended_at=None,
        duration_ms=None,
    )
    assert span.operation == "producer-specific/operation:v2"
