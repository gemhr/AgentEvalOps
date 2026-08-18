"""Focused unit tests for the Trace→Dataset feedback command/service."""

# ruff: noqa: D101, D102, D105, D415

import dataclasses
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.evaluation.feedback import TraceFeedbackCandidateError, TraceFeedbackCommand, TraceFeedbackError
from app.core.evaluation.immutable import FrozenDict
from app.core.evaluation.references import CaseVersionRef
from app.core.online.entities import GenericOutcome, trace_evidence_ref
from app.core.traces.entities import Span, Trace, TraceDetail
from app.registry.constants import SpanKind, SpanStatusCode, TraceStatus
from app.registry.exceptions import NotFoundError
from app.services.evaluation.feedback import TraceFeedbackService
from app.services.trace_service import TraceService

pytestmark = pytest.mark.asyncio

PROJECT_A = UUID("00000000-0000-4000-a000-0000000000aa")
PROJECT_B = UUID("00000000-0000-4000-a000-0000000000bb")


def _trace(
    *,
    project_id: UUID = PROJECT_A,
    trace_id: UUID | None = None,
    outcome: GenericOutcome = GenericOutcome.FAILURE,
    span_outcome: GenericOutcome | None = None,
) -> Trace:
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trace_id = trace_id or uuid4()
    span = Span(
        span_id=uuid4(),
        trace_id=trace_id,
        parent_span_id=None,
        name="operation",
        kind=SpanKind.OTHER,
        status=SpanStatusCode.ERROR if span_outcome == GenericOutcome.FAILURE else SpanStatusCode.OK,
        started_at=started,
        ended_at=started,
        normalized_outcome=span_outcome or outcome,
    )
    return Trace(
        trace_id=trace_id,
        project_id=project_id,
        name="trace",
        status=TraceStatus.COMPLETED,
        started_at=started,
        ended_at=started,
        spans=[span],
        normalized_source_kind="legacy",
        normalized_outcome=outcome,
    )


class FakeTraceService:
    """Deterministic stand-in for ``TraceService.get_trace``."""

    def __init__(self, trace: Trace | None) -> None:
        self.trace = trace

    async def get_trace(self, trace_id: UUID, project_id: UUID) -> TraceDetail:
        if self.trace is None or self.trace.trace_id != trace_id or self.trace.project_id != project_id:
            raise NotFoundError(f"Trace {trace_id} not found.")
        return TraceDetail(trace=self.trace, total_tokens=0, total_cost=0.0)


def _service(trace: Trace | None) -> TraceFeedbackService:
    return TraceFeedbackService(trace_service=FakeTraceService(trace))  # type: ignore[arg-type]


def _command(trace: Trace | None = None, **overrides: object) -> TraceFeedbackCommand:
    values: dict[str, object] = {
        "project_id": PROJECT_A,
        "trace_id": uuid4(),
        "dataset_id": "dataset",
        "dataset_version": "v2",
        "case_id": "case-f",
        "case_version": "v1",
        "input_payload": {"sanitized_input": True},
    }
    if trace is not None:
        values["project_id"] = trace.project_id
        values["trace_id"] = trace.trace_id
    values.update(overrides)
    return TraceFeedbackCommand(**values)  # type: ignore[arg-type]


async def test_command_constructs_test_case_version_with_trace_evidence() -> None:
    trace_id = uuid4()
    command = _command(trace_id=trace_id, input_payload={"sanitized_input": True}, expected_output={"answer": 42})
    test_case, dataset = await _service(_trace(trace_id=trace_id)).create_feedback_case(command)

    assert test_case.case_id == "case-f"
    assert test_case.version == "v1"
    assert test_case.expected_output == {"answer": 42}
    assert test_case.evidence_refs == (trace_evidence_ref(trace_id),)
    evidence = test_case.evidence_refs[0]
    assert evidence.kind == "trace"
    assert evidence.identifier == str(trace_id)
    assert evidence.media_type is None
    assert evidence.schema_version is None
    assert evidence.metadata == {}
    assert dataset.version == "v2"


async def test_caller_supplied_input_payload_verbatim() -> None:
    trace = _trace()
    command = _command(trace, input_payload={"sanitized": True, "content": "caller-visible"})
    test_case, _ = await _service(trace).create_feedback_case(command)
    assert test_case.input_payload == {"sanitized": True, "content": "caller-visible"}


async def test_service_does_not_import_or_copy_trace_payload() -> None:
    source = Path(__file__).parents[2] / "app" / "services" / "evaluation" / "feedback.py"
    text = source.read_text(encoding="utf-8")
    assert "localagent" not in text
    assert "sidecar" not in text
    assert "TraceModel" not in text
    # The trace carries a payload that must never leak into the feedback case.
    trace = _trace()
    trace.input = {"secret": "production"}
    trace.output = {"secret": "production"}
    command = _command(trace, input_payload={"clean": True})
    test_case, _ = await _service(trace).create_feedback_case(command)
    assert test_case.input_payload == {"clean": True}


async def test_expected_output_none_stays_none() -> None:
    trace = _trace()
    command = _command(trace, expected_output=None)
    test_case, _ = await _service(trace).create_feedback_case(command)
    assert test_case.expected_output is None


async def test_criticality_is_not_auto_inferred() -> None:
    trace = _trace()
    command = _command(trace, metadata={"source": "feedback"})
    test_case, _ = await _service(trace).create_feedback_case(command)
    assert test_case.metadata == {"source": "feedback"}
    assert "critical" not in test_case.metadata
    assert "critical" not in test_case.tags


async def test_new_dataset_contains_new_case_ref_and_preserves_base() -> None:
    trace = _trace()
    base = (CaseVersionRef("case-a", "v1"),)
    command = _command(trace, base_case_refs=base)
    _, dataset = await _service(trace).create_feedback_case(command)
    assert set(dataset.case_version_refs) == {CaseVersionRef("case-a", "v1"), CaseVersionRef("case-f", "v1")}


async def test_parent_version_wired_and_versions_caller_supplied() -> None:
    trace = _trace()
    command = _command(trace, dataset_version="v2", parent_dataset_version="v1", case_version="v3")
    _, dataset = await _service(trace).create_feedback_case(command)
    assert dataset.version == "v2"
    assert dataset.parent_version == "v1"
    assert dataset.case_version_refs[0].version == "v3"


async def test_objects_are_immutable() -> None:
    trace = _trace()
    test_case, dataset = await _service(trace).create_feedback_case(_command(trace))
    assert isinstance(test_case.metadata, FrozenDict)
    with pytest.raises(dataclasses.FrozenInstanceError):
        test_case.name = "mutated"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        dataset.version = "mutated"  # type: ignore[misc]


async def test_child_span_failure_counts_even_if_trace_summary_success() -> None:
    trace = _trace(outcome=GenericOutcome.SUCCESS, span_outcome=GenericOutcome.TIMEOUT)
    test_case, _ = await _service(trace).create_feedback_case(_command(trace))
    assert test_case.evidence_refs[0].identifier == str(trace.trace_id)


async def test_non_failing_trace_is_rejected() -> None:
    trace = _trace(outcome=GenericOutcome.SUCCESS, span_outcome=GenericOutcome.SUCCESS)
    with pytest.raises(TraceFeedbackCandidateError):
        await _service(trace).create_feedback_case(_command(trace))


async def test_unknown_is_not_a_failing_candidate() -> None:
    trace = _trace(outcome=GenericOutcome.UNKNOWN, span_outcome=GenericOutcome.UNKNOWN)
    with pytest.raises(TraceFeedbackCandidateError):
        await _service(trace).create_feedback_case(_command(trace))


async def test_cross_project_trace_is_rejected() -> None:
    trace = _trace(project_id=PROJECT_A)
    command = _command(trace, project_id=PROJECT_B)
    with pytest.raises(TraceFeedbackCandidateError):
        await _service(trace).create_feedback_case(command)


async def test_missing_trace_is_rejected() -> None:
    with pytest.raises(TraceFeedbackCandidateError):
        await _service(None).create_feedback_case(_command())


def test_command_rejects_self_parent_and_duplicate_refs() -> None:
    with pytest.raises(ValueError):
        _command(dataset_version="v1", parent_dataset_version="v1")
    with pytest.raises(ValueError):
        _command(base_case_refs=(CaseVersionRef("case-a", "v1"), CaseVersionRef("case-a", "v1")))
    with pytest.raises(ValueError):
        _command(base_case_refs=(CaseVersionRef("case-f", "v1"),))


def test_service_requires_session_or_trace_service() -> None:
    with pytest.raises(TypeError):
        TraceFeedbackService()
