"""RAG Artifact 到现有 metric 纯函数的最薄 Evaluator adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.core.evaluation.catalog import EvaluatorSpec
from app.core.evaluation.dataset import RankingGroundTruth, RetrievalGroundTruth
from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.rag_artifact import RAG_ARTIFACT_EVIDENCE_KIND, RagEvaluationArtifactV1
from app.core.evaluation.ranking_metrics import calculate_ndcg_at_k
from app.core.evaluation.results import EvaluationResultDraft, EvaluationVerdict
from app.core.evaluation.retrieval_metrics import calculate_mrr, calculate_recall_at_k
from app.services.evaluation.loop import ResolvedEvaluator

EVALUATOR_VERSION = "rag-quality-metrics.v1"
RECALL_IDS = {1: "recall_at_1", 3: "recall_at_3", 5: "recall_at_5"}
NDCG_IDS = {3: "ndcg_at_3", 5: "ndcg_at_5"}
MRR_ID = "mrr"


def _rag_artifact(evaluation_input: EvaluationInput):
    refs = tuple(
        ref for ref in evaluation_input.evidence_refs if ref.kind == RAG_ARTIFACT_EVIDENCE_KIND
    )
    if len(refs) != 1:
        return None, ()
    return RagEvaluationArtifactV1.model_validate(refs[0].metadata["payload"]), refs


def _ground_truth(evaluation_input: EvaluationInput) -> Mapping[str, object] | None:
    case = evaluation_input.metadata.get("case")
    if not isinstance(case, Mapping):
        return None
    value = case.get("rag_ground_truth")
    return value if isinstance(value, Mapping) else None


@dataclass(frozen=True, slots=True)
class RagMetricEvaluator:
    """计算单个已版本化 RAG metric slot."""

    evaluator_id: str
    k: int | None = None

    async def evaluate(
        self, evaluation_input: EvaluationInput, context: EvaluatorContext
    ) -> EvaluationResultDraft:
        """从 Ground Truth 与唯一 RAG Artifact 计算 metric draft."""
        spec = context.evaluator_spec
        artifact, refs = _rag_artifact(evaluation_input)
        truth = _ground_truth(evaluation_input)
        if artifact is None or truth is None:
            return self._unavailable(spec, "rag_metric_input_unavailable")
        if self.evaluator_id in RECALL_IDS.values() or self.evaluator_id == MRR_ID:
            raw = truth.get("retrieval")
            if not isinstance(raw, Mapping):
                return self._unavailable(spec, "retrieval_ground_truth_unavailable")
            retrieval = RetrievalGroundTruth.model_validate(raw)
            if self.evaluator_id == MRR_ID:
                result = calculate_mrr(retrieval, artifact)
                score = result.value
                detail = {
                    "first_relevant_rank": result.first_relevant_rank,
                    "mrr_source": result.source,
                }
            else:
                if self.k is None:
                    raise ValueError("recall evaluator requires k")
                result = calculate_recall_at_k(retrieval, artifact, k_values=(self.k,))
                score = result.value_at(self.k)
                detail = {"k": self.k, "relevant_total": result.relevant_total}
        else:
            raw = truth.get("ranking")
            if not isinstance(raw, Mapping):
                return self._unavailable(spec, "ranking_ground_truth_unavailable")
            if self.k is None:
                raise ValueError("ndcg evaluator requires k")
            ranking = RankingGroundTruth.model_validate(raw)
            result = calculate_ndcg_at_k(ranking, artifact, k_values=(self.k,))
            score = result.value_at(self.k)
            detail = {"k": self.k, "relevance_kind": "graded"}
        return EvaluationResultDraft(
            evaluator_id=spec.evaluator_id,
            evaluator_version=spec.evaluator_version,
            config_ref=spec.config_ref,
            prompt_ref=spec.prompt_ref,
            verdict=EvaluationVerdict.PASS,
            reason="rag_metric_computed",
            score=score,
            evidence_refs=refs,
            metadata={
                "artifact_id": artifact.artifact_id,
                "retrieval_status": artifact.retrieval_status,
                **detail,
            },
        )

    @staticmethod
    def _unavailable(spec: EvaluatorSpec, reason: str) -> EvaluationResultDraft:
        return EvaluationResultDraft(
            evaluator_id=spec.evaluator_id,
            evaluator_version=spec.evaluator_version,
            config_ref=spec.config_ref,
            prompt_ref=spec.prompt_ref,
            verdict=EvaluationVerdict.INCONCLUSIVE,
            reason=reason,
            metadata={"source_status": "INPUT_UNAVAILABLE"},
        )


class RagMetricEvaluatorResolver:
    """解析冻结的 RAG metric evaluator identity."""

    def resolve(self, spec: EvaluatorSpec) -> ResolvedEvaluator:
        """把 suite spec 解析为对应的 deterministic evaluator."""
        reverse_recall = {value: key for key, value in RECALL_IDS.items()}
        reverse_ndcg = {value: key for key, value in NDCG_IDS.items()}
        if spec.evaluator_version != EVALUATOR_VERSION:
            raise ValueError(f"unsupported RAG evaluator version: {spec.evaluator_version}")
        if spec.evaluator_id == MRR_ID:
            evaluator = RagMetricEvaluator(MRR_ID)
        elif spec.evaluator_id in reverse_recall:
            evaluator = RagMetricEvaluator(spec.evaluator_id, reverse_recall[spec.evaluator_id])
        elif spec.evaluator_id in reverse_ndcg:
            evaluator = RagMetricEvaluator(spec.evaluator_id, reverse_ndcg[spec.evaluator_id])
        else:
            raise ValueError(f"unsupported RAG evaluator: {spec.evaluator_id}")
        return ResolvedEvaluator(spec.evaluator_id, spec.evaluator_version, evaluator)


__all__ = [
    "EVALUATOR_VERSION",
    "MRR_ID",
    "NDCG_IDS",
    "RECALL_IDS",
    "RagMetricEvaluator",
    "RagMetricEvaluatorResolver",
]
