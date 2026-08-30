"""WP5 Stateful Memory Evaluation 的 assertion 代数、冻结 failure taxonomy 与 blocked 语义。"""
# ruff: noqa: D105, D415

# ruff: noqa: D415

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from app.core.evaluation.immutable import FrozenDict, JsonValue, freeze_json, require_text


class AssertionStatus(StrEnum):
    """统一 assertion 状态。BLOCKED 表示证据/前置不足，绝不自动作 0 分。"""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvaluationLayer(StrEnum):
    """Evaluation layer：Layer 1 deterministic contract vs Layer 2 real-model behavioral。

    该 typed enum 是所有需要区分两层的 evaluator/gate/report 的权威依据；禁止通过
    runtime implementation detail（如 ScriptedMemoryTarget 等 harness 类型）猜测层级。
    """

    LAYER_1_DETERMINISTIC = "LAYER_1_DETERMINISTIC"
    LAYER_2_REAL_MODEL = "LAYER_2_REAL_MODEL"


class EvidenceGapClassification(StrEnum):
    """identity evidence gap 的严格分类（R3-B）。

    - EXPECTED_EVIDENCE_LIMITATION：Dataset 在对应 layer 声明了 EXPECTED_LIMITATION，
      当前 runtime evidence 无法证明 identity；这是 accepted limitation，不是 infra
      failure，也不得从 reason 文本推断。
    - 未声明该分类的 identity BLOCKED 一律视为 unexpected evidence/evaluation failure。
    """

    EXPECTED_EVIDENCE_LIMITATION = "EXPECTED_EVIDENCE_LIMITATION"


class FailureTaxonomy(StrEnum):
    """冻结 failure taxonomy（WP5 Architecture §28）；保持可扩展但不无限泛化。"""

    FORMATION_FALSE_NEGATIVE = "FORMATION_FALSE_NEGATIVE"
    FORMATION_FALSE_POSITIVE = "FORMATION_FALSE_POSITIVE"
    PREDICATE_CLASSIFICATION_ERROR = "PREDICATE_CLASSIFICATION_ERROR"
    LIFECYCLE_OPERATION_MISMATCH = "LIFECYCLE_OPERATION_MISMATCH"
    FINAL_STATE_MISMATCH = "FINAL_STATE_MISMATCH"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
    RETRIEVAL_MISS = "RETRIEVAL_MISS"
    IRRELEVANT_RETRIEVAL = "IRRELEVANT_RETRIEVAL"
    SUPERSEDED_LEAKAGE = "SUPERSEDED_LEAKAGE"
    FORGOTTEN_LEAKAGE = "FORGOTTEN_LEAKAGE"
    SCOPE_LEAKAGE = "SCOPE_LEAKAGE"
    CONTEXT_INJECTION_MISS = "CONTEXT_INJECTION_MISS"
    GENERATION_USE_FAILURE = "GENERATION_USE_FAILURE"
    RUNTIME_BLOCKED = "RUNTIME_BLOCKED"
    EVALUATION_INFRA_FAILURE = "EVALUATION_INFRA_FAILURE"


class BlockReason(StrEnum):
    """BLOCKED 的必须分类；runtime block 与 evaluation infra failure 独立报告。"""

    RUNTIME = "runtime"
    EVIDENCE_CAPTURE = "evidence_capture"
    EVALUATION_INFRASTRUCTURE = "evaluation_infrastructure"
    PREREQUISITE = "prerequisite"
    NOT_SUPPORTED_BY_CURRENT_EVIDENCE = "not_supported_by_current_evidence"


class AssertionDimension(StrEnum):
    """assertion 所属 dimension（metric/artifact 归类用）。"""

    FORMATION = "FORMATION"
    PREDICATE = "PREDICATE"
    LIFECYCLE = "LIFECYCLE"
    FINAL_STATE = "FINAL_STATE"
    INVARIANT = "INVARIANT"
    RETRIEVAL = "RETRIEVAL"
    RANKING = "RANKING"
    INJECTION = "INJECTION"
    LEAKAGE = "LEAKAGE"
    GENERATION = "GENERATION"
    E2E = "E2E"


EVALUABLE_STATUSES: Final[frozenset[str]] = frozenset({AssertionStatus.PASS.value, AssertionStatus.FAIL.value})


@dataclass(frozen=True, slots=True)
class MemoryAssertion:
    """一个可追溯的 assertion result。

    - expected / actual_evidence：JSON-safe 投影或 evidence identifier。
    - failure_taxonomy：仅 FAIL 时设置 primary taxonomy；BLOCKED 使用 blocked_by 分类。
    - blocked_by：仅 BLOCKED 时设置；BLOCKED 必须有明确原因。
    """

    assertion_id: str
    dimension: AssertionDimension
    status: AssertionStatus
    expected: JsonValue = None
    actual_evidence: JsonValue = None
    failure_taxonomy: FailureTaxonomy | None = None
    blocked_by: BlockReason | None = None
    evidence_gap_classification: EvidenceGapClassification | None = None
    reason: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        require_text(self.assertion_id, "assertion_id")
        if not isinstance(self.dimension, AssertionDimension):
            raise ValueError("unknown assertion dimension")
        if not isinstance(self.status, AssertionStatus):
            raise ValueError("unknown assertion status")
        if self.failure_taxonomy is not None and not isinstance(self.failure_taxonomy, FailureTaxonomy):
            raise TypeError("failure_taxonomy must be FailureTaxonomy")
        if self.blocked_by is not None and not isinstance(self.blocked_by, BlockReason):
            raise TypeError("blocked_by must be BlockReason")
        if self.evidence_gap_classification is not None and not isinstance(
            self.evidence_gap_classification, EvidenceGapClassification
        ):
            raise TypeError("evidence_gap_classification must be EvidenceGapClassification")
        object.__setattr__(self, "expected", freeze_json(self.expected))
        object.__setattr__(self, "actual_evidence", freeze_json(self.actual_evidence))

        if self.status is AssertionStatus.FAIL:
            if self.failure_taxonomy is None and self.dimension is not AssertionDimension.E2E:
                raise ValueError("FAIL assertion requires a failure_taxonomy")
        elif self.failure_taxonomy is not None:
            raise ValueError("non-FAIL assertion must not declare failure_taxonomy")

        if self.status is AssertionStatus.BLOCKED:
            if self.blocked_by is None:
                raise ValueError("BLOCKED assertion requires a blocked_by reason")
        elif self.blocked_by is not None:
            raise ValueError("non-BLOCKED assertion must not declare blocked_by")

        if self.evidence_gap_classification is not None and self.status is not AssertionStatus.BLOCKED:
            raise ValueError("evidence_gap_classification requires a BLOCKED assertion")

        if self.status is AssertionStatus.PASS and self.reason is None:
            raise ValueError("PASS assertion requires a reason")

    def to_metadata(self) -> FrozenDict:
        """序列化为 JSON-safe provenance 快照。"""
        return freeze_json(
            {
                "assertion_id": self.assertion_id,
                "dimension": self.dimension.value,
                "status": self.status.value,
                "expected": self.expected,
                "actual_evidence": self.actual_evidence,
                "failure_taxonomy": (self.failure_taxonomy.value if self.failure_taxonomy is not None else None),
                "blocked_by": self.blocked_by.value if self.blocked_by is not None else None,
                "evidence_gap_classification": (
                    self.evidence_gap_classification.value if self.evidence_gap_classification is not None else None
                ),
                "reason": self.reason,
                "required": self.required,
            }
        )


__all__ = [
    "AssertionDimension",
    "AssertionStatus",
    "BlockReason",
    "EVALUABLE_STATUSES",
    "EvaluationLayer",
    "EvidenceGapClassification",
    "FailureTaxonomy",
    "MemoryAssertion",
]
