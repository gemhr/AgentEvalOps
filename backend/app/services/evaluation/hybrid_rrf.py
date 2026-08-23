"""Stage5 Phase3 WP2 Current + BM25 + RRF evaluation 与诊断."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from statistics import mean, median
from typing import Any
from uuid import UUID

from app.core.evaluation.beir_scifact import BeirScifactAsset
from app.core.evaluation.dataset import EvaluationDataset
from app.core.evaluation.document_metrics import DocumentProjection
from app.services.evaluation.beir_scifact_baseline import execute_beir_scifact_baseline
from app.services.evaluation.persistence import EvaluationPersistenceService
from app.services.evaluation.rag_baseline import execute_rag_quality_baseline

RRF_ALGORITHM_REF = "rrf.v1"
RRF_K = 60
CURRENT_CHANNEL_REF = "current-dense-led-ranked.v1"
BM25_CHANNEL_REF = "bm25-lucene-idf.v1"
PER_CHANNEL_CANDIDATE_LIMIT = 8
PRE_FUSION_UNION_MAX = 16
FINAL_FUSED_CANDIDATE_LIMIT = 8
HYBRID_SCIFACT_REF = "stage5-phase3-wp2-beir-scifact-hybrid-rrf.v1"
HYBRID_SYNTHETIC_REF = "stage5-phase3-wp2-synthetic-hybrid-rrf.v1"


def _contract_metadata() -> dict[str, object]:
    return {
        "retrieval_channel": "CURRENT_DENSE_LED_PLUS_BM25_RRF",
        "algorithm_ref": RRF_ALGORITHM_REF,
        "rrf_k": RRF_K,
        "left_channel": CURRENT_CHANNEL_REF,
        "right_channel": BM25_CHANNEL_REF,
        "per_channel_candidate_limit": PER_CHANNEL_CANDIDATE_LIMIT,
        "pre_fusion_union_max": PRE_FUSION_UNION_MAX,
        "final_fused_candidate_limit": FINAL_FUSED_CANDIDATE_LIMIT,
        "fusion_unit": "CHUNK_IDENTITY",
        "execution_mode": "sequential",
        "cross_encoder": "NOT_STARTED",
        "no_answer_optimization": "NOT_STARTED",
        "context_selection_optimization": "NOT_STARTED",
        "production_default_switch": "NOT_DONE",
    }


async def execute_beir_scifact_hybrid_rrf(
    *,
    persistence: EvaluationPersistenceService,
    project_id: UUID,
    asset: BeirScifactAsset,
    base_url: str,
    document_projection: DocumentProjection,
    dense_index_cache: Mapping[str, object],
    sparse_index_cache: Mapping[str, object],
) -> dict[str, object]:
    """复用既有 EvaluationRun/Attempt/Result 与 BEIR projection."""
    metadata = {
        **_contract_metadata(),
        "dense_index_cache": dict(dense_index_cache),
        "sparse_index_cache": dict(sparse_index_cache),
    }
    return await execute_beir_scifact_baseline(
        persistence=persistence,
        project_id=project_id,
        asset=asset,
        base_url=base_url,
        document_projection=document_projection,
        baseline_ref=HYBRID_SCIFACT_REF,
        run_metadata=metadata,
        report_metadata=metadata,
        worker_ref="beir-scifact-hybrid-rrf-v1",
    )


async def execute_synthetic_hybrid_rrf(
    *,
    persistence: EvaluationPersistenceService,
    project_id: UUID,
    dataset: EvaluationDataset,
    base_url: str,
) -> dict[str, object]:
    """复用 frozen synthetic Dataset 与现有聚合器."""
    metadata = _contract_metadata()
    return await execute_rag_quality_baseline(
        persistence=persistence,
        project_id=project_id,
        dataset=dataset,
        base_url=base_url,
        baseline_ref=HYBRID_SYNTHETIC_REF,
        run_metadata=metadata,
        report_metadata=metadata,
        worker_ref="synthetic-hybrid-rrf-v1",
    )


def _cases(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(item["benchmark_query_id"]): item for item in report["case_results"]}


def _hit(case: Mapping[str, Any], k: int) -> bool:
    return bool(set(case["qrels_document_ids"]) & set(case["ranked_document_ids"][:k]))


def _first_relevant_rank(case: Mapping[str, Any]) -> int | None:
    relevant = set(case["qrels_document_ids"])
    for rank, document_id in enumerate(case["ranked_document_ids"], 1):
        if document_id in relevant:
            return rank
    return None


def _ordered_query_ids(values: Mapping[str, Any]) -> list[str]:
    return sorted(values, key=lambda value: (int(value), value))


def _representative(
    current: Mapping[str, Mapping[str, Any]],
    bm25: Mapping[str, Mapping[str, Any]],
    rrf: Mapping[str, Mapping[str, Any]],
) -> dict[str, object]:
    predicates = {
        "bm25_rescue_realized": lambda c, b, r: not _hit(c, 5) and _hit(b, 5) and _hit(r, 5),
        "bm25_signal_not_promoted": lambda c, b, r: not _hit(c, 5) and _hit(b, 5) and not _hit(r, 5),
        "current_strong_preserved": lambda c, b, r: _hit(c, 1) and not _hit(b, 5) and _hit(r, 1),
        "current_strong_degraded": lambda c, b, r: _hit(c, 1) and not _hit(r, 1),
        "both_hit_rrf_improved": lambda c, b, r: _hit(c, 5)
        and _hit(b, 5)
        and (_first_relevant_rank(r) or 10**9) < (_first_relevant_rank(c) or 10**9),
        "both_miss": lambda c, b, r: not _hit(c, 5) and not _hit(b, 5),
    }
    result: dict[str, object] = {}
    for category, predicate in predicates.items():
        result[category] = "NOT_AVAILABLE"
        for query_id in _ordered_query_ids(current):
            if predicate(current[query_id], bm25[query_id], rrf[query_id]):
                result[category] = {
                    "query_id": query_id,
                    "query": rrf[query_id]["query"],
                    "qrels_document_ids": rrf[query_id]["qrels_document_ids"],
                    "current_ranked_document_ids": current[query_id]["ranked_document_ids"],
                    "bm25_ranked_document_ids": bm25[query_id]["ranked_document_ids"],
                    "rrf_ranked_document_ids": rrf[query_id]["ranked_document_ids"],
                    "truthfulness": "真实",
                }
                break
    return result


def analyze_hybrid_rrf(
    current_report: Mapping[str, Any],
    bm25_report: Mapping[str, Any],
    rrf_report: Mapping[str, Any],
) -> dict[str, object]:
    """量化 rescue、regression、oracle upper bound 与 relevant-rank transition."""
    current = _cases(current_report)
    bm25 = _cases(bm25_report)
    rrf = _cases(rrf_report)
    if current.keys() != bm25.keys() or current.keys() != rrf.keys() or len(current) != 300:
        raise ValueError("HYBRID_RRF_QUERY_ALIGNMENT_FAILED")

    by_k: dict[str, object] = {}
    for k in (1, 3, 5):
        counts = {
            "current_miss_rrf_hit": 0,
            "current_hit_rrf_miss": 0,
            "current_hit_rrf_hit": 0,
            "current_miss_rrf_miss": 0,
        }
        potential = 0
        oracle = 0
        for query_id in _ordered_query_ids(current):
            current_hit = _hit(current[query_id], k)
            bm25_hit = _hit(bm25[query_id], k)
            rrf_hit = _hit(rrf[query_id], k)
            counts[
                f"current_{'hit' if current_hit else 'miss'}_rrf_{'hit' if rrf_hit else 'miss'}"
            ] += 1
            potential += int(not current_hit and bm25_hit)
            oracle += int(current_hit or bm25_hit)
        rescued = counts["current_miss_rrf_hit"]
        lost = counts["current_hit_rrf_miss"]
        by_k[f"top{k}"] = {
            **counts,
            "bm25_rescue_potential": potential,
            "rrf_realized_rescue": rescued,
            "realized_rescue_rate": rescued / potential if potential else None,
            "rrf_lost_current_hit": lost,
            "net_hit_delta": rescued - lost,
            "oracle_union_hit_count": oracle,
            "oracle_union_hit_rate": oracle / len(current),
            "oracle_classification": "DERIVED_DIAGNOSTIC_ONLY",
        }

    transitions = {"improved": 0, "degraded": 0, "unchanged": 0}
    for query_id in _ordered_query_ids(current):
        current_rank = _first_relevant_rank(current[query_id])
        rrf_rank = _first_relevant_rank(rrf[query_id])
        if rrf_rank is not None and (current_rank is None or rrf_rank < current_rank):
            transitions["improved"] += 1
        elif current_rank is not None and (rrf_rank is None or rrf_rank > current_rank):
            transitions["degraded"] += 1
        else:
            transitions["unchanged"] += 1

    return {
        "query_count": len(current),
        "by_k": by_k,
        "rank_transition": transitions,
        "representative_cases": _representative(current, bm25, rrf),
    }


def _latency(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "mean": float(mean(ordered)),
        "p50": float(median(ordered)),
        "p95": float(ordered[p95_index]),
    }


def align_provenance(
    report: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    """按 query digest 对齐 LocalAgent sidecar，拒绝缺失或重复."""
    by_digest: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        digest = row.get("query_sha256")
        if not isinstance(digest, str) or digest in by_digest:
            raise ValueError("HYBRID_RRF_PROVENANCE_INVALID")
        by_digest[digest] = row
    aligned: dict[str, Mapping[str, Any]] = {}
    for case in report["case_results"]:
        digest = hashlib.sha256(case["query"].encode("utf-8")).hexdigest()
        row = by_digest.get(digest)
        if row is None:
            raise ValueError("HYBRID_RRF_PROVENANCE_MISSING")
        aligned[str(case["benchmark_query_id"])] = row
    if len(aligned) != len(report["case_results"]):
        raise ValueError("HYBRID_RRF_PROVENANCE_ALIGNMENT_FAILED")
    return aligned


def build_rrf_candidate_evidence(
    report: Mapping[str, Any],
    analysis: Mapping[str, Any],
    provenance: Mapping[str, Mapping[str, Any]],
) -> dict[str, object]:
    """生成不复制 SciFact corpus 的 case-level RRF evidence."""
    cases = []
    latency_values: dict[str, list[float]] = {
        "current_channel": [],
        "bm25_channel": [],
        "rrf_fusion": [],
        "hybrid_total": [],
    }

    def ranked_chunks(values: Sequence[Sequence[str]]) -> list[dict[str, object]]:
        return [
            {"document_id": value[0], "chunk_id": value[1], "rank": rank}
            for rank, value in enumerate(values, 1)
        ]

    for item in report["case_results"]:
        query_id = str(item["benchmark_query_id"])
        row = provenance[query_id]
        latency = row["latency_ms"]
        for name in latency_values:
            latency_values[name].append(float(latency[name]))
        cases.append(
            {
                "query_id": query_id,
                "qrels_document_ids": item["qrels_document_ids"],
                "current_chunk_ranks": ranked_chunks(row["current_chunk_ranking"]),
                "bm25_chunk_ranks": ranked_chunks(row["bm25_chunk_ranking"]),
                "rrf_fused_chunks": row["fused_items"],
                "projected_document_ranking": item["ranked_document_ids"],
                "metrics": item["scores"],
                "latency_ms": latency,
            }
        )
    return {
        "evidence_schema_version": "beir-scifact-rrf-candidate.v1",
        "benchmark_kind": report["benchmark_kind"],
        **_contract_metadata(),
        "metrics": report["metrics"],
        "latency_ms": {name: _latency(values) for name, values in latency_values.items()},
        "analysis": dict(analysis),
        "dense_index_cache": report["dense_index_cache"],
        "sparse_index_cache": report["sparse_index_cache"],
        "cases": cases,
    }


__all__ = [
    "BM25_CHANNEL_REF",
    "CURRENT_CHANNEL_REF",
    "FINAL_FUSED_CANDIDATE_LIMIT",
    "HYBRID_SCIFACT_REF",
    "HYBRID_SYNTHETIC_REF",
    "PER_CHANNEL_CANDIDATE_LIMIT",
    "PRE_FUSION_UNION_MAX",
    "RRF_ALGORITHM_REF",
    "RRF_K",
    "align_provenance",
    "analyze_hybrid_rrf",
    "build_rrf_candidate_evidence",
    "execute_beir_scifact_hybrid_rrf",
    "execute_synthetic_hybrid_rrf",
]
