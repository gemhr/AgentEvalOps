"""Stage5 Phase3 versioned RAG quality baseline orchestration."""

from __future__ import annotations

import math
from collections import Counter
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
from app.core.evaluation.process_trajectory import ProcessKind, parse_process_trajectory
from app.core.evaluation.platform_metrics import MetricObservation, MetricState, aggregate_metric
from app.core.evaluation.references import VersionRef
from app.services.evaluation.loop import EvaluationLoopService
from app.services.evaluation.persistence import EvaluationPersistenceService
from app.services.evaluation.platform_metrics import _quality_definition

SUITE_ID = "rag-baseline-suite"
SUITE_VERSION = "v1"
CONFIG_REF = VersionRef("rag_metric_config", "rag-quality-metrics.v1-k1-3-5")


def build_rag_baseline_suite(
    dataset: EvaluationDataset, *, process_evidence: bool = True
) -> EvaluationSuiteVersion:
    """为 Dataset v1 构建冻结的 Recall/MRR/NDCG Suite."""
    specs = []
    for k, evaluator_id in RECALL_IDS.items():
        specs.append(_spec(evaluator_id, {"metric": "recall_at_k", "k": k}, process_evidence=process_evidence))
    specs.append(_spec(MRR_ID, {"metric": "mrr"}, process_evidence=process_evidence))
    for k, evaluator_id in NDCG_IDS.items():
        specs.append(_spec(evaluator_id, {"metric": "ndcg_at_k", "k": k}, process_evidence=process_evidence))
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


def _spec(
    evaluator_id: str, config: Mapping[str, object], *, process_evidence: bool = True
) -> EvaluatorSpec:
    snapshot = dict(config)
    if process_evidence:
        snapshot["process_evidence_requirements"] = {
            "schema_version": "process-evidence-requirements.v1",
            "trajectory_schema_version": "stage11.wp4.v1",
            "requirements": [{
                "kind": "RETRIEVAL",
                "fields": ["retrieval_id", "retrieved_items", "ranked_items"],
                "accepted_provenance": ["FIXTURE", "RUNTIME"],
                "allow_partial": False,
                "allow_not_applicable": False,
            }],
        }
    return EvaluatorSpec(
        evaluator_id=evaluator_id,
        evaluator_version=EVALUATOR_VERSION,
        evaluator_kind=EvaluatorKind.DETERMINISTIC,
        config_ref=CONFIG_REF,
        score_direction=ScoreDirection.HIGHER_IS_BETTER,
        config_snapshot=snapshot,
        score_range=(0.0, 1.0),
        required_evidence_kinds=("process_trajectory",) if process_evidence else ("rag_evaluation_artifact",),
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


async def execute_rag_quality_baseline(
    *,
    persistence: EvaluationPersistenceService,
    project_id: UUID,
    dataset: EvaluationDataset,
    base_url: str,
    bearer_token: str,
    baseline_ref: str = "stage5-phase3-rag-quality-baseline.v1",
    run_metadata: Mapping[str, object] | None = None,
    report_metadata: Mapping[str, object] | None = None,
    worker_ref: str = "rag-baseline-v1",
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
        metadata={"baseline_ref": baseline_ref, **dict(run_metadata or {})},
    )
    target = LocalAgentHttpExecutionTarget(target_ref, base_url, bearer_token=bearer_token)
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
                worker_ref=worker_ref,
            )
    finally:
        await target.aclose()
    final_attempts = await persistence.list_attempts(project_id, run.run_id)
    results = await persistence.list_results(project_id, run.run_id)
    result_by_slot = {(item.case_id, item.evaluator_id): item for item in results}
    aggregates = {}
    for spec in suite.evaluator_specs:
        definition = _quality_definition({
            "evaluator_id": spec.evaluator_id, "evaluator_version": spec.evaluator_version,
            "score_direction": spec.score_direction.value, "config_snapshot": spec.config_snapshot,
            "definition_digest": spec.definition_digest,
        })
        observations = []
        for case_ref in suite.case_selection:
            result = result_by_slot.get((case_ref.case_id, spec.evaluator_id))
            valid = result is not None and result.score is not None and result.verdict.value in {"PASS", "FAIL"}
            observations.append(MetricObservation(
                case_ref.case_id, MetricState.VALID if valid else MetricState.MISSING if result is None
                else MetricState.ERROR if result.verdict.value == "ERROR" else MetricState.INCONCLUSIVE
                if result.verdict.value == "INCONCLUSIVE" else MetricState.UNAVAILABLE,
                result.score if valid else None, observed=result is not None,
            ))
        aggregates[spec.evaluator_id] = aggregate_metric(definition, tuple(observations), scope="baseline")
    dataset_cases = {item.case_id: item for item in dataset.cases}
    case_results = []
    retrieval_latencies: list[int] = []
    rerank_latencies: list[int] = []
    total_latencies: list[int] = []
    outcomes: Counter[str] = Counter()
    for attempt in final_attempts:
        refs = [ref for ref in attempt.outcome_evidence_refs if ref.kind == "process_trajectory"]
        if len(refs) != 1:
            raise RuntimeError("baseline attempt must contain exactly one process trajectory")
        trajectory = parse_process_trajectory(refs[0])
        retrievals = [record.payload for record in trajectory.records if record.kind is ProcessKind.RETRIEVAL]
        if len(retrievals) != 1:
            raise RuntimeError("baseline attempt must contain exactly one Retrieval record")
        retrieval = retrievals[0]
        outcomes[retrieval.retrieval_status] += 1
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
                "query": None,
                "ground_truth": case.ground_truth.model_dump(mode="json", exclude_none=True),
                "retrieved_ids": [
                    [item.document_id, item.chunk_id] for item in retrieval.retrieved_items
                ],
                "ranked_ids": [[item.document_id, item.chunk_id] for item in retrieval.ranked_items],
                "selected_ids": [
                    [item.document_id, item.chunk_id] for item in retrieval.selected_items
                ],
                "scores": scores,
                "retrieval_status": retrieval.retrieval_status,
                "retrieval_latency_ms": None,
                "rerank_latency_ms": None,
                "total_latency_ms": None,
                "rewritten_query": None,
                "retrieval_channels": {},
            }
        )
    metrics = {name: float(item.value) for name, item in sorted(aggregates.items()) if item.value is not None}
    report = {
        "baseline_ref": baseline_ref,
        "run_id": str(run.run_id),
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.version,
        "dataset_case_count": len(dataset),
        "evaluated_retrieval_cases": aggregates[RECALL_IDS[1]].coverage.valid_count,
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
    report.update(dict(report_metadata or {}))
    return report


__all__ = [
    "SUITE_ID",
    "SUITE_VERSION",
    "build_rag_baseline_suite",
    "execute_rag_quality_baseline",
]
