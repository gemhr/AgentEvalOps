from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.core.evaluation.references import CaseVersionRef
from app.core.evaluation.process_trajectory import (
    Availability,
    CausalityEdgeV1,
    Completeness,
    CoverageV1,
    EdgeRelation,
    ProcessEvidenceRequirementV1,
    ProcessKind,
    ProcessTrajectoryV1,
    Provenance,
    Sensitivity,
    SourceManifestV1,
    build_process_evidence,
    build_process_trajectory,
    merge_process_evidence,
    parse_process_trajectory,
    thin_process_trajectory_ref,
    validate_process_requirements,
)


def _evidence(attempt: str = "attempt-1", *, event: str = "event-1", status: str = "SUCCESS"):
    return build_process_evidence(
        evaluation_attempt_id=attempt,
        schema_version="stage11.wp4.v1",
        kind=ProcessKind.TOOL,
        producer_id="fixture.tool",
        provenance=Provenance.FIXTURE,
        runtime_run_id="runtime-1",
        source_stream_id="stream-1",
        source_event_id=event,
        projection_role="tool.completed",
        source_schema_ref="fixture.v1",
        sensitivity=Sensitivity.SAFE_METADATA,
        payload={"phase": "COMPLETED", "tool_name": "lookup", "result_status": status},
    )


def _trajectory(records=(), edges=()):
    manifest = SourceManifestV1(
        producer_id="fixture.tool", runtime_run_id="runtime-1", source_stream_id="stream-1",
        source_schema_ref="fixture.v1", source_terminal_confirmed=True, availability=Availability.PRESENT,
    )
    coverage = CoverageV1(
        kind=ProcessKind.TOOL, scope="attempt", availability=Availability.PRESENT,
        completeness=Completeness.COMPLETE, supporting_source_refs=("fixture.tool",),
    )
    return build_process_trajectory(
        schema_version="stage11.wp4.v1", trajectory_id="trajectory://attempt-1", project_id="project-1",
        evaluation_run_id="run-1", evaluation_attempt_id="attempt-1", dataset_id="dataset-1",
        case_ref=CaseVersionRef("case-1", "v1"), execution_request_id="request-1",
        execution_target_ref={"target_id": "fixture"}, frozen_subject_ref={"subject": "s1"},
        sealed=True, execution_partial=False, body_policy_ref=None, source_manifests=(manifest,),
        coverage=(coverage,), records=tuple(records), edges=tuple(edges),
    )


def test_evidence_identity_replay_conflict_and_occurrence_are_stable():
    first = _evidence()
    assert first.evidence_id == _evidence().evidence_id
    assert len(merge_process_evidence((first, first))) == 1
    with pytest.raises(ValueError, match="IDENTITY_CONFLICT"):
        merge_process_evidence((first, _evidence(status="FAILED")))
    assert _evidence(event="event-2").evidence_id != first.evidence_id
    assert _evidence(attempt="attempt-2").evidence_id != first.evidence_id


def test_trajectory_digest_is_order_independent_but_preserves_record_occurrences():
    one, two = _evidence(event="event-1"), _evidence(event="event-2")
    assert _trajectory((one, two)).content_sha256 == _trajectory((two, one)).content_sha256
    assert len(_trajectory((one, two)).records) == 2
    assert parse_process_trajectory(_trajectory((one, two)).as_evidence_ref()).content_sha256 == _trajectory((two, one)).content_sha256


def test_graph_rejects_cycles_unresolved_targets_and_cross_attempt_identity():
    one, two = _evidence(event="event-1"), _evidence(event="event-2")
    with pytest.raises(ValueError, match="causality cycle"):
        _trajectory((one, two), (
            CausalityEdgeV1(one.evidence_id, two.evidence_id, EdgeRelation.DEPENDS_ON),
            CausalityEdgeV1(two.evidence_id, one.evidence_id, EdgeRelation.DEPENDS_ON),
        ))
    unresolved = _trajectory((one,), (CausalityEdgeV1(one.evidence_id, "pe://missing", EdgeRelation.CONSUMES),))
    assert unresolved.edges == ()
    assert unresolved.coverage[0].completeness is Completeness.PARTIAL
    assert unresolved.coverage[0].reason_code == "UNRESOLVED_CAUSALITY_TARGET"
    with pytest.raises(ValueError, match="canonical Attempt"):
        _trajectory((_evidence(attempt="attempt-2"),))


def test_payload_and_objects_are_deeply_immutable_and_extra_fields_rejected():
    with pytest.raises(ValueError, match="unsupported fields"):
        build_process_evidence(
            evaluation_attempt_id="attempt-1", schema_version="stage11.wp4.v1", kind=ProcessKind.TOOL,
            producer_id="p", provenance=Provenance.RUNTIME, runtime_run_id="r", source_stream_id="s",
            source_event_id="e", projection_role="tool.completed", source_schema_ref="v1",
            sensitivity=Sensitivity.SAFE_METADATA, payload={"untrusted": "field"},
        )
    record = _evidence()
    with pytest.raises((TypeError, FrozenInstanceError)):
        record.payload["tool_name"] = "changed"


def test_complete_empty_scope_is_valid_for_zero_assertion_but_partial_is_not():
    trajectory = _trajectory()
    requirement = ProcessEvidenceRequirementV1(
        kind=ProcessKind.TOOL, fields=(), accepted_provenance=(Provenance.FIXTURE,),
    )
    assert validate_process_requirements(trajectory, (requirement,)) is None
    partial = build_process_trajectory(**{
        field: getattr(trajectory, field)
        for field in trajectory.__dataclass_fields__
        if field not in {"content_sha256", "coverage"}
    } | {
        "content_sha256": "",
        "coverage": (CoverageV1(kind=ProcessKind.TOOL, scope="attempt", availability=Availability.PRESENT,
                                 completeness=Completeness.PARTIAL, reason_code="SOURCE_GAP"),),
    })
    assert validate_process_requirements(partial, (requirement,)) == "REQUIRED_PROCESS_EVIDENCE_INVALID"
    allow_partial = ProcessEvidenceRequirementV1(
        kind=ProcessKind.TOOL, fields=(), accepted_provenance=(Provenance.FIXTURE,), allow_partial=True,
    )
    assert validate_process_requirements(partial, (allow_partial,)) == "REQUIRED_PROCESS_EVIDENCE_INVALID"


def test_explicit_not_applicable_requirement_is_accepted():
    complete = _trajectory()
    unavailable = build_process_trajectory(**{
        field: getattr(complete, field)
        for field in complete.__dataclass_fields__
        if field not in {"content_sha256", "coverage"}
    } | {
        "coverage": (CoverageV1(kind=ProcessKind.TOOL, scope="attempt", availability=Availability.NOT_APPLICABLE,
                                 reason_code="NOT_APPLICABLE"),),
    })
    requirement = ProcessEvidenceRequirementV1(
        kind=ProcessKind.TOOL, fields=(), accepted_provenance=(Provenance.FIXTURE,),
        allow_not_applicable=True,
    )
    assert validate_process_requirements(unavailable, (requirement,)) is None
    assert validate_process_requirements(unavailable, (
        ProcessEvidenceRequirementV1(kind=ProcessKind.TOOL, fields=(), accepted_provenance=(Provenance.FIXTURE,)),
    )) == "REQUIRED_PROCESS_EVIDENCE_UNAVAILABLE"


def test_required_fields_and_provenance_fail_closed_and_result_ref_is_thin():
    record = _evidence()
    trajectory = _trajectory((record,))
    wrong_provenance = ProcessEvidenceRequirementV1(
        kind=ProcessKind.TOOL, fields=("tool_name",), accepted_provenance=(Provenance.RUNTIME,),
    )
    missing_field = ProcessEvidenceRequirementV1(
        kind=ProcessKind.TOOL, fields=("operation_id",), accepted_provenance=(Provenance.FIXTURE,),
    )
    assert validate_process_requirements(trajectory, (wrong_provenance,)) == "REQUIRED_PROCESS_EVIDENCE_INVALID"
    assert validate_process_requirements(trajectory, (missing_field,)) == "REQUIRED_PROCESS_EVIDENCE_INVALID"
    support = thin_process_trajectory_ref(trajectory)
    assert set(support.metadata) == {"content_sha256", "evaluation_attempt_id", "accepted_provenance", "availability", "policy_ref"}
    assert "payload" not in support.metadata


def test_wrong_trajectory_digest_is_rejected():
    reference = _trajectory((_evidence(),)).as_evidence_ref()
    payload = dict(reference.metadata["payload"])
    payload["content_sha256"] = "0" * 64
    forged = type(reference)(reference.kind, reference.identifier, schema_version=reference.schema_version, metadata={"payload": payload})
    with pytest.raises(ValueError, match="digest mismatch"):
        parse_process_trajectory(forged)


def test_empty_record_or_trajectory_digest_is_rejected_on_parse():
    reference = _trajectory((_evidence(),)).as_evidence_ref()
    payload = dict(reference.metadata["payload"])
    payload["content_sha256"] = ""
    forged = type(reference)(reference.kind, reference.identifier, schema_version=reference.schema_version,
                             metadata={"payload": payload})
    with pytest.raises(ValueError, match="ProcessTrajectory content digest is required"):
        parse_process_trajectory(forged)

    payload = dict(reference.metadata["payload"])
    record = dict(payload["records"][0])
    record["content_sha256"] = ""
    payload["records"] = [record]
    forged = type(reference)(reference.kind, reference.identifier, schema_version=reference.schema_version,
                             metadata={"payload": payload})
    with pytest.raises(ValueError, match="ProcessEvidence content digest is required"):
        parse_process_trajectory(forged)


def test_trajectory_inline_size_limit_downgrades_coverage():
    large = build_process_evidence(
        evaluation_attempt_id="attempt-1", schema_version="stage11.wp4.v1", kind=ProcessKind.STEP,
        producer_id="fixture.step", provenance=Provenance.FIXTURE, runtime_run_id="runtime-1",
        source_stream_id="stream-1", source_event_id="step-1", projection_role="step.completed",
        source_schema_ref="fixture.v1", sensitivity=Sensitivity.SAFE_METADATA,
        payload={"state": "x" * (1024 * 1024)},
    )
    trajectory = _trajectory((large,))
    assert trajectory.records == ()
    assert trajectory.coverage[0].completeness is Completeness.PARTIAL
    assert trajectory.coverage[0].reason_code == "SIZE_LIMIT"
