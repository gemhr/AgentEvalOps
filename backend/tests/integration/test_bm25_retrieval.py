"""真实 SciFact 300-query 与 synthetic BM25-only evaluation."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

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
from tests.integration.conftest import TEST_PROJECT_ID

AGENTEVALOPS_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_ROOT = AGENTEVALOPS_ROOT.parent
LOCAL_AGENT = Path(os.getenv("LOCAL_AGENT_REPO", WORKSPACE_ROOT / "Local_Agent"))
LOCAL_AGENT_PYTHON = LOCAL_AGENT / ".venv" / "Scripts" / "python.exe"
BEIR_ROOT = Path(
    os.getenv(
        "BEIR_SCIFACT_ROOT",
        WORKSPACE_ROOT / "_external" / "beir" / "datasets" / "scifact",
    )
)
BEIR_CACHE_ROOT = BEIR_ROOT.parents[1] / "cache"

pytestmark = pytest.mark.skipif(
    not LOCAL_AGENT_PYTHON.is_file() or not BEIR_ROOT.is_dir(),
    reason="local LocalAgent or external SciFact asset unavailable",
)


def _ready_cache(root: Path, schema: str) -> Path:
    override = os.getenv(
        "BEIR_SCIFACT_BM25_CACHE_DIR"
        if "bm25" in schema
        else "BEIR_SCIFACT_PREBUILT_DIR"
    )
    candidates = [Path(override)] if override else list(root.glob("*"))
    for candidate in candidates:
        metadata_path = candidate / "cache_metadata.json"
        if not metadata_path.is_file():
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("cache_status") == "READY" and metadata.get("cache_schema_version") == schema:
            return candidate
    raise AssertionError(f"READY cache not found for {schema}")


async def _wait_for_tcp(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = asyncio.get_running_loop().time() + 120
    while asyncio.get_running_loop().time() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise RuntimeError(f"BM25 runtime exited: {stderr}")
        with socket.socket() as probe:
            probe.settimeout(0.05)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        await asyncio.sleep(0.2)
    raise TimeoutError("BM25 runtime did not open its TCP port")


@asynccontextmanager
async def _runtime(*arguments: str):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen(
        [
            str(LOCAL_AGENT_PYTHON),
            "scripts/bm25_evaluation_runtime.py",
            *arguments,
            "--port",
            str(port),
        ],
        cwd=LOCAL_AGENT,
        env={
            **os.environ,
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    await _wait_for_tcp(port, process)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _persistence() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(
        lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
    )


def _write_from_env(name: str, payload: object) -> None:
    output = os.getenv(name)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_bm25_scifact_and_synthetic_real_execution() -> None:
    dense_cache = _ready_cache(
        BEIR_CACHE_ROOT / "scifact",
        "beir-scifact-dense-index-cache.v1",
    )
    sparse_cache = _ready_cache(
        BEIR_CACHE_ROOT / "scifact-bm25",
        "beir-scifact-bm25-index-cache.v1",
    )
    sparse_metadata = json.loads(
        (sparse_cache / "cache_metadata.json").read_text(encoding="utf-8")
    )
    projection = DocumentProjection.from_manifest(
        json.loads((dense_cache / "manifest.json").read_text(encoding="utf-8"))
    )

    async with _runtime("serve-scifact", "--cache-dir", str(sparse_cache)) as base_url:
        bm25_report = await execute_beir_scifact_bm25(
            persistence=_persistence(),
            project_id=TEST_PROJECT_ID,
            asset=load_beir_scifact_asset(BEIR_ROOT),
            base_url=base_url,
            document_projection=projection,
            sparse_index_cache={
                "identity": sparse_metadata["cache_key"],
                "status": "CACHE_HIT",
                "schema_version": sparse_metadata["cache_schema_version"],
                "chunk_manifest_sha256": sparse_metadata["chunk_manifest_sha256"],
                "build_elapsed_seconds": sparse_metadata["build_elapsed_seconds"],
            },
        )

    dataset_path = AGENTEVALOPS_ROOT / "backend/evaluation_assets/rag_quality_v1/rag_evaluation_dataset.v1.json"
    async with _runtime("serve-synthetic") as base_url:
        synthetic_report = await execute_synthetic_bm25(
            persistence=_persistence(),
            project_id=TEST_PROJECT_ID,
            dataset=load_dataset(dataset_path),
            base_url=base_url,
        )

    current = json.loads(
        (
            AGENTEVALOPS_ROOT
            / ".ai/evidence/stage5_phase3_wp0b/beir_scifact_current_dense_baseline_v1.json"
        ).read_text(encoding="utf-8")
    )
    complementarity = analyze_complementarity(current, bm25_report)
    evidence = build_bm25_candidate_evidence(bm25_report, complementarity)

    assert bm25_report["dataset_case_count"] == 300
    assert bm25_report["evaluated_retrieval_cases"] == 300
    assert len(bm25_report["case_results"]) == 300
    assert set(bm25_report["metrics"]) == {
        "document_recall_at_1",
        "document_recall_at_3",
        "document_recall_at_5",
        "document_mrr",
        "document_ndcg_at_3",
        "document_ndcg_at_5",
    }
    assert complementarity["current_dense_miss_count"] == 60
    assert synthetic_report["dataset_case_count"] == 24
    assert synthetic_report["evaluated_retrieval_cases"] == 20
    assert len(evidence["cases"]) == 300

    _write_from_env("BM25_SCIFACT_REPORT_OUTPUT", bm25_report)
    _write_from_env("BM25_SYNTHETIC_REPORT_OUTPUT", synthetic_report)
    _write_from_env("BM25_EVIDENCE_OUTPUT", evidence)
