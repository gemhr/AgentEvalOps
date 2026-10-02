"""Read-only WP6 evidence validation, claim binding, and report projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from pydantic import PrivateAttr

from app.core.evaluation.platform_metrics import MetricReportV1
from app.core.evaluation.production_evidence import (
    ClaimProjectionV1,
    ClaimScopeV1,
    ClaimType,
    ClaimStatus,
    EvidenceBasis,
    EnvironmentIdentityV1,
    EnvironmentType,
    EvidenceRequirementV1,
    EvidenceType,
    EvidencePredicate,
    EvidenceV1,
    EvidenceStrength,
    MetricReportRefV1,
    ProductionClaimV1,
    ProductionEvidenceBundleV1,
    ProductionEvidenceSubjectV1,
    ProductionReadinessReportV1,
    TestEvidenceV1,
    ReportStatus,
    SafeAggregateImportV1,
    SourceAuthenticity,
    semantic_digest,
    parse_safe_aggregate,
)


_BOUND_CLAIM_TOKEN = object()
_CAPTURED_TEST_TOKEN = object()


class CapturedTestReceipt(TestEvidenceV1):
    """JUnit DTO subtype retaining capture provenance only in process memory."""

    _capture_token: object = PrivateAttr(default=None)
    _capture_object_id: int = PrivateAttr(default=0)
    _capture_digest: str = PrivateAttr(default="")

    @property
    def receipt(self) -> TestEvidenceV1:
        """Return the stable serialized receipt view."""
        return self


def _captured_test_receipt(receipt: TestEvidenceV1) -> CapturedTestReceipt:
    """Internal bridge used by the controlled JUnit parser, not the file contract."""
    captured = CapturedTestReceipt.model_validate(receipt.model_dump())
    object.__setattr__(captured, "_capture_token", _CAPTURED_TEST_TOKEN)
    object.__setattr__(captured, "_capture_object_id", id(captured))
    object.__setattr__(captured, "_capture_digest", semantic_digest(captured.model_dump()))
    return captured


@dataclass(frozen=True, slots=True)
class _BoundProductionClaim:
    claim: ProductionClaimV1
    _token: object = field(repr=False, compare=False)
    _original_claim: ProductionClaimV1 = field(repr=False, compare=False)
    _evidence_digests: tuple[tuple[str, str], ...] = field(repr=False, compare=False)
    _metric_ref_digests: tuple[str, ...] = field(repr=False, compare=False)
    _bound_object_id: int = field(default=0, repr=False, compare=False)

    def __getattr__(self, name: str):
        return getattr(self.claim, name)

    def model_dump(self, *args, **kwargs):
        return self.claim.model_dump(*args, **kwargs)


class _BoundProductionEvidenceBundle(ProductionEvidenceBundleV1):
    _binder_claim_ids: frozenset[int] = PrivateAttr(default_factory=frozenset)
    _binder_bundle_digest: str = PrivateAttr(default="")


@dataclass(frozen=True, slots=True)
class ValidatedSourceReceipt:
    """受控调用边界提供来源核验结果，普通 Evidence JSON 不能构造此输入."""

    evidence_id: str
    evidence_digest: str
    source_ref: str
    verification_ref: str


class ProductionEvidenceService:
    """Pure projections over supplied references; never executes or writes runs."""

    @staticmethod
    def metric_report_ref(
        report: MetricReportV1, *, population_identity: str,
    ) -> MetricReportRefV1:
        """Preserve a WP5 report reference without assigning source authenticity."""
        return MetricReportRefV1(
            metric_contract_version=report.metric_contract_version,
            report_digest=report.semantic_digest,
            run_refs=(report.baseline_run_id, report.candidate_run_id),
            comparison_digest=report.comparison_digest,
            population_identity=population_identity,
            # WP5 reports carry metric truth but no source/environment attestation.
            source_authenticity=SourceAuthenticity.DECLARED_SOURCE,
        )

    @staticmethod
    def validate_evidence(
        evidence: EvidenceV1,
        *,
        trusted_source_receipts: Iterable[ValidatedSourceReceipt] = (),
    ) -> tuple[str, ...]:
        """Return stable reasons for evidence that cannot support real-world claims."""
        reasons: list[str] = []
        if evidence.evidence_type is EvidenceType.PRODUCTION_EVIDENCE:
            if not ProductionEvidenceService._has_trusted_source(evidence, trusted_source_receipts):
                reasons.append("SOURCE_NOT_VERIFIED")
            if evidence.synthetic:
                reasons.append("SYNTHETIC_EVIDENCE")
        if evidence.evidence_type is EvidenceType.TEST_EVIDENCE:
            if evidence.test_payload is None:
                reasons.append("TEST_RECEIPT_MISSING")
        return tuple(reasons)

    @staticmethod
    def _has_trusted_source(
        evidence: EvidenceV1, receipts: Iterable[ValidatedSourceReceipt],
    ) -> bool:
        return any(
            receipt.evidence_id == evidence.evidence_id
            and receipt.evidence_digest == evidence.semantic_digest
            and receipt.source_ref == evidence.source_ref
            and bool(receipt.verification_ref)
            for receipt in receipts
        )

    @staticmethod
    def _identity_value(subject: ProductionEvidenceSubjectV1, name: str):
        aliases = {
            "deployment_id": "deployment_id",
            "artifact_digest": "artifact_digest",
            "provider_identity": "provider_identity",
            "provider_binding_identity": "provider_identity",
            "actual_model": "actual_model_binding",
        }
        value = getattr(subject, aliases.get(name, name), None)
        return value if isinstance(value, str) and value not in {"", "UNKNOWN", "NOT_APPLICABLE"} else None

    @classmethod
    def _evidence_identity(cls, item: EvidenceV1) -> dict[str, str]:
        identity = dict(item.subject_identity)
        if item.test_payload is not None:
            state = item.test_payload.worktree_state.value
            if "worktree_state" in identity and identity["worktree_state"] != state:
                return {"__alias_conflict__": "true"}
            identity.setdefault("worktree_state", state)
        if "provider_binding_identity" in identity:
            if "provider_identity" in identity and identity["provider_identity"] != identity["provider_binding_identity"]:
                return {"__alias_conflict__": "true"}
            identity.setdefault("provider_identity", identity["provider_binding_identity"])
        if "actual_model" in identity:
            if "actual_model_binding" in identity and identity["actual_model_binding"] != identity["actual_model"]:
                return {"__alias_conflict__": "true"}
            identity.setdefault("actual_model_binding", identity["actual_model"])
        aggregate = item.safe_payload
        if aggregate is not None:
            payload_identity = {
                "deployment_id": aggregate.deployment.id,
                "artifact_digest": aggregate.deployment.artifact_digest,
                "environment_id": aggregate.environment.id,
                "environment_type": aggregate.environment.type.value,
                "source_revision": aggregate.subject.source_revision,
                "working_tree_content_digest": aggregate.subject.working_tree_content_digest,
                "runtime_version": aggregate.subject.runtime_version,
                "eval_version": aggregate.subject.eval_version,
                "provider_identity": aggregate.subject.provider_binding_identity,
                "actual_model_binding": aggregate.subject.actual_model,
            }
            for key, value in payload_identity.items():
                if key in identity and value is not None and identity[key] != value:
                    return {"__alias_conflict__": "true"}
                if value is not None:
                    identity.setdefault(key, value)
        return {key: value for key, value in identity.items() if isinstance(value, str)}

    @staticmethod
    def _scope_version_identity(claim: ProductionClaimV1) -> tuple[str, str] | None:
        prefix, separator, value = claim.scope.version_ref.partition(":")
        if not separator or value in {"", "UNKNOWN", "UNVERIFIED", "NOT_APPLICABLE"}:
            return None
        field_name = {
            "runtime": "runtime_version", "agent": "agent_identity", "workflow": "workflow_identity",
            "toolset": "toolset_identity", "provider": "provider_identity", "model": "actual_model_binding",
            "eval": "eval_version", "source": "source_revision", "deployment": "deployment_id",
        }.get(prefix)
        return (field_name, value) if field_name is not None else None

    @classmethod
    def _scope_version_matches(cls, claim: ProductionClaimV1, subject: ProductionEvidenceSubjectV1) -> bool:
        version_identity = cls._scope_version_identity(claim)
        return bool(version_identity and getattr(subject, version_identity[0], None) == version_identity[1])

    @staticmethod
    def _predicate_is_true(
        predicate: EvidencePredicate | None,
        item: EvidenceV1,
        *,
        trusted_source_receipts: Iterable[ValidatedSourceReceipt],
        environment: EnvironmentIdentityV1,
        metric_report_refs: Iterable[MetricReportRefV1] = (),
    ) -> bool:
        if predicate is None:
            return False
        if environment.environment_type is EnvironmentType.PRODUCTION and (
            item.evidence_type is not EvidenceType.PRODUCTION_EVIDENCE
        ):
            return False
        payload = item.safe_payload
        if item.evidence_type is EvidenceType.PRODUCTION_EVIDENCE:
            if (
                environment.environment_type is not EnvironmentType.PRODUCTION
                or not environment.environment_id
                or not environment.identity_source_ref
                or item.synthetic
                or not ProductionEvidenceService._has_trusted_source(item, trusted_source_receipts)
                or payload is None
                or payload.synthetic is not False
                or payload.environment.type is not EnvironmentType.PRODUCTION
                or payload.environment.id != environment.environment_id
                or not payload.deployment.id
                or not payload.deployment.artifact_digest
            ):
                return False
        if predicate in {EvidencePredicate.TEST_COMMAND_SUCCEEDED, EvidencePredicate.INTEGRATION_SCENARIO_SUCCEEDED,
                         EvidencePredicate.CI_WIRING_PRESENT}:
            receipt = item.test_payload
            return bool(
                receipt is not None and item.evidence_type in {EvidenceType.TEST_EVIDENCE, EvidenceType.INTEGRATION_EVIDENCE}
                and receipt.process_status.value == "SUCCEEDED" and receipt.process_exit_code == 0
                and receipt.failed == 0 and receipt.errors == 0 and receipt.raw_result_digest
                and receipt.passed is not None
            )
        if predicate is EvidencePredicate.FAULT_SCENARIO_HANDLED:
            fault = item.fault_payload
            return bool(
                item.evidence_type is EvidenceType.FAULT_INJECTION_EVIDENCE and fault is not None
                and fault.expected_semantics and set(fault.expected_semantics).issubset(set(fault.observed_assertions))
            )
        if predicate is EvidencePredicate.ACTUAL_COST_MEASURED:
            cost = payload.cost_summary if payload else None
            return bool(payload and payload.window_start and payload.window_end and payload.population_ref
                        and cost and cost.actual_amount is not None and cost.currency and cost.receipt_digest
                        and cost.coverage is not None and 0 < cost.coverage <= 1)
        if predicate is EvidencePredicate.PERFORMANCE_MEASURED:
            latency = payload.latency if payload else None
            return bool(payload and payload.window_start and payload.window_end and payload.population_ref
                        and latency and latency.sample_count and latency.definition_ref and latency.unit and latency.method
                        and latency.coverage is not None and latency.coverage > 0
                        and any(value is not None for value in (latency.p50, latency.p95, latency.p99)))
        if predicate is EvidencePredicate.PRODUCTION_OBSERVATION_AVAILABLE:
            return bool(payload and payload.window_start and payload.window_end and payload.population_ref
                        and payload.expected_count is not None)
        if predicate is EvidencePredicate.METRIC_REPORT_AVAILABLE:
            return bool(payload and payload.metric_report_digest and any(
                item.report_digest == payload.metric_report_digest for item in metric_report_refs
            ))
        return False

    @staticmethod
    def _field(subject: ProductionEvidenceSubjectV1, environment: EnvironmentIdentityV1, name: str):
        """Resolve a frozen subject or environment field by name."""
        if name == "environment_type":
            return environment.environment_type
        if name == "environment_id":
            return environment.environment_id
        if name == "identity_source_ref":
            return environment.identity_source_ref
        return getattr(subject, name, None)

    def bind_claim(
        self,
        claim: ProductionClaimV1,
        subject: ProductionEvidenceSubjectV1,
        environment: EnvironmentIdentityV1,
        evidence: Iterable[EvidenceV1],
        *,
        trusted_source_receipts: Iterable[ValidatedSourceReceipt] = (),
        captured_test_receipts: Iterable[CapturedTestReceipt] = (),
        metric_report_refs: Iterable[MetricReportRefV1] = (),
    ) -> _BoundProductionClaim:
        """Recalculate support from exact scoped requirements and supplied evidence."""
        available: dict[str, EvidenceV1] = {}
        for item in evidence:
            item = EvidenceV1.model_validate(item.model_dump())
            previous = available.setdefault(item.evidence_id, item)
            if previous.semantic_digest != item.semantic_digest:
                raise ValueError("EVIDENCE_IDENTITY_CONFLICT")
        trusted_receipts = tuple(trusted_source_receipts)
        captured_tests = tuple(
            receipt for receipt in captured_test_receipts
            if isinstance(receipt, CapturedTestReceipt) and receipt._capture_token is _CAPTURED_TEST_TOKEN
            and receipt._capture_object_id == id(receipt)
            and receipt._capture_digest == semantic_digest(receipt.model_dump())
        )
        metric_refs = tuple(metric_report_refs)
        matched: list[EvidenceV1] = []
        supported_predicates: list[EvidencePredicate] = []
        reasons: list[str] = []
        unsatisfied: list[str] = []
        subject_ref_matches = bool(claim.subject_ref) and subject.subject_ref == claim.subject_ref \
            and claim.scope.subject_ref == claim.subject_ref
        environment_matches = (
            claim.scope.environment_ref == environment.environment_id == subject.environment_ref
            and environment.environment_type is not EnvironmentType.UNKNOWN
        )
        version_matches = self._scope_version_matches(claim, subject)
        scope_matches = subject_ref_matches and environment_matches and version_matches
        if not subject_ref_matches:
            reasons.append("SUBJECT_REF_MISMATCH")
        if not environment_matches:
            reasons.append("ENVIRONMENT_MISMATCH")
        if not version_matches:
            reasons.append("EVIDENCE_SUBJECT_MISMATCH")
        for requirement in claim.required_evidence_requirements:
            candidates = [item for item in available.values() if item.evidence_type is requirement.evidence_type]
            accepted = None
            mismatch_seen = False
            for item in candidates:
                if not scope_matches or item.subject_ref != claim.subject_ref or item.subject_ref is None:
                    mismatch_seen = True
                    continue
                if item.environment_ref != claim.scope.environment_ref:
                    mismatch_seen = True
                    reasons.append("ENVIRONMENT_MISMATCH")
                    continue
                if requirement.required_environment_type is not None and environment.environment_type is not requirement.required_environment_type:
                    mismatch_seen = True
                    continue
                if requirement.required_environment_type is not None and not environment.identity_source_ref:
                    mismatch_seen = True
                    continue
                if requirement.required_strength is not None and item.strength is not requirement.required_strength:
                    mismatch_seen = True
                    continue
                verified = self._has_trusted_source(item, trusted_receipts)
                if requirement.required_authenticity is not None:
                    if requirement.required_authenticity is SourceAuthenticity.VERIFIED_SOURCE and not verified:
                        mismatch_seen = True
                        continue
                    if requirement.required_authenticity is not SourceAuthenticity.VERIFIED_SOURCE and (
                        item.source_authenticity is not requirement.required_authenticity
                    ):
                        mismatch_seen = True
                        continue
                if item.evidence_type is EvidenceType.PRODUCTION_EVIDENCE and not verified:
                    mismatch_seen = True
                    continue
                if item.evidence_type is EvidenceType.TEST_EVIDENCE and item.test_payload is not None:
                    receipt = item.test_payload
                    if (
                        receipt.worktree_state is not subject.worktree_state
                        or receipt.source_revision != subject.source_revision
                        or receipt.working_tree_content_digest != subject.working_tree_content_digest
                    ):
                        mismatch_seen = True
                        reasons.append("EVIDENCE_SUBJECT_MISMATCH")
                        continue
                identity = self._evidence_identity(item)
                if "__alias_conflict__" in identity:
                    mismatch_seen = True
                    reasons.append("EVIDENCE_SUBJECT_MISMATCH")
                    continue
                if item.evidence_type is EvidenceType.TEST_EVIDENCE and subject.worktree_state.value == "DIRTY" and any(
                    identity.get(name) != self._field(subject, environment, name)
                    for name in ("source_revision", "working_tree_content_digest")
                ):
                    mismatch_seen = True
                    reasons.append("EVIDENCE_SUBJECT_MISMATCH")
                    continue
                scope_version = self._scope_version_identity(claim)
                if scope_version and identity.get(scope_version[0]) != scope_version[1]:
                    mismatch_seen = True
                    reasons.append("EVIDENCE_SUBJECT_MISMATCH")
                    continue
                build_fields = {"source_revision", "working_tree_content_digest"}
                build_claim = (
                    bool(build_fields.intersection(requirement.required_subject_fields))
                    or bool(build_fields.intersection(dict(requirement.expected_subject_identity)))
                    or (self._scope_version_identity(claim) or (None, None))[0] == "source_revision"
                    or ((subject.worktree_state.value == "DIRTY" or subject.source_revision
                         or subject.working_tree_content_digest or identity.get("source_revision")
                         or identity.get("working_tree_content_digest")) and (
                        bool({"runtime_version", "eval_version"}.intersection(requirement.required_subject_fields))
                        or bool({"runtime_version", "eval_version"}.intersection(
                            dict(requirement.expected_subject_identity)))
                        or (scope_version or (None, None))[0] in {"runtime_version", "eval_version"}
                    ))
                )
                if build_claim:
                    if subject.worktree_state.value == "UNKNOWN":
                        mismatch_seen = True
                        reasons.append("EVIDENCE_SUBJECT_MISMATCH")
                        continue
                    build_names = ("source_revision", "working_tree_content_digest") \
                        if subject.worktree_state.value == "DIRTY" else ("source_revision",)
                    if ((subject.worktree_state.value == "DIRTY" and identity.get("worktree_state") != "DIRTY")
                            or ("worktree_state" in identity
                                and identity["worktree_state"] != subject.worktree_state.value)):
                        mismatch_seen = True
                        reasons.append("EVIDENCE_SUBJECT_MISMATCH")
                        continue
                    if any(
                        self._field(subject, environment, name) in (None, "", "UNKNOWN", "NOT_APPLICABLE")
                        or identity.get(name) != self._field(subject, environment, name)
                        for name in build_names
                    ):
                        mismatch_seen = True
                        reasons.append("EVIDENCE_SUBJECT_MISMATCH")
                        continue
                if item.evidence_type is EvidenceType.TEST_EVIDENCE and not any(
                    trusted.model_dump() == item.test_payload.model_dump() for trusted in captured_tests
                ):
                    mismatch_seen = True
                    reasons.append("TEST_CAPTURE_NOT_TRUSTED")
                    continue
                missing = [name for name in requirement.required_subject_fields
                           if self._field(subject, environment, name) in (None, "", "UNKNOWN", "NOT_APPLICABLE")
                           or identity.get(name) in (None, "", "UNKNOWN", "NOT_APPLICABLE")]
                if missing:
                    mismatch_seen = True
                    continue
                exact_required = all(
                    identity.get(name) == self._field(subject, environment, name)
                    for name in requirement.required_subject_fields
                )
                expected = dict(requirement.expected_subject_identity)
                different = not exact_required or any(
                    value in {"UNKNOWN", "", "NOT_APPLICABLE"}
                    or self._field(subject, environment, name) != value
                    or identity.get(name) != value
                    for name, value in expected.items()
                )
                different = different or any((
                    requirement.metric_definition_digest is not None
                    and item.metric_definition_digest != requirement.metric_definition_digest,
                    requirement.population_ref is not None and item.population_ref != requirement.population_ref,
                    requirement.scenario_ref is not None and item.scenario_ref != requirement.scenario_ref,
                    requirement.policy_ref is not None and item.policy_ref != requirement.policy_ref,
                ))
                aggregate = item.safe_payload
                if aggregate is not None:
                    different = different or any((
                        aggregate.environment.id != environment.environment_id,
                        aggregate.environment.type is not environment.environment_type,
                        aggregate.source.export_id != item.source_ref,
                        aggregate.source.artifact_digest != item.source_artifact_digest,
                    ))
                if different:
                    mismatch_seen = True
                    continue
                if item.evidence_type is EvidenceType.FAULT_INJECTION_EVIDENCE and (
                    claim.claim_type.value not in {"EXECUTION_RELIABILITY", "RECOVERY"}
                    or not requirement.scenario_ref or item.fault_payload is None
                    or item.fault_payload.scenario != requirement.scenario_ref
                ):
                    mismatch_seen = True
                    continue
                if not self._predicate_is_true(
                    requirement.predicate, item, trusted_source_receipts=trusted_receipts, environment=environment,
                    metric_report_refs=metric_refs,
                ):
                    mismatch_seen = True
                    continue
                accepted = item
                break
            if accepted is not None:
                matched.append(accepted)
                if requirement.predicate is not None:
                    supported_predicates.append(requirement.predicate)
            elif requirement.required:
                unsatisfied.append((requirement.predicate or requirement.evidence_type).value)
                reasons.append("PREDICATE_NOT_PROVEN" if mismatch_seen else "EVIDENCE_NOT_AVAILABLE")
        required_types = set(claim.required_evidence_types)
        present_types = {item.evidence_type for item in matched}
        requirement_types = {item.evidence_type for item in claim.required_evidence_requirements}
        unsatisfied.extend(item.value for item in sorted(required_types - present_types, key=lambda item: item.value)
                           if item not in requirement_types and item.value not in unsatisfied)
        status = ClaimStatus.UNVERIFIED
        if supported_predicates and unsatisfied:
            status = ClaimStatus.PARTIALLY_SUPPORTED
        elif supported_predicates and not unsatisfied:
            status = ClaimStatus.SUPPORTED
        payload = claim.model_dump()
        if status is ClaimStatus.SUPPORTED and claim.scope.version_ref in {"", "UNKNOWN", "UNVERIFIED"}:
            status = ClaimStatus.UNVERIFIED
            reasons.append("EVIDENCE_SUBJECT_MISMATCH")
            unsatisfied.append("VERSION_SCOPE_UNKNOWN")
        payload.update({
            "observed_evidence_refs": tuple(sorted(item.evidence_id for item in matched)),
            "supported_predicates": tuple(sorted(set(supported_predicates), key=lambda item: item.value)),
            "status": status,
            "reason_codes": tuple(sorted(set(reasons))),
            "limitations": tuple(dict.fromkeys((*claim.limitations, *unsatisfied))),
        })
        payload.pop("semantic_digest", None)
        bound_claim = ProductionClaimV1.model_validate(payload)
        bound = _BoundProductionClaim(
            claim=bound_claim, _token=_BOUND_CLAIM_TOKEN, _original_claim=bound_claim,
            _evidence_digests=tuple(sorted({(item.evidence_id, item.semantic_digest) for item in matched})),
            _metric_ref_digests=tuple(sorted(semantic_digest(ref.model_dump()) for ref in metric_refs)),
        )
        object.__setattr__(bound, "_bound_object_id", id(bound))
        return bound

    @staticmethod
    def assemble_bundle(
        *, subject_ref: str, evidence: Iterable[EvidenceV1],
        claims: Iterable[ProductionClaimV1 | _BoundProductionClaim],
        metric_report_refs: Iterable[MetricReportRefV1] = (), acceptance_policy_ref: str | None = None,
        limitations: tuple[str, ...] = (), generated_at: datetime | None = None,
    ) -> ProductionEvidenceBundleV1:
        """Create a detached immutable bundle, deduplicating only identical evidence."""
        unique: dict[str, EvidenceV1] = {}
        for item in evidence:
            # 重新校验输入内容，不能沿用 copy/update 后遗留的旧 digest。
            item = EvidenceV1.model_validate(item.model_dump())
            prior = unique.setdefault(item.evidence_id, item)
            if prior.semantic_digest != item.semantic_digest:
                raise ValueError("EVIDENCE_ID_CONFLICT")
        refs = tuple(sorted(unique))
        metric_report_refs = tuple(metric_report_refs)
        metric_ref_digests = {semantic_digest(ref.model_dump()) for ref in metric_report_refs}
        raw_claims = tuple(claims)
        projected_claims = []
        bound_projected_claims = []
        for item in raw_claims:
            is_wrapper = isinstance(item, _BoundProductionClaim)
            claim = item.claim if is_wrapper else item
            is_bound = (
                is_wrapper and item._token is _BOUND_CLAIM_TOKEN and item._bound_object_id == id(item)
                and item.claim is item._original_claim
                and all(ref in unique and unique[ref].semantic_digest == digest
                        for ref, digest in item._evidence_digests)
                and set(item._metric_ref_digests).issubset(metric_ref_digests)
            )
            if not is_bound:
                data = claim.model_dump()
                data.update({
                    "status": ClaimStatus.UNVERIFIED,
                    "supported_predicates": (),
                    "observed_evidence_refs": (),
                    "reason_codes": tuple(sorted(set((*claim.reason_codes, "BINDER_PROOF_CONTEXT_MISSING")))),
                    "limitations": tuple(dict.fromkeys((*claim.limitations, "BINDER_PROOF_CONTEXT_MISSING"))),
                    "semantic_digest": "",
                })
                claim = ProductionClaimV1.model_validate(data)
            else:
                bound_projected_claims.append(claim)
            projected_claims.append(claim)
        bundle = _BoundProductionEvidenceBundle(
            subject_ref=subject_ref,
            evidence_refs=refs,
            safe_snapshots=tuple(unique[key] for key in refs),
            claims=tuple(sorted(projected_claims, key=lambda item: (item.claim_id, item.claim_version))),
            metric_report_refs=tuple(sorted(metric_report_refs, key=lambda item: item.report_digest)),
            acceptance_policy_ref=acceptance_policy_ref,
            limitations=limitations,
            generated_at=generated_at or datetime.now(timezone.utc),
        )
        bundle._binder_claim_ids = frozenset(id(claim) for claim in bound_projected_claims)
        bundle._binder_bundle_digest = bundle.semantic_digest
        return bundle

    @staticmethod
    def readiness_report(
        bundle: ProductionEvidenceBundleV1, *, required_claim_ids: tuple[str, ...] | None = None,
        generated_at: datetime | None = None,
    ) -> ProductionReadinessReportV1:
        """Project claim status without creating an aggregate readiness score."""
        requested = tuple(required_claim_ids or ())
        claims_by_id = {claim.claim_id: claim for claim in bundle.claims}
        selected = tuple(sorted(
            (claim for claim in bundle.claims if required_claim_ids is None or claim.claim_id in requested),
            key=lambda item: (item.claim_id, item.claim_version),
        ))
        missing_ids = tuple(sorted(set(requested) - set(claims_by_id)))
        evidence_by_id = {item.evidence_id: item for item in bundle.safe_snapshots}
        maturity_by_type = {
            EvidenceType.REPOSITORY_EVIDENCE: "SUPPORTED_BY_REPO",
            EvidenceType.TEST_EVIDENCE: "SUPPORTED_BY_TEST",
            EvidenceType.INTEGRATION_EVIDENCE: "SUPPORTED_BY_INTEGRATION",
            EvidenceType.FAULT_INJECTION_EVIDENCE: "SUPPORTED_BY_TEST",
            EvidenceType.LOAD_TEST_EVIDENCE: "SUPPORTED_BY_SYNTHETIC",
            EvidenceType.STAGING_EVIDENCE: "SUPPORTED_BY_STAGING",
            EvidenceType.PRODUCTION_EVIDENCE: "SUPPORTED_BY_PRODUCTION",
            EvidenceType.OPERATIONAL_EVIDENCE: "SUPPORTED_BY_OPERATIONAL",
            EvidenceType.BUSINESS_EVIDENCE: "SUPPORTED_BY_BUSINESS",
            EvidenceType.DECLARED_CONTEXT: "DECLARED",
        }
        binder_context_valid = False
        if isinstance(bundle, _BoundProductionEvidenceBundle):
            try:
                recomputed = ProductionEvidenceBundleV1.model_validate(bundle.model_dump()).semantic_digest
                binder_context_valid = recomputed == bundle._binder_bundle_digest == bundle.semantic_digest
            except (TypeError, ValueError):
                binder_context_valid = False
        bound_ids = bundle._binder_claim_ids if binder_context_valid else frozenset()
        projections = tuple(ClaimProjectionV1(
            claim_id=claim.claim_id,
            claim_version=claim.claim_version,
            scope=claim.scope,
            status=(claim.status if id(claim) in bound_ids else ClaimStatus.UNVERIFIED),
            maturity=(tuple(sorted({
                maturity_by_type[evidence_by_id[ref].evidence_type]
                for ref in (claim.observed_evidence_refs if id(claim) in bound_ids else ())
                if ref in evidence_by_id
            })) if claim.observed_evidence_refs and id(claim) in bound_ids else
                ("WP5_METRIC_REPORT_REF",) if bundle.metric_report_refs and id(claim) in bound_ids and claim.status is ClaimStatus.SUPPORTED else
                ("REQUIRES_REAL_WORLD_INPUT",)),
            supported_predicates=(tuple(item.value for item in claim.supported_predicates)
                                  if id(claim) in bound_ids else ()),
            unsatisfied_requirements=tuple(dict.fromkeys((*claim.limitations, *(
                (item.predicate.value if item.predicate else item.evidence_type.value)
                for item in claim.required_evidence_requirements
                if item.required and id(claim) not in bound_ids
                or item.required and item.predicate not in claim.supported_predicates
            )))),
            what_proven=(tuple(item.value for item in claim.supported_predicates)
                         if id(claim) in bound_ids else ()),
            what_not_proven=tuple(dict.fromkeys((*claim.limitations, *(
                (item.predicate.value if item.predicate else item.evidence_type.value)
                for item in claim.required_evidence_requirements
                if item.required and id(claim) not in bound_ids
                or item.required and item.predicate not in claim.supported_predicates
            )))),
            required_next_evidence=tuple(dict.fromkeys((*claim.limitations, *(
                (item.predicate.value if item.predicate else item.evidence_type.value)
                for item in claim.required_evidence_requirements
                if item.required and id(claim) not in bound_ids
                or item.required and item.predicate not in claim.supported_predicates
            )))),
            source_refs=(claim.observed_evidence_refs if id(claim) in bound_ids else ()),
            limitations=(claim.limitations if id(claim) in bound_ids else
                         tuple(dict.fromkeys((*claim.limitations, "BINDER_PROOF_CONTEXT_MISSING")))),
        ) for claim in selected)
        complete = bool(selected) and not missing_ids and all(
            id(item) in bound_ids and item.status is ClaimStatus.SUPPORTED
            for item in selected
        )
        only_repo = bool(selected) and bool(bundle.safe_snapshots) and all(
            evidence.evidence_type is EvidenceType.REPOSITORY_EVIDENCE for evidence in bundle.safe_snapshots
        )
        status = (ReportStatus.SCOPED_EVIDENCE_COMPLETE if complete else
                  ReportStatus.REPOSITORY_EVIDENCE_ONLY if only_repo else
                  ReportStatus.REAL_WORLD_EVIDENCE_PENDING)
        return ProductionReadinessReportV1(
            subject_ref=bundle.subject_ref,
            report_status=status,
            claims=projections,
            source_refs=bundle.evidence_refs,
            limitations=tuple(dict.fromkeys((*bundle.limitations, *(f"MISSING_CLAIM:{item}" for item in missing_ids)))),
            generated_at=generated_at or datetime.now(timezone.utc),
            source_bundle_digest=bundle.semantic_digest,
            missing_claim_ids=missing_ids,
        )


def repository_evidence_inventory(source_refs: tuple[str, ...]) -> tuple[EvidenceV1, ...]:
    """Create only explicit static inventory refs; never scans the filesystem."""
    now = datetime.now(timezone.utc)
    return tuple(EvidenceV1(
        evidence_id=f"repo:{semantic_digest(ref)[:16]}", evidence_version="v1",
        evidence_type=EvidenceType.REPOSITORY_EVIDENCE, basis=EvidenceBasis.UNKNOWN,
        strength=EvidenceStrength.STATIC,
        synthetic=False, source_kind="REPOSITORY_REFERENCE", source_ref=ref,
        source_authenticity=SourceAuthenticity.DECLARED_SOURCE, captured_at=now,
        payload_kind="SUMMARY", safe_payload_ref=ref, limitations=("STATIC_REFERENCE_ONLY",),
    ) for ref in source_refs)


def historical_summary_evidence(summary_ref: str) -> EvidenceV1:
    """Register a historical report reference without inventing its raw receipt facts."""
    return EvidenceV1(
        evidence_id=f"historical:{semantic_digest(summary_ref)[:16]}", evidence_version="v1",
        evidence_type=EvidenceType.DECLARED_CONTEXT, basis=EvidenceBasis.DECLARED,
        strength=EvidenceStrength.STATIC, synthetic=True, source_kind="HISTORICAL_HANDOFF",
        source_ref=summary_ref, source_authenticity=SourceAuthenticity.DECLARED_SOURCE,
        captured_at=datetime.now(timezone.utc), payload_kind="SUMMARY", safe_payload_ref=summary_ref,
        limitations=("HISTORICAL_SUMMARY_ONLY", "NOT_CURRENT_REVISION_TEST_EVIDENCE"),
    )


def import_safe_aggregate(data: bytes | str) -> SafeAggregateImportV1:
    """Accept a bounded, allowlisted aggregate; imports remain declared until verified."""
    return parse_safe_aggregate(data)


def minimal_claim_catalog(subject_ref: str) -> tuple[ProductionClaimV1, ...]:
    """Return only the frozen P0 catalog, with unavailable claims left unverified."""
    entries = (
        ("evaluation-contract-integrity", "Evaluation Contract Integrity", ClaimType.FUNCTIONAL_CORRECTNESS),
        ("persistence-scenario-evidence", "Persistence Scenario Evidence", ClaimType.DURABILITY),
        ("failure-handling-evidence", "Failure Handling Evidence", ClaimType.EXECUTION_RELIABILITY),
        ("process-evidence-integrity", "Process Evidence Integrity", ClaimType.OBSERVABILITY),
        ("metric-gate-integrity", "Metric/Gate Integrity", ClaimType.FUNCTIONAL_CORRECTNESS),
        ("release-ci-wiring-evidence", "Release/CI Wiring Evidence", ClaimType.DEPLOYABILITY),
        ("recovery-capability", "Recovery Capability", ClaimType.RECOVERY),
        ("performance-evidence-availability", "Performance Evidence Availability", ClaimType.THROUGHPUT),
        ("cost-evidence-availability", "Cost Evidence Availability", ClaimType.COST),
        ("data-safety-boundary", "Data Safety Boundary", ClaimType.DATA_SAFETY),
        ("production-threshold-availability", "Production Threshold Availability", ClaimType.OPERABILITY),
        ("business-adoption-availability", "Business Adoption Availability", ClaimType.BUSINESS_ADOPTION),
    )
    return tuple(ProductionClaimV1(
        claim_id=claim_id, claim_version="v1", claim_type=claim_type, subject_ref=subject_ref,
        scope=ClaimScopeV1(
            subject_ref=subject_ref, environment_ref="UNKNOWN", version_ref="UNVERIFIED",
            evidence_scope=f"catalog:{claim_id}",
        ), requirement=title,
        status=ClaimStatus.UNVERIFIED,
        reason_codes=("NOT_MEASURED", "EVIDENCE_NOT_AVAILABLE")
        if claim_type is ClaimType.BUSINESS_ADOPTION else
        ("NOT_CONFIGURED", "EVIDENCE_NOT_AVAILABLE")
        if claim_id == "production-threshold-availability" else ("EVIDENCE_NOT_AVAILABLE",),
        limitations=("REQUIRES_REAL_WORLD_INPUT",),
    ) for claim_id, title, claim_type in entries)
