"""Document-level 投影与指标 —— Chunk ranking 到 benchmark document ranking 的纯函数投影。

BEIR qrels 是 document-level ground truth；LocalAgent retrieval 返回 chunk-level ranking。
本模块把 artifact 的 retrieved/ranked/selected chunk 序列投影为去重后的 document 序列
（同一 document 由其在当前 stage 排名最高的 chunk 决定 document rank），投影结果仍是
RagEvaluationArtifactV1，从而直接复用现有 Recall@K / MRR / NDCG 纯函数。

投影对未知 document identity fail closed（UnknownBenchmarkDocumentError），不静默丢弃
或猜测 benchmark document id。
"""

# ruff: noqa: D105, D415

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1

DOCUMENT_PROJECTION_ERROR_CODE: Final[str] = "UNKNOWN_BENCHMARK_DOCUMENT"


class UnknownBenchmarkDocumentError(ValueError):
    """Artifact 中出现无法映射回 benchmark document 的 document identity。"""

    def __init__(self, document_id: str) -> None:
        super().__init__(
            f"{DOCUMENT_PROJECTION_ERROR_CODE}: artifact document identity "
            f"{document_id!r} cannot be projected to a benchmark document"
        )
        self.document_id = document_id


class DocumentProjection:
    """LocalAgent chunk document_id 到 benchmark document id 的不可变投影映射。"""

    __slots__ = ("_mapping", "_reverse")

    def __init__(self, mapping: Mapping[str, str]) -> None:
        values = list(mapping.values())
        if len(values) != len(set(values)):
            raise ValueError("benchmark document ids must be unique in projection mapping")
        self._mapping: dict[str, str] = dict(mapping)
        self._reverse: dict[str, str] = {value: key for key, value in mapping.items()}

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> "DocumentProjection":
        """从 BEIR ingest manifest 的 documents 列表构造投影映射。"""
        documents = manifest.get("documents")
        if not isinstance(documents, Sequence) or not documents:
            raise ValueError("manifest must contain a non-empty documents list")
        mapping: dict[str, str] = {}
        for entry in documents:
            if not isinstance(entry, Mapping):
                raise ValueError("manifest document entry must be an object")
            document_id = entry.get("document_id")
            benchmark_document_id = entry.get("benchmark_document_id")
            if not isinstance(document_id, str) or not isinstance(benchmark_document_id, str):
                raise ValueError("manifest document entry requires string identity fields")
            if document_id in mapping and mapping[document_id] != benchmark_document_id:
                raise ValueError(f"conflicting projection for document {document_id!r}")
            mapping[document_id] = benchmark_document_id
        return cls(mapping)

    def benchmark_document_id(self, document_id: str) -> str:
        """返回 benchmark document id；未知 identity fail closed。"""
        try:
            return self._mapping[document_id]
        except KeyError as error:
            raise UnknownBenchmarkDocumentError(document_id) from error

    def localagent_document_id(self, benchmark_document_id: str) -> str:
        """返回 LocalAgent document_id；未知 benchmark id fail closed。"""
        try:
            return self._reverse[benchmark_document_id]
        except KeyError as error:
            raise UnknownBenchmarkDocumentError(benchmark_document_id) from error

    def __len__(self) -> int:
        return len(self._mapping)

    def document_ids(self) -> frozenset[str]:
        """返回全部 LocalAgent document identity。"""
        return frozenset(self._mapping)


def project_document_sequence(
    ordered_items: Sequence[tuple[int, str]],
    projection: DocumentProjection,
) -> list[tuple[str, int]]:
    """把按 stage rank 排序的 (rank, document_id) 序列投影为去重 document 序列。

    同一 benchmark document 的重复 chunk 只保留最小 rank（该 stage 排名最高的 chunk
    决定 document 位置）；输出顺序按该最小 rank 排列，并重新编号为 1..n。
    """
    best_rank: dict[str, int] = {}
    for rank, document_id in ordered_items:
        benchmark_id = projection.benchmark_document_id(document_id)
        current = best_rank.get(benchmark_id)
        if current is None or rank < current:
            best_rank[benchmark_id] = rank
    ordered = sorted(best_rank.items(), key=lambda entry: entry[1])
    return [(benchmark_id, position) for position, (benchmark_id, _rank) in enumerate(ordered, 1)]


def project_artifact_to_documents(
    artifact: RagEvaluationArtifactV1,
    projection: DocumentProjection,
) -> RagEvaluationArtifactV1:
    """把 chunk-level artifact 投影为 document-level artifact（复用 v1 schema）。

    投影后 document identity 使用 (benchmark_document_id, benchmark_document_id)；
    selected ⊆ ranked ⊆ retrieved 的层间不变量在投影后保持。分数、channels、source
    等字段取自决定该 document rank 的 chunk。
    """
    retrieved_ordered = sorted(artifact.retrieved_items, key=lambda item: item.retrieval_rank)
    retrieved_documents = project_document_sequence(
        [(item.retrieval_rank, item.document_id) for item in retrieved_ordered], projection
    )
    retrieved_by_document = _first_by_document(retrieved_ordered, projection)
    retrieved_position = {doc: position for doc, position in retrieved_documents}

    ranked_ordered = sorted(artifact.ranked_items, key=lambda item: item.rank)
    ranked_documents = project_document_sequence(
        [(item.rank, item.document_id) for item in ranked_ordered], projection
    )
    ranked_by_document = _first_by_document(ranked_ordered, projection)

    selected_ordered = sorted(artifact.selected_items, key=lambda item: item.selection_rank)
    selected_documents = project_document_sequence(
        [(item.selection_rank, item.document_id) for item in selected_ordered], projection
    )
    selected_by_document = _first_by_document(selected_ordered, projection)

    payload = artifact.model_dump(mode="json")
    payload["retrieved_items"] = [
        _projected_ranked_item(
            retrieved_by_document[document_id], document_id, position, position, position
        )
        for document_id, position in retrieved_documents
    ]
    payload["ranked_items"] = [
        _projected_ranked_item(
            ranked_by_document[document_id],
            document_id,
            position,
            retrieved_position.get(document_id, position),
            position,
        )
        for document_id, position in ranked_documents
    ]
    payload["selected_items"] = [
        _projected_selected_item(selected_by_document[document_id], document_id, position)
        for document_id, position in selected_documents
    ]
    payload["citations"] = []
    return RagEvaluationArtifactV1.model_validate(payload)


def _first_by_document(items: Sequence[Any], projection: DocumentProjection) -> dict[str, Any]:
    """按已排序 item 序列保留每个 document 的首个（最优 rank）item。"""
    result: dict[str, Any] = {}
    for item in items:
        benchmark_id = projection.benchmark_document_id(item.document_id)
        if benchmark_id not in result:
            result[benchmark_id] = item
    return result


def _projected_ranked_item(
    item: Any,
    document_id: str,
    rank: int,
    retrieval_rank: int,
    rerank_rank: int,
) -> dict[str, Any]:
    payload = item.model_dump(mode="json")
    payload["document_id"] = document_id
    payload["chunk_id"] = document_id
    payload["rank"] = rank
    payload["retrieval_rank"] = retrieval_rank
    payload["rerank_rank"] = rerank_rank
    return payload


def _projected_selected_item(item: Any, document_id: str, selection_rank: int) -> dict[str, Any]:
    payload = item.model_dump(mode="json")
    payload["document_id"] = document_id
    payload["chunk_id"] = document_id
    payload["selection_rank"] = selection_rank
    return payload


__all__ = [
    "DOCUMENT_PROJECTION_ERROR_CODE",
    "DocumentProjection",
    "UnknownBenchmarkDocumentError",
    "project_artifact_to_documents",
    "project_document_sequence",
]
