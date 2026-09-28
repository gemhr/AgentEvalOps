"""Strict safe projections for existing LocalAgent and isolated journal sources."""

# ruff: noqa: D101, D102, D415

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.core.evaluation.process_trajectory import (
    Availability,
    CausalityEdgeV1,
    Completeness,
    CoverageV1,
    EdgeRelation,
    ProcessEvidenceV1,
    ProcessKind,
    Provenance,
    Sensitivity,
    StepPhase,
    build_process_evidence,
)
from app.core.evaluation.stateful_journal import JournalEvents
from app.core.localagent.contract import TRACE_EXPORT_CONTRACT_IDENTITY
from app.core.localagent.entities import LocalAgentTraceEnvelopeInV1
from app.core.localagent.validation import validate_contract, validate_envelope_semantics

TRACE_PRODUCER = "localagent.runtime.trace_export.v1"
STATEFUL_PRODUCER = "stateful.isolated_harness.journal.v1"


@dataclass(frozen=True, slots=True)
class SafeProjectionResult:
    records: tuple[ProcessEvidenceV1, ...]
    coverage: tuple[CoverageV1, ...]
    edges: tuple[CausalityEdgeV1, ...] = ()
    diagnostics: tuple[str, ...] = ()


def project_localagent_trace_export_v1(
    envelopes: Iterable[LocalAgentTraceEnvelopeInV1],
    *,
    evaluation_attempt_id: str,
    runtime_run_id: str,
) -> SafeProjectionResult:
    """Project only validated v1 Planning/Step scalar facts; never reads Trace rows."""
    source = tuple(envelopes)
    by_span: dict[str, LocalAgentTraceEnvelopeInV1] = {}
    for envelope in source:
        if not isinstance(envelope, LocalAgentTraceEnvelopeInV1):
            raise TypeError("TraceExport projection requires strict envelope DTOs")
        validate_contract(envelope)
        validate_envelope_semantics(envelope)
        if envelope.run_id != runtime_run_id:
            raise ValueError("TraceExport runtime run binding mismatch")
        if envelope.span_id in by_span and by_span[envelope.span_id] != envelope:
            raise ValueError("TraceExport span identity conflict")
        by_span[envelope.span_id] = envelope

    records: list[ProcessEvidenceV1] = []
    span_record_ids: dict[str, str] = {}
    diagnostics: list[str] = []
    for envelope in sorted(by_span.values(), key=lambda item: (item.trace_id, item.span_id)):
        attrs = envelope.attributes
        common = dict(
            evaluation_attempt_id=evaluation_attempt_id,
            schema_version="stage11.wp4.v1",
            producer_id=TRACE_PRODUCER,
            provenance=Provenance.RUNTIME,
            runtime_run_id=envelope.run_id,
            source_stream_id=envelope.trace_id,
            source_event_id=envelope.span_id,
            source_schema_ref=TRACE_EXPORT_CONTRACT_IDENTITY + ".v1",
            sensitivity=Sensitivity.SAFE_METADATA,
        )
        if envelope.operation in {"runtime.run", "runtime.planning"}:
            planning = {
                key: attrs[key]
                for key in (
                    "plan_id", "plan_version", "plan_fingerprint", "planning_source", "shape",
                    "schema_version", "planner_model_invoked", "specialist_count", "synthesis_required", "step_count",
                )
                if key in attrs
            }
            if "compiled_shape" in attrs:
                planning["shape"] = attrs["compiled_shape"]
            unavailable = {
                key: Availability.UNAVAILABLE.value
                for key in (
                    "plan_id", "plan_version", "plan_fingerprint", "planning_source", "schema_version",
                    "planner_model_invoked", "shape", "specialist_count", "synthesis_required", "step_count",
                    "selected_step_ids", "resolved_performer_refs", "dependency_step_ids", "replan_of",
                ) if key not in planning
            }
            record = build_process_evidence(
                **common, kind=ProcessKind.PLANNING,
                projection_role="planning." + envelope.operation,
                payload=planning, field_availability=unavailable,
            )
            records.append(record)
            span_record_ids[envelope.span_id] = record.evidence_id
        elif envelope.operation == "runtime.step":
            phase = {
                "OK": StepPhase.COMPLETED,
                "ERROR": StepPhase.FAILED,
                "TIMED_OUT": StepPhase.FAILED,
                "CANCELLED": StepPhase.CANCELLED,
            }[envelope.status]
            payload = {
                "step_id": envelope.step_id,
                "phase": phase,
                "result_status": envelope.status,
                "error_code": envelope.error_code,
                "parent_step_id": (
                    by_span[envelope.parent_span_id].step_id
                    if envelope.parent_span_id in by_span and by_span[envelope.parent_span_id].step_id is not None
                    else None
                ),
                "preferred_agent": attrs.get("preferred_agent"),
                "execution_kind": attrs.get("execution_kind"),
                "output_policy": attrs.get("output_policy"),
                "dependency_count": attrs.get("dependency_count"),
                "state": attrs.get("state"),
                "result_char_count": attrs.get("result_char_count"),
            }
            payload = {key: value for key, value in payload.items() if value is not None}
            availability = {
                key: Availability.UNAVAILABLE.value
                for key in (
                    "parent_step_id", "task_type", "preferred_agent", "actual_performer_ref",
                    "dependency_step_ids", "safe_input_ref", "safe_output_ref", "result_status", "error_code",
                    "execution_kind", "output_policy", "dependency_count", "result_char_count", "state",
                ) if key not in payload
            }
            record = build_process_evidence(
                **common, kind=ProcessKind.STEP, projection_role="step.terminal",
                payload=payload, field_availability=availability, step_id=envelope.step_id,
            )
            records.append(record)
            span_record_ids[envelope.span_id] = record.evidence_id
        else:
            diagnostics.append("UNSUPPORTED_OPERATION_IGNORED")

    edges = tuple(
        CausalityEdgeV1(span_record_ids[parent.parent_span_id], span_record_ids[parent.span_id], EdgeRelation.CONTAINS)
        for parent in by_span.values()
        if parent.parent_span_id in span_record_ids and parent.span_id in span_record_ids
        and parent.parent_span_id != parent.span_id
    )
    result_coverage = []
    for kind in (ProcessKind.PLANNING, ProcessKind.STEP):
        present = any(record.kind is kind for record in records)
        result_coverage.append(CoverageV1(
            kind=kind, scope="trace_export_v1",
            availability=Availability.PRESENT if present else Availability.UNAVAILABLE,
            completeness=Completeness.PARTIAL if present else None,
            reason_code="SOURCE_SCHEMA_PARTIAL" if present else "SOURCE_NOT_AVAILABLE",
            supporting_source_refs=(TRACE_PRODUCER,) if present else (),
        ))
    return SafeProjectionResult(tuple(records), tuple(result_coverage), edges, tuple(sorted(diagnostics)))


def project_stateful_journal_memory(
    events: JournalEvents,
    *,
    evaluation_attempt_id: str,
    expected_runtime_run_id: str,
) -> SafeProjectionResult:
    """Project strict, isolated journal DTO facts; provenance remains FIXTURE."""
    if not isinstance(events, JournalEvents) or events.run_id != expected_runtime_run_id:
        raise ValueError("stateful journal run binding mismatch")
    records: list[ProcessEvidenceV1] = []
    if any(
        event.run_id != events.run_id
        for group in (events.formation, events.lifecycle, events.retrieval)
        for event in group
    ):
        raise ValueError("stateful journal event run binding mismatch")
    for event_type, group in (
        ("FORMATION", events.formation), ("LIFECYCLE", events.lifecycle), ("RETRIEVAL", events.retrieval),
    ):
        for event in group:
            data: dict[str, object] = {
                "event_type": event_type,
                "event_id": event.event_id,
                "sequence": event.sequence,
                "run_id": event.run_id,
            }
            if event_type == "FORMATION":
                data.update({key: getattr(event, key) for key in (
                    "status", "safe_error_code", "formation_method", "proposed_count", "accepted_count",
                    "ignored_count", "persisted_count", "reused_count", "failed_count", "candidate_outcomes",
                )})
            elif event_type == "LIFECYCLE":
                data.update({key: getattr(event, key) for key in (
                    "outcome", "safe_error_code", "memory_type", "operation", "affected_count",
                    "winner_memory_id", "new_memory_id", "candidate_outcome", "affected_transitions",
                )})
            else:
                data.update({key: getattr(event, key) for key in (
                    "status", "safe_error_code", "retrieval_method", "ranking_method", "candidate_count",
                    "eligible_count", "selected_count", "context_record_count", "malformed_count",
                    "omitted_count", "registered_selected_count", "open_selected_count", "planning_injected",
                    "direct_entry_supplied",
                )})
            data = {key: value for key, value in data.items() if value is not None}
            records.append(build_process_evidence(
                evaluation_attempt_id=evaluation_attempt_id,
                schema_version="stage11.wp4.v1", kind=ProcessKind.MEMORY,
                producer_id=STATEFUL_PRODUCER, provenance=Provenance.FIXTURE,
                runtime_run_id=event.run_id, source_stream_id="stateful.journal",
                source_event_id=event.event_id, projection_role="memory." + event_type.lower(),
                source_schema_ref="stateful.memory_journal.v1", sensitivity=Sensitivity.SAFE_METADATA,
                payload=data, source_sequence=event.sequence,
            ))
    present = bool(records)
    coverage = (CoverageV1(
        kind=ProcessKind.MEMORY, scope="isolated_journal",
        availability=Availability.PRESENT if present else Availability.UNAVAILABLE,
        completeness=Completeness.PARTIAL if present else None,
        reason_code="SOURCE_SCHEMA_PARTIAL" if present else "SOURCE_NOT_AVAILABLE",
        supporting_source_refs=(STATEFUL_PRODUCER,) if present else (),
    ),)
    return SafeProjectionResult(tuple(records), coverage)
