"""Stage5 Phase3 WP1 BM25-only runner 与 case-level complementarity."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.core.evaluation.beir_scifact import BeirScifactAsset
from app.core.evaluation.dataset import EvaluationDataset
from app.core.evaluation.document_metrics import DocumentProjection
from app.services.evaluation.beir_scifact_baseline import execute_beir_scifact_baseline
from app.services.evaluation.persistence import EvaluationPersistenceService
from app.services.evaluation.rag_baseline import execute_rag_quality_baseline

BM25_ALGORITHM_REF = "bm25-lucene-idf.v1"
BM25_TOKENIZER_REF = "bm25-unicode-lexical-tokenizer.v1"
BM25_K1 = 1.2
BM25_B = 0.75
BM25_CANDIDATE_LIMIT = 8
BM25_SCIFACT_REF = "stage5-phase3-wp1-beir-scifact-bm25-only.v1"
BM25_SYNTHETIC_REF = "stage5-phase3-wp1-synthetic-bm25-only.v1"


def _contract_metadata() -> dict[str, object]:
    return {
        "retrieval_channel": "BM25_ONLY",
        "algorithm_ref": BM25_ALGORITHM_REF,
        "tokenizer_ref": BM25_TOKENIZER_REF,
        "k1": BM25_K1,
        "b": BM25_B,
        "candidate_limit": BM25_CANDIDATE_LIMIT,
        "dense_fusion": "NOT_STARTED",
        "rrf": "NOT_STARTED",
        "cross_encoder": "NOT_STARTED",
    }


async def execute_beir_scifact_bm25(
    *,
    persistence: EvaluationPersistenceService,
    project_id: UUID,
    asset: BeirScifactAsset,
    base_url: str,
    document_projection: DocumentProjection,
    sparse_index_cache: Mapping[str, object],
) -> dict[str, object]:
    """复用现有 BEIR EvaluationLoop 执行 BM25-only channel."""
    metadata = _contract_metadata()
    return await execute_beir_scifact_baseline(
        persistence=persistence,
        project_id=project_id,
        asset=asset,
        base_url=base_url,
        document_projection=document_projection,
        baseline_ref=BM25_SCIFACT_REF,
        run_metadata={**metadata, "sparse_index_cache": dict(sparse_index_cache)},
        report_metadata={**metadata, "sparse_index_cache": dict(sparse_index_cache)},
        worker_ref="beir-scifact-bm25-v1",
    )


async def execute_synthetic_bm25(
    *,
    persistence: EvaluationPersistenceService,
    project_id: UUID,
    dataset: EvaluationDataset,
    base_url: str,
) -> dict[str, object]:
    """复用现有 synthetic EvaluationLoop 执行 BM25 smoke/regression."""
    metadata = _contract_metadata()
    return await execute_rag_quality_baseline(
        persistence=persistence,
        project_id=project_id,
        dataset=dataset,
        base_url=base_url,
        baseline_ref=BM25_SYNTHETIC_REF,
        run_metadata=metadata,
        report_metadata=metadata,
        worker_ref="synthetic-bm25-v1",
    )


def _hit(case: Mapping[str, Any], field: str, k: int) -> bool:
    relevant = set(case["qrels_document_ids"])
    return bool(relevant & set(case[field][:k]))


def analyze_complementarity(
    current_report: Mapping[str, Any], bm25_report: Mapping[str, Any]
) -> dict[str, object]:
    """按 query_id 对齐 Current 与 BM25 candidate/ranking evidence."""
    current_cases = {
        str(item["benchmark_query_id"]): item for item in current_report["case_results"]
    }
    bm25_cases = {
        str(item["benchmark_query_id"]): item for item in bm25_report["case_results"]
    }
    if current_cases.keys() != bm25_cases.keys() or len(current_cases) != 300:
        raise ValueError("BM25_COMPLEMENTARITY_QUERY_ALIGNMENT_FAILED")

    by_k: dict[str, object] = {}
    for k in (1, 3, 5):
        counts = {
            "current_hit_bm25_hit": 0,
            "current_hit_bm25_miss": 0,
            "current_miss_bm25_hit": 0,
            "current_miss_bm25_miss": 0,
        }
        for query_id in sorted(current_cases, key=lambda value: (int(value), value)):
            current_hit = _hit(current_cases[query_id], "ranked_document_ids", k)
            bm25_hit = _hit(bm25_cases[query_id], "ranked_document_ids", k)
            key = f"current_{'hit' if current_hit else 'miss'}_bm25_{'hit' if bm25_hit else 'miss'}"
            counts[key] += 1
        by_k[f"top{k}"] = {
            **counts,
            f"BM25_RESCUED_CURRENT_MISS@{k}": counts["current_miss_bm25_hit"],
            f"CURRENT_RESCUED_BM25_MISS@{k}": counts["current_hit_bm25_miss"],
        }

    dense_miss_query_ids = []
    rescued_dense_miss_query_ids = []
    for query_id, current_case in current_cases.items():
        relevant = set(current_case["qrels_document_ids"])
        if not relevant & set(current_case["retrieved_document_ids"]):
            dense_miss_query_ids.append(query_id)
            bm25_case = bm25_cases[query_id]
            if relevant & set(bm25_case["ranked_document_ids"]):
                rescued_dense_miss_query_ids.append(query_id)

    representative: dict[str, dict[str, object]] = {}
    categories = (
        ("bm25_clear_win", lambda c, b: not _hit(c, "ranked_document_ids", 5) and _hit(b, "ranked_document_ids", 1)),
        ("lexical_query_win", lambda c, b: not _hit(c, "ranked_document_ids", 5) and _hit(b, "ranked_document_ids", 1)),
        ("current_clear_win", lambda c, b: _hit(c, "ranked_document_ids", 1) and not _hit(b, "ranked_document_ids", 5)),
        (
            "semantic_query_bm25_degradation",
            lambda c, b: _hit(c, "ranked_document_ids", 1) and not _hit(b, "ranked_document_ids", 5),
        ),
        ("both_hit", lambda c, b: _hit(c, "ranked_document_ids", 5) and _hit(b, "ranked_document_ids", 5)),
        ("both_miss", lambda c, b: not _hit(c, "ranked_document_ids", 5) and not _hit(b, "ranked_document_ids", 5)),
        (
            "bm25_rescued_current_dense_miss",
            lambda c, b: not set(c["qrels_document_ids"]) & set(c["retrieved_document_ids"])
            and _hit(b, "ranked_document_ids", 5),
        ),
    )
    for name, predicate in categories:
        for query_id in sorted(current_cases, key=lambda value: (int(value), value)):
            current_case = current_cases[query_id]
            bm25_case = bm25_cases[query_id]
            if predicate(current_case, bm25_case):
                representative[name] = {
                    "query_id": query_id,
                    "query": bm25_case["query"],
                    "qrels_document_ids": bm25_case["qrels_document_ids"],
                    "current_ranked_document_ids": current_case["ranked_document_ids"],
                    "bm25_ranked_document_ids": bm25_case["ranked_document_ids"],
                    "truthfulness": "真实",
                }
                break

    return {
        "query_count": len(current_cases),
        "by_k": by_k,
        "current_dense_miss_count": len(dense_miss_query_ids),
        "bm25_rescued_current_dense_miss_count": len(rescued_dense_miss_query_ids),
        "bm25_rescued_current_dense_miss_query_ids": sorted(
            rescued_dense_miss_query_ids, key=lambda value: (int(value), value)
        ),
        "representative_cases": representative,
    }


def build_bm25_candidate_evidence(
    report: Mapping[str, Any], complementarity: Mapping[str, Any]
) -> dict[str, object]:
    """生成不复制 corpus 正文的 BM25 case-level evidence."""
    cases = []
    for item in report["case_results"]:
        cases.append(
            {
                "query_id": item["benchmark_query_id"],
                "bm25_chunk_ranking": item["ranked_chunk_ids"],
                "projected_document_ranking": item["ranked_document_ids"],
                "qrels_ids": item["qrels_document_ids"],
                "metrics": item["scores"],
                "latency_ms": item["retrieval_latency_ms"],
            }
        )
    return {
        "evidence_schema_version": "beir-scifact-bm25-candidate.v1",
        "benchmark_kind": report["benchmark_kind"],
        "algorithm_ref": BM25_ALGORITHM_REF,
        "tokenizer_ref": BM25_TOKENIZER_REF,
        "k1": BM25_K1,
        "b": BM25_B,
        "candidate_limit": BM25_CANDIDATE_LIMIT,
        "metrics": report["metrics"],
        "latency_ms": report["latency_ms"]["retrieval"],
        "sparse_index_cache": report["sparse_index_cache"],
        "complementarity": dict(complementarity),
        "cases": cases,
    }


__all__ = [
    "BM25_ALGORITHM_REF",
    "BM25_B",
    "BM25_CANDIDATE_LIMIT",
    "BM25_K1",
    "BM25_SCIFACT_REF",
    "BM25_SYNTHETIC_REF",
    "BM25_TOKENIZER_REF",
    "analyze_complementarity",
    "build_bm25_candidate_evidence",
    "execute_beir_scifact_bm25",
    "execute_synthetic_bm25",
]
