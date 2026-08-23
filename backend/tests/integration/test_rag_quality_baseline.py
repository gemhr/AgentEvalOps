"""真实 Qwen + fresh Chroma + LocalAgent HTTP + PostgreSQL RAG baseline."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.core.evaluation.dataset import load_dataset
from app.infrastructure.db.engine import async_session_factory
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    PostgresEvaluationPersistenceUnitOfWork,
)
from app.services.evaluation.persistence import EvaluationPersistenceService
from app.services.evaluation.rag_baseline import execute_rag_quality_baseline
from tests.integration.conftest import TEST_PROJECT_ID

LOCAL_AGENT = Path(r"D:\PythonProject\Local_Agent")
LOCAL_AGENT_PYTHON = LOCAL_AGENT / ".venv" / "Scripts" / "python.exe"
DATASET = (
    Path(__file__).resolve().parents[2]
    / "evaluation_assets"
    / "rag_quality_v1"
    / "rag_evaluation_dataset.v1.json"
)


async def _wait_for_tcp(port: int, process: subprocess.Popen[bytes]) -> None:
    for _ in range(240):
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise RuntimeError(f"LocalAgent evaluation server exited: {stderr}")
        with socket.socket() as probe:
            probe.settimeout(0.05)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        await asyncio.sleep(0.25)
    raise TimeoutError("LocalAgent evaluation server did not open its TCP port")


@asynccontextmanager
async def _localagent_server(tmp_path: Path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    persist_dir = tmp_path / "evaluation-chroma"
    manifest_path = tmp_path / "corpus-manifest.json"
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
            "scripts/rag_evaluation_runtime.py",
            "serve",
            "--persist-dir",
            str(persist_dir),
            "--manifest-out",
            str(manifest_path),
            "--port",
            str(port),
        ],
        cwd=LOCAL_AGENT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    await _wait_for_tcp(port, process)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        yield f"http://127.0.0.1:{port}", manifest
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


async def _remove_evaluation_dir(path: Path) -> None:
    for _ in range(20):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            await asyncio.sleep(0.25)
    shutil.rmtree(path)


@pytest.mark.asyncio
async def test_full_rag_quality_baseline_real_execution(tmp_path: Path) -> None:
    dataset = load_dataset(DATASET)
    build_a = tmp_path / "build-a"
    async with _localagent_server(build_a) as (base_url, manifest):
        available = {
            (item["document_id"], item["chunk_id"]) for item in manifest["chunks"]
        }
        expected = {
            (chunk.document_id, chunk.chunk_id)
            for case in dataset.cases
            if case.ground_truth.retrieval is not None
            for chunk in case.ground_truth.retrieval.relevant_chunks
        }
        assert expected <= available
        assert manifest["document_count"] == 15
        assert manifest["chunk_count"] == 60
        assert manifest["embedding_dimension"] == 1024

        report = await execute_rag_quality_baseline(
            persistence=_persistence(),
            project_id=TEST_PROJECT_ID,
            dataset=dataset,
            base_url=base_url,
        )

    manifest_identity = [
        (item["document_id"], item["chunk_id"], item["content_hash"])
        for item in manifest["chunks"]
    ]
    selected_case_ids = {
        "exact-keyword-outcome-unknown",
        "semantic-recovery-read-only",
        "short-outputgate",
    }
    selected_results = {
        item["case_id"]: (
            item["retrieved_ids"],
            item["ranked_ids"],
            item["selected_ids"],
        )
        for item in report["case_results"]
        if item["case_id"] in selected_case_ids
    }
    await _remove_evaluation_dir(build_a)

    build_b = tmp_path / "build-b"
    async with _localagent_server(build_b) as (base_url, rebuilt_manifest):
        rebuilt_report = await execute_rag_quality_baseline(
            persistence=_persistence(),
            project_id=TEST_PROJECT_ID,
            dataset=dataset,
            base_url=base_url,
        )
    rebuilt_identity = [
        (item["document_id"], item["chunk_id"], item["content_hash"])
        for item in rebuilt_manifest["chunks"]
    ]
    rebuilt_selected_results = {
        item["case_id"]: (
            item["retrieved_ids"],
            item["ranked_ids"],
            item["selected_ids"],
        )
        for item in rebuilt_report["case_results"]
        if item["case_id"] in selected_case_ids
    }
    assert manifest_identity == rebuilt_identity
    assert selected_results == rebuilt_selected_results

    assert report["dataset_case_count"] == 24
    assert report["evaluated_retrieval_cases"] == 20
    assert set(report["metrics"]) == {
        "recall_at_1",
        "recall_at_3",
        "recall_at_5",
        "mrr",
        "ndcg_at_3",
        "ndcg_at_5",
    }
    assert len(report["case_results"]) == 24
    assert sum(report["outcomes"].values()) == 24
    assert report["latency_ms"]["retrieval"]["mean"] >= 0
    output = os.getenv("RAG_BASELINE_OUTPUT")
    if output:
        Path(output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print("RAG_BASELINE_RESULT=" + json.dumps(report, ensure_ascii=False, sort_keys=True))
