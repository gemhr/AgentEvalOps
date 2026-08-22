"""Security Evaluation Evidence —— 将 Security Ground Truth 与真实 Execution Evidence 投影为统一输入。

本模块是 WP3 的窄契约层：只消费已经存在的真实 Evidence（final answer / RAG artifact /
runtime terminal facts），严格解析、校验 identity、投影并按四态语义分类可用性。
它不做任何判定：不输出 score / verdict / attack_success，不调用 Judge，不重跑 Retrieval，
不调用 Agent，不访问 DB / HTTP。Security Evaluator（WP4）只消费本模块输出的
``SecurityEvaluationInput``。

核心原则：

- Ground Truth Authority 唯一来源是 ``EvaluationCase -> GroundTruth.security``；
  禁止从 metadata / case name / prompt 文本重新推断 security 语义。
- ``case_input`` 是 Dataset 声明的 requested/synthetic 测试刺激，不是 Runtime Evidence；
  二者在 ``SecurityEvaluationInput`` 中保持分离。
- ``RagEvaluationArtifactV1`` 中 retrieved / ranked / selected 是三个不同事实：
  ``rag_context`` 只投影 ``selected_items``；retrieved/ranked 仅在 ``retrieval_evidence`` 保留。
- Missing / Empty / Unsupported 使用 ``EvidenceAvailability`` 四态显式区分，不混成 None。
"""

# ruff: noqa: D415

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from app.core.evaluation.dataset import (
    AttackSource,
    AttackType,
    EvaluationCase,
    ExpectedSecurityBehavior,
    SecurityCaseKind,
    SecurityGroundTruth,
    Severity,
)
from app.core.evaluation.execution import ExecutionOutcome, OutcomeKind
from app.core.evaluation.generation_evidence import (
    FINAL_ANSWER_EVIDENCE_KIND,
    FinalAnswerEvidenceV1,
)
from app.core.evaluation.generation_judge import SelectedContextItem
from app.core.evaluation.immutable import FrozenDict, FrozenJsonValue, freeze_json
from app.core.evaluation.rag_artifact import (
    RAG_ARTIFACT_EVIDENCE_KIND,
    RagEvaluationArtifactV1,
)
from app.core.evaluation.references import EvidenceRef

SECURITY_EVALUATION_INPUT_SCHEMA_VERSION = "security-evaluation-input.v1"

_LOCALAGENT_RUN_PREFIX = "localagent-run://"

# TOOL_OUTPUT / AGENT_MESSAGE 当前不存在生产 evaluation evidence exporter，
# 因此相关 EvidenceKind 恒为 UNSUPPORTED。这是 Known Contract Gap，不是可运行状态。
_EVIDENCE_GAP_REASON = (
    "runtime evaluation evidence is not exported yet; "
    "dataset case_input only declares the test stimulus"
)


class EvidenceAvailability(StrEnum):
    """Evidence 的严格可用性语义（四态，不创建更多状态）。"""

    AVAILABLE = "AVAILABLE"
    KNOWN_EMPTY = "KNOWN_EMPTY"
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"


class EvidenceKind(StrEnum):
    """Security Evaluation 关注的 evidence 类别。"""

    CASE_INPUT = "CASE_INPUT"
    ACTUAL_ANSWER = "ACTUAL_ANSWER"
    RAG_CONTEXT = "RAG_CONTEXT"
    RETRIEVAL = "RETRIEVAL"
    CITATION = "CITATION"
    TOOL_OUTPUT = "TOOL_OUTPUT"
    AGENT_MESSAGE = "AGENT_MESSAGE"
    JUDGE_REFERENCE = "JUDGE_REFERENCE"


class SecurityEvaluationInputError(ValueError):
    """Evidence integrity / identity 违规 —— builder fail closed。"""


@dataclass(frozen=True, slots=True)
class RuntimeTerminalEvidence:
    """从 ExecutionOutcome 真实 terminal facts 投影的窄结构。

    ``safe_message`` 只是 terminal projection（bounded reason 文本），
    绝不是 actual answer；actual answer 唯一权威是 final-answer evidence。
    """

    outcome_kind: OutcomeKind
    stop_reason: str | None = None
    error_code: str | None = None
    safe_message: str | None = None


@dataclass(frozen=True, slots=True)
class ActualAnswerEvidence:
    """唯一权威为 EvidenceRef(kind="final_answer") 的 final answer evidence。"""

    availability: EvidenceAvailability
    content: str | None = None
    evidence_id: str | None = None
    schema_version: str | None = None
    media_type: str | None = None
    content_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RagContextEvidence:
    """实际进入 Context 的 selected_items 投影（按 (invocation_index, selection_rank) 排序）。"""

    availability: EvidenceAvailability
    items: tuple[SelectedContextItem, ...] = ()
    artifact_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievedItemIdentity:
    """retrieved / ranked 候选的身份与排序投影；selected 事实单独由 rag_context 承载。"""

    artifact_id: str
    retrieval_id: str
    invocation_index: int
    document_id: str
    chunk_id: str
    rank: int
    retrieval_rank: int
    selected: bool
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalEvidence:
    """retrieved / ranked 候选集合；与 selected（进入 Context）严格区分。"""

    availability: EvidenceAvailability
    retrieved: tuple[RetrievedItemIdentity, ...] = ()
    ranked: tuple[RetrievedItemIdentity, ...] = ()


@dataclass(frozen=True, slots=True)
class CitationItemProjection:
    """从 RAG artifact citations 投影的 citation identity。"""

    citation_id: str
    document_id: str
    chunk_id: str
    context_block_id: str
    context_content_hash: str
    selected: bool


@dataclass(frozen=True, slots=True)
class CitationEvidence:
    """Citation identity 投影；Answer↔Citation binding 无 contract，显式 UNAVAILABLE。"""

    availability: EvidenceAvailability
    items: tuple[CitationItemProjection, ...] = ()
    answer_to_citation_binding: EvidenceAvailability = EvidenceAvailability.UNAVAILABLE
    answer_to_citation_binding_reason: str | None = None


@dataclass(frozen=True, slots=True)
class JudgeFacingEvidence:
    """后续 Security Evaluator / Judge 真正会看到的输入投影（DERIVABLE_WITHOUT_RERUN）。"""

    availability: EvidenceAvailability
    question: str | None = None
    actual_answer: str | None = None
    reference_answer: str | None = None
    context: tuple[SelectedContextItem, ...] = ()


@dataclass(frozen=True, slots=True)
class AttackSourceRequirement:
    """一个 attack source 对某类 evidence 的 requirement 及其实际状态。"""

    evidence_kind: EvidenceKind
    status: EvidenceAvailability
    required: bool


# Attack Source -> 该来源下 Security Evaluator 需要的最小 Evidence 集合。
# 映射建立在真实 evidence availability 上：TOOL_OUTPUT / AGENT_MESSAGE 当前无生产
# evidence exporter，故其状态恒为 UNSUPPORTED，而不是从 case_input 伪造成 observed。
ATTACK_SOURCE_EVIDENCE_REQUIREMENTS: dict[AttackSource, tuple[EvidenceKind, ...]] = {
    AttackSource.USER_INPUT: (EvidenceKind.CASE_INPUT, EvidenceKind.ACTUAL_ANSWER),
    AttackSource.RETRIEVED_CONTEXT: (
        EvidenceKind.CASE_INPUT,
        EvidenceKind.ACTUAL_ANSWER,
        EvidenceKind.RAG_CONTEXT,
        EvidenceKind.RETRIEVAL,
        EvidenceKind.CITATION,
    ),
    AttackSource.TOOL_OUTPUT: (
        EvidenceKind.CASE_INPUT,
        EvidenceKind.ACTUAL_ANSWER,
        EvidenceKind.TOOL_OUTPUT,
    ),
    AttackSource.AGENT_MESSAGE: (
        EvidenceKind.CASE_INPUT,
        EvidenceKind.ACTUAL_ANSWER,
        EvidenceKind.AGENT_MESSAGE,
    ),
    AttackSource.REFERENCE_DATA: (
        EvidenceKind.CASE_INPUT,
        EvidenceKind.ACTUAL_ANSWER,
        EvidenceKind.JUDGE_REFERENCE,
    ),
}


@dataclass(frozen=True, slots=True)
class SecurityEvaluationInput:
    """Security Ground Truth + 真实 Execution Evidence 的统一输入；不含任何判定。"""

    schema_version: str
    case_id: str
    case_kind: SecurityCaseKind
    attack_type: AttackType | None = None
    attack_source: AttackSource | None = None
    severity: Severity | None = None
    expected_behaviors: tuple[ExpectedSecurityBehavior, ...] = ()
    case_input: FrozenDict | None = None
    runtime_terminal: RuntimeTerminalEvidence | None = None
    actual_answer: ActualAnswerEvidence | None = None
    rag_context: RagContextEvidence | None = None
    retrieval_evidence: RetrievalEvidence | None = None
    citation_evidence: CitationEvidence | None = None
    judge_facing: JudgeFacingEvidence | None = None
    attack_source_requirements: tuple[AttackSourceRequirement, ...] = ()

    def evidence_availability(self) -> FrozenDict:
        """返回 per-evidence-kind 的四态可用性摘要（CASE_INPUT 恒 AVAILABLE）。"""
        return FrozenDict(
            {
                EvidenceKind.CASE_INPUT.value: EvidenceAvailability.AVAILABLE,
                EvidenceKind.ACTUAL_ANSWER.value: (
                    self.actual_answer.availability if self.actual_answer is not None else EvidenceAvailability.UNAVAILABLE
                ),
                EvidenceKind.RAG_CONTEXT.value: (
                    self.rag_context.availability if self.rag_context is not None else EvidenceAvailability.UNAVAILABLE
                ),
                EvidenceKind.RETRIEVAL.value: (
                    self.retrieval_evidence.availability
                    if self.retrieval_evidence is not None
                    else EvidenceAvailability.UNAVAILABLE
                ),
                EvidenceKind.CITATION.value: (
                    self.citation_evidence.availability
                    if self.citation_evidence is not None
                    else EvidenceAvailability.UNAVAILABLE
                ),
                EvidenceKind.TOOL_OUTPUT.value: EvidenceAvailability.UNSUPPORTED,
                EvidenceKind.AGENT_MESSAGE.value: EvidenceAvailability.UNSUPPORTED,
                EvidenceKind.JUDGE_REFERENCE.value: (
                    self.judge_facing.availability if self.judge_facing is not None else EvidenceAvailability.UNAVAILABLE
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class _RagProjection:
    items: tuple[SelectedContextItem, ...]
    availability: EvidenceAvailability
    artifact_ids: tuple[str, ...]
    artifacts: tuple[RagEvaluationArtifactV1, ...]


def build_security_evaluation_input(
    case: EvaluationCase,
    execution_outcome: ExecutionOutcome,
    *,
    expected_attempt_id: str,
) -> SecurityEvaluationInput:
    """把 Security Ground Truth 与真实 execution evidence 投影为统一输入。

    Args:
        case: 携带 ``GroundTruth.security`` 的 dataset case（Security GT 唯一权威）。
        execution_outcome: 一次 attempt 的 terminal observation（含 evidence_refs）。
        expected_attempt_id: 当前 attempt 的 LocalAgent run id（= str(attempt_id)），
            用于 evidence identity fail-closed。

    Raises:
        SecurityEvaluationInputError: case 无 security GT、evidence 冲突、
            evidence malformed 或 identity mismatch 时 fail closed。

    Notes:
        纯投影：无 DB / HTTP / Agent / Judge / Retrieval rerun。不产生任何 verdict / score。
    """
    security = case.ground_truth.security
    if security is None:
        raise SecurityEvaluationInputError("case has no security ground truth")
    _validate_outcome_attempt(execution_outcome, expected_attempt_id)
    refs = execution_outcome.evidence_refs
    runtime_terminal = _runtime_terminal(execution_outcome)
    actual_answer = _project_final_answer(refs, expected_attempt_id)
    rag = _project_rag(refs, expected_attempt_id)
    retrieval = _project_retrieval(rag)
    citation = _project_citation(rag)
    judge_facing = _project_judge_facing(case, actual_answer, rag)
    requirements = _requirements(security, actual_answer, rag, retrieval, citation, judge_facing)
    return SecurityEvaluationInput(
        schema_version=SECURITY_EVALUATION_INPUT_SCHEMA_VERSION,
        case_id=case.case_id,
        case_kind=security.case_kind,
        attack_type=security.attack_type,
        attack_source=security.attack_source,
        severity=security.severity,
        expected_behaviors=tuple(security.expected_behaviors),
        case_input=freeze_json(case.input),
        runtime_terminal=runtime_terminal,
        actual_answer=actual_answer,
        rag_context=RagContextEvidence(
            availability=rag.availability,
            items=rag.items,
            artifact_ids=rag.artifact_ids,
        ),
        retrieval_evidence=retrieval,
        citation_evidence=citation,
        judge_facing=judge_facing,
        attack_source_requirements=requirements,
    )


def attack_source_evidence_status(
    input_value: SecurityEvaluationInput,
) -> tuple[AttackSourceRequirement, ...]:
    """返回当前 input 的 attack source 所要求 evidence 的实际状态矩阵。

    BENIGN_CONTROL（attack_source=None）返回空矩阵：不制造 attack requirement。
    """
    return input_value.attack_source_requirements


def _validate_outcome_attempt(outcome: ExecutionOutcome, expected_attempt_id: str) -> None:
    artifact = outcome.output_artifact_ref
    if artifact is None:
        return
    artifact_id = artifact.artifact_id
    if not isinstance(artifact_id, str) or not artifact_id.startswith(_LOCALAGENT_RUN_PREFIX):
        return
    run_id = artifact_id.removeprefix(_LOCALAGENT_RUN_PREFIX)
    if run_id != expected_attempt_id:
        raise SecurityEvaluationInputError("output artifact attempt identity mismatch")


def _runtime_terminal(outcome: ExecutionOutcome) -> RuntimeTerminalEvidence:
    metadata = outcome.metadata
    stop_reason = metadata.get("provider_stop_reason")
    if not isinstance(stop_reason, str):
        stop_reason = None
    return RuntimeTerminalEvidence(
        outcome_kind=outcome.kind,
        stop_reason=stop_reason,
        error_code=outcome.error_category,
        safe_message=outcome.reason,
    )


def _project_final_answer(
    refs: tuple[EvidenceRef, ...],
    expected_attempt_id: str,
) -> ActualAnswerEvidence:
    answer_refs = tuple(item for item in refs if item.kind == FINAL_ANSWER_EVIDENCE_KIND)
    if not answer_refs:
        return ActualAnswerEvidence(availability=EvidenceAvailability.UNAVAILABLE)
    if len(answer_refs) > 1:
        raise SecurityEvaluationInputError("conflicting multiple final answer evidence")
    ref = answer_refs[0]
    try:
        artifact = FinalAnswerEvidenceV1.model_validate(ref.metadata["payload"])
    except (KeyError, TypeError, ValidationError):
        raise SecurityEvaluationInputError("malformed final answer evidence") from None
    if artifact.run_id != expected_attempt_id or ref.identifier != artifact.evidence_id:
        raise SecurityEvaluationInputError("final answer evidence attempt identity mismatch")
    return ActualAnswerEvidence(
        availability=EvidenceAvailability.AVAILABLE,
        content=artifact.content,
        evidence_id=artifact.evidence_id,
        schema_version=artifact.schema_version,
        media_type=artifact.media_type,
        content_sha256=artifact.content_sha256,
    )


def _project_rag(refs: tuple[EvidenceRef, ...], expected_attempt_id: str) -> _RagProjection:
    rag_refs = tuple(item for item in refs if item.kind == RAG_ARTIFACT_EVIDENCE_KIND)
    if not rag_refs:
        return _RagProjection(items=(), availability=EvidenceAvailability.UNAVAILABLE, artifact_ids=(), artifacts=())
    artifacts: list[RagEvaluationArtifactV1] = []
    for ref in rag_refs:
        try:
            artifact = RagEvaluationArtifactV1.model_validate(ref.metadata["payload"])
        except (KeyError, TypeError, ValidationError):
            raise SecurityEvaluationInputError("malformed rag evaluation artifact evidence") from None
        if artifact.run_id != expected_attempt_id or ref.identifier != artifact.artifact_id:
            raise SecurityEvaluationInputError("rag artifact evidence attempt identity mismatch")
        artifacts.append(artifact)
    items: list[SelectedContextItem] = []
    for artifact in artifacts:
        for item in artifact.selected_items:
            items.append(
                SelectedContextItem(
                    artifact_id=artifact.artifact_id,
                    invocation_index=artifact.invocation_index,
                    selection_rank=item.selection_rank,
                    document_id=item.document_id,
                    chunk_id=item.chunk_id,
                    context_block_id=item.context_block_id,
                    citation_id=item.citation_id,
                    context_content_hash=item.context_content_hash,
                    text=item.text,
                )
            )
    items.sort(key=lambda item: (item.invocation_index, item.selection_rank))
    availability = (
        EvidenceAvailability.AVAILABLE
        if items
        else EvidenceAvailability.KNOWN_EMPTY
    )
    return _RagProjection(
        items=tuple(items),
        availability=availability,
        artifact_ids=tuple(artifact.artifact_id for artifact in artifacts),
        artifacts=tuple(artifacts),
    )


def _project_retrieval(rag: _RagProjection) -> RetrievalEvidence:
    if not rag.artifacts:
        return RetrievalEvidence(availability=EvidenceAvailability.UNAVAILABLE)
    retrieved: list[RetrievedItemIdentity] = []
    ranked: list[RetrievedItemIdentity] = []
    for artifact in rag.artifacts:
        for item in artifact.retrieved_items:
            retrieved.append(_retrieved_identity(artifact, item))
        for item in artifact.ranked_items:
            ranked.append(_retrieved_identity(artifact, item))
    availability = (
        EvidenceAvailability.AVAILABLE
        if retrieved
        else EvidenceAvailability.KNOWN_EMPTY
    )
    return RetrievalEvidence(
        availability=availability,
        retrieved=tuple(retrieved),
        ranked=tuple(ranked),
    )


def _retrieved_identity(
    artifact: RagEvaluationArtifactV1,
    item: object,
) -> RetrievedItemIdentity:
    return RetrievedItemIdentity(
        artifact_id=artifact.artifact_id,
        retrieval_id=artifact.retrieval_id,
        invocation_index=artifact.invocation_index,
        document_id=item.document_id,
        chunk_id=item.chunk_id,
        rank=item.rank,
        retrieval_rank=item.retrieval_rank,
        selected=item.selected,
        content_hash=item.content_hash,
    )


def _project_citation(rag: _RagProjection) -> CitationEvidence:
    if not rag.artifacts:
        return CitationEvidence(
            availability=EvidenceAvailability.UNAVAILABLE,
            answer_to_citation_binding=EvidenceAvailability.UNAVAILABLE,
            answer_to_citation_binding_reason=_EVIDENCE_GAP_REASON,
        )
    selected_citation_ids = {
        item.citation_id for artifact in rag.artifacts for item in artifact.selected_items
    }
    items: list[CitationItemProjection] = []
    seen: set[str] = set()
    for artifact in rag.artifacts:
        for citation in artifact.citations:
            if citation.citation_id in seen:
                continue
            seen.add(citation.citation_id)
            items.append(
                CitationItemProjection(
                    citation_id=citation.citation_id,
                    document_id=citation.document_id,
                    chunk_id=citation.chunk_id,
                    context_block_id=citation.context_block_id,
                    context_content_hash=citation.context_content_hash,
                    selected=citation.citation_id in selected_citation_ids,
                )
            )
    availability = (
        EvidenceAvailability.AVAILABLE
        if items
        else EvidenceAvailability.KNOWN_EMPTY
    )
    return CitationEvidence(
        availability=availability,
        items=tuple(items),
        answer_to_citation_binding=EvidenceAvailability.UNAVAILABLE,
        answer_to_citation_binding_reason=(
            "no answer-to-citation binding evidence is exported; "
            "citation_id on a chunk does not prove the final answer referenced it"
        ),
    )


def _project_judge_facing(
    case: EvaluationCase,
    actual_answer: ActualAnswerEvidence,
    rag: _RagProjection,
) -> JudgeFacingEvidence:
    question: str | None = None
    query = case.input.get("query")
    if isinstance(query, str):
        question = query
    reference_answer: str | None = None
    if case.ground_truth.generation is not None:
        reference_answer = case.ground_truth.generation.reference_answer
    actual = (
        actual_answer.content
        if actual_answer.availability is EvidenceAvailability.AVAILABLE
        else None
    )
    availability = (
        EvidenceAvailability.AVAILABLE
        if actual is not None
        else EvidenceAvailability.UNAVAILABLE
    )
    return JudgeFacingEvidence(
        availability=availability,
        question=question,
        actual_answer=actual,
        reference_answer=reference_answer,
        context=rag.items,
    )


def _requirements(
    security: SecurityGroundTruth,
    actual_answer: ActualAnswerEvidence,
    rag: _RagProjection,
    retrieval: RetrievalEvidence,
    citation: CitationEvidence,
    judge_facing: JudgeFacingEvidence,
) -> tuple[AttackSourceRequirement, ...]:
    if security.attack_source is None:
        return ()
    kinds = ATTACK_SOURCE_EVIDENCE_REQUIREMENTS[security.attack_source]
    status_by_kind = {
        EvidenceKind.CASE_INPUT: EvidenceAvailability.AVAILABLE,
        EvidenceKind.ACTUAL_ANSWER: actual_answer.availability,
        EvidenceKind.RAG_CONTEXT: rag.availability,
        EvidenceKind.RETRIEVAL: retrieval.availability,
        EvidenceKind.CITATION: citation.availability,
        EvidenceKind.TOOL_OUTPUT: EvidenceAvailability.UNSUPPORTED,
        EvidenceKind.AGENT_MESSAGE: EvidenceAvailability.UNSUPPORTED,
        EvidenceKind.JUDGE_REFERENCE: judge_facing.availability,
    }
    return tuple(
        AttackSourceRequirement(
            evidence_kind=kind,
            status=status_by_kind[kind],
            required=True,
        )
        for kind in kinds
    )


__all__ = [
    "ATTACK_SOURCE_EVIDENCE_REQUIREMENTS",
    "ActualAnswerEvidence",
    "AttackSourceRequirement",
    "CitationEvidence",
    "CitationItemProjection",
    "EvidenceAvailability",
    "EvidenceKind",
    "JudgeFacingEvidence",
    "RagContextEvidence",
    "RetrievalEvidence",
    "RetrievedItemIdentity",
    "RuntimeTerminalEvidence",
    "SECURITY_EVALUATION_INPUT_SCHEMA_VERSION",
    "SecurityEvaluationInput",
    "SecurityEvaluationInputError",
    "attack_source_evidence_status",
    "build_security_evaluation_input",
]