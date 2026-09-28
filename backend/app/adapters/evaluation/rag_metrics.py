# ruff: noqa: D415
"""RAG Artifact 到现有 metric 纯函数的最薄 Evaluator adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.core.evaluation.catalog import EvaluatorSpec
from app.core.evaluation.dataset import (
    DocumentRetrievalGroundTruth,
    GradedRelevance,
    GroundTruthChunk,
    RankingGroundTruth,
    RetrievalGroundTruth,
)
from app.core.evaluation.document_metrics import (
    DocumentProjection,
    UnknownBenchmarkDocumentError,
    project_artifact_to_documents,
    project_document_sequence,
)
from app.core.evaluation.evaluators import EvaluationInput, EvaluatorContext
from app.core.evaluation.rag_artifact import RAG_ARTIFACT_EVIDENCE_KIND, RagEvaluationArtifactV1
from app.core.evaluation.process_trajectory import PROCESS_TRAJECTORY_KIND, ProcessKind
from app.core.evaluation.ranking_metrics import calculate_ndcg_at_k
from app.core.evaluation.references import VersionRef
from app.core.evaluation.results import EvaluationResultDraft, EvaluationVerdict
from app.core.evaluation.retrieval_metrics import calculate_mrr, calculate_recall_at_k
from app.services.evaluation.loop import ResolvedEvaluator

EVALUATOR_VERSION = "rag-quality-metrics.v1"
DOCUMENT_EVALUATOR_VERSION = "rag-document-metrics.v1"
RECALL_IDS = {1: "recall_at_1", 3: "recall_at_3", 5: "recall_at_5"}
NDCG_IDS = {3: "ndcg_at_3", 5: "ndcg_at_5"}
MRR_ID = "mrr"
DOCUMENT_RECALL_IDS = {1: "document_recall_at_1", 3: "document_recall_at_3", 5: "document_recall_at_5"}
DOCUMENT_NDCG_IDS = {3: "document_ndcg_at_3", 5: "document_ndcg_at_5"}
DOCUMENT_MRR_ID = "document_mrr"


@dataclass(frozen=True, slots=True)
class _MetricItemView:
    document_id: str
    chunk_id: str
    rank: int | None
    retrieval_rank: int | None


@dataclass(frozen=True, slots=True)
class _MetricRetrievalView:
    artifact_id: str
    retrieval_id: str
    retrieval_status: str | None
    retrieval_strategy: str | None
    retrieved_items: tuple[_MetricItemView, ...]
    ranked_items: tuple[_MetricItemView, ...]


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


def _process_retrieval_input(evaluation_input: EvaluationInput):
    """把经 Loop shared validator 验证的单一 Retrieval record 映射到旧 metric 输入形状。"""
    trajectory = evaluation_input.process_trajectory
    if trajectory is None or evaluation_input.process_evidence_error is not None:
        return None
    records = [record for record in trajectory.records if record.kind is ProcessKind.RETRIEVAL]
    if len(records) != 1:
        raise ValueError("formal RAG metrics require exactly one retrieval invocation")
    payload = records[0].payload

    def metric_items(items, rank_field: str):
        if items is None:
            raise ValueError("formal RAG retrieval ranking field is unavailable")
        mapped = []
        for item in items:
            rank = getattr(item, rank_field)
            if rank is None or rank < 1:
                raise ValueError("formal RAG retrieval rank is unavailable")
            mapped.append(_MetricItemView(
                document_id=item.document_id, chunk_id=item.chunk_id,
                rank=item.rank, retrieval_rank=item.retrieval_rank,
            ))
        return tuple(mapped)

    return _MetricRetrievalView(
        artifact_id=records[0].source_event_id,
        retrieval_id=payload.retrieval_id,
        retrieval_status=payload.status,
        retrieval_strategy=payload.retrieval_strategy,
        retrieved_items=metric_items(payload.retrieved_items, "retrieval_rank"),
        ranked_items=metric_items(payload.ranked_items, "rank"),
    )


def _project_process_to_documents(artifact: _MetricRetrievalView, projection: DocumentProjection) -> _MetricRetrievalView:
    """按原有 document rank 投影规则映射安全 Retrieval 视图。"""
    retrieved = project_document_sequence(
        [(item.retrieval_rank, item.document_id) for item in artifact.retrieved_items], projection
    )
    ranked = project_document_sequence(
        [(item.rank, item.document_id) for item in artifact.ranked_items], projection
    )
    retrieved_positions = dict(retrieved)
    return _MetricRetrievalView(
        artifact_id=artifact.artifact_id,
        retrieval_id=artifact.retrieval_id,
        retrieval_status=artifact.retrieval_status,
        retrieval_strategy=artifact.retrieval_strategy,
        retrieved_items=tuple(_MetricItemView(doc, doc, rank, rank) for doc, rank in retrieved),
        ranked_items=tuple(
            _MetricItemView(doc, doc, rank, retrieved_positions.get(doc, rank)) for doc, rank in ranked
        ),
    )


def _is_process_spec(spec: EvaluatorSpec) -> bool:
    return PROCESS_TRAJECTORY_KIND in spec.required_evidence_kinds


@dataclass(frozen=True, slots=True)
class RagMetricEvaluator:
    """计算单个已版本化 RAG metric slot."""

    evaluator_id: str
    k: int | None = None
    document_projection: DocumentProjection | None = None

    async def evaluate(
        self, evaluation_input: EvaluationInput, context: EvaluatorContext
    ) -> EvaluationResultDraft:
        """从 Ground Truth 与唯一 RAG Artifact 计算 metric draft."""
        spec = context.evaluator_spec
        formal_process = _is_process_spec(spec)
        if formal_process:
            artifact = _process_retrieval_input(evaluation_input)
            refs = ()
        else:
            artifact, refs = _rag_artifact(evaluation_input)
        truth = _ground_truth(evaluation_input)
        if artifact is None or truth is None:
            return self._unavailable(spec, "rag_metric_input_unavailable")
        if self.evaluator_id in _document_evaluator_ids():
            return self._evaluate_document(spec, artifact, truth, refs)
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

    def _evaluate_document(
        self,
        spec: EvaluatorSpec,
        artifact: RagEvaluationArtifactV1,
        truth: Mapping[str, object],
        refs: tuple,
    ) -> EvaluationResultDraft:
        """Document-level metric：先投影 chunk ranking，再复用现有纯函数。"""
        raw = truth.get("document_retrieval")
        if not isinstance(raw, Mapping):
            return self._unavailable(spec, "document_ground_truth_unavailable")
        if self.document_projection is None:
            return self._unavailable(spec, "document_projection_unavailable")
        document_truth = DocumentRetrievalGroundTruth.model_validate(raw)
        relevant = [
            GroundTruthChunk(document_id=item.document_id, chunk_id=item.document_id)
            for item in document_truth.relevant_documents
            if item.relevance > 0
        ]
        if not relevant:
            return self._unavailable(spec, "document_ground_truth_unavailable")
        try:
            projected = (
                _project_process_to_documents(artifact, self.document_projection)
                if isinstance(artifact, _MetricRetrievalView)
                else project_artifact_to_documents(artifact, self.document_projection)
            )
        except UnknownBenchmarkDocumentError as error:
            draft = self._unavailable(spec, "document_projection_unavailable")
            return EvaluationResultDraft(
                evaluator_id=draft.evaluator_id,
                evaluator_version=draft.evaluator_version,
                config_ref=draft.config_ref,
                prompt_ref=draft.prompt_ref,
                verdict=draft.verdict,
                reason=draft.reason,
                metadata={"source_status": "PROJECTION_FAILED", "error": str(error)},
            )
        retrieval = RetrievalGroundTruth(relevant_chunks=relevant)
        if self.evaluator_id == DOCUMENT_MRR_ID:
            result = calculate_mrr(retrieval, projected)
            detail = {
                "first_relevant_rank": result.first_relevant_rank,
                "mrr_source": result.source,
                "relevance_kind": _relevance_kind(document_truth),
            }
            score = result.value
        elif self.evaluator_id in DOCUMENT_RECALL_IDS.values():
            if self.k is None:
                raise ValueError("document recall evaluator requires k")
            result = calculate_recall_at_k(retrieval, projected, k_values=(self.k,))
            score = result.value_at(self.k)
            detail = {
                "k": self.k,
                "relevant_total": result.relevant_total,
                "relevance_kind": _relevance_kind(document_truth),
            }
        else:
            if self.k is None:
                raise ValueError("document ndcg evaluator requires k")
            graded = [
                GradedRelevance(
                    document_id=item.document_id, chunk_id=item.document_id, relevance=item.relevance
                )
                for item in document_truth.relevant_documents
                if item.relevance > 0
            ]
            ranking = RankingGroundTruth(graded_relevance=graded)
            result = calculate_ndcg_at_k(ranking, projected, k_values=(self.k,))
            score = result.value_at(self.k)
            detail = {
                "k": self.k,
                "relevance_kind": _relevance_kind(document_truth),
            }
        return EvaluationResultDraft(
            evaluator_id=spec.evaluator_id,
            evaluator_version=spec.evaluator_version,
            config_ref=spec.config_ref,
            prompt_ref=spec.prompt_ref,
            verdict=EvaluationVerdict.PASS,
            reason="rag_document_metric_computed",
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


def _document_evaluator_ids() -> frozenset[str]:
    return frozenset(
        set(DOCUMENT_RECALL_IDS.values()) | set(DOCUMENT_NDCG_IDS.values()) | {DOCUMENT_MRR_ID}
    )


def _relevance_kind(document_truth: DocumentRetrievalGroundTruth) -> str:
    relevances = {item.relevance for item in document_truth.relevant_documents}
    return "binary" if relevances == {1} else "graded"


class RagMetricEvaluatorResolver:
    """解析冻结的 RAG metric evaluator identity（chunk-level 与 document-level）。"""

    def __init__(self, document_projection: DocumentProjection | None = None) -> None:
        self._document_projection = document_projection

    def resolve(self, spec: EvaluatorSpec) -> ResolvedEvaluator:
        """把 suite spec 解析为对应的 deterministic evaluator."""
        if spec.result_schema_ref != VersionRef("evaluation_result", "v1") or spec.comparison_semantics != "verdict_transition.v1":
            raise ValueError("unsupported evaluator result schema or comparison semantics")
        formal_process = _is_process_spec(spec)
        legacy_artifact = RAG_ARTIFACT_EVIDENCE_KIND in spec.required_evidence_kinds
        if formal_process == legacy_artifact:
            raise ValueError("evaluator descriptor must select exactly one RAG evidence authority")
        reverse_recall = {value: key for key, value in RECALL_IDS.items()}
        reverse_ndcg = {value: key for key, value in NDCG_IDS.items()}
        reverse_document_recall = {value: key for key, value in DOCUMENT_RECALL_IDS.items()}
        reverse_document_ndcg = {value: key for key, value in DOCUMENT_NDCG_IDS.items()}
        if spec.evaluator_version == DOCUMENT_EVALUATOR_VERSION:
            if spec.evaluator_id == DOCUMENT_MRR_ID:
                evaluator = RagMetricEvaluator(
                    DOCUMENT_MRR_ID, document_projection=self._document_projection
                )
            elif spec.evaluator_id in reverse_document_recall:
                evaluator = RagMetricEvaluator(
                    spec.evaluator_id,
                    reverse_document_recall[spec.evaluator_id],
                    document_projection=self._document_projection,
                )
            elif spec.evaluator_id in reverse_document_ndcg:
                evaluator = RagMetricEvaluator(
                    spec.evaluator_id,
                    reverse_document_ndcg[spec.evaluator_id],
                    document_projection=self._document_projection,
                )
            else:
                raise ValueError(f"unsupported document RAG evaluator: {spec.evaluator_id}")
            return ResolvedEvaluator(spec.evaluator_id, spec.evaluator_version, evaluator)
        if spec.evaluator_version != EVALUATOR_VERSION:
            raise ValueError(f"unsupported RAG evaluator version: {spec.evaluator_version}")
        if formal_process and spec.evaluator_id not in {*RECALL_IDS.values(), *NDCG_IDS.values(), MRR_ID}:
            raise ValueError(f"unsupported process-backed RAG evaluator: {spec.evaluator_id}")
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
    "DOCUMENT_EVALUATOR_VERSION",
    "DOCUMENT_MRR_ID",
    "DOCUMENT_NDCG_IDS",
    "DOCUMENT_RECALL_IDS",
    "EVALUATOR_VERSION",
    "MRR_ID",
    "NDCG_IDS",
    "RECALL_IDS",
    "RagMetricEvaluator",
    "RagMetricEvaluatorResolver",
]
