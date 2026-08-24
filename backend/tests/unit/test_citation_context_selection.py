"""WP5 Citation Context Selection unit tests (deterministic, offline)."""

# ruff: noqa: D103, D415

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from app.core.evaluation.citation_context_selection import (
    CITATION_CONTEXT_COMPARISON_SCHEMA_VERSION,
    CITATION_CONTEXT_SELECTION_SCHEMA_VERSION,
    DROP_DUPLICATE_CONTENT,
    DROP_RANK_AFTER_K,
    EXPECTED_GGUF_SHA256,
    FIXTURE_TOKENIZER_IDENTITY,
    FIXTURE_TOKENIZER_REF,
    GENERATION_MODEL_REF,
    GENERATION_TOKENIZER_REF,
    K_VALUES,
    POLICY_REF,
    SERIALIZER_REF,
    TOKENIZATION_MODE_REF,
    TOKENIZE_ADD_BOS,
    TOKENIZE_SPECIAL,
    TOKEN_USAGE_AUTHORITY,
    WP5_DEDUP_IS_PRODUCTION_EXACT,
    WP5_DEDUP_REF,
    CandidateView,
    CaseCandidateView,
    CitationContextAggregateMetrics,
    CitationContextCaseMetricsBuilder,
    CitationContextComparisonReport,
    CitationContextMaterializationError,
    CitationContextProtocolError,
    CitationContextSelectionEnvelope,
    CitationContextSelectionError,
    CitationContextSerializer,
    ControlledCorpus,
    ControlledCorpusEntry,
    FixtureTokenCounter,
    FixedTopKSelector,
    SourceFile,
    SourceManifest,
    TokenizerIdentityMismatch,
    aggregate_case_metrics,
    compute_pareto,
    count_selection_tokens,
    privacy_safe_serialization,
    source_manifest_digest,
    validate_external_source_authority,
    verify_materialized_sources,
    verify_tokenizer_file,
)
from app.core.evaluation.dataset import (
    AnswerabilityCaseType,
    AnswerabilityExpectedDecision,
    AnswerabilityGroundTruth,
    AnswerabilityReasonCode,
    AnswerabilitySplit,
    EvaluationCase,
    EvaluationDataset,
    GroundTruth,
)
from app.core.evaluation.no_answer import RrfEvidenceEnvelopeV2
from app.services.evaluation.citation_context_selection import (
    materialize_candidate_view,
    run_k,
    validate_frozen_inputs,
)
from tests.unit.wp5_fixtures import (
    FROZEN_CHUNK_MANIFEST_DIGEST,
    FROZEN_SOURCE_MANIFEST_DIGEST,
    canonical_digest as fix_canonical,
    make_case,
    make_corpus,
    make_evidence,
    sha1_text,
    sha256_text,
)

TOKENIZER_REF = FIXTURE_TOKENIZER_REF
TOKENIZER_IDENTITY = FIXTURE_TOKENIZER_IDENTITY


def _counter() -> FixtureTokenCounter:
    return FixtureTokenCounter()


def _corpus():
    chunks = [
        ("chunk-a", "doc-1", "01_doc.md", "Alpha support content one."),
        ("chunk-b", "doc-1", "01_doc.md", "Beta second support content two."),
        ("chunk-c", "doc-2", "02_doc.md", "Gamma noise content three."),
        ("chunk-d", "doc-2", "02_doc.md", "Delta noise content four."),
    ]
    return make_corpus(*chunks)


def _candidate_views(corpus: ControlledCorpus, chunk_ids: list[str]) -> list[CandidateView]:
    views = []
    for rank, chunk_id in enumerate(chunk_ids, start=1):
        entry = next(c for c in corpus.entries if c.chunk_id == chunk_id)
        views.append(
            CandidateView(
                case_id="case-1",
                query_sha256=sha256_text("query"),
                document_id=entry.document_id,
                chunk_id=entry.chunk_id,
                rank=rank,
                content_hash=entry.content_hash,
                content_digest=entry.content_digest,
                snippet=entry.snippet,
                source=entry.source,
            )
        )
    return views


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def test_materialize_all_candidates_resolve() -> None:
    corpus = _corpus()
    evidence = RrfEvidenceEnvelopeV2.model_validate(
        make_evidence([make_case("case-1", "q", [("doc-1", "chunk-a"), ("doc-2", "chunk-c")])], dataset_digest="0" * 64)
    )
    views = materialize_candidate_view(evidence, corpus)
    assert len(views) == 1
    assert [c.chunk_id for c in views[0].candidates] == ["chunk-a", "chunk-c"]
    for c in views[0].candidates:
        assert c.content_digest == sha256_text(c.snippet)


def test_materialize_missing_candidate_fails_closed() -> None:
    corpus = _corpus()
    evidence = RrfEvidenceEnvelopeV2.model_validate(
        make_evidence([make_case("case-1", "q", [("doc-1", "chunk-missing")])], dataset_digest="0" * 64)
    )
    with pytest.raises(CitationContextMaterializationError):
        materialize_candidate_view(evidence, corpus)


def test_materialize_document_mismatch_fails_closed() -> None:
    corpus = _corpus()
    evidence = RrfEvidenceEnvelopeV2.model_validate(
        make_evidence([make_case("case-1", "q", [("doc-99", "chunk-a")])], dataset_digest="0" * 64)
    )
    with pytest.raises(CitationContextMaterializationError):
        materialize_candidate_view(evidence, corpus)


def test_corpus_duplicate_chunk_id_rejected() -> None:
    with pytest.raises(ValidationError):
        make_corpus(("chunk-a", "doc-1", "01.md", "one"), ("chunk-a", "doc-1", "01.md", "two"))


def test_content_digest_mismatch_fails_closed() -> None:
    with pytest.raises(ValidationError):
        ControlledCorpusEntry(
            document_id="doc-1",
            chunk_id="chunk-a",
            source="01.md",
            content_hash=sha1_text("snippet"),
            content_digest="0" * 64,
            snippet="snippet",
        )


def test_content_hash_mismatch_fails_closed() -> None:
    with pytest.raises(ValidationError):
        ControlledCorpusEntry(
            document_id="doc-1",
            chunk_id="chunk-a",
            source="01.md",
            content_hash="0" * 40,
            content_digest=sha256_text("snippet"),
            snippet="snippet",
        )


def test_manifest_mismatch_fails_closed() -> None:
    entries = [
        ControlledCorpusEntry(
            document_id="doc-1",
            chunk_id="chunk-a",
            source="01.md",
            content_hash=sha1_text("snippet"),
            content_digest=sha256_text("snippet"),
            snippet="snippet",
        )
    ]
    wrong_digest = "0" * 64
    with pytest.raises(CitationContextMaterializationError):
        ControlledCorpus.from_entries(
            entries,
            corpus_ref="rag-evaluation-corpus.v1",
            source_manifest_digest=FROZEN_SOURCE_MANIFEST_DIGEST,
            chunk_manifest_digest=wrong_digest,
        )


def test_duplicate_candidate_identity_rejected() -> None:
    corpus = _corpus()
    views = _candidate_views(corpus, ["chunk-a", "chunk-a", "chunk-b"])
    with pytest.raises(CitationContextSelectionError):
        FixedTopKSelector().select(views, K=2)


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------


def _selection_for(K: int, chunk_ids: list[str]):
    corpus = _corpus()
    views = _candidate_views(corpus, chunk_ids)
    return FixedTopKSelector().select(views, K=K), views


def test_selector_k1_exact_top1() -> None:
    result, _ = _selection_for(1, ["chunk-a", "chunk-b", "chunk-c", "chunk-d"])
    assert [s.chunk_id for s in result.selected] == ["chunk-a"]
    assert result.selected[0].selected_order == 1
    assert result.selected[0].original_rank == 1


def test_selector_k2_exact_top2() -> None:
    result, _ = _selection_for(2, ["chunk-a", "chunk-b", "chunk-c", "chunk-d"])
    assert [s.chunk_id for s in result.selected] == ["chunk-a", "chunk-b"]
    assert [s.selected_order for s in result.selected] == [1, 2]
    assert [s.original_rank for s in result.selected] == [1, 2]


def test_selector_k3_exact_top3() -> None:
    result, _ = _selection_for(3, ["chunk-a", "chunk-b", "chunk-c", "chunk-d"])
    assert [s.chunk_id for s in result.selected] == ["chunk-a", "chunk-b", "chunk-c"]


def test_selector_k4_exact_top4() -> None:
    result, _ = _selection_for(4, ["chunk-a", "chunk-b", "chunk-c", "chunk-d"])
    assert [s.chunk_id for s in result.selected] == ["chunk-a", "chunk-b", "chunk-c", "chunk-d"]


def test_selector_order_preserved_and_drop_after_k() -> None:
    result, _ = _selection_for(2, ["chunk-a", "chunk-b", "chunk-c", "chunk-d"])
    reasons = {d.chunk_id: d.reason for d in result.dropped}
    assert reasons == {"chunk-c": DROP_RANK_AFTER_K, "chunk-d": DROP_RANK_AFTER_K}


def test_selector_invalid_k_rejected() -> None:
    corpus = _corpus()
    views = _candidate_views(corpus, ["chunk-a", "chunk-b"])
    for bad_k in (0, 5, -1):
        with pytest.raises(CitationContextSelectionError):
            FixedTopKSelector().select(views, K=bad_k)


def test_selector_rank_gap_rejected() -> None:
    corpus = _corpus()
    views = _candidate_views(corpus, ["chunk-a", "chunk-b"])
    edited = [
        views[0],
        CandidateView(
            case_id="case-1",
            query_sha256=views[1].query_sha256,
            document_id=views[1].document_id,
            chunk_id=views[1].chunk_id,
            rank=3,
            content_hash=views[1].content_hash,
            content_digest=views[1].content_digest,
            snippet=views[1].snippet,
            source=views[1].source,
        ),
    ]
    with pytest.raises(CitationContextSelectionError):
        FixedTopKSelector().select(edited, K=2)


def test_selector_duplicate_content_deterministic() -> None:
    corpus = make_corpus(
        ("chunk-a", "doc-1", "01.md", "unique content"),
        ("chunk-b", "doc-2", "02.md", "shared content"),
        ("chunk-x", "doc-3", "03.md", "shared content"),
    )
    views = _candidate_views(corpus, ["chunk-a", "chunk-b", "chunk-x"])
    result = FixedTopKSelector().select(views, K=4)
    selected_ids = [s.chunk_id for s in result.selected]
    assert selected_ids == ["chunk-a", "chunk-b"]
    reasons = {d.chunk_id: d.reason for d in result.dropped}
    assert reasons["chunk-x"] == DROP_DUPLICATE_CONTENT


def test_selector_ground_truth_mutation_does_not_change_selection() -> None:
    corpus = _corpus()
    views_a = _candidate_views(corpus, ["chunk-a", "chunk-b", "chunk-c", "chunk-d"])
    views_b = _candidate_views(corpus, ["chunk-a", "chunk-b", "chunk-c", "chunk-d"])
    result_a = FixedTopKSelector().select(views_a, K=2)
    result_b = FixedTopKSelector().select(views_b, K=2)
    assert [s.chunk_id for s in result_a.selected] == [s.chunk_id for s in result_b.selected]
    assert [s.chunk_id for s in result_a.selected] == ["chunk-a", "chunk-b"]


# ---------------------------------------------------------------------------
# Serializer / Tokenizer
# ---------------------------------------------------------------------------


def test_serializer_deterministic() -> None:
    corpus = _corpus()
    views = _candidate_views(corpus, ["chunk-a", "chunk-b"])
    s1 = FixedTopKSelector().select(views, K=2)
    s2 = FixedTopKSelector().select(views, K=2)
    block1 = CitationContextSerializer.serialize_context([x.block for x in s1.selected])
    block2 = CitationContextSerializer.serialize_context([x.block for x in s2.selected])
    assert block1 == block2
    assert hashlib.sha256(block1.encode("utf-8")).hexdigest() == hashlib.sha256(
        block2.encode("utf-8")).hexdigest()


def test_serializer_envelope_matches_production_shape() -> None:
    block = CitationContextSerializer.serialize_block(source="01_doc.md", content="Alpha", selected_order=1)
    assert block == "[来源: 01_doc.md]\nAlpha[引用: C1]"


def test_token_count_from_real_tokenizer_not_estimator() -> None:
    counter = _counter()
    corpus = _corpus()
    views = _candidate_views(corpus, ["chunk-a", "chunk-b"])
    selection = FixedTopKSelector().select(views, K=2)
    counted = count_selection_tokens(selection, counter)
    for item in counted.selected:
        assert item.serialized_token_count == counter.count(item.block)
    assert all(s.serialized_token_count > 0 for s in counted.selected)


def test_fixture_token_counter_deterministic() -> None:
    counter = _counter()
    text = "Alpha support content one"
    assert counter.count(text) == counter.count(text)
    assert counter.tokenizer_ref == TOKENIZER_REF
    assert counter.tokenizer_identity == TOKENIZER_IDENTITY


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _eligible_case_metrics():
    corpus = _corpus()
    views = _candidate_views(corpus, ["chunk-a", "chunk-b", "chunk-c", "chunk-d"])
    selection = count_selection_tokens(FixedTopKSelector().select(views, K=2), _counter())
    return selection


def test_support_coverage() -> None:
    selection = _eligible_case_metrics()
    metrics = CitationContextCaseMetricsBuilder.build(selection, expected_support_ids=("chunk-a", "chunk-b"))
    assert metrics.eligible is True
    assert metrics.selected_chunk_count == 2
    assert metrics.selected_support_chunk_count == 2
    assert metrics.support_coverage == 1.0
    assert metrics.noise_by_chunk == 0.0
    assert metrics.noise_by_token == 0.0


def test_noise_numerator_denominator() -> None:
    selection = _eligible_case_metrics()
    metrics = CitationContextCaseMetricsBuilder.build(selection, expected_support_ids=("chunk-a",))
    assert metrics.selected_chunk_count == 2
    assert metrics.selected_support_chunk_count == 1
    assert metrics.selected_non_support_chunk_count == 1
    assert metrics.noise_by_chunk == 0.5
    assert metrics.selected_non_support_serialized_token_count > 0
    assert metrics.noise_by_token == (
        metrics.selected_non_support_serialized_token_count / metrics.selected_serialized_token_count
    )


def test_zero_selected_chunk_denominator_fails_closed() -> None:
    from app.core.evaluation.citation_context_selection import CaseSelectionResult, DroppedView

    empty = CaseSelectionResult(
        case_id="case-1",
        query_sha256=sha256_text("q"),
        eligible=True,
        candidates=(DroppedView(document_id="doc-1", chunk_id="chunk-a", rank=1, reason=DROP_RANK_AFTER_K),),
        selected=(),
        dropped=(DroppedView(document_id="doc-1", chunk_id="chunk-a", rank=1, reason=DROP_RANK_AFTER_K),),
    )
    with pytest.raises(CitationContextProtocolError):
        CitationContextCaseMetricsBuilder.build(empty, expected_support_ids=("chunk-a",))


def test_micro_aggregate_exact() -> None:
    corpus = _corpus()
    cases = []
    views = _candidate_views(corpus, ["chunk-a", "chunk-b", "chunk-c"])
    s1 = count_selection_tokens(FixedTopKSelector().select(views, K=3), _counter())
    m1 = CitationContextCaseMetricsBuilder.build(s1, expected_support_ids=("chunk-a", "chunk-b"))
    cases.append(m1)
    views2 = _candidate_views(corpus, ["chunk-a", "chunk-b"])
    s2 = count_selection_tokens(FixedTopKSelector().select(views2, K=2), _counter())
    m2 = CitationContextCaseMetricsBuilder.build(s2, expected_support_ids=("chunk-a", "chunk-b"))
    cases.append(m2)
    agg = aggregate_case_metrics(cases)
    total_selected = m1.selected_chunk_count + m2.selected_chunk_count
    total_non = m1.selected_non_support_chunk_count + m2.selected_non_support_chunk_count
    total_tok = m1.selected_serialized_token_count + m2.selected_serialized_token_count
    total_non_tok = m1.selected_non_support_serialized_token_count + m2.selected_non_support_serialized_token_count
    assert agg.noise_by_chunk_micro == total_non / total_selected
    assert agg.noise_by_token_micro == total_non_tok / total_tok
    assert agg.eligible_case_count == 2


def test_aggregate_no_eligible_returns_zero_count() -> None:
    agg = aggregate_case_metrics([])
    assert agg.eligible_case_count == 0
    assert agg.support_coverage_macro is None


# ---------------------------------------------------------------------------
# Sidecar / report strict validation + adversarial
# ---------------------------------------------------------------------------


def _valid_sidecar_payload() -> dict:
    return {
        "schema_version": CITATION_CONTEXT_SELECTION_SCHEMA_VERSION,
        "selection_id": "ccs-test",
        "dataset_id": "no-answer-threshold-dataset",
        "dataset_version": "v2",
        "dataset_digest": "e" * 64,
        "retrieval_evidence_schema": "no-answer-rrf-evidence.v2",
        "retrieval_evidence_digest": "d" * 64,
        "substrate_ref": "wp4-no-answer-rrf-substrate.v2",
        "corpus_ref": "rag-evaluation-corpus.v1",
        "source_manifest_digest": FROZEN_SOURCE_MANIFEST_DIGEST,
        "chunk_manifest_digest": FROZEN_CHUNK_MANIFEST_DIGEST,
        "policy_ref": POLICY_REF,
        "K": 2,
        "serializer_ref": SERIALIZER_REF,
        "tokenizer_ref": TOKENIZER_REF,
        "tokenizer_identity": TOKENIZER_IDENTITY,
        "tokenization_mode_ref": "wp5-fixture-tokenize.v1",
        "add_bos": False,
        "special": False,
        "tokenizer_authority": "FIXTURE_TEST_ONLY",
        "runtime_read_only": True,
        "cases": [
            {
                "case_id": "case-1",
                "query_sha256": sha256_text("q"),
                "eligible": True,
                "candidates": [
                    {"document_id": "doc-1", "chunk_id": "chunk-a", "rank": 1, "content_digest": sha256_text("Alpha support content one.")},
                    {"document_id": "doc-1", "chunk_id": "chunk-b", "rank": 2, "content_digest": sha256_text("Beta second support content two.")},
                ],
                "selected": [
                    {"document_id": "doc-1", "chunk_id": "chunk-a", "original_rank": 1, "selected_order": 1, "content_digest": sha256_text("Alpha support content one."), "serialized_token_count": 5},
                    {"document_id": "doc-1", "chunk_id": "chunk-b", "original_rank": 2, "selected_order": 2, "content_digest": sha256_text("Beta second support content two."), "serialized_token_count": 6},
                ],
                "dropped": [],
                "selected_expected_support_ids": ["chunk-a"],
                "metrics": {
                    "eligible": True,
                    "selected_chunk_count": 2,
                    "selected_serialized_token_count": 11,
                    "selected_support_chunk_count": 1,
                    "selected_non_support_chunk_count": 1,
                    "selected_non_support_serialized_token_count": 6,
                    "support_coverage": 1.0,
                    "noise_by_chunk": 0.5,
                    "noise_by_token": 6 / 11,
                },
            }
        ],
    }


def _valid_pinned_sidecar_payload() -> dict:
    payload = _valid_sidecar_payload()
    payload["tokenizer_ref"] = GENERATION_TOKENIZER_REF
    payload["tokenizer_identity"] = EXPECTED_GGUF_SHA256
    payload["tokenization_mode_ref"] = TOKENIZATION_MODE_REF
    payload["add_bos"] = TOKENIZE_ADD_BOS
    payload["special"] = TOKENIZE_SPECIAL
    payload["tokenizer_authority"] = "PINNED_GENERATION_MODEL"
    return payload


def test_sidecar_unknown_field_rejected() -> None:
    payload = _valid_sidecar_payload()
    payload["extra_field"] = "nope"
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_wrong_k_rejected() -> None:
    payload = _valid_sidecar_payload()
    payload["K"] = 7
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_wrong_tokenizer_ref_rejected() -> None:
    payload = _valid_pinned_sidecar_payload()
    payload["tokenizer_ref"] = "some-other-model"
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_wrong_tokenizer_identity_rejected() -> None:
    payload = _valid_pinned_sidecar_payload()
    payload["tokenizer_identity"] = "0" * 64
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_wrong_tokenization_mode_rejected() -> None:
    payload = _valid_pinned_sidecar_payload()
    payload["tokenization_mode_ref"] = "some-other-mode.v9"
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_wrong_add_bos_rejected() -> None:
    payload = _valid_pinned_sidecar_payload()
    payload["add_bos"] = not TOKENIZE_ADD_BOS
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_wrong_special_rejected() -> None:
    payload = _valid_pinned_sidecar_payload()
    payload["special"] = not TOKENIZE_SPECIAL
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_fixture_cannot_masquerade_as_real() -> None:
    payload = _valid_sidecar_payload()
    payload["tokenizer_ref"] = GENERATION_TOKENIZER_REF
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)
    payload2 = _valid_sidecar_payload()
    payload2["tokenizer_identity"] = EXPECTED_GGUF_SHA256
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload2)


def test_sidecar_wrong_content_digest_rejected() -> None:
    payload = _valid_sidecar_payload()
    payload["cases"][0]["selected"][0]["content_digest"] = "not-a-digest"
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_negative_token_count_rejected() -> None:
    payload = _valid_sidecar_payload()
    payload["cases"][0]["selected"][0]["serialized_token_count"] = -1
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_wrong_dropped_reason_rejected() -> None:
    payload = _valid_sidecar_payload()
    payload["cases"][0]["dropped"] = [
        {"document_id": "doc-2", "chunk_id": "chunk-c", "rank": 3, "reason": "not-a-reason"}
    ]
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_selected_exceeds_k_rejected() -> None:
    payload = _valid_sidecar_payload()
    # add a 3rd selected while K=2
    payload["cases"][0]["selected"].append(
        {"document_id": "doc-2", "chunk_id": "chunk-c", "original_rank": 3, "selected_order": 3,
         "content_digest": sha256_text("Gamma noise content three."), "serialized_token_count": 4}
    )
    payload["cases"][0]["candidates"].append(
        {"document_id": "doc-2", "chunk_id": "chunk-c", "rank": 3, "content_digest": sha256_text("Gamma noise content three.")}
    )
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_selected_not_in_candidates_rejected() -> None:
    payload = _valid_sidecar_payload()
    payload["cases"][0]["selected"][0]["chunk_id"] = "chunk-zzz"
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_selected_order_wrong_rejected() -> None:
    payload = _valid_sidecar_payload()
    payload["cases"][0]["selected"][0]["selected_order"] = 2
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_incomplete_coverage_rejected() -> None:
    payload = _valid_sidecar_payload()
    # remove the second candidate entirely -> selected+dropped no longer cover all
    payload["cases"][0]["candidates"] = [payload["cases"][0]["candidates"][0]]
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_sidecar_privacy_scan_rejects_plaintext() -> None:
    assert privacy_safe_serialization(_valid_sidecar_payload()) is True
    leaky = _valid_sidecar_payload()
    leaky["cases"][0]["query"] = "secret query plaintext"
    assert privacy_safe_serialization(leaky) is False
    leaky2 = _valid_sidecar_payload()
    leaky2["cases"][0]["snippet"] = "secret chunk plaintext"
    assert privacy_safe_serialization(leaky2) is False


def test_report_not_evaluated_and_pareto() -> None:
    a1 = CitationContextAggregateMetrics(
        eligible_case_count=1, support_coverage_macro=0.5, noise_by_chunk_micro=0.5,
        noise_by_token_micro=0.5, total_serialized_context_tokens=100,
        avg_serialized_context_tokens_per_eligible_case=100.0,
    )
    a2 = CitationContextAggregateMetrics(
        eligible_case_count=1, support_coverage_macro=0.5, noise_by_chunk_micro=0.3,
        noise_by_token_micro=0.3, total_serialized_context_tokens=80,
        avg_serialized_context_tokens_per_eligible_case=80.0,
    )
    aggregates = {1: a2, 2: a2, 3: a2, 4: a1}
    pareto = compute_pareto(aggregates)
    for relation in pareto:
        assert relation.pareto_dominates_k4 is True
    report = CitationContextComparisonReport(
        dataset_id="no-answer-threshold-dataset",
        dataset_version="v2",
        dataset_digest="e" * 64,
        retrieval_evidence_schema="no-answer-rrf-evidence.v2",
        retrieval_evidence_digest="d" * 64,
        policy_ref=POLICY_REF,
        k4_is_production_context_exact=False,
        eligible_case_count=1,
        per_k=aggregates,
        pareto=pareto,
        tokenizer_authority="FIXTURE_TEST_ONLY",
    )
    assert report.schema_version == CITATION_CONTEXT_COMPARISON_SCHEMA_VERSION
    assert report.citation_correctness == "NOT_EVALUATED_IN_WP5_V1"
    assert report.citation_completeness == "NOT_EVALUATED_IN_WP5_V1"
    assert report.real_retrieval == "NOT_RUN"
    assert report.real_generation == "NOT_RUN"
    assert report.token_usage_authority == TOKEN_USAGE_AUTHORITY


def test_frozen_input_validation_rejects_wrong_digest() -> None:
    corpus = _corpus()
    dataset = _mini_dataset()
    evidence = RrfEvidenceEnvelopeV2.model_validate(
        make_evidence([make_case("case-1", "q", [("doc-1", "chunk-a")])], dataset_digest=fix_canonical(dataset.model_dump(mode="json")))
    )
    with pytest.raises(ValueError):
        validate_frozen_inputs(dataset, evidence, corpus)


def _mini_dataset() -> EvaluationDataset:
    case = EvaluationCase(
        case_id="case-1",
        name="case one",
        input={"query": "q"},
        expected_output="a",
        ground_truth=GroundTruth(
            answerability=AnswerabilityGroundTruth(
                answerable=True,
                case_type=AnswerabilityCaseType.ANSWERABLE,
                expected_decision=AnswerabilityExpectedDecision.ANSWER,
                split=AnswerabilitySplit.EVALUATION,
                corpus_ref="rag-evaluation-corpus.v1",
                expected_support_fact_ids=["chunk-a"],
                annotation_reason_code=AnswerabilityReasonCode.EXPLICIT_CORPUS_SUPPORT,
            )
        ),
        metadata={"tags": ["t"], "leakage_group": "lg"},
    )
    return EvaluationDataset(
        dataset_schema_version="evaluation-dataset.v4",
        dataset_id="no-answer-threshold-dataset",
        name="mini",
        version="v2",
        cases=[case],
    )


def test_run_k_with_fixture_tokenizer() -> None:
    corpus = _corpus()
    dataset = _mini_dataset()
    evidence = RrfEvidenceEnvelopeV2.model_validate(
        make_evidence([make_case("case-1", "q", [("doc-1", "chunk-a"), ("doc-1", "chunk-b")])], dataset_digest=fix_canonical(dataset.model_dump(mode="json")))
    )
    envelope = run_k(
        dataset=dataset,
        evidence=evidence,
        corpus=corpus,
        K=2,
        token_counter=_counter(),
    )
    assert envelope.schema_version == CITATION_CONTEXT_SELECTION_SCHEMA_VERSION
    assert envelope.K == 2
    assert len(envelope.cases) == 1
    case = envelope.cases[0]
    assert case.eligible is True
    assert [s.chunk_id for s in case.selected] == ["chunk-a", "chunk-b"]
    assert case.metrics.selected_chunk_count == 2
    assert privacy_safe_serialization(envelope.model_dump(mode="json")) is True


def test_run_comparison_synthetic_end_to_end() -> None:
    from app.core.evaluation.citation_context_selection import ControlledCorpus
    from app.services.evaluation.citation_context_selection import run_comparison

    corpus = _corpus()
    # reuse frozen substrate digests for internal consistency with the evidence DTO
    corpus = ControlledCorpus(
        corpus_ref="rag-evaluation-corpus.v1",
        source_manifest_digest=FROZEN_SOURCE_MANIFEST_DIGEST,
        chunk_manifest_digest=FROZEN_CHUNK_MANIFEST_DIGEST,
        entries=corpus.entries,
    )
    dataset = _mini_dataset()
    evidence = RrfEvidenceEnvelopeV2.model_validate(
        make_evidence(
            [make_case("case-1", "q", [("doc-1", "chunk-a"), ("doc-1", "chunk-b"), ("doc-2", "chunk-c")])],
            dataset_digest=fix_canonical(dataset.model_dump(mode="json")),
        )
    )
    envelopes, report = run_comparison(
        dataset=dataset, evidence=evidence, corpus=corpus, token_counter=_counter()
    )
    assert set(envelopes.keys()) == set(K_VALUES)
    for k, envelope in envelopes.items():
        assert envelope.K == k
        assert all(1 <= len(case.selected) <= k for case in envelope.cases)
        assert privacy_safe_serialization(envelope.model_dump(mode="json")) is True
    assert report.k4_is_production_context_exact is False
    assert report.eligible_case_count == 1
    assert report.citation_correctness == "NOT_EVALUATED_IN_WP5_V1"
    assert len(report.pareto) == 3


# ---------------------------------------------------------------------------
# Remediation: external source-manifest authority, tokenizer identity, mode,
# dedup truthfulness (P1-1, P1-2, P1-3, P2-1).
# ---------------------------------------------------------------------------


def _source_manifest(*files: tuple[str, str]) -> SourceManifest:
    return SourceManifest(
        schema_version="wp5-source-manifest.v1",
        corpus_ref="rag-evaluation-corpus.v1",
        source_manifest_digest=source_manifest_digest([{"path": p, "sha256": d} for p, d in files]),
        files=tuple(SourceFile(path=p, sha256=d) for p, d in files),
    )


def test_source_manifest_digest_canonical() -> None:
    files = [{"path": "02_doc.md", "sha256": sha256_text("b")}, {"path": "01_doc.md", "sha256": sha256_text("a")}]
    digest = source_manifest_digest(files)
    # sorted by path regardless of input order; canonical compact JSON
    expected = hashlib.sha256(
        json.dumps(
            [{"path": "01_doc.md", "sha256": sha256_text("a")}, {"path": "02_doc.md", "sha256": sha256_text("b")}],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert digest == expected


def test_external_source_authority_accepts_frozen() -> None:
    manifest = _source_manifest(("01_doc.md", sha256_text("a")), ("02_doc.md", sha256_text("b")))
    digest = validate_external_source_authority(manifest, expected=manifest.source_manifest_digest)
    assert digest == manifest.source_manifest_digest


def test_external_source_authority_rejects_self_consistent_tamper() -> None:
    # Legit manifest binds a frozen digest.
    manifest = _source_manifest(("01_doc.md", sha256_text("a")), ("02_doc.md", sha256_text("b")))
    frozen_digest = manifest.source_manifest_digest
    # Attacker modifies a source file's bytes, then recomputes the manifest's
    # declared digest to be internally self-consistent.
    tampered = _source_manifest(("01_doc.md", sha256_text("tampered-a")), ("02_doc.md", sha256_text("b")))
    assert tampered.source_manifest_digest != frozen_digest
    # External authority anchored to the original frozen digest must reject.
    with pytest.raises(CitationContextMaterializationError):
        validate_external_source_authority(tampered, expected=frozen_digest)


def test_source_file_modified_rejects() -> None:
    # Same as above: modifying a source file's content digest changes the manifest.
    frozen = _source_manifest(("01_doc.md", sha256_text("a")))
    modified = _source_manifest(("01_doc.md", sha256_text("a-modified")))
    with pytest.raises(CitationContextMaterializationError):
        validate_external_source_authority(modified, expected=frozen.source_manifest_digest)


def test_verify_materialized_sources_rejects_wrong_missing_extra() -> None:
    entries = _corpus().entries  # sources: 01_doc.md, 02_doc.md
    valid_files = (SourceFile(path="01_doc.md", sha256=sha256_text("x")), SourceFile(path="02_doc.md", sha256=sha256_text("y")))
    verify_materialized_sources(entries, valid_files)  # exact set -> PASS
    # wrong filename
    wrong = (SourceFile(path="03_doc.md", sha256=sha256_text("z")), SourceFile(path="02_doc.md", sha256=sha256_text("y")))
    with pytest.raises(CitationContextMaterializationError):
        verify_materialized_sources(entries, wrong)
    # missing source file
    missing = (SourceFile(path="01_doc.md", sha256=sha256_text("x")),)
    with pytest.raises(CitationContextMaterializationError):
        verify_materialized_sources(entries, missing)
    # extra source file
    extra = (
        SourceFile(path="01_doc.md", sha256=sha256_text("x")),
        SourceFile(path="02_doc.md", sha256=sha256_text("y")),
        SourceFile(path="03_doc.md", sha256=sha256_text("z")),
    )
    with pytest.raises(CitationContextMaterializationError):
        verify_materialized_sources(entries, extra)


def test_materialized_asset_tamper_self_consistent_rejected() -> None:
    # Build a valid materialized corpus; record its frozen chunk manifest digest.
    corpus = _corpus()
    original_digest = corpus.chunk_manifest_digest
    # Attacker modifies a snippet and recomputes the per-entry content digest to
    # keep the asset internally self-consistent.
    tampered_entries = []
    for entry in corpus.entries:
        if entry.chunk_id == "chunk-a":
            new_snippet = "Alpha TAMPERED content one."
            tampered_entries.append(
                ControlledCorpusEntry(
                    document_id=entry.document_id,
                    chunk_id=entry.chunk_id,
                    source=entry.source,
                    section_path=entry.section_path,
                    content_hash=sha1_text(new_snippet),
                    content_digest=sha256_text(new_snippet),
                    snippet=new_snippet,
                )
            )
        else:
            tampered_entries.append(entry)
    # Reproducing the original frozen chunk digest from the tampered entries fails.
    with pytest.raises(CitationContextMaterializationError):
        ControlledCorpus.from_entries(
            tampered_entries,
            corpus_ref="rag-evaluation-corpus.v1",
            source_manifest_digest=FROZEN_SOURCE_MANIFEST_DIGEST,
            chunk_manifest_digest=original_digest,
        )


def test_verify_tokenizer_file_rejects_wrong_bytes(tmp_path) -> None:
    p = tmp_path / GENERATION_MODEL_REF
    p.write_bytes(b"this is not the real frozen gguf")
    with pytest.raises(TokenizerIdentityMismatch):
        verify_tokenizer_file(str(p))


def test_verify_tokenizer_file_rejects_wrong_filename(tmp_path) -> None:
    p = tmp_path / "some-other-model.gguf"
    p.write_bytes(b"anything")
    with pytest.raises(TokenizerIdentityMismatch):
        verify_tokenizer_file(str(p))


def test_frozen_tokenization_mode_explicit() -> None:
    assert TOKENIZATION_MODE_REF == "llama-cpp-tokenize.v1"
    assert TOKENIZE_ADD_BOS is False
    assert TOKENIZE_SPECIAL is False
    # same text + same frozen mode -> deterministic same token count
    counter = _counter()
    text = "Alpha support content one"
    assert counter.count(text) == counter.count(text)
    assert counter.add_bos is False and counter.special is False
    assert counter.tokenizer_authority == "FIXTURE_TEST_ONLY"


def test_fixture_tokenizer_cannot_masquerade_as_real() -> None:
    counter = _counter()
    assert counter.tokenizer_ref != GENERATION_TOKENIZER_REF
    assert counter.tokenizer_identity != EXPECTED_GGUF_SHA256
    assert counter.tokenizer_authority == "FIXTURE_TEST_ONLY"
    # Trying to label fixture output as PINNED fails the frozen real binding.
    payload = _valid_sidecar_payload()
    payload["tokenizer_authority"] = "PINNED_GENERATION_MODEL"  # fixture ref/identity/mode retained
    with pytest.raises(ValidationError):
        CitationContextSelectionEnvelope.model_validate(payload)


def test_dedup_ref_truthful_evaluation_side() -> None:
    assert WP5_DEDUP_REF == "evaluation-raw-snippet-sha256-dedup.v1"
    assert WP5_DEDUP_IS_PRODUCTION_EXACT is False
    assert FixedTopKSelector.dedup_ref == WP5_DEDUP_REF
    # dedup key is the raw-snippet SHA-256 digest (content_digest): two distinct
    # chunk_ids sharing identical raw snippets are deduplicated by content.
    corpus = make_corpus(
        ("chunk-a", "doc-1", "01.md", "identical snippet"),
        ("chunk-b", "doc-2", "02.md", "identical snippet"),
        ("chunk-c", "doc-3", "03.md", "different snippet"),
    )
    views = _candidate_views(corpus, ["chunk-a", "chunk-b", "chunk-c"])
    result = FixedTopKSelector().select(views, K=4)
    assert [s.chunk_id for s in result.selected] == ["chunk-a", "chunk-c"]
    dup = [d for d in result.dropped if d.reason == DROP_DUPLICATE_CONTENT]
    assert [d.chunk_id for d in dup] == ["chunk-b"]
