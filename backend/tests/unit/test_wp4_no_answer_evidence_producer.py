"""WP4 thin evidence producer focused tests（DETERMINISTIC_TEST_ONLY，非真实 benchmark）。"""

# ruff: noqa: D415

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import socket
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.evaluation.dataset import EvaluationDataset, load_dataset
from app.core.evaluation.no_answer import (
    RrfEvidenceEnvelopeV2,
    WP4_BM25_CACHE_IDENTITY,
    WP4_CHUNK_MANIFEST_DIGEST,
    WP4_DENSE_CACHE_IDENTITY,
    WP4_RRF_SUBSTRATE_REF,
    WP4_SOURCE_MANIFEST_DIGEST,
)

PRODUCER = (
    Path(__file__).resolve().parents[2] / "scripts/generate_wp4_no_answer_rrf_evidence.py"
)
ASSET = (
    Path(__file__).resolve().parents[2]
    / "evaluation_assets/no_answer_threshold_v2/no_answer_threshold_dataset.v2.json"
)
CURRENT = "current-dense-led-ranked.v1"
BM25 = "bm25-lucene-idf.v1"


def _real_evidence_dir() -> Path | None:
    candidate = (
        Path(__file__).resolve().parents[3].parent
        / "Local_Agent/.ai/evidence/stage5_phase3_wp4_substrate_phase_a"
    )
    return candidate if candidate.is_dir() else None


@pytest.fixture()
def ready_dir(tmp_path: Path):
    source = _real_evidence_dir()
    if source is None:
        pytest.skip("LocalAgent Phase A READY evidence not present (read-only substrate authority)")
    destination = tmp_path / "substrate"
    shutil.copytree(source, destination)
    return destination


def _load_producer():
    spec = importlib.util.spec_from_file_location("wp4_evidence_producer", PRODUCER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


def _rrf_score(*, current_rank: int | None = None, bm25_rank: int | None = None) -> float:
    return sum(1.0 / (60 + rank) for rank in (current_rank, bm25_rank) if rank is not None)


def _row(case_id: str, channels_by_rank: tuple[tuple[str, ...], ...]) -> dict:
    """构造 LocalAgent RRF provenance row shape（不含 run_id/query_sha256，由 bundle 注入）。"""
    fused = []
    current_ranking = []
    bm25_ranking = []
    for rank, channels in enumerate(channels_by_rank, start=1):
        document_id = f"doc-{case_id}-{rank}"
        chunk_id = f"chunk-{case_id}-{rank}"
        current_rank = rank if CURRENT in channels else None
        bm25_rank = rank if BM25 in channels else None
        fused.append(
            {
                "document_id": document_id,
                "chunk_id": chunk_id,
                "current_rank": current_rank,
                "bm25_rank": bm25_rank,
                "rrf_score": _rrf_score(current_rank=current_rank, bm25_rank=bm25_rank),
                "rrf_rank": rank,
                "source_channels": list(channels),
                "contributing_channel_count": len(channels),
            }
        )
        if current_rank is not None:
            current_ranking.append([document_id, chunk_id])
        if bm25_rank is not None:
            bm25_ranking.append([document_id, chunk_id])
    return {
        "algorithm_ref": "rrf.v1",
        "rrf_k": 60,
        "current_status": "SUCCEEDED" if current_ranking else "EMPTY",
        "bm25_status": "SUCCEEDED" if bm25_ranking else "EMPTY",
        "current_chunk_ranking": current_ranking,
        "bm25_chunk_ranking": bm25_ranking,
        "fused_items": fused,
    }


def _bundle(
    producer,
    case_id: str,
    query: str,
    channels_by_rank: tuple[tuple[str, ...], ...],
    *,
    run_id: str = "run-x",
):
    """构造同一 run 的 RuntimeRetrievalResult（artifact + provenance row）。"""
    row = _row(case_id, channels_by_rank)
    fused = row["fused_items"]
    items = [
        {
            "document_id": item["document_id"],
            "chunk_id": item["chunk_id"],
            "rank": item["rrf_rank"],
            "retrieval_score": item["rrf_score"],
            "retrieval_channels": list(item["source_channels"]),
        }
        for item in fused
    ]
    artifact = {
        "schema_version": "rag-evaluation-artifact.v1",
        "artifact_id": f"rag-eval://{run_id}/hybrid-rrf-1",
        "run_id": run_id,
        "attempt_id": run_id,
        "retrieval_id": "hybrid-rrf-1",
        "invocation_index": 1,
        "retrieval_status": "SUCCEEDED" if fused else "EMPTY",
        "query": query,
        "rewritten_query": query,
        "retrieved_items": items,
        "ranked_items": items,
        "selected_items": [],
        "citations": [],
    }
    row = {
        **row,
        "run_id": run_id,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
    }
    return producer.RuntimeRetrievalResult(result_artifact=artifact, provenance_row=row)


class FakeRuntimeProvider:
    """返回真实-shape LocalAgent artifact + provenance bundle；不执行任何 retrieval。"""

    def __init__(
        self,
        producer,
        bundles_by_case: dict[str, object],
        *,
        record_queries: list | None = None,
    ) -> None:
        self._producer = producer
        self._bundles = bundles_by_case
        self._record_queries = record_queries if record_queries is not None else []

    def retrieve(self, case_id: str, query: str, *, run_id: str):
        """记录查询并返回与 case 对应的同一 run bundle。"""
        self._record_queries.append((case_id, query))
        bundle = self._bundles[case_id]
        row = {**bundle.provenance_row, "run_id": run_id,
               "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest()}
        artifact = {
            **bundle.result_artifact,
            "run_id": run_id,
            "query": query,
            "artifact_id": f"rag-eval://{run_id}/hybrid-rrf-1",
        }
        return self._producer.RuntimeRetrievalResult(result_artifact=artifact, provenance_row=row)


def test_validate_ready_substrate_accepts_real_phase_a_evidence(ready_dir: Path) -> None:
    producer = _load_producer()
    facts = producer.validate_ready_substrate(ready_dir)
    assert facts.dense_cache_identity == WP4_DENSE_CACHE_IDENTITY
    assert facts.bm25_cache_identity == WP4_BM25_CACHE_IDENTITY
    assert facts.source_manifest_digest == WP4_SOURCE_MANIFEST_DIGEST
    assert facts.chunk_manifest_digest == WP4_CHUNK_MANIFEST_DIGEST
    assert facts.corpus_id == "rag-evaluation-corpus.v1"
    assert facts.document_count == 15 and facts.chunk_count == 60


@pytest.mark.parametrize(
    "mutator",
    [
        lambda meta: meta.__setitem__("cache_key", "0" * 64),
        lambda meta: meta.__setitem__("cache_status", "BUILDING"),
        lambda meta: meta.__setitem__("corpus_id", "other-corpus"),
        lambda meta: meta.__setitem__("chunk_manifest_sha256", "0" * 64),
        lambda meta: meta.__setitem__("source_manifest_sha256", "0" * 64),
        lambda meta: meta.__setitem__("document_count", 14),
        lambda meta: meta.__setitem__("chunk_count", 59),
    ],
)
def test_validate_ready_substrate_fails_closed_on_dense_mismatch(ready_dir: Path, mutator) -> None:
    producer = _load_producer()
    metadata = json.loads((ready_dir / "dense_cache_metadata.json").read_text(encoding="utf-8"))
    mutator(metadata)
    (ready_dir / "dense_cache_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(RuntimeError, match="SUBSTRATE_NOT_READY"):
        producer.validate_ready_substrate(ready_dir)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda meta: meta.__setitem__("cache_key", "0" * 64),
        lambda meta: meta.__setitem__("corpus_id", "other-corpus"),
        lambda meta: meta.__setitem__("chunk_manifest_sha256", "0" * 64),
    ],
)
def test_validate_ready_substrate_fails_closed_on_bm25_mismatch(ready_dir: Path, mutator) -> None:
    producer = _load_producer()
    metadata = json.loads((ready_dir / "bm25_cache_metadata.json").read_text(encoding="utf-8"))
    mutator(metadata)
    (ready_dir / "bm25_cache_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(RuntimeError, match="SUBSTRATE_NOT_READY"):
        producer.validate_ready_substrate(ready_dir)


def test_validate_ready_substrate_fails_closed_on_manifest_mismatch(ready_dir: Path) -> None:
    producer = _load_producer()
    dense_chunks = json.loads((ready_dir / "dense_chunk_manifest.json").read_text(encoding="utf-8"))
    dense_chunks["chunks"] = dense_chunks["chunks"][:-1] + [
        {
            "chunk_id": "mutated",
            "content_hash": "mutated",
            "document_id": "mutated",
            "section_path": "mutated",
            "source": "mutated.md",
        }
    ]
    (ready_dir / "dense_chunk_manifest.json").write_text(json.dumps(dense_chunks), encoding="utf-8")
    with pytest.raises(RuntimeError, match="SUBSTRATE_NOT_READY"):
        producer.validate_ready_substrate(ready_dir)


def test_validate_ready_substrate_fails_closed_on_missing_metadata(tmp_path: Path) -> None:
    producer = _load_producer()
    with pytest.raises(OSError):
        producer.validate_ready_substrate(tmp_path / "missing")


def test_producer_valid_overall_artifact_projection_exact(ready_dir: Path) -> None:
    """合法 artifact + provenance 的 exact 投影：status/counts 来自 artifact，序列/rank/score 来自 row。"""
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT, BM25), (BM25,)))
    case = producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)
    assert case["retrieval_status"] == result.result_artifact["retrieval_status"] == "SUCCEEDED"
    assert case["retrieved_candidate_count"] == len(result.result_artifact["retrieved_items"]) == 2
    assert case["ranked_candidate_count"] == len(result.result_artifact["ranked_items"]) == 2
    assert case["retrieval_artifact_id"] == result.result_artifact["artifact_id"]
    assert [item["rank"] for item in case["ranked_candidates"]] == [1, 2]
    assert [item["rrf_score"] for item in case["ranked_candidates"]] == [
        _rrf_score(current_rank=1, bm25_rank=1),
        _rrf_score(bm25_rank=2),
    ]
    assert case["ranked_candidates"][0]["current_rank"] == 1
    assert case["ranked_candidates"][0]["bm25_rank"] == 1
    assert case["ranked_candidates"][0]["source_channels"] == [CURRENT, BM25]


def test_producer_projects_empty_status_without_candidates(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-empty", "query", ())
    case = producer.project_retrieval_result_to_case(case_id="case-empty", query="query", result=result)
    assert case["retrieval_status"] == "EMPTY"
    assert case["ranked_candidate_count"] == 0 and case["ranked_candidates"] == []
    assert case["retrieved_candidate_count"] == 0


def test_producer_fails_closed_on_query_digest_mismatch(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT,),))
    result.provenance_row["query_sha256"] = hashlib.sha256("other".encode()).hexdigest()
    with pytest.raises(RuntimeError, match="QUERY_DIGEST_MISMATCH"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_cross_run_binding(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT,),))
    result.provenance_row["run_id"] = "other-run"
    with pytest.raises(RuntimeError, match="ARTIFACT_PROVENANCE_RUN_MISMATCH"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_missing_artifact_query(ready_dir: Path) -> None:
    """缺失 result_artifact.query 必须 FAIL CLOSED（不得因非 str 绕过 digest 绑定）。"""
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT,),))
    del result.result_artifact["query"]
    with pytest.raises(RuntimeError, match="ARTIFACT_QUERY_MISSING"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_missing_artifact_id(ready_dir: Path) -> None:
    """缺失 result_artifact.artifact_id 必须 FAIL CLOSED（不得合成 rrf-artifact-{run_id} fallback）。"""
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT,),))
    del result.result_artifact["artifact_id"]
    with pytest.raises(RuntimeError, match="ARTIFACT_ID_MISSING"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_missing_runtime_artifact(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT,),))
    del result.provenance_row["fused_items"]
    with pytest.raises(RuntimeError, match="FUSED_ITEMS"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


@pytest.mark.parametrize(
    "mutator,error",
    [
        (lambda row: row.__setitem__("algorithm_ref", "other"), "ALGORITHM_MISMATCH"),
        (lambda row: row.__setitem__("rrf_k", 999), "RRF_K_MISMATCH"),
        (lambda row: row.__setitem__("current_status", "FAILED"), "CHANNEL_TECHNICAL_FAILURE"),
        (lambda row: row.__setitem__("bm25_status", "TIMED_OUT"), "CHANNEL_TECHNICAL_FAILURE"),
    ],
)
def test_producer_fails_closed_on_wrong_runtime_algorithm_k_and_status(
    ready_dir: Path, mutator, error: str
) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT, BM25),))
    mutator(result.provenance_row)
    with pytest.raises(RuntimeError, match=error):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_overall_failed_artifact(ready_dir: Path) -> None:
    """合法 provenance row + retrieval_status=FAILED 的 artifact：不得被改写为 SUCCEEDED。"""
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT, BM25),))
    result.result_artifact["retrieval_status"] = "FAILED"
    with pytest.raises(RuntimeError, match="OVERALL_STATUS_TECHNICAL"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_overall_empty_artifact_with_candidates(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT, BM25),))
    result.result_artifact["retrieval_status"] = "EMPTY"
    with pytest.raises(RuntimeError, match="OVERALL_STATUS_EMPTY_CANDIDATE_MISMATCH"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_authoritative_count_mismatch(ready_dir: Path) -> None:
    """Artifact ranked_items=2 与 provenance fused_items=1 不一致必须 FAIL CLOSED。"""
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT, BM25),))
    duplicated = result.result_artifact["ranked_items"] + result.result_artifact["ranked_items"]
    result.result_artifact["retrieved_items"] = duplicated
    result.result_artifact["ranked_items"] = duplicated
    with pytest.raises(RuntimeError, match="AUTHORITATIVE_COUNT_MISMATCH"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_artifact_sequence_mismatch(ready_dir: Path) -> None:
    """Artifact ranked sequence identity/order 与 fused candidates 不一致必须 FAIL CLOSED。"""
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT, BM25), (BM25,)))
    result.result_artifact["ranked_items"] = [
        {"document_id": "other", "chunk_id": "other", "rank": 1},
        result.result_artifact["ranked_items"][1],
    ]
    with pytest.raises(RuntimeError, match="ARTIFACT_SEQUENCE_IDENTITY_MISMATCH"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_out_of_order_fused_items(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT,), (CURRENT, BM25)))
    # 乱序但每个 fused item 的 rank 各自合法（1 与 2 都在）。
    result.provenance_row["fused_items"] = [
        result.provenance_row["fused_items"][1],
        result.provenance_row["fused_items"][0],
    ]
    with pytest.raises(RuntimeError, match="FUSED_ORDER_INVALID"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_rrf_score_mismatch(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT, BM25),))
    result.provenance_row["fused_items"][0]["rrf_score"] = 0.99
    with pytest.raises(RuntimeError, match="RRF_SCORE_MISMATCH"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_channel_rank_out_of_range(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((BM25,),))
    result.provenance_row["fused_items"][0]["bm25_rank"] = 3
    with pytest.raises(RuntimeError, match="CHANNEL_RANK_OUT_OF_RANGE"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_channel_rank_identity_mismatch(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT,),))
    result.provenance_row["current_chunk_ranking"] = [["doc-other", "chunk-other"]]
    with pytest.raises(RuntimeError, match="CHANNEL_RANK_IDENTITY_MISMATCH"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_channel_rank_presence_mismatch(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT,),))
    result.provenance_row["fused_items"][0]["bm25_rank"] = 1
    with pytest.raises(RuntimeError, match="CHANNEL_RANK_PRESENCE_MISMATCH"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_channel_status_ranking_mismatch(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((CURRENT, BM25),))
    result.provenance_row["current_status"] = "EMPTY"
    with pytest.raises(RuntimeError, match="CHANNEL_STATUS_RANKING_MISMATCH"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_channel_ranking_duplicate(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((BM25,), (BM25,)))
    result.provenance_row["bm25_chunk_ranking"] = [
        ["doc-case-x-1", "chunk-case-x-1"],
        ["doc-case-x-1", "chunk-case-x-1"],
    ]
    with pytest.raises(RuntimeError, match="RANKING_DUPLICATE"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_fails_closed_on_per_channel_budget_exceeded(ready_dir: Path) -> None:
    producer = _load_producer()
    result = _bundle(producer, "case-x", "query", ((BM25,),))
    result.provenance_row["current_chunk_ranking"] = [
        [f"doc-extra-{i}", f"chunk-extra-{i}"] for i in range(9)
    ]
    result.provenance_row["current_status"] = "SUCCEEDED"
    with pytest.raises(RuntimeError, match="CURRENT_RANKING_INVALID"):
        producer.project_retrieval_result_to_case(case_id="case-x", query="query", result=result)


def test_producer_orchestration_projects_full_population(ready_dir: Path) -> None:
    producer = _load_producer()
    dataset = load_dataset(ASSET)
    bundles = {
        case.case_id: _bundle(producer, case.case_id, case.input["query"], ((CURRENT, BM25), (BM25,)))
        for case in dataset.cases
    }
    ready = producer.validate_ready_substrate(ready_dir)
    evidence = producer.orchestrate_evidence(dataset, FakeRuntimeProvider(producer, bundles), ready)
    assert isinstance(evidence, RrfEvidenceEnvelopeV2)
    assert {case.case_id for case in evidence.cases} == {case.case_id for case in dataset.cases}
    assert len(evidence.cases) == 28
    assert evidence.substrate_ref == WP4_RRF_SUBSTRATE_REF


def test_producer_formal_mode_rejects_case_subset(ready_dir: Path) -> None:
    producer = _load_producer()
    dataset = load_dataset(ASSET)
    ready = producer.validate_ready_substrate(ready_dir)
    bundles = {
        "cal-answer-terminal-owner": _bundle(producer, "cal-answer-terminal-owner", "q", ())
    }
    with pytest.raises(RuntimeError, match="CASE_POPULATION_MISMATCH"):
        producer.orchestrate_evidence(
            dataset,
            FakeRuntimeProvider(producer, bundles),
            ready,
            case_ids={"cal-answer-terminal-owner"},
        )


def test_producer_orchestration_test_only_subset_projects_substrate(ready_dir: Path) -> None:
    producer = _load_producer()
    dataset = load_dataset(ASSET)
    recorded: list = []
    selected = {
        "cal-answer-terminal-owner",
        "cal-empty-rfc9999",
        "cal-misleading-context-dedup-provenance",
    }
    query_by_id = {case.case_id: case.input["query"] for case in dataset.cases}
    bundles = {
        case_id: _bundle(producer, case_id, query_by_id[case_id], channels)
        for case_id, channels in (
            ("cal-answer-terminal-owner", ((CURRENT, BM25), (BM25,))),
            ("cal-empty-rfc9999", ()),
            ("cal-misleading-context-dedup-provenance", ((BM25,),)),
        )
    }
    ready = producer.validate_ready_substrate(ready_dir)
    evidence = producer.orchestrate_evidence(
        dataset,
        FakeRuntimeProvider(producer, bundles, record_queries=recorded),
        ready,
        case_ids=selected,
        mode=producer.TEST_ONLY_MODE,
    )
    assert isinstance(evidence, RrfEvidenceEnvelopeV2)
    assert evidence.substrate_ref == WP4_RRF_SUBSTRATE_REF
    assert evidence.dense_cache_identity == WP4_DENSE_CACHE_IDENTITY
    assert evidence.bm25_cache_identity == WP4_BM25_CACHE_IDENTITY
    assert {case.case_id for case in evidence.cases} == selected
    assert {case_id for case_id, _ in recorded} == selected
    assert all(case.query_sha256 for case in evidence.cases)


def test_producer_label_blind_population_and_order_spy(ready_dir: Path) -> None:
    """GroundTruth 变化不得改变 runtime population / order / query digests。"""
    producer = _load_producer()
    dataset_a = load_dataset(ASSET)
    payload = dataset_a.model_dump(mode="json")
    cases = payload["cases"]
    cal_idx = next(
        i
        for i, case in enumerate(cases)
        if case["ground_truth"]["answerability"]["split"] == "CALIBRATION"
    )
    eval_idx = next(
        i
        for i, case in enumerate(cases)
        if case["ground_truth"]["answerability"]["split"] == "EVALUATION"
    )
    cal_truth = cases[cal_idx]["ground_truth"]
    cal_meta = cases[cal_idx]["metadata"]
    cases[cal_idx]["ground_truth"], cases[eval_idx]["ground_truth"] = (
        cases[eval_idx]["ground_truth"],
        cal_truth,
    )
    cases[cal_idx]["metadata"], cases[eval_idx]["metadata"] = cases[eval_idx]["metadata"], cal_meta
    dataset_b = EvaluationDataset.model_validate(payload)
    assert [case.case_id for case in dataset_a.cases] == [case.case_id for case in dataset_b.cases]
    assert [case.input["query"] for case in dataset_a.cases] == [
        case.input["query"] for case in dataset_b.cases
    ]
    assert dataset_a.cases[cal_idx].ground_truth.answerability != dataset_b.cases[
        cal_idx
    ].ground_truth.answerability

    class SpyProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def retrieve(self, case_id: str, query: str, *, run_id: str):
            self.calls.append((case_id, hashlib.sha256(query.encode("utf-8")).hexdigest()))
            return _bundle(producer, case_id, query, (), run_id=run_id)

    ready = producer.validate_ready_substrate(ready_dir)
    spy_a = SpyProvider()
    spy_b = SpyProvider()
    producer.orchestrate_evidence(dataset_a, spy_a, ready)
    producer.orchestrate_evidence(dataset_b, spy_b, ready)
    assert len(spy_a.calls) == len(dataset_a.cases) == 28
    ground_truth_changed_runtime_population = len(spy_a.calls) != len(spy_b.calls)
    ground_truth_changed_runtime_order = spy_a.calls != spy_b.calls
    assert ground_truth_changed_runtime_population is False
    assert ground_truth_changed_runtime_order is False


def test_producer_orchestration_fails_closed_on_runtime_technical_failure(ready_dir: Path) -> None:
    producer = _load_producer()
    dataset = load_dataset(ASSET)
    ready = producer.validate_ready_substrate(ready_dir)

    class FailingProvider:
        def retrieve(self, case_id: str, query: str, *, run_id: str):
            raise RuntimeError("HYBRID_CHANNEL_FAILED")

    with pytest.raises(RuntimeError, match="HYBRID_CHANNEL_FAILED"):
        producer.orchestrate_evidence(
            dataset,
            FailingProvider(),
            ready,
            case_ids={"cal-answer-terminal-owner"},
            mode=producer.TEST_ONLY_MODE,
        )


def test_producer_orchestration_fails_closed_on_candidate_duplicate(ready_dir: Path) -> None:
    producer = _load_producer()
    dataset = load_dataset(ASSET)
    ready = producer.validate_ready_substrate(ready_dir)
    identity = ["doc-x", "chunk-x"]
    row = {
        "algorithm_ref": "rrf.v1",
        "rrf_k": 60,
        "current_status": "SUCCEEDED",
        "bm25_status": "SUCCEEDED",
        "current_chunk_ranking": [identity],
        "bm25_chunk_ranking": [identity],
        "fused_items": [
            {
                "document_id": identity[0],
                "chunk_id": identity[1],
                "current_rank": 1,
                "bm25_rank": None,
                "rrf_score": _rrf_score(current_rank=1),
                "rrf_rank": 1,
                "source_channels": [CURRENT],
                "contributing_channel_count": 1,
            },
            {
                "document_id": identity[0],
                "chunk_id": identity[1],
                "current_rank": None,
                "bm25_rank": 1,
                "rrf_score": _rrf_score(bm25_rank=1),
                "rrf_rank": 2,
                "source_channels": [BM25],
                "contributing_channel_count": 1,
            },
        ],
    }
    bundle = _bundle(producer, "cal-answer-terminal-owner", "q", ())
    fixed_row = {**bundle.provenance_row, **row}
    fixed_artifact = {**bundle.result_artifact, "retrieval_status": "SUCCEEDED"}
    duplicate_bundle = producer.RuntimeRetrievalResult(
        result_artifact=fixed_artifact, provenance_row=fixed_row
    )
    with pytest.raises(RuntimeError, match="FUSED_CANDIDATE_DUPLICATE"):
        producer.orchestrate_evidence(
            dataset,
            FakeRuntimeProvider(producer, {"cal-answer-terminal-owner": duplicate_bundle}),
            ready,
            case_ids={"cal-answer-terminal-owner"},
            mode=producer.TEST_ONLY_MODE,
        )


def test_producer_orchestration_fails_closed_on_invalid_rank(ready_dir: Path) -> None:
    producer = _load_producer()
    dataset = load_dataset(ASSET)
    ready = producer.validate_ready_substrate(ready_dir)
    bundle = _bundle(producer, "cal-answer-terminal-owner", "q", ((CURRENT,),))
    bundle.provenance_row["fused_items"][0]["rrf_rank"] = 3
    with pytest.raises(RuntimeError, match="FUSED_ORDER_INVALID"):
        producer.orchestrate_evidence(
            dataset,
            FakeRuntimeProvider(producer, {"cal-answer-terminal-owner": bundle}),
            ready,
            case_ids={"cal-answer-terminal-owner"},
            mode=producer.TEST_ONLY_MODE,
        )


def test_producer_help_does_not_attempt_network(monkeypatch, capsys) -> None:
    attempts: list[object] = []

    def fail_connect(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("network attempt during --help")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    module = _load_producer()
    with pytest.raises(SystemExit) as exc:
        module.main(["--help"])
    assert exc.value.code == 0
    assert attempts == []
    assert "--metadata-dir" in capsys.readouterr().out