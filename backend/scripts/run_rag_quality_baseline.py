#!/usr/bin/env python
"""执行 Stage5 Phase3 RAG quality baseline 并输出 JSON."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from app.core.evaluation.dataset import load_dataset
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)
from app.services.evaluation.persistence import EvaluationPersistenceService
from app.services.evaluation.rag_baseline import execute_rag_quality_baseline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--localagent-base-url", required=True)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("evaluation_assets/rag_quality_v1/rag_evaluation_dataset.v1.json"),
    )
    parser.add_argument("--output", type=Path)
    return parser


async def _run(args) -> dict[str, object]:
    persistence = EvaluationPersistenceService(
        lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
    )
    return await execute_rag_quality_baseline(
        persistence=persistence,
        project_id=args.project_id,
        dataset=load_dataset(args.dataset),
        base_url=args.localagent_base_url,
    )


def main() -> int:
    """运行 CLI 并可选保存 JSON report."""
    args = _parser().parse_args()
    report = asyncio.run(_run(args))
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
