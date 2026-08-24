#!/usr/bin/env python
"""WP4 thin RRF evidence producer（只编排现有 LocalAgent RRF runtime，不复制 retrieval 实现）。

职责边界（45_codex_substrate_rebaseline.md Section 8 / Phase B contract）：
- 遍历 Dataset 的完整 frozen case identity population，retrieval path 只读取
  case_id + query（label-blind，绝不读取 GroundTruth / split / case_type）；
- 读取并验证 LocalAgent synthetic READY substrate metadata（Dense/BM25 identities、
  corpus/source/chunk manifests、status=READY）；mismatch 一律 FAIL CLOSED；
- 通过薄 adapter 调用现有 LocalAgent RRF runtime（RRF component Owner = LocalAgent
  HybridRrfRetriever；runtime Owner = LocalAgent HybridRrfEvaluationService）；
- 将 runtime provenance 保真投影为 `no-answer-rrf-evidence.v2`（不排序、不重算、
  不重建；对 algorithm/k/channel status/ranking/count/fused order/channel ranks/
  RRF score 做 exact validate），strict validate 后安全落盘。

禁止：实现 embedding、读 Chroma 并写 Dense search、实现 BM25、计算 channel ranking、
实现/重算 RRF score、rerank、改变 candidate budget。--help 必须在无 embedding / Chroma /
模型 / 网络 side effect 下 exit 0（lazy runtime resolution）。
"""

# ruff: noqa: D103, D415, E402

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.evaluation.no_answer import (
    BM25_CHANNEL_REF,
    CORPUS_REF,
    CURRENT_CHANNEL_REF,
    FINAL_CANDIDATE_LIMIT,
    PER_CHANNEL_CANDIDATE_LIMIT,
    PRE_FUSION_UNION_LIMIT,
    RRF_BASELINE_REF,
    RRF_K,
    WP4_BM25_CACHE_IDENTITY,
    WP4_CHUNK_MANIFEST_DIGEST,
    WP4_DENSE_CACHE_IDENTITY,
    WP4_RRF_SUBSTRATE_REF,
    WP4_SOURCE_MANIFEST_DIGEST,
    RrfEvidenceEnvelopeV2,
)
from app.services.evaluation.no_answer_threshold import canonical_digest

READY_STATUS = "READY"
FORMAL_MODE = "FORMAL"
TEST_ONLY_MODE = "DETERMINISTIC_TEST_ONLY"
_VALID_MODES = frozenset({FORMAL_MODE, TEST_ONLY_MODE})
_TECHNICAL_STATUSES = frozenset({"FAILED", "TIMED_OUT", "CANCELLED"})
_VALID_CHANNEL_STATUSES = frozenset({"SUCCEEDED", "EMPTY", "DEGRADED"})


def _chunk_list_digest(chunks: list[dict[str, Any]]) -> str:
    """对 ordered chunk identity 列表计算 canonical digest（只用于 manifest 一致性验证）。"""
    canonical = json.dumps(list(chunks), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReadySubstrateFacts:
    """从 LocalAgent READY metadata 验证得到的 frozen substrate facts。"""

    dense_cache_identity: str
    bm25_cache_identity: str
    corpus_id: str
    source_manifest_digest: str
    chunk_manifest_digest: str
    document_count: int
    chunk_count: int


def validate_ready_substrate(metadata_dir: Path) -> ReadySubstrateFacts:
    """读取并验证 LocalAgent synthetic READY substrate metadata；mismatch 一律 FAIL CLOSED。"""
    root = metadata_dir.resolve()
    dense_meta = json.loads((root / "dense_cache_metadata.json").read_text(encoding="utf-8"))
    bm25_meta = json.loads((root / "bm25_cache_metadata.json").read_text(encoding="utf-8"))
    corpus_manifest = json.loads((root / "corpus_manifest.json").read_text(encoding="utf-8"))
    dense_chunks = json.loads((root / "dense_chunk_manifest.json").read_text(encoding="utf-8")).get("chunks")
    bm25_chunks = json.loads((root / "bm25_chunk_manifest.json").read_text(encoding="utf-8")).get("chunks")

    dense_digest = dense_meta.get("chunk_manifest_sha256")
    bm25_digest = bm25_meta.get("chunk_manifest_sha256")
    source_digest = corpus_manifest.get("source_manifest_sha256")
    chunk_digest = corpus_manifest.get("chunk_manifest_sha256")
    checks = [
        dense_meta.get("cache_status") == READY_STATUS,
        bm25_meta.get("cache_status") == READY_STATUS,
        dense_meta.get("cache_key") == WP4_DENSE_CACHE_IDENTITY,
        bm25_meta.get("cache_key") == WP4_BM25_CACHE_IDENTITY,
        corpus_manifest.get("corpus_id") == CORPUS_REF,
        dense_meta.get("corpus_id") == CORPUS_REF,
        bm25_meta.get("corpus_id") == CORPUS_REF,
        source_digest == WP4_SOURCE_MANIFEST_DIGEST,
        dense_meta.get("source_manifest_sha256") == WP4_SOURCE_MANIFEST_DIGEST,
        bm25_meta.get("source_manifest_sha256") == WP4_SOURCE_MANIFEST_DIGEST,
        chunk_digest == WP4_CHUNK_MANIFEST_DIGEST,
        dense_digest == WP4_CHUNK_MANIFEST_DIGEST,
        bm25_digest == WP4_CHUNK_MANIFEST_DIGEST,
        dense_digest == bm25_digest,
        corpus_manifest.get("document_count") == 15,
        dense_meta.get("document_count") == 15,
        bm25_meta.get("document_count") == 15,
        corpus_manifest.get("chunk_count") == 60,
        dense_meta.get("chunk_count") == 60,
        bm25_meta.get("chunk_count") == 60,
    ]
    if not isinstance(dense_chunks, list) or not isinstance(bm25_chunks, list):
        checks.append(False)
    elif (
        len(dense_chunks) != 60
        or len(bm25_chunks) != 60
        or dense_chunks != bm25_chunks
        or _chunk_list_digest(dense_chunks) != WP4_CHUNK_MANIFEST_DIGEST
        or _chunk_list_digest(bm25_chunks) != WP4_CHUNK_MANIFEST_DIGEST
    ):
        checks.append(False)
    if not all(checks):
        raise RuntimeError("SUBSTRATE_NOT_READY: READY metadata does not match frozen synthetic substrate")
    return ReadySubstrateFacts(
        dense_cache_identity=dense_meta["cache_key"],
        bm25_cache_identity=bm25_meta["cache_key"],
        corpus_id=CORPUS_REF,
        source_manifest_digest=str(source_digest),
        chunk_manifest_digest=str(chunk_digest),
        document_count=15,
        chunk_count=60,
    )


@dataclass(frozen=True, slots=True)
class RuntimeRetrievalResult:
    """同一次 LocalAgent retrieval 的 result artifact + RRF provenance row bundle。

    `result_artifact`：HybridRrfEvaluationService.execute() 真实 result artifact shape
    （overall `retrieval_status`、`retrieved_items`、`ranked_items`、`run_id`、`artifact_id`）。
    `provenance_row`：同一 run 的 RRF JSONL provenance row shape（channel rankings、fused items）。
    """

    result_artifact: dict[str, Any]
    provenance_row: dict[str, Any]


class RuntimeProvider(Protocol):
    """LocalAgent RRF runtime 的薄调用边界；返回同一 run 的 artifact + provenance bundle。"""

    def retrieve(self, case_id: str, query: str, *, run_id: str) -> RuntimeRetrievalResult:
        """对单个 case 执行 label-blind retrieval，返回同一 run 的 artifact + provenance。"""


def _validate_channel_ranking(ranking: Any, *, channel: str) -> list[tuple[str, str]]:
    """验证 runtime channel ranked list：有序 [document_id, chunk_id]、rank 1..N、无重复、<= budget。

    返回按 rank 排序的 identity 列表，作为 fused candidate 的 channel rank Authority。
    """
    if not isinstance(ranking, list) or len(ranking) > PER_CHANNEL_CANDIDATE_LIMIT:
        raise RuntimeError(f"PROVENANCE_{channel.upper()}_RANKING_INVALID")
    identities: list[tuple[str, str]] = []
    for item in ranking:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise RuntimeError(f"PROVENANCE_{channel.upper()}_RANKING_INVALID")
        document_id, chunk_id = item[0], item[1]
        if (
            not isinstance(document_id, str)
            or not isinstance(chunk_id, str)
            or not document_id
            or not chunk_id
        ):
            raise RuntimeError(f"PROVENANCE_{channel.upper()}_RANKING_INVALID")
        identities.append((document_id, chunk_id))
    if len(set(identities)) != len(identities):
        raise RuntimeError(f"PROVENANCE_{channel.upper()}_RANKING_DUPLICATE")
    return identities


def _identity_of(item: Any) -> tuple[str, str]:
    if not isinstance(item, dict):
        raise RuntimeError("ARTIFACT_ITEM_INVALID")
    document_id = item.get("document_id")
    chunk_id = item.get("chunk_id")
    if (
        not isinstance(document_id, str)
        or not isinstance(chunk_id, str)
        or not document_id
        or not chunk_id
    ):
        raise RuntimeError("ARTIFACT_ITEM_INVALID")
    return document_id, chunk_id


def project_retrieval_result_to_case(
    *, case_id: str, query: str, result: RuntimeRetrievalResult
) -> dict[str, Any]:
    """保真投影同一 run 的 result artifact + provenance row 为 evidence v2 case payload。

    Overall status / retrieved / ranked counts 只来自 LocalAgent result artifact 的
    authoritative facts；provenance row 提供 channel 与 RRF fusion 细节；双方 exact
    cross-validate。不排序、不重算、不重建任何 runtime Authority 事实。
    """
    artifact = result.result_artifact
    row = result.provenance_row

    artifact_run = artifact.get("run_id")
    row_run = row.get("run_id")
    if not isinstance(artifact_run, str) or artifact_run != row_run:
        raise RuntimeError("ARTIFACT_PROVENANCE_RUN_MISMATCH")

    query_digest = row.get("query_sha256")
    if query_digest != hashlib.sha256(query.encode("utf-8")).hexdigest():
        raise RuntimeError("EVIDENCE_QUERY_DIGEST_MISMATCH")
    artifact_query = artifact.get("query")
    if not isinstance(artifact_query, str) or not artifact_query:
        raise RuntimeError("ARTIFACT_QUERY_MISSING")
    if hashlib.sha256(artifact_query.encode("utf-8")).hexdigest() != query_digest:
        raise RuntimeError("ARTIFACT_QUERY_DIGEST_MISMATCH")
    artifact_id = artifact.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise RuntimeError("ARTIFACT_ID_MISSING")
    if "\\" in artifact_id or artifact_id.startswith(("/", "file:")):
        raise RuntimeError("ARTIFACT_ID_INVALID")

    if row.get("algorithm_ref") != RRF_BASELINE_REF:
        raise RuntimeError("PROVENANCE_ALGORITHM_MISMATCH")
    if row.get("rrf_k") != RRF_K:
        raise RuntimeError("PROVENANCE_RRF_K_MISMATCH")

    current_status = row.get("current_status")
    bm25_status = row.get("bm25_status")
    if current_status in _TECHNICAL_STATUSES or bm25_status in _TECHNICAL_STATUSES:
        raise RuntimeError("PROVENANCE_CHANNEL_TECHNICAL_FAILURE")
    if (
        current_status not in _VALID_CHANNEL_STATUSES
        or bm25_status not in _VALID_CHANNEL_STATUSES
    ):
        raise RuntimeError("PROVENANCE_CHANNEL_STATUS_INVALID")

    current_ids = _validate_channel_ranking(row.get("current_chunk_ranking"), channel="current")
    bm25_ids = _validate_channel_ranking(row.get("bm25_chunk_ranking"), channel="bm25")
    if (current_status == "EMPTY") != (len(current_ids) == 0):
        raise RuntimeError("PROVENANCE_CHANNEL_STATUS_RANKING_MISMATCH")
    if (bm25_status == "EMPTY") != (len(bm25_ids) == 0):
        raise RuntimeError("PROVENANCE_CHANNEL_STATUS_RANKING_MISMATCH")
    union_ids = set(current_ids) | set(bm25_ids)
    if len(union_ids) > PRE_FUSION_UNION_LIMIT:
        raise RuntimeError("PROVENANCE_UNION_BUDGET_EXCEEDED")

    fused = row.get("fused_items")
    if not isinstance(fused, list) or len(fused) > FINAL_CANDIDATE_LIMIT:
        raise RuntimeError("PROVENANCE_FUSED_ITEMS_INVALID")

    ranked: list[dict[str, Any]] = []
    fused_identities: set[tuple[str, str]] = set()
    for expected_rank, item in enumerate(fused, 1):
        if not isinstance(item, dict):
            raise RuntimeError("PROVENANCE_FUSED_ITEMS_INVALID")
        document_id = item.get("document_id")
        chunk_id = item.get("chunk_id")
        identity = (document_id, chunk_id)
        if (
            not isinstance(document_id, str)
            or not isinstance(chunk_id, str)
            or not document_id
            or not chunk_id
        ):
            raise RuntimeError("PROVENANCE_FUSED_ITEMS_INVALID")
        if identity in fused_identities:
            raise RuntimeError("PROVENANCE_FUSED_CANDIDATE_DUPLICATE")
        fused_identities.add(identity)
        if item.get("rrf_rank") != expected_rank:
            raise RuntimeError("PROVENANCE_FUSED_ORDER_INVALID")

        channels = item.get("source_channels")
        if (
            not isinstance(channels, list)
            or not channels
            or len(channels) > 2
            or len(set(channels)) != len(channels)
            or any(channel not in (CURRENT_CHANNEL_REF, BM25_CHANNEL_REF) for channel in channels)
        ):
            raise RuntimeError("PROVENANCE_FUSED_CHANNELS_INVALID")
        if item.get("contributing_channel_count") != len(channels):
            raise RuntimeError("PROVENANCE_FUSED_CHANNEL_COUNT_MISMATCH")

        current_rank = item.get("current_rank")
        bm25_rank = item.get("bm25_rank")
        if (current_rank is not None) != (CURRENT_CHANNEL_REF in channels):
            raise RuntimeError("PROVENANCE_CHANNEL_RANK_PRESENCE_MISMATCH")
        if (bm25_rank is not None) != (BM25_CHANNEL_REF in channels):
            raise RuntimeError("PROVENANCE_CHANNEL_RANK_PRESENCE_MISMATCH")
        for _name, rank, ranking in (
            ("current", current_rank, current_ids),
            ("bm25", bm25_rank, bm25_ids),
        ):
            if rank is not None:
                if rank < 1 or rank > len(ranking):
                    raise RuntimeError("PROVENANCE_CHANNEL_RANK_OUT_OF_RANGE")
                if ranking[rank - 1] != identity:
                    raise RuntimeError("PROVENANCE_CHANNEL_RANK_IDENTITY_MISMATCH")

        rrf_score = item.get("rrf_score")
        if not isinstance(rrf_score, (int, float)):
            raise RuntimeError("PROVENANCE_RRF_SCORE_INVALID")
        expected_score = sum(
            1.0 / (RRF_K + rank)
            for rank in (current_rank, bm25_rank)
            if rank is not None
        )
        if not math.isclose(rrf_score, expected_score, rel_tol=0.0, abs_tol=1e-12):
            raise RuntimeError("PROVENANCE_RRF_SCORE_MISMATCH")

        ranked.append(
            {
                "document_id": document_id,
                "chunk_id": chunk_id,
                "rank": expected_rank,
                "rrf_score": rrf_score,
                "source_channels": list(channels),
                "contributing_channel_count": len(channels),
                "current_rank": current_rank,
                "bm25_rank": bm25_rank,
            }
        )

    # ---- result artifact overall Authority ----
    artifact_status = artifact.get("retrieval_status")
    if artifact_status not in {"SUCCEEDED", "EMPTY", "DEGRADED", "FAILED", "TIMED_OUT", "CANCELLED"}:
        raise RuntimeError("ARTIFACT_STATUS_INVALID")
    if artifact_status in _TECHNICAL_STATUSES:
        raise RuntimeError("OVERALL_STATUS_TECHNICAL")

    retrieved_items = artifact.get("retrieved_items")
    ranked_items = artifact.get("ranked_items")
    if not isinstance(retrieved_items, list) or not isinstance(ranked_items, list):
        raise RuntimeError("ARTIFACT_SEQUENCES_INVALID")
    retrieved_candidate_count = len(retrieved_items)
    ranked_candidate_count = len(ranked_items)

    # status/candidate consistency（不重建 status）
    if artifact_status == "EMPTY":
        if ranked or ranked_candidate_count or retrieved_candidate_count:
            raise RuntimeError("OVERALL_STATUS_EMPTY_CANDIDATE_MISMATCH")
    elif not ranked:
        raise RuntimeError("OVERALL_STATUS_SUCCEEDED_NO_CANDIDATES")

    # counts：来自 authoritative artifact sequence length，并 exact 验证与 fused 一致
    if ranked_candidate_count != len(ranked):
        raise RuntimeError("AUTHORITATIVE_COUNT_MISMATCH")
    if ranked_candidate_count > retrieved_candidate_count:
        raise RuntimeError("AUTHORITATIVE_COUNT_MISMATCH")
    if (
        retrieved_candidate_count > PRE_FUSION_UNION_LIMIT
        or ranked_candidate_count > FINAL_CANDIDATE_LIMIT
    ):
        raise RuntimeError("AUTHORITATIVE_COUNT_BUDGET_EXCEEDED")

    artifact_ranked_ids = [_identity_of(item) for item in ranked_items]
    artifact_retrieved_ids = [_identity_of(item) for item in retrieved_items]
    projected_ids = [(candidate["document_id"], candidate["chunk_id"]) for candidate in ranked]
    if artifact_ranked_ids != projected_ids:
        raise RuntimeError("ARTIFACT_SEQUENCE_IDENTITY_MISMATCH")
    if artifact_retrieved_ids != artifact_ranked_ids:
        raise RuntimeError("ARTIFACT_SEQUENCE_IDENTITY_MISMATCH")

    if not ranked:
        if union_ids or current_status != "EMPTY" or bm25_status != "EMPTY":
            raise RuntimeError("PROVENANCE_EMPTY_STATUS_MISMATCH")
    elif (
        current_status not in ("SUCCEEDED", "DEGRADED")
        and bm25_status not in ("SUCCEEDED", "DEGRADED")
    ):
        raise RuntimeError("PROVENANCE_SUCCEEDED_STATUS_MISMATCH")

    return {
        "case_id": case_id,
        "query_sha256": query_digest,
        "retrieval_artifact_id": artifact_id,
        "retrieval_status": artifact_status,
        "retrieved_candidate_count": retrieved_candidate_count,
        "ranked_candidate_count": ranked_candidate_count,
        "ranked_candidates": ranked,
    }


def _expected_case_ids(dataset: Any) -> set[str]:
    """从 frozen Dataset cases 机械派生完整 case identity population（不读取 GroundTruth）。"""
    return {case.case_id for case in dataset.cases}


def orchestrate_evidence(
    dataset: Any,
    provider: RuntimeProvider,
    ready: ReadySubstrateFacts,
    *,
    case_ids: set[str] | None = None,
    mode: str = FORMAL_MODE,
) -> RrfEvidenceEnvelopeV2:
    """label-blind 编排：机械遍历完整 case population，只读 case_id + query。

    正式模式（FORMAL）必须覆盖完整 Dataset population；`case_ids` 只允许在
    DETERMINISTIC_TEST_ONLY 模式用于显式小范围测试，且缺 case 在写 wire 前 FAIL CLOSED。
    绝不读取 GroundTruth / split / case_type / support IDs 影响 retrieval。
    """
    if mode not in _VALID_MODES:
        raise RuntimeError("PRODUCER_MODE_INVALID")
    full_case_ids = _expected_case_ids(dataset)
    if mode == TEST_ONLY_MODE:
        population = set(case_ids) if case_ids is not None else set(full_case_ids)
        if not population or not population <= full_case_ids:
            raise RuntimeError("CASE_POPULATION_MISMATCH")
    elif case_ids is None or set(case_ids) == full_case_ids:
        population = set(full_case_ids)
    else:
        raise RuntimeError(
            "CASE_POPULATION_MISMATCH: formal evidence requires the full Dataset case population"
        )

    evidence_cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for case in dataset.cases:
        if case.case_id not in population:
            continue
        if case.case_id in seen:
            raise RuntimeError("duplicate case identity in dataset")
        seen.add(case.case_id)
        query = case.input.get("query")
        if not isinstance(query, str) or not query:
            raise RuntimeError("dataset query required")
        row = provider.retrieve(case.case_id, query, run_id=str(uuid4()))
        if not isinstance(row, RuntimeRetrievalResult):
            raise RuntimeError("runtime provider returned invalid retrieval result")
        evidence_cases.append(
            project_retrieval_result_to_case(case_id=case.case_id, query=query, result=row)
        )
    if set(seen) != population:
        raise RuntimeError("CASE_COVERAGE_MISMATCH: every expected case must produce evidence")
    if not evidence_cases:
        raise RuntimeError("no eligible cases produced evidence")
    return RrfEvidenceEnvelopeV2.model_validate(
        {
            "schema_version": "no-answer-rrf-evidence.v2",
            "substrate_ref": WP4_RRF_SUBSTRATE_REF,
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "dataset_digest": canonical_digest(dataset.model_dump(mode="json")),
            "corpus_ref": ready.corpus_id,
            "source_manifest_digest": ready.source_manifest_digest,
            "chunk_manifest_digest": ready.chunk_manifest_digest,
            "dense_cache_identity": ready.dense_cache_identity,
            "bm25_cache_identity": ready.bm25_cache_identity,
            "algorithm_ref": RRF_BASELINE_REF,
            "rrf_k": RRF_K,
            "dense_channel_ref": CURRENT_CHANNEL_REF,
            "bm25_channel_ref": BM25_CHANNEL_REF,
            "per_channel_candidate_limit": PER_CHANNEL_CANDIDATE_LIMIT,
            "pre_fusion_union_limit": PRE_FUSION_UNION_LIMIT,
            "final_candidate_limit": FINAL_CANDIDATE_LIMIT,
            "ce_used": False,
            "new_model_used": False,
            "runtime_read_only": True,
            "cases": evidence_cases,
        }
    )


class LocalAgentRuntimeProvider:
    """极薄 adapter：lazy 复用 LocalAgent 现有 synthetic READY runtime。

    本 Phase（Phase B）禁止实际运行真实 28-case retrieval；该类只作为真实 seam 的
    最小实现，依赖 LocalAgent 自身的 cache lifecycle 与 HybridRrfEvaluationService。
    构造/调用前不会初始化 embedding / Chroma / 模型 / 网络。
    """

    def __init__(
        self,
        *,
        local_agent_root: Path,
        cache_root: Path,
        corpus_dir: Path | None = None,
        embedding_model_path: Path | None = None,
        metadata_dir: Path | None = None,
    ) -> None:
        self._local_agent_root = local_agent_root.resolve()
        self._cache_root = cache_root.resolve()
        self._corpus_dir = corpus_dir.resolve() if corpus_dir else None
        self._embedding_model_path = embedding_model_path
        self._metadata_dir = metadata_dir.resolve() if metadata_dir else None

    def retrieve(self, case_id: str, query: str, *, run_id: str) -> RuntimeRetrievalResult:
        """Real seam：在单独授权前拒绝真实 retrieval（Phase B 禁止 28-case evidence）。"""
        # 真实 retrieval 路径在单独授权前不得执行；此处仅表达 thin seam 的结构。
        # 未来真实执行时必须从同一次 HybridRrfEvaluationService execution 同时取得
        # result artifact 与对应 provenance row（禁止分别来自两个不同 run）。
        raise RuntimeError(
            "LOCALAGENT_REAL_RETRIEVAL_NOT_ALLOWED_IN_PHASE_B: real 28-case retrieval "
            "requires a separate authorization gate"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--metadata-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--local-agent-root", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--corpus-dir", type=Path)
    parser.add_argument("--embedding-model-path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # argparse --help 在此之前退出；runtime/embedding/Chroma/provider 全部 lazy resolution。
    from app.core.evaluation.dataset import load_dataset

    ready = validate_ready_substrate(args.metadata_dir)
    provider = LocalAgentRuntimeProvider(
        local_agent_root=args.local_agent_root,
        cache_root=args.cache_root,
        corpus_dir=args.corpus_dir,
        embedding_model_path=args.embedding_model_path,
        metadata_dir=args.metadata_dir,
    )
    dataset = load_dataset(args.dataset)
    evidence = orchestrate_evidence(dataset, provider, ready)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "no-answer-rrf-evidence.v2.json").write_text(
        json.dumps(evidence.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())