#!/usr/bin/env python
"""执行 Stage5 Phase3 WP0B BEIR SciFact current dense baseline 并输出 JSON."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from app.core.evaluation.beir_scifact import (
    BeirScifactAssetError,
    load_beir_scifact_asset,
)
from app.core.evaluation.document_metrics import DocumentProjection
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)
from app.services.evaluation.beir_scifact_baseline import execute_beir_scifact_baseline
from app.services.evaluation.persistence import EvaluationPersistenceService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--localagent-base-url", required=True)
    parser.add_argument(
        "--beir-dataset-root",
        type=Path,
        required=True,
        help="External read-only BEIR SciFact dataset directory (corpus.jsonl, queries.jsonl, qrels/)",
    )
    parser.add_argument(
        "--projection-manifest",
        type=Path,
        required=True,
        help="READY persistent BEIR SciFact cache 中的 manifest.json",
    )
    parser.add_argument(
        "--cache-metadata",
        type=Path,
        help="READY persistent cache 的 cache_metadata.json；默认与 manifest 同目录",
    )
    parser.add_argument("--output", type=Path)
    return parser


async def _run(args) -> dict[str, object]:
    asset = load_beir_scifact_asset(args.beir_dataset_root)
    manifest = json.loads(args.projection_manifest.read_text(encoding="utf-8"))
    metadata_path = args.cache_metadata or args.projection_manifest.parent / "cache_metadata.json"
    cache_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(cache_metadata, dict) or cache_metadata.get("cache_status") != "READY":
        raise ValueError("BEIR SciFact dense cache is not READY")
    required_cache_fields = ("cache_key", "manifest_sha256", "cache_schema_version")
    if any(not isinstance(cache_metadata.get(field), str) for field in required_cache_fields):
        raise ValueError("BEIR SciFact dense cache metadata is incomplete")
    projection = DocumentProjection.from_manifest(manifest)
    persistence = EvaluationPersistenceService(
        lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
    )
    return await execute_beir_scifact_baseline(
        persistence=persistence,
        project_id=args.project_id,
        asset=asset,
        base_url=args.localagent_base_url,
        document_projection=projection,
        dense_index_cache={
            "identity": cache_metadata["cache_key"],
            "status": "CACHE_HIT",
            "manifest_sha256": cache_metadata["manifest_sha256"],
            "schema_version": cache_metadata["cache_schema_version"],
        },
    )


def main() -> int:
    """运行 CLI 并可选保存 JSON report."""
    args = _parser().parse_args()
    try:
        report = asyncio.run(_run(args))
    except BeirScifactAssetError as error:
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}))
        return 2
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
