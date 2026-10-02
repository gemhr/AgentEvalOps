"""Stage11 WP6 safe, immutable production-evidence contracts."""

# ruff: noqa: D101, D102

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

CONTRACT_VERSION = "stage11.wp6.v1"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+~-]{0,255}$")


def _require_safe_identifier(value: str | None) -> str | None:
    if value is not None and not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError("identifier field rejected")
    return value


class StrictModel(BaseModel):
    """Strict recursively immutable serialized contract base."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class EvidenceType(StrEnum):
    REPOSITORY_EVIDENCE = "REPOSITORY_EVIDENCE"
    TEST_EVIDENCE = "TEST_EVIDENCE"
    INTEGRATION_EVIDENCE = "INTEGRATION_EVIDENCE"
    FAULT_INJECTION_EVIDENCE = "FAULT_INJECTION_EVIDENCE"
    LOAD_TEST_EVIDENCE = "LOAD_TEST_EVIDENCE"
    STAGING_EVIDENCE = "STAGING_EVIDENCE"
    PRODUCTION_EVIDENCE = "PRODUCTION_EVIDENCE"
    OPERATIONAL_EVIDENCE = "OPERATIONAL_EVIDENCE"
    BUSINESS_EVIDENCE = "BUSINESS_EVIDENCE"
    DECLARED_CONTEXT = "DECLARED_CONTEXT"


class EvidenceBasis(StrEnum):
    MEASURED = "MEASURED"
    OBSERVED = "OBSERVED"
    SIMULATED = "SIMULATED"
    TESTED = "TESTED"
    DECLARED = "DECLARED"
    UNKNOWN = "UNKNOWN"


class EvidenceStrength(StrEnum):
    CONTRACT = "CONTRACT"
    STATIC = "STATIC"
    DETERMINISTIC_TEST = "DETERMINISTIC_TEST"
    INTEGRATION = "INTEGRATION"
    SYNTHETIC_LOAD = "SYNTHETIC_LOAD"
    STAGING = "STAGING"
    PRODUCTION_OBSERVATION = "PRODUCTION_OBSERVATION"


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    UNMEASURED = "UNMEASURED"
    UNKNOWN = "UNKNOWN"
    REQUIRES_REAL_WORLD_INPUT = "REQUIRES_REAL_WORLD_INPUT"


class EvidenceAvailabilityV1(StrictModel):
    """Availability for a required value; absent measurements remain null."""

    status: Availability
    value: int | float | None = None
    unit: str | None = None
    evidence_refs: tuple[str, ...] = ()

    @field_validator("value")
    @classmethod
    def finite_optional_value(cls, value: int | float | None) -> int | float | None:
        """Reject non-finite measurements."""
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("availability value must be finite")
        return value


class EnvironmentType(StrEnum):
    LOCAL = "LOCAL"
    CI = "CI"
    TEST = "TEST"
    STAGING = "STAGING"
    PRODUCTION = "PRODUCTION"
    UNKNOWN = "UNKNOWN"


class WorktreeState(StrEnum):
    CLEAN = "CLEAN"
    DIRTY = "DIRTY"
    UNKNOWN = "UNKNOWN"


class SourceAuthenticity(StrEnum):
    VERIFIED_SOURCE = "VERIFIED_SOURCE"
    DECLARED_SOURCE = "DECLARED_SOURCE"
    UNKNOWN_SOURCE = "UNKNOWN_SOURCE"


class CIEvidenceSourceV1(StrictModel):
    """Narrow source wrapper for controlled CI execution receipts."""

    evidence_type: Literal["TEST_EVIDENCE"] = "TEST_EVIDENCE"
    basis: Literal["TESTED"] = "TESTED"
    workflow: str | None = None
    job: str | None = None
    run_id: str | None = None
    source_revision: str | None = None
    environment_type: EnvironmentType
    synthetic: bool = True
    source_authenticity: SourceAuthenticity

    @model_validator(mode="after")
    def reject_self_verified_source(self) -> CIEvidenceSourceV1:
        if self.source_authenticity is SourceAuthenticity.VERIFIED_SOURCE:
            raise ValueError("VERIFIED_SOURCE requires a trusted service receipt")
        return self


class ClaimType(StrEnum):
    FUNCTIONAL_CORRECTNESS = "FUNCTIONAL_CORRECTNESS"
    EXECUTION_RELIABILITY = "EXECUTION_RELIABILITY"
    RECOVERY = "RECOVERY"
    DURABILITY = "DURABILITY"
    THROUGHPUT = "THROUGHPUT"
    LATENCY = "LATENCY"
    RESOURCE_USAGE = "RESOURCE_USAGE"
    COST = "COST"
    SECURITY = "SECURITY"
    DATA_SAFETY = "DATA_SAFETY"
    OBSERVABILITY = "OBSERVABILITY"
    OPERABILITY = "OPERABILITY"
    DEPLOYABILITY = "DEPLOYABILITY"
    ROLLBACK = "ROLLBACK"
    COMPATIBILITY = "COMPATIBILITY"
    BUSINESS_ADOPTION = "BUSINESS_ADOPTION"
    BUSINESS_VALUE = "BUSINESS_VALUE"


class ClaimStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNVERIFIED = "UNVERIFIED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidencePredicate(StrEnum):
    """有限的 WP6 声明谓词，无谓词时证据只表示可用性."""

    TEST_COMMAND_SUCCEEDED = "TEST_COMMAND_SUCCEEDED"
    INTEGRATION_SCENARIO_SUCCEEDED = "INTEGRATION_SCENARIO_SUCCEEDED"
    FAULT_SCENARIO_HANDLED = "FAULT_SCENARIO_HANDLED"
    METRIC_REPORT_AVAILABLE = "METRIC_REPORT_AVAILABLE"
    ACTUAL_COST_MEASURED = "ACTUAL_COST_MEASURED"
    PERFORMANCE_MEASURED = "PERFORMANCE_MEASURED"
    PRODUCTION_OBSERVATION_AVAILABLE = "PRODUCTION_OBSERVATION_AVAILABLE"
    PRODUCTION_POLICY_CONFIGURED = "PRODUCTION_POLICY_CONFIGURED"
    DATA_SAFETY_CONTRACT_PRESENT = "DATA_SAFETY_CONTRACT_PRESENT"
    CI_WIRING_PRESENT = "CI_WIRING_PRESENT"


class ProcessStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NOT_EXECUTED = "NOT_EXECUTED"
    INTERRUPTED = "INTERRUPTED"
    UNKNOWN = "UNKNOWN"


class ReportStatus(StrEnum):
    REPOSITORY_EVIDENCE_ONLY = "REPOSITORY_EVIDENCE_ONLY"
    REAL_WORLD_EVIDENCE_PENDING = "REAL_WORLD_EVIDENCE_PENDING"
    SCOPED_EVIDENCE_COMPLETE = "SCOPED_EVIDENCE_COMPLETE"


def semantic_digest(value: object) -> str:
    """Hash canonical JSON content; this does not authenticate its source."""

    def encode(item: object) -> object:
        if isinstance(item, BaseModel):
            return {key: encode(member) for key, member in item.model_dump(mode="python").items()}
        if isinstance(item, StrEnum):
            return item.value
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, dict):
            return {str(key): encode(member) for key, member in item.items()}
        if isinstance(item, (tuple, list)):
            return [encode(member) for member in item]
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("evidence numbers must be finite")
        return item

    payload = json.dumps(encode(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EnvironmentIdentityV1(StrictModel):
    environment_type: EnvironmentType
    environment_id: str | None = None
    identity_source_ref: str | None = None
    region: str | None = None
    topology: str | None = None
    dependency_versions: tuple[tuple[str, str], ...] = ()


class ProductionEvidenceSubjectV1(StrictModel):
    subject_ref: str
    project_scope: str | None = None
    agent_identity: str | None = None
    workflow_identity: str | None = None
    toolset_identity: str | None = None
    provider_identity: str | None = None
    actual_model_binding: str | None = None
    runtime_version: str | None = None
    eval_version: str | None = None
    run_mode: str | None = None
    profile: str | None = None
    environment_ref: str | None = None
    source_revision: str | None = None
    working_tree_content_digest: str | None = None
    worktree_state: WorktreeState = WorktreeState.UNKNOWN
    deployment_id: str | None = None
    artifact_digest: str | None = None

    @model_validator(mode="after")
    def dirty_tree_has_content_digest(self) -> ProductionEvidenceSubjectV1:
        _require_safe_identifier(self.subject_ref)
        if self.worktree_state is WorktreeState.DIRTY and (
            not self.working_tree_content_digest or not self.source_revision
        ):
            raise ValueError("dirty source requires source_revision and working_tree_content_digest")
        if self.working_tree_content_digest and not self.source_revision:
            raise ValueError("dirty source requires source_revision")
        return self


class PhaseSummaryV1(StrictModel):
    phase: Literal["collection", "setup", "call", "teardown"]
    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    skipped: int | None = None
    unknown: int | None = None
    summary_ref: str | None = None


class TestEvidenceV1(StrictModel):
    command_argv: tuple[str, ...]
    cwd_alias: str
    scope: str
    selected_test_ids: tuple[str, ...] = ()
    selection_digest: str | None = None
    body_assertions: str
    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    skipped: int | None = None
    xfail: int | None = None
    xpass: int | None = None
    phases: tuple[PhaseSummaryV1, ...] = ()
    process_exit_code: int | None = None
    process_status: ProcessStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    source_revision: str | None = None
    working_tree_content_digest: str | None = None
    worktree_state: WorktreeState = WorktreeState.UNKNOWN
    build_identity: str | None = None
    dependency_versions: tuple[tuple[str, str], ...] = ()
    dependency_modes: tuple[tuple[str, str], ...] = ()
    dependency_health: Availability = Availability.UNMEASURED
    raw_result_digest: str | None = None
    safe_artifact_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_process_truth(self) -> TestEvidenceV1:
        if self.working_tree_content_digest and not self.source_revision:
            raise ValueError("dirty test evidence requires source_revision")
        if self.worktree_state is WorktreeState.DIRTY and (
            not self.working_tree_content_digest or not self.source_revision
        ):
            raise ValueError("dirty test evidence requires source_revision and working_tree_content_digest")
        if self.process_status is ProcessStatus.SUCCEEDED and (
            self.process_exit_code != 0 or self.failed != 0 or self.errors != 0 or not self.raw_result_digest
        ):
            raise ValueError("SUCCEEDED requires exit 0, no failures/errors, and a result digest")
        if self.process_exit_code not in (None, 0) and self.process_status is ProcessStatus.SUCCEEDED:
            raise ValueError("non-zero process exit cannot succeed")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        return self


class SyntheticLoadEvidenceV1(StrictModel):
    synthetic: Literal[True]
    workload_id: str
    workload_version: str
    workload_digest: str
    provider_mode: str
    configured_concurrency: int | None = None
    offered_concurrency: int | None = None
    observed_concurrency: int | None = None
    planned_count: int | None = None
    attempted_count: int | None = None
    completed_count: int | None = None
    success_count: int | None = None
    failure_count: int | None = None
    timeout_count: int | None = None
    cancelled_count: int | None = None
    unknown_count: int | None = None
    unfinished_count: int | None = None
    observed_duration_seconds: float | None = None
    measurement_window: tuple[datetime, datetime] | None = None
    latency_definition: str | None = None
    latency_sample_count: int | None = None
    latency_method: str | None = None
    latency_coverage: float | None = None
    resource_observations: tuple[tuple[str, float | None], ...] = ()
    not_instrumented: tuple[str, ...] = ()
    policy_ref: str | None = None
    limitations: tuple[str, ...] = ()

    @field_validator("observed_duration_seconds", "latency_coverage")
    @classmethod
    def finite_load_values(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("load evidence numbers must be finite")
        return value


class FaultEvidenceV1(StrictModel):
    fault_point: str
    scenario: str
    scenario_version: str
    trigger_receipt_ref: str
    expected_semantics: tuple[str, ...]
    observed_assertions: tuple[str, ...]
    dependency_topology: tuple[tuple[str, str], ...] = ()
    limitations: tuple[str, ...] = ()


class EvidenceV1(StrictModel):
    evidence_id: str
    evidence_version: str
    evidence_type: EvidenceType
    basis: EvidenceBasis
    strength: EvidenceStrength
    synthetic: bool
    subject_ref: str | None = None
    subject_identity: tuple[tuple[str, str], ...] = ()
    environment_ref: str | None = None
    metric_definition_digest: str | None = None
    population_ref: str | None = None
    scenario_ref: str | None = None
    policy_ref: str | None = None
    source_kind: str
    source_ref: str | None = None
    source_artifact_digest: str | None = None
    source_authenticity: SourceAuthenticity = SourceAuthenticity.UNKNOWN_SOURCE
    collector: str | None = None
    collector_version: str | None = None
    observed_window: tuple[datetime, datetime] | None = None
    observed_at: datetime | None = None
    captured_at: datetime
    payload_kind: Literal["TEST", "SYNTHETIC_LOAD", "FAULT", "SAFE_AGGREGATE", "SUMMARY"]
    test_payload: TestEvidenceV1 | None = None
    load_payload: SyntheticLoadEvidenceV1 | None = None
    fault_payload: FaultEvidenceV1 | None = None
    safe_payload_ref: str | None = None
    safe_payload: SafeAggregateImportV1 | None = None
    limitations: tuple[str, ...] = ()
    semantic_digest: str = ""

    @field_validator("subject_identity")
    @classmethod
    def unique_subject_identity(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        names = [name for name, _ in value]
        if len(names) != len(set(names)):
            raise ValueError("subject identity keys must be unique")
        identity = dict(value)
        if ("provider_identity" in identity and "provider_binding_identity" in identity
                and identity["provider_identity"] != identity["provider_binding_identity"]):
            raise ValueError("provider identity aliases conflict")
        if ("actual_model_binding" in identity and "actual_model" in identity
                and identity["actual_model_binding"] != identity["actual_model"]):
            raise ValueError("model identity aliases conflict")
        return value

    @model_validator(mode="after")
    def payload_matches_kind_and_hash(self) -> EvidenceV1:
        selected = {
            "TEST": self.test_payload,
            "SYNTHETIC_LOAD": self.load_payload,
            "FAULT": self.fault_payload,
            "SAFE_AGGREGATE": self.safe_payload,
            "SUMMARY": self.safe_payload_ref,
        }
        if selected[self.payload_kind] is None:
            raise ValueError("payload_kind requires its strict payload")
        payload_values = (self.test_payload, self.load_payload, self.fault_payload,
                          self.safe_payload_ref, self.safe_payload)
        if sum(value is not None for value in payload_values) != 1:
            raise ValueError("evidence must contain exactly one payload")
        if self.evidence_type is EvidenceType.TEST_EVIDENCE and self.payload_kind != "TEST":
            raise ValueError("test evidence requires a test receipt payload")
        if self.evidence_type is EvidenceType.FAULT_INJECTION_EVIDENCE and self.payload_kind != "FAULT":
            raise ValueError("fault evidence requires a fault payload")
        if self.evidence_type is EvidenceType.LOAD_TEST_EVIDENCE and self.payload_kind != "SYNTHETIC_LOAD":
            raise ValueError("load evidence requires a synthetic load payload")
        if self.evidence_type is EvidenceType.PRODUCTION_EVIDENCE and self.payload_kind != "SAFE_AGGREGATE":
            raise ValueError("production evidence requires a strict safe aggregate payload")
        if self.safe_payload is not None and self.synthetic is not (self.safe_payload.synthetic is True):
            if self.safe_payload.synthetic != "UNKNOWN":
                raise ValueError("evidence synthetic flag conflicts with aggregate payload")
        if self.load_payload is not None and not self.synthetic:
            raise ValueError("synthetic load payload requires synthetic evidence")
        if self.source_authenticity is SourceAuthenticity.VERIFIED_SOURCE:
            raise ValueError("VERIFIED_SOURCE requires a trusted service receipt")
        expected = semantic_digest(self.model_dump(exclude={"semantic_digest"}))
        if self.semantic_digest and self.semantic_digest != expected:
            raise ValueError("evidence semantic digest mismatch")
        object.__setattr__(self, "semantic_digest", expected)
        return self


class EvidenceRequirementV1(StrictModel):
    evidence_type: EvidenceType
    predicate: EvidencePredicate | None = None
    required: bool = True
    required_strength: EvidenceStrength | None = None
    required_authenticity: SourceAuthenticity | None = None
    required_environment_type: EnvironmentType | None = None
    required_subject_fields: tuple[str, ...] = ()
    expected_subject_identity: tuple[tuple[str, str], ...] = ()
    metric_definition_digest: str | None = None
    population_ref: str | None = None
    scenario_ref: str | None = None
    policy_ref: str | None = None


class ClaimScopeV1(StrictModel):
    subject_ref: str
    environment_ref: str
    version_ref: str
    evidence_scope: str


class ProductionClaimV1(StrictModel):
    claim_id: str
    claim_version: str
    claim_type: ClaimType
    subject_ref: str
    scope: ClaimScopeV1
    requirement: str
    required_evidence_types: tuple[EvidenceType, ...] = ()
    required_evidence_requirements: tuple[EvidenceRequirementV1, ...] = ()
    observed_evidence_refs: tuple[str, ...] = ()
    supported_predicates: tuple[EvidencePredicate, ...] = ()
    status: ClaimStatus
    reason_codes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    semantic_digest: str = ""

    @model_validator(mode="after")
    def scope_is_bound_to_subject(self) -> ProductionClaimV1:
        if self.scope.subject_ref != self.subject_ref:
            raise ValueError("claim scope subject mismatch")
        if self.status is ClaimStatus.SUPPORTED and any(
            value in {"", "UNKNOWN", "UNVERIFIED"}
            for value in (self.scope.environment_ref, self.scope.version_ref, self.scope.evidence_scope)
        ):
            raise ValueError("supported claim requires explicit environment, version, and evidence scope")
        return self

    @model_validator(mode="after")
    def claim_digest(self) -> ProductionClaimV1:
        expected = semantic_digest(self.model_dump(exclude={"semantic_digest", "requirement"}))
        if self.semantic_digest and self.semantic_digest != expected:
            raise ValueError("claim semantic digest mismatch")
        object.__setattr__(self, "semantic_digest", expected)
        return self


class MetricReportRefV1(StrictModel):
    metric_contract_version: str
    report_digest: str
    run_refs: tuple[str, ...]
    comparison_digest: str
    population_identity: str
    source_authenticity: SourceAuthenticity

    @model_validator(mode="after")
    def reject_self_verified_source(self) -> MetricReportRefV1:
        if self.source_authenticity is SourceAuthenticity.VERIFIED_SOURCE:
            raise ValueError("VERIFIED_SOURCE requires a trusted service receipt")
        if (not self.metric_contract_version or not self.report_digest or not self.run_refs
                or not self.comparison_digest or not self.population_identity):
            raise ValueError("metric report reference is incomplete")
        return self


class ProductionEvidenceBundleV1(StrictModel):
    contract_version: Literal["stage11.wp6.v1"] = CONTRACT_VERSION
    subject_ref: str
    evidence_refs: tuple[str, ...] = ()
    safe_snapshots: tuple[EvidenceV1, ...] = ()
    claims: tuple[ProductionClaimV1, ...] = ()
    metric_report_refs: tuple[MetricReportRefV1, ...] = ()
    acceptance_policy_ref: str | None = None
    limitations: tuple[str, ...] = ()
    generated_at: datetime
    semantic_digest: str = ""

    @model_validator(mode="after")
    def bundle_digest_and_dedup(self) -> ProductionEvidenceBundleV1:
        refs: dict[str, str] = {}
        for evidence in self.safe_snapshots:
            prior = refs.setdefault(evidence.evidence_id, evidence.semantic_digest)
            if prior != evidence.semantic_digest:
                raise ValueError("EVIDENCE_ID_CONFLICT")
        if not set(refs).issubset(set(self.evidence_refs)):
            raise ValueError("safe snapshot must be referenced")
        if set(refs) != set(self.evidence_refs):
            raise ValueError("evidence refs must resolve exactly to safe snapshots")
        evidence_by_id = {item.evidence_id: item for item in self.safe_snapshots}
        if any(item.subject_ref != self.subject_ref for item in self.safe_snapshots):
            raise ValueError("evidence subject differs from bundle subject")
        for claim in self.claims:
            if claim.subject_ref != self.subject_ref:
                raise ValueError("claim subject differs from bundle subject")
            for ref in claim.observed_evidence_refs:
                evidence = evidence_by_id.get(ref)
                if evidence is None:
                    raise ValueError("claim evidence ref is unresolved")
                if evidence.subject_ref != self.subject_ref:
                    raise ValueError("claim evidence subject differs from bundle subject")
                if evidence.environment_ref != claim.scope.environment_ref:
                    raise ValueError("claim evidence environment differs from claim scope")
            if claim.status is ClaimStatus.SUPPORTED and (
                not claim.supported_predicates or not claim.observed_evidence_refs
            ):
                raise ValueError("supported claim requires bound predicates and evidence refs")
            if EvidencePredicate.METRIC_REPORT_AVAILABLE in claim.supported_predicates:
                reports = {item.report_digest for item in self.metric_report_refs}
                metric_digests = {
                    evidence_by_id[ref].safe_payload.metric_report_digest
                    for ref in claim.observed_evidence_refs
                    if ref in evidence_by_id and evidence_by_id[ref].safe_payload is not None
                }
                if not (reports & metric_digests):
                    raise ValueError("required MetricReport reference is unresolved")
        payload = self.model_dump(exclude={"semantic_digest", "generated_at"})
        payload["claims"] = [
            {key: value for key, value in claim.items() if key != "requirement"}
            for claim in payload["claims"]
        ]
        expected = semantic_digest(payload)
        if self.semantic_digest and self.semantic_digest != expected:
            raise ValueError("bundle semantic digest mismatch")
        object.__setattr__(self, "semantic_digest", expected)
        return self


class ClaimProjectionV1(StrictModel):
    claim_id: str
    claim_version: str
    scope: ClaimScopeV1
    status: ClaimStatus
    maturity: tuple[str, ...]
    supported_predicates: tuple[str, ...] = ()
    unsatisfied_requirements: tuple[str, ...] = ()
    what_proven: tuple[str, ...] = ()
    what_not_proven: tuple[str, ...] = ()
    required_next_evidence: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class ProductionReadinessReportV1(StrictModel):
    subject_ref: str
    report_status: ReportStatus
    claims: tuple[ClaimProjectionV1, ...]
    source_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    generated_at: datetime
    source_bundle_digest: str
    missing_claim_ids: tuple[str, ...] = ()
    semantic_digest: str = ""

    @model_validator(mode="after")
    def report_digest(self) -> ProductionReadinessReportV1:
        expected = semantic_digest(self.model_dump(exclude={"semantic_digest", "generated_at"}))
        if self.semantic_digest and self.semantic_digest != expected:
            raise ValueError("readiness semantic digest mismatch")
        object.__setattr__(self, "semantic_digest", expected)
        return self


class AggregateEnvironmentV1(StrictModel):
    type: EnvironmentType
    id: str | None = None
    identity_source_ref: str | None = None

    @field_validator("id", "identity_source_ref")
    @classmethod
    def safe_refs(cls, value: str | None) -> str | None:
        return _require_safe_identifier(value)


class AggregateDeploymentV1(StrictModel):
    id: str | None = None
    artifact_digest: str | None = None

    @field_validator("id", "artifact_digest")
    @classmethod
    def safe_refs(cls, value: str | None) -> str | None:
        return _require_safe_identifier(value)


class AggregateSubjectV1(StrictModel):
    source_revision: str | None = None
    working_tree_content_digest: str | None = None
    runtime_version: str | None = None
    eval_version: str | None = None
    agent_version: str | None = None
    workflow_version: str | None = None
    toolset_identity: str | None = None
    provider_binding_identity: str | None = None
    actual_model: str | None = None

    @field_validator(
        "source_revision", "working_tree_content_digest", "runtime_version", "eval_version",
        "agent_version", "workflow_version", "toolset_identity", "provider_binding_identity", "actual_model",
    )
    @classmethod
    def safe_refs(cls, value: str | None) -> str | None:
        return _require_safe_identifier(value)


class AggregateSourceV1(StrictModel):
    kind: str | None = None
    export_id: str | None = None
    artifact_digest: str | None = None
    verification_ref: str | None = None

    @field_validator("kind", "export_id", "artifact_digest", "verification_ref")
    @classmethod
    def safe_refs(cls, value: str | None) -> str | None:
        return _require_safe_identifier(value)


class LatencySummaryV1(StrictModel):
    definition_ref: str | None = None
    unit: str | None = None
    method: str | None = None
    sample_count: int | None = None
    coverage: float | None = None
    p50: float | None = None
    p95: float | None = None
    p99: float | None = None

    @field_validator("definition_ref", "unit", "method")
    @classmethod
    def safe_refs(cls, value: str | None) -> str | None:
        return _require_safe_identifier(value)

    @field_validator("coverage", "p50", "p95", "p99")
    @classmethod
    def finite_latency_values(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("latency values must be finite")
        if value is not None and value < 0:
            raise ValueError("latency values must be >= 0")
        return value

    @model_validator(mode="after")
    def sample_semantics(self) -> LatencySummaryV1:
        if self.sample_count is not None and self.sample_count < 0:
            raise ValueError("sample_count must be >= 0")
        if self.coverage is not None and not 0 <= self.coverage <= 1:
            raise ValueError("latency coverage must be between 0 and 1")
        if self.sample_count in (None, 0) and any(value is not None for value in (self.p50, self.p95, self.p99)):
            raise ValueError("percentiles require samples")
        if self.sample_count is not None and self.sample_count > 0 and any(
            value is not None for value in (self.p50, self.p95, self.p99)
        ):
            if not self.definition_ref or not self.unit or not self.method:
                raise ValueError("percentile summary requires definition, unit, and method")
        return self


class CostSummaryV1(StrictModel):
    currency: str
    actual_amount: float | None = None
    receipt_digest: str | None = None
    coverage: float | None = None
    count: int | None = None

    @field_validator("currency", "receipt_digest")
    @classmethod
    def safe_refs(cls, value: str | None) -> str | None:
        return _require_safe_identifier(value)

    @model_validator(mode="after")
    def actual_receipt(self) -> CostSummaryV1:
        if self.actual_amount is not None and (
            not math.isfinite(self.actual_amount) or self.actual_amount < 0 or not self.receipt_digest
        ):
            raise ValueError("actual cost requires finite amount and receipt digest")
        if self.coverage is not None and not 0 <= self.coverage <= 1:
            raise ValueError("cost coverage must be between 0 and 1")
        if self.count is not None and self.count < 0:
            raise ValueError("cost count must be >= 0")
        return self


class SafeAggregateImportV1(StrictModel):
    contract_version: Literal["stage11.wp6.v1"]
    environment: AggregateEnvironmentV1
    deployment: AggregateDeploymentV1
    subject: AggregateSubjectV1
    source: AggregateSourceV1
    window_start: datetime | None = None
    window_end: datetime | None = None
    population_ref: str | None = None
    expected_count: int | None = None
    success_count: int | None = None
    failure_count: int | None = None
    timeout_count: int | None = None
    cancelled_count: int | None = None
    outcome_unknown_count: int | None = None
    pending_count: int | None = None
    latency: LatencySummaryV1
    cost_summary: CostSummaryV1 | None = None
    metric_report_digest: str | None = None
    synthetic: Literal[True, False, "UNKNOWN"]

    @field_validator("population_ref", "metric_report_digest")
    @classmethod
    def safe_refs(cls, value: str | None) -> str | None:
        return _require_safe_identifier(value)
    @model_validator(mode="after")
    def validate_counts_and_source(self) -> SafeAggregateImportV1:
        counts = (self.success_count, self.failure_count, self.timeout_count,
                  self.cancelled_count, self.outcome_unknown_count)
        if any(value is not None and value < 0 for value in (*counts, self.pending_count, self.expected_count)):
            raise ValueError("counts must be >= 0")
        if self.expected_count is not None:
            known_terminal_lower_bound = sum(value for value in counts if value is not None)
            if known_terminal_lower_bound > self.expected_count:
                raise ValueError("terminal counts exceed expected population")
            if self.pending_count is not None and known_terminal_lower_bound + self.pending_count > self.expected_count:
                raise ValueError("terminal and pending counts exceed expected population")
            if self.pending_count is not None and sum(value or 0 for value in counts) + self.pending_count > self.expected_count:
                raise ValueError("terminal and pending counts exceed expected population")
        if self.window_start and self.window_end and self.window_end < self.window_start:
            raise ValueError("window_end precedes window_start")
        return self


def parse_safe_aggregate(data: bytes | str) -> SafeAggregateImportV1:
    """Parse bounded, strict JSON without echoing sensitive values in errors."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > 1_000_000:
        raise ValueError("IMPORT_TOO_LARGE")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("IMPORT_INVALID_JSON") from None
    try:
        if _contains_suspicious_value(payload):
            raise ValueError("IMPORT_FIELD_REJECTED")
        return SafeAggregateImportV1.model_validate_json(raw)
    except Exception:
        # Keep Pydantic's validation message away from untrusted import values.
        raise ValueError("IMPORT_FIELD_REJECTED") from None


def _contains_suspicious_value(value: object) -> bool:
    import re

    suspicious = re.compile(
        r"(?i)(bearer\s+\S+|(?:api[_-]?key|access[_-]?token|password|secret|credential|cookie)\s*[:=]|"
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|://[^/\s:]+:[^/@\s]+@)"
    )
    if isinstance(value, str):
        return suspicious.search(value) is not None
    if isinstance(value, dict):
        return any(_contains_suspicious_value(key) or _contains_suspicious_value(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_suspicious_value(item) for item in value)
    return False


EvidenceV1.model_rebuild()
