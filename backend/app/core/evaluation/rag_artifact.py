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
RAG_ARTIFACT_SCHEMA_VERSION_V2 = "rag-evaluation-artifact.v2"
RAG_ARTIFACT_EVIDENCE_KIND = "rag_evaluation_artifact"
RAG_ARTIFACT_MEDIA_TYPE = "application/vnd.agentevalops.rag-evaluation-artifact+json"
RAG_ARTIFACT_EVIDENCE_SCHEMA_VERSION = "v1"
RAG_EVALUATION_PROTOCOL_VERSION = "localagent-rag-evaluation-execute.v1"

_RETRIEVAL_STATUSES = frozenset(
    {"SUCCEEDED", "EMPTY", "DEGRADED", "FAILED", "TIMED_OUT", "CANCELLED"}
)
_CAPTURE_STATUSES = frozenset({"COMPLETE", "PARTIAL", "FAILED"})
# Stage5-Phase6-WP1 artifact v2：新增通道/评分语义（schema/plumbing 支持）。
# 同时保留历史 consumer 测试使用的 legacy shorthand（VECTOR / KEYWORD）。
_RETRIEVAL_CHANNELS = frozenset(
    {
        "VECTOR",
        "KEYWORD",
        "VECTOR_REWRITTEN_QUERY",
        "VECTOR_ORIGINAL_QUERY",
        "VECTOR_ORIGINAL_AND_REWRITTEN",
        "BM25",
        "RRF",
    }
)
_RETRIEVAL_SCORE_KINDS = frozenset(
    {
        "VECTOR",
        "KEYWORD",
        "VECTOR_NORMALIZED_RELEVANCE",
        "KEYWORD_FIXED_HEURISTIC",
        "HEURISTIC_RERANK",
        "BM25_RAW_SCORE",
        "RRF_SCORE",
    }
)
_LEGACY_V1_SHORTHAND = frozenset({"VECTOR", "KEYWORD"})
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
    dense_channel_rank: int | None = Field(default=None, ge=1)
    bm25_channel_rank: int | None = Field(default=None, ge=1)
    rrf_fused_rank: int | None = Field(default=None, ge=1)

    @field_validator("retrieval_score_kind")
    @classmethod
    def _retrieval_score_kind(cls, value: str) -> str:
        if value not in _RETRIEVAL_SCORE_KINDS:
            raise ValueError(f"unsupported retrieval_score_kind: {value}")
        return value

    @field_validator("rerank_score_kind")
    @classmethod
    def _rerank_score_kind(cls, value: str | None) -> str | None:
        if value is not None and value not in _RETRIEVAL_SCORE_KINDS:
            raise ValueError(f"unsupported rerank_score_kind: {value}")
        return value

    @field_validator("retrieval_channels")
    @classmethod
    def _retrieval_channels(cls, values: list[str]) -> list[str]:
        unknown = [value for value in values if value not in _RETRIEVAL_CHANNELS]
        if unknown:
            raise ValueError(f"unsupported retrieval_channels: {unknown}")
        return values


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
    bm25_queries: int = 0
    rrf_fusions: int = 0
    document_reads: int = 0
    context_chars: int = 0


class RagEvaluationArtifactV1(BaseModel):
    """LocalAgent RAG evaluation artifact 的严格消费侧视图。

    WP1：同时接受 ``rag-evaluation-artifact.v1`` 与 ``rag-evaluation-artifact.v2``
    （v2 为 v1 的超集，新增 snapshot 级 ``retrieval_strategy``/``provenance_sha256``
    与 BM25/RRF 通道/评分语义）。v1 payload 无新增字段仍可解析。
    """

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
    retrieval_strategy: StrictStr | None = None
    provenance_sha256: StrictStr | None = None

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value not in {RAG_ARTIFACT_SCHEMA_VERSION, RAG_ARTIFACT_SCHEMA_VERSION_V2}:
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

        if self.schema_version == RAG_ARTIFACT_SCHEMA_VERSION_V2:
            for item in (*self.retrieved_items, *self.ranked_items):
                if item.retrieval_score_kind in _LEGACY_V1_SHORTHAND:
                    raise ValueError("v2 does not accept legacy retrieval_score_kind shorthand")
                if any(
                    channel in _LEGACY_V1_SHORTHAND
                    for channel in item.retrieval_channels
                ):
                    raise ValueError("v2 does not accept legacy retrieval_channels shorthand")
                if item.rerank_score_kind in _LEGACY_V1_SHORTHAND:
                    raise ValueError("v2 does not accept legacy rerank_score_kind shorthand")

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
    "RAG_ARTIFACT_SCHEMA_VERSION_V2",
    "RAG_EVALUATION_PROTOCOL_VERSION",
    "RagEvaluationArtifactV1",
    "build_rag_artifact_evidence",
    "validate_capture_status",
]
