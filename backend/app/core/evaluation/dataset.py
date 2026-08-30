"""Evaluation Dataset v1 —— Case / Ground Truth / Dataset 的严格 schema 与文件加载。

本模块定义可重复执行的 EvaluationCase、按 evaluator 族分段的 GroundTruth，以及由 Case 组成的
EvaluationDataset。Dataset 当前是测试资产（JSON file），不是业务数据：不落库、不加 migration、
不绑定 Runtime/Run/Artifact —— 一个 Case 可以产生多次 attempt 用于 A/B 对比。

chunk 身份沿用 RagEvaluationArtifactV1 的 (document_id, chunk_id) 约定，使未来 Recall@K / MRR /
NDCG evaluator 可以直接与 retrieved_items / ranked_items 对齐。
"""

# ruff: noqa: D415

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from app.core.evaluation.immutable import freeze_json

EVALUATION_DATASET_SCHEMA_VERSION = "evaluation-dataset.v1"
EVALUATION_DATASET_SECURITY_SCHEMA_VERSION = "evaluation-dataset.v2"
EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION = "evaluation-dataset.v3"
EVALUATION_DATASET_ANSWERABILITY_SCHEMA_VERSION = "evaluation-dataset.v4"
# WP5 Stateful Memory Evaluation schema variant。契约定义见 stateful_memory_dataset.py；
# 它与上述 flat single-query case schema 共享 EvaluationDataset 的 versioned/strict/UTF-8
# 约定，但不复用 flat case 的 step 语义。
EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION = "stateful-memory-scenario.v1"
# R3-B：Stateful Dataset V2 schema variant。契约定义见 stateful_memory_dataset_v2.py；
# 它引入 seeded canonical_text 与分层 identity evidence policy，与 V1 严格隔离。
EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION_V2 = "stateful-memory-scenario.v2"

# 受支持 document contract 版本：v1（retrieval/ranking/generation）、v2（v1 + security）
# 与 v3（v1 + document_retrieval，面向 document-level public benchmark ground truth）。
SUPPORTED_DATASET_SCHEMA_VERSIONS = (
    EVALUATION_DATASET_SCHEMA_VERSION,
    EVALUATION_DATASET_SECURITY_SCHEMA_VERSION,
    EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION,
    EVALUATION_DATASET_ANSWERABILITY_SCHEMA_VERSION,
)


class AnswerabilityCaseType(StrEnum):
    """No-Answer Dataset 的人工标注 case 类别。"""

    ANSWERABLE = "ANSWERABLE"
    EMPTY = "EMPTY"
    WEAK = "WEAK"
    MISLEADING = "MISLEADING"
    CONFLICT = "CONFLICT"


class AnswerabilityExpectedDecision(StrEnum):
    """Ground Truth 期望的 evaluation-side decision。"""

    ANSWER = "ANSWER"
    ABSTAIN = "ABSTAIN"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"


class AnswerabilitySplit(StrEnum):
    """静态 Dataset split；runner 不执行随机切分。"""

    CALIBRATION = "CALIBRATION"
    EVALUATION = "EVALUATION"
    DIAGNOSTIC = "DIAGNOSTIC"


class AnswerabilityReasonCode(StrEnum):
    """人工标注依据的封闭 reason code。"""

    EXPLICIT_CORPUS_SUPPORT = "EXPLICIT_CORPUS_SUPPORT"
    FACT_ABSENT_FROM_CORPUS = "FACT_ABSENT_FROM_CORPUS"
    INSUFFICIENT_SUPPORT = "INSUFFICIENT_SUPPORT"
    OVERLAP_WITHOUT_SUPPORT = "OVERLAP_WITHOUT_SUPPORT"
    CONFLICTING_REFERENCE_FACTS = "CONFLICTING_REFERENCE_FACTS"


class SecurityCaseKind(StrEnum):
    """Security case 的显式类别：攻击样本或良性对照。"""

    ATTACK = "ATTACK"
    BENIGN_CONTROL = "BENIGN_CONTROL"


class AttackType(StrEnum):
    """Prompt Injection 攻击类型的最小 versioned taxonomy。

    只覆盖项目真实场景的最小集合，不扩展成完整 OWASP 分类。
    """

    DIRECT_INSTRUCTION_OVERRIDE = "DIRECT_INSTRUCTION_OVERRIDE"
    INDIRECT_CONTEXT_INJECTION = "INDIRECT_CONTEXT_INJECTION"
    SYSTEM_PROMPT_EXTRACTION = "SYSTEM_PROMPT_EXTRACTION"
    ROLE_CONFUSION = "ROLE_CONFUSION"
    UNAUTHORIZED_TOOL_INSTRUCTION = "UNAUTHORIZED_TOOL_INSTRUCTION"
    CROSS_AGENT_INSTRUCTION_INJECTION = "CROSS_AGENT_INSTRUCTION_INJECTION"
    JUDGE_INJECTION = "JUDGE_INJECTION"


class AttackSource(StrEnum):
    """攻击载荷进入系统的位置。"""

    USER_INPUT = "USER_INPUT"
    RETRIEVED_CONTEXT = "RETRIEVED_CONTEXT"
    TOOL_OUTPUT = "TOOL_OUTPUT"
    AGENT_MESSAGE = "AGENT_MESSAGE"
    REFERENCE_DATA = "REFERENCE_DATA"


class ExpectedSecurityBehavior(StrEnum):
    """系统面对攻击时应遵守的安全边界（requirement list 的最小集合）。"""

    PRESERVE_ORIGINAL_TASK = "PRESERVE_ORIGINAL_TASK"
    IGNORE_UNTRUSTED_INSTRUCTION = "IGNORE_UNTRUSTED_INSTRUCTION"
    DO_NOT_DISCLOSE_PROTECTED_CONTENT = "DO_NOT_DISCLOSE_PROTECTED_CONTENT"
    DO_NOT_PERFORM_UNAUTHORIZED_ACTION = "DO_NOT_PERFORM_UNAUTHORIZED_ACTION"


class Severity(StrEnum):
    """Security case 的严重级别（当前仅为 Dataset Fact，不驱动 release decision）。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# 与 producer wire id 一致的 bounded 标识符字符集。
_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$")


class EvaluationDatasetLoadError(ValueError):
    """Dataset 文件无法读取或无法解析为 JSON object 时抛出。"""


def _require_wire_id(value: str, field_name: str) -> str:
    if not _WIRE_ID.match(value):
        raise ValueError(f"invalid {field_name} format: {value!r}")
    return value


def _validate_json_object(value: dict[str, Any], field_name: str) -> dict[str, Any]:
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    try:
        freeze_json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-compatible: {exc}") from exc
    return value


class GroundTruthChunk(BaseModel):
    """Retrieval Ground Truth 中一个 relevant chunk 的身份引用。"""

    model_config = ConfigDict(extra="forbid")

    document_id: StrictStr
    chunk_id: StrictStr

    @field_validator("document_id", "chunk_id")
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return _require_wire_id(value, info.field_name)


class GradedRelevance(BaseModel):
    """Ranking Ground Truth 中一个 chunk 的分级相关性（NDCG 输入）。"""

    model_config = ConfigDict(extra="forbid")

    chunk_id: StrictStr
    relevance: int = Field(ge=0)
    document_id: StrictStr | None = None

    @field_validator("chunk_id")
    @classmethod
    def _chunk_id(cls, value: str) -> str:
        return _require_wire_id(value, "chunk_id")

    @field_validator("document_id")
    @classmethod
    def _document_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _require_wire_id(value, "document_id")

    def identity(self) -> tuple[str | None, str]:
        """返回与 artifact item 对齐的 (document_id, chunk_id) 身份。"""
        return (self.document_id, self.chunk_id)


class RetrievalGroundTruth(BaseModel):
    """Recall@K / MRR 输入：relevant chunk 集合。"""

    model_config = ConfigDict(extra="forbid")

    relevant_chunks: list[GroundTruthChunk] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_chunks(self) -> "RetrievalGroundTruth":
        identities = [(item.document_id, item.chunk_id) for item in self.relevant_chunks]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate relevant chunk identity is not allowed")
        return self

    def chunk_identities(self) -> set[tuple[str, str]]:
        """返回 relevant chunk 的 (document_id, chunk_id) 身份集合。"""
        return {(item.document_id, item.chunk_id) for item in self.relevant_chunks}


class RankingGroundTruth(BaseModel):
    """NDCG 输入：chunk 级 graded relevance。"""

    model_config = ConfigDict(extra="forbid")

    graded_relevance: list[GradedRelevance] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_chunks(self) -> "RankingGroundTruth":
        identities = [item.identity() for item in self.graded_relevance]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate graded relevance chunk identity is not allowed")
        return self


class DocumentRelevance(BaseModel):
    """Document-level Ground Truth 中一个 relevant document 的分级相关性。"""

    model_config = ConfigDict(extra="forbid")

    document_id: StrictStr
    relevance: int = Field(ge=0)

    @field_validator("document_id")
    @classmethod
    def _document_id(cls, value: str) -> str:
        return _require_wire_id(value, "document_id")


class DocumentRetrievalGroundTruth(BaseModel):
    """Document-level Recall@K / MRR / NDCG 输入：relevant document 的分级相关性。

    面向 document-level public benchmark（如 BEIR qrels）：ground truth 权威是
    benchmark 自身的 document id，而不是任何 chunk identity。relevance 允许分级；
    binary relevance dataset（如 SciFact）全部为 1。
    """

    model_config = ConfigDict(extra="forbid")

    relevant_documents: list[DocumentRelevance] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_documents(self) -> "DocumentRetrievalGroundTruth":
        document_ids = [item.document_id for item in self.relevant_documents]
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("duplicate relevant document identity is not allowed")
        return self

    def relevance_map(self) -> dict[str, int]:
        """返回 document_id -> relevance 的映射。"""
        return {item.document_id: item.relevance for item in self.relevant_documents}


class GenerationGroundTruth(BaseModel):
    """LLM Judge / 参考答案输入。"""

    model_config = ConfigDict(extra="forbid")

    reference_answer: StrictStr = Field(min_length=1)


class SecurityGroundTruth(BaseModel):
    """Security Evaluation 的 Expected Behavior Ground Truth（Dataset 权威，非 Evaluator 输出）。"""

    model_config = ConfigDict(extra="forbid")

    case_kind: SecurityCaseKind
    attack_type: AttackType | None = None
    attack_source: AttackSource | None = None
    severity: Severity | None = None
    expected_behaviors: list[ExpectedSecurityBehavior] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_case_semantics(self) -> "SecurityGroundTruth":
        if self.case_kind == SecurityCaseKind.ATTACK:
            if self.attack_type is None:
                raise ValueError("attack case requires attack_type")
            if self.attack_source is None:
                raise ValueError("attack case requires attack_source")
            if self.severity is None:
                raise ValueError("attack case requires severity")
        else:
            if self.attack_type is not None:
                raise ValueError("benign control must not declare attack_type")
            if self.attack_source is not None:
                raise ValueError("benign control must not declare attack_source")
            if self.severity is not None:
                raise ValueError("benign control must not declare severity")
        if len(self.expected_behaviors) != len(set(self.expected_behaviors)):
            raise ValueError("duplicate expected_behavior is not allowed")
        return self


class AnswerabilityGroundTruth(BaseModel):
    """No-Answer threshold 的人工 Ground Truth；不得从 retrieval outcome 派生。"""

    model_config = ConfigDict(extra="forbid")

    answerable: bool
    case_type: AnswerabilityCaseType
    expected_decision: AnswerabilityExpectedDecision
    split: AnswerabilitySplit
    corpus_ref: StrictStr
    expected_support_fact_ids: list[StrictStr]
    annotation_reason_code: AnswerabilityReasonCode

    @field_validator("corpus_ref")
    @classmethod
    def _corpus_ref(cls, value: str) -> str:
        return _require_wire_id(value, "corpus_ref")

    @field_validator("expected_support_fact_ids")
    @classmethod
    def _support_fact_ids(cls, values: list[str]) -> list[str]:
        checked = [_require_wire_id(value, "expected_support_fact_ids") for value in values]
        if len(checked) != len(set(checked)):
            raise ValueError("duplicate expected_support_fact_ids are not allowed")
        return checked

    @model_validator(mode="after")
    def _semantics(self) -> "AnswerabilityGroundTruth":
        if self.case_type == AnswerabilityCaseType.ANSWERABLE:
            if not self.answerable or self.expected_decision != AnswerabilityExpectedDecision.ANSWER:
                raise ValueError("ANSWERABLE requires answerable=true and expected_decision=ANSWER")
            if not self.expected_support_fact_ids:
                raise ValueError("ANSWERABLE requires expected_support_fact_ids")
            if self.annotation_reason_code != AnswerabilityReasonCode.EXPLICIT_CORPUS_SUPPORT:
                raise ValueError("ANSWERABLE requires EXPLICIT_CORPUS_SUPPORT")
        elif self.case_type == AnswerabilityCaseType.CONFLICT:
            if self.answerable or self.expected_decision != AnswerabilityExpectedDecision.DIAGNOSTIC_ONLY:
                raise ValueError("CONFLICT requires answerable=false and expected_decision=DIAGNOSTIC_ONLY")
            if self.split != AnswerabilitySplit.DIAGNOSTIC:
                raise ValueError("CONFLICT is diagnostic-only")
            if self.annotation_reason_code != AnswerabilityReasonCode.CONFLICTING_REFERENCE_FACTS:
                raise ValueError("CONFLICT requires CONFLICTING_REFERENCE_FACTS")
        else:
            reason_by_type = {
                AnswerabilityCaseType.EMPTY: AnswerabilityReasonCode.FACT_ABSENT_FROM_CORPUS,
                AnswerabilityCaseType.WEAK: AnswerabilityReasonCode.INSUFFICIENT_SUPPORT,
                AnswerabilityCaseType.MISLEADING: AnswerabilityReasonCode.OVERLAP_WITHOUT_SUPPORT,
            }
            if self.answerable or self.expected_decision != AnswerabilityExpectedDecision.ABSTAIN:
                raise ValueError(f"{self.case_type} requires answerable=false and expected_decision=ABSTAIN")
            if self.expected_support_fact_ids:
                raise ValueError(f"{self.case_type} must not declare expected_support_fact_ids")
            if self.annotation_reason_code != reason_by_type[self.case_type]:
                raise ValueError(f"{self.case_type} has mismatched annotation_reason_code")
        if self.split in {AnswerabilitySplit.CALIBRATION, AnswerabilitySplit.EVALUATION}:
            if self.expected_decision == AnswerabilityExpectedDecision.DIAGNOSTIC_ONLY:
                raise ValueError("calibration/evaluation split must not contain DIAGNOSTIC_ONLY")
        return self


class GroundTruth(BaseModel):
    """按 evaluator 族分段的 Ground Truth；至少提供一段。"""

    model_config = ConfigDict(extra="forbid")

    retrieval: RetrievalGroundTruth | None = None
    ranking: RankingGroundTruth | None = None
    generation: GenerationGroundTruth | None = None
    security: SecurityGroundTruth | None = None
    document_retrieval: DocumentRetrievalGroundTruth | None = None
    answerability: AnswerabilityGroundTruth | None = None

    @model_validator(mode="after")
    def _require_section(self) -> "GroundTruth":
        if (
            self.retrieval is None
            and self.ranking is None
            and self.generation is None
            and self.security is None
            and self.document_retrieval is None
            and self.answerability is None
        ):
            raise ValueError(
                "ground_truth must provide at least one of "
                "retrieval/ranking/generation/security/document_retrieval/answerability"
            )
        return self


class EvaluationCase(BaseModel):
    """一个可重复执行和评估的问题样本；不绑定 Runtime、Run 或 Artifact。"""

    model_config = ConfigDict(extra="forbid")

    case_id: StrictStr
    name: StrictStr = Field(min_length=1)
    input: dict[str, Any]
    expected_output: StrictStr | None = Field(default=None, min_length=1)
    ground_truth: GroundTruth
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("case_id")
    @classmethod
    def _case_id(cls, value: str) -> str:
        return _require_wire_id(value, "case_id")

    @field_validator("input")
    @classmethod
    def _input(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_json_object(value, "input")

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            return value
        try:
            freeze_json(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"metadata must be JSON-compatible: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _answerability_metadata(self) -> "EvaluationCase":
        if self.ground_truth.answerability is None:
            return self
        if set(self.metadata) != {"tags", "leakage_group"}:
            raise ValueError("answerability case metadata requires exactly tags and leakage_group")
        tags = self.metadata["tags"]
        leakage_group = self.metadata["leakage_group"]
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise ValueError("answerability metadata.tags must be non-empty strings")
        if not isinstance(leakage_group, str) or not leakage_group.strip():
            raise ValueError("answerability metadata.leakage_group is required")
        _require_wire_id(leakage_group, "leakage_group")
        return self


class EvaluationDataset(BaseModel):
    """一组 Case 的版本化集合（测试资产，非业务数据）。"""

    model_config = ConfigDict(extra="forbid")

    dataset_schema_version: StrictStr
    dataset_id: StrictStr
    name: StrictStr = Field(min_length=1)
    description: StrictStr | None = None
    version: StrictStr
    cases: list[EvaluationCase] = Field(min_length=1)

    @field_validator("dataset_schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value not in SUPPORTED_DATASET_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported dataset_schema_version: {value}")
        return value

    @field_validator("dataset_id", "version")
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return _require_wire_id(value, info.field_name)

    @model_validator(mode="after")
    def _validate_security_version_contract(self) -> "EvaluationDataset":
        if self.dataset_schema_version == EVALUATION_DATASET_ANSWERABILITY_SCHEMA_VERSION:
            leakage_splits: dict[str, AnswerabilitySplit] = {}
            for case in self.cases:
                truth = case.ground_truth.answerability
                if truth is None:
                    continue
                leakage_group = str(case.metadata["leakage_group"])
                previous = leakage_splits.setdefault(leakage_group, truth.split)
                if previous != truth.split:
                    raise ValueError("leakage_group must not cross dataset splits")
            return self
        if self.dataset_schema_version == EVALUATION_DATASET_SCHEMA_VERSION:
            for case in self.cases:
                if case.ground_truth.answerability is not None:
                    raise ValueError("evaluation-dataset.v1 must not declare answerability ground truth")
                if case.ground_truth.security is not None:
                    raise ValueError(
                        f"{EVALUATION_DATASET_SCHEMA_VERSION} must not declare security ground truth; "
                        f"use {EVALUATION_DATASET_SECURITY_SCHEMA_VERSION} for security cases"
                    )
                if case.ground_truth.document_retrieval is not None:
                    raise ValueError(
                        f"{EVALUATION_DATASET_SCHEMA_VERSION} must not declare document ground truth; "
                        f"use {EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION} for document-level cases"
                    )
        if self.dataset_schema_version == EVALUATION_DATASET_SECURITY_SCHEMA_VERSION:
            for case in self.cases:
                if case.ground_truth.answerability is not None:
                    raise ValueError("evaluation-dataset.v2 must not declare answerability ground truth")
                if case.ground_truth.document_retrieval is not None:
                    raise ValueError(
                        f"{EVALUATION_DATASET_SECURITY_SCHEMA_VERSION} must not declare document "
                        f"ground truth; use {EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION} "
                        f"for document-level cases"
                    )
        if self.dataset_schema_version == EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION:
            for case in self.cases:
                if case.ground_truth.answerability is not None:
                    raise ValueError("evaluation-dataset.v3 must not declare answerability ground truth")
                if case.ground_truth.security is not None:
                    raise ValueError(
                        f"{EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION} must not declare security "
                        f"ground truth; use {EVALUATION_DATASET_SECURITY_SCHEMA_VERSION} "
                        f"for security cases"
                    )
        return self

    @model_validator(mode="after")
    def _unique_case_ids(self) -> "EvaluationDataset":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("duplicate case_id is not allowed")
        return self

    def __len__(self) -> int:
        """返回 dataset 中 case 的数量。"""
        return len(self.cases)


def validate_case(payload: object) -> EvaluationCase:
    """校验单个 case payload；失败抛 pydantic ValidationError。"""
    return EvaluationCase.model_validate(payload)


def validate_dataset(payload: object) -> EvaluationDataset:
    """校验 dataset payload；失败抛 pydantic ValidationError。"""
    return EvaluationDataset.model_validate(payload)


def load_dataset(path: str | Path) -> EvaluationDataset:
    """从 JSON 文件加载并严格校验 dataset。

    Args:
        path: JSON dataset 文件路径。

    Returns:
        校验通过的 EvaluationDataset。

    Raises:
        EvaluationDatasetLoadError: 文件不可读或不是合法 JSON object。
        pydantic.ValidationError: 内容不符合 dataset schema。
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvaluationDatasetLoadError(f"cannot read dataset file: {file_path}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvaluationDatasetLoadError(f"dataset file is not valid JSON: {file_path}") from exc
    if not isinstance(payload, dict):
        raise EvaluationDatasetLoadError(f"dataset file must contain a JSON object: {file_path}")
    return EvaluationDataset.model_validate(payload)


def iter_cases(dataset: EvaluationDataset) -> Iterator[EvaluationCase]:
    """按声明顺序迭代 dataset 中的 cases。"""
    return iter(dataset.cases)


__all__ = [
    "AnswerabilityCaseType",
    "AnswerabilityExpectedDecision",
    "AnswerabilityGroundTruth",
    "AnswerabilityReasonCode",
    "AnswerabilitySplit",
    "AttackSource",
    "AttackType",
    "EVALUATION_DATASET_SCHEMA_VERSION",
    "EVALUATION_DATASET_SECURITY_SCHEMA_VERSION",
    "EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION",
    "EVALUATION_DATASET_ANSWERABILITY_SCHEMA_VERSION",
    "EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION",
    "EVALUATION_DATASET_STATEFUL_MEMORY_SCHEMA_VERSION_V2",
    "EvaluationCase",
    "EvaluationDataset",
    "EvaluationDatasetLoadError",
    "ExpectedSecurityBehavior",
    "GenerationGroundTruth",
    "GradedRelevance",
    "GroundTruth",
    "GroundTruthChunk",
    "DocumentRelevance",
    "DocumentRetrievalGroundTruth",
    "RankingGroundTruth",
    "RetrievalGroundTruth",
    "SUPPORTED_DATASET_SCHEMA_VERSIONS",
    "SecurityCaseKind",
    "SecurityGroundTruth",
    "Severity",
    "iter_cases",
    "load_dataset",
    "validate_case",
    "validate_dataset",
]
