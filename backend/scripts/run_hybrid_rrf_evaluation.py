#!/usr/bin/env python
"""执行 Stage5 Phase3 WP2 SciFact 与 synthetic Hybrid RRF evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from app.core.evaluation.beir_scifact import load_beir_scifact_asset
from app.core.evaluation.dataset import load_dataset
from app.core.evaluation.document_metrics import DocumentProjection
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)
from app.services.evaluation.hybrid_rrf import (
    align_provenance,
    analyze_hybrid_rrf,
    build_rrf_candidate_evidence,
    execute_beir_scifact_hybrid_rrf,
    execute_synthetic_hybrid_rrf,
)
from app.services.evaluation.persistence import EvaluationPersistenceService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--scifact-base-url", required=True)
    parser.add_argument("--synthetic-base-url", required=True)
    parser.add_argument("--beir-dataset-root", type=Path, required=True)
    parser.add_argument("--dense-manifest", type=Path, required=True)
    parser.add_argument("--dense-cache-metadata", type=Path, required=True)
    parser.add_argument("--sparse-cache-metadata", type=Path, required=True)
    parser.add_argument("--current-report", type=Path, required=True)
    parser.add_argument("--bm25-report", type=Path, required=True)
    parser.add_argument("--scifact-provenance", type=Path, required=True)
    parser.add_argument("--synthetic-provenance", type=Path, required=True)
    parser.add_argument(
        "--synthetic-dataset",
        type=Path,
        default=Path("evaluation_assets/rag_quality_v1/rag_evaluation_dataset.v1.json"),
    )
    parser.add_argument("--rrf-report-output", type=Path, required=True)
    parser.add_argument("--synthetic-output", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    return parser


def _persistence() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(
        lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
    )


def _cache(path: Path, expected_schema: str) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("cache_status") != "READY"
        or value.get("cache_schema_version") != expected_schema
        or not isinstance(value.get("cache_key"), str)
    ):
        raise ValueError(f"cache metadata is not READY for {expected_schema}")
    return {
        "identity": value["cache_key"],
        "status": "CACHE_HIT",
        "schema_version": expected_schema,
        "chunk_manifest_sha256": value.get("chunk_manifest_sha256"),
        "embedding_rebuild": "NO" if "dense" in expected_schema else None,
    }


def _jsonl(path: Path) -> list[dict[str, object]]:
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("hybrid provenance row must be an object")
            values.append(value)
    return values


async def _run(args) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    dense_cache = _cache(args.dense_cache_metadata, "beir-scifact-dense-index-cache.v1")
    sparse_cache = _cache(args.sparse_cache_metadata, "beir-scifact-bm25-index-cache.v1")
    projection = DocumentProjection.from_manifest(
        json.loads(args.dense_manifest.read_text(encoding="utf-8"))
    )
    rrf_report = await execute_beir_scifact_hybrid_rrf(
        persistence=_persistence(),
        project_id=args.project_id,
        asset=load_beir_scifact_asset(args.beir_dataset_root),
        base_url=args.scifact_base_url,
        document_projection=projection,
        dense_index_cache=dense_cache,
        sparse_index_cache=sparse_cache,
    )
    synthetic_report = await execute_synthetic_hybrid_rrf(
        persistence=_persistence(),
        project_id=args.project_id,
        dataset=load_dataset(args.synthetic_dataset),
        base_url=args.synthetic_base_url,
    )
    current = json.loads(args.current_report.read_text(encoding="utf-8"))
    bm25 = json.loads(args.bm25_report.read_text(encoding="utf-8"))
    analysis = analyze_hybrid_rrf(current, bm25, rrf_report)
    provenance = align_provenance(rrf_report, _jsonl(args.scifact_provenance))
    evidence = build_rrf_candidate_evidence(rrf_report, analysis, provenance)
    # synthetic sidecar 也必须完整产生；其指标保持独立，不混入 SciFact evidence。
    align_provenance(
        {
            "case_results": [
                {**item, "benchmark_query_id": item["case_id"]}
                for item in synthetic_report["case_results"]
            ]
        },
        _jsonl(args.synthetic_provenance),
    )
    return rrf_report, synthetic_report, evidence


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """执行两套 Hybrid RRF evaluation 并写出报告与 evidence."""
    args = _parser().parse_args()
    rrf_report, synthetic_report, evidence = asyncio.run(_run(args))
    _write(args.rrf_report_output, rrf_report)
    _write(args.synthetic_output, synthetic_report)
    _write(args.evidence_output, evidence)
    print(
        json.dumps(
            {
                "status": "PASS",
                "scifact_metrics": rrf_report["metrics"],
                "synthetic_metrics": synthetic_report["metrics"],
                "top5": evidence["analysis"]["by_k"]["top5"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
