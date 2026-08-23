"""Hybrid RRF runner、诊断与 evidence projection 合同."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.evaluation import hybrid_rrf


def _case(query_id: int, relevant_rank: int | None) -> dict[str, Any]:
    relevant = f"relevant-{query_id}"
    ranking = [f"doc-{query_id}-{rank}" for rank in range(1, 9)]
    if relevant_rank is not None:
        ranking[relevant_rank - 1] = relevant
    return {
        "benchmark_query_id": str(query_id),
        "query": f"query {query_id}",
        "qrels_document_ids": [relevant],
        "ranked_document_ids": ranking,
        "ranked_chunk_ids": [[f"local-{query_id}", f"chunk-{query_id}"]],
        "scores": {"document_mrr": 0.0},
    }


def test_analysis_quantifies_rescue_regression_oracle_and_rank_transition() -> None:
    current = []
    bm25 = []
    rrf = []
    for query_id in range(1, 301):
        group = query_id % 6
        current.append(_case(query_id, {0: 2, 1: None, 2: 1, 3: 1, 4: 3}.get(group)))
        bm25.append(_case(query_id, {0: 1, 1: 1, 2: None, 3: 8, 4: 2}.get(group)))
        rrf.append(_case(query_id, {0: 1, 1: 2, 2: 1, 3: None, 4: 4}.get(group)))
    analysis = hybrid_rrf.analyze_hybrid_rrf(
        {"case_results": current}, {"case_results": bm25}, {"case_results": rrf}
    )
    top5 = analysis["by_k"]["top5"]
    assert top5["current_miss_rrf_hit"] == 50
    assert top5["current_hit_rrf_miss"] == 50
    assert top5["bm25_rescue_potential"] == 50
    assert top5["rrf_realized_rescue"] == 50
    assert top5["realized_rescue_rate"] == 1.0
    assert top5["net_hit_delta"] == 0
    assert top5["oracle_classification"] == "DERIVED_DIAGNOSTIC_ONLY"
    assert sum(analysis["rank_transition"].values()) == 300
    assert all(
        category in analysis["representative_cases"]
        for category in (
            "bm25_rescue_realized",
            "bm25_signal_not_promoted",
            "current_strong_preserved",
            "current_strong_degraded",
            "both_hit_rrf_improved",
            "both_miss",
        )
    )


def test_analysis_fails_closed_on_query_alignment() -> None:
    with pytest.raises(ValueError, match="QUERY_ALIGNMENT"):
        hybrid_rrf.analyze_hybrid_rrf(
            {"case_results": [_case(1, 1)]},
            {"case_results": [_case(2, 1)]},
            {"case_results": [_case(1, 1)]},
        )


def test_provenance_alignment_and_evidence_keep_required_fields() -> None:
    case = _case(1, 1)
    report = {
        "benchmark_kind": "BEIR_SCIFACT_LOCALAGENT_ADAPTED",
        "metrics": {"document_mrr": 1.0},
        "dense_index_cache": {"status": "CACHE_HIT"},
        "sparse_index_cache": {"status": "CACHE_HIT"},
        "case_results": [case],
    }
    row = {
        "query_sha256": hashlib.sha256(case["query"].encode()).hexdigest(),
        "current_chunk_ranking": [["doc", "current"]],
        "bm25_chunk_ranking": [["doc", "bm25"]],
        "fused_items": [
            {
                "document_id": "doc",
                "chunk_id": "current",
                "current_rank": 1,
                "bm25_rank": None,
                "rrf_score": 1 / 61,
                "rrf_rank": 1,
                "source_channels": [hybrid_rrf.CURRENT_CHANNEL_REF],
            }
        ],
        "latency_ms": {
            "current_channel": 10.0,
            "bm25_channel": 2.0,
            "rrf_fusion": 0.1,
            "hybrid_total": 12.2,
        },
    }
    aligned = hybrid_rrf.align_provenance(report, [row])
    evidence = hybrid_rrf.build_rrf_candidate_evidence(report, {"by_k": {}}, aligned)
    assert set(evidence["cases"][0]) == {
        "query_id",
        "qrels_document_ids",
        "current_chunk_ranks",
        "bm25_chunk_ranks",
        "rrf_fused_chunks",
        "projected_document_ranking",
        "metrics",
        "latency_ms",
    }
    assert evidence["latency_ms"]["rrf_fusion"] == {"mean": 0.1, "p50": 0.1, "p95": 0.1}
    assert evidence["cases"][0]["current_chunk_ranks"] == [
        {"document_id": "doc", "chunk_id": "current", "rank": 1}
    ]
    assert "query" not in evidence["cases"][0]


def test_provenance_missing_or_duplicate_fails_closed() -> None:
    report = {"case_results": [_case(1, 1)]}
    with pytest.raises(ValueError, match="MISSING"):
        hybrid_rrf.align_provenance(report, [])
    digest = hashlib.sha256(b"query 1").hexdigest()
    with pytest.raises(ValueError, match="INVALID"):
        hybrid_rrf.align_provenance(
            report, [{"query_sha256": digest}, {"query_sha256": digest}]
        )


@pytest.mark.asyncio
async def test_wrappers_reuse_existing_evaluation_domain(monkeypatch) -> None:
    scifact = AsyncMock(return_value={"kind": "scifact"})
    synthetic = AsyncMock(return_value={"kind": "synthetic"})
    monkeypatch.setattr(hybrid_rrf, "execute_beir_scifact_baseline", scifact)
    monkeypatch.setattr(hybrid_rrf, "execute_rag_quality_baseline", synthetic)
    marker = object()
    assert await hybrid_rrf.execute_beir_scifact_hybrid_rrf(
        persistence=marker,
        project_id=uuid4(),
        asset=marker,
        base_url="http://localhost",
        document_projection=marker,
        dense_index_cache={"identity": "dense"},
        sparse_index_cache={"identity": "bm25"},
    ) == {"kind": "scifact"}
    assert await hybrid_rrf.execute_synthetic_hybrid_rrf(
        persistence=marker,
        project_id=uuid4(),
        dataset=marker,
        base_url="http://localhost",
    ) == {"kind": "synthetic"}
    metadata = scifact.await_args.kwargs["report_metadata"]
    assert metadata["rrf_k"] == 60
    assert metadata["production_default_switch"] == "NOT_DONE"
    assert synthetic.await_args.kwargs["baseline_ref"] == hybrid_rrf.HYBRID_SYNTHETIC_REF
