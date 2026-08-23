"""Stage5 Phase3 WP0B BEIR SciFact current dense baseline orchestration."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Mapping
from uuid import UUID

from app.adapters.evaluation.http_localagent import (
    LOCALAGENT_HTTP_EVALUATION_V2_CONFIG,
    LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION,
    LOCALAGENT_HTTP_TARGET_ID,
    LOCALAGENT_HTTP_TARGET_KIND,
    LocalAgentHttpExecutionTarget,
)
from app.adapters.evaluation.rag_metrics import (
    DOCUMENT_EVALUATOR_VERSION,
    DOCUMENT_MRR_ID,
    DOCUMENT_NDCG_IDS,
    DOCUMENT_RECALL_IDS,
    RagMetricEvaluatorResolver,
)
from app.core.evaluation.beir_scifact import (
    BENCHMARK_KIND,
    BEIR_SCIFACT_DATASET_ID,
    BEIR_SCIFACT_DATASET_VERSION,
    BeirScifactAsset,
    build_beir_scifact_dataset,
)
from app.core.evaluation.catalog import (
    EvaluationPolicy,
    EvaluationSuiteVersion,
    EvaluatorKind,
    EvaluatorSpec,
    ScoreDirection,
)
from app.core.evaluation.dataset import EvaluationDataset
from app.core.evaluation.dataset_bridge import bridge_dataset_to_catalog
from app.core.evaluation.document_metrics import DocumentProjection
from app.core.evaluation.execution import ExecutionTargetRef
from app.core.evaluation.rag_artifact import RAG_ARTIFACT_EVIDENCE_KIND, RagEvaluationArtifactV1
from app.core.evaluation.references import VersionRef
from app.services.evaluation.loop import EvaluationLoopService
from app.services.evaluation.persistence import EvaluationPersistenceService

SUITE_ID = "beir-scifact-baseline-suite"
SUITE_VERSION = "v1"
BASELINE_REF = "stage5-phase3-wp0b-beir-scifact-current-dense-baseline.v1"
CONFIG_REF = VersionRef("rag_document_metric_config", "rag-document-metrics.v1-k1-3-5")


def build_beir_scifact_suite(dataset: EvaluationDataset) -> EvaluationSuiteVersion:
    """为 BEIR SciFact dataset 构建冻结的 document-level Recall/MRR/NDCG Suite."""
    specs = []
    for k, evaluator_id in DOCUMENT_RECALL_IDS.items():
        specs.append(_spec(evaluator_id, {"metric": "document_recall_at_k", "k": k}))
    specs.append(_spec(DOCUMENT_MRR_ID, {"metric": "document_mrr"}))
    for k, evaluator_id in DOCUMENT_NDCG_IDS.items():
        specs.append(_spec(evaluator_id, {"metric": "document_ndcg_at_k", "k": k}))
    refs = tuple(bridge_dataset_to_catalog(dataset, created_at=_now())[0].case_version_refs)
    return EvaluationSuiteVersion(
        suite_id=SUITE_ID,
        version=SUITE_VERSION,
        case_selection=refs,
        evaluator_specs=tuple(specs),
        evaluation_policy=EvaluationPolicy(),
        created_at=_now(),
        metadata={
            "metric_k": [1, 3, 5],
            "candidate_limit": 8,
            "query_rewrite": "identity-rewrite.v1",
            "benchmark_kind": BENCHMARK_KIND,
        },
    )


def _spec(evaluator_id: str, config: Mapping[str, object]) -> EvaluatorSpec:
    return EvaluatorSpec(
        evaluator_id=evaluator_id,
        evaluator_version=DOCUMENT_EVALUATOR_VERSION,
        evaluator_kind=EvaluatorKind.DETERMINISTIC,
        config_ref=CONFIG_REF,
        score_direction=ScoreDirection.HIGHER_IS_BETTER,
        config_snapshot=dict(config),
        score_range=(0.0, 1.0),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FixedTargetResolver:
    def __init__(self, target: LocalAgentHttpExecutionTarget) -> None:
        self.target = target

    def resolve(self, target_ref: ExecutionTargetRef):
        if target_ref != self.target.target_ref:
            raise ValueError("baseline target identity mismatch")
        return self.target


def _target_ref() -> ExecutionTargetRef:
    return ExecutionTargetRef(
        target_id=LOCALAGENT_HTTP_TARGET_ID,
        target_kind=LOCALAGENT_HTTP_TARGET_KIND,
        target_version_ref=LOCALAGENT_HTTP_EVALUATION_V2_TARGET_VERSION,
        config_ref=LOCALAGENT_HTTP_EVALUATION_V2_CONFIG,
    )


def _percentile(values: list[int], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return float(ordered[index])


def _latency(values: list[int]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "p50": None, "p95": None}
    return {
        "mean": float(mean(values)),
        "p50": float(median(values)),
        "p95": _percentile(values, 0.95),
    }


def _first_relevant_document_rank(document_ids: list[str], relevant: set[str]) -> int | None:
    for position, document_id in enumerate(document_ids, 1):
        if document_id in relevant:
            return position
    return None


async def execute_beir_scifact_baseline(
    *,
    persistence: EvaluationPersistenceService,
    project_id: UUID,
    asset: BeirScifactAsset,
    base_url: str,
    document_projection: DocumentProjection,
    dense_index_cache: Mapping[str, object] | None = None,
    baseline_ref: str = BASELINE_REF,
    run_metadata: Mapping[str, object] | None = None,
    report_metadata: Mapping[str, object] | None = None,
    worker_ref: str = "beir-scifact-baseline-v1",
) -> dict[str, object]:
    """通过现有 EvaluationLoop 执行完整 BEIR SciFact test split 并聚合 document metrics."""
    dataset = build_beir_scifact_dataset(asset)
    created_at = _now()
    catalog_dataset, cases = bridge_dataset_to_catalog(dataset, created_at=created_at)
    suite = build_beir_scifact_suite(dataset)
    run, attempts = await persistence.create_run(
        project_id=project_id,
        dataset=catalog_dataset,
        suite=suite,
        cases=cases,
        target=_target_ref(),
        timeout=timedelta(seconds=60),
        metadata={
            "baseline_ref": baseline_ref,
            "benchmark_kind": BENCHMARK_KIND,
            "benchmark": "beir",
            "benchmark_dataset": "scifact",
            "benchmark_split": "test",
            "dense_index_cache": dict(dense_index_cache or {}),
            **dict(run_metadata or {}),
        },
    )
    target = LocalAgentHttpExecutionTarget(_target_ref(), base_url)
    loop = EvaluationLoopService(
        persistence,
        _FixedTargetResolver(target),
        RagMetricEvaluatorResolver(document_projection=document_projection),
    )
    try:
        for attempt in attempts:
            await loop.execute_attempt(
                project_id,
                attempt.attempt_id,
                cases[attempt.case_ref],
                lease=timedelta(minutes=5),
                worker_ref=worker_ref,
            )
    finally:
        await target.aclose()

    final_attempts = await persistence.list_attempts(project_id, run.run_id)
    results = await persistence.list_results(project_id, run.run_id)
    result_scores: dict[str, list[float]] = defaultdict(list)
    for result in results:
        if result.score is not None:
            result_scores[result.evaluator_id].append(result.score)

    dataset_cases = {item.case_id: item for item in dataset.cases}
    case_results = []
    retrieval_latencies: list[int] = []
    rerank_latencies: list[int] = []
    total_latencies: list[int] = []
    outcomes: Counter[str] = Counter()
    diagnostics: Counter[str] = Counter()
    unique_retrieved_docs: list[int] = []
    unique_ranked_docs: list[int] = []
    selected_doc_counts: list[int] = []
    for attempt in final_attempts:
        refs = [ref for ref in attempt.outcome_evidence_refs if ref.kind == RAG_ARTIFACT_EVIDENCE_KIND]
        if len(refs) != 1:
            raise RuntimeError("baseline attempt must contain exactly one RAG artifact")
        artifact = RagEvaluationArtifactV1.model_validate(refs[0].metadata["payload"])
        outcomes[artifact.retrieval_status] += 1
        if artifact.retrieval_latency_ms is not None:
            retrieval_latencies.append(artifact.retrieval_latency_ms)
        if artifact.rerank_latency_ms is not None:
            rerank_latencies.append(artifact.rerank_latency_ms)
        total_latencies.append(artifact.total_latency_ms)

        case = dataset_cases[attempt.case_ref.case_id]
        truth = case.ground_truth.document_retrieval
        relevant = {
            item.document_id for item in truth.relevant_documents if item.relevance > 0
        }
        retrieved_documents = [
            document_projection.benchmark_document_id(item.document_id)
            for item in sorted(artifact.retrieved_items, key=lambda entry: entry.retrieval_rank)
        ]
        seen: set[str] = set()
        retrieved_document_ids: list[str] = []
        for document_id in retrieved_documents:
            if document_id not in seen:
                seen.add(document_id)
                retrieved_document_ids.append(document_id)
        ranked_document_ids = _projected_ranked_documents(
            artifact, document_projection, "ranked_items", "rank"
        )
        selected_document_ids = _projected_ranked_documents(
            artifact, document_projection, "selected_items", "selection_rank"
        )
        unique_retrieved_docs.append(len(retrieved_document_ids))
        unique_ranked_docs.append(len(ranked_document_ids))
        selected_doc_counts.append(len(selected_document_ids))

        retrieved_rank = _first_relevant_document_rank(retrieved_document_ids, relevant)
        ranked_rank = _first_relevant_document_rank(ranked_document_ids, relevant)
        if ranked_rank == 1:
            diagnostics["queries_with_relevant_doc_at_rank_1"] += 1
        if ranked_rank is not None and ranked_rank <= 3:
            diagnostics["queries_with_relevant_doc_in_top3"] += 1
        if ranked_rank is not None and ranked_rank <= 5:
            diagnostics["queries_with_relevant_doc_in_top5"] += 1
        else:
            diagnostics["queries_with_no_relevant_doc_in_top5"] += 1
        if retrieved_rank is None:
            diagnostics["dense_retrieval_miss"] += 1
        if (
            retrieved_rank is not None
            and ranked_rank is not None
            and ranked_rank < retrieved_rank
        ):
            diagnostics["heuristic_rerank_improved"] += 1
        if (
            retrieved_rank is not None
            and ranked_rank is not None
            and ranked_rank > retrieved_rank
        ):
            diagnostics["heuristic_rerank_degraded"] += 1
        selected_relevant = bool(set(selected_document_ids) & relevant)
        if ranked_rank is not None and ranked_rank <= 3 and not selected_relevant:
            diagnostics["selection_dropped_relevant_doc"] += 1

        scores = {
            result.evaluator_id: result.score
            for result in results
            if result.attempt_id == str(attempt.attempt_id)
        }
        case_results.append(
            {
                "case_id": case.case_id,
                "case_version": dataset.version,
                "benchmark_query_id": case.metadata["benchmark_query_id"],
                "query": case.input.get("query"),
                "qrels_document_ids": sorted(relevant),
                "retrieved_document_ids": retrieved_document_ids,
                "ranked_document_ids": ranked_document_ids,
                "selected_document_ids": selected_document_ids,
                "retrieved_chunk_ids": [
                    [item.document_id, item.chunk_id] for item in artifact.retrieved_items
                ],
                "ranked_chunk_ids": [
                    [item.document_id, item.chunk_id] for item in artifact.ranked_items
                ],
                "selected_chunk_ids": [
                    [item.document_id, item.chunk_id] for item in artifact.selected_items
                ],
                "scores": scores,
                "retrieval_status": artifact.retrieval_status,
                "retrieval_latency_ms": artifact.retrieval_latency_ms,
                "rerank_latency_ms": artifact.rerank_latency_ms,
                "total_latency_ms": artifact.total_latency_ms,
                "rewritten_query": artifact.rewritten_query,
            }
        )

    metrics = {
        evaluator_id: float(mean(values)) for evaluator_id, values in sorted(result_scores.items())
    }
    report = {
        "baseline_ref": baseline_ref,
        "benchmark_kind": BENCHMARK_KIND,
        "run_id": str(run.run_id),
        "dataset_id": BEIR_SCIFACT_DATASET_ID,
        "dataset_version": BEIR_SCIFACT_DATASET_VERSION,
        "dataset_case_count": len(dataset),
        "evaluated_retrieval_cases": len(result_scores.get(DOCUMENT_RECALL_IDS[1], [])),
        "suite_id": SUITE_ID,
        "suite_version": SUITE_VERSION,
        "asset": {
            "root": str(asset.root),
            "checksums": dict(asset.checksums),
            "statistics": dict(asset.statistics),
        },
        "document_projection": {
            "mapped_documents": len(document_projection),
        },
        "dense_index_cache": dict(dense_index_cache or {}),
        "metrics": metrics,
        "outcomes": dict(sorted(outcomes.items())),
        "diagnostics": dict(sorted(diagnostics.items())),
        "document_counts": {
            "mean_unique_retrieved_documents": float(mean(unique_retrieved_docs)),
            "mean_unique_ranked_documents": float(mean(unique_ranked_docs)),
            "mean_selected_documents": float(mean(selected_doc_counts)),
        },
        "latency_ms": {
            "retrieval": _latency(retrieval_latencies),
            "rerank": _latency(rerank_latencies),
            "total": _latency(total_latencies),
        },
        "case_results": case_results,
    }
    report.update(dict(report_metadata or {}))
    return report


def _projected_ranked_documents(
    artifact: RagEvaluationArtifactV1,
    document_projection: DocumentProjection,
    field: str,
    rank_field: str,
) -> list[str]:
    items = getattr(artifact, field)
    ordered = sorted(items, key=lambda entry: getattr(entry, rank_field))
    document_ids: list[str] = []
    seen: set[str] = set()
    for item in ordered:
        benchmark_id = document_projection.benchmark_document_id(item.document_id)
        if benchmark_id not in seen:
            seen.add(benchmark_id)
            document_ids.append(benchmark_id)
    return document_ids


__all__ = [
    "BASELINE_REF",
    "SUITE_ID",
    "SUITE_VERSION",
    "build_beir_scifact_suite",
    "execute_beir_scifact_baseline",
]
