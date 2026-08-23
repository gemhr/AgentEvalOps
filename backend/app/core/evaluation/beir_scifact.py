"""BEIR SciFact public retrieval benchmark 的最薄 Dataset Adapter。

以 BEIR 官方格式为唯一权威：corpus.jsonl / queries.jsonl / qrels/test.tsv 从外部
read-only 路径加载（stdlib json + csv），不把完整 dataset 复制进 Git tracked assets，
不重新生成 query 或 relevance label。qrels 是唯一 ground truth authority，
document-level 原样保留为 DocumentRetrievalGroundTruth，不展开为 fake chunk truth。
"""

# ruff: noqa: D415

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.core.evaluation.dataset import (
    EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION,
    DocumentRelevance,
    DocumentRetrievalGroundTruth,
    EvaluationDataset,
    EvaluationCase,
    GroundTruth,
)

BENCHMARK_KIND = "BEIR_SCIFACT_LOCALAGENT_ADAPTED"
BEIR_BENCHMARK = "beir"
SCIFACT_DATASET = "scifact"
SCIFACT_SPLIT = "test"

BEIR_SCIFACT_DATASET_ID = "beir-scifact-dataset"
BEIR_SCIFACT_DATASET_VERSION = "v1"

# 冻结的 Asset identity：后续 Candidate 必须使用完全相同的 asset。
FROZEN_ZIP_MD5 = "5f7d1de60b170fc8027bb7898e2efca1"
FROZEN_CORPUS_SHA256 = "dec31c8182f3d744c7d2c09423756fd1d17cbef75808db13ba01cc0aab4d1ac6"
FROZEN_QUERIES_SHA256 = "8ff84a7c903f722981cd8d595c022660140c51867b27608a6d4910db86080313"
FROZEN_QRELS_TEST_SHA256 = "0864bb985e0ca2367ba217977e72004d549054b2b06666ed9d4825ac7c21284c"

CHECKSUM_MISMATCH = "BEIR_SCIFACT_CHECKSUM_MISMATCH"
INTEGRITY_GAP = "BEIR_SCIFACT_DATASET_INTEGRITY_GAP"

TRUTHFULNESS_LABEL = "PUBLIC_BENCHMARK_BEIR_SCIFACT"


class BeirScifactAssetError(ValueError):
    """BEIR SciFact asset 校验失败；code 携带稳定错误码。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.is_file():
        raise BeirScifactAssetError(f"missing BEIR SciFact asset file: {path}")
    return path


def _query_sort_key(query_id: str) -> tuple[int, str]:
    return (0, query_id) if query_id.isdigit() else (1, query_id)


@dataclass(frozen=True, slots=True)
class BeirScifactAsset:
    """已验证的外部 BEIR SciFact asset 及其解析结果与真实统计。"""

    root: Path
    corpus: dict[str, dict[str, str]]
    queries: dict[str, str]
    qrels: dict[str, dict[str, int]]
    checksums: dict[str, str]
    statistics: dict[str, Any]

    @property
    def test_query_ids(self) -> list[str]:
        """按确定性顺序返回全部 test qrels query id。"""
        return sorted(self.qrels, key=_query_sort_key)


def load_beir_scifact_asset(
    root: str | Path,
    *,
    verify_checksums: bool = True,
    expected_checksums: Mapping[str, str] | None = None,
) -> BeirScifactAsset:
    """加载并验证外部 BEIR SciFact asset；checksum 或 dataset integrity 失败即 fail closed.

    qrels 语义遵循 BEIR 官方 input loader：同一 (query-id, corpus-id) 的重复行按
    dict 覆盖处理（last-wins），不制造新的 relevance entry；全部重复会计入统计。
    """
    base = Path(root)
    corpus_path = _require_file(base, "corpus.jsonl")
    queries_path = _require_file(base, "queries.jsonl")
    qrels_path = _require_file(base, "qrels/test.tsv")

    checksums = {
        "corpus_sha256": _sha256(corpus_path),
        "queries_sha256": _sha256(queries_path),
        "qrels_test_sha256": _sha256(qrels_path),
    }
    expected = dict(expected_checksums) if expected_checksums is not None else {
        "corpus_sha256": FROZEN_CORPUS_SHA256,
        "queries_sha256": FROZEN_QUERIES_SHA256,
        "qrels_test_sha256": FROZEN_QRELS_TEST_SHA256,
    }
    if verify_checksums:
        mismatches = {key: value for key, value in checksums.items() if value != expected[key]}
        if mismatches:
            raise BeirScifactAssetError(
                f"{CHECKSUM_MISMATCH}: {sorted(mismatches)} do not match frozen asset identity"
            )

    corpus: dict[str, dict[str, str]] = {}
    with corpus_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            entry = json.loads(line)
            document_id = entry.get("_id")
            if not isinstance(document_id, str) or not document_id:
                raise BeirScifactAssetError(f"corpus.jsonl line {line_number} has invalid _id")
            if document_id in corpus:
                raise BeirScifactAssetError(f"corpus.jsonl duplicate _id: {document_id}")
            title = entry.get("title")
            text = entry.get("text")
            if not isinstance(title, str) or not isinstance(text, str):
                raise BeirScifactAssetError(
                    f"corpus.jsonl line {line_number} has invalid title/text"
                )
            corpus[document_id] = {"title": title, "text": text}

    queries: dict[str, str] = {}
    with queries_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            entry = json.loads(line)
            query_id = entry.get("_id")
            text = entry.get("text")
            if not isinstance(query_id, str) or not query_id or not isinstance(text, str):
                raise BeirScifactAssetError(f"queries.jsonl line {line_number} is invalid")
            if query_id in queries:
                raise BeirScifactAssetError(f"queries.jsonl duplicate _id: {query_id}")
            queries[query_id] = text

    qrels: dict[str, dict[str, int]] = {}
    duplicate_rows = 0
    with qrels_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        header = next(reader, None)
        if header != ["query-id", "corpus-id", "score"]:
            raise BeirScifactAssetError(f"qrels/test.tsv unexpected header: {header!r}")
        for row_number, row in enumerate(reader, 2):
            if not row:
                continue
            if len(row) != 3:
                raise BeirScifactAssetError(f"qrels/test.tsv row {row_number} has {len(row)} cols")
            query_id, corpus_id, score_text = row
            try:
                score = int(score_text)
            except ValueError as error:
                raise BeirScifactAssetError(
                    f"qrels/test.tsv row {row_number} has non-integer score"
                ) from error
            if score < 0:
                raise BeirScifactAssetError(f"qrels/test.tsv row {row_number} has negative score")
            if corpus_id not in corpus:
                raise BeirScifactAssetError(
                    f"{INTEGRITY_GAP}: qrels row {row_number} references unknown corpus id "
                    f"{corpus_id!r}"
                )
            if query_id not in queries:
                raise BeirScifactAssetError(
                    f"{INTEGRITY_GAP}: qrels row {row_number} references unknown query id "
                    f"{query_id!r}"
                )
            judgments = qrels.setdefault(query_id, {})
            if corpus_id in judgments:
                duplicate_rows += 1
                if judgments[corpus_id] != score:
                    raise BeirScifactAssetError(
                        f"qrels/test.tsv row {row_number} conflicts on "
                        f"({query_id!r}, {corpus_id!r})"
                    )
            judgments[corpus_id] = score

    qrels_rows = sum(len(docs) for docs in qrels.values()) + duplicate_rows
    docs_per_query = [len(docs) for docs in qrels.values()]
    score_distribution = Counter(
        score for docs in qrels.values() for score in docs.values()
    )
    statistics = {
        "corpus_document_count": len(corpus),
        "query_file_count": len(queries),
        "test_query_count": len(qrels),
        "qrels_rows": qrels_rows,
        "qrels_duplicate_rows": duplicate_rows,
        "unique_relevant_document_count": len(
            {doc for docs in qrels.values() for doc in docs}
        ),
        "relevance_score_distribution": dict(sorted(score_distribution.items())),
        "relevant_documents_per_query_distribution": dict(
            sorted(Counter(docs_per_query).items())
        ),
        "mean_relevant_documents_per_query": (
            sum(docs_per_query) / len(docs_per_query) if docs_per_query else 0.0
        ),
    }
    return BeirScifactAsset(
        root=base,
        corpus=corpus,
        queries=queries,
        qrels=qrels,
        checksums=checksums,
        statistics=statistics,
    )


def build_beir_scifact_dataset(asset: BeirScifactAsset) -> EvaluationDataset:
    """从 qrels 权威构建内存中的 document-level EvaluationDataset（不落 Git asset）。"""
    relevance_kind = (
        "binary"
        if set(asset.statistics["relevance_score_distribution"]) == {1}
        else "graded"
    )
    cases = []
    for query_id in asset.test_query_ids:
        judgments = asset.qrels[query_id]
        cases.append(
            EvaluationCase(
                case_id=f"scifact-test-{query_id}",
                name=f"BEIR SciFact test query {query_id}",
                input={"agent_id": "knowledge_expert", "query": asset.queries[query_id]},
                ground_truth=GroundTruth(
                    document_retrieval=DocumentRetrievalGroundTruth(
                        relevant_documents=[
                            DocumentRelevance(document_id=doc_id, relevance=score)
                            for doc_id, score in sorted(
                                judgments.items(), key=lambda item: _query_sort_key(item[0])
                            )
                        ]
                    )
                ),
                metadata={
                    "benchmark": BEIR_BENCHMARK,
                    "benchmark_dataset": SCIFACT_DATASET,
                    "benchmark_split": SCIFACT_SPLIT,
                    "benchmark_query_id": query_id,
                    "case_type": "PUBLIC_BENCHMARK_RETRIEVAL",
                    "truthfulness_label": TRUTHFULNESS_LABEL,
                    "relevance_kind": relevance_kind,
                },
            )
        )
    return EvaluationDataset(
        dataset_schema_version=EVALUATION_DATASET_DOCUMENT_SCHEMA_VERSION,
        dataset_id=BEIR_SCIFACT_DATASET_ID,
        name="BEIR SciFact test split (document-level retrieval)",
        description=(
            "BEIR SciFact test split loaded at runtime from the external read-only asset; "
            "qrels are the sole document-level ground truth authority."
        ),
        version=BEIR_SCIFACT_DATASET_VERSION,
        cases=cases,
    )


__all__ = [
    "BENCHMARK_KIND",
    "BEIR_BENCHMARK",
    "BEIR_SCIFACT_DATASET_ID",
    "BEIR_SCIFACT_DATASET_VERSION",
    "BeirScifactAsset",
    "BeirScifactAssetError",
    "CHECKSUM_MISMATCH",
    "FROZEN_CORPUS_SHA256",
    "FROZEN_QRELS_TEST_SHA256",
    "FROZEN_QUERIES_SHA256",
    "FROZEN_ZIP_MD5",
    "INTEGRITY_GAP",
    "SCIFACT_DATASET",
    "SCIFACT_SPLIT",
    "TRUTHFULNESS_LABEL",
    "build_beir_scifact_dataset",
    "load_beir_scifact_asset",
]
