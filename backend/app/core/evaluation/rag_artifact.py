"""RAG Evaluation Artifact v1 —— AgentEvalOps consumer 侧 strict DTO 与 Evidence 映射。

对应 LocalAgent producer contract（`retrieval_evaluation.py` + `26_localagent_zcode_verification.md`）。
本模块只做消费侧解析、校验与 EvidenceRef 映射；不改变结构、不重跑 retrieval、不重建 ranking。
"""

# ruff: noqa: D415

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from app.core.evaluation.references import EvidenceRef

RAG_ARTIFACT_SCHEMA_VERSION = "rag-evaluation-artifact.v1"
RAG_ARTIFACT_EVIDENCE_KIND = "rag_evaluation_artifact"
RAG_ARTIFACT_MEDIA_TYPE = "application/vnd.agentevalops.rag-evaluation-artifact+json"
RAG_ARTIFACT_EVIDENCE_SCHEMA_VERSION = "v1"
RAG_EVALUATION_PROTOCOL_VERSION = "localagent-rag-evaluation-execute.v1"

_RETRIEVAL_STATUSES = frozenset(
    {"SUCCEEDED", "EMPTY", "DEGRADED", "FAILED", "TIMED_OUT", "CANCELLED"}
)
_CAPTURE_STATUSES = frozenset({"COMPLETE", "PARTIAL", "FAILED"})
# 与 producer `_WIRE_ID` 一致：retrieval id / artifact id 片段只允许 wire-safe 字符。
_ARTIFACT_ID = re.compile(r"^rag-eval://([A-Za-z0-9-]+)/([A-Za-z0-9._~-]+)$")


class _RagSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: StrictStr
    collection: StrictStr
    display_name: StrictStr
    document_version: StrictStr


class _RetrievedRankedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: StrictStr
    chunk_id: StrictStr
    rank: int = Field(ge=1)
    retrieval_rank: int = Field(ge=1)
    rerank_rank: int | None = Field(default=None, ge=1)
    retrieval_score: float
    retrieval_score_kind: StrictStr
    retrieval_channels: list[StrictStr]
    rerank_score: float | None = None
    rerank_score_kind: StrictStr | None = None
    source: _RagSource
    page: int | None = None
    section: StrictStr | None = None
    sheet: StrictStr | None = None
    content_hash: StrictStr | None = None
    selected: bool


class _SelectedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: StrictStr
    chunk_id: StrictStr
    selection_rank: int = Field(ge=1)
    context_block_id: StrictStr
    citation_id: StrictStr
    context_content_hash: StrictStr
    text: StrictStr


class _Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_id: StrictStr
    document_id: StrictStr
    chunk_id: StrictStr
    context_block_id: StrictStr
    context_content_hash: StrictStr
    display_label: StrictStr
    page: int | None = None
    section: StrictStr | None = None


class _RagError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: StrictStr
    safe_error_code: StrictStr
    safe_message: StrictStr
    stage: StrictStr | None = None
    failed_source_count: int = 0


class _RagBudgetUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retrieval_calls: int = 0
    embedding_calls: int = 0
    vector_queries: int = 0
    keyword_queries: int = 0
    document_reads: int = 0
    context_chars: int = 0


class RagEvaluationArtifactV1(BaseModel):
    """LocalAgent RAG evaluation artifact 的严格消费侧视图。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: StrictStr
    artifact_id: StrictStr
    run_id: StrictStr
    attempt_id: StrictStr
    retrieval_id: StrictStr
    invocation_index: int = Field(ge=1)
    retrieval_status: StrictStr
    query: StrictStr
    rewritten_query: StrictStr
    retrieved_items: list[_RetrievedRankedItem]
    ranked_items: list[_RetrievedRankedItem]
    selected_items: list[_SelectedItem]
    citations: list[_Citation]
    retrieval_latency_ms: int | None = None
    rerank_latency_ms: int | None = None
    total_latency_ms: int
    degraded: bool
    degradation_reasons: list[StrictStr]
    error: _RagError | None = None
    budget_usage: _RagBudgetUsage

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value != RAG_ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {value}")
        return value

    @field_validator("retrieval_status")
    @classmethod
    def _retrieval_status(cls, value: str) -> str:
        if value not in _RETRIEVAL_STATUSES:
            raise ValueError(f"unsupported retrieval_status: {value}")
        return value

    @model_validator(mode="after")
    def _validate_identity_and_layers(self) -> "RagEvaluationArtifactV1":
        match = _ARTIFACT_ID.match(self.artifact_id)
        if not match:
            raise ValueError("invalid artifact_id format")
        run_id, retrieval_id = match.groups()
        if run_id != self.run_id or retrieval_id != self.retrieval_id:
            raise ValueError("artifact_id does not match run_id/retrieval_id")
        if self.attempt_id != self.run_id:
            raise ValueError("attempt_id must equal run_id")

        selected_ids = {(item.document_id, item.chunk_id) for item in self.selected_items}
        ranked_ids = {(item.document_id, item.chunk_id) for item in self.ranked_items}
        retrieved_ids = {(item.document_id, item.chunk_id) for item in self.retrieved_items}
        if not selected_ids <= ranked_ids <= retrieved_ids:
            raise ValueError("selected/ranked/retrieved identity invariant violated")
        return self


def validate_capture_status(value: str) -> None:
    """校验 response 级 capture status；无效即抛 ValueError。"""
    if value not in _CAPTURE_STATUSES:
        raise ValueError(f"unsupported capture_status: {value}")


def build_rag_artifact_evidence(
    artifact: RagEvaluationArtifactV1,
    capture_status: str,
) -> EvidenceRef:
    """把一个已解析 artifact 映射为 inline EvidenceRef（不新增 Artifact Store）。"""
    validate_capture_status(capture_status)
    return EvidenceRef(
        kind=RAG_ARTIFACT_EVIDENCE_KIND,
        identifier=artifact.artifact_id,
        media_type=RAG_ARTIFACT_MEDIA_TYPE,
        schema_version=RAG_ARTIFACT_EVIDENCE_SCHEMA_VERSION,
        metadata={
            "artifact_schema_version": artifact.schema_version,
            "artifact_id": artifact.artifact_id,
            "run_id": artifact.run_id,
            "retrieval_id": artifact.retrieval_id,
            "invocation_index": artifact.invocation_index,
            "retrieval_status": artifact.retrieval_status,
            "capture_status": capture_status,
            "payload": artifact.model_dump(mode="json"),
        },
    )


__all__ = [
    "RAG_ARTIFACT_EVIDENCE_KIND",
    "RAG_ARTIFACT_EVIDENCE_SCHEMA_VERSION",
    "RAG_ARTIFACT_MEDIA_TYPE",
    "RAG_ARTIFACT_SCHEMA_VERSION",
    "RAG_EVALUATION_PROTOCOL_VERSION",
    "RagEvaluationArtifactV1",
    "build_rag_artifact_evidence",
    "validate_capture_status",
]
