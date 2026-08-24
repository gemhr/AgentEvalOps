"""Validated v2 Dataset + strict RRF fixture 到 WP4 Gate v2 的 deterministic integration。"""

# ruff: noqa: D415

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core.evaluation.dataset import AnswerabilityCaseType, AnswerabilitySplit, load_dataset
from app.core.evaluation.no_answer import (
    WP2_BM25_CACHE_IDENTITY,
    WP2_DENSE_CACHE_IDENTITY,
    RrfEvidenceEnvelope,
)
from app.services.evaluation.no_answer_threshold import (
    GateOutcome,
    acceptance_gate_v2,
    build_evaluation_context,
    calibrate,
    canonical_digest,
    evaluate,
    signals_for_split,
    validate_experiment_evidence,
)


@pytest.fixture(autouse=True)
def _override_deps():
    """本 deterministic integration 不依赖 API、PostgreSQL 或 Redis fixture。"""
    yield


def test_deterministic_strict_rrf_evidence_to_lock_evaluation_and_gate_v2() -> None:
    asset = (
        Path(__file__).resolve().parents[2]
        / "evaluation_assets/no_answer_threshold_v2/no_answer_threshold_dataset.v2.json"
    )
    dataset = load_dataset(asset)
    evidence_cases = []
    for case in dataset.cases:
        truth = case.ground_truth.answerability
        assert truth is not None
        if truth.split == AnswerabilitySplit.DIAGNOSTIC:
            continue
        scores = (
            (0.04, 0.02)
            if truth.case_type == AnswerabilityCaseType.ANSWERABLE
            else (0.02, 0.019)
        )
        evidence_cases.append(
            {
                "case_id": case.case_id,
                "query_sha256": hashlib.sha256(case.input["query"].encode("utf-8")).hexdigest(),
                "retrieval_artifact_id": f"rrf-fixture-{case.case_id}",
                "retrieval_status": "SUCCEEDED",
                "retrieved_candidate_count": 2,
                "ranked_candidate_count": 2,
                "ranked_candidates": [
                    {
                        "document_id": f"doc-{case.case_id}-{rank}",
                        "chunk_id": f"chunk-{case.case_id}-{rank}",
                        "rank": rank,
                        "rrf_score": score,
                        "source_channels": [
                            "current-dense-led-ranked.v1",
                            "bm25-lucene-idf.v1",
                        ],
                        "contributing_channel_count": 2,
                    }
                    for rank, score in enumerate(scores, start=1)
                ],
            }
        )
    evidence = RrfEvidenceEnvelope.model_validate(
        {
            "schema_version": "no-answer-rrf-evidence.v1",
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "dataset_digest": canonical_digest(dataset.model_dump(mode="json")),
            "corpus_ref": "rag-evaluation-corpus.v1",
            "dense_cache_identity": WP2_DENSE_CACHE_IDENTITY,
            "bm25_cache_identity": WP2_BM25_CACHE_IDENTITY,
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
            "cases": evidence_cases,
        }
    )
    validated = validate_experiment_evidence(dataset, evidence)
    calibration_cases = [
        case
        for case in dataset.cases
        if case.ground_truth.answerability.split == AnswerabilitySplit.CALIBRATION
    ]
    evaluation_cases = [
        case
        for case in dataset.cases
        if case.ground_truth.answerability.split == AnswerabilitySplit.EVALUATION
    ]
    calibration = calibrate(
        calibration_cases=calibration_cases,
        calibration_signals=signals_for_split(validated, AnswerabilitySplit.CALIBRATION),
        validated_experiment=validated,
    )
    context = build_evaluation_context(validated)
    evaluation = evaluate(
        locked_policy=calibration.locked_policy,
        evaluation_context=context,
        evaluation_cases=evaluation_cases,
        evaluation_signals=signals_for_split(validated, AnswerabilitySplit.EVALUATION),
    )
    gate = acceptance_gate_v2(
        evaluation=evaluation,
        dataset=dataset,
        evidence=evidence,
        locked_policy=calibration.locked_policy,
        evaluation_context=context,
    )

    assert validated.verify() and calibration.locked_policy.verify() and context.verify()
    assert len(evaluation.case_facts) == len(evaluation.decisions) == 14
    assert evaluation.metrics.false_answer_count == 0
    assert gate.outcome == GateOutcome.ACCEPT
