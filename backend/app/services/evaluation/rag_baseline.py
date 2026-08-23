"""Stage5 Phase3 versioned RAG quality baseline orchestration."""

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
    EVALUATOR_VERSION,
    MRR_ID,
    NDCG_IDS,
    RECALL_IDS,
    RagMetricEvaluatorResolver,
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
from app.core.evaluation.execution import ExecutionTargetRef
from app.core.evaluation.rag_artifact import RAG_ARTIFACT_EVIDENCE_KIND, RagEvaluationArtifactV1
from app.core.evaluation.references import VersionRef
from app.services.evaluation.loop import EvaluationLoopService
from app.services.evaluation.persistence import EvaluationPersistenceService

SUITE_ID = "rag-baseline-suite"
SUITE_VERSION = "v1"
CONFIG_REF = VersionRef("rag_metric_config", "rag-quality-metrics.v1-k1-3-5")


def build_rag_baseline_suite(dataset: EvaluationDataset) -> EvaluationSuiteVersion:
    """为 Dataset v1 构建冻结的 Recall/MRR/NDCG Suite."""
    specs = []
    for k, evaluator_id in RECALL_IDS.items():
        specs.append(_spec(evaluator_id, {"metric": "recall_at_k", "k": k}))
    specs.append(_spec(MRR_ID, {"metric": "mrr"}))
    for k, evaluator_id in NDCG_IDS.items():
        specs.append(_spec(evaluator_id, {"metric": "ndcg_at_k", "k": k}))
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
        },
    )


def _spec(evaluator_id: str, config: Mapping[str, object]) -> EvaluatorSpec:
    return EvaluatorSpec(
        evaluator_id=evaluator_id,
        evaluator_version=EVALUATOR_VERSION,
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


def _latency(values: list[int]) -> dict[str, float]:
    return {
        "mean": float(mean(values)),
        "p50": float(median(values)),
        "p95": _percentile(values, 0.95),
    }


async def execute_rag_quality_baseline(
    *,
    persistence: EvaluationPersistenceService,
    project_id: UUID,
    dataset: EvaluationDataset,
    base_url: str,
) -> dict[str, object]:
    """通过现有 EvaluationLoop 执行并聚合一次真实 RAG Baseline."""
    created_at = _now()
    catalog_dataset, cases = bridge_dataset_to_catalog(dataset, created_at=created_at)
    suite = build_rag_baseline_suite(dataset)
    target_ref = _target_ref()
    run, attempts = await persistence.create_run(
        project_id=project_id,
        dataset=catalog_dataset,
        suite=suite,
        cases=cases,
        target=target_ref,
        timeout=timedelta(seconds=60),
        metadata={"baseline_ref": "stage5-phase3-rag-quality-baseline.v1"},
    )
    target = LocalAgentHttpExecutionTarget(target_ref, base_url)
    loop = EvaluationLoopService(
        persistence,
        _FixedTargetResolver(target),
        RagMetricEvaluatorResolver(),
    )
    try:
        for attempt in attempts:
            await loop.execute_attempt(
                project_id,
                attempt.attempt_id,
                cases[attempt.case_ref],
                lease=timedelta(minutes=5),
                worker_ref="rag-baseline-v1",
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
        scores = {
            result.evaluator_id: result.score
            for result in results
            if result.attempt_id == str(attempt.attempt_id)
        }
        case_results.append(
            {
                "case_id": case.case_id,
                "case_version": dataset.version,
                "case_type": case.metadata.get("case_type"),
                "query": case.input.get("query"),
                "ground_truth": case.ground_truth.model_dump(mode="json", exclude_none=True),
                "retrieved_ids": [
                    [item.document_id, item.chunk_id] for item in artifact.retrieved_items
                ],
                "ranked_ids": [[item.document_id, item.chunk_id] for item in artifact.ranked_items],
                "selected_ids": [
                    [item.document_id, item.chunk_id] for item in artifact.selected_items
                ],
                "scores": scores,
                "retrieval_status": artifact.retrieval_status,
                "retrieval_latency_ms": artifact.retrieval_latency_ms,
                "rerank_latency_ms": artifact.rerank_latency_ms,
                "total_latency_ms": artifact.total_latency_ms,
                "rewritten_query": artifact.rewritten_query,
                "retrieval_channels": {
                    item.chunk_id: list(item.retrieval_channels) for item in artifact.retrieved_items
                },
            }
        )
    metrics = {
        evaluator_id: float(mean(values)) for evaluator_id, values in sorted(result_scores.items())
    }
    return {
        "baseline_ref": "stage5-phase3-rag-quality-baseline.v1",
        "run_id": str(run.run_id),
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.version,
        "dataset_case_count": len(dataset),
        "evaluated_retrieval_cases": len(result_scores.get(RECALL_IDS[1], [])),
        "suite_id": SUITE_ID,
        "suite_version": SUITE_VERSION,
        "metrics": metrics,
        "outcomes": dict(sorted(outcomes.items())),
        "latency_ms": {
            "retrieval": _latency(retrieval_latencies),
            "rerank": _latency(rerank_latencies),
            "total": _latency(total_latencies),
        },
        "case_results": case_results,
    }


__all__ = [
    "SUITE_ID",
    "SUITE_VERSION",
    "build_rag_baseline_suite",
    "execute_rag_quality_baseline",
]
