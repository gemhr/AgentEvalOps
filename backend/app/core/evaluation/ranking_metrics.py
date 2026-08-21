"""Ranking 质量指标 —— 基于 graded relevance Ground Truth 与 RAG Artifact 的 NDCG 纯函数计算。

本模块只做指标计算：不加载 Dataset、不访问数据库、不重新执行 rerank、不修改 Artifact。
排名事实唯一来源是 ranked_items[].rank（producer 声明的最终排名）；identity matching 使用
WP1 约定的 (document_id, chunk_id)，document_id 缺省时按 chunk_id 匹配并对歧义与
多个 GT identity 命中同一 artifact item 的重叠情况 fail closed。
"""

# ruff: noqa: D105, D415

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.core.evaluation.dataset import RankingGroundTruth
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1
from app.core.evaluation.retrieval_metrics import DEFAULT_K_VALUES, _validate_k_values

NDCG_METRIC_NAME: Final[str] = "ndcg_at_k"


def _gain(relevance: int) -> float:
    """返回标准指数增益 gain(rel) = 2^rel - 1。"""
    return 2.0**relevance - 1.0


def _discount(rank: int) -> float:
    """返回位置折损 log2(rank + 1)。"""
    return math.log2(rank + 1)


@dataclass(frozen=True, slots=True)
class NDCGAtKResult:
    """NDCG@K 计算结果；values 与 k_values 按位置对齐。"""

    metric_name: str
    k_values: tuple[int, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.metric_name != NDCG_METRIC_NAME:
            raise ValueError(f"unexpected metric_name: {self.metric_name}")
        if len(self.k_values) != len(self.values):
            raise ValueError("k_values and values must align")
        for value in self.values:
            if not 0.0 <= value <= 1.0:
                raise ValueError("ndcg value must be within [0, 1]")

    def value_at(self, k: int) -> float:
        """返回指定 k 的 NDCG 值；k 不在本次计算范围内时抛 KeyError。"""
        return self.values[self.k_values.index(k)]

    def as_dict(self) -> dict[str, float]:
        """返回 JSON-ready 形式，例如 {"1": 0.5, "5": 1.0}。"""
        return {str(k): value for k, value in zip(self.k_values, self.values, strict=True)}


def _matched_min_ranks(
    ground_truth: RankingGroundTruth, artifact: RagEvaluationArtifactV1
) -> dict[tuple[str | None, str], int]:
    """解析每个 graded item 在 ranked_items 中的首次（最小 rank）命中。

    document_id 存在时按 (document_id, chunk_id) 精确匹配；document_id 为 None 时按
    chunk_id 匹配，若同一 chunk_id 在 artifact 中出现于多个不同 document 则视为
    identity ambiguity 并 fail closed（抛 ValueError），不做静默猜测。重复 artifact
    identity 只取最小 rank，保证一个 GT item 不被重复命中。

    若两个不同的 GT identity（如 (None, chunk) 与 (doc, chunk)）解析到同一个
    artifact ranked item，视为 identity overlap 并 fail closed：不自动 merge
    relevance、不取 max、不静默去重。
    """
    if not ground_truth.graded_relevance:
        raise ValueError("ground truth graded_relevance must not be empty")

    min_rank_by_identity: dict[tuple[str, str], int] = {}
    documents_by_chunk: dict[str, set[str]] = {}
    for entry in artifact.ranked_items:
        identity = (entry.document_id, entry.chunk_id)
        current = min_rank_by_identity.get(identity)
        if current is None or entry.rank < current:
            min_rank_by_identity[identity] = entry.rank
        documents_by_chunk.setdefault(entry.chunk_id, set()).add(entry.document_id)

    matched: dict[tuple[str | None, str], int] = {}
    owner_by_artifact_identity: dict[tuple[str, str], tuple[str | None, str]] = {}
    for graded in ground_truth.graded_relevance:
        if graded.document_id is not None:
            artifact_identity = (graded.document_id, graded.chunk_id)
            rank = min_rank_by_identity.get(artifact_identity)
            if rank is None:
                continue
        else:
            documents = documents_by_chunk.get(graded.chunk_id, set())
            if not documents:
                continue
            if len(documents) > 1:
                raise ValueError(
                    f"ambiguous chunk identity: ground truth chunk {graded.chunk_id!r} without "
                    f"document_id matches multiple documents {sorted(documents)} in ranked_items"
                )
            document_id = next(iter(documents))
            artifact_identity = (document_id, graded.chunk_id)
            rank = min_rank_by_identity[artifact_identity]
        owner = owner_by_artifact_identity.get(artifact_identity)
        if owner is not None:
            raise ValueError(
                f"overlapping ground truth identities: {owner!r} and {graded.identity()!r} "
                f"both match ranked item {artifact_identity!r}"
            )
        owner_by_artifact_identity[artifact_identity] = graded.identity()
        matched[graded.identity()] = rank
    return matched


def calculate_ndcg_at_k(
    ground_truth: RankingGroundTruth,
    artifact: RagEvaluationArtifactV1,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
) -> NDCGAtKResult:
    """计算 NDCG@K = DCG@K / IDCG@K，使用指数增益 gain(rel) = 2^rel - 1。

    排名事实唯一来源是 ranked_items[].rank：不按 score 重排、不使用列表顺序、不重新
    rerank。系统 DCG 只累计每个 GT item 的首次（最小 rank）命中，重复 artifact
    identity 不重复贡献 gain；若多个 GT identity 解析到同一 artifact item（identity
    overlap）则 fail closed。未召回的 GT item 对 DCG 贡献 0 但仍参与 IDCG。IDCG 由
    graded_relevance 按 relevance 降序的理想排名构造；K 超过 GT 数量时使用全部 GT。
    IDCG 为 0（如全零 relevance）时 NDCG 定义为 0.0，不产生 NaN 或除零。
    """
    ks = _validate_k_values(k_values)
    matched = _matched_min_ranks(ground_truth, artifact)

    contributions = [
        (rank, graded.relevance)
        for graded in ground_truth.graded_relevance
        if (rank := matched.get(graded.identity())) is not None
    ]
    ideal_relevances = sorted(
        (graded.relevance for graded in ground_truth.graded_relevance), reverse=True
    )

    values = []
    for k in ks:
        dcg = sum(
            _gain(relevance) / _discount(rank)
            for rank, relevance in contributions
            if rank <= k
        )
        idcg = sum(
            _gain(relevance) / _discount(position + 1)
            for position, relevance in enumerate(ideal_relevances[:k])
        )
        values.append(dcg / idcg if idcg > 0.0 else 0.0)
    return NDCGAtKResult(metric_name=NDCG_METRIC_NAME, k_values=ks, values=tuple(values))


__all__ = [
    "NDCGAtKResult",
    "NDCG_METRIC_NAME",
    "calculate_ndcg_at_k",
]
