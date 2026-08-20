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
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from app.core.evaluation.immutable import freeze_json

EVALUATION_DATASET_SCHEMA_VERSION = "evaluation-dataset.v1"

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


class GenerationGroundTruth(BaseModel):
    """LLM Judge / 参考答案输入。"""

    model_config = ConfigDict(extra="forbid")

    reference_answer: StrictStr = Field(min_length=1)


class GroundTruth(BaseModel):
    """按 evaluator 族分段的 Ground Truth；至少提供一段。"""

    model_config = ConfigDict(extra="forbid")

    retrieval: RetrievalGroundTruth | None = None
    ranking: RankingGroundTruth | None = None
    generation: GenerationGroundTruth | None = None

    @model_validator(mode="after")
    def _require_section(self) -> "GroundTruth":
        if self.retrieval is None and self.ranking is None and self.generation is None:
            raise ValueError("ground_truth must provide at least one of retrieval/ranking/generation")
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
        if value != EVALUATION_DATASET_SCHEMA_VERSION:
            raise ValueError(f"unsupported dataset_schema_version: {value}")
        return value

    @field_validator("dataset_id", "version")
    @classmethod
    def _ids(cls, value: str, info: Any) -> str:
        return _require_wire_id(value, info.field_name)

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
    "EVALUATION_DATASET_SCHEMA_VERSION",
    "EvaluationCase",
    "EvaluationDataset",
    "EvaluationDatasetLoadError",
    "GenerationGroundTruth",
    "GradedRelevance",
    "GroundTruth",
    "GroundTruthChunk",
    "RankingGroundTruth",
    "RetrievalGroundTruth",
    "iter_cases",
    "load_dataset",
    "validate_case",
    "validate_dataset",
]
