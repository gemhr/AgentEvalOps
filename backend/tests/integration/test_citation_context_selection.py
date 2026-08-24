"""WP5 Citation Context Selection deterministic integration (evaluation-only).

Covers the end-to-end chain: frozen evidence + controlled corpus + pinned
tokenizer fixture -> candidate materialization -> K=1..4 selectors -> four strict
sidecars -> metric evaluator -> comparison report. REAL_RETRIEVAL = NO,
REAL_GENERATION = NO. When the real WP4 frozen evidence + materialized corpus are
present locally they are used with RETRIEVAL_NOT_RERUN = true; otherwise a
synthetic deterministic fixture drives the same pipeline.
"""

# ruff: noqa: D103, D415

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.evaluation.citation_context_selection import (
    CITATION_CONTEXT_COMPARISON_SCHEMA_VERSION,
    CITATION_CONTEXT_SELECTION_SCHEMA_VERSION,
    K_VALUES,
    FixtureTokenCounter,
    privacy_safe_serialization,
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
    load_dataset,
)
from app.core.evaluation.no_answer import RrfEvidenceEnvelopeV2
from app.services.evaluation.citation_context_selection import (
    DATASET_CANONICAL_DIGEST,
    REAL_EVIDENCE_CANONICAL_DIGEST,
    canonical_digest as service_digest,
    load_controlled_corpus,
    run_comparison,
    validate_frozen_inputs,
)
from tests.unit.wp5_fixtures import (
    FROZEN_CHUNK_MANIFEST_DIGEST,
    FROZEN_SOURCE_MANIFEST_DIGEST,
    make_case,
    make_corpus,
    make_evidence,
    sha256_text,
)


@pytest.fixture(autouse=True)
def _override_deps():
    """This deterministic integration does not depend on API/PostgreSQL/Redis."""
    yield


def _counter() -> FixtureTokenCounter:
    return FixtureTokenCounter()


def _real_assets() -> tuple[Path, Path, Path, Path] | None:
    evidence = (
        Path(__file__).resolve().parents[3]
        / ".ai/evidence/stage5_phase3_wp4_no_answer_threshold/real_rrf_evidence_v2/no_answer_rrf_evidence.v2.json"
    )
    corpus = (
        Path(__file__).resolve().parents[3]
        / ".ai/evidence/stage5_phase3_wp5_citation_context_selection/controlled_corpus.materialized.v1.json"
    )
    source_manifest = (
        Path(__file__).resolve().parents[3]
        / ".ai/evidence/stage5_phase3_wp5_citation_context_selection/source_manifest.v1.json"
    )
    dataset = (
        Path(__file__).resolve().parents[2]
        / "evaluation_assets/no_answer_threshold_v2/no_answer_threshold_dataset.v2.json"
    )
    if evidence.is_file() and corpus.is_file() and source_manifest.is_file() and dataset.is_file():
        return evidence, corpus, source_manifest, dataset
    return None


def _synthetic_dataset() -> EvaluationDataset:
    cases = []
    for index in range(1, 4):
        case_id = f"case-{index}"
        cases.append(
            EvaluationCase(
                case_id=case_id,
                name=case_id,
                input={"query": f"query {index}"},
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
                metadata={"tags": ["t"], "leakage_group": f"lg-{index}"},
            )
        )
    return EvaluationDataset(
        dataset_schema_version="evaluation-dataset.v4",
        dataset_id="no-answer-threshold-dataset",
        name="synthetic",
        version="v2",
        cases=cases,
    )


def _synthetic_fixture() -> tuple[EvaluationDataset, RrfEvidenceEnvelopeV2, object]:
    from app.core.evaluation.citation_context_selection import ControlledCorpus, ControlledCorpusEntry

    chunks = [
        ("chunk-a", "doc-1", "01.md", "Alpha support content one."),
        ("chunk-b", "doc-1", "01.md", "Beta support content two."),
        ("chunk-c", "doc-2", "02.md", "Gamma noise content three."),
        ("chunk-d", "doc-2", "02.md", "Delta noise content four."),
    ]
    corpus = make_corpus(*chunks)
    # Use frozen substrate digests so the sidecar/evidence remain consistent.
    corpus = ControlledCorpus(
        corpus_ref="rag-evaluation-corpus.v1",
        source_manifest_digest=FROZEN_SOURCE_MANIFEST_DIGEST,
        chunk_manifest_digest=FROZEN_CHUNK_MANIFEST_DIGEST,
        entries=corpus.entries,
    )
    dataset = _synthetic_dataset()
    dataset_digest = service_digest(dataset.model_dump(mode="json"))
    evidence = RrfEvidenceEnvelopeV2.model_validate(
        make_evidence(
            [
                make_case("case-1", "query 1", [("doc-1", "chunk-a"), ("doc-1", "chunk-b"), ("doc-2", "chunk-c")]),
                make_case("case-2", "query 2", [("doc-1", "chunk-a"), ("doc-2", "chunk-c"), ("doc-2", "chunk-d")]),
                make_case("case-3", "query 3", [("doc-1", "chunk-b"), ("doc-1", "chunk-a"), ("doc-2", "chunk-d")]),
            ],
            dataset_digest=dataset_digest,
        )
    )
    return dataset, evidence, corpus


def _assert_comparison_shape(report) -> None:
    assert report.schema_version == CITATION_CONTEXT_COMPARISON_SCHEMA_VERSION
    assert report.k4_is_production_context_exact is False
    assert report.citation_correctness == "NOT_EVALUATED_IN_WP5_V1"
    assert report.citation_completeness == "NOT_EVALUATED_IN_WP5_V1"
    assert report.real_retrieval == "NOT_RUN"
    assert report.real_generation == "NOT_RUN"
    assert report.token_usage_authority == "PINNED_GENERATION_MODEL_TOKENIZER_ON_WP5_SERIALIZED_RAG_CONTEXT"
    assert set(report.per_k.keys()) == set(K_VALUES)
    assert len(report.pareto) == 3


def test_synthetic_deterministic_end_to_end_k1_to_k4() -> None:
    dataset, evidence, corpus = _synthetic_fixture()
    envelopes, report = run_comparison(
        dataset=dataset, evidence=evidence, corpus=corpus, token_counter=_counter()
    )
    assert set(envelopes.keys()) == set(K_VALUES)
    for k, envelope in envelopes.items():
        assert envelope.schema_version == CITATION_CONTEXT_SELECTION_SCHEMA_VERSION
        assert envelope.K == k
        assert envelope.runtime_read_only is True
        assert len(envelope.cases) == 3
        assert all(1 <= len(case.selected) <= k for case in envelope.cases)
        assert all(len(case.selected) == min(k, len(case.candidates)) for case in envelope.cases)
        assert privacy_safe_serialization(envelope.model_dump(mode="json")) is True
    _assert_comparison_shape(report)
    assert report.eligible_case_count == 3
    assert report.tokenizer_authority == "FIXTURE_TEST_ONLY"


def test_real_frozen_evidence_end_to_end_retrieval_not_rerun() -> None:
    assets = _real_assets()
    if assets is None:
        pytest.skip("real WP4 frozen evidence + materialized corpus not present (read-only substrate authority)")
    evidence_path, corpus_path, source_manifest_path, dataset_path = assets
    dataset = load_dataset(dataset_path)
    evidence = RrfEvidenceEnvelopeV2.model_validate(json.loads(evidence_path.read_text(encoding="utf-8")))
    corpus = load_controlled_corpus(corpus_path, source_manifest_path)
    # fail-closed frozen identity check
    validate_frozen_inputs(dataset, evidence, corpus)
    assert service_digest(evidence.model_dump(mode="json")) == REAL_EVIDENCE_CANONICAL_DIGEST
    assert service_digest(dataset.model_dump(mode="json")) == DATASET_CANONICAL_DIGEST

    envelopes, report = run_comparison(
        dataset=dataset, evidence=evidence, corpus=corpus, token_counter=_counter()
    )
    assert set(envelopes.keys()) == set(K_VALUES)
    for k, envelope in envelopes.items():
        assert envelope.K == k
        assert len(envelope.cases) == len(dataset.cases)
        assert all(case.query_sha256 for case in envelope.cases)
        assert privacy_safe_serialization(envelope.model_dump(mode="json")) is True
    _assert_comparison_shape(report)
    # RETRIEVAL_NOT_RERUN: WP5 only consumes the frozen evidence, never re-runs retrieval.
    assert report.real_retrieval == "NOT_RUN"
