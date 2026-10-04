"""离线生成 G4 固定 RAG golden；Go tests 不执行 Python。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.core.evaluation.dataset import RankingGroundTruth, RetrievalGroundTruth
from app.core.evaluation.rag_artifact import RagEvaluationArtifactV1
from app.core.evaluation.ranking_metrics import calculate_ndcg_at_k
from app.core.evaluation.retrieval_metrics import calculate_mrr, calculate_recall_at_k


def item(doc, chunk, rank, retrieval_rank=None):
    return dict(document_id=doc, chunk_id=chunk, rank=rank,
                retrieval_rank=retrieval_rank or rank, retrieval_score=0.5,
                retrieval_score_kind="VECTOR_NORMALIZED_RELEVANCE",
                retrieval_channels=["VECTOR_ORIGINAL_QUERY"],
                source=dict(source_type="test", collection="test", display_name="test", document_version="v1"),
                selected=False)


def artifact(items, ranked=None, strategy=None):
    return dict(schema_version="rag-evaluation-artifact.v2", artifact_id="rag-eval://remote/r1",
                run_id="remote", attempt_id="remote", retrieval_id="r1", invocation_index=1,
                retrieval_status="SUCCEEDED", query="query", rewritten_query="query",
                retrieved_items=items, ranked_items=items if ranked is None else ranked,
                selected_items=[], citations=[], total_latency_ms=1, degraded=False,
                degradation_reasons=[], budget_usage={}, retrieval_strategy=strategy)


vectors = []
gt = dict(relevant_chunks=[dict(document_id="d", chunk_id="a"), dict(document_id="d", chunk_id="b")],
          graded_relevance=[dict(document_id="d", chunk_id="a", relevance=3), dict(document_id="d", chunk_id="b", relevance=1)])
cases = [
    ("rank_order", artifact([item("d", "b", 2), item("d", "a", 1)]), gt, 2),
    ("duplicate_occupancy", artifact([item("d", "a", 1), item("d", "a", 2), item("d", "b", 3)]), gt, 2),
    ("graded_relevance", artifact([item("d", "b", 1), item("d", "a", 2)]), gt, 2),
    ("namespace_mismatch", artifact([item("other", "a", 1)]), gt, 2),
    ("rank_fallback", artifact([item("d", "a", 4)], ranked=[]), gt, 5),
    ("hybrid_fused_rank", artifact([item("d", "b", 1, 1), item("d", "a", 2, 2)],
                                    ranked=[item("d", "a", 1), item("d", "b", 2)], strategy="HYBRID_RRF"), gt, 1),
    ("zero_gain", artifact([item("d", "a", 1)]), dict(relevant_chunks=[dict(document_id="d", chunk_id="a")],
      graded_relevance=[dict(document_id="d", chunk_id="a", relevance=0)]), 5),
    ("empty_artifact", artifact([]), gt, 5),
]
for name, raw, truth, k in cases:
    rag = RagEvaluationArtifactV1.model_validate(raw)
    retrieval = RetrievalGroundTruth.model_validate({"relevant_chunks": truth["relevant_chunks"]})
    ranking = RankingGroundTruth.model_validate({"graded_relevance": truth["graded_relevance"]})
    expected = {"recall@k.v1": calculate_recall_at_k(retrieval, rag, [k]).values[0],
                "mrr.v1": calculate_mrr(retrieval, rag).value,
                "ndcg.v1": calculate_ndcg_at_k(ranking, rag, [k]).values[0]}
    vectors.append(dict(name=name, artifact=raw, ground_truth=truth, k=k, expected=expected, state="APPLICABLE"))
for name, truth, state in [("missing_gt", None, "MISSING_EVIDENCE"),
                            ("declared_empty_gt", dict(relevant_chunks=[], graded_relevance=[]), "NOT_APPLICABLE")]:
    vectors.append(dict(name=name, artifact=artifact([]), ground_truth=truth, k=5,
                        expected={"recall@k.v1": None, "mrr.v1": None, "ndcg.v1": None}, state=state))
target = Path(__file__).with_name("rag_golden.json")
target.write_text(json.dumps(dict(oracle="Python retrieval_metrics/ranking_metrics at 94101995d3b5a4330626e7a6681d03d9d60aabde",
                                 breaks="declared empty GT => NOT_APPLICABLE; absent GT => MISSING_EVIDENCE (Python rejects both)",
                                 vectors=vectors), ensure_ascii=False, indent=2), encoding="utf-8")
print(f"wrote {len(vectors)} fixed vectors: {target.name}")
