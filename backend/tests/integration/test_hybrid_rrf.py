"""真实 SciFact 300-query 与 synthetic 24-case Hybrid RRF evaluation."""

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
from app.services.evaluation.hybrid_rrf import (
    align_provenance,
    analyze_hybrid_rrf,
    build_rrf_candidate_evidence,
    execute_beir_scifact_hybrid_rrf,
    execute_synthetic_hybrid_rrf,
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
    candidates = list(root.glob("*"))
    for candidate in candidates:
        metadata_path = candidate / "cache_metadata.json"
        if not metadata_path.is_file():
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("cache_status") == "READY" and metadata.get("cache_schema_version") == schema:
            return candidate
    raise AssertionError(f"READY cache not found for {schema}")


async def _wait_for_tcp(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = asyncio.get_running_loop().time() + 600
    while asyncio.get_running_loop().time() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise RuntimeError(f"LocalAgent runtime exited: {stderr}")
        with socket.socket() as probe:
            probe.settimeout(0.05)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        await asyncio.sleep(0.2)
    raise TimeoutError("LocalAgent runtime did not open its TCP port")


@asynccontextmanager
async def _runtime(script: str, *arguments: str):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env = {
        **os.environ,
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }
    process = subprocess.Popen(
        [str(LOCAL_AGENT_PYTHON), script, *arguments, "--port", str(port)],
        cwd=LOCAL_AGENT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    await _wait_for_tcp(port, process)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=20)


def _persistence() -> EvaluationPersistenceService:
    return EvaluationPersistenceService(
        lambda: PostgresEvaluationPersistenceUnitOfWork(async_session_factory)
    )


def _metadata(path: Path, *, dense: bool) -> dict[str, object]:
    value = json.loads((path / "cache_metadata.json").read_text(encoding="utf-8"))
    return {
        "identity": value["cache_key"],
        "status": "CACHE_HIT",
        "schema_version": value["cache_schema_version"],
        "chunk_manifest_sha256": value.get(
            "chunk_manifest_sha256", value.get("manifest_sha256")
        ),
        "embedding_rebuild": "NO" if dense else None,
    }


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


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
async def test_hybrid_rrf_scifact_and_synthetic_real_execution(tmp_path: Path) -> None:
    dense_cache = _ready_cache(
        BEIR_CACHE_ROOT / "scifact", "beir-scifact-dense-index-cache.v1"
    )
    sparse_cache = _ready_cache(
        BEIR_CACHE_ROOT / "scifact-bm25", "beir-scifact-bm25-index-cache.v1"
    )
    projection = DocumentProjection.from_manifest(
        json.loads((dense_cache / "manifest.json").read_text(encoding="utf-8"))
    )
    scifact_sidecar = tmp_path / "scifact-provenance.jsonl"
    async with _runtime(
        "scripts/beir_scifact_runtime.py", "serve", "--cache-dir", str(dense_cache)
    ) as current_url, _runtime(
        "scripts/bm25_evaluation_runtime.py", "serve-scifact", "--cache-dir", str(sparse_cache)
    ) as bm25_url, _runtime(
        "scripts/hybrid_rrf_evaluation_runtime.py",
        "--current-base-url",
        current_url,
        "--bm25-base-url",
        bm25_url,
        "--provenance-out",
        str(scifact_sidecar),
    ) as hybrid_url:
        rrf_report = await execute_beir_scifact_hybrid_rrf(
            persistence=_persistence(),
            project_id=TEST_PROJECT_ID,
            asset=load_beir_scifact_asset(BEIR_ROOT),
            base_url=hybrid_url,
            document_projection=projection,
            dense_index_cache=_metadata(dense_cache, dense=True),
            sparse_index_cache=_metadata(sparse_cache, dense=False),
        )

    synthetic_sidecar = tmp_path / "synthetic-provenance.jsonl"
    synthetic_persist = tmp_path / "synthetic-chroma"
    async with _runtime(
        "scripts/rag_evaluation_runtime.py", "serve", "--persist-dir", str(synthetic_persist)
    ) as current_url, _runtime(
        "scripts/bm25_evaluation_runtime.py", "serve-synthetic"
    ) as bm25_url, _runtime(
        "scripts/hybrid_rrf_evaluation_runtime.py",
        "--current-base-url",
        current_url,
        "--bm25-base-url",
        bm25_url,
        "--provenance-out",
        str(synthetic_sidecar),
    ) as hybrid_url:
        dataset_path = (
            AGENTEVALOPS_ROOT
            / "backend/evaluation_assets/rag_quality_v1/rag_evaluation_dataset.v1.json"
        )
        synthetic_report = await execute_synthetic_hybrid_rrf(
            persistence=_persistence(),
            project_id=TEST_PROJECT_ID,
            dataset=load_dataset(dataset_path),
            base_url=hybrid_url,
        )

    current = json.loads(
        (
            AGENTEVALOPS_ROOT
            / ".ai/evidence/stage5_phase3_wp0b/beir_scifact_current_dense_baseline_v1.json"
        ).read_text(encoding="utf-8")
    )
    bm25 = json.loads(
        (
            AGENTEVALOPS_ROOT
            / ".ai/evidence/stage5_phase3_wp1/beir_scifact_bm25_report_v1.json"
        ).read_text(encoding="utf-8")
    )
    analysis = analyze_hybrid_rrf(current, bm25, rrf_report)
    provenance = align_provenance(rrf_report, _jsonl(scifact_sidecar))
    evidence = build_rrf_candidate_evidence(rrf_report, analysis, provenance)

    assert rrf_report["dataset_case_count"] == 300
    assert rrf_report["evaluated_retrieval_cases"] == 300
    assert len(rrf_report["case_results"]) == 300
    assert len(evidence["cases"]) == 300
    assert evidence["rrf_k"] == 60
    assert evidence["dense_index_cache"]["status"] == "CACHE_HIT"
    assert evidence["dense_index_cache"]["embedding_rebuild"] == "NO"
    assert evidence["sparse_index_cache"]["status"] == "CACHE_HIT"
    assert synthetic_report["dataset_case_count"] == 24
    assert synthetic_report["evaluated_retrieval_cases"] == 20
    assert len(_jsonl(synthetic_sidecar)) == 24

    _write_from_env("RRF_SCIFACT_REPORT_OUTPUT", rrf_report)
    _write_from_env("RRF_SYNTHETIC_REPORT_OUTPUT", synthetic_report)
    _write_from_env("RRF_EVIDENCE_OUTPUT", evidence)
