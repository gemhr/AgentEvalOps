"""Retrieval 质量指标 —— 基于 Ground Truth chunk identity 与 RAG Artifact 的纯函数计算。

本模块只做指标计算：不加载 Dataset、不访问数据库、不重新执行 retrieval、不修改 Artifact。
identity matching 统一使用 WP1 约定的 (document_id, chunk_id)，不通过 text similarity、
score、uri 或 index 重新判断。
"""

# ruff: noqa: D105, D415

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, TypeAlias

from app.core.evaluation.dataset import RetrievalGroundTruth
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1

DEFAULT_K_VALUES: Final[tuple[int, ...]] = (1, 5, 10)
RECALL_METRIC_NAME: Final[str] = "recall_at_k"
MRR_METRIC_NAME: Final[str] = "mrr"

MRR_SOURCE_RANKED: Final[str] = "ranked_items"
MRR_SOURCE_RETRIEVED_FALLBACK: Final[str] = "retrieved_items_fallback"
MRR_SOURCE_EMPTY: Final[str] = "empty"

ChunkIdentity: TypeAlias = tuple[str, str]


def _validate_k_values(k_values: Sequence[int]) -> tuple[int, ...]:
    values = tuple(k_values)
    if not values:
        raise ValueError("k_values must not be empty")
    for k in values:
        if not isinstance(k, int) or isinstance(k, bool) or k < 1:
            raise ValueError(f"k must be a positive integer: {k!r}")
    if len(values) != len(set(values)):
        raise ValueError("duplicate k is not allowed")
    return values


def _require_relevant_identities(ground_truth: RetrievalGroundTruth) -> frozenset[ChunkIdentity]:
    identities = ground_truth.chunk_identities()
    if not identities:
        raise ValueError("ground truth relevant_chunks must not be empty")
    return frozenset(identities)


def _retrieval_window_identities(
    artifact: RagEvaluationArtifactV1, k: int
) -> list[ChunkIdentity]:
    """返回 top-k 的 identity；Hybrid 使用唯一的 RRF fused ranking。

    Hybrid 的 ``retrieved_items`` 是 pre-fusion 多通道证据，同一 identity 可以
    合法出现多次，不能用于 Recall@K。BASELINE 保持既有 retrieved 语义。
    """
    if artifact.retrieval_strategy == "HYBRID_RRF":
        ordered = sorted(artifact.ranked_items, key=lambda item: item.rank)
    else:
        ordered = sorted(artifact.retrieved_items, key=lambda item: item.retrieval_rank)
    return [(item.document_id, item.chunk_id) for item in ordered[:k]]


@dataclass(frozen=True, slots=True)
class RecallAtKResult:
    """Recall@K 计算结果；values 与 k_values 按位置对齐。"""

    metric_name: str
    k_values: tuple[int, ...]
    values: tuple[float, ...]
    relevant_total: int

    def __post_init__(self) -> None:
        if self.metric_name != RECALL_METRIC_NAME:
            raise ValueError(f"unexpected metric_name: {self.metric_name}")
        if len(self.k_values) != len(self.values):
            raise ValueError("k_values and values must align")
        if self.relevant_total < 1:
            raise ValueError("relevant_total must be positive")
        for value in self.values:
            if not 0.0 <= value <= 1.0:
                raise ValueError("recall value must be within [0, 1]")

    def value_at(self, k: int) -> float:
        """返回指定 k 的 recall 值；k 不在本次计算范围内时抛 KeyError。"""
        return self.values[self.k_values.index(k)]

    def as_dict(self) -> dict[str, float]:
        """返回 JSON-ready 形式，例如 {"1": 0.33, "5": 0.66}。"""
        return {str(k): value for k, value in zip(self.k_values, self.values, strict=True)}


@dataclass(frozen=True, slots=True)
class MRRResult:
    """MRR 计算结果；source 记录 rank 事实来自 ranked_items 还是 fallback。"""

    metric_name: str
    value: float
    first_relevant_rank: int | None
    source: str

    def __post_init__(self) -> None:
        if self.metric_name != MRR_METRIC_NAME:
            raise ValueError(f"unexpected metric_name: {self.metric_name}")
        if not 0.0 <= self.value <= 1.0:
            raise ValueError("mrr value must be within [0, 1]")
        if self.value > 0.0 and self.first_relevant_rank is None:
            raise ValueError("positive mrr requires first_relevant_rank")
        if self.source not in (MRR_SOURCE_RANKED, MRR_SOURCE_RETRIEVED_FALLBACK, MRR_SOURCE_EMPTY):
            raise ValueError(f"unknown mrr source: {self.source}")


def calculate_recall_at_k(
    ground_truth: RetrievalGroundTruth,
    artifact: RagEvaluationArtifactV1,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
) -> RecallAtKResult:
    """计算 Recall@K：top-K 检索窗口内命中的 relevant chunk 数 / relevant chunk 总数。

    top-K 窗口取 retrieved_items 按 artifact 自身 retrieval_rank 的前 k 个 item；
    重复 identity 在窗口内只命中一次，但会占用一个排名槽位。
    """
    ks = _validate_k_values(k_values)
    relevant = _require_relevant_identities(ground_truth)
    values = tuple(
        len(relevant.intersection(_retrieval_window_identities(artifact, k))) / len(relevant)
        for k in ks
    )
    return RecallAtKResult(
        metric_name=RECALL_METRIC_NAME,
        k_values=ks,
        values=values,
        relevant_total=len(relevant),
    )


def calculate_mrr(
    ground_truth: RetrievalGroundTruth,
    artifact: RagEvaluationArtifactV1,
) -> MRRResult:
    """计算 MRR = 1 / 第一个 relevant chunk 的 rank。

    rank 事实优先取 ranked_items 的 rank 字段；ranked_items 为空时 fallback 到
    retrieved_items 的 retrieval_rank（记录 source）；两者皆空时 MRR=0。不重新排序、
    不用 score 重算排名。
    """
    relevant = _require_relevant_identities(ground_truth)
    if artifact.ranked_items:
        source = MRR_SOURCE_RANKED
        candidate_ranks = [
            (item.rank, (item.document_id, item.chunk_id)) for item in artifact.ranked_items
        ]
    elif artifact.retrieved_items:
        source = MRR_SOURCE_RETRIEVED_FALLBACK
        candidate_ranks = [
            (item.retrieval_rank, (item.document_id, item.chunk_id))
            for item in artifact.retrieved_items
        ]
    else:
        return MRRResult(
            metric_name=MRR_METRIC_NAME,
            value=0.0,
            first_relevant_rank=None,
            source=MRR_SOURCE_EMPTY,
        )
    relevant_ranks = [rank for rank, identity in candidate_ranks if identity in relevant]
    if not relevant_ranks:
        return MRRResult(
            metric_name=MRR_METRIC_NAME,
            value=0.0,
            first_relevant_rank=None,
            source=source,
        )
    first_rank = min(relevant_ranks)
    return MRRResult(
        metric_name=MRR_METRIC_NAME,
        value=1.0 / first_rank,
        first_relevant_rank=first_rank,
        source=source,
    )


__all__ = [
    "DEFAULT_K_VALUES",
    "MRRResult",
    "MRR_METRIC_NAME",
    "MRR_SOURCE_EMPTY",
    "MRR_SOURCE_RANKED",
    "MRR_SOURCE_RETRIEVED_FALLBACK",
    "RecallAtKResult",
    "RECALL_METRIC_NAME",
    "calculate_mrr",
    "calculate_recall_at_k",
]
