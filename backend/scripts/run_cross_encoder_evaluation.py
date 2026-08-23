#!/usr/bin/env python
"""执行 Stage5 Phase3 WP3 SciFact 与 synthetic Cross-Encoder evaluation 并输出机械 gate.

真实模型资产不存在时（APPROVED_CROSS_ENCODER_MODEL_ASSET_NOT_PRESENT），CE runtime
无法启动，本 runner 无法产生真实 evidence；gate 输出 ``NOT_EVALUATED_BLOCKED``。
不得用 fake 数据冒充真实 benchmark。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.evaluation.beir_scifact import load_beir_scifact_asset
from app.core.evaluation.dataset import load_dataset
from app.core.evaluation.document_metrics import DocumentProjection
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)
from app.services.evaluation.cross_encoder import (
    WP2_DENSE_CACHE_KEY,
    CeExpectedConfig,
    align_ce_provenance,
    analyze_ce_rerank,
    build_ce_candidate_evidence,
    build_runner_invariants,
    evaluate_ce_acceptance_gate,
)
from app.services.evaluation.hybrid_rrf import (
    execute_beir_scifact_hybrid_rrf,
    execute_synthetic_hybrid_rrf,
)
from app.services.evaluation.persistence import EvaluationPersistenceService

# WP2 冻结的 BM25 cache identity（candidate/cache/corpus invariant 校验目标）。
WP2_BM25_CACHE_KEY = "594c9c95a3b6f29bcd2fcd738e23ee345427d06b49a70ac3149544a1a4f8f84b"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--ce-base-url", required=True)
    parser.add_argument("--synthetic-base-url", required=True)
    parser.add_argument("--beir-dataset-root", type=Path, required=True)
    parser.add_argument("--dense-manifest", type=Path, required=True)
    parser.add_argument("--dense-cache-metadata", type=Path, required=True)
    parser.add_argument("--sparse-cache-metadata", type=Path, required=True)
    parser.add_argument("--rrf-report", type=Path, required=True)
    parser.add_argument("--rrf-synthetic-report", type=Path, required=True)
    parser.add_argument("--ce-provenance", type=Path, required=True)
    parser.add_argument("--synthetic-ce-provenance", type=Path, required=True)
    parser.add_argument("--ce-expected-model-ref", required=True)
    parser.add_argument("--ce-expected-asset-tree-sha256", required=True)
    parser.add_argument(
        "--synthetic-dataset",
        type=Path,
        default=Path("evaluation_assets/rag_quality_v1/rag_evaluation_dataset.v1.json"),
    )
    parser.add_argument("--ce-report-output", type=Path, required=True)
    parser.add_argument("--ce-evidence-output", type=Path, required=True)
    parser.add_argument("--ce-gate-output", type=Path, required=True)
    return parser


def _persistence() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(
        lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
    )


def _cache(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("cache_status") != "READY"
        or not isinstance(value.get("cache_key"), str)
    ):
        raise ValueError(f"cache metadata is not READY: {path}")
    return {"cache_key": value["cache_key"], "cache_schema_version": value.get("cache_schema_version", "")}


def _jsonl(path: Path) -> list[dict[str, object]]:
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("CE provenance row must be an object")
            values.append(value)
    return values


def _invariants(args, report, synthetic_report, failure_rows, aligned) -> dict[str, object]:
    dense = _cache(args.dense_cache_metadata)
    sparse = _cache(args.sparse_cache_metadata)
    load_beir_scifact_asset(args.beir_dataset_root)  # verify_checksums=True 冻结 corpus
    return build_runner_invariants(
        dense_cache_key=dense["cache_key"],
        sparse_cache_key=sparse["cache_key"],
        corpus_checksums_ok=True,
        report=report,
        synthetic_report=synthetic_report,
        aligned=aligned,
        failure_rows=failure_rows,
    )


def _expected_config(args) -> CeExpectedConfig:
    return CeExpectedConfig(
        model_ref=args.ce_expected_model_ref,
        asset_tree_sha256=args.ce_expected_asset_tree_sha256,
    )


def _synthetic_report_view(synthetic_report: Mapping[str, Any]) -> dict[str, object]:
    """把 synthetic case_results 规范化为与 SciFact 一致的 retrieved/ranked chunk id 字段."""
    cases = []
    for item in synthetic_report["case_results"]:
        view = dict(item)
        view["benchmark_query_id"] = item["case_id"]
        view["retrieved_chunk_ids"] = item["retrieved_ids"]
        view["ranked_chunk_ids"] = item["ranked_ids"]
        cases.append(view)
    return {"case_results": cases}


async def _run(args) -> tuple[dict, dict, dict, dict, dict, dict, dict]:
    projection = DocumentProjection.from_manifest(
        json.loads(args.dense_manifest.read_text(encoding="utf-8"))
    )
    ce_report = await execute_beir_scifact_hybrid_rrf(
        persistence=_persistence(),
        project_id=args.project_id,
        asset=load_beir_scifact_asset(args.beir_dataset_root),
        base_url=args.ce_base_url,
        document_projection=projection,
        dense_index_cache={"identity": _cache(args.dense_cache_metadata)["cache_key"], "status": "CACHE_HIT"},
        sparse_index_cache={"identity": _cache(args.sparse_cache_metadata)["cache_key"], "status": "CACHE_HIT"},
    )
    synthetic_report = await execute_synthetic_hybrid_rrf(
        persistence=_persistence(),
        project_id=args.project_id,
        dataset=load_dataset(args.synthetic_dataset),
        base_url=args.synthetic_base_url,
    )
    rrf_report = json.loads(args.rrf_report.read_text(encoding="utf-8"))
    rrf_synthetic = json.loads(args.rrf_synthetic_report.read_text(encoding="utf-8"))
    ce_sidecar = _jsonl(args.ce_provenance)
    synthetic_sidecar = _jsonl(args.synthetic_ce_provenance)
    expected = _expected_config(args)

    # synthetic CE sidecar 也必须使用完全相同的批准 provenance 与 exact transition 校验。
    synthetic_view = _synthetic_report_view(synthetic_report)
    align_ce_provenance(synthetic_view, synthetic_sidecar, expected=expected)

    analysis = analyze_ce_rerank(rrf_report, ce_report)
    provenance = align_ce_provenance(ce_report, ce_sidecar, expected=expected)
    aligned = provenance["aligned"]
    failure_rows = provenance["failure_rows"]
    invariants = _invariants(args, ce_report, synthetic_report, failure_rows, aligned)
    success_rows = list(aligned.values())
    real_load_latency_present = any(
        isinstance(row.get("latency_ms", {}).get("model_load_latency_ms"), (int, float))
        for row in success_rows
    )
    total_queries = len(ce_report["case_results"]) + len(failure_rows)
    gate = evaluate_ce_acceptance_gate(
        scifact_metrics=ce_report.get("metrics"),
        synthetic_metrics=synthetic_report.get("metrics"),
        technical_failure_count=len(failure_rows),
        total_queries=total_queries,
        case_guardrails_ok=analysis["case_guardrails_ok"],
        rank_transition_ok=analysis["rank_transition_ok"],
        invariants_ok=invariants["all_ok"],
        real_load_latency_present=real_load_latency_present,
    )
    evidence = build_ce_candidate_evidence(
        report=ce_report,
        analysis=analysis,
        aligned=aligned,
        failure_rows=failure_rows,
        invariants=invariants,
        gate_input=gate,
        rrf_report=rrf_report,
        expected=expected,
    )
    return ce_report, synthetic_report, evidence, gate, analysis, rrf_synthetic, invariants


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """执行 CE SciFact + synthetic evaluation，写出 report/evidence/gate."""
    args = _parser().parse_args()
    ce_report, synthetic_report, evidence, gate, analysis, rrf_synthetic, invariants = asyncio.run(
        _run(args)
    )
    _write(args.ce_report_output, ce_report)
    _write(args.ce_evidence_output, evidence)
    _write(args.ce_gate_output, gate)
    print(
        json.dumps(
            {
                "status": "DONE",
                "gate_outcome": gate["outcome"],
                "blocked_reasons": gate["blocked_reasons"],
                "failure_reasons": gate["failure_reasons"],
                "scifact_metrics": ce_report.get("metrics"),
                "synthetic_metrics": synthetic_report.get("metrics"),
                "technical_failure_count": invariants["technical_failure_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())