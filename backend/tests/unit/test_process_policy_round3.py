import pytest
from dataclasses import replace
from types import SimpleNamespace

from app.core.evaluation.evidence_body_policy import (
    EvidenceBodyMode,
    EvidenceBodyPolicyV1,
    parse_evidence_body_policy,
)
from app.core.evaluation.expected_process import parse_expected_process
from app.core.evaluation.execution import OutcomeKind
from app.core.evaluation.generation_evidence import FinalAnswerEvidenceV1
from app.core.evaluation.process_trajectory import (
    Availability, Completeness, CoverageV1, ProcessKind, Provenance, Sensitivity,
    build_process_evidence, build_process_trajectory, thin_process_trajectory_ref,
)
from app.core.evaluation.references import CaseVersionRef
from app.services.evaluation.comparison import _valid_process_result_support


def test_body_policy_defaults_to_metadata_only_and_requires_frozen_ref_for_approval():
    assert parse_evidence_body_policy({"metadata": {}}).mode is EvidenceBodyMode.METADATA_ONLY
    with pytest.raises(ValueError, match="invalid frozen"):
        parse_evidence_body_policy({"metadata": {"evidence_body_policy": {
            "schema_version": "evidence_body_policy.v1",
            "mode": "APPROVED_SAFE_SNAPSHOT",
            "approved_fields": ["final_answer.body"],
        }}})
    policy = EvidenceBodyPolicyV1.model_validate({
        "schema_version": "evidence_body_policy.v1",
        "mode": "APPROVED_SAFE_SNAPSHOT",
        "policy_ref": {"kind": "evidence_body_policy", "opaque_value": "v1-approved"},
        "approved_fields": ("final_answer.body",),
    })
    assert policy.identity_ref == "evidence_body_policy:v1-approved"


def test_body_policy_rejects_unknown_or_wildcard_fields():
    with pytest.raises(ValueError):
        EvidenceBodyPolicyV1.model_validate({
            "schema_version": "evidence_body_policy.v1",
            "mode": "APPROVED_SAFE_SNAPSHOT",
            "policy_ref": {"kind": "evidence_body_policy", "opaque_value": "v1"},
            "approved_fields": ("*",),
        })


def test_expected_process_preserves_absence_and_does_not_mix_with_actual():
    assert parse_expected_process({}) is None
    expected = parse_expected_process({"expected_process": {"schema_version": "expected_process.v1"}})
    assert expected is not None
    assert expected.expected_tool_calls is None
    assert expected.relevant_document_ids is None


def test_process_result_support_rejects_policy_mismatch():
    record = build_process_evidence(
        evaluation_attempt_id="attempt-1", schema_version="stage11.wp4.v1", kind=ProcessKind.TOOL,
        producer_id="fixture", provenance=Provenance.FIXTURE, runtime_run_id="runtime-1",
        source_stream_id="stream", source_event_id="event", projection_role="tool.completed",
        source_schema_ref="fixture.v1", sensitivity=Sensitivity.SAFE_METADATA,
        payload={"phase": "COMPLETED", "tool_name": "lookup"},
    )
    case_ref = CaseVersionRef("case-1", "v1")
    trajectory = build_process_trajectory(
        schema_version="stage11.wp4.v1", trajectory_id="trajectory://attempt-1",
        project_id="project-1", evaluation_run_id="run-1", evaluation_attempt_id="attempt-1",
        dataset_id="dataset-1", case_ref=case_ref, execution_request_id="request-1",
        execution_target_ref={"target_id": "target"}, frozen_subject_ref={"subject": "s1"},
        sealed=True, execution_partial=False, body_policy_ref="evidence_body_policy:policy-a",
        source_manifests=(), coverage=(CoverageV1(ProcessKind.TOOL, "attempt", Availability.PRESENT,
        Completeness.COMPLETE),), records=(record,), edges=(),
    )
    reference = trajectory.as_evidence_ref()
    thin = thin_process_trajectory_ref(trajectory)
    requirements = {"schema_version": "process-evidence-requirements.v1",
        "trajectory_schema_version": "stage11.wp4.v1", "requirements": [{
            "kind": "TOOL", "fields": ["tool_name"], "accepted_provenance": ["FIXTURE"],
            "allow_partial": False, "allow_not_applicable": False,
        }]}
    spec = {"config_snapshot": {"process_evidence_requirements": requirements}}
    attempt = SimpleNamespace(
        execution_outcome_kind=OutcomeKind.SUCCESS, outcome_evidence_refs=(reference,),
        attempt_id="attempt-1", project_id="project-1", execution_request=SimpleNamespace(request_id="request-1"),
        case_ref=case_ref,
    )
    run = SimpleNamespace(run_id="run-1", project_id="project-1", dataset_snapshot={"dataset_id": "dataset-1"},
                          subject_ref={"subject": "s1"})
    result = SimpleNamespace(
        evidence_refs=(thin,), run_id="run-1", dataset_id="dataset-1", case_id="case-1",
        case_version="v1", execution_request_id="request-1",
    )
    assert _valid_process_result_support(spec, result, attempt, run)
    forged = replace(thin, metadata={**dict(thin.metadata), "policy_ref": "evidence_body_policy:policy-b"})
    mismatched_result = SimpleNamespace(**{**vars(result), "evidence_refs": (forged,)})
    assert not _valid_process_result_support(spec, mismatched_result, attempt, run)


def test_final_answer_digest_corruption_and_forbidden_policy_paths_fail_closed():
    with pytest.raises(ValueError):
        FinalAnswerEvidenceV1(
            schema_version="final-answer-evidence.v1", evidence_id="final-answer://runtime-1",
            run_id="runtime-1", attempt_id="runtime-1", media_type="text/plain; charset=utf-8",
            content_sha256="0" * 64, content="body",
        )
    with pytest.raises(ValueError):
        EvidenceBodyPolicyV1.model_validate({
            "schema_version": "evidence_body_policy.v1", "mode": "APPROVED_SAFE_SNAPSHOT",
            "policy_ref": {"kind": "evidence_body_policy", "opaque_value": "v1"},
            "approved_fields": ("authorization",),
        })
