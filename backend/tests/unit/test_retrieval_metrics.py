"""Retrieval quality metrics - Recall@K and MRR pure-function focused tests."""

from __future__ import annotations

import pytest

from app.core.evaluation.dataset import RetrievalGroundTruth, validate_case
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1
from app.core.evaluation.retrieval_metrics import (
    MRR_SOURCE_EMPTY,
    MRR_SOURCE_RANKED,
    MRR_SOURCE_RETRIEVED_FALLBACK,
    calculate_mrr,
    calculate_recall_at_k,
)

RUN_ID = "11111111-1111-4111-8111-111111111111"


def gt(*chunks: tuple[str, str]) -> RetrievalGroundTruth:
    return RetrievalGroundTruth.model_validate(
        {"relevant_chunks": [{"document_id": doc, "chunk_id": chunk} for doc, chunk in chunks]}
    )


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


def test_recall_full_hit() -> None:
    truth = gt(("d", "A"), ("d", "B"))
    result = calculate_recall_at_k(
        truth, artifact(retrieved=[item("d", "A", 1), item("d", "B", 2)]), k_values=(1, 2)
    )
    assert result.values == (0.5, 1.0)
    assert result.relevant_total == 2
    assert result.as_dict() == {"1": 0.5, "2": 1.0}


def test_recall_partial_hit() -> None:
    truth = gt(("d", "A"), ("d", "B"), ("d", "C"))
    retrieved = [item("d", "A", 1)] + [item("d", x, i + 2) for i, x in enumerate("DEFG")]
    result = calculate_recall_at_k(truth, artifact(retrieved=retrieved))
    assert result.value_at(1) == pytest.approx(1 / 3)
    assert result.value_at(5) == pytest.approx(1 / 3)
    assert result.value_at(10) == pytest.approx(1 / 3)


def test_recall_no_hit() -> None:
    truth = gt(("d", "A"))
    retrieved = [item("d", x, i + 1) for i, x in enumerate("XYZ")]
    result = calculate_recall_at_k(truth, artifact(retrieved=retrieved))
    assert result.values == (0.0, 0.0, 0.0)


def test_recall_empty_retrieval_is_zero() -> None:
    truth = gt(("d", "A"))
    result = calculate_recall_at_k(truth, artifact(retrieved=[]))
    assert result.values == (0.0, 0.0, 0.0)


def test_recall_k_boundary() -> None:
    truth = gt(("d", "A"))
    retrieved = [item("d", "B", 1), item("d", "A", 2)]
    result = calculate_recall_at_k(truth, artifact(retrieved=retrieved), k_values=(1, 2, 3))
    assert result.value_at(1) == 0.0
    assert result.value_at(2) == 1.0
    assert result.value_at(3) == 1.0


def test_recall_k_exceeding_item_count_computes_normally() -> None:
    truth = gt(("d", "A"), ("d", "B"), ("d", "C"))
    retrieved = [item("d", "A", 1), item("d", "B", 2), item("d", "C", 3)]
    result = calculate_recall_at_k(truth, artifact(retrieved=retrieved), k_values=(10,))
    assert result.value_at(10) == 1.0


def test_recall_duplicate_item_counted_once_but_occupies_slot() -> None:
    truth = gt(("d", "A"), ("d", "B"))
    retrieved = [item("d", "A", 1), item("d", "A", 2), item("d", "B", 3)]
    result = calculate_recall_at_k(truth, artifact(retrieved=retrieved), k_values=(2, 3))
    assert result.value_at(2) == pytest.approx(1 / 2)
    assert result.value_at(3) == 1.0


def test_recall_uses_retrieval_rank_ordering() -> None:
    truth = gt(("d", "A"))
    retrieved = [item("d", "A", 2, retrieval_rank=5), item("d", "Z", 1, retrieval_rank=1)]
    result = calculate_recall_at_k(truth, artifact(retrieved=retrieved), k_values=(1, 5))
    assert result.value_at(1) == 0.0
    assert result.value_at(5) == 1.0


def test_recall_invalid_k_rejected() -> None:
    truth = gt(("d", "A"))
    art = artifact(retrieved=[item("d", "A", 1)])
    with pytest.raises(ValueError, match="positive integer"):
        calculate_recall_at_k(truth, art, k_values=(0,))
    with pytest.raises(ValueError, match="positive integer"):
        calculate_recall_at_k(truth, art, k_values=(-5,))
    with pytest.raises(ValueError, match="positive integer"):
        calculate_recall_at_k(truth, art, k_values=(True,))
    with pytest.raises(ValueError, match="duplicate k"):
        calculate_recall_at_k(truth, art, k_values=(1, 1))
    with pytest.raises(ValueError, match="must not be empty"):
        calculate_recall_at_k(truth, art, k_values=())


def test_recall_empty_ground_truth_rejected_without_nan() -> None:
    empty_truth = RetrievalGroundTruth.model_construct(relevant_chunks=[])
    with pytest.raises(ValueError, match="must not be empty"):
        calculate_recall_at_k(empty_truth, artifact(retrieved=[item("d", "A", 1)]))
    with pytest.raises(ValueError, match="must not be empty"):
        calculate_mrr(empty_truth, artifact(retrieved=[item("d", "A", 1)]))


def test_mrr_rank_one() -> None:
    truth = gt(("d", "A"))
    ranked = [item("d", "A", 1), item("d", "X", 2)]
    result = calculate_mrr(truth, artifact(retrieved=ranked))
    assert result.value == 1.0
    assert result.first_relevant_rank == 1
    assert result.source == MRR_SOURCE_RANKED


def test_mrr_rank_middle() -> None:
    truth = gt(("d", "A"))
    ranked = [item("d", "X", 1), item("d", "Y", 2), item("d", "A", 3)]
    result = calculate_mrr(truth, artifact(retrieved=ranked))
    assert result.value == pytest.approx(1 / 3)
    assert result.first_relevant_rank == 3


def test_mrr_no_hit_is_zero() -> None:
    truth = gt(("d", "A"))
    ranked = [item("d", "X", 1), item("d", "Y", 2)]
    result = calculate_mrr(truth, artifact(retrieved=ranked))
    assert result.value == 0.0
    assert result.first_relevant_rank is None


def test_mrr_uses_rank_field_not_list_order() -> None:
    truth = gt(("d", "A"))
    ranked = [item("d", "X", 2), item("d", "A", 1)]
    result = calculate_mrr(truth, artifact(retrieved=ranked))
    assert result.value == 1.0
    assert result.first_relevant_rank == 1


def test_mrr_duplicate_identity_uses_min_rank() -> None:
    truth = gt(("d", "A"))
    ranked = [item("d", "A", 1), item("d", "A", 2), item("d", "B", 3)]
    result = calculate_mrr(truth, artifact(retrieved=ranked))
    assert result.value == 1.0
    assert result.first_relevant_rank == 1


def test_mrr_multiple_relevant_uses_first() -> None:
    truth = gt(("d", "A"), ("d", "B"))
    ranked = [item("d", "X", 1), item("d", "B", 2), item("d", "A", 4)]
    result = calculate_mrr(truth, artifact(retrieved=ranked))
    assert result.value == pytest.approx(1 / 2)
    assert result.first_relevant_rank == 2


def test_mrr_empty_ranked_falls_back_to_retrieved_items() -> None:
    truth = gt(("d", "A"))
    retrieved = [item("d", "X", 2, retrieval_rank=2), item("d", "A", 1, retrieval_rank=1)]
    result = calculate_mrr(truth, artifact(retrieved=retrieved, ranked=[]))
    assert result.value == 1.0
    assert result.first_relevant_rank == 1
    assert result.source == MRR_SOURCE_RETRIEVED_FALLBACK


def test_mrr_empty_retrieval_is_zero() -> None:
    truth = gt(("d", "A"))
    result = calculate_mrr(truth, artifact(retrieved=[], ranked=[]))
    assert result.value == 0.0
    assert result.first_relevant_rank is None
    assert result.source == MRR_SOURCE_EMPTY


def _integration_case_payload() -> dict[str, object]:
    return {
        "case_id": "case-001",
        "name": "CDT 字段映射解释",
        "input": {"query": "解释CDT字段映射"},
        "expected_output": "应说明 CDT 字段映射规则。",
        "ground_truth": {
            "retrieval": {
                "relevant_chunks": [
                    {"document_id": "source-stable", "chunk_id": "c0"},
                    {"document_id": "source-stable", "chunk_id": "c1"},
                ]
            },
            "generation": {"reference_answer": "CDT 字段映射的参考解释。"},
        },
        "metadata": {"topic": "cdt"},
    }


def test_case_plus_artifact_to_metric_result() -> None:
    case = validate_case(_integration_case_payload())
    assert case.ground_truth.retrieval is not None
    art = artifact(
        retrieved=[item("source-stable", "c0", 1), item("other", "z9", 2)],
        ranked=[item("source-stable", "c0", 1), item("other", "z9", 2)],
    )
    recall = calculate_recall_at_k(case.ground_truth.retrieval, art)
    mrr = calculate_mrr(case.ground_truth.retrieval, art)
    assert recall.metric_name == "recall_at_k"
    assert recall.value_at(1) == pytest.approx(1 / 2)
    assert recall.value_at(10) == pytest.approx(1 / 2)
    assert mrr.value == 1.0
    assert mrr.first_relevant_rank == 1


def test_case_without_retrieval_ground_truth_cannot_compute() -> None:
    payload = _integration_case_payload()
    payload["ground_truth"] = {"generation": {"reference_answer": "只有参考答案。"}}
    case = validate_case(payload)
    assert case.ground_truth.retrieval is None


def test_case_plus_empty_retrieval_artifact_is_zero() -> None:
    case = validate_case(_integration_case_payload())
    assert case.ground_truth.retrieval is not None
    art = artifact(retrieved=[], ranked=[])
    assert calculate_recall_at_k(case.ground_truth.retrieval, art).values == (0.0, 0.0, 0.0)
    assert calculate_mrr(case.ground_truth.retrieval, art).value == 0.0
