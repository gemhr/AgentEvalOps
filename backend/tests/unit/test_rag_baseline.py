"""Stage5 Phase3 RAG Dataset、Suite 与 metric adapter tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.adapters.evaluation.rag_metrics import RagMetricEvaluatorResolver
from app.core.evaluation.dataset import load_dataset
from app.core.evaluation.dataset_bridge import bridge_dataset_to_catalog
from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.references import ArtifactRef, CaseVersionRef, EvidenceRef
from app.services.evaluation.rag_baseline import build_rag_baseline_suite
from tests.unit.test_rag_artifact import artifact_payload

DATASET = (
    Path(__file__).resolve().parents[2]
    / "evaluation_assets"
    / "rag_quality_v1"
    / "rag_evaluation_dataset.v1.json"
)


def test_dataset_v1_identity_case_mix_and_no_answer_semantics() -> None:
    dataset = load_dataset(DATASET)

    assert dataset.dataset_id == "rag-evaluation-dataset"
    assert dataset.version == "v1"
    assert len(dataset) == 24
    assert len({case.case_id for case in dataset.cases}) == 24
    retrieval = [case for case in dataset.cases if case.ground_truth.retrieval is not None]
    no_answer = [case for case in dataset.cases if case.metadata.get("case_type") == "NO_ANSWER"]
    assert len(retrieval) == 20
    assert len(no_answer) == 4
    assert all(case.ground_truth.retrieval is None for case in no_answer)
    assert all(case.ground_truth.ranking is None for case in no_answer)
    assert all(case.ground_truth.generation is not None for case in no_answer)


def test_bridge_preserves_versioned_rag_ground_truth_and_suite() -> None:
    dataset = load_dataset(DATASET)
    catalog, cases = bridge_dataset_to_catalog(
        dataset, created_at=datetime(2026, 8, 23, tzinfo=timezone.utc)
    )
    suite = build_rag_baseline_suite(dataset)

    assert catalog.case_version_refs == suite.case_selection
    assert all(ref.version == "v1" for ref in suite.case_selection)
    assert [spec.evaluator_id for spec in suite.evaluator_specs] == [
        "recall_at_1",
        "recall_at_3",
        "recall_at_5",
        "mrr",
        "ndcg_at_3",
        "ndcg_at_5",
    ]
    case = cases[CaseVersionRef("exact-http-evaluation-v2", "v1")]
    assert case.metadata["rag_ground_truth"]["retrieval"]["relevant_chunks"]


@pytest.mark.asyncio
async def test_real_artifact_shape_flows_through_recall_mrr_and_ndcg_adapters() -> None:
    dataset = load_dataset(DATASET)
    _catalog, cases = bridge_dataset_to_catalog(
        dataset, created_at=datetime(2026, 8, 23, tzinfo=timezone.utc)
    )
    ref = CaseVersionRef("exact-http-evaluation-v2", "v1")
    case = cases[ref]
    relevant = case.metadata["rag_ground_truth"]["retrieval"]["relevant_chunks"][0]
    payload = artifact_payload()
    payload["retrieved_items"][0]["document_id"] = relevant["document_id"]
    payload["retrieved_items"][0]["chunk_id"] = relevant["chunk_id"]
    payload["ranked_items"][0]["document_id"] = relevant["document_id"]
    payload["ranked_items"][0]["chunk_id"] = relevant["chunk_id"]
    payload["selected_items"][0]["document_id"] = relevant["document_id"]
    payload["selected_items"][0]["chunk_id"] = relevant["chunk_id"]
    payload["citations"][0]["document_id"] = relevant["document_id"]
    payload["citations"][0]["chunk_id"] = relevant["chunk_id"]
    evidence = EvidenceRef(
        kind="rag_evaluation_artifact",
        identifier=payload["artifact_id"],
        metadata={"payload": payload},
    )
    value = EvaluationInput(
        case_ref=ref,
        input_payload=case.input_payload,
        expected_output=None,
        assertion_specs=(),
        actual_artifact=ArtifactRef("output", "localagent-run://test"),
        evidence_refs=(evidence,),
        metadata={"case": case.metadata},
    )
    suite = build_rag_baseline_suite(dataset)
    resolver = RagMetricEvaluatorResolver()
    scores = {}
    for spec in suite.evaluator_specs:
        draft = await resolver.resolve(spec).evaluator.evaluate(value, EvaluatorContext(spec))
        scores[spec.evaluator_id] = draft.score

    assert scores == {
        "recall_at_1": 1.0,
        "recall_at_3": 1.0,
        "recall_at_5": 1.0,
        "mrr": 1.0,
        "ndcg_at_3": 1.0,
        "ndcg_at_5": 1.0,
    }
