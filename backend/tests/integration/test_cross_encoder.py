"""Cross-Encoder 集成边界：确定性 wiring（无真实模型）+ 真实模型 BLOCK/SKIP.

REAL_BENCHMARK_ALLOWED = NO（APPROVED_CROSS_ENCODER_MODEL_ASSET_NOT_PRESENT）。
本文件不生成任何真实 CE benchmark evidence；真实模型路径只做 precondition 保护的
BLOCK/SKIP，绝不把 SKIP 写成 PASS。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

from app.services.evaluation import cross_encoder as ce

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

# 真实模型资产路径（由项目批准后提供）；当前不存在 -> BLOCKED。
_CE_MODEL_PATH_ENV = os.getenv("LOCAL_AGENT_CE_MODEL_PATH", "").strip()
APPROVED_CE_MODEL_DIR = Path(_CE_MODEL_PATH_ENV) if _CE_MODEL_PATH_ENV else Path()
MODEL_ASSET_PRESENT = bool(_CE_MODEL_PATH_ENV) and APPROVED_CE_MODEL_DIR.is_dir()

_EXPECTED_MODEL_REF = "approved/cross-encoder@v1"
_EXPECTED_DIGEST = "a" * 64


def _expected() -> ce.CeExpectedConfig:
    return ce.CeExpectedConfig(model_ref=_EXPECTED_MODEL_REF, asset_tree_sha256=_EXPECTED_DIGEST)


def _ready_dense_cache() -> Path:
    root = BEIR_CACHE_ROOT / "scifact"
    for candidate in root.glob("*"):
        metadata_path = candidate / "cache_metadata.json"
        if not metadata_path.is_file():
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("cache_status") == "READY" and metadata.get("cache_schema_version") == "beir-scifact-dense-index-cache.v1":
            return candidate
    raise AssertionError("READY dense cache not found")


def _ready_sparse_cache() -> Path:
    root = BEIR_CACHE_ROOT / "scifact-bm25"
    for candidate in root.glob("*"):
        metadata_path = candidate / "cache_metadata.json"
        if not metadata_path.is_file():
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("cache_status") == "READY":
            return candidate
    raise AssertionError("READY BM25 cache not found")


# ---------------------------------------------------------------------------
# Part A：确定性 wiring，不依赖真实模型资产
# ---------------------------------------------------------------------------


def test_ce_runtime_fails_closed_when_approved_asset_missing(tmp_path: Path) -> None:
    """CE runtime 在资产缺失时启动即 fail closed（typed asset code，非 0 退出）."""
    dense_cache = _ready_dense_cache()
    missing = tmp_path / "missing-ce-asset"
    result = subprocess.run(
        [
            str(LOCAL_AGENT_PYTHON),
            "scripts/hybrid_rrf_evaluation_runtime.py",
            "--current-base-url",
            "http://127.0.0.1:1",
            "--bm25-base-url",
            "http://127.0.0.1:1",
            "--ce-model-ref",
            "approved/cross-encoder@v1",
            "--ce-model-path",
            str(missing),
            "--ce-asset-tree-sha256",
            "0" * 64,
            "--ce-max-length",
            "512",
            "--ce-dense-cache-dir",
            str(dense_cache),
            "--port",
            "0",
        ],
        cwd=LOCAL_AGENT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    stderr = result.stderr or ""
    assert result.returncode != 0
    assert "CROSS_ENCODER_ASSET_MISSING" in stderr


def _case(query_id: int, relevant_rank: int | None) -> dict[str, object]:
    relevant = f"relevant-{query_id}"
    ranking = [f"doc-{query_id}-{rank}" for rank in range(1, 9)]
    if relevant_rank is not None:
        ranking[relevant_rank - 1] = relevant
    return {
        "benchmark_query_id": str(query_id),
        "query": f"query {query_id}",
        "qrels_document_ids": [relevant],
        "ranked_document_ids": ranking,
        "ranked_chunk_ids": [[f"local-{query_id}", f"chunk-{query_id}"]],
        "retrieved_chunk_ids": [[f"local-{query_id}", f"chunk-{query_id}"]],
        "retrieved_document_ids": ranking,
        "scores": {"document_mrr": 0.0},
    }


def _sidecar_item(document_id: str, chunk_id: str) -> dict[str, object]:
    return {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "content_hash": "c" * 40,
        "resolved_text_sha256": "0" * 64,
        "pre_ce_rrf_rank": 1,
        "post_ce_rank": 1,
        "cross_encoder_score": 0.5,
    }


def test_consumer_plumbing_is_deterministic_and_honest_when_no_real_model(tmp_path: Path) -> None:
    """无真实模型时：对齐/分析/evidence/gate 全链路可用，gate 诚实 NOT_EVALUATED_BLOCKED."""
    rrf_report = {"case_results": [_case(qid, 1) for qid in range(1, 301)]}
    ce_report = {
        "benchmark_kind": "BEIR_SCIFACT_LOCALAGENT_ADAPTED",
        "metrics": {key: ce.RRF_SCIFACT_BASELINE[key] for key in ce.RRF_SCIFACT_BASELINE},
        "evaluated_retrieval_cases": 300,
        "case_results": [_case(qid, 1) for qid in range(1, 301)],
    }
    sidecar = []
    for qid in range(1, 301):
        query = f"query {qid}"
        row = {
            "schema_version": ce.CE_SIDE_CAR_SCHEMA_VERSION,
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "algorithm_ref": ce.CE_ALGORITHM_REF,
            "model_ref": _EXPECTED_MODEL_REF,
            "asset_tree_sha256": _EXPECTED_DIGEST,
            "device": "cpu",
            "cache_identity": ce.WP2_DENSE_CACHE_KEY,
            "candidate_count": 1,
            "status": "SUCCEEDED",
            "items": [_sidecar_item(f"local-{qid}", f"chunk-{qid}")],
            "latency_ms": {
                "model_load_latency_ms": None,  # 无真实模型 -> 无 cold load evidence
                "inference_latency_ms": 1.0,
                "ce_total_latency_ms": 1.5,
            },
        }
        sidecar.append(row)
    analysis = ce.analyze_ce_rerank(rrf_report, ce_report)
    aligned = ce.align_ce_provenance(ce_report, sidecar, expected=_expected())["aligned"]
    gate = ce.evaluate_ce_acceptance_gate(
        scifact_metrics=ce_report["metrics"],
        synthetic_metrics={key: ce.RRF_SYNTHETIC_BASELINE[key] for key in ce.RRF_SYNTHETIC_BASELINE},
        technical_failure_count=0,
        total_queries=300,
        case_guardrails_ok=analysis["case_guardrails_ok"],
        rank_transition_ok=analysis["rank_transition_ok"],
        invariants_ok=True,
        real_load_latency_present=False,  # 诚实：无真实模型加载证据
    )
    assert gate["outcome"] == "NOT_EVALUATED_BLOCKED"
    assert "real_model_load_latency_evidence_missing" in gate["blocked_reasons"]
    assert len(aligned) == 300


def test_runner_success_invariant_allows_accept_with_real_caches() -> None:
    """P1-01 runner-level：真实 cache/corpus 下 all_ok=True，完整 gate input 可达 ACCEPT."""
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from scripts.run_cross_encoder_evaluation import _invariants

    dense_cache = _ready_dense_cache()
    sparse_cache = _ready_sparse_cache()
    args = types.SimpleNamespace(
        dense_cache_metadata=dense_cache / "cache_metadata.json",
        sparse_cache_metadata=sparse_cache / "cache_metadata.json",
        beir_dataset_root=BEIR_ROOT,
    )
    report = {
        "evaluated_retrieval_cases": 300,
        "case_results": [_case(qid, 1) for qid in range(1, 301)],
    }
    synthetic_report = {"dataset_case_count": 24, "case_results": []}
    invariants = _invariants(args, report, synthetic_report, [], {})
    assert invariants["technical_failure_count"] == 0
    assert invariants["all_ok"] is True

    metrics = {key: ce.RRF_SCIFACT_BASELINE[key] + 0.011 for key in ce.RRF_SCIFACT_BASELINE}
    synthetic_metrics = {key: ce.RRF_SYNTHETIC_BASELINE[key] for key in ce.RRF_SYNTHETIC_BASELINE}
    gate = ce.evaluate_ce_acceptance_gate(
        scifact_metrics=metrics,
        synthetic_metrics=synthetic_metrics,
        technical_failure_count=0,
        total_queries=300,
        case_guardrails_ok=True,
        rank_transition_ok=True,
        invariants_ok=invariants["all_ok"],
        real_load_latency_present=True,
    )
    assert gate["outcome"] == "ACCEPT"


# ---------------------------------------------------------------------------
# Part B：真实模型路径（BLOCKED/SKIP；绝不生成 real evidence）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not MODEL_ASSET_PRESENT,
    reason="APPROVED_CROSS_ENCODER_MODEL_ASSET_NOT_PRESENT: real CE benchmark blocked",
)
def test_real_model_benchmark_is_blocked_without_approved_asset() -> None:
    """只有批准资产存在时才允许真实 benchmark；否则本测试如实 SKIP（不是 PASS）."""
    assert APPROVED_CE_MODEL_DIR.is_dir()
    assert (APPROVED_CE_MODEL_DIR / "config.json").is_file()


def test_integration_never_generates_fake_real_evidence(tmp_path: Path) -> None:
    """显式断言：无批准资产时不存在真实 CE result evidence 文件."""
    evidence_dir = AGENTEVALOPS_ROOT / ".ai" / "evidence" / "stage5_phase3_wp3"
    if MODEL_ASSET_PRESENT or not evidence_dir.is_dir():
        return
    for path in evidence_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload.get("truthfulness") != "REAL_CE_RESULT"