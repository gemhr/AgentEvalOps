"""WP2 focused tests: retrieval corpus、retriever、data provider 与 data sufficiency。"""

# ruff: noqa: D415

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.adapters.feature_risk_review.data_provider import NormalizedFeatureRiskReviewDataProvider
from app.adapters.feature_risk_review.retrieval import (
    SourcePreservingLexicalRetriever,
    load_retrieval_corpus,
)
from app.core.feature_risk_review import (
    FeatureRiskReviewDataError,
    RiskRetrievalQuery,
    load_feature_risk_review_cases,
)

ASSET_ROOT = Path(__file__).resolve().parents[2] / "evaluation_assets" / "feature_risk_review_v1"
CORPUS_PATH = ASSET_ROOT / "retrieval" / "phase4_retrieval_corpus.v1.json"


def test_corpus_artifact_loads_and_is_source_preserving() -> None:
    corpus = load_retrieval_corpus(CORPUS_PATH)
    assert corpus.schema_version == "feature-risk-review-retrieval-corpus.v1"
    assert corpus.chunks
    for chunk in corpus.chunks:
        ref = chunk.evidence_ref
        assert (ASSET_ROOT / ref.source_path).is_file()
        assert str(ref.source_url).startswith("https://github.com/")


def test_corpus_contains_enrichment_and_tracking_issue_sources() -> None:
    corpus = load_retrieval_corpus(CORPUS_PATH)
    source_types = {chunk.evidence_ref.source_type for chunk in corpus.chunks}
    assert "kubernetes_issue_snapshot" in source_types
    assert "github_enhancement_tracking_issue" in source_types
    historical = [c for c in corpus.chunks if c.evidence_ref.source_type == "kubernetes_issue_snapshot"]
    assert len(historical) >= 10


def test_corpus_excludes_evaluation_reference_sections() -> None:
    protected = {
        "Risks and Mitigations",
        "Test Plan",
        "Production Readiness",
        "Upgrade / Downgrade Strategy",
        "Version Skew Strategy",
        "Graduation Criteria",
        "Implementation History",
    }
    corpus = load_retrieval_corpus(CORPUS_PATH)
    for chunk in corpus.chunks:
        section = chunk.evidence_ref.section or ""
        for item in protected:
            assert item not in section, f"{chunk.chunk_id} leaks {item}"


async def test_query_to_retrieval_to_fragment_to_evidence_ref() -> None:
    retriever = SourcePreservingLexicalRetriever(path=CORPUS_PATH)
    hits = await retriever.retrieve(query="in-place pod resize restarts pod", top_k=5)
    assert hits
    assert hits[0].source_fragment
    assert hits[0].evidence_ref.evidence_id
    assert hits[0].relevance_score is not None


async def test_retrieval_is_deterministic() -> None:
    retriever = SourcePreservingLexicalRetriever(path=CORPUS_PATH)
    q = "sidecar container startup probe restart policy"
    first = await retriever.retrieve(query=q, top_k=5)
    second = await retriever.retrieve(query=q, top_k=5)
    assert [h.evidence_ref.evidence_id for h in first] == [h.evidence_ref.evidence_id for h in second]
    assert [h.relevance_score for h in first] == [h.relevance_score for h in second]


async def test_empty_or_noise_query_returns_empty() -> None:
    retriever = SourcePreservingLexicalRetriever(path=CORPUS_PATH)
    assert await retriever.retrieve(query="the and for", top_k=5) == []
    assert await retriever.retrieve(query="", top_k=5) == []


def test_at_least_three_of_five_cases_retrieve_meaningful_historical_evidence() -> None:
    """Data sufficiency spike：≥3/5 case 能检索出有风险价值的 historical evidence。"""

    async def spike() -> bool:
        retriever = SourcePreservingLexicalRetriever(path=CORPUS_PATH)
        cases = load_feature_risk_review_cases(ASSET_ROOT)
        meaningful = 0
        for case in cases:
            issue_id = case.historical_issues[0].issue_id
            query = f"{case.feature_document.title}. {case.feature_document.agent_visible_content[:600]}"
            hits = await retriever.retrieve(query=query, top_k=5)
            has_historical = any(
                hit.evidence_ref.source_type == "kubernetes_issue_snapshot"
                or hit.evidence_ref.source_id != issue_id
                or hit.evidence_ref.source_type == "github_enhancement_tracking_issue"
                for hit in hits
            )
            if has_historical:
                meaningful += 1
        return meaningful >= 3

    assert asyncio.run(spike())


def test_data_provider_returns_source_backed_data_for_all_cases() -> None:
    provider = NormalizedFeatureRiskReviewDataProvider(root=ASSET_ROOT)

    async def check() -> None:
        cases = load_feature_risk_review_cases(ASSET_ROOT)
        for case in cases:
            case_id = case.feature_document.case_id
            query = RiskRetrievalQuery(
                change_point_descriptions=["change"],
                affected_components=["component"],
                potential_risk_areas=["risk"],
            )
            issues = await provider.historical_issues(case_id=case_id, query_inputs=query)
            evidence = await provider.test_evidence(case_id=case_id)
            assert issues
            assert issues[0].severity is None
            assert evidence.test_plans
            assert evidence.test_cases == []
            for ref in (issues[0].evidence_ref, evidence.test_plans[0].evidence_ref):
                assert (ASSET_ROOT / ref.source_path).is_file()

    asyncio.run(check())


def test_data_provider_unknown_case_fails_clearly() -> None:
    provider = NormalizedFeatureRiskReviewDataProvider(root=ASSET_ROOT)

    async def check() -> None:
        try:
            await provider.test_evidence(case_id="missing_case")
        except FeatureRiskReviewDataError as exc:
            assert "unknown case_id" in str(exc)
            return
        raise AssertionError("expected FeatureRiskReviewDataError")

    asyncio.run(check())


def test_corpus_artifact_is_valid_json_and_committed() -> None:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert payload["corpus_id"] == "kubernetes-feature-risk-review-phase4.v1"
    assert len(payload["chunks"]) > 100