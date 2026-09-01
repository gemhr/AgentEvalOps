"""RAG Evaluation Artifact v1 consumer DTO / Evidence mapping focused tests."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.core.evaluation.rag_artifact import (
    RAG_ARTIFACT_EVIDENCE_KIND,
    RAG_ARTIFACT_EVIDENCE_SCHEMA_VERSION,
    RAG_ARTIFACT_MEDIA_TYPE,
    RAG_ARTIFACT_SCHEMA_VERSION,
    RAG_ARTIFACT_SCHEMA_VERSION_V2,
    RagEvaluationArtifactV1,
    build_rag_artifact_evidence,
)
from app.infrastructure.db.repositories.evaluation_persistence_repo import (
    _evidence,
    _evidence_from,
)

RUN_ID = "11111111-1111-4111-8111-111111111111"


def _item() -> dict[str, object]:
    return {
        "document_id": "source-stable",
        "chunk_id": "c0",
        "rank": 1,
        "retrieval_rank": 1,
        "rerank_rank": 1,
        "retrieval_score": 0.9,
        "retrieval_score_kind": "VECTOR_NORMALIZED_RELEVANCE",
        "retrieval_channels": ["VECTOR_REWRITTEN_QUERY"],
        "rerank_score": 0.9,
        "rerank_score_kind": "HEURISTIC_RERANK",
        "source": {
            "source_type": "md",
            "collection": "kb",
            "display_name": "x.md",
            "document_version": "v1",
        },
        "page": 1,
        "section": "S",
        "sheet": None,
        "content_hash": "abc",
        "selected": True,
    }


def artifact_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": RAG_ARTIFACT_SCHEMA_VERSION,
        "artifact_id": f"rag-eval://{RUN_ID}/r1",
        "run_id": RUN_ID,
        "attempt_id": RUN_ID,
        "retrieval_id": "r1",
        "invocation_index": 1,
        "retrieval_status": "SUCCEEDED",
        "query": "q",
        "rewritten_query": "rw",
        "retrieved_items": [_item()],
        "ranked_items": [_item()],
        "selected_items": [
            {
                "document_id": "source-stable",
                "chunk_id": "c0",
                "selection_rank": 1,
                "context_block_id": "context-1",
                "citation_id": "Rr1-1",
                "context_content_hash": "xyz",
                "text": "evidence",
            }
        ],
        "citations": [
            {
                "citation_id": "Rr1-1",
                "document_id": "source-stable",
                "chunk_id": "c0",
                "context_block_id": "context-1",
                "context_content_hash": "xyz",
                "display_label": "x.md",
                "page": 1,
                "section": "S",
            }
        ],
        "retrieval_latency_ms": 5,
        "rerank_latency_ms": 3,
        "total_latency_ms": 20,
        "degraded": False,
        "degradation_reasons": [],
        "error": None,
        "budget_usage": {
            "retrieval_calls": 1,
            "embedding_calls": 2,
            "vector_queries": 2,
            "keyword_queries": 1,
            "document_reads": 1,
            "context_chars": 100,
        },
    }
    payload.update(changes)
    return payload


def test_valid_artifact_parses_and_preserves_identity() -> None:
    artifact = RagEvaluationArtifactV1.model_validate(artifact_payload())
    assert artifact.schema_version == RAG_ARTIFACT_SCHEMA_VERSION
    assert artifact.artifact_id == f"rag-eval://{RUN_ID}/r1"
    assert artifact.run_id == RUN_ID
    assert artifact.retrieval_id == "r1"
    assert artifact.invocation_index == 1
    assert artifact.retrieval_status == "SUCCEEDED"


def test_missing_required_field_rejected() -> None:
    payload = artifact_payload()
    payload.pop("query")
    with pytest.raises(ValidationError):
        RagEvaluationArtifactV1.model_validate(payload)


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        RagEvaluationArtifactV1.model_validate(artifact_payload(unexpected=1))


def test_extra_field_in_nested_item_rejected() -> None:
    payload = artifact_payload()
    payload["retrieved_items"] = [{**_item(), "raw": "leak"}]
    with pytest.raises(ValidationError, match="Extra inputs"):
        RagEvaluationArtifactV1.model_validate(payload)


def test_unsupported_schema_version_rejected() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        RagEvaluationArtifactV1.model_validate(
            artifact_payload(schema_version="rag-evaluation-artifact.v9")
        )


def test_invalid_retrieval_status_rejected() -> None:
    with pytest.raises(ValidationError, match="retrieval_status"):
        RagEvaluationArtifactV1.model_validate(artifact_payload(retrieval_status="BOGUS"))


@pytest.mark.parametrize("retrieval_status", ["SUCCEEDED", "EMPTY", "DEGRADED", "FAILED", "TIMED_OUT", "CANCELLED"])
def test_all_retrieval_statuses_accepted(retrieval_status: str) -> None:
    artifact = RagEvaluationArtifactV1.model_validate(
        artifact_payload(retrieval_status=retrieval_status)
    )
    assert artifact.retrieval_status == retrieval_status


def test_invalid_artifact_id_format_rejected() -> None:
    with pytest.raises(ValidationError, match="artifact_id"):
        RagEvaluationArtifactV1.model_validate(artifact_payload(artifact_id="http://x/y"))


def test_artifact_id_mismatch_rejected() -> None:
    payload = artifact_payload()
    payload["artifact_id"] = f"rag-eval://{RUN_ID}/other"
    with pytest.raises(ValidationError, match="artifact_id"):
        RagEvaluationArtifactV1.model_validate(payload)


def test_attempt_id_must_equal_run_id() -> None:
    with pytest.raises(ValidationError, match="attempt_id"):
        RagEvaluationArtifactV1.model_validate(
            artifact_payload(attempt_id="22222222-2222-4222-8222-222222222222")
        )


def test_invocation_index_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        RagEvaluationArtifactV1.model_validate(artifact_payload(invocation_index=0))


def test_selected_not_in_retrieved_rejected() -> None:
    payload = artifact_payload()
    payload["selected_items"] = [
        {
            "document_id": "other-source",
            "chunk_id": "other",
            "selection_rank": 1,
            "context_block_id": "context-1",
            "citation_id": "Rr1-1",
            "context_content_hash": "xyz",
            "text": "evidence",
        }
    ]
    with pytest.raises(ValidationError, match="identity invariant"):
        RagEvaluationArtifactV1.model_validate(payload)


def test_build_evidence_metadata_roundtrip_via_repo_mapping() -> None:
    artifact = RagEvaluationArtifactV1.model_validate(artifact_payload())
    ref = build_rag_artifact_evidence(artifact, "COMPLETE")
    assert ref.kind == RAG_ARTIFACT_EVIDENCE_KIND
    assert ref.identifier == artifact.artifact_id
    assert ref.media_type == RAG_ARTIFACT_MEDIA_TYPE
    assert ref.schema_version == RAG_ARTIFACT_EVIDENCE_SCHEMA_VERSION

    meta = dict(ref.metadata)
    assert meta["artifact_schema_version"] == RAG_ARTIFACT_SCHEMA_VERSION
    assert meta["artifact_id"] == artifact.artifact_id
    assert meta["retrieval_status"] == "SUCCEEDED"
    assert meta["capture_status"] == "COMPLETE"
    assert meta["payload"]["artifact_id"] == artifact.artifact_id
    assert meta["payload"]["schema_version"] == RAG_ARTIFACT_SCHEMA_VERSION

    # EvidenceRef 必须能通过现有 repo 序列化映射往返，保留 schema_version / artifact_id / payload。
    wire = _evidence(ref)
    restored = _evidence_from(wire)
    assert restored == ref
    assert restored.metadata["artifact_id"] == artifact.artifact_id
    assert restored.metadata["artifact_schema_version"] == RAG_ARTIFACT_SCHEMA_VERSION
    assert restored.metadata["capture_status"] == "COMPLETE"


@pytest.mark.parametrize("capture_status", ["COMPLETE", "PARTIAL", "FAILED"])
def test_build_evidence_accepts_all_capture_statuses(capture_status: str) -> None:
    artifact = RagEvaluationArtifactV1.model_validate(artifact_payload())
    ref = build_rag_artifact_evidence(artifact, capture_status)
    assert ref.metadata["capture_status"] == capture_status


def test_build_evidence_rejects_unknown_capture_status() -> None:
    artifact = RagEvaluationArtifactV1.model_validate(artifact_payload())
    with pytest.raises(ValueError, match="capture_status"):
        build_rag_artifact_evidence(artifact, "BOGUS")


# ---------------------------------------------------------------------------
# Stage5-Phase6-WP1 artifact v2 consumer acceptance（v1 兼容保留）
# ---------------------------------------------------------------------------


def v2_payload(**changes: object) -> dict[str, object]:
    payload = artifact_payload(schema_version=RAG_ARTIFACT_SCHEMA_VERSION_V2)
    payload["retrieval_strategy"] = "HYBRID_RRF"
    payload["provenance_sha256"] = "a" * 64
    payload.update(changes)
    return payload


def test_v2_artifact_accepted_with_strategy_and_provenance() -> None:
    artifact = RagEvaluationArtifactV1.model_validate(v2_payload())
    assert artifact.schema_version == RAG_ARTIFACT_SCHEMA_VERSION_V2
    assert artifact.retrieval_strategy == "HYBRID_RRF"
    assert artifact.provenance_sha256 == "a" * 64


def test_v2_bm25_channel_and_score_kind_accepted() -> None:
    payload = v2_payload()
    payload["retrieved_items"] = [
        {
            **_item(),
            "retrieval_score_kind": "BM25_RAW_SCORE",
            "retrieval_channels": ["BM25"],
            "rerank_score_kind": None,
            "rerank_score": None,
            "rerank_rank": None,
        }
    ]
    payload["ranked_items"] = [
        {
            **_item(),
            "retrieval_score_kind": "RRF_SCORE",
            "retrieval_channels": ["BM25", "RRF"],
            "rerank_score_kind": None,
            "rerank_score": None,
            "rerank_rank": None,
        }
    ]
    artifact = RagEvaluationArtifactV1.model_validate(payload)
    assert artifact.retrieved_items[0].retrieval_score_kind == "BM25_RAW_SCORE"
    assert artifact.retrieved_items[0].retrieval_channels == ["BM25"]
    assert artifact.ranked_items[0].retrieval_score_kind == "RRF_SCORE"
    assert artifact.ranked_items[0].retrieval_channels == ["BM25", "RRF"]


def test_v2_unknown_channel_rejected() -> None:
    payload = v2_payload()
    payload["retrieved_items"] = [{**_item(), "retrieval_channels": ["CROSS_ENCODER"]}]
    with pytest.raises(ValidationError, match="retrieval_channels"):
        RagEvaluationArtifactV1.model_validate(payload)


def test_v2_unknown_score_kind_rejected() -> None:
    payload = v2_payload()
    payload["retrieved_items"] = [{**_item(), "retrieval_score_kind": "CROSS_ENCODER_SCORE"}]
    with pytest.raises(ValidationError, match="retrieval_score_kind"):
        RagEvaluationArtifactV1.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retrieval_score_kind", "VECTOR"),
        ("retrieval_channels", ["KEYWORD"]),
        ("rerank_score_kind", "VECTOR"),
    ],
)
def test_v2_legacy_shorthand_rejected(field: str, value: object) -> None:
    payload = v2_payload()
    payload["retrieved_items"] = [{**_item(), field: value}]
    with pytest.raises(ValidationError, match="v2 does not accept legacy"):
        RagEvaluationArtifactV1.model_validate(payload)


def test_v2_missing_provenance_allowed_for_baseline_truthfulness() -> None:
    """v2 schema 允许 provenance_sha256 缺失（BASELINE 不产生 Hybrid provenance）。"""
    payload = artifact_payload(schema_version=RAG_ARTIFACT_SCHEMA_VERSION_V2)
    payload["retrieval_strategy"] = "BASELINE"
    artifact = RagEvaluationArtifactV1.model_validate(payload)
    assert artifact.retrieval_strategy == "BASELINE"
    assert artifact.provenance_sha256 is None


def test_v2_evidence_roundtrip_preserves_strategy() -> None:
    artifact = RagEvaluationArtifactV1.model_validate(v2_payload())
    ref = build_rag_artifact_evidence(artifact, "COMPLETE")
    meta = dict(ref.metadata)
    assert meta["payload"]["schema_version"] == RAG_ARTIFACT_SCHEMA_VERSION_V2
    assert meta["payload"]["retrieval_strategy"] == "HYBRID_RRF"
    assert meta["payload"]["provenance_sha256"] == "a" * 64
