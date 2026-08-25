"""Source-preserving local HistoricalKnowledgeRetriever adapter。

WP2 的最小 Phase4 retrieval：在 `retrieval/phase4_retrieval_corpus.v1.json`
source-preserving corpus 上做确定性 lexical overlap 检索。它不是 BM25、RRF、
Cross-Encoder、embedding 或 vector DB；不访问外网；每个 hit 都携带由 corpus
构建时创建的 ``EvidenceRef``。``EvidenceRef`` 由 corpus/retriever 创建，
模型只能消费与传播。
"""

# ruff: noqa: D415

from __future__ import annotations

import math
import re
import string
from pathlib import Path

from app.core.feature_risk_review.contracts import EvidenceRef, _Contract
from app.core.feature_risk_review.errors import FeatureRiskReviewDataError
from app.core.feature_risk_review.ports import RetrievedKnowledgeFragment
from pydantic import Field, StrictStr

_DEFAULT_CORPUS_PATH = (
    Path(__file__).resolve().parents[2]
    / "evaluation_assets"
    / "feature_risk_review_v1"
    / "retrieval"
    / "phase4_retrieval_corpus.v1.json"
)

_DEFAULT_CORPUS_ID = "kubernetes-feature-risk-review-phase4.v1"

# 作为 historical-knowledge retriever 的领域相关加权：对真实历史知识（tracking issue、
# k/k issue snapshot）给予小的确定性 boost，使其能与其他 case 的 KEP 片段竞争，
# 而不淹没于查询自身 KEP 的长文本高分。数值很小，不改变相关性排序的主序。
_HISTORICAL_SOURCE_BOOST: dict[str, float] = {
    "kubernetes_issue_snapshot": 0.5,
    "github_enhancement_tracking_issue": 0.25,
}

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "will",
        "have",
        "has",
        "not",
        "can",
        "but",
        "its",
        "into",
        "than",
        "also",
        "may",
        "must",
        "such",
        "when",
        "which",
        "these",
        "those",
        "should",
        "would",
        "there",
        "their",
        "them",
        "been",
        "were",
        "was",
        "one",
        "two",
        "all",
        "any",
        "using",
        "used",
        "use",
        "over",
        "under",
        "about",
        "each",
        "both",
        "some",
    }
)


class CorpusChunk(_Contract):
    """corpus 中的一条 source-preserving 片段与其 EvidenceRef。"""

    chunk_id: StrictStr = Field(min_length=1)
    text: StrictStr = Field(min_length=1)
    evidence_ref: EvidenceRef


class RetrievalCorpus(_Contract):
    """WP2 source-preserving retrieval corpus artifact 的 schema。"""

    schema_version: StrictStr = Field(min_length=1)
    corpus_id: StrictStr = Field(min_length=1)
    source_project: StrictStr = Field(min_length=1)
    source_commit: StrictStr = Field(min_length=1)
    agent_visible_boundary: StrictStr = Field(min_length=1)
    chunks: list[CorpusChunk]


def _tokenize(text: str) -> list[str]:
    lowered = text.lower()
    table = str.maketrans({char: " " for char in string.punctuation})
    words = re.split(r"\s+", lowered.translate(table).strip())
    return [word for word in words if len(word) >= 2 and word not in _STOPWORDS]


def load_retrieval_corpus(path: Path | str | None = None) -> RetrievalCorpus:
    """从本地 corpus artifact 加载并验证 retrieval corpus。"""
    corpus_path = Path(path) if path is not None else _DEFAULT_CORPUS_PATH
    try:
        payload = corpus_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FeatureRiskReviewDataError(f"retrieval corpus not found: {corpus_path}") from exc
    return RetrievalCorpus.model_validate_json(payload)


class SourcePreservingLexicalRetriever:
    """确定性、离线、source-preserving 的 lexical overlap retriever。"""

    def __init__(self, *, corpus: RetrievalCorpus | None = None, path: Path | str | None = None) -> None:
        self._corpus = corpus if corpus is not None else load_retrieval_corpus(path)
        self._tokens_by_chunk: list[tuple[str, frozenset[str], CorpusChunk]] = []
        for chunk in self._corpus.chunks:
            self._tokens_by_chunk.append((chunk.chunk_id, frozenset(_tokenize(chunk.text)), chunk))

    @property
    def corpus(self) -> RetrievalCorpus:
        """返回已加载的 retrieval corpus。"""
        return self._corpus

    async def retrieve(self, *, query: str, top_k: int = 5) -> list[RetrievedKnowledgeFragment]:
        """按 lexical overlap 检索并返回 source-preserving fragments。"""
        if top_k < 1:
            return []
        query_tokens = frozenset(_tokenize(query))
        if not query_tokens:
            return []
        scored: list[tuple[float, str, CorpusChunk]] = []
        for chunk_id, chunk_tokens, chunk in self._tokens_by_chunk:
            overlap = len(query_tokens & chunk_tokens)
            if overlap == 0:
                continue
            score = overlap / math.sqrt(max(1, len(chunk_tokens)))
            score += _HISTORICAL_SOURCE_BOOST.get(chunk.evidence_ref.source_type, 0.0)
            scored.append((score, chunk_id, chunk))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            RetrievedKnowledgeFragment(
                source_fragment=chunk.text,
                evidence_ref=chunk.evidence_ref,
                relevance_score=round(score, 6),
            )
            for score, _chunk_id, chunk in scored[:top_k]
        ]


__all__ = [
    "CorpusChunk",
    "RetrievalCorpus",
    "SourcePreservingLexicalRetriever",
    "load_retrieval_corpus",
]