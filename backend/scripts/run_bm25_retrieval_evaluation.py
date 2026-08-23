#!/usr/bin/env python
"""执行 SciFact 与 synthetic 的 BM25-only WP1 evaluation."""

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
from app.services.evaluation.bm25_retrieval import (
    analyze_complementarity,
    build_bm25_candidate_evidence,
    execute_beir_scifact_bm25,
    execute_synthetic_bm25,
)
from app.services.evaluation.persistence import EvaluationPersistenceService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--scifact-base-url", required=True)
    parser.add_argument("--synthetic-base-url", required=True)
    parser.add_argument("--beir-dataset-root", type=Path, required=True)
    parser.add_argument("--dense-manifest", type=Path, required=True)
    parser.add_argument("--sparse-cache-metadata", type=Path, required=True)
    parser.add_argument("--current-evidence", type=Path, required=True)
    parser.add_argument(
        "--synthetic-dataset",
        type=Path,
        default=Path("evaluation_assets/rag_quality_v1/rag_evaluation_dataset.v1.json"),
    )
    parser.add_argument("--bm25-report-output", type=Path, required=True)
    parser.add_argument("--synthetic-output", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    return parser


def _persistence() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(
        lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
    )


async def _run(args) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    sparse_metadata = json.loads(args.sparse_cache_metadata.read_text(encoding="utf-8"))
    required = (
        "cache_key",
        "cache_status",
        "cache_schema_version",
        "chunk_manifest_sha256",
        "build_elapsed_seconds",
    )
    if not isinstance(sparse_metadata, dict) or any(field not in sparse_metadata for field in required):
        raise ValueError("BEIR SciFact BM25 cache metadata is incomplete")
    if sparse_metadata["cache_status"] != "READY":
        raise ValueError("BEIR SciFact BM25 cache is not READY")
    dense_manifest = json.loads(args.dense_manifest.read_text(encoding="utf-8"))
    projection = DocumentProjection.from_manifest(dense_manifest)
    bm25_report = await execute_beir_scifact_bm25(
        persistence=_persistence(),
        project_id=args.project_id,
        asset=load_beir_scifact_asset(args.beir_dataset_root),
        base_url=args.scifact_base_url,
        document_projection=projection,
        sparse_index_cache={
            "identity": sparse_metadata["cache_key"],
            "status": "CACHE_HIT",
            "schema_version": sparse_metadata["cache_schema_version"],
            "chunk_manifest_sha256": sparse_metadata["chunk_manifest_sha256"],
            "build_elapsed_seconds": sparse_metadata["build_elapsed_seconds"],
        },
    )
    synthetic_report = await execute_synthetic_bm25(
        persistence=_persistence(),
        project_id=args.project_id,
        dataset=load_dataset(args.synthetic_dataset),
        base_url=args.synthetic_base_url,
    )
    current = json.loads(args.current_evidence.read_text(encoding="utf-8"))
    complementarity = analyze_complementarity(current, bm25_report)
    evidence = build_bm25_candidate_evidence(bm25_report, complementarity)
    return bm25_report, synthetic_report, evidence


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """运行两套 BM25 evaluation 并写出 report/evidence."""
    args = _parser().parse_args()
    bm25_report, synthetic_report, evidence = asyncio.run(_run(args))
    _write(args.bm25_report_output, bm25_report)
    _write(args.synthetic_output, synthetic_report)
    _write(args.evidence_output, evidence)
    print(
        json.dumps(
            {
                "status": "PASS",
                "scifact_metrics": bm25_report["metrics"],
                "synthetic_metrics": synthetic_report["metrics"],
                "complementarity": evidence["complementarity"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
