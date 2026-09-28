from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.evaluation.process_projections import (
    project_localagent_trace_export_v1,
    project_stateful_journal_memory,
)
from app.core.evaluation.process_trajectory import (
    Availability,
    EdgeRelation,
    MemoryEventType,
    ProcessKind,
    Provenance,
)
from app.core.evaluation.stateful_journal import FormationEvent, JournalEvents
from app.core.localagent.contract import (
    TRACE_EXPORT_CONTRACT_FINGERPRINT,
    TRACE_EXPORT_CONTRACT_IDENTITY,
    TRACE_EXPORT_CONTRACT_VERSION,
)
from app.core.localagent.entities import LocalAgentTraceEnvelopeInV1


def _envelope(*, span_id, operation, step_id=None, parent_span_id=None, attributes=None, seconds=0, status="OK"):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return LocalAgentTraceEnvelopeInV1(
        contract_identity=TRACE_EXPORT_CONTRACT_IDENTITY,
        contract_version=TRACE_EXPORT_CONTRACT_VERSION,
        contract_fingerprint=TRACE_EXPORT_CONTRACT_FINGERPRINT,
        run_id="runtime-1", trace_id="trace-1", span_id=span_id,
        parent_span_id=parent_span_id, step_id=step_id, operation=operation,
        component="agent", started_at=start, completed_at=start + timedelta(milliseconds=1),
        duration_ms=Decimal("1"), status=status,
        error_code=None if status == "OK" else "execution_error",
        attributes=attributes or {},
    )


def test_trace_export_projects_terminal_facts_partial_coverage_and_only_explicit_parent_edges():
    source = (
        _envelope(span_id="plan", operation="runtime.run", attributes={
            "plan_id": "plan-1", "plan_version": 2, "plan_fingerprint": "a" * 64,
            "planning_source": "deterministic", "step_count": 2, "shape": "2",
        }),
        _envelope(span_id="step-a", operation="runtime.step", step_id="step-a", parent_span_id="plan",
                  attributes={"preferred_agent": "agent-a", "execution_kind": "AGENT",
                              "output_policy": "INTERNAL", "dependency_count": 2,
                              "state": "SUCCEEDED", "result_char_count": 10}, seconds=1),
        _envelope(span_id="step-b", operation="runtime.step", step_id="step-b", parent_span_id="plan",
                  attributes={"preferred_agent": "agent-b", "execution_kind": "AGENT",
                              "dependency_count": 0, "state": "SUCCEEDED"}, seconds=20),
    )
    projected = project_localagent_trace_export_v1(
        source, evaluation_attempt_id="attempt-1", runtime_run_id="runtime-1")
    planning = next(item for item in projected.records if item.kind is ProcessKind.PLANNING)
    step_records = [item for item in projected.records if item.kind is ProcessKind.STEP]
    assert planning.payload.step_count == 2
    assert planning.payload.selected_step_ids is None
    assert planning.field_availability["selected_step_ids"] == "UNAVAILABLE"
    assert step_records[0].payload.phase.value == "COMPLETED"
    assert step_records[0].payload.dependency_count == 2
    assert step_records[0].payload.dependency_step_ids is None
    assert all(item.provenance is Provenance.RUNTIME for item in projected.records)
    assert all(item.completeness.value == "PARTIAL" for item in projected.coverage
               if item.availability is Availability.PRESENT)
    assert all(edge.relation is EdgeRelation.CONTAINS for edge in projected.edges)
    assert len(projected.edges) == 2


def test_trace_terminal_failures_never_invent_started_phase():
    failed = _envelope(span_id="failed", operation="runtime.step", step_id="step-f",
                       attributes={"state": "FAILED"}, status="ERROR")
    projected = project_localagent_trace_export_v1(
        (failed,), evaluation_attempt_id="attempt-1", runtime_run_id="runtime-1")
    step = next(item for item in projected.records if item.kind is ProcessKind.STEP)
    assert step.payload.phase.value == "FAILED"


def test_canonical_no_source_projection_is_explicitly_unavailable():
    projected = project_localagent_trace_export_v1(
        (), evaluation_attempt_id="attempt-1", runtime_run_id="runtime-1")
    assert all(item.availability is Availability.UNAVAILABLE for item in projected.coverage)
    assert all(item.reason_code == "SOURCE_NOT_AVAILABLE" for item in projected.coverage)


def test_stateful_projection_uses_only_typed_safe_fields_and_specialized_provenance():
    events = JournalEvents("harness-run", (
        FormationEvent("harness-run", "event-1", "HYBRID", "SUCCEEDED", None,
                       1, 1, 0, 1, 0, 0, "NONE", sequence=7),
    ), (), ())
    projected = project_stateful_journal_memory(
        events, evaluation_attempt_id="attempt-1", expected_runtime_run_id="harness-run")
    record = projected.records[0]
    assert record.provenance is Provenance.FIXTURE
    assert record.payload.event_type is MemoryEventType.FORMATION
    assert record.payload.event_id == "event-1" and record.payload.sequence == 7
    assert "memory_text" not in type(record.payload).model_fields
    assert projected.coverage[0].completeness.value == "PARTIAL"


def test_generic_trace_attrs_and_stateful_run_mismatch_cannot_enter_projection():
    import pytest

    with pytest.raises(TypeError, match="strict envelope DTO"):
        project_localagent_trace_export_v1(({"operation": "runtime.step"},),
                                           evaluation_attempt_id="attempt-1", runtime_run_id="runtime-1")
    event = FormationEvent("other-run", "event", "HYBRID", "SUCCEEDED", None,
                           1, 1, 0, 1, 0, 0, "NONE", sequence=1)
    with pytest.raises(ValueError, match="run binding"):
        project_stateful_journal_memory(JournalEvents("expected-run", (event,), (), ()),
            evaluation_attempt_id="attempt-1", expected_runtime_run_id="expected-run")


def test_selected_retrieval_dto_has_no_used_citation_authority_and_tool_phase_has_no_commit_inference():
    from app.core.evaluation.process_trajectory import RetrievalEvidencePayloadV1, ToolEvidencePayloadV1, ToolPhase

    retrieval = RetrievalEvidencePayloadV1.model_validate({
        "retrieval_id": "retrieval-1", "status": "OK",
        "selected": ({"document_id": "doc-1", "chunk_id": "chunk-1", "selection_rank": 1},),
    })
    completed = ToolEvidencePayloadV1(phase=ToolPhase.COMPLETED, tool_name="lookup")
    assert retrieval.selected is not None and "used_citations" not in type(retrieval).model_fields
    assert completed.side_effect_state is None
