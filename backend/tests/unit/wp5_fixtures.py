"""Shared WP5 test fixtures: synthetic controlled corpus, evidence, and dataset.

These fixtures are deterministic and privacy-safe (no query/chunk plaintext in
evidence or dataset output paths). They do not touch LocalAgent or the frozen
WP4 real evidence.
"""

# ruff: noqa: D100, D103, D415

from __future__ import annotations

import hashlib
import json

from app.core.evaluation.citation_context_selection import (
    ControlledCorpus,
    ControlledCorpusEntry,
    ordered_chunk_manifest_digest,
)

FROZEN_SOURCE_MANIFEST_DIGEST = "4da8c504a8ad77ae6c8dd9ec004c7178f26fe5ee7be1a4cf94b822bce9b427f6"
FROZEN_CHUNK_MANIFEST_DIGEST = "149a39a7d6b45fb7484f934288037f787b6322dd13d135fd721b4a1d5117cc91"


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_corpus(*chunks: tuple[str, str, str, str]) -> ControlledCorpus:
    """Build a controlled corpus from (chunk_id, document_id, source, snippet) tuples."""
    entries = []
    for chunk_id, document_id, source, snippet in chunks:
        entries.append(
            ControlledCorpusEntry(
                document_id=document_id,
                chunk_id=chunk_id,
                source=source,
                section_path=None,
                content_hash=sha1_text(snippet),
                content_digest=sha256_text(snippet),
                snippet=snippet,
            )
        )
    digest = ordered_chunk_manifest_digest(entries)
    return ControlledCorpus(
        corpus_ref="rag-evaluation-corpus.v1",
        source_manifest_digest=FROZEN_SOURCE_MANIFEST_DIGEST,
        chunk_manifest_digest=digest,
        entries=tuple(entries),
    )


def _rrf_score(*, current: int | None = None, bm25: int | None = None) -> float:
    return sum(1.0 / (60 + rank) for rank in (current, bm25) if rank is not None)


def make_evidence(
    cases: list[dict],
    *,
    dataset_digest: str,
) -> dict:
    """Build an RrfEvidenceEnvelopeV2 payload dict with the given ranked candidates."""
    return {
        "schema_version": "no-answer-rrf-evidence.v2",
        "substrate_ref": "wp4-no-answer-rrf-substrate.v2",
        "dataset_id": "no-answer-threshold-dataset",
        "dataset_version": "v2",
        "dataset_digest": dataset_digest,
        "corpus_ref": "rag-evaluation-corpus.v1",
        "source_manifest_digest": FROZEN_SOURCE_MANIFEST_DIGEST,
        "chunk_manifest_digest": FROZEN_CHUNK_MANIFEST_DIGEST,
        "dense_cache_identity": "92c4743c308e914e311345c22cb09f633a8bb89a6dd73e3820f45cb167046616",
        "bm25_cache_identity": "33040278c1995934df185be2c625fb2e45f0950436e0d748f03e80950e65c4f9",
        "algorithm_ref": "rrf.v1",
        "rrf_k": 60,
        "dense_channel_ref": "current-dense-led-ranked.v1",
        "bm25_channel_ref": "bm25-lucene-idf.v1",
        "per_channel_candidate_limit": 8,
        "pre_fusion_union_limit": 16,
        "final_candidate_limit": 8,
        "ce_used": False,
        "new_model_used": False,
        "runtime_read_only": True,
        "cases": cases,
    }


def make_case(
    case_id: str,
    query: str,
    candidates: list[tuple[str, str]],
    *,
    retrieval_artifact_id: str | None = None,
) -> dict:
    """Build one evidence case with ranked candidates [(document_id, chunk_id), ...]."""
    items = []
    for rank, (document_id, chunk_id) in enumerate(candidates, start=1):
        channels = ["current-dense-led-ranked.v1"]
        current = rank
        items.append(
            {
                "document_id": document_id,
                "chunk_id": chunk_id,
                "rank": rank,
                "rrf_score": _rrf_score(current=current),
                "source_channels": channels,
                "contributing_channel_count": 1,
                "current_rank": current,
                "bm25_rank": None,
            }
        )
    return {
        "case_id": case_id,
        "query_sha256": sha256_text(query),
        "retrieval_artifact_id": retrieval_artifact_id or f"artifact-{case_id}",
        "retrieval_status": "SUCCEEDED",
        "retrieved_candidate_count": len(candidates),
        "ranked_candidate_count": len(candidates),
        "ranked_candidates": items,
    }
