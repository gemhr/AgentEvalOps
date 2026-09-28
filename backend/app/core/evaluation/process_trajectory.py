"""WP4 的不可变过程证据、轨迹与 required-evidence 校验。"""

# ruff: noqa: D101, D102, D105, D415

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, model_validator

from app.core.evaluation.immutable import FrozenDict, FrozenJsonValue, freeze_json, json_compatible, require_text
from app.core.evaluation.references import CaseVersionRef, EvidenceRef, freeze_metadata
from app.core.evaluation.evidence_body_policy import parse_evidence_body_policy
from app.core.evaluation.execution import ExecutionOutcome, ExecutionTargetRef
from app.core.evaluation.generation_evidence import FINAL_ANSWER_EVIDENCE_KIND, FinalAnswerEvidenceV1
from app.core.evaluation.hitl_evidence import (
    HITL_TOOL_APPROVAL_EVIDENCE_KIND,
    HITL_TOOL_APPROVAL_EVIDENCE_SCHEMA_VERSION,
    HitlEvidenceProvenance,
    HitlToolApprovalEvidenceV1,
)
from app.core.evaluation.rag_artifact import RAG_ARTIFACT_EVIDENCE_KIND, RagEvaluationArtifactV1
from app.core.evaluation.run_attempts import EvaluationRun, ExecutionAttempt

PROCESS_TRAJECTORY_KIND = "process_trajectory"
PROCESS_TRAJECTORY_SCHEMA = "stage11.wp4.v1"
REQUIREMENTS_SCHEMA = "process-evidence-requirements.v1"
MAX_TRAJECTORY_BYTES = 1024 * 1024


class ProcessKind(StrEnum):
    PLANNING = "PLANNING"
    STEP = "STEP"
    TOOL = "TOOL"
    RETRIEVAL = "RETRIEVAL"
    MODEL = "MODEL"
    GENERATION = "GENERATION"
    RECOVERY = "RECOVERY"
    MEMORY = "MEMORY"


class Provenance(StrEnum):
    RUNTIME = "RUNTIME"
    FIXTURE = "FIXTURE"
    CALLER_EXPECTED = "CALLER_EXPECTED"
    EVALUATOR_DERIVED = "EVALUATOR_DERIVED"
    LEGACY_IMPORTED = "LEGACY_IMPORTED"


class Availability(StrEnum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    REDACTED = "REDACTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Completeness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class Sensitivity(StrEnum):
    SAFE_METADATA = "SAFE_METADATA"
    APPROVED_CONTENT = "APPROVED_CONTENT"


class EdgeRelation(StrEnum):
    CONTAINS = "CONTAINS"
    DEPENDS_ON = "DEPENDS_ON"
    CONSUMES = "CONSUMES"
    RETRY_OF = "RETRY_OF"
    FALLBACK_FROM = "FALLBACK_FROM"
    RESUMES_FROM = "RESUMES_FROM"


_PAYLOAD_FIELDS: dict[ProcessKind, frozenset[str]]
_TOOL_PHASES = frozenset({"SELECTED", "REQUESTED", "GOVERNANCE", "APPROVAL", "STARTED", "COMPLETED"})
_STEP_PHASES = frozenset({"STARTED", "COMPLETED"})


class _PayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PlanningEvidencePayloadV1(_PayloadModel):
    plan_id: StrictStr | None = None
    plan_version: StrictInt | None = None
    plan_fingerprint: StrictStr | None = None
    planning_source: StrictStr | None = None
    intent_ref: StrictStr | None = None
    intent_digest: StrictStr | None = None
    selected_step_ids: tuple[StrictStr, ...] | None = None
    task_type: StrictStr | None = None
    resolved_performer_refs: tuple[StrictStr, ...] | None = None
    dependency_step_ids: tuple[StrictStr, ...] | None = None
    synthesis_required: StrictBool | None = None
    replan_of: StrictStr | None = None
    schema_version: StrictInt | None = None
    planner_model_invoked: StrictBool | None = None
    shape: StrictStr | None = None
    specialist_count: StrictInt | None = None
    step_count: StrictInt | None = None


class StepPhase(StrEnum):
    PLANNED = "PLANNED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class StepEvidencePayloadV1(_PayloadModel):
    plan_id: StrictStr | None = None
    step_id: StrictStr | None = None
    parent_step_id: StrictStr | None = None
    phase: StepPhase | None = None
    task_type: StrictStr | None = None
    preferred_agent: StrictStr | None = None
    actual_performer_ref: StrictStr | None = None
    dependency_step_ids: tuple[StrictStr, ...] | None = None
    safe_input_ref: StrictStr | None = None
    safe_output_ref: StrictStr | None = None
    result_status: StrictStr | None = None
    error_code: StrictStr | None = None
    execution_kind: StrictStr | None = None
    output_policy: StrictStr | None = None
    state: StrictStr | None = None
    dependency_count: StrictInt | None = None
    result_char_count: StrictInt | None = None


class ToolPhase(StrEnum):
    SELECTED = "SELECTED"
    REQUESTED = "REQUESTED"
    GOVERNANCE = "GOVERNANCE"
    APPROVAL = "APPROVAL"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"


class ToolEvidencePayloadV1(_PayloadModel):
    phase: ToolPhase
    tool_name: StrictStr | None = None
    operation_id: StrictStr | None = None
    invocation_identity_digest: StrictStr | None = None
    invocation_binding_digest: StrictStr | None = None
    input_schema_ref: StrictStr | None = None
    argument_validation: StrictStr | None = None
    governance_decision: StrictStr | None = None
    approval_id: StrictStr | None = None
    approval_state: StrictStr | None = None
    result_status: StrictStr | None = None
    side_effect_state: StrictStr | None = None
    safe_result_ref: StrictStr | None = None
    safe_result_digest: StrictStr | None = None
    safe_error_code: StrictStr | None = None


class RetrievalItemV1(_PayloadModel):
    document_id: StrictStr
    chunk_id: StrictStr
    document_version: StrictStr | None = None
    rank: StrictInt | None = None
    retrieval_rank: StrictInt | None = None
    rerank_rank: StrictInt | None = None
    dense_channel_rank: StrictInt | None = None
    bm25_channel_rank: StrictInt | None = None
    rrf_fused_rank: StrictInt | None = None
    fusion_rank: StrictInt | None = None
    channel_rank: StrictInt | None = None
    score_kind: StrictStr | None = None
    content_hash: StrictStr | None = None
    provenance: StrictStr | None = None
    selection_rank: StrictInt | None = None
    citation_id: StrictStr | None = None
    context_content_hash: StrictStr | None = None


class RetrievalEvidencePayloadV1(_PayloadModel):
    retrieval_id: StrictStr
    query_digest: StrictStr | None = None
    status: StrictStr | None = None
    retrieval_status: StrictStr | None = None
    top_k: StrictInt | None = None
    retriever_identity: StrictStr | None = None
    profile_identity: StrictStr | None = None
    index_identity: StrictStr | None = None
    retrieved: tuple[RetrievalItemV1, ...] | None = None
    ranked: tuple[RetrievalItemV1, ...] | None = None
    selected: tuple[RetrievalItemV1, ...] | None = None
    retrieved_items: tuple[RetrievalItemV1, ...] | None = None
    ranked_items: tuple[RetrievalItemV1, ...] | None = None
    selected_items: tuple[RetrievalItemV1, ...] | None = None
    retrieval_strategy: StrictStr | None = None
    provenance_sha256: StrictStr | None = None
    generation_id: StrictStr | None = None
    citations: tuple[RetrievalItemV1, ...] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_status(cls, value: object) -> object:
        if isinstance(value, Mapping) and "retrieval_status" in value and "status" not in value:
            return {**value, "status": value["retrieval_status"]}
        return value


class ModelEvidencePayloadV1(_PayloadModel):
    operation_id: StrictStr | None = None
    physical_attempt_index: StrictInt | None = None
    requested_model: StrictStr | None = None
    actual_model: StrictStr | None = None
    provider: StrictStr | None = None
    profile: StrictStr | None = None
    prompt_ref: StrictStr | None = None
    prompt_version: StrictStr | None = None
    input_tokens: StrictInt | None = None
    output_tokens: StrictInt | None = None
    latency_ms: StrictInt | None = None
    cost: str | None = None
    currency: StrictStr | None = None
    stop_reason: StrictStr | None = None
    error_code: StrictStr | None = None
    circuit_decision: StrictStr | None = None


class GenerationRole(StrEnum):
    PERFORMER_OUTPUT = "PERFORMER_OUTPUT"
    SYNTHESIS_OUTPUT = "SYNTHESIS_OUTPUT"
    FINAL_DELIVERED = "FINAL_DELIVERED"
    OUTPUT_DELIVERY = "OUTPUT_DELIVERY"


class GenerationEvidencePayloadV1(_PayloadModel):
    role: GenerationRole
    step_id: StrictStr | None = None
    operation_id: StrictStr | None = None
    output_ref: StrictStr | None = None
    output_digest: StrictStr | None = None
    delivery_status: StrictStr | None = None
    upstream_refs: tuple[StrictStr, ...] | None = None


class RecoveryEvidencePayloadV1(_PayloadModel):
    checkpoint_ref: StrictStr | None = None
    checkpoint_digest: StrictStr | None = None
    resume_generation: StrictInt | None = None
    recovery_status: StrictStr | None = None


class MemoryEventType(StrEnum):
    FORMATION = "FORMATION"
    LIFECYCLE = "LIFECYCLE"
    RETRIEVAL = "RETRIEVAL"


class MemoryEvidencePayloadV1(_PayloadModel):
    event_type: MemoryEventType
    event_id: StrictStr
    sequence: StrictInt
    run_id: StrictStr
    status: StrictStr | None = None
    safe_error_code: StrictStr | None = None
    formation_method: StrictStr | None = None
    proposed_count: StrictInt | None = None
    accepted_count: StrictInt | None = None
    ignored_count: StrictInt | None = None
    persisted_count: StrictInt | None = None
    reused_count: StrictInt | None = None
    failed_count: StrictInt | None = None
    candidate_outcomes: StrictStr | None = None
    memory_type: StrictStr | None = None
    operation: StrictStr | None = None
    outcome: StrictStr | None = None
    affected_count: StrictInt | None = None
    winner_memory_id: StrictStr | None = None
    new_memory_id: StrictStr | None = None
    candidate_outcome: StrictStr | None = None
    affected_transitions: StrictStr | None = None
    retrieval_method: StrictStr | None = None
    ranking_method: StrictStr | None = None
    candidate_count: StrictInt | None = None
    eligible_count: StrictInt | None = None
    selected_count: StrictInt | None = None
    context_record_count: StrictInt | None = None
    malformed_count: StrictInt | None = None
    omitted_count: StrictInt | None = None
    registered_selected_count: StrictInt | None = None
    open_selected_count: StrictInt | None = None
    planning_injected: StrictBool | None = None
    direct_entry_supplied: StrictBool | None = None

    @model_validator(mode="after")
    def _kind_fields(self) -> "MemoryEvidencePayloadV1":
        groups = {
            MemoryEventType.FORMATION: {"formation_method", "proposed_count", "accepted_count", "ignored_count", "persisted_count", "reused_count", "failed_count", "candidate_outcomes"},
            MemoryEventType.LIFECYCLE: {"memory_type", "operation", "outcome", "affected_count", "winner_memory_id", "new_memory_id", "candidate_outcome", "affected_transitions"},
            MemoryEventType.RETRIEVAL: {"retrieval_method", "ranking_method", "candidate_count", "eligible_count", "selected_count", "context_record_count", "malformed_count", "omitted_count", "registered_selected_count", "open_selected_count", "planning_injected", "direct_entry_supplied"},
        }
        allowed = groups[self.event_type] | {"event_type", "event_id", "sequence", "run_id", "status", "safe_error_code"}
        if any(getattr(self, name) is not None for name in set(type(self).model_fields) - allowed):
            raise ValueError("memory payload fields do not match event_type")
        return self


_PAYLOAD_MODELS: dict[ProcessKind, type[_PayloadModel]] = {
    ProcessKind.PLANNING: PlanningEvidencePayloadV1,
    ProcessKind.STEP: StepEvidencePayloadV1,
    ProcessKind.TOOL: ToolEvidencePayloadV1,
    ProcessKind.RETRIEVAL: RetrievalEvidencePayloadV1,
    ProcessKind.MODEL: ModelEvidencePayloadV1,
    ProcessKind.GENERATION: GenerationEvidencePayloadV1,
    ProcessKind.RECOVERY: RecoveryEvidencePayloadV1,
    ProcessKind.MEMORY: MemoryEvidencePayloadV1,
}
_PAYLOAD_FIELDS = {kind: frozenset(model.model_fields) for kind, model in _PAYLOAD_MODELS.items()}
_REQUIREMENT_FIELDS = {
    **_PAYLOAD_FIELDS,
    ProcessKind.GENERATION: _PAYLOAD_FIELDS[ProcessKind.GENERATION] | {"output_body"},
}


def canonical_json(value: object) -> bytes:
    """按 WP4 规则生成 UTF-8 canonical JSON。"""
    return json.dumps(json_compatible(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _payload(kind: ProcessKind, value: Mapping[str, object] | _PayloadModel) -> _PayloadModel:
    if isinstance(value, _PayloadModel):
        if not isinstance(value, _PAYLOAD_MODELS[kind]):
            raise ValueError("process payload kind mismatch")
        return value

    def normalize(item: object) -> object:
        if isinstance(item, Mapping):
            return {key: normalize(nested) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return tuple(normalize(nested) for nested in item)
        return item

    if set(value) - _PAYLOAD_FIELDS[kind]:
        raise ValueError("process payload contains unsupported fields")
    try:
        normalized = normalize(value)
        if not isinstance(normalized, dict):
            raise TypeError("process payload must be a JSON object")
        data = normalized
        enum_fields = {
            ProcessKind.TOOL: {"phase": ToolPhase},
            ProcessKind.STEP: {"phase": StepPhase},
            ProcessKind.GENERATION: {"role": GenerationRole},
            ProcessKind.MEMORY: {"event_type": MemoryEventType},
        }.get(kind, {})
        for field_name, enum_type in enum_fields.items():
            if isinstance(data.get(field_name), str):
                data[field_name] = enum_type(data[field_name])
        return _PAYLOAD_MODELS[kind].model_validate(data)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid strict process payload") from exc


@dataclass(frozen=True, slots=True)
class ProcessEvidenceV1:
    schema_version: str
    evidence_id: str
    kind: ProcessKind
    producer_id: str
    provenance: Provenance
    runtime_run_id: str
    source_stream_id: str
    source_event_id: str
    projection_role: str
    source_schema_ref: str
    sensitivity: Sensitivity
    payload: _PayloadModel
    field_availability: FrozenDict = field(default_factory=FrozenDict)
    content_sha256: str = ""
    source_sequence: int | None = None
    source_timestamp: str | None = None
    step_id: str | None = None
    parent_step_id: str | None = None
    operation_id: str | None = None
    physical_attempt_index: int | None = None
    resume_generation: int | None = None

    def __post_init__(self) -> None:
        if self.schema_version != PROCESS_TRAJECTORY_SCHEMA:
            raise ValueError("unsupported ProcessEvidence schema")
        if not isinstance(self.kind, ProcessKind) or not isinstance(self.provenance, Provenance) or not isinstance(self.sensitivity, Sensitivity):
            raise ValueError("invalid ProcessEvidence enum value")
        require_text(self.evidence_id, "evidence_id")
        for name in ("producer_id", "runtime_run_id", "source_stream_id", "source_event_id", "projection_role", "source_schema_ref"):
            require_text(getattr(self, name), name)
        for name in ("step_id", "parent_step_id", "operation_id"):
            if getattr(self, name) is not None:
                require_text(getattr(self, name), name)
        if self.source_sequence is not None and self.source_sequence < 0:
            raise ValueError("source_sequence must be non-negative")
        if self.physical_attempt_index is not None and self.physical_attempt_index < 0:
            raise ValueError("physical_attempt_index must be non-negative")
        payload = _payload(self.kind, self.payload)
        availability = freeze_metadata(self.field_availability)
        if any(key not in _REQUIREMENT_FIELDS[self.kind] for key in availability):
            raise ValueError("field_availability contains unsupported field")
        if any(value not in {item.value for item in Availability} for value in availability.values()):
            raise ValueError("field_availability contains unknown availability")
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "field_availability", availability)
        if self.content_sha256 == "":
            object.__setattr__(self, "content_sha256", self.computed_digest())
        elif self.content_sha256 != self.computed_digest():
            raise ValueError("ProcessEvidence content digest mismatch")

    def computed_digest(self) -> str:
        return _digest({key: value for key, value in self.to_dict().items() if key != "content_sha256"})

    def to_dict(self) -> dict[str, object]:
        result = {
            "schema_version": self.schema_version, "evidence_id": self.evidence_id, "kind": self.kind.value,
            "producer_id": self.producer_id, "provenance": self.provenance.value, "runtime_run_id": self.runtime_run_id,
            "source_stream_id": self.source_stream_id, "source_event_id": self.source_event_id,
            "projection_role": self.projection_role, "source_schema_ref": self.source_schema_ref,
            "sensitivity": self.sensitivity.value, "payload": self.payload.model_dump(mode="json", exclude_none=True), "field_availability": self.field_availability,
            "content_sha256": self.content_sha256,
        }
        for key in ("source_sequence", "source_timestamp", "step_id", "parent_step_id", "operation_id", "physical_attempt_index", "resume_generation"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True, slots=True)
class CoverageV1:
    kind: ProcessKind
    scope: str
    availability: Availability
    completeness: Completeness | None = None
    reason_code: str | None = None
    supporting_source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProcessKind) or not isinstance(self.availability, Availability):
            raise ValueError("invalid Coverage enum value")
        if self.completeness is not None and not isinstance(self.completeness, Completeness):
            raise ValueError("invalid Coverage completeness")
        require_text(self.scope, "scope")
        if (self.availability is Availability.PRESENT) != (self.completeness is not None):
            raise ValueError("coverage completeness must be present only for PRESENT")
        if self.reason_code is not None:
            require_text(self.reason_code, "reason_code")
        object.__setattr__(self, "supporting_source_refs", tuple(self.supporting_source_refs))


@dataclass(frozen=True, slots=True)
class SourceManifestV1:
    producer_id: str
    runtime_run_id: str
    source_stream_id: str
    source_schema_ref: str
    source_terminal_confirmed: bool
    availability: Availability
    reason_code: str | None = None
    source_sequence_min: int | None = None
    source_sequence_max: int | None = None
    watermark: str | None = None
    immutable_source_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.availability, Availability) or type(self.source_terminal_confirmed) is not bool:
            raise ValueError("invalid SourceManifest status")
        for name in ("producer_id", "runtime_run_id", "source_stream_id", "source_schema_ref"):
            require_text(getattr(self, name), name)
        if self.source_sequence_min is not None and self.source_sequence_max is not None and self.source_sequence_min > self.source_sequence_max:
            raise ValueError("invalid source sequence bounds")


@dataclass(frozen=True, slots=True, order=True)
class CausalityEdgeV1:
    from_evidence_id: str
    to_evidence_id: str
    relation: EdgeRelation

    def __post_init__(self) -> None:
        if not isinstance(self.relation, EdgeRelation):
            raise ValueError("invalid causality relation")
        require_text(self.from_evidence_id, "from_evidence_id")
        require_text(self.to_evidence_id, "to_evidence_id")
        if self.from_evidence_id == self.to_evidence_id:
            raise ValueError("self causality edge is invalid")


@dataclass(frozen=True, slots=True)
class ProcessTrajectoryV1:
    schema_version: str
    trajectory_id: str
    project_id: str
    evaluation_run_id: str
    evaluation_attempt_id: str
    dataset_id: str
    case_ref: CaseVersionRef
    execution_request_id: str
    execution_target_ref: FrozenJsonValue
    frozen_subject_ref: FrozenJsonValue
    sealed: bool
    execution_partial: bool
    body_policy_ref: str | None
    source_manifests: tuple[SourceManifestV1, ...]
    coverage: tuple[CoverageV1, ...]
    records: tuple[ProcessEvidenceV1, ...]
    edges: tuple[CausalityEdgeV1, ...]
    content_sha256: str = ""
    sealed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.schema_version != PROCESS_TRAJECTORY_SCHEMA or self.sealed is not True:
            raise ValueError("invalid ProcessTrajectory schema or seal state")
        if type(self.execution_partial) is not bool or not isinstance(self.case_ref, CaseVersionRef):
            raise ValueError("invalid ProcessTrajectory binding types")
        if any(not isinstance(item, ProcessEvidenceV1) for item in self.records):
            raise ValueError("invalid ProcessTrajectory record type")
        if any(not isinstance(item, (CoverageV1, SourceManifestV1, CausalityEdgeV1))
               for group in (self.coverage, self.source_manifests, self.edges) for item in group):
            raise ValueError("invalid ProcessTrajectory component type")
        if self.body_policy_ref is not None:
            require_text(self.body_policy_ref, "body_policy_ref")
        if self.trajectory_id != f"trajectory://{self.evaluation_attempt_id}":
            raise ValueError("trajectory_id must bind canonical Attempt")
        for name in ("project_id", "evaluation_run_id", "evaluation_attempt_id", "dataset_id", "execution_request_id"):
            require_text(getattr(self, name), name)
        if self.sealed_at is not None and (self.sealed_at.tzinfo is None or self.sealed_at.utcoffset() is None):
            raise ValueError("sealed_at must be timezone-aware")
        records = tuple(sorted(self.records, key=lambda item: item.evidence_id))
        edges = tuple(sorted(self.edges, key=lambda item: (item.from_evidence_id, item.to_evidence_id, item.relation.value)))
        ids = [record.evidence_id for record in records]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate evidence identity")
        if any(record.evidence_id != evidence_id(
            self.evaluation_attempt_id, record.producer_id, record.runtime_run_id,
            record.source_stream_id, record.source_event_id, record.projection_role,
        ) for record in records):
            raise ValueError("ProcessEvidence identity is not bound to canonical Attempt")
        id_set = set(ids)
        graph: dict[str, list[str]] = {item: [] for item in ids}
        for edge in edges:
            if edge.from_evidence_id not in id_set or edge.to_evidence_id not in id_set:
                raise ValueError("causality edge target is unresolved")
            graph[edge.from_evidence_id].append(edge.to_evidence_id)
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("causality cycle")
            if node in visited:
                return
            visiting.add(node)
            for child in graph[node]:
                visit(child)
            visiting.remove(node)
            visited.add(node)
        for node in graph:
            visit(node)
        object.__setattr__(self, "execution_target_ref", freeze_json(self.execution_target_ref))
        object.__setattr__(self, "frozen_subject_ref", freeze_json(self.frozen_subject_ref))
        object.__setattr__(self, "source_manifests", tuple(self.source_manifests))
        object.__setattr__(self, "coverage", tuple(sorted(self.coverage, key=lambda item: (item.kind.value, item.scope))))
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "edges", edges)
        if self.content_sha256 == "":
            object.__setattr__(self, "content_sha256", self.computed_digest())
        elif self.content_sha256 != self.computed_digest():
            raise ValueError("ProcessTrajectory content digest mismatch")
        if len(canonical_json(self.to_dict(include_digest=False))) > MAX_TRAJECTORY_BYTES:
            raise ValueError("trajectory exceeds inline size limit")

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version, "trajectory_id": self.trajectory_id, "project_id": self.project_id,
            "evaluation_run_id": self.evaluation_run_id, "evaluation_attempt_id": self.evaluation_attempt_id,
            "dataset_id": self.dataset_id, "case_ref": {"case_id": self.case_ref.case_id, "version": self.case_ref.version},
            "execution_request_id": self.execution_request_id, "execution_target_ref": self.execution_target_ref,
            "frozen_subject_ref": self.frozen_subject_ref, "sealed": self.sealed, "execution_partial": self.execution_partial,
            "body_policy_ref": self.body_policy_ref,
            "source_manifests": [manifest.__dict__ if hasattr(manifest, "__dict__") else {name: getattr(manifest, name) for name in manifest.__dataclass_fields__} for manifest in self.source_manifests],
            "coverage": [{"kind": item.kind.value, "scope": item.scope, "availability": item.availability.value, "completeness": item.completeness.value if item.completeness else None, "reason_code": item.reason_code, "supporting_source_refs": item.supporting_source_refs} for item in self.coverage],
            "records": [item.to_dict() for item in self.records],
            "edges": [{"from_evidence_id": edge.from_evidence_id, "to_evidence_id": edge.to_evidence_id, "relation": edge.relation.value} for edge in self.edges],
        }
        if include_digest:
            result["content_sha256"] = self.content_sha256
        return result

    def computed_digest(self) -> str:
        return _digest(self.to_dict(include_digest=False))

    def as_evidence_ref(self) -> EvidenceRef:
        return EvidenceRef(kind=PROCESS_TRAJECTORY_KIND, identifier=self.trajectory_id, schema_version=PROCESS_TRAJECTORY_SCHEMA, metadata={"payload": self.to_dict()})


def evidence_id(attempt_id: str, producer_id: str, runtime_run_id: str, source_stream_id: str, source_event_id: str, projection_role: str) -> str:
    """生成稳定 occurrence identity。"""
    return "pe://" + _digest([attempt_id, producer_id, runtime_run_id, source_stream_id, source_event_id, projection_role])


def build_process_trajectory(**values: object) -> ProcessTrajectoryV1:
    """构建并封存规范排序、带内容摘要的轨迹。"""
    data = dict(values)
    records = tuple(data.get("records", ()))
    record_ids = {record.evidence_id for record in records}
    edges = tuple(data.get("edges", ()))
    if any(edge.from_evidence_id not in record_ids or edge.to_evidence_id not in record_ids for edge in edges):
        data["edges"] = tuple(edge for edge in edges if edge.from_evidence_id in record_ids and edge.to_evidence_id in record_ids)
        data["coverage"] = tuple(
            replace(item, completeness=Completeness.PARTIAL, reason_code="UNRESOLVED_CAUSALITY_TARGET")
            if item.availability is Availability.PRESENT else item
            for item in data.get("coverage", ())
        )
    try:
        trajectory = ProcessTrajectoryV1(**data)  # type: ignore[arg-type]
    except ValueError as exc:
        if "inline size limit" not in str(exc):
            raise
        present_kinds = {item.kind for item in data.get("coverage", ()) if item.availability is Availability.PRESENT}
        data["records"] = ()
        data["edges"] = ()
        data["source_manifests"] = ()
        data["coverage"] = tuple(
            replace(item, completeness=Completeness.PARTIAL, reason_code="SIZE_LIMIT")
            if item.kind in present_kinds else item
            for item in data.get("coverage", ())
        )
        trajectory = ProcessTrajectoryV1(**data)  # type: ignore[arg-type]
    return replace(trajectory, content_sha256=trajectory.computed_digest())


def build_process_evidence(**values: object) -> ProcessEvidenceV1:
    """构建带稳定 occurrence identity 与内容摘要的证据记录。"""
    data = dict(values)
    attempt_id = str(data.pop("evaluation_attempt_id"))
    data["evidence_id"] = evidence_id(
        attempt_id,
        str(data["producer_id"]),
        str(data["runtime_run_id"]),
        str(data["source_stream_id"]),
        str(data["source_event_id"]),
        str(data["projection_role"]),
    )
    record = ProcessEvidenceV1(**data)  # type: ignore[arg-type]
    return replace(record, content_sha256=record.computed_digest())


@dataclass(frozen=True, slots=True)
class ProcessEvidenceRequirementV1:
    kind: ProcessKind
    fields: tuple[str, ...]
    accepted_provenance: tuple[Provenance, ...]
    allow_partial: bool = False
    allow_not_applicable: bool = False
    phases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.phases and self.kind not in {ProcessKind.TOOL, ProcessKind.STEP}:
            raise ValueError("phases are supported only for TOOL and STEP")
        allowed = _REQUIREMENT_FIELDS[self.kind]
        for path in self.fields:
            if path not in allowed:
                raise ValueError("unknown required process field")
        supported_phases = _TOOL_PHASES if self.kind is ProcessKind.TOOL else _STEP_PHASES
        if any(phase not in supported_phases for phase in self.phases):
            raise ValueError("unknown required process phase")
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "phases", tuple(self.phases))
        object.__setattr__(self, "accepted_provenance", tuple(self.accepted_provenance))


def validate_process_requirements(trajectory: ProcessTrajectoryV1, requirements: tuple[ProcessEvidenceRequirementV1, ...]) -> str | None:
    """验证 requirements 中的全部 AND 条件，返回稳定错误码或 None。"""
    if not requirements:
        return "REQUIRED_PROCESS_EVIDENCE_MISSING"
    records = {item.evidence_id: item for item in trajectory.records}
    for requirement in requirements:
        scopes = [item for item in trajectory.coverage if item.kind is requirement.kind]
        if not scopes:
            return "REQUIRED_PROCESS_EVIDENCE_MISSING"
        if any(item.availability is Availability.UNAVAILABLE for item in scopes):
            return "REQUIRED_PROCESS_EVIDENCE_UNAVAILABLE"
        if any(item.availability is Availability.REDACTED for item in scopes):
            return "REQUIRED_PROCESS_EVIDENCE_REDACTED"
        if any(item.availability is Availability.NOT_APPLICABLE for item in scopes) and not requirement.allow_not_applicable:
            return "REQUIRED_PROCESS_EVIDENCE_UNAVAILABLE"
        if any(item.availability not in {Availability.PRESENT, Availability.NOT_APPLICABLE} for item in scopes):
            return "REQUIRED_PROCESS_EVIDENCE_MISSING"
        if all(item.availability is Availability.NOT_APPLICABLE for item in scopes):
            return None
        scopes = [item for item in scopes if item.availability is Availability.PRESENT]
        if any(item.completeness is Completeness.PARTIAL for item in scopes) and not requirement.allow_partial:
            return "REQUIRED_PROCESS_EVIDENCE_INVALID"
        matched = [record for record in records.values() if record.kind is requirement.kind]
        if requirement.phases:
            matched = [record for record in matched if getattr(record.payload, "phase", None) in requirement.phases]
        if (not matched and (requirement.fields or any(item.completeness is Completeness.PARTIAL for item in scopes))) or any(record.provenance not in requirement.accepted_provenance for record in matched):
            return "REQUIRED_PROCESS_EVIDENCE_INVALID"
        for record in matched:
            for path in requirement.fields:
                field_state = record.field_availability.get(path)
                if field_state == Availability.REDACTED.value:
                    return "REQUIRED_PROCESS_EVIDENCE_REDACTED"
                if field_state == Availability.UNAVAILABLE.value:
                    return "REQUIRED_PROCESS_EVIDENCE_UNAVAILABLE"
                if path == "output_body" and record.kind is ProcessKind.GENERATION:
                    if field_state != Availability.PRESENT.value:
                        return "REQUIRED_PROCESS_EVIDENCE_MISSING"
                    continue
                if path not in record.payload.model_fields_set:
                    return "REQUIRED_PROCESS_EVIDENCE_INVALID"
    return None


def parse_process_requirements(value: object) -> tuple[ProcessEvidenceRequirementV1, ...]:
    """严格解析 EvaluatorSpec.config_snapshot 中的 requirements。"""
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "trajectory_schema_version", "requirements"}:
        raise ValueError("invalid process_evidence_requirements schema")
    if value["schema_version"] != REQUIREMENTS_SCHEMA or value["trajectory_schema_version"] != PROCESS_TRAJECTORY_SCHEMA:
        raise ValueError("unsupported process requirements schema version")
    items = value["requirements"]
    if not isinstance(items, (list, tuple)):
        raise ValueError("invalid process requirements list")
    result: list[ProcessEvidenceRequirementV1] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("invalid process requirement")
        if set(item) - {"kind", "phases", "fields", "accepted_provenance", "allow_partial", "allow_not_applicable"}:
            raise ValueError("unknown process requirement field")
        if not {"kind", "fields", "accepted_provenance", "allow_partial", "allow_not_applicable"} <= set(item):
            raise ValueError("incomplete process requirement")
        if type(item["allow_partial"]) is not bool or type(item["allow_not_applicable"]) is not bool:
            raise ValueError("invalid process requirement flags")
        fields, phases, provenance = item["fields"], item.get("phases", ()), item["accepted_provenance"]
        if not all(isinstance(part, (list, tuple)) for part in (fields, phases, provenance)):
            raise ValueError("invalid process requirement lists")
        if any(not isinstance(part, str) for part in (*fields, *phases, *provenance)):
            raise ValueError("invalid process requirement enum")
        result.append(ProcessEvidenceRequirementV1(
            kind=ProcessKind(item["kind"]), fields=tuple(fields), phases=tuple(phases),
            accepted_provenance=tuple(Provenance(part) for part in provenance),
            allow_partial=item["allow_partial"], allow_not_applicable=item["allow_not_applicable"],
        ))
    if not result:
        raise ValueError("empty process requirements cannot validate process evidence")
    return tuple(result)


def parse_process_trajectory(reference: EvidenceRef) -> ProcessTrajectoryV1:
    """严格验证 EvidenceRef envelope、typed snapshot 与两层 digest。"""
    if reference.kind != PROCESS_TRAJECTORY_KIND or reference.schema_version != PROCESS_TRAJECTORY_SCHEMA:
        raise ValueError("unsupported process trajectory evidence reference")
    payload = reference.metadata.get("payload")
    expected = {
        "schema_version", "trajectory_id", "project_id", "evaluation_run_id", "evaluation_attempt_id", "dataset_id",
        "case_ref", "execution_request_id", "execution_target_ref", "frozen_subject_ref", "sealed", "execution_partial",
        "body_policy_ref", "source_manifests", "coverage", "records", "edges", "content_sha256",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected or payload["trajectory_id"] != reference.identifier:
        raise ValueError("invalid process trajectory payload")
    records: list[ProcessEvidenceV1] = []
    for item in payload["records"]:
        required = {"schema_version", "evidence_id", "kind", "producer_id", "provenance", "runtime_run_id", "source_stream_id", "source_event_id", "projection_role", "source_schema_ref", "sensitivity", "payload", "field_availability", "content_sha256"}
        optional = {"source_sequence", "source_timestamp", "step_id", "parent_step_id", "operation_id", "physical_attempt_index", "resume_generation"}
        if not isinstance(item, Mapping) or not required <= set(item) or set(item) - required - optional:
            raise ValueError("invalid process evidence record")
        if not isinstance(item["content_sha256"], str) or not item["content_sha256"]:
            raise ValueError("ProcessEvidence content digest is required")
        records.append(ProcessEvidenceV1(
            schema_version=item["schema_version"], evidence_id=item["evidence_id"], kind=ProcessKind(item["kind"]),
            producer_id=item["producer_id"], provenance=Provenance(item["provenance"]), runtime_run_id=item["runtime_run_id"],
            source_stream_id=item["source_stream_id"], source_event_id=item["source_event_id"], projection_role=item["projection_role"],
            source_schema_ref=item["source_schema_ref"], sensitivity=Sensitivity(item["sensitivity"]), payload=item["payload"],
            field_availability=item["field_availability"], content_sha256=item["content_sha256"],
            **{key: item[key] for key in optional if key in item},
        ))
    coverage = tuple(CoverageV1(
        kind=ProcessKind[item["kind"]], scope=item["scope"], availability=Availability[item["availability"]],
        completeness=Completeness[item["completeness"]] if item["completeness"] else None,
        reason_code=item["reason_code"], supporting_source_refs=tuple(item["supporting_source_refs"]),
    ) for item in payload["coverage"])
    manifests = tuple(SourceManifestV1(
        producer_id=item["producer_id"], runtime_run_id=item["runtime_run_id"], source_stream_id=item["source_stream_id"],
        source_schema_ref=item["source_schema_ref"], source_terminal_confirmed=item["source_terminal_confirmed"],
        availability=Availability[item["availability"]], reason_code=item["reason_code"],
        source_sequence_min=item["source_sequence_min"], source_sequence_max=item["source_sequence_max"],
        watermark=item["watermark"], immutable_source_ref=item["immutable_source_ref"],
    ) for item in payload["source_manifests"])
    edges = tuple(CausalityEdgeV1(item["from_evidence_id"], item["to_evidence_id"], EdgeRelation[item["relation"]]) for item in payload["edges"])
    case = payload["case_ref"]
    if not isinstance(payload["content_sha256"], str) or not payload["content_sha256"]:
        raise ValueError("ProcessTrajectory content digest is required")
    trajectory = ProcessTrajectoryV1(
        schema_version=payload["schema_version"], trajectory_id=payload["trajectory_id"], project_id=payload["project_id"],
        evaluation_run_id=payload["evaluation_run_id"], evaluation_attempt_id=payload["evaluation_attempt_id"],
        dataset_id=payload["dataset_id"], case_ref=CaseVersionRef(case["case_id"], case["version"]),
        execution_request_id=payload["execution_request_id"], execution_target_ref=payload["execution_target_ref"],
        frozen_subject_ref=payload["frozen_subject_ref"], sealed=payload["sealed"], execution_partial=payload["execution_partial"],
        body_policy_ref=payload["body_policy_ref"], source_manifests=manifests, coverage=coverage, records=tuple(records),
        edges=edges, content_sha256=payload["content_sha256"],
    )
    return trajectory


def thin_process_trajectory_ref(trajectory: ProcessTrajectoryV1) -> EvidenceRef:
    """为 Result/Comparison 生成不含过程正文的最小支持引用。"""
    provenance = tuple(sorted({record.provenance.value for record in trajectory.records}))
    availability = "PRESENT" if any(item.availability is Availability.PRESENT for item in trajectory.coverage) else "UNAVAILABLE"
    return EvidenceRef(
        kind=PROCESS_TRAJECTORY_KIND,
        identifier=trajectory.trajectory_id,
        schema_version=PROCESS_TRAJECTORY_SCHEMA,
        metadata={
            "content_sha256": trajectory.content_sha256,
            "evaluation_attempt_id": trajectory.evaluation_attempt_id,
            "accepted_provenance": provenance,
            "availability": availability,
            "policy_ref": trajectory.body_policy_ref,
        },
    )


def merge_process_evidence(records: tuple[ProcessEvidenceV1, ...]) -> tuple[ProcessEvidenceV1, ...]:
    """精确重放去重；相同 occurrence identity 的内容冲突时拒绝。"""
    unique: dict[str, ProcessEvidenceV1] = {}
    for record in records:
        previous = unique.get(record.evidence_id)
        if previous is not None and previous.computed_digest() != record.computed_digest():
            raise ValueError("IDENTITY_CONFLICT")
        unique[record.evidence_id] = record
    return tuple(unique[key] for key in sorted(unique))


def seal_attempt_trajectory(
    run: EvaluationRun,
    attempt: ExecutionAttempt,
    target_ref: ExecutionTargetRef,
    outcome: ExecutionOutcome,
    *,
    sealed_at: datetime,
) -> EvidenceRef:
    """从已验证专项 DTO 投影安全事实，并绑定当前 canonical Attempt。"""
    if attempt.run_id != run.run_id or attempt.project_id != run.project_id or outcome.request_id != attempt.execution_request.request_id:
        raise ValueError("trajectory canonical binding mismatch")
    if target_ref.target_kind == "LOCALAGENT_HTTP":
        provenance, producer = Provenance.RUNTIME, "localagent.http.evaluation_v2"
    elif target_ref.target_kind == "FIXTURE":
        provenance, producer = Provenance.FIXTURE, "evaluation.fixture"
    else:
        provenance, producer = Provenance.LEGACY_IMPORTED, "evaluation.unknown_source"
    runtime_run_id = attempt.execution_request.attempt_id
    records: list[ProcessEvidenceV1] = []
    manifests: list[SourceManifestV1] = []
    coverage: list[CoverageV1] = []
    present: set[ProcessKind] = set()
    partial_kinds: set[ProcessKind] = set()
    hitl_complete = False
    body_policy = parse_evidence_body_policy(run.suite_snapshot)
    for reference in outcome.evidence_refs:
        source_payload = reference.metadata.get("payload")
        try:
            if reference.kind == RAG_ARTIFACT_EVIDENCE_KIND and isinstance(source_payload, Mapping):
                artifact = RagEvaluationArtifactV1.model_validate(source_payload)
                if artifact.run_id != runtime_run_id or artifact.attempt_id != runtime_run_id:
                    raise ValueError("RAG source binding mismatch")
                retrieved = tuple({key: value for key, value in {
                    "document_id": item.document_id, "chunk_id": item.chunk_id, "rank": item.rank,
                    "retrieval_rank": item.retrieval_rank, "rerank_rank": item.rerank_rank,
                    "dense_channel_rank": item.dense_channel_rank, "bm25_channel_rank": item.bm25_channel_rank,
                    "rrf_fused_rank": item.rrf_fused_rank, "score_kind": item.retrieval_score_kind,
                    "content_hash": item.content_hash,
                }.items() if value is not None} for item in artifact.retrieved_items)
                ranked = tuple({key: value for key, value in {
                    "document_id": item.document_id, "chunk_id": item.chunk_id, "rank": item.rank,
                    "retrieval_rank": item.retrieval_rank, "rerank_rank": item.rerank_rank,
                    "dense_channel_rank": item.dense_channel_rank, "bm25_channel_rank": item.bm25_channel_rank,
                    "rrf_fused_rank": item.rrf_fused_rank, "score_kind": item.retrieval_score_kind,
                    "content_hash": item.content_hash,
                }.items() if value is not None} for item in artifact.ranked_items)
                selected = tuple({
                    "document_id": item.document_id, "chunk_id": item.chunk_id, "selection_rank": item.selection_rank,
                    "citation_id": item.citation_id, "context_content_hash": item.context_content_hash,
                } for item in artifact.selected_items)
                retrieval_payload: dict[str, object] = {
                    "retrieval_id": artifact.retrieval_id,
                    "retrieval_status": artifact.retrieval_status,
                    "retrieved_items": retrieved,
                    "ranked_items": ranked,
                    "selected_items": selected,
                }
                field_availability: dict[str, str] = {
                    "citations": Availability.UNAVAILABLE.value,
                    "top_k": Availability.UNAVAILABLE.value,
                }
                if artifact.query_digest is not None:
                    retrieval_payload["query_digest"] = artifact.query_digest
                else:
                    retrieval_payload["query_digest"] = _digest(artifact.query)
                for name in ("retrieval_strategy", "provenance_sha256", "generation_id"):
                    value = getattr(artifact, name)
                    if value is not None:
                        retrieval_payload[name] = value
                    else:
                        field_availability[name] = Availability.UNAVAILABLE.value
                identity = build_process_evidence(
                    evaluation_attempt_id=str(attempt.attempt_id), schema_version=PROCESS_TRAJECTORY_SCHEMA,
                    kind=ProcessKind.RETRIEVAL, producer_id=producer, provenance=provenance,
                    runtime_run_id=runtime_run_id, source_stream_id="rag.artifacts", source_event_id=artifact.artifact_id,
                    projection_role="retrieval.snapshot", source_schema_ref=artifact.schema_version,
                    sensitivity=Sensitivity.SAFE_METADATA,
                    payload=retrieval_payload,
                    field_availability=field_availability,
                )
                records.append(identity)
                capture_status = reference.metadata.get("capture_status")
                present.add(ProcessKind.RETRIEVAL)
                if capture_status != "COMPLETE":
                    partial_kinds.add(ProcessKind.RETRIEVAL)
                manifests.append(SourceManifestV1(producer, runtime_run_id, "rag.artifacts", artifact.schema_version,
                                                  capture_status == "COMPLETE", Availability.PRESENT,
                                                  None if capture_status == "COMPLETE" else "SOURCE_PARTIAL"))
            elif reference.kind == FINAL_ANSWER_EVIDENCE_KIND and isinstance(source_payload, Mapping):
                answer = FinalAnswerEvidenceV1.model_validate(source_payload)
                if answer.run_id != runtime_run_id or answer.attempt_id != runtime_run_id:
                    raise ValueError("generation source binding mismatch")
                records.append(build_process_evidence(
                    evaluation_attempt_id=str(attempt.attempt_id), schema_version=PROCESS_TRAJECTORY_SCHEMA,
                    kind=ProcessKind.GENERATION, producer_id=producer, provenance=provenance,
                    runtime_run_id=runtime_run_id, source_stream_id="output.delivery", source_event_id=answer.evidence_id,
                    projection_role="generation.final_delivered", source_schema_ref=answer.schema_version,
                    sensitivity=Sensitivity.SAFE_METADATA,
                    payload={"role": "FINAL_DELIVERED", "output_digest": answer.content_sha256},
                    field_availability={
                        "output_ref": Availability.REDACTED.value,
                        "output_body": (
                            Availability.PRESENT.value
                            if "final_answer.body" in body_policy.approved_fields
                            else Availability.REDACTED.value
                        ),
                    },
                ))
                present.add(ProcessKind.GENERATION)
                manifests.append(SourceManifestV1(producer, runtime_run_id, "output.delivery", answer.schema_version,
                                                  True, Availability.PRESENT))
            elif reference.kind == HITL_TOOL_APPROVAL_EVIDENCE_KIND and isinstance(source_payload, Mapping):
                hitl = HitlToolApprovalEvidenceV1.model_validate(source_payload)
                if hitl.run_id != runtime_run_id or reference.identifier != hitl.evidence_id:
                    raise ValueError("HITL source binding mismatch")
                if hitl.provenance is HitlEvidenceProvenance.REAL_LOCALAGENT_EVIDENCE and target_ref.target_kind == "LOCALAGENT_HTTP":
                    event_provenance = Provenance.RUNTIME
                elif hitl.provenance is HitlEvidenceProvenance.DETERMINISTIC_TEST_EVIDENCE and target_ref.target_kind == "FIXTURE":
                    event_provenance = Provenance.FIXTURE
                else:
                    event_provenance = Provenance.CALLER_EXPECTED
                for event in hitl.events:
                    phase = {
                        "TOOL_APPROVAL_REQUESTED": "APPROVAL", "TOOL_APPROVAL_DECIDED": "APPROVAL",
                        "TOOL_STARTED": "STARTED", "TOOL_COMPLETED": "COMPLETED",
                    }[event.event_type.value]
                    payload: dict[str, object] = {"phase": phase}
                    for key in ("tool_name", "approval_id", "invocation_identity_digest", "invocation_binding_digest"):
                        value = getattr(event, key)
                        if value is not None:
                            payload[key] = value
                    if event.decision_status is not None:
                        payload["approval_state"] = event.decision_status.value
                    records.append(build_process_evidence(
                        evaluation_attempt_id=str(attempt.attempt_id), schema_version=PROCESS_TRAJECTORY_SCHEMA,
                        kind=ProcessKind.TOOL, producer_id=producer, provenance=event_provenance,
                        runtime_run_id=runtime_run_id, source_stream_id="hitl.journal",
                        source_event_id=f"sequence:{event.sequence}", projection_role=f"tool.{event.event_type.value.lower()}",
                        source_schema_ref=HITL_TOOL_APPROVAL_EVIDENCE_SCHEMA_VERSION,
                        sensitivity=Sensitivity.SAFE_METADATA, payload=payload,
                        field_availability={"result_status": Availability.UNAVAILABLE.value,
                                            "side_effect_state": Availability.UNAVAILABLE.value},
                        source_sequence=event.sequence, step_id=event.step_id,
                        operation_id=event.invocation_identity_digest,
                    ))
                present.add(ProcessKind.TOOL)
                if not hitl.trace_complete:
                    partial_kinds.add(ProcessKind.TOOL)
                hitl_complete = hitl_complete or hitl.trace_complete
                manifests.append(SourceManifestV1(producer, runtime_run_id, "hitl.journal",
                                                  HITL_TOOL_APPROVAL_EVIDENCE_SCHEMA_VERSION,
                                                  hitl.trace_complete, Availability.PRESENT,
                                                  None if hitl.trace_complete else "SOURCE_PARTIAL"))
        except (TypeError, ValueError):
            continue
    capture_partial = any(manifest.availability is Availability.PRESENT and not manifest.source_terminal_confirmed for manifest in manifests)
    for kind in ProcessKind:
        if kind is ProcessKind.TOOL and hitl_complete:
            present.add(kind)
        if kind in present:
            coverage.append(CoverageV1(kind, "attempt", Availability.PRESENT,
                                       Completeness.PARTIAL if kind in partial_kinds or kind is ProcessKind.RETRIEVAL and capture_partial else Completeness.COMPLETE,
                                       reason_code="SOURCE_PARTIAL" if kind in partial_kinds or kind is ProcessKind.RETRIEVAL and capture_partial else None,
                                       supporting_source_refs=tuple(sorted({m.producer_id for m in manifests}))))
        else:
            reason = "SOURCE_NOT_AVAILABLE" if kind in {
                ProcessKind.PLANNING, ProcessKind.STEP, ProcessKind.MEMORY,
            } else "PRODUCER_UNSUPPORTED"
            coverage.append(CoverageV1(kind, "attempt", Availability.UNAVAILABLE,
                                       reason_code=reason))
    frozen_subject = run.subject_ref if run.subject_ref is not None else {"availability": "UNAVAILABLE", "reason_code": "SOURCE_UNAVAILABLE"}
    snapshot_values: dict[str, object] = dict(
        schema_version=PROCESS_TRAJECTORY_SCHEMA, trajectory_id=f"trajectory://{attempt.attempt_id}",
        project_id=str(run.project_id), evaluation_run_id=str(run.run_id), evaluation_attempt_id=str(attempt.attempt_id),
        dataset_id=str(run.dataset_snapshot["dataset_id"]), case_ref=attempt.case_ref,
        execution_request_id=attempt.execution_request.request_id,
        execution_target_ref={"target_id": target_ref.target_id, "target_kind": target_ref.target_kind,
                              "target_version_ref": target_ref.target_version_ref.opaque_value if target_ref.target_version_ref else None},
        frozen_subject_ref=frozen_subject, sealed=True, execution_partial=outcome.kind.value != "SUCCESS",
        body_policy_ref=body_policy.identity_ref,
        source_manifests=tuple(manifests), coverage=tuple(coverage),
        records=merge_process_evidence(tuple(records)), edges=(), sealed_at=sealed_at,
    )
    try:
        snapshot = build_process_trajectory(**snapshot_values)
    except ValueError as exc:
        if "inline size limit" not in str(exc):
            raise
        present_kinds = {item.kind for item in coverage if item.availability is Availability.PRESENT}
        snapshot_values["records"] = ()
        snapshot_values["edges"] = ()
        snapshot_values["source_manifests"] = ()
        snapshot_values["coverage"] = tuple(
            CoverageV1(
                kind=item.kind, scope=item.scope, availability=item.availability,
                completeness=Completeness.PARTIAL if item.kind in present_kinds else None,
                reason_code="SIZE_LIMIT" if item.kind in present_kinds else item.reason_code,
                supporting_source_refs=(),
            ) for item in coverage
        )
        snapshot = build_process_trajectory(**snapshot_values)
    return snapshot.as_evidence_ref()
