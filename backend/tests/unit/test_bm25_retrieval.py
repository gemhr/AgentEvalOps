"""BM25-only runner metadata 与 complementarity 合同."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.evaluation import bm25_retrieval


def _case(query_id: int, current_rank: int | None, *, retrieved: bool = True) -> dict[str, Any]:
    relevant = f"relevant-{query_id}"
    ranking = [f"d-{query_id}-{index}" for index in range(1, 9)]
    if current_rank is not None:
        ranking[current_rank - 1] = relevant
    retrieved_ids = list(ranking)
    if retrieved and relevant not in retrieved_ids:
        retrieved_ids[-1] = relevant
    return {
        "benchmark_query_id": str(query_id),
        "query": f"query {query_id}",
        "qrels_document_ids": [relevant],
        "retrieved_document_ids": retrieved_ids if retrieved else ranking[:7],
        "ranked_document_ids": ranking,
        "ranked_chunk_ids": [[f"local-{query_id}", f"chunk-{query_id}"]],
        "scores": {},
        "retrieval_latency_ms": 1,
    }


def test_complementarity_aligns_300_queries_and_counts_each_quadrant() -> None:
    current = []
    bm25 = []
    for query_id in range(1, 301):
        group = query_id % 4
        current.append(_case(query_id, 1 if group in {0, 1} else None, retrieved=group != 2))
        bm25.append(_case(query_id, 1 if group in {0, 2} else None))
    analysis = bm25_retrieval.analyze_complementarity(
        {"case_results": current}, {"case_results": bm25}
    )
    top5 = analysis["by_k"]["top5"]
    assert top5 == {
        "current_hit_bm25_hit": 75,
        "current_hit_bm25_miss": 75,
        "current_miss_bm25_hit": 75,
        "current_miss_bm25_miss": 75,
        "BM25_RESCUED_CURRENT_MISS@5": 75,
        "CURRENT_RESCUED_BM25_MISS@5": 75,
    }
    assert analysis["current_dense_miss_count"] == 75
    assert analysis["bm25_rescued_current_dense_miss_count"] == 75


def test_complementarity_fails_closed_on_query_alignment() -> None:
    with pytest.raises(ValueError, match="QUERY_ALIGNMENT"):
        bm25_retrieval.analyze_complementarity(
            {"case_results": [_case(1, 1)]}, {"case_results": [_case(2, 1)]}
        )


def test_candidate_evidence_keeps_only_required_case_level_fields() -> None:
    report = {
        "benchmark_kind": "BEIR_SCIFACT_LOCALAGENT_ADAPTED",
        "metrics": {"document_mrr": 0.5},
        "latency_ms": {"retrieval": {"mean": 1.0, "p50": 1.0, "p95": 1.0}},
        "sparse_index_cache": {"identity": "cache"},
        "case_results": [_case(1, 1)],
    }
    evidence = bm25_retrieval.build_bm25_candidate_evidence(report, {"query_count": 1})
    assert set(evidence["cases"][0]) == {
        "query_id",
        "bm25_chunk_ranking",
        "projected_document_ranking",
        "qrels_ids",
        "metrics",
        "latency_ms",
    }
    assert "query" not in evidence["cases"][0]


@pytest.mark.asyncio
async def test_bm25_wrappers_reuse_existing_runners(monkeypatch) -> None:
    scifact = AsyncMock(return_value={"kind": "scifact"})
    synthetic = AsyncMock(return_value={"kind": "synthetic"})
    monkeypatch.setattr(bm25_retrieval, "execute_beir_scifact_baseline", scifact)
    monkeypatch.setattr(bm25_retrieval, "execute_rag_quality_baseline", synthetic)
    marker = object()

    assert await bm25_retrieval.execute_beir_scifact_bm25(
        persistence=marker,
        project_id=uuid4(),
        asset=marker,
        base_url="http://localhost",
        document_projection=marker,
        sparse_index_cache={"identity": "cache"},
    ) == {"kind": "scifact"}
    assert await bm25_retrieval.execute_synthetic_bm25(
        persistence=marker,
        project_id=uuid4(),
        dataset=marker,
        base_url="http://localhost",
    ) == {"kind": "synthetic"}
    assert scifact.await_args.kwargs["baseline_ref"] == bm25_retrieval.BM25_SCIFACT_REF
    assert synthetic.await_args.kwargs["baseline_ref"] == bm25_retrieval.BM25_SYNTHETIC_REF
    assert scifact.await_args.kwargs["report_metadata"]["rrf"] == "NOT_STARTED"
