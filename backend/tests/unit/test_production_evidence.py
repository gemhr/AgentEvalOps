"""WP6 evidence contracts reject unsupported production conclusions."""

# ruff: noqa: D101, D102, D103

from datetime import datetime, timezone
from dataclasses import replace
import json

import pytest
from pydantic import ValidationError

from app.core.evaluation.production_evidence import (
    ClaimStatus,
    ClaimScopeV1,
    ClaimType,
    EnvironmentIdentityV1,
    EnvironmentType,
    EvidenceBasis,
    EvidenceRequirementV1,
    EvidenceStrength,
    EvidenceType,
    EvidencePredicate,
    EvidenceV1,
    FaultEvidenceV1,
    ProductionClaimV1,
    ProductionEvidenceSubjectV1,
    ProcessStatus,
    SafeAggregateImportV1,
    SourceAuthenticity,
    TestEvidenceV1 as ReceiptModel,
    ProductionEvidenceBundleV1,
    ReportStatus,
    EvidenceAvailabilityV1,
    Availability,
    SyntheticLoadEvidenceV1,
    WorktreeState,
    semantic_digest,
)
from app.services.evaluation.production_evidence import (
    ProductionEvidenceService,
    ValidatedSourceReceipt,
    import_safe_aggregate,
    historical_summary_evidence,
    minimal_claim_catalog,
)
from scripts.ci.test_evidence import capture_junit_receipt

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


def _evidence(
    evidence_type: EvidenceType = EvidenceType.TEST_EVIDENCE,
    *,
    synthetic: bool = True,
    env_ref: str = "test-env",
    subject_ref: str = "subject:v2",
    auth: SourceAuthenticity = SourceAuthenticity.DECLARED_SOURCE,
) -> EvidenceV1:
    test_payload = ReceiptModel(
        command_argv=("uv", "run", "pytest", "tests/unit/test_example.py"), cwd_alias="backend",
        scope="unit", body_assertions="1 passed", passed=1, failed=0, errors=0, process_exit_code=0,
        process_status=ProcessStatus.SUCCEEDED, raw_result_digest="a" * 64,
    )
    production = evidence_type is EvidenceType.PRODUCTION_EVIDENCE
    evidence_payload = _safe_aggregate(synthetic=synthetic if production else False)
    return EvidenceV1(
        evidence_id="evidence-1", evidence_version="v1", evidence_type=evidence_type,
        basis=EvidenceBasis.TESTED, strength=EvidenceStrength.DETERMINISTIC_TEST, synthetic=synthetic,
        subject_ref=subject_ref, environment_ref=env_ref, source_kind="safe-import",
        source_ref="export-1" if production else "ci:run-1",
        source_artifact_digest="sha256:export" if production else "b" * 64,
        source_authenticity=auth, captured_at=NOW,
        payload_kind="SAFE_AGGREGATE" if production else "TEST",
        test_payload=None if production else test_payload,
        safe_payload=evidence_payload if production else None,
    )


def _controlled_test_evidence(evidence: EvidenceV1, *, worktree_state: WorktreeState = WorktreeState.UNKNOWN,
                              source_revision: str | None = None,
                              working_tree_content_digest: str | None = None,
                              failed: bool = False) -> tuple[EvidenceV1, object]:
    xml = (b"<testsuite tests='1' failures='1'><testcase classname='suite' name='test_x'>"
           b"<failure message='call failed'/></testcase></testsuite>" if failed else
           b"<testsuite tests='1'><testcase classname='suite' name='test_x'/></testsuite>")
    captured = capture_junit_receipt(
        xml, command_argv=("uv", "run", "pytest", "tests/test_x.py"), cwd_alias="backend", scope="unit",
        process_exit_code=1 if failed else 0, started_at=NOW, finished_at=NOW,
        source_revision=source_revision, worktree_state=worktree_state,
        working_tree_content_digest=working_tree_content_digest,
    )
    payload = evidence.model_dump()
    payload.update({"test_payload": captured.receipt, "semantic_digest": ""})
    return EvidenceV1.model_validate(payload), captured
def _safe_aggregate(**overrides) -> SafeAggregateImportV1:
    payload = {
        "contract_version": "stage11.wp6.v1",
        "environment": {"type": "PRODUCTION", "id": "prod-1", "identity_source_ref": "env-receipt"},
        "deployment": {"id": "deploy-1", "artifact_digest": "sha256:image"},
        "subject": {
            "source_revision": "rev2", "working_tree_content_digest": "tree2",
            "runtime_version": "v2", "eval_version": "eval-v2", "agent_version": "agent-v2",
            "workflow_version": "workflow-v2", "toolset_identity": "tools-v1",
            "provider_binding_identity": "provider-a", "actual_model": "model-a",
        },
        "source": {"kind": "upload", "export_id": "export-1", "artifact_digest": "sha256:export",
                   "verification_ref": "collector-receipt"},
        "window_start": "2026-10-01T00:00:00Z", "window_end": "2026-10-01T01:00:00Z",
        "population_ref": "population-v1", "expected_count": 2, "success_count": 2,
        "failure_count": 0, "timeout_count": 0, "cancelled_count": 0,
        "outcome_unknown_count": 0, "pending_count": 0,
        "latency": {"definition_ref": "metric-def-v1", "unit": "ms", "method": "nearest-rank",
                    "sample_count": 2, "coverage": 1.0, "p50": 10.0, "p95": 10.0, "p99": 10.0},
        "cost_summary": {"currency": "USD", "actual_amount": 1.0, "receipt_digest": "sha256:cost",
                         "coverage": 1.0, "count": 2},
        "metric_report_digest": "sha256:metric", "synthetic": False,
    }
    payload.update(overrides)
    return import_safe_aggregate(json.dumps(payload))


def _production_claim() -> ProductionClaimV1:
    return ProductionClaimV1(
        claim_id="latency-prod", claim_version="v1", claim_type=ClaimType.LATENCY,
        subject_ref="subject:v2", scope=ClaimScopeV1(
            subject_ref="subject:v2", environment_ref="prod-1", version_ref="runtime:v2",
            evidence_scope="p95 within approved SLO",
        ), requirement="p95 within approved SLO",
        required_evidence_types=(EvidenceType.PRODUCTION_EVIDENCE,),
        required_evidence_requirements=(EvidenceRequirementV1(
            evidence_type=EvidenceType.PRODUCTION_EVIDENCE,
            predicate=EvidencePredicate.PERFORMANCE_MEASURED,
            required_authenticity=SourceAuthenticity.VERIFIED_SOURCE,
            required_environment_type=EnvironmentType.PRODUCTION,
            required_subject_fields=("deployment_id", "artifact_digest", "runtime_version", "actual_model_binding"),
            expected_subject_identity=(
                ("runtime_version", "v2"), ("deployment_id", "deploy-1"),
                ("source_revision", "rev2"), ("working_tree_content_digest", "tree2"),
                ("provider_identity", "provider-a"), ("actual_model_binding", "model-a"),
                ("artifact_digest", "sha256:image"), ("eval_version", "eval-v2"),
            ),
            metric_definition_digest="metric-def-v1", population_ref="population-v1",
            scenario_ref="scenario-v1", policy_ref="policy-v1",
        ),), status=ClaimStatus.UNVERIFIED,
    )


def test_test_synthetic_old_or_unknown_evidence_never_supports_production_claim() -> None:
    service = ProductionEvidenceService()
    claim = _production_claim()
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:v2",
        runtime_version="v2", eval_version="eval-v2", source_revision="rev2",
        working_tree_content_digest="tree2", worktree_state=WorktreeState.DIRTY,
        deployment_id="deploy-1", artifact_digest="sha256:image",
        provider_identity="provider-a", actual_model_binding="model-a", environment_ref="prod-1",
    )
    env = EnvironmentIdentityV1(
        environment_type=EnvironmentType.PRODUCTION, environment_id="prod-1", identity_source_ref="receipt-1",
    )
    variants = (
        _evidence(),
        _evidence(EvidenceType.INTEGRATION_EVIDENCE),
        _evidence(EvidenceType.PRODUCTION_EVIDENCE, synthetic=True),
        _evidence(EvidenceType.PRODUCTION_EVIDENCE, env_ref="other-prod", synthetic=False),
        _evidence(EvidenceType.PRODUCTION_EVIDENCE, subject_ref="subject:v1", synthetic=False),
        _evidence(EvidenceType.PRODUCTION_EVIDENCE, synthetic=False, auth=SourceAuthenticity.UNKNOWN_SOURCE),
    )
    for item in variants:
        bound = service.bind_claim(claim, subject, env, (item,))
        assert bound.status is ClaimStatus.UNVERIFIED
        assert not bound.observed_evidence_refs

    throughput_payload = claim.model_dump()
    throughput_payload.update({"claim_id": "throughput-prod", "claim_type": ClaimType.THROUGHPUT,
                               "requirement": "production throughput claim", "semantic_digest": ""})
    throughput_claim = ProductionClaimV1.model_validate(throughput_payload)
    assert service.bind_claim(throughput_claim, subject, env, (_evidence(EvidenceType.INTEGRATION_EVIDENCE),)) \
        .status is ClaimStatus.UNVERIFIED
    load = EvidenceV1(
        evidence_id="load-test-1", evidence_version="v1", evidence_type=EvidenceType.LOAD_TEST_EVIDENCE,
        basis=EvidenceBasis.SIMULATED, strength=EvidenceStrength.SYNTHETIC_LOAD, synthetic=True,
        subject_ref=claim.subject_ref, environment_ref="prod-1", source_kind="synthetic-test",
        source_ref="load:test", captured_at=NOW, payload_kind="SYNTHETIC_LOAD",
        load_payload=SyntheticLoadEvidenceV1(
            synthetic=True, workload_id="TEST FIXTURE", workload_version="v1", workload_digest="c" * 64,
            provider_mode="scripted", configured_concurrency=10, offered_concurrency=None,
            observed_concurrency=None, planned_count=1, attempted_count=1,
        ),
    )
    assert service.bind_claim(claim, subject, env, (load,)).status is ClaimStatus.UNVERIFIED

    # UNKNOWN == UNKNOWN is not identity proof, and old runtime/dirty code are scoped separately.
    unknown_env = EnvironmentIdentityV1(environment_type=EnvironmentType.UNKNOWN)
    assert service.bind_claim(claim, subject, unknown_env, (variants[0],)).status is ClaimStatus.UNVERIFIED
    valid = _valid_production_evidence(worktree_state=WorktreeState.DIRTY)
    trusted_source = ValidatedSourceReceipt(
        evidence_id=valid.evidence_id, evidence_digest=valid.semantic_digest,
        source_ref=valid.source_ref, verification_ref="TEST FIXTURE trusted-source-context",
    )
    assert service.bind_claim(
        claim, subject, env, (valid,), trusted_source_receipts=(trusted_source,),
    ).status is ClaimStatus.SUPPORTED
    with pytest.raises(ValidationError, match="VERIFIED_SOURCE requires a trusted service receipt"):
        EvidenceV1.model_validate({**valid.model_dump(), "source_authenticity": SourceAuthenticity.VERIFIED_SOURCE,
                                  "semantic_digest": ""})
    for field, value in (
        ("subject_ref", "subject:v1"),
        ("environment_ref", "other-prod"),
        ("metric_definition_digest", "metric-old"),
        ("population_ref", "population-old"),
        ("scenario_ref", "scenario-old"),
        ("policy_ref", "policy-old"),
    ):
        changed_payload = valid.model_dump()
        changed_payload[field] = value
        changed_payload["semantic_digest"] = ""
        changed = EvidenceV1.model_validate(changed_payload)
        changed_receipt = ValidatedSourceReceipt(
            evidence_id=changed.evidence_id, evidence_digest=changed.semantic_digest,
            source_ref=changed.source_ref, verification_ref="TEST FIXTURE trusted-source-context",
        )
        assert service.bind_claim(
            claim, subject, env, (changed,), trusted_source_receipts=(changed_receipt,),
        ).status is ClaimStatus.UNVERIFIED
    for key, value in (("deployment_id", "old-deploy"), ("runtime_version", "v1"),
                       ("source_revision", "old-revision"),
                       ("working_tree_content_digest", "old-tree"),
                       ("provider_identity", "other-provider"),
                       ("actual_model_binding", "other-model")):
        changed_payload = valid.model_dump()
        identity = dict(changed_payload["subject_identity"])
        identity[key] = value
        changed_payload["subject_identity"] = tuple(sorted(identity.items()))
        changed_payload["semantic_digest"] = ""
        changed = EvidenceV1.model_validate(changed_payload)
        changed_receipt = ValidatedSourceReceipt(
            evidence_id=changed.evidence_id, evidence_digest=changed.semantic_digest,
            source_ref=changed.source_ref, verification_ref="TEST FIXTURE trusted-source-context",
        )
        assert service.bind_claim(
            claim, subject, env, (changed,), trusted_source_receipts=(changed_receipt,),
        ).status is ClaimStatus.UNVERIFIED
    with pytest.raises(ValidationError):
        ProductionEvidenceSubjectV1(subject_ref="subject:test", working_tree_content_digest="dirty")
    with pytest.raises(ValidationError):
        ProductionEvidenceSubjectV1(subject_ref="subject:test", source_revision="head-only",
                                    worktree_state=WorktreeState.DIRTY)
    with pytest.raises(ValidationError):
        ReceiptModel(
            command_argv=("pytest",), cwd_alias="backend", scope="unit", body_assertions="1 passed",
            process_status=ProcessStatus.UNKNOWN, source_revision="head-only", worktree_state=WorktreeState.DIRTY,
        )


def test_safe_aggregate_strict_counts_latency_and_secret_errors() -> None:
    base = {
        "contract_version": "stage11.wp6.v1",
        "environment": {"type": "PRODUCTION", "id": "prod-a", "identity_source_ref": None},
        "deployment": {"id": None, "artifact_digest": None},
        "subject": {"source_revision": None, "working_tree_content_digest": None},
        "source": {"kind": "upload", "export_id": "export-1", "artifact_digest": None,
                   "verification_ref": None},
        "window_start": None, "window_end": None, "population_ref": "population-hash",
        "expected_count": 10, "success_count": 7, "failure_count": 1, "timeout_count": 0,
        "cancelled_count": None, "outcome_unknown_count": 1, "pending_count": 1,
        "latency": {"definition_ref": None, "unit": None, "method": None, "sample_count": 0,
                    "coverage": None, "p50": None, "p95": None, "p99": None},
        "cost_summary": None, "metric_report_digest": None, "synthetic": "UNKNOWN",
    }
    parsed = import_safe_aggregate(json.dumps(base))
    assert parsed.environment.type is EnvironmentType.PRODUCTION
    assert parsed.source.verification_ref is None  # self-asserted type is never source verification
    assert parsed.expected_count == 10
    assert parsed.success_count != 0

    with pytest.raises(ValueError, match="IMPORT_FIELD_REJECTED") as secret_error:
        import_safe_aggregate(json.dumps({**base, "api_key": "top-secret-value"}))
    assert "top-secret-value" not in str(secret_error.value)
    with pytest.raises(ValueError, match="IMPORT_FIELD_REJECTED"):
        import_safe_aggregate(json.dumps({**base, "raw_prompt": "customer text"}))
    with pytest.raises(ValueError, match="IMPORT_FIELD_REJECTED"):
        import_safe_aggregate(json.dumps({**base, "source": {**base["source"], "export_id": "Bearer super-secret"}}))
    with pytest.raises(ValueError, match="IMPORT_FIELD_REJECTED"):
        import_safe_aggregate(json.dumps({**base, "source_authenticity": "VERIFIED_SOURCE"}))
    with pytest.raises(ValueError, match="IMPORT_FIELD_REJECTED"):
        import_safe_aggregate(json.dumps({**base, "population_ref": "customer population"}))
    assert parsed.cost_summary is None
    with pytest.raises(ValueError):
        SafeAggregateImportV1.model_validate_json(json.dumps({**base, "success_count": 11}))
    with pytest.raises(ValueError):
        SafeAggregateImportV1.model_validate_json(json.dumps({**base, "latency": {
            **base["latency"], "p95": 123, "sample_count": 0,
        }}))
    with pytest.raises(ValueError):
        SafeAggregateImportV1.model_validate_json(json.dumps({**base, "latency": {
            **base["latency"], "sample_count": 10, "p95": 123,
        }}))
    with pytest.raises(ValueError):
        SafeAggregateImportV1.model_validate_json(json.dumps({**base, "latency": {
            **base["latency"], "definition_ref": "latency-v1", "unit": "ms",
            "method": None, "sample_count": 10, "coverage": 2.0, "p95": 123,
        }}))


def test_junit_receipt_keeps_body_pass_and_teardown_failure_separate() -> None:
    xml = b"""<testsuite tests='1' errors='1' failures='0'>
      <testcase classname='suite' name='test_pass'><error message='failed on teardown'>hidden</error></testcase>
    </testsuite>"""
    receipt = capture_junit_receipt(
        xml, command_argv=("uv", "run", "pytest", "tests/test_x.py"), cwd_alias="backend",
        scope="unit", process_exit_code=1, started_at=NOW, finished_at=NOW,
        source_revision="abc123", worktree_state=WorktreeState.CLEAN,
        selected_test_ids=("tests/test_x.py::test_pass",),
    )
    assert receipt.passed == 0  # teardown alone does not prove the call phase passed
    assert receipt.errors == 1
    assert receipt.process_status is ProcessStatus.FAILED
    assert receipt.process_exit_code == 1
    assert receipt.phases[-1].phase == "teardown"
    body_pass = ReceiptModel(
        command_argv=("uv", "run", "pytest"), cwd_alias="backend", scope="unit",
        body_assertions="4 passed", passed=4, failed=0, errors=4, process_exit_code=1,
        process_status=ProcessStatus.FAILED, raw_result_digest="c" * 64,
    )
    assert body_pass.body_assertions == "4 passed"
    assert body_pass.process_status is ProcessStatus.FAILED


def _valid_production_evidence(*, worktree_state: WorktreeState = WorktreeState.CLEAN) -> EvidenceV1:
    payload = _evidence(
        EvidenceType.PRODUCTION_EVIDENCE, synthetic=False, env_ref="prod-1",
        auth=SourceAuthenticity.DECLARED_SOURCE,
    ).model_dump()
    payload.update({
        "subject_identity": (
            ("worktree_state", worktree_state.value),
            ("runtime_version", "v2"), ("deployment_id", "deploy-1"),
            ("source_revision", "rev2"), ("working_tree_content_digest", "tree2"),
            ("provider_identity", "provider-a"), ("actual_model_binding", "model-a"),
            ("artifact_digest", "sha256:image"), ("eval_version", "eval-v2"),
        ),
        "metric_definition_digest": "metric-def-v1", "population_ref": "population-v1",
        "scenario_ref": "scenario-v1", "policy_ref": "policy-v1", "semantic_digest": "",
    })
    return EvidenceV1.model_validate(payload)


def test_fault_evidence_only_binds_to_exact_failure_scenario_claim() -> None:
    evidence = EvidenceV1(
        evidence_id="fault-1", evidence_version="v1", evidence_type=EvidenceType.FAULT_INJECTION_EVIDENCE,
        basis=EvidenceBasis.TESTED, strength=EvidenceStrength.DETERMINISTIC_TEST, synthetic=True,
        subject_ref="subject:test", environment_ref="test-env", source_kind="pytest", source_ref="test:1",
        subject_identity=(("runtime_version", "v1"),),
        scenario_ref="TEST FIXTURE redis timeout",
        source_authenticity=SourceAuthenticity.DECLARED_SOURCE, captured_at=NOW, payload_kind="FAULT",
        fault_payload=FaultEvidenceV1(
            fault_point="redis-timeout", scenario="TEST FIXTURE redis timeout", scenario_version="v1",
            trigger_receipt_ref="test:trigger", expected_semantics=("run marked failed",),
            observed_assertions=("run marked failed",),
        ),
    )
    requirement = EvidenceRequirementV1(
        evidence_type=EvidenceType.FAULT_INJECTION_EVIDENCE,
        predicate=EvidencePredicate.FAULT_SCENARIO_HANDLED,
        scenario_ref="TEST FIXTURE redis timeout",
    )
    latency_claim = ProductionClaimV1(
        claim_id="latency", claim_version="v1", claim_type=ClaimType.LATENCY,
        subject_ref="subject:test", scope=ClaimScopeV1(
            subject_ref="subject:test", environment_ref="test-env", version_ref="runtime:v1",
            evidence_scope="latency",
        ), requirement="latency p95",
        required_evidence_types=(EvidenceType.FAULT_INJECTION_EVIDENCE,),
        required_evidence_requirements=(requirement,), status=ClaimStatus.UNVERIFIED,
    )
    subject = ProductionEvidenceSubjectV1(subject_ref="subject:test", runtime_version="v1",
                                          environment_ref="test-env")
    env = EnvironmentIdentityV1(environment_type=EnvironmentType.TEST, environment_id="test-env")
    service = ProductionEvidenceService()
    assert service.bind_claim(latency_claim, subject, env, (evidence,)).status is ClaimStatus.UNVERIFIED
    failure_claim = ProductionClaimV1.model_validate({
        **latency_claim.model_dump(), "claim_id": "failure-handling",
        "claim_type": ClaimType.EXECUTION_RELIABILITY, "requirement": "specific timeout handled",
        "semantic_digest": "",
    })
    assert service.bind_claim(failure_claim, subject, env, (evidence,)).status is ClaimStatus.SUPPORTED
    historical = historical_summary_evidence("handoff:220")
    assert historical.evidence_type is EvidenceType.DECLARED_CONTEXT
    assert historical.source_authenticity is SourceAuthenticity.DECLARED_SOURCE
    assert historical.observed_window is None and historical.source_artifact_digest is None
    assert "NOT_CURRENT_REVISION_TEST_EVIDENCE" in historical.limitations


def test_generated_at_is_not_bundle_digest_and_identity_changes_are() -> None:
    evidence = _valid_production_evidence()
    claim = _production_claim()
    service = ProductionEvidenceService()
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:v2",
        runtime_version="v2", source_revision="rev2", working_tree_content_digest="tree2",
        worktree_state=WorktreeState.CLEAN,
        deployment_id="deploy-1", artifact_digest="sha256:image", actual_model_binding="model-a",
        provider_identity="provider-a", eval_version="eval-v2", environment_ref="prod-1",
    )
    environment = EnvironmentIdentityV1(
        environment_type=EnvironmentType.PRODUCTION, environment_id="prod-1", identity_source_ref="receipt-1",
    )
    trusted_source = ValidatedSourceReceipt(
        evidence_id=evidence.evidence_id, evidence_digest=evidence.semantic_digest,
        source_ref=evidence.source_ref, verification_ref="TEST FIXTURE trusted-source-context",
    )
    claim = service.bind_claim(claim, subject, environment, (evidence,), trusted_source_receipts=(trusted_source,))
    first = service.assemble_bundle(subject_ref=claim.subject_ref, evidence=(evidence,), claims=(claim,),
                                    generated_at=NOW)
    refs_before = first.evidence_refs, first.claims, first.metric_report_refs
    later = service.assemble_bundle(subject_ref=claim.subject_ref, evidence=(evidence,), claims=(claim,),
                                    generated_at=NOW.replace(day=3))
    assert first.semantic_digest == later.semantic_digest
    display_only_data = claim.model_dump()
    display_only_data.update({"requirement": "rephrased display text", "semantic_digest": ""})
    display_only_claim = ProductionClaimV1.model_validate(display_only_data)
    display_only_bound = service.bind_claim(
        display_only_claim, subject, environment, (evidence,), trusted_source_receipts=(trusted_source,),
    )
    display_only_bundle = service.assemble_bundle(
        subject_ref=claim.subject_ref, evidence=(evidence,), claims=(display_only_bound,), generated_at=NOW,
    )
    assert claim.semantic_digest == display_only_claim.semantic_digest
    assert first.semantic_digest == display_only_bundle.semantic_digest
    assert refs_before == (first.evidence_refs, first.claims, first.metric_report_refs)

    changed_window_payload = evidence.model_dump()
    changed_window_payload["observed_window"] = (NOW, NOW.replace(day=3))
    changed_window_payload["semantic_digest"] = ""
    changed_window = EvidenceV1.model_validate(changed_window_payload)
    changed = service.assemble_bundle(subject_ref=claim.subject_ref, evidence=(changed_window,), claims=(claim,),
                                      generated_at=NOW)
    assert first.semantic_digest != changed.semantic_digest

    changed_source_payload = evidence.model_dump()
    changed_source_payload["source_ref"] = "export-new"
    changed_source_payload["semantic_digest"] = ""
    changed_source = EvidenceV1.model_validate(changed_source_payload)
    source_changed_bundle = service.assemble_bundle(
        subject_ref=claim.subject_ref, evidence=(changed_source,), claims=(claim,), generated_at=NOW,
    )
    assert first.semantic_digest != source_changed_bundle.semantic_digest
    changed_claim_data = claim.model_dump()
    changed_claim_data.update({"status": ClaimStatus.UNVERIFIED, "semantic_digest": ""})
    status_changed_claim = ProductionClaimV1.model_validate(changed_claim_data)
    status_changed_bundle = service.assemble_bundle(
        subject_ref=claim.subject_ref, evidence=(evidence,), claims=(status_changed_claim,), generated_at=NOW,
    )
    assert first.semantic_digest != status_changed_bundle.semantic_digest

    with pytest.raises(ValidationError):
        first.subject_ref = "mutated"
    with pytest.raises(ValueError, match="EVIDENCE_ID_CONFLICT"):
        service.assemble_bundle(subject_ref=claim.subject_ref, evidence=(evidence, changed_window), claims=(claim,))

    report = service.readiness_report(first, required_claim_ids=(claim.claim_id,), generated_at=NOW)
    same_report_later = service.readiness_report(
        first, required_claim_ids=(claim.claim_id,), generated_at=NOW.replace(day=3),
    )
    assert report.report_status is ReportStatus.SCOPED_EVIDENCE_COMPLETE
    assert report.source_bundle_digest == first.semantic_digest
    assert report.semantic_digest == same_report_later.semantic_digest
    assert report.claims[0].what_proven == (EvidencePredicate.PERFORMANCE_MEASURED.value,)
    assert report.claims[0].what_not_proven == ()
    assert report.report_status.value in {
        "REPOSITORY_EVIDENCE_ONLY", "REAL_WORLD_EVIDENCE_PENDING", "SCOPED_EVIDENCE_COMPLETE",
    }
    assert "PRODUCTION_READY" not in str(report.model_dump())
    assert all("score" not in key.lower() and "grade" not in key.lower() for key in report.model_dump())

    unavailable = EvidenceAvailabilityV1(status=Availability.REQUIRES_REAL_WORLD_INPUT)
    assert unavailable.value is None
    catalog = minimal_claim_catalog(claim.subject_ref)
    assert catalog and all(item.status is ClaimStatus.UNVERIFIED for item in catalog)
    adoption = next(item for item in catalog if item.claim_type is ClaimType.BUSINESS_ADOPTION)
    assert adoption.status is ClaimStatus.UNVERIFIED
    assert "NOT_MEASURED" in adoption.reason_codes
    assert "0" not in adoption.limitations
    threshold = next(item for item in catalog if item.claim_id == "production-threshold-availability")
    assert "NOT_CONFIGURED" in threshold.reason_codes
    cost = next(item for item in catalog if item.claim_type is ClaimType.COST)
    assert cost.status is ClaimStatus.UNVERIFIED
    assert EvidenceAvailabilityV1(status=Availability.UNMEASURED).value is None
    load = SyntheticLoadEvidenceV1(
        synthetic=True, workload_id="TEST FIXTURE", workload_version="v1", workload_digest="a" * 64,
        provider_mode="scripted", configured_concurrency=10, offered_concurrency=None,
        observed_concurrency=None, planned_count=2, attempted_count=2,
    )
    assert load.configured_concurrency == 10 and load.observed_concurrency is None
    assert "incident_frequency" not in FaultEvidenceV1.model_fields
    assert "mttr" not in FaultEvidenceV1.model_fields


@pytest.mark.asyncio
async def test_fixture_metric_report_ref_preserves_truth_without_source_authenticity() -> None:
    from uuid import uuid4

    from app.core.evaluation.results import EvaluationVerdict
    from app.services.evaluation.comparison import EvaluationComparisonService
    from app.services.evaluation.platform_metrics import build_metric_report
    from tests.unit.test_evaluation_comparison import (
        FakePersistence, PROJECT_ID, make_result, make_run,
    )

    baseline_id, candidate_id = uuid4(), uuid4()
    runs = {baseline_id: make_run(baseline_id), candidate_id: make_run(candidate_id)}
    results = {run_id: (make_result(
        run_id=run_id, case_id="fixture", case_version="v1", evaluator_id="eval",
        evaluator_version="v1", verdict=EvaluationVerdict.PASS, score=0.9,
    ),) for run_id in runs}
    persistence = FakePersistence(runs, results)
    comparison = await EvaluationComparisonService(persistence).compare_runs(
        PROJECT_ID, baseline_id, candidate_id,
    )
    report = build_metric_report(
        comparison, persistence.attempts[baseline_id], persistence.attempts[candidate_id],
        persistence.results[baseline_id], persistence.results[candidate_id],
    )
    ref = ProductionEvidenceService.metric_report_ref(report, population_identity="TEST FIXTURE")
    assert ref.metric_contract_version == report.metric_contract_version
    assert ref.report_digest == report.semantic_digest
    assert ref.run_refs == (str(baseline_id), str(candidate_id))
    assert ref.comparison_digest == report.comparison_digest
    assert ref.source_authenticity is SourceAuthenticity.DECLARED_SOURCE


def test_test_command_predicate_requires_a_valid_successful_receipt() -> None:
    service = ProductionEvidenceService()
    subject = ProductionEvidenceSubjectV1(subject_ref="subject:v2", runtime_version="v2",
                                          environment_ref="test-env")
    environment = EnvironmentIdentityV1(
        environment_type=EnvironmentType.TEST, environment_id="test-env", identity_source_ref="TEST FIXTURE",
    )
    claim = ProductionClaimV1(
        claim_id="test-command", claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref="subject:v2", scope=ClaimScopeV1(
            subject_ref="subject:v2", environment_ref="test-env", version_ref="runtime:v2",
            evidence_scope="unit",
        ), requirement="test command succeeded", required_evidence_types=(EvidenceType.TEST_EVIDENCE,),
        required_evidence_requirements=(EvidenceRequirementV1(
            evidence_type=EvidenceType.TEST_EVIDENCE, predicate=EvidencePredicate.TEST_COMMAND_SUCCEEDED,
            required_environment_type=EnvironmentType.TEST, required_subject_fields=("runtime_version",),
        ),), status=ClaimStatus.SUPPORTED,
    )
    good = _evidence(env_ref="test-env", subject_ref="subject:v2")
    good_data = good.model_dump()
    good_data["subject_identity"] = (("runtime_version", "v2"),)
    good_data["semantic_digest"] = ""
    good, good_capture = _controlled_test_evidence(EvidenceV1.model_validate(good_data))
    assert service.bind_claim(claim, subject, environment, (good,),
                              captured_test_receipts=(good_capture,)).status is ClaimStatus.SUPPORTED
    bad, bad_capture = _controlled_test_evidence(good, failed=True)
    assert service.bind_claim(claim, subject, environment, (bad,),
                              captured_test_receipts=(bad_capture,)).status is ClaimStatus.UNVERIFIED


def test_cost_predicate_needs_actual_amount_currency_digest_and_coverage() -> None:
    service = ProductionEvidenceService()
    good = _valid_production_evidence(worktree_state=WorktreeState.DIRTY)
    with pytest.raises(ValidationError, match="synthetic flag conflicts"):
        EvidenceV1.model_validate({**good.model_dump(), "synthetic": True, "semantic_digest": ""})
    claim_data = _production_claim().model_dump()
    claim_data.update({"claim_id": "cost", "claim_type": ClaimType.COST,
                       "requirement": "actual cost measured", "semantic_digest": ""})
    requirements = dict(claim_data["required_evidence_requirements"][0])
    requirements["predicate"] = EvidencePredicate.ACTUAL_COST_MEASURED
    claim_data["required_evidence_requirements"] = (requirements,)
    claim = ProductionClaimV1.model_validate(claim_data)
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:v2",
        runtime_version="v2", eval_version="eval-v2", source_revision="rev2",
        working_tree_content_digest="tree2", worktree_state=WorktreeState.DIRTY,
        deployment_id="deploy-1", artifact_digest="sha256:image",
        provider_identity="provider-a", actual_model_binding="model-a", environment_ref="prod-1",
    )
    env = EnvironmentIdentityV1(
        environment_type=EnvironmentType.PRODUCTION, environment_id="prod-1", identity_source_ref="TEST FIXTURE",
    )
    def trusted(item: EvidenceV1) -> ValidatedSourceReceipt:
        return ValidatedSourceReceipt(item.evidence_id, item.semantic_digest, item.source_ref, "TEST FIXTURE")

    assert service.bind_claim(claim, subject, env, (good,),
                              trusted_source_receipts=(trusted(good),)).status is ClaimStatus.SUPPORTED
    data = good.model_dump()
    aggregate = good.safe_payload.model_dump()
    aggregate["cost_summary"] = None
    data.update({"safe_payload": aggregate, "semantic_digest": ""})
    missing = EvidenceV1.model_validate(data)
    assert service.bind_claim(claim, subject, env, (missing,),
                              trusted_source_receipts=(trusted(missing),)).status is ClaimStatus.UNVERIFIED


def test_required_only_version_and_environment_bind_to_the_frozen_subject() -> None:
    service = ProductionEvidenceService()
    claim = _production_claim()
    claim_data = claim.model_dump()
    requirement = dict(claim_data["required_evidence_requirements"][0])
    requirement["expected_subject_identity"] = ()
    claim_data.update({"required_evidence_requirements": (requirement,), "semantic_digest": ""})
    claim = ProductionClaimV1.model_validate(claim_data)
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:v2", runtime_version="v2", eval_version="eval-v2",
        source_revision="rev2", working_tree_content_digest="tree2", worktree_state=WorktreeState.CLEAN,
        deployment_id="deploy-1",
        artifact_digest="sha256:image", provider_identity="provider-a", actual_model_binding="model-a",
        environment_ref="prod-1",
    )
    evidence = _valid_production_evidence()
    stale_data = evidence.model_dump()
    identity = dict(stale_data["subject_identity"])
    identity["runtime_version"] = "v1"
    stale_data.update({"subject_identity": tuple(sorted(identity.items())), "semantic_digest": ""})
    stale = EvidenceV1.model_validate(stale_data)
    environment = EnvironmentIdentityV1(
        environment_type=EnvironmentType.PRODUCTION, environment_id="prod-1", identity_source_ref="TEST FIXTURE",
    )
    trusted = ValidatedSourceReceipt(stale.evidence_id, stale.semantic_digest, stale.source_ref, "TEST FIXTURE")
    result = service.bind_claim(claim, subject, environment, (stale,), trusted_source_receipts=(trusted,))
    assert result.status is ClaimStatus.UNVERIFIED
    assert "EVIDENCE_SUBJECT_MISMATCH" in result.reason_codes

    wrong_environment = EnvironmentIdentityV1(
        environment_type=EnvironmentType.TEST, environment_id="test-env", identity_source_ref="TEST FIXTURE",
    )
    result = service.bind_claim(claim, subject, wrong_environment, (evidence,))
    assert result.status is ClaimStatus.UNVERIFIED
    assert "ENVIRONMENT_MISMATCH" in result.reason_codes


def test_bundle_lineage_rejects_missing_refs_and_readiness_projects_only_proven_predicates() -> None:
    service = ProductionEvidenceService()
    subject = ProductionEvidenceSubjectV1(subject_ref="subject:v2", runtime_version="v2",
                                          environment_ref="test-env")
    environment = EnvironmentIdentityV1(
        environment_type=EnvironmentType.TEST, environment_id="test-env", identity_source_ref="TEST FIXTURE",
    )
    evidence = _evidence(env_ref="test-env", subject_ref="subject:v2")
    claim = ProductionClaimV1(
        claim_id="partial", claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref="subject:v2", scope=ClaimScopeV1(
            subject_ref="subject:v2", environment_ref="test-env", version_ref="runtime:v2",
            evidence_scope="unit",
        ), requirement="command passed and integration passed",
        required_evidence_types=(EvidenceType.TEST_EVIDENCE, EvidenceType.INTEGRATION_EVIDENCE),
        required_evidence_requirements=(
            EvidenceRequirementV1(evidence_type=EvidenceType.TEST_EVIDENCE,
                                  predicate=EvidencePredicate.TEST_COMMAND_SUCCEEDED,
                                  required_environment_type=EnvironmentType.TEST,
                                  required_subject_fields=("runtime_version",)),
            EvidenceRequirementV1(evidence_type=EvidenceType.INTEGRATION_EVIDENCE,
                                  predicate=EvidencePredicate.INTEGRATION_SCENARIO_SUCCEEDED,
                                  required_environment_type=EnvironmentType.TEST,
                                  required_subject_fields=("runtime_version",)),
        ), status=ClaimStatus.SUPPORTED,
    )
    evidence_data = evidence.model_dump()
    evidence_data["subject_identity"] = (("runtime_version", "v2"),)
    evidence_data["semantic_digest"] = ""
    evidence, captured = _controlled_test_evidence(EvidenceV1.model_validate(evidence_data))
    bound = service.bind_claim(claim, subject, environment, (evidence,), captured_test_receipts=(captured,))
    assert bound.status is ClaimStatus.PARTIALLY_SUPPORTED
    bundle = service.assemble_bundle(subject_ref="subject:v2", evidence=(evidence,), claims=(bound,))
    projection = service.readiness_report(bundle).claims[0]
    assert projection.what_proven == (EvidencePredicate.TEST_COMMAND_SUCCEEDED.value,)
    assert projection.what_not_proven == (EvidencePredicate.INTEGRATION_SCENARIO_SUCCEEDED.value,)
    missing_report = service.readiness_report(
        bundle, required_claim_ids=(claim.claim_id, "requested-but-missing"),
    )
    assert missing_report.report_status is ReportStatus.REAL_WORLD_EVIDENCE_PENDING
    assert missing_report.missing_claim_ids == ("requested-but-missing",)

    no_requirements = ProductionClaimV1(
        claim_id="forged", claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref="subject:v2", scope=claim.scope, requirement="caller says supported",
        status=ClaimStatus.SUPPORTED,
    )
    derived = service.bind_claim(no_requirements, subject, environment, ())
    assert derived.status is ClaimStatus.UNVERIFIED
    no_proof_bundle = service.assemble_bundle(subject_ref="subject:v2", evidence=(), claims=(derived,))
    assert service.readiness_report(no_proof_bundle, required_claim_ids=("forged",)).report_status \
        is ReportStatus.REAL_WORLD_EVIDENCE_PENDING
    with pytest.raises(ValidationError, match="claim evidence ref is unresolved"):
        dangling_data = bound.model_dump()
        dangling_data.update({"observed_evidence_refs": ("dangling",), "semantic_digest": ""})
        ProductionEvidenceBundleV1(
            subject_ref="subject:v2", evidence_refs=(), safe_snapshots=(),
            claims=(ProductionClaimV1.model_validate(dangling_data),), generated_at=NOW,
        )
    with pytest.raises(ValidationError, match="evidence subject differs"):
        service.assemble_bundle(subject_ref="other-subject", evidence=(evidence,), claims=(bound,))


def test_binder_deduplicates_same_identity_and_rejects_conflict_in_any_order() -> None:
    service = ProductionEvidenceService()
    subject = ProductionEvidenceSubjectV1(subject_ref="subject:v2", runtime_version="v2",
                                          environment_ref="test-env")
    environment = EnvironmentIdentityV1(
        environment_type=EnvironmentType.TEST, environment_id="test-env", identity_source_ref="TEST FIXTURE",
    )
    evidence = _evidence(env_ref="test-env", subject_ref="subject:v2")
    payload = evidence.model_dump()
    payload["subject_identity"] = (("runtime_version", "v2"),)
    payload["semantic_digest"] = ""
    evidence, captured = _controlled_test_evidence(EvidenceV1.model_validate(payload))
    claim = ProductionClaimV1(
        claim_id="dedup", claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref="subject:v2", scope=ClaimScopeV1(
            subject_ref="subject:v2", environment_ref="test-env", version_ref="runtime:v2",
            evidence_scope="unit",
        ), requirement="test passed", required_evidence_types=(EvidenceType.TEST_EVIDENCE,),
        required_evidence_requirements=(EvidenceRequirementV1(
            evidence_type=EvidenceType.TEST_EVIDENCE, predicate=EvidencePredicate.TEST_COMMAND_SUCCEEDED,
            required_environment_type=EnvironmentType.TEST, required_subject_fields=("runtime_version",),
        ),), status=ClaimStatus.UNVERIFIED,
    )
    for items in ((evidence, evidence.model_copy()), (evidence.model_copy(), evidence)):
        result = service.bind_claim(claim, subject, environment, items, captured_test_receipts=(captured,))
        assert result.status is ClaimStatus.SUPPORTED
        assert result.observed_evidence_refs == (evidence.evidence_id,)
    conflict_data = evidence.model_dump()
    conflict_data["source_ref"] = "other-source"
    conflict_data["semantic_digest"] = ""
    conflict = EvidenceV1.model_validate(conflict_data)
    for items in ((evidence, conflict), (conflict, evidence)):
        with pytest.raises(ValueError, match="EVIDENCE_IDENTITY_CONFLICT"):
            service.bind_claim(claim, subject, environment, items)


def test_dirty_build_requires_revision_and_tree_even_when_only_revision_is_requested() -> None:
    service = ProductionEvidenceService()
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:dirty", source_revision="rev2", working_tree_content_digest="tree2",
        worktree_state=WorktreeState.DIRTY, environment_ref="test-env",
    )
    environment = EnvironmentIdentityV1(
        environment_type=EnvironmentType.TEST, environment_id="test-env", identity_source_ref="TEST FIXTURE",
    )
    claim = ProductionClaimV1(
        claim_id="dirty-build", claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref=subject.subject_ref, scope=ClaimScopeV1(
            subject_ref=subject.subject_ref, environment_ref="test-env", version_ref="source:rev2",
            evidence_scope="unit",
        ), requirement="build identity is source revision rev2", status=ClaimStatus.UNVERIFIED,
        required_evidence_requirements=(EvidenceRequirementV1(
            evidence_type=EvidenceType.TEST_EVIDENCE, predicate=EvidencePredicate.TEST_COMMAND_SUCCEEDED,
            required_environment_type=EnvironmentType.TEST, required_subject_fields=("source_revision",),
        ),),
    )

    def bind_with(envelope_identity: tuple[tuple[str, str], ...], *, receipt_state: WorktreeState,
                  receipt_revision: str | None, receipt_tree: str | None) -> ClaimStatus:
        raw = _evidence(env_ref="test-env", subject_ref=subject.subject_ref).model_dump()
        raw.update({"evidence_id": f"evidence-{receipt_revision}-{receipt_tree}",
                    "subject_identity": envelope_identity, "semantic_digest": ""})
        evidence, captured = _controlled_test_evidence(
            EvidenceV1.model_validate(raw), worktree_state=receipt_state,
            source_revision=receipt_revision, working_tree_content_digest=receipt_tree,
        )
        return service.bind_claim(claim, subject, environment, (evidence,),
                                  captured_test_receipts=(captured,)).status

    assert bind_with((("source_revision", "rev2"), ("working_tree_content_digest", "old-tree")),
                     receipt_state=WorktreeState.DIRTY, receipt_revision="rev2", receipt_tree="old-tree") \
        is ClaimStatus.UNVERIFIED
    assert bind_with((("source_revision", "rev2"),), receipt_state=WorktreeState.DIRTY,
                     receipt_revision="rev2", receipt_tree="tree2") is ClaimStatus.UNVERIFIED
    assert bind_with(( ("source_revision", "rev2"), ("working_tree_content_digest", "tree2")),
                     receipt_state=WorktreeState.DIRTY, receipt_revision="old-revision", receipt_tree="old-tree") \
        is ClaimStatus.UNVERIFIED
    assert bind_with((("source_revision", "rev2"), ("working_tree_content_digest", "tree2")),
                     receipt_state=WorktreeState.UNKNOWN, receipt_revision="rev2", receipt_tree=None) \
        is ClaimStatus.UNVERIFIED


def test_dirty_test_receipt_build_must_match_envelope_and_subject() -> None:
    service = ProductionEvidenceService()
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:dirty", source_revision="rev2", working_tree_content_digest="tree2",
        worktree_state=WorktreeState.DIRTY, environment_ref="test-env",
    )
    environment = EnvironmentIdentityV1(
        environment_type=EnvironmentType.TEST, environment_id="test-env", identity_source_ref="TEST FIXTURE",
    )
    claim = ProductionClaimV1(
        claim_id="dirty-test", claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref=subject.subject_ref, scope=ClaimScopeV1(
            subject_ref=subject.subject_ref, environment_ref="test-env", version_ref="source:rev2",
            evidence_scope="unit",
        ), requirement="test ran on frozen dirty build", status=ClaimStatus.UNVERIFIED,
        required_evidence_requirements=(EvidenceRequirementV1(
            evidence_type=EvidenceType.TEST_EVIDENCE, predicate=EvidencePredicate.TEST_COMMAND_SUCCEEDED,
            required_environment_type=EnvironmentType.TEST, required_subject_fields=("source_revision",),
        ),),
    )
    raw = _evidence(env_ref="test-env", subject_ref=subject.subject_ref).model_dump()
    raw.update({"subject_identity": (("source_revision", "rev2"),
                                    ("working_tree_content_digest", "tree2")), "semantic_digest": ""})
    evidence, captured = _controlled_test_evidence(
        EvidenceV1.model_validate(raw), worktree_state=WorktreeState.DIRTY,
        source_revision="rev2", working_tree_content_digest="tree2",
    )
    bound = service.bind_claim(claim, subject, environment, (evidence,), captured_test_receipts=(captured,))
    bundle = service.assemble_bundle(subject_ref=subject.subject_ref, evidence=(evidence,), claims=(bound,))
    assert bound.status is ClaimStatus.SUPPORTED
    assert service.readiness_report(bundle).claims[0].status is ClaimStatus.SUPPORTED


def test_dirty_build_exact_positive() -> None:
    service = ProductionEvidenceService()
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:dirty", source_revision="rev2", working_tree_content_digest="tree2",
        worktree_state=WorktreeState.DIRTY, environment_ref="test-env",
    )
    environment = EnvironmentIdentityV1(
        environment_type=EnvironmentType.TEST, environment_id="test-env", identity_source_ref="TEST FIXTURE",
    )
    claim = ProductionClaimV1(
        claim_id="dirty-positive", claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref=subject.subject_ref, scope=ClaimScopeV1(
            subject_ref=subject.subject_ref, environment_ref="test-env", version_ref="source:rev2",
            evidence_scope="unit",
        ), requirement="dirty build was tested exactly", status=ClaimStatus.UNVERIFIED,
        required_evidence_requirements=(EvidenceRequirementV1(
            evidence_type=EvidenceType.TEST_EVIDENCE, predicate=EvidencePredicate.TEST_COMMAND_SUCCEEDED,
            required_environment_type=EnvironmentType.TEST, required_subject_fields=("source_revision",),
        ),),
    )
    raw = _evidence(env_ref="test-env", subject_ref=subject.subject_ref).model_dump()
    raw.update({"subject_identity": (("source_revision", "rev2"),
                                    ("working_tree_content_digest", "tree2")), "semantic_digest": ""})
    evidence, captured = _controlled_test_evidence(
        EvidenceV1.model_validate(raw), worktree_state=WorktreeState.DIRTY,
        source_revision="rev2", working_tree_content_digest="tree2",
    )
    bound = service.bind_claim(claim, subject, environment, (evidence,), captured_test_receipts=(captured,))
    assert bound.status is ClaimStatus.SUPPORTED


def test_serialized_supported_claim_cannot_bypass_binder() -> None:
    service = ProductionEvidenceService()
    evidence = _evidence()
    claim = ProductionClaimV1(
        claim_id="forged", claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref="subject:v2", scope=ClaimScopeV1(
            subject_ref="subject:v2", environment_ref="test-env", version_ref="runtime:v2",
            evidence_scope="unit",
        ), requirement="caller asserts test passed",
        required_evidence_requirements=(EvidenceRequirementV1(
            evidence_type=EvidenceType.TEST_EVIDENCE, predicate=EvidencePredicate.TEST_COMMAND_SUCCEEDED,
        ),), status=ClaimStatus.SUPPORTED,
        supported_predicates=(EvidencePredicate.TEST_COMMAND_SUCCEEDED,),
        observed_evidence_refs=(evidence.evidence_id,),
    )
    bundle = ProductionEvidenceBundleV1(
        subject_ref="subject:v2", evidence_refs=(evidence.evidence_id,), safe_snapshots=(evidence,),
        claims=(claim,), generated_at=NOW,
    )
    round_trip = ProductionEvidenceBundleV1.model_validate_json(bundle.model_dump_json())
    report = service.readiness_report(round_trip)
    assert report.report_status is not ReportStatus.SCOPED_EVIDENCE_COMPLETE
    assert report.claims[0].status is ClaimStatus.UNVERIFIED
    assert report.claims[0].what_proven == ()


@pytest.mark.parametrize("case", ("failed_test", "declared_production", "null_cost", "synthetic"))
def test_forged_supported_proof_does_not_complete_serialized_report(case: str) -> None:
    service = ProductionEvidenceService()
    if case == "failed_test":
        evidence, captured = _controlled_test_evidence(_evidence(), failed=True)
        assert captured.process_status is ProcessStatus.FAILED
        predicate = EvidencePredicate.TEST_COMMAND_SUCCEEDED
        evidence_type = EvidenceType.TEST_EVIDENCE
        environment = "test-env"
    else:
        evidence = _evidence(EvidenceType.PRODUCTION_EVIDENCE, synthetic=(case == "synthetic"), env_ref="prod-1")
        raw = evidence.model_dump()
        if case == "null_cost":
            aggregate = evidence.safe_payload.model_dump()
            aggregate["cost_summary"] = None
            raw["safe_payload"] = SafeAggregateImportV1.model_validate(aggregate)
        raw["semantic_digest"] = ""
        evidence = EvidenceV1.model_validate(raw)
        predicate = (EvidencePredicate.ACTUAL_COST_MEASURED if case == "null_cost" else
                     EvidencePredicate.PERFORMANCE_MEASURED)
        evidence_type = EvidenceType.PRODUCTION_EVIDENCE
        environment = "prod-1"
    claim = ProductionClaimV1(
        claim_id=case, claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref=evidence.subject_ref, scope=ClaimScopeV1(
            subject_ref=evidence.subject_ref, environment_ref=environment, version_ref="runtime:v2",
            evidence_scope="forged",
        ), requirement=f"forged {case}",
        required_evidence_requirements=(EvidenceRequirementV1(evidence_type=evidence_type, predicate=predicate),),
        status=ClaimStatus.SUPPORTED, supported_predicates=(predicate,),
        observed_evidence_refs=(evidence.evidence_id,),
    )
    bundle = ProductionEvidenceBundleV1(
        subject_ref=evidence.subject_ref, evidence_refs=(evidence.evidence_id,), safe_snapshots=(evidence,),
        claims=(claim,), generated_at=NOW,
    )
    parsed = ProductionEvidenceBundleV1.model_validate_json(bundle.model_dump_json())
    report = service.readiness_report(parsed)
    assert report.report_status is not ReportStatus.SCOPED_EVIDENCE_COMPLETE
    assert report.claims[0].status is ClaimStatus.UNVERIFIED
    assert report.claims[0].what_proven == ()


def test_binder_derived_valid_claim_can_complete_scoped_report() -> None:
    service = ProductionEvidenceService()
    evidence = _valid_production_evidence()
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:v2", runtime_version="v2", eval_version="eval-v2",
        source_revision="rev2", working_tree_content_digest="tree2", worktree_state=WorktreeState.CLEAN,
        deployment_id="deploy-1", artifact_digest="sha256:image", provider_identity="provider-a",
        actual_model_binding="model-a", environment_ref="prod-1",
    )
    environment = EnvironmentIdentityV1(
        environment_type=EnvironmentType.PRODUCTION, environment_id="prod-1", identity_source_ref="receipt-1",
    )
    trusted = ValidatedSourceReceipt(evidence.evidence_id, evidence.semantic_digest, evidence.source_ref, "verified")
    bound = service.bind_claim(_production_claim(), subject, environment, (evidence,), trusted_source_receipts=(trusted,))
    bundle = service.assemble_bundle(subject_ref=subject.subject_ref, evidence=(evidence,), claims=(bound,))
    first = service.readiness_report(bundle, generated_at=NOW)
    later = service.readiness_report(bundle, generated_at=NOW.replace(day=3))
    assert first.report_status is ReportStatus.SCOPED_EVIDENCE_COMPLETE
    assert first.semantic_digest == later.semantic_digest
    serialized = ProductionEvidenceBundleV1.model_validate_json(bundle.model_dump_json())
    serialized_report = service.readiness_report(serialized, generated_at=NOW)
    assert serialized_report.report_status is not ReportStatus.SCOPED_EVIDENCE_COMPLETE
    assert serialized_report.claims[0].what_proven == ()


def test_dirty_runtime_and_eval_provenance_require_atomic_build_identity() -> None:
    service = ProductionEvidenceService()
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:v2", runtime_version="v2", eval_version="eval-v2",
        source_revision="rev2", working_tree_content_digest="tree2", worktree_state=WorktreeState.DIRTY,
        deployment_id="deploy-1", artifact_digest="sha256:image", provider_identity="provider-a",
        actual_model_binding="model-a", environment_ref="prod-1",
    )
    env = EnvironmentIdentityV1(
        environment_type=EnvironmentType.PRODUCTION, environment_id="prod-1", identity_source_ref="TEST_FIXTURE",
    )
    good = _valid_production_evidence(worktree_state=WorktreeState.DIRTY)
    for prefix, name, value in (("runtime", "runtime_version", "v2"), ("eval", "eval_version", "eval-v2")):
        data = _production_claim().model_dump()
        data["scope"]["version_ref"] = f"{prefix}:{value}"
        data["required_evidence_requirements"][0].update(
            required_subject_fields=(name,), expected_subject_identity=())
        data["semantic_digest"] = ""
        claim = ProductionClaimV1.model_validate(data)

        def bind(item, claim=claim):
            receipt = ValidatedSourceReceipt(item.evidence_id, item.semantic_digest, item.source_ref, "TEST_FIXTURE")
            return service.bind_claim(claim, subject, env, (item,), trusted_source_receipts=(receipt,))

        assert bind(good).status is ClaimStatus.SUPPORTED
        unknown_subject = ProductionEvidenceSubjectV1.model_validate({
            **subject.model_dump(), "worktree_state": WorktreeState.UNKNOWN,
        })
        receipt = ValidatedSourceReceipt(good.evidence_id, good.semantic_digest, good.source_ref, "TEST_FIXTURE")
        assert service.bind_claim(claim, unknown_subject, env, (good,),
                                  trusted_source_receipts=(receipt,)).status is ClaimStatus.UNVERIFIED
        for tree, state in (("old-tree", "DIRTY"), (None, "DIRTY"), ("tree2", "CLEAN"), ("tree2", None)):
            payload = good.model_dump()
            identity = dict(good.subject_identity)
            identity.pop("working_tree_content_digest", None)
            identity.pop("worktree_state", None)
            if tree is not None:
                identity["working_tree_content_digest"] = tree
            if state is not None:
                identity["worktree_state"] = state
            aggregate = good.safe_payload.model_dump()
            aggregate["subject"]["working_tree_content_digest"] = tree
            payload.update(subject_identity=tuple(identity.items()), safe_payload=aggregate, semantic_digest="")
            assert bind(EvidenceV1.model_validate(payload)).status is ClaimStatus.UNVERIFIED

    test_subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:v2", runtime_version="v2", source_revision="rev2",
        working_tree_content_digest="tree2", worktree_state=WorktreeState.DIRTY, environment_ref="test-env",
    )
    test_env = EnvironmentIdentityV1(environment_type=EnvironmentType.TEST, environment_id="test-env")
    raw = _evidence().model_dump()
    raw.update(subject_identity=(("runtime_version", "v2"), ("source_revision", "rev2"),
                                 ("working_tree_content_digest", "tree2"), ("worktree_state", "CLEAN")),
               semantic_digest="")
    evidence, captured = _controlled_test_evidence(
        EvidenceV1.model_validate(raw), worktree_state=WorktreeState.DIRTY,
        source_revision="rev2", working_tree_content_digest="tree2")
    test_claim = ProductionClaimV1(
        claim_id="state-conflict", claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref=test_subject.subject_ref,
        scope=ClaimScopeV1(subject_ref=test_subject.subject_ref, environment_ref="test-env",
                          version_ref="runtime:v2", evidence_scope="TEST_FIXTURE"),
        requirement="test frozen build", status=ClaimStatus.UNVERIFIED,
        required_evidence_requirements=(EvidenceRequirementV1(
            evidence_type=EvidenceType.TEST_EVIDENCE, predicate=EvidencePredicate.TEST_COMMAND_SUCCEEDED),),
    )
    assert service.bind_claim(test_claim, test_subject, test_env, (evidence,),
                              captured_test_receipts=(captured,)).status is ClaimStatus.UNVERIFIED


def test_capture_copy_cannot_inherit_execution_authority() -> None:
    service = ProductionEvidenceService()
    good, captured = _controlled_test_evidence(_evidence())
    failed, failed_capture = _controlled_test_evidence(good, failed=True)
    claim = ProductionClaimV1(
        claim_id="capture", claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref="subject:v2",
        scope=ClaimScopeV1(subject_ref="subject:v2", environment_ref="test-env",
                          version_ref="runtime:v2", evidence_scope="TEST_FIXTURE"),
        requirement="command succeeded", status=ClaimStatus.UNVERIFIED,
        required_evidence_requirements=(EvidenceRequirementV1(
            evidence_type=EvidenceType.TEST_EVIDENCE, predicate=EvidencePredicate.TEST_COMMAND_SUCCEEDED),),
    )
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:v2", runtime_version="v2", environment_ref="test-env")
    env = EnvironmentIdentityV1(environment_type=EnvironmentType.TEST, environment_id="test-env")
    data = good.model_dump()
    data.update(subject_identity=(("runtime_version", "v2"),), semantic_digest="")
    good = EvidenceV1.model_validate(data)
    assert service.bind_claim(claim, subject, env, (good,),
                              captured_test_receipts=(captured,)).status is ClaimStatus.SUPPORTED
    success = captured.receipt
    updates = {name: getattr(success, name) for name in ReceiptModel.model_fields}
    for copied in (captured.model_copy(), captured.model_copy(deep=True),
                   failed_capture.model_copy(update=updates)):
        assert service.bind_claim(claim, subject, env, (good,),
                                  captured_test_receipts=(copied,)).status is ClaimStatus.UNVERIFIED
    assert failed.test_payload.process_status is ProcessStatus.FAILED


def test_bound_claim_copy_and_evidence_replacement_require_rebinding() -> None:
    service = ProductionEvidenceService()
    raw = _evidence().model_dump()
    raw.update(subject_identity=(("runtime_version", "v2"),), semantic_digest="")
    good, captured = _controlled_test_evidence(EvidenceV1.model_validate(raw))
    failed, failed_capture = _controlled_test_evidence(good, failed=True)
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:v2", runtime_version="v2", environment_ref="test-env")
    env = EnvironmentIdentityV1(environment_type=EnvironmentType.TEST, environment_id="test-env")
    claim = ProductionClaimV1(
        claim_id="bound", claim_version="v1", claim_type=ClaimType.FUNCTIONAL_CORRECTNESS,
        subject_ref=subject.subject_ref,
        scope=ClaimScopeV1(subject_ref=subject.subject_ref, environment_ref="test-env",
                          version_ref="runtime:v2", evidence_scope="TEST_FIXTURE"),
        requirement="command succeeded", status=ClaimStatus.UNVERIFIED,
        required_evidence_requirements=(EvidenceRequirementV1(
            evidence_type=EvidenceType.TEST_EVIDENCE, predicate=EvidencePredicate.TEST_COMMAND_SUCCEEDED),),
    )
    bound = service.bind_claim(claim, subject, env, (good,), captured_test_receipts=(captured,))
    failed_bound = service.bind_claim(claim, subject, env, (failed,), captured_test_receipts=(failed_capture,))
    forged_data = failed_bound.model_dump()
    forged_data.update(status=ClaimStatus.SUPPORTED,
                       supported_predicates=(EvidencePredicate.TEST_COMMAND_SUCCEEDED,),
                       observed_evidence_refs=(failed.evidence_id,), limitations=(), semantic_digest="")
    forged = replace(failed_bound, claim=ProductionClaimV1.model_validate(forged_data))
    for item, proof in ((failed, bound), (failed, forged), (good, replace(bound)),
                        (good, replace(bound, claim=bound.claim.model_copy()))):
        bundle = service.assemble_bundle(subject_ref=subject.subject_ref, evidence=(item,), claims=(proof,))
        report = service.readiness_report(bundle)
        assert report.report_status is not ReportStatus.SCOPED_EVIDENCE_COMPLETE
        assert report.claims[0].what_proven == ()
    stale_digest = good.model_copy(update={"test_payload": failed.test_payload})
    with pytest.raises(ValidationError, match="evidence semantic digest mismatch"):
        service.assemble_bundle(subject_ref=subject.subject_ref, evidence=(stale_digest,), claims=(bound,))
    positive = service.assemble_bundle(subject_ref=subject.subject_ref, evidence=(good,), claims=(bound,))
    assert service.readiness_report(positive).report_status is ReportStatus.SCOPED_EVIDENCE_COMPLETE


def test_production_proof_cannot_be_reused_for_changed_measurements_or_source() -> None:
    service = ProductionEvidenceService()
    good = _valid_production_evidence()
    subject = ProductionEvidenceSubjectV1(
        subject_ref="subject:v2", runtime_version="v2", eval_version="eval-v2",
        source_revision="rev2", working_tree_content_digest="tree2", worktree_state=WorktreeState.CLEAN,
        deployment_id="deploy-1", artifact_digest="sha256:image", provider_identity="provider-a",
        actual_model_binding="model-a", environment_ref="prod-1",
    )
    env = EnvironmentIdentityV1(
        environment_type=EnvironmentType.PRODUCTION, environment_id="prod-1", identity_source_ref="TEST_FIXTURE")
    trusted = ValidatedSourceReceipt(good.evidence_id, good.semantic_digest, good.source_ref, "TEST_FIXTURE")
    for kind in ("cost", "synthetic", "source"):
        data = _production_claim().model_dump()
        if kind == "cost":
            data["required_evidence_requirements"][0]["predicate"] = EvidencePredicate.ACTUAL_COST_MEASURED
        data["semantic_digest"] = ""
        claim = ProductionClaimV1.model_validate(data)
        bound = service.bind_claim(claim, subject, env, (good,), trusted_source_receipts=(trusted,))
        assert bound.status is ClaimStatus.SUPPORTED
        payload = good.model_dump()
        aggregate = good.safe_payload.model_dump()
        if kind == "cost":
            aggregate["cost_summary"] = None
        elif kind == "synthetic":
            aggregate["synthetic"] = True
            payload["synthetic"] = True
        else:
            aggregate["source"].update(export_id="new-export", artifact_digest="sha256:new-export")
            payload.update(source_ref="new-export", source_artifact_digest="sha256:new-export")
        payload.update(safe_payload=aggregate, semantic_digest="")
        changed = EvidenceV1.model_validate(payload)
        bundle = service.assemble_bundle(subject_ref=subject.subject_ref, evidence=(changed,), claims=(bound,))
        report = service.readiness_report(bundle)
        assert report.report_status is not ReportStatus.SCOPED_EVIDENCE_COMPLETE
        assert report.claims[0].what_proven == ()

\n