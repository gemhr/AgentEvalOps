"""WP5 evaluation-only Citation Context Selection orchestration.

Builds the validated candidate view from the frozen WP4 RRF evidence + frozen
controlled corpus, runs the label-blind ``fixed-top-k.v1`` selector for
``K in {1,2,3,4}``, counts serialized tokens with the pinned tokenizer, computes
support-coverage / noise / token metrics over eligible cases, and emits strict
``citation-context-selection.v1`` sidecars plus a deterministic comparison
report. It never re-runs retrieval or generation and never selects a winner.
"""

# ruff: noqa: D101, D102, D415

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from app.core.evaluation.citation_context_selection import (
    EXPECTED_CHUNK_MANIFEST_DIGEST,
    K_VALUES,
    POLICY_REF,
    SERIALIZER_REF,
    CandidateView,
    CaseCandidateView,
    CaseSelectionResult,
    CitationContextAggregateMetrics,
    CitationContextCandidate,
    CitationContextCaseMetricsBuilder,
    CitationContextCaseSidecar,
    CitationContextComparisonReport,
    CitationContextDropped,
    CitationContextSelected,
    CitationContextSelectionEnvelope,
    ControlledCorpus,
    ControlledCorpusEntry,
    FixedTopKSelector,
    SourceManifest,
    TokenCounter,
    aggregate_case_metrics,
    canonical_digest,
    compute_pareto,
    count_selection_tokens,
    privacy_safe_serialization,
    validate_external_source_authority,
    verify_materialized_sources,
)
from app.core.evaluation.dataset import AnswerabilityExpectedDecision, EvaluationDataset
from app.core.evaluation.no_answer import RrfEvidenceEnvelopeV2

REAL_EVIDENCE_CANONICAL_DIGEST = "48d397d0d5c3e972a6c8bc62b05237904717c2d494db88f515a8407367f6b28d"
DATASET_CANONICAL_DIGEST = "e0042be4e1611eddc209159c8bd598dfa0637285a9b96a237f053a00fad8f9dd"


def load_source_manifest(path: str | Path) -> SourceManifest:
    """Load the independent frozen source-manifest asset and verify its digest."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("source manifest asset must be a JSON object")
    return SourceManifest.model_validate(payload)


def load_controlled_corpus(
    materialized_path: str | Path,
    source_manifest_path: str | Path,
) -> ControlledCorpus:
    """Load the materialized corpus against the independent frozen source authority.

    The source-manifest digest is NOT read from the materialized asset (which
    would be self-authorization). It is recomputed from the independent source
    manifest and checked against the hardcoded frozen source digest; the chunk
    manifest digest is pinned to the hardcoded frozen constant and reproduced
    from the entries. The materialized source set must exactly match the source
    manifest file set (wrong/missing/extra source file -> fail closed).
    """
    payload = json.loads(Path(materialized_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("controlled corpus asset must be a JSON object")
    if payload.get("schema_version") != "wp5-controlled-corpus.materialized.v1":
        raise ValueError("unsupported controlled corpus asset schema_version")
    entries = [
        ControlledCorpusEntry(
            document_id=item["document_id"],
            chunk_id=item["chunk_id"],
            source=item["source"],
            section_path=item.get("section_path"),
            content_hash=item["content_hash"],
            content_digest=item["content_digest"],
            snippet=item["snippet"],
        )
        for item in payload["entries"]
    ]
    manifest = load_source_manifest(source_manifest_path)
    source_digest = validate_external_source_authority(manifest)
    verify_materialized_sources(entries, manifest.files)
    # chunk manifest digest is pinned to the hardcoded frozen constant and
    # reproduced from the entries by from_entries (any entry tamper -> reject).
    return ControlledCorpus.from_entries(
        entries,
        corpus_ref=payload["corpus_ref"],
        source_manifest_digest=source_digest,
        chunk_manifest_digest=EXPECTED_CHUNK_MANIFEST_DIGEST,
    )



def validate_frozen_inputs(
    dataset: EvaluationDataset,
    evidence: RrfEvidenceEnvelopeV2,
    corpus: ControlledCorpus,
) -> None:
    """Fail closed unless evidence/dataset/corpus bind the frozen WP4 identities."""
    if canonical_digest(evidence.model_dump(mode="json")) != REAL_EVIDENCE_CANONICAL_DIGEST:
        raise ValueError("retrieval evidence does not match frozen WP4 REAL_EVIDENCE_CANONICAL_DIGEST")
    if canonical_digest(dataset.model_dump(mode="json")) != DATASET_CANONICAL_DIGEST:
        raise ValueError("dataset does not match frozen WP4 DATASET_CANONICAL_DIGEST")
    if evidence.source_manifest_digest != corpus.source_manifest_digest:
        raise ValueError("evidence/corpus source manifest digest mismatch")
    if evidence.chunk_manifest_digest != corpus.chunk_manifest_digest:
        raise ValueError("evidence/corpus chunk manifest digest mismatch")



def materialize_candidate_view(
    evidence: RrfEvidenceEnvelopeV2,
    corpus: ControlledCorpus,
) -> list[CaseCandidateView]:
    """Resolve every ranked candidate to corpus plaintext; fail closed on any miss."""
    cases: list[CaseCandidateView] = []
    for case in evidence.cases:
        candidates: list[CandidateView] = []
        for item in case.ranked_candidates:
            entry = corpus.materialize(document_id=item.document_id, chunk_id=item.chunk_id)
            candidates.append(
                CandidateView(
                    case_id=case.case_id,
                    query_sha256=case.query_sha256,
                    document_id=item.document_id,
                    chunk_id=item.chunk_id,
                    rank=item.rank,
                    content_hash=entry.content_hash,
                    content_digest=entry.content_digest,
                    snippet=entry.snippet,
                    source=entry.source,
                )
            )
        cases.append(
            CaseCandidateView(
                case_id=case.case_id,
                query_sha256=case.query_sha256,
                candidates=tuple(candidates),
            )
        )
    return cases


def _case_eligible(dataset: EvaluationDataset, case_id: str) -> tuple[bool, tuple[str, ...]]:
    for case in dataset.cases:
        if case.case_id != case_id:
            continue
        truth = case.ground_truth.answerability
        if truth is None:
            return False, ()
        eligible = (
            truth.answerable
            and truth.expected_decision == AnswerabilityExpectedDecision.ANSWER
            and bool(truth.expected_support_fact_ids)
        )
        return eligible, tuple(truth.expected_support_fact_ids)
    return False, ()


def _project_case(
    case_view: CaseCandidateView,
    selection: CaseSelectionResult,
    *,
    eligible: bool,
    expected_support_ids: tuple[str, ...],
) -> CitationContextCaseSidecar:
    candidates = tuple(
        CitationContextCandidate(
            document_id=c.document_id,
            chunk_id=c.chunk_id,
            rank=c.rank,
            content_digest=c.content_digest,
        )
        for c in case_view.candidates
    )
    selected = tuple(
        CitationContextSelected(
            document_id=s.document_id,
            chunk_id=s.chunk_id,
            original_rank=s.original_rank,
            selected_order=s.selected_order,
            content_digest=s.content_digest,
            serialized_token_count=s.serialized_token_count,
        )
        for s in selection.selected
    )
    dropped = tuple(
        CitationContextDropped(
            document_id=d.document_id,
            chunk_id=d.chunk_id,
            rank=d.rank,
            reason=d.reason,
        )
        for d in selection.dropped
    )
    support_ids = expected_support_ids if eligible else ()
    metrics = CitationContextCaseMetricsBuilder.build(
        selection, expected_support_ids=support_ids if eligible else None
    )
    return CitationContextCaseSidecar(
        case_id=case_view.case_id,
        query_sha256=case_view.query_sha256,
        eligible=eligible,
        candidates=candidates,
        selected=selected,
        dropped=dropped,
        selected_expected_support_ids=support_ids,
        metrics=metrics,
    )


def run_k(
    *,
    dataset: EvaluationDataset,
    evidence: RrfEvidenceEnvelopeV2,
    corpus: ControlledCorpus,
    K: int,
    token_counter: TokenCounter,
    selector: FixedTopKSelector | None = None,
) -> CitationContextSelectionEnvelope:
    """Run one K over the full frozen population and emit a strict sidecar."""
    if K not in K_VALUES:
        raise ValueError("K must be one of 1, 2, 3, 4")
    selector = selector or FixedTopKSelector()
    case_views = materialize_candidate_view(evidence, corpus)
    evidence_digest = canonical_digest(evidence.model_dump(mode="json"))
    dataset_digest = canonical_digest(dataset.model_dump(mode="json"))

    sidecar_cases: list[CitationContextCaseSidecar] = []
    for case_view in case_views:
        raw = selector.select(case_view.candidates, K=K)
        counted = count_selection_tokens(raw, token_counter)
        eligible, expected = _case_eligible(dataset, case_view.case_id)
        sidecar_case = _project_case(
            case_view, counted, eligible=eligible, expected_support_ids=expected
        )
        sidecar_cases.append(sidecar_case)

    identity = "|".join(
        (POLICY_REF, str(K), dataset.dataset_id, dataset.version, evidence_digest, SERIALIZER_REF)
    )
    selection_id = f"ccs-{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
    return CitationContextSelectionEnvelope(
        selection_id=selection_id,
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_digest=dataset_digest,
        retrieval_evidence_schema=evidence.schema_version,
        retrieval_evidence_digest=evidence_digest,
        substrate_ref=evidence.substrate_ref,
        corpus_ref=evidence.corpus_ref,
        source_manifest_digest=evidence.source_manifest_digest,
        chunk_manifest_digest=evidence.chunk_manifest_digest,
        policy_ref=POLICY_REF,
        K=K,
        serializer_ref=SERIALIZER_REF,
        tokenizer_ref=token_counter.tokenizer_ref,
        tokenizer_identity=token_counter.tokenizer_identity,
        tokenization_mode_ref=token_counter.tokenization_mode_ref,
        add_bos=token_counter.add_bos,
        special=token_counter.special,
        tokenizer_authority=token_counter.tokenizer_authority,
        runtime_read_only=True,
        cases=tuple(sidecar_cases),
    )


def run_comparison(
    *,
    dataset: EvaluationDataset,
    evidence: RrfEvidenceEnvelopeV2,
    corpus: ControlledCorpus,
    token_counter: TokenCounter,
    selector: FixedTopKSelector | None = None,
) -> tuple[dict[int, CitationContextSelectionEnvelope], CitationContextComparisonReport]:
    """Run K=1..4 and build the deterministic comparison report (no winner)."""
    envelopes: dict[int, CitationContextSelectionEnvelope] = {}
    aggregates: dict[int, CitationContextAggregateMetrics] = {}
    eligible_count: int | None = None
    for k in K_VALUES:
        envelope = run_k(
            dataset=dataset,
            evidence=evidence,
            corpus=corpus,
            K=k,
            token_counter=token_counter,
            selector=selector,
        )
        envelopes[k] = envelope
        aggregates[k] = aggregate_case_metrics([case.metrics for case in envelope.cases])
        eligible_count = sum(1 for case in envelope.cases if case.eligible)

    evidence_digest = canonical_digest(evidence.model_dump(mode="json"))
    dataset_digest = canonical_digest(dataset.model_dump(mode="json"))
    report = CitationContextComparisonReport(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_digest=dataset_digest,
        retrieval_evidence_schema=evidence.schema_version,
        retrieval_evidence_digest=evidence_digest,
        policy_ref=POLICY_REF,
        k4_is_production_context_exact=False,
        eligible_case_count=eligible_count or 0,
        per_k={k: aggregates[k] for k in K_VALUES},
        pareto=compute_pareto(aggregates),
        tokenizer_authority=token_counter.tokenizer_authority,
    )
    return envelopes, report


def validate_sidecar_privacy(envelope: CitationContextSelectionEnvelope) -> bool:
    """Ensure a produced sidecar leaks no plaintext."""
    return privacy_safe_serialization(envelope.model_dump(mode="json"))


__all__ = [
    "DATASET_CANONICAL_DIGEST",
    "REAL_EVIDENCE_CANONICAL_DIGEST",
    "load_controlled_corpus",
    "load_source_manifest",
    "materialize_candidate_view",
    "run_comparison",
    "run_k",
    "validate_frozen_inputs",
    "validate_sidecar_privacy",
]
