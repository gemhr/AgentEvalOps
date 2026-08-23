"""真实 Qwen + fresh Chroma + LocalAgent HTTP + PostgreSQL 的 BEIR SciFact baseline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.core.evaluation.beir_scifact import (
    FROZEN_ZIP_MD5,
    load_beir_scifact_asset,
)
from app.core.evaluation.document_metrics import DocumentProjection
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)
from app.services.evaluation.beir_scifact_baseline import execute_beir_scifact_baseline
from app.services.evaluation.persistence import EvaluationPersistenceService
from tests.integration.conftest import TEST_PROJECT_ID

LOCAL_AGENT = Path(r"D:\PythonProject\Local_Agent")
LOCAL_AGENT_PYTHON = LOCAL_AGENT / ".venv" / "Scripts" / "python.exe"
BEIR_SCIFACT_ROOT = Path(r"D:\PythonProject\_external\beir\datasets\scifact")
BEIR_SCIFACT_ZIP = Path(r"D:\PythonProject\_external\beir\datasets\scifact.zip")

pytestmark = pytest.mark.skipif(
    not LOCAL_AGENT.is_dir() or not BEIR_SCIFACT_ROOT.is_dir(),
    reason="local LocalAgent repo or external BEIR SciFact asset not available",
)


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


async def _wait_for_tcp(port: int, process: subprocess.Popen[bytes], timeout_s: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise RuntimeError(f"LocalAgent BEIR server exited: {stderr}")
        with socket.socket() as probe:
            probe.settimeout(0.05)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        await asyncio.sleep(0.25)
    raise TimeoutError("LocalAgent BEIR server did not open its TCP port")


@asynccontextmanager
async def _beir_server(cache_dir: Path, port: int):
    environment = {
        **os.environ,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    }
    process = subprocess.Popen(
        [
            str(LOCAL_AGENT_PYTHON),
            "scripts/beir_scifact_runtime.py",
            "serve",
            "--cache-dir",
            str(cache_dir),
            "--port",
            str(port),
        ],
        cwd=LOCAL_AGENT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    await _wait_for_tcp(port, process, timeout_s=300.0)
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


@pytest.mark.asyncio
async def test_full_beir_scifact_baseline_real_execution(tmp_path: Path) -> None:
    # Asset validation：冻结 ZIP MD5 与三个文件的 SHA-256。
    assert BEIR_SCIFACT_ZIP.is_file(), "BEIR SciFact dataset zip is missing"
    assert _md5(BEIR_SCIFACT_ZIP) == FROZEN_ZIP_MD5
    asset = load_beir_scifact_asset(BEIR_SCIFACT_ROOT, verify_checksums=True)

    assert asset.statistics["corpus_document_count"] == 5183
    assert asset.statistics["test_query_count"] == 300
    assert asset.statistics["qrels_rows"] == 339
    assert asset.statistics["relevance_score_distribution"] == {1: 339}

    # BEIR_SCIFACT_PREBUILT_DIR 指向已完成的 cache identity 目录；未设置时显式 cold build。
    prebuilt = os.getenv("BEIR_SCIFACT_PREBUILT_DIR")
    if prebuilt:
        cache_dir = Path(prebuilt)
    else:
        build = subprocess.run(
            [
                str(LOCAL_AGENT_PYTHON),
                "scripts/beir_scifact_runtime.py",
                "build",
                "--beir-corpus",
                str(BEIR_SCIFACT_ROOT / "corpus.jsonl"),
                "--cache-root",
                str(tmp_path / "cache"),
            ],
            cwd=LOCAL_AGENT,
            env={
                **os.environ,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            },
            capture_output=True,
            timeout=7200,
        )
        assert build.returncode == 0, build.stderr[-4000:]
        cache_dir = Path(json.loads(build.stdout)["cache_dir"])
    manifest_path = cache_dir / "manifest.json"
    cache_metadata = json.loads((cache_dir / "cache_metadata.json").read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["corpus_id"] == "beir-scifact-corpus.v1"
    assert manifest["collection_name"] == "beir_scifact_eval_v1"
    assert manifest["embedding_dimension"] == 1024
    assert manifest["document_count"] == 5183
    assert manifest["chunk_count"] > 0

    projection = DocumentProjection.from_manifest(manifest)
    projected_benchmark_ids = {
        entry["benchmark_document_id"] for entry in manifest["documents"]
    }
    qrels_document_ids = {doc for docs in asset.qrels.values() for doc in docs}
    assert qrels_document_ids <= projected_benchmark_ids

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    async with _beir_server(cache_dir, port) as base_url:
        report = await execute_beir_scifact_baseline(
            persistence=_persistence(),
            project_id=TEST_PROJECT_ID,
            asset=asset,
            base_url=base_url,
            document_projection=projection,
            dense_index_cache={
                "identity": cache_metadata["cache_key"],
                "status": "CACHE_HIT",
                "manifest_sha256": cache_metadata["manifest_sha256"],
            },
        )

    assert report["dataset_case_count"] == 300
    assert report["evaluated_retrieval_cases"] == 300
    assert set(report["metrics"]) == {
        "document_recall_at_1",
        "document_recall_at_3",
        "document_recall_at_5",
        "document_mrr",
        "document_ndcg_at_3",
        "document_ndcg_at_5",
    }
    assert len(report["case_results"]) == 300
    assert sum(report["outcomes"].values()) == 300
    assert report["latency_ms"]["retrieval"]["mean"] >= 0
    assert report["document_projection"]["mapped_documents"] == manifest["document_count"]
    assert report["dense_index_cache"]["status"] == "CACHE_HIT"

    output = os.getenv("BEIR_SCIFACT_BASELINE_OUTPUT")
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print("BEIR_SCIFACT_BASELINE_RESULT=" + json.dumps(report["metrics"], sort_keys=True))
