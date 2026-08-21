"""Ranking quality metric - NDCG@K pure-function focused tests."""

from __future__ import annotations

import math

import pytest

from app.core.evaluation.dataset import RankingGroundTruth, validate_case
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1
from app.core.evaluation.ranking_metrics import NDCG_METRIC_NAME, calculate_ndcg_at_k

RUN_ID = "11111111-1111-4111-8111-111111111111"
LOG2_3 = math.log2(3)
LOG2_4 = math.log2(4)


def gt(*graded: tuple[str | None, str, int]) -> RankingGroundTruth:
    entries: list[dict[str, object]] = []
    for document_id, chunk_id, relevance in graded:
        entry: dict[str, object] = {"chunk_id": chunk_id, "relevance": relevance}
        if document_id is not None:
            entry["document_id"] = document_id
        entries.append(entry)
    return RankingGroundTruth.model_validate({"graded_relevance": entries})


def item(doc: str, chunk: str, rank: int, retrieval_rank: int | None = None) -> dict[str, object]:
    return {
        "document_id": doc,
        "chunk_id": chunk,
        "rank": rank,
        "retrieval_rank": rank if retrieval_rank is None else retrieval_rank,
        "retrieval_score": 0.9,
        "retrieval_score_kind": "VECTOR_NORMALIZED_RELEVANCE",
        "retrieval_channels": ["VECTOR_REWRITTEN_QUERY"],
        "source": {
            "source_type": "md",
            "collection": "kb",
            "display_name": "x.md",
            "document_version": "v1",
        },
        "selected": False,
    }


def artifact(
    retrieved: list[dict[str, object]] | None = None,
    ranked: list[dict[str, object]] | None = None,
) -> RagEvaluationArtifactV1:
    retrieved_items = [] if retrieved is None else retrieved
    ranked_items = retrieved_items if ranked is None else ranked
    return RagEvaluationArtifactV1.model_validate(
        {
            "schema_version": "rag-evaluation-artifact.v1",
            "artifact_id": f"rag-eval://{RUN_ID}/r1",
            "run_id": RUN_ID,
            "attempt_id": RUN_ID,
            "retrieval_id": "r1",
            "invocation_index": 1,
            "retrieval_status": "SUCCEEDED",
            "query": "q",
            "rewritten_query": "rw",
            "retrieved_items": retrieved_items,
            "ranked_items": ranked_items,
            "selected_items": [],
            "citations": [],
            "retrieval_latency_ms": 10,
            "rerank_latency_ms": 5,
            "total_latency_ms": 15,
            "degraded": False,
            "degradation_reasons": [],
            "budget_usage": {},
        }
    )


def test_ndcg_perfect_ranking_is_one() -> None:
    truth = gt(("d", "A", 3), ("d", "B", 2), ("d", "C", 1))
    ranked = [item("d", "A", 1), item("d", "B", 2), item("d", "C", 3)]
    result = calculate_ndcg_at_k(
        truth, artifact(retrieved=ranked, ranked=ranked), k_values=(1, 3, 5, 10)
    )
    assert result.metric_name == NDCG_METRIC_NAME
    assert result.values == (1.0, 1.0, 1.0, 1.0)
    assert result.as_dict() == {"1": 1.0, "3": 1.0, "5": 1.0, "10": 1.0}


def test_ndcg_reverse_ranking_between_zero_and_one() -> None:
    truth = gt(("d", "A", 3), ("d", "B", 2), ("d", "C", 1))
    ranked = [item("d", "C", 1), item("d", "B", 2), item("d", "A", 3)]
    expected_full = (1.0 + 3.0 / LOG2_3 + 7.0 / 2.0) / (7.0 + 3.0 / LOG2_3 + 1.0 / 2.0)
    result = calculate_ndcg_at_k(
        truth, artifact(retrieved=ranked, ranked=ranked), k_values=(1, 3, 10)
    )
    assert result.value_at(1) == pytest.approx(1.0 / 7.0)
    assert result.value_at(3) == pytest.approx(expected_full)
    assert result.value_at(10) == pytest.approx(expected_full)
    assert all(0.0 < value < 1.0 for value in result.values)


def test_ndcg_partial_hit_penalizes_misses_via_idcg() -> None:
    truth = gt(("d", "A", 3), ("d", "B", 2), ("d", "C", 1))
    ranked = [item("d", "X", 1), item("d", "Y", 2), item("d", "A", 3)]
    idcg_full = 7.0 + 3.0 / LOG2_3 + 1.0 / 2.0
    result = calculate_ndcg_at_k(
        truth, artifact(retrieved=ranked, ranked=ranked), k_values=(1, 2, 3, 5)
    )
    assert result.value_at(1) == 0.0
    assert result.value_at(2) == 0.0
    assert result.value_at(3) == pytest.approx(7.0 / LOG2_4 / idcg_full)
    assert result.value_at(5) == pytest.approx(7.0 / LOG2_4 / idcg_full)
    assert result.value_at(5) < 1.0


def test_ndcg_no_hit_is_zero() -> None:
    truth = gt(("d", "A", 3), ("d", "B", 2), ("d", "C", 1))
    ranked = [item("d", "X", 1), item("d", "Y", 2), item("d", "Z", 3)]
    result = calculate_ndcg_at_k(truth, artifact(retrieved=ranked, ranked=ranked))
    assert result.values == (0.0, 0.0, 0.0)


def test_ndcg_empty_ranked_items_is_zero() -> None:
    truth = gt(("d", "A", 3))
    result = calculate_ndcg_at_k(truth, artifact(retrieved=[], ranked=[]))
    assert result.values == (0.0, 0.0, 0.0)


def test_ndcg_empty_ranked_items_with_retrieved_present_still_zero() -> None:
    truth = gt(("d", "A", 3))
    art = artifact(retrieved=[item("d", "A", 1)], ranked=[])
    result = calculate_ndcg_at_k(truth, art)
    assert result.values == (0.0, 0.0, 0.0)


def test_ndcg_k_boundary() -> None:
    truth = gt(("d", "A", 3), ("d", "B", 2), ("d", "C", 1))
    ranked = [item("d", "B", 1), item("d", "A", 2), item("d", "C", 3)]
    result = calculate_ndcg_at_k(
        truth, artifact(retrieved=ranked, ranked=ranked), k_values=(1, 2, 5)
    )
    assert result.value_at(1) == pytest.approx(3.0 / 7.0)
    assert result.value_at(2) == pytest.approx((3.0 + 7.0 / LOG2_3) / (7.0 + 3.0 / LOG2_3))
    assert result.value_at(5) == pytest.approx(
        (3.0 + 7.0 / LOG2_3 + 0.5) / (7.0 + 3.0 / LOG2_3 + 0.5)
    )


def test_ndcg_k_exceeding_gt_count_uses_all_gt() -> None:
    truth = gt(("d", "A", 3), ("d", "B", 2))
    ranked = [item("d", "A", 1), item("d", "B", 2)]
    result = calculate_ndcg_at_k(truth, artifact(retrieved=ranked, ranked=ranked), k_values=(10,))
    assert result.value_at(10) == 1.0


def test_ndcg_zero_relevance_is_zero_not_nan() -> None:
    truth = gt(("d", "A", 0), ("d", "B", 0))
    ranked = [item("d", "A", 1), item("d", "B", 2)]
    result = calculate_ndcg_at_k(truth, artifact(retrieved=ranked, ranked=ranked), k_values=(1, 2))
    assert result.values == (0.0, 0.0)


def test_ndcg_uses_rank_field_not_list_order() -> None:
    truth = gt(("d", "A", 3))
    ranked = [item("d", "A", 2), item("d", "X", 1)]
    result = calculate_ndcg_at_k(
        truth, artifact(retrieved=ranked, ranked=ranked), k_values=(1, 2)
    )
    assert result.value_at(1) == 0.0
    assert result.value_at(2) == pytest.approx((7.0 / LOG2_3) / 7.0)


def test_ndcg_duplicate_ranked_identity_counts_once() -> None:
    truth = gt(("d", "A", 3))
    ranked = [item("d", "A", 1), item("d", "A", 2), item("d", "X", 3)]
    result = calculate_ndcg_at_k(
        truth, artifact(retrieved=ranked, ranked=ranked), k_values=(1, 2)
    )
    assert result.value_at(1) == 1.0
    assert result.value_at(2) == 1.0


def test_ndcg_document_id_present_exact_match() -> None:
    truth = gt(("d1", "c1", 3))
    hit = [item("d1", "c1", 1)]
    result = calculate_ndcg_at_k(truth, artifact(retrieved=hit, ranked=hit), k_values=(1,))
    assert result.value_at(1) == 1.0

    miss = [item("d2", "c1", 1)]
    result = calculate_ndcg_at_k(truth, artifact(retrieved=miss, ranked=miss), k_values=(1,))
    assert result.value_at(1) == 0.0


def test_ndcg_document_id_present_ignores_same_chunk_in_other_document() -> None:
    truth = gt(("d1", "c1", 3))
    ranked = [item("d2", "c1", 1), item("d1", "c1", 2)]
    result = calculate_ndcg_at_k(
        truth, artifact(retrieved=ranked, ranked=ranked), k_values=(1, 2)
    )
    assert result.value_at(1) == 0.0
    assert result.value_at(2) == pytest.approx((7.0 / LOG2_3) / 7.0)


def test_ndcg_document_id_none_unique_chunk_matches() -> None:
    truth = gt((None, "c1", 3))
    ranked = [item("d1", "c1", 1)]
    result = calculate_ndcg_at_k(truth, artifact(retrieved=ranked, ranked=ranked), k_values=(1,))
    assert result.value_at(1) == 1.0


def test_ndcg_document_id_none_ambiguous_chunk_fails_closed() -> None:
    truth = gt((None, "c1", 3))
    ranked = [item("d1", "c1", 1), item("d2", "c1", 2)]
    with pytest.raises(ValueError, match="ambiguous chunk identity"):
        calculate_ndcg_at_k(truth, artifact(retrieved=ranked, ranked=ranked))


def test_ndcg_invalid_k_rejected() -> None:
    truth = gt(("d", "A", 3))
    art = artifact(retrieved=[item("d", "A", 1)], ranked=[item("d", "A", 1)])
    with pytest.raises(ValueError, match="positive integer"):
        calculate_ndcg_at_k(truth, art, k_values=(0,))
    with pytest.raises(ValueError, match="positive integer"):
        calculate_ndcg_at_k(truth, art, k_values=(-5,))
    with pytest.raises(ValueError, match="positive integer"):
        calculate_ndcg_at_k(truth, art, k_values=(True,))
    with pytest.raises(ValueError, match="duplicate k"):
        calculate_ndcg_at_k(truth, art, k_values=(1, 1))
    with pytest.raises(ValueError, match="must not be empty"):
        calculate_ndcg_at_k(truth, art, k_values=())


def test_ndcg_empty_ground_truth_rejected_without_nan() -> None:
    empty_truth = RankingGroundTruth.model_construct(graded_relevance=[])
    art = artifact(retrieved=[item("d", "A", 1)], ranked=[item("d", "A", 1)])
    with pytest.raises(ValueError, match="must not be empty"):
        calculate_ndcg_at_k(empty_truth, art)


def test_ndcg_overlapping_gt_identities_fail_closed() -> None:
    truth = gt((None, "chunk1", 3), ("docA", "chunk1", 2))
    ranked = [item("docA", "chunk1", 1)]
    with pytest.raises(ValueError, match="overlapping ground truth identities"):
        calculate_ndcg_at_k(truth, artifact(retrieved=ranked, ranked=ranked))


def test_ndcg_overlapping_gt_identities_fail_closed_reversed_order() -> None:
    truth = gt(("docA", "chunk1", 2), (None, "chunk1", 3))
    ranked = [item("docA", "chunk1", 1)]
    with pytest.raises(ValueError, match="overlapping ground truth identities"):
        calculate_ndcg_at_k(truth, artifact(retrieved=ranked, ranked=ranked))


def test_ndcg_overlapping_gt_without_artifact_overlap_computes_normally() -> None:
    truth = gt((None, "chunk1", 3), ("docA", "chunk1", 2))
    ranked = [item("docB", "chunk1", 1), item("docB", "x1", 2)]
    result = calculate_ndcg_at_k(
        truth, artifact(retrieved=ranked, ranked=ranked), k_values=(1, 2)
    )
    assert result.value_at(1) == 1.0
    assert result.value_at(2) == pytest.approx(7.0 / (7.0 + 3.0 / LOG2_3))


def test_ndcg_overlapping_gt_with_chunk_absent_from_artifact_computes_normally() -> None:
    truth = gt((None, "chunk1", 3), ("docA", "chunk1", 2))
    ranked = [item("docA", "chunk9", 1)]
    result = calculate_ndcg_at_k(
        truth, artifact(retrieved=ranked, ranked=ranked), k_values=(1,)
    )
    assert result.value_at(1) == 0.0


def _integration_case_payload() -> dict[str, object]:
    return {
        "case_id": "case-001",
        "name": "CDT 字段映射排序",
        "input": {"query": "解释CDT字段映射"},
        "expected_output": "应按相关性排序引用 CDT 字段映射文档。",
        "ground_truth": {
            "ranking": {
                "graded_relevance": [
                    {"document_id": "source-stable", "chunk_id": "c0", "relevance": 3},
                    {"chunk_id": "c1", "relevance": 1},
                ]
            },
            "generation": {"reference_answer": "CDT 字段映射的参考解释。"},
        },
        "metadata": {"topic": "cdt"},
    }


def test_case_ranking_ground_truth_plus_artifact_to_ndcg() -> None:
    case = validate_case(_integration_case_payload())
    assert case.ground_truth.ranking is not None
    ranked = [item("source-stable", "c0", 1), item("other", "z9", 2)]
    result = calculate_ndcg_at_k(
        case.ground_truth.ranking, artifact(retrieved=ranked, ranked=ranked), k_values=(1, 2, 5)
    )
    assert result.metric_name == "ndcg_at_k"
    assert result.value_at(1) == 1.0
    assert result.value_at(2) == pytest.approx(7.0 / (7.0 + 1.0 / LOG2_3))
    assert result.value_at(5) == pytest.approx(7.0 / (7.0 + 1.0 / LOG2_3))


def test_case_without_ranking_ground_truth_cannot_compute() -> None:
    payload = _integration_case_payload()
    payload["ground_truth"] = {"generation": {"reference_answer": "只有参考答案。"}}
    case = validate_case(payload)
    assert case.ground_truth.ranking is None
