"""WP4 No-Answer policy、strict evidence、lock、Gate 与 privacy focused tests。"""

# ruff: noqa: D415

from __future__ import annotations

import hashlib
import importlib.util
import socket
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.evaluation.dataset import (
    AnswerabilityCaseType,
    AnswerabilitySplit,
    EvaluationDataset,
    load_dataset,
)
from app.core.evaluation.no_answer import (
    WP2_BM25_CACHE_IDENTITY,
    WP2_DENSE_CACHE_IDENTITY,
    EmptyOrigin,
    NoAnswerDecisionValue,
    NoAnswerPolicy,
    NoAnswerPolicyConfig,
    NoAnswerProtocolError,
    NoAnswerSignal,
    RetrievalStatus,
    RrfEvidenceEnvelope,
    calculate_confusion,
    derive_empty_origin,
)
from app.services.evaluation.no_answer_threshold import (
    EvaluationContextIdentity,
    EvaluationResult,
    GateInvariants,
    GateOutcome,
    ValidatedExperimentInvariants,
    acceptance_gate_v1,
    acceptance_gate_v2,
    build_candidate_grid,
    build_evaluation_context,
    calibrate,
    canonical_digest,
    evaluate,
    privacy_safe_serialization,
    signals_for_split,
    validate_experiment_evidence,
    validate_no_answer_dataset,
)

ASSET = (
    Path(__file__).resolve().parents[2]
    / "evaluation_assets/no_answer_threshold_v2/no_answer_threshold_dataset.v2.json"
)
V1_ASSET = (
    Path(__file__).resolve().parents[2]
    / "evaluation_assets/no_answer_threshold_v1/no_answer_threshold_dataset.v1.json"
)
RUNNER = Path(__file__).resolve().parents[2] / "scripts/run_no_answer_threshold_evaluation.py"
CONFIG = NoAnswerPolicyConfig(min_top1_score=0.03, min_top1_top2_margin=0.01)


def _signal(
    case_id: str = "case-1",
    *,
    status: RetrievalStatus = RetrievalStatus.SUCCEEDED,
    scores: tuple[float, ...] = (0.04, 0.02),
    retrieved: int | None = 2,
    ranked: int | None = 2,
) -> NoAnswerSignal:
    return NoAnswerSignal(
        case_id=case_id,
        query_sha256=hashlib.sha256(case_id.encode()).hexdigest(),
        retrieval_artifact_id=f"rrf-artifact-{case_id}",
        retrieval_status=status,
        retrieved_candidate_count=retrieved,
        ranked_candidate_count=ranked,
        candidate_count=len(scores),
        rrf_scores=scores,
    )


def _decision(signal: NoAnswerSignal):
    return NoAnswerPolicy.decide(
        signal=signal,
        config=CONFIG,
        policy_ref="policy-1",
        dataset_id="dataset",
        dataset_version="v1",
        split="CALIBRATION",
    )


@pytest.mark.parametrize(
    "signal,expected",
    [
        (_signal(status=RetrievalStatus.EMPTY, scores=(), retrieved=0, ranked=0), NoAnswerDecisionValue.ABSTAIN),
        (_signal(scores=(0.04,), retrieved=1, ranked=1), NoAnswerDecisionValue.ANSWER),
        (_signal(scores=(0.02,), retrieved=1, ranked=1), NoAnswerDecisionValue.ABSTAIN),
        (_signal(scores=(0.03,), retrieved=1, ranked=1), NoAnswerDecisionValue.ANSWER),
        (_signal(scores=(0.04, 0.03)), NoAnswerDecisionValue.ANSWER),
        (_signal(scores=(0.04, 0.035)), NoAnswerDecisionValue.ABSTAIN),
        (_signal(status=RetrievalStatus.DEGRADED), NoAnswerDecisionValue.ANSWER),
    ],
)
def test_policy_boundaries(signal: NoAnswerSignal, expected: NoAnswerDecisionValue) -> None:
    decision = _decision(signal)
    assert decision is not None and decision.decision == expected


@pytest.mark.parametrize("status", [RetrievalStatus.FAILED, RetrievalStatus.TIMED_OUT, RetrievalStatus.CANCELLED])
def test_technical_failure_produces_no_decision(status: RetrievalStatus) -> None:
    assert _decision(_signal(status=status, scores=(), retrieved=0, ranked=0)) is None


@pytest.mark.parametrize("scores", [(float("nan"),), (float("inf"),)])
def test_non_finite_scores_fail_closed(scores: tuple[float, ...]) -> None:
    with pytest.raises(ValidationError, match="finite"):
        _signal(scores=scores, retrieved=1, ranked=1)


def test_candidate_count_order_and_empty_origin_fail_closed() -> None:
    payload = _signal().model_dump()
    payload["candidate_count"] = 1
    with pytest.raises(ValidationError, match="candidate_count"):
        NoAnswerSignal.model_validate(payload)
    with pytest.raises(ValidationError, match="descending"):
        _signal(scores=(0.02, 0.04))
    assert derive_empty_origin(_signal(status=RetrievalStatus.EMPTY, scores=(), retrieved=0, ranked=0)) == EmptyOrigin.ZERO_CANDIDATE
    assert derive_empty_origin(_signal(status=RetrievalStatus.EMPTY, scores=(), retrieved=2, ranked=0)) == EmptyOrigin.FILTERED_EMPTY
    assert derive_empty_origin(_signal()) == EmptyOrigin.NOT_APPLICABLE


def test_confusion_metrics_and_zero_denominator() -> None:
    metrics = calculate_confusion(
        [
            (True, NoAnswerDecisionValue.ANSWER),
            (True, NoAnswerDecisionValue.ABSTAIN),
            (False, NoAnswerDecisionValue.ANSWER),
            (False, NoAnswerDecisionValue.ABSTAIN),
        ],
        technical_failure_count=2,
    )
    assert (metrics.true_answer_count, metrics.false_abstain_count) == (1, 1)
    assert (metrics.false_answer_count, metrics.true_abstain_count) == (1, 1)
    assert metrics.no_answer_accuracy == metrics.false_answer_rate == metrics.false_abstain_rate == metrics.coverage == 0.5
    with pytest.raises(NoAnswerProtocolError, match="denominator"):
        calculate_confusion([(True, NoAnswerDecisionValue.ANSWER)])


def _dataset() -> EvaluationDataset:
    dataset = load_dataset(ASSET)
    validate_no_answer_dataset(dataset)
    return dataset


def _ranked_candidates(case_id: str, scores: tuple[float, ...]) -> list[dict[str, object]]:
    return [
        {
            "document_id": f"doc-{case_id}-{rank}",
            "chunk_id": f"chunk-{case_id}-{rank}",
            "rank": rank,
            "rrf_score": score,
            "source_channels": ["current-dense-led-ranked.v1", "bm25-lucene-idf.v1"],
            "contributing_channel_count": 2,
        }
        for rank, score in enumerate(scores, start=1)
    ]


def _evidence_payload(
    dataset: EvaluationDataset,
    *,
    identical: bool = False,
    evaluation_negative_answer: int = 0,
    evaluation_answerable_abstain: int = 0,
) -> dict[str, object]:
    cases = []
    negative_seen = 0
    answerable_seen = 0
    for case in dataset.cases:
        truth = case.ground_truth.answerability
        assert truth is not None
        if truth.split == AnswerabilitySplit.DIAGNOSTIC:
            continue
        if identical:
            scores = (0.04, 0.02)
        elif truth.case_type == AnswerabilityCaseType.ANSWERABLE:
            if truth.split == AnswerabilitySplit.EVALUATION:
                answerable_seen += 1
            scores = (
                (0.02, 0.019)
                if truth.split == AnswerabilitySplit.EVALUATION
                and answerable_seen <= evaluation_answerable_abstain
                else (0.04, 0.02)
            )
        else:
            if truth.split == AnswerabilitySplit.EVALUATION:
                negative_seen += 1
            scores = (
                (0.04, 0.02)
                if truth.split == AnswerabilitySplit.EVALUATION
                and negative_seen <= evaluation_negative_answer
                else (0.02, 0.019)
            )
        cases.append(
            {
                "case_id": case.case_id,
                "query_sha256": hashlib.sha256(case.input["query"].encode("utf-8")).hexdigest(),
                "retrieval_artifact_id": f"rrf-artifact-{case.case_id}",
                "retrieval_status": "SUCCEEDED",
                "retrieved_candidate_count": 2,
                "ranked_candidate_count": 2,
                "ranked_candidates": _ranked_candidates(case.case_id, scores),
            }
        )
    return {
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
        "cases": cases,
    }


def _validated_chain(**evidence_changes: object):
    dataset = _dataset()
    payload = _evidence_payload(dataset, **evidence_changes)
    evidence = RrfEvidenceEnvelope.model_validate(payload)
    validated = validate_experiment_evidence(dataset, evidence)
    calibration_cases = [
        case for case in dataset.cases if case.ground_truth.answerability.split == AnswerabilitySplit.CALIBRATION
    ]
    evaluation_cases = [
        case for case in dataset.cases if case.ground_truth.answerability.split == AnswerabilitySplit.EVALUATION
    ]
    calibration = calibrate(
        calibration_cases=calibration_cases,
        calibration_signals=signals_for_split(validated, AnswerabilitySplit.CALIBRATION),
        validated_experiment=validated,
    )
    context = build_evaluation_context(validated)
    result = evaluate(
        locked_policy=calibration.locked_policy,
        evaluation_context=context,
        evaluation_cases=evaluation_cases,
        evaluation_signals=signals_for_split(validated, AnswerabilitySplit.EVALUATION),
    )
    return dataset, evidence, validated, calibration, context, result


def test_v2_asset_coverage_annotation_and_semantic_leakage() -> None:
    dataset = _dataset()
    coverage = validate_no_answer_dataset(dataset)
    assert canonical_digest(dataset.model_dump(mode="json")) == (
        "e0042be4e1611eddc209159c8bd598dfa0637285a9b96a237f053a00fad8f9dd"
    )
    assert len(dataset.cases) == 28
    assert coverage == {
        "CALIBRATION": {"ANSWERABLE": 4, "EMPTY": 4, "MISLEADING": 2, "WEAK": 4},
        "EVALUATION": {"ANSWERABLE": 4, "EMPTY": 4, "MISLEADING": 2, "WEAK": 4},
    }
    misleading = {
        case.case_id
        for case in dataset.cases
        if case.ground_truth.answerability.case_type == AnswerabilityCaseType.MISLEADING
    }
    assert misleading == {
        "cal-misleading-context-dedup-provenance",
        "cal-misleading-marker-network",
        "eval-misleading-journal-atomicity",
        "eval-misleading-dataset-execution-order",
    }
    groups = [case.metadata["leakage_group"] for case in dataset.cases]
    assert len(set(groups)) < len(groups)
    cases_by_id = {case.case_id: case for case in dataset.cases}
    assert "eval-answer-state-owner" not in cases_by_id
    replacement = cases_by_id["eval-answer-metadata-error-code"]
    assert replacement.metadata["leakage_group"] == "eval-error-codes"
    assert replacement.ground_truth.answerability.expected_support_fact_ids == [
        "dfad608271cc0fd70c77957e103b3d107befaca0"
    ]
    v1 = load_dataset(V1_ASSET)
    assert dataset.version == "v2" and v1.version == "v1"
    with pytest.raises(NoAnswerProtocolError, match="ASSET_IDENTITY_MISMATCH"):
        validate_no_answer_dataset(v1)


def test_v2_minima_zero_conflict_total_over_28_and_missing_answerability() -> None:
    dataset = _dataset()
    payload = dataset.model_dump(mode="json")
    extra = deepcopy(payload["cases"][0])
    extra["case_id"] = "cal-extra-answerable"
    extra["name"] = "extra"
    extra["metadata"] = {"tags": ["extra"], "leakage_group": "cal-extra-fact"}
    expanded = EvaluationDataset.model_validate({**payload, "cases": [*payload["cases"], extra]})
    assert validate_no_answer_dataset(expanded)["CALIBRATION"]["ANSWERABLE"] == 5

    too_few = deepcopy(payload)
    too_few["cases"] = [
        case
        for case in too_few["cases"]
        if case["case_id"] != "eval-misleading-dataset-execution-order"
    ]
    with pytest.raises(NoAnswerProtocolError, match="DATASET_NOT_READY"):
        validate_no_answer_dataset(EvaluationDataset.model_validate(too_few))

    missing = deepcopy(payload)
    missing["cases"][0]["ground_truth"] = {"generation": {"reference_answer": "answer"}}
    missing["cases"][0]["metadata"] = {"topic": "generic-v4"}
    with pytest.raises(NoAnswerProtocolError, match="ANSWERABILITY_REQUIRED"):
        validate_no_answer_dataset(EvaluationDataset.model_validate(missing))


def test_grid_calibration_lock_and_order_independence() -> None:
    dataset = _dataset()
    evidence = RrfEvidenceEnvelope.model_validate(_evidence_payload(dataset))
    validated = validate_experiment_evidence(dataset, evidence)
    cases = [
        case for case in dataset.cases if case.ground_truth.answerability.split == AnswerabilitySplit.CALIBRATION
    ]
    signals = signals_for_split(validated, AnswerabilitySplit.CALIBRATION)
    grid = build_candidate_grid(signals)
    assert grid == build_candidate_grid(tuple(reversed(signals)))
    first = calibrate(calibration_cases=cases, calibration_signals=signals, validated_experiment=validated)
    second = calibrate(
        calibration_cases=list(reversed(cases)),
        calibration_signals=tuple(reversed(signals)),
        validated_experiment=validated,
    )
    assert first.locked_policy == second.locked_policy
    assert first.locked_policy.verify()


def test_no_feasible_policy_is_explicit() -> None:
    dataset = _dataset()
    evidence = RrfEvidenceEnvelope.model_validate(_evidence_payload(dataset, identical=True))
    validated = validate_experiment_evidence(dataset, evidence)
    cases = [
        case for case in dataset.cases if case.ground_truth.answerability.split == AnswerabilitySplit.CALIBRATION
    ]
    with pytest.raises(NoAnswerProtocolError, match="NO_FEASIBLE"):
        calibrate(
            calibration_cases=cases,
            calibration_signals=signals_for_split(validated, AnswerabilitySplit.CALIBRATION),
            validated_experiment=validated,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("dense_cache_identity", "arbitrary"),
        ("bm25_cache_identity", "arbitrary"),
        ("algorithm_ref", "other"),
        ("rrf_k", 61),
        ("per_channel_candidate_limit", 7),
        ("pre_fusion_union_limit", 15),
        ("final_candidate_limit", 7),
        ("ce_used", True),
        ("new_model_used", True),
        ("runtime_read_only", False),
    ],
)
def test_strict_evidence_rejects_wrong_frozen_identity(field: str, value: object) -> None:
    payload = _evidence_payload(_dataset())
    payload[field] = value
    with pytest.raises(ValidationError):
        RrfEvidenceEnvelope.model_validate(payload)


@pytest.mark.parametrize("field", ["unknown", "query_plaintext", "chunk_text", "local_path", "credential"])
def test_strict_evidence_rejects_unknown_or_plaintext_fields(field: str) -> None:
    payload = _evidence_payload(_dataset())
    payload[field] = "SECRET_QUERY_WP4"
    with pytest.raises(ValidationError):
        RrfEvidenceEnvelope.model_validate(payload)


def test_strict_evidence_rejects_duplicate_case_artifact_and_candidate() -> None:
    payload = _evidence_payload(_dataset())
    duplicate_case = deepcopy(payload)
    duplicate_case["cases"].append(deepcopy(duplicate_case["cases"][0]))
    with pytest.raises(ValidationError, match="duplicate evidence case"):
        RrfEvidenceEnvelope.model_validate(duplicate_case)

    duplicate_artifact = deepcopy(payload)
    duplicate_artifact["cases"][1]["retrieval_artifact_id"] = duplicate_artifact["cases"][0]["retrieval_artifact_id"]
    with pytest.raises(ValidationError, match="duplicate retrieval artifact"):
        RrfEvidenceEnvelope.model_validate(duplicate_artifact)

    duplicate_candidate = deepcopy(payload)
    duplicate_candidate["cases"][0]["ranked_candidates"][1]["document_id"] = duplicate_candidate["cases"][0]["ranked_candidates"][0]["document_id"]
    duplicate_candidate["cases"][0]["ranked_candidates"][1]["chunk_id"] = duplicate_candidate["cases"][0]["ranked_candidates"][0]["chunk_id"]
    with pytest.raises(ValidationError, match="duplicate candidate"):
        RrfEvidenceEnvelope.model_validate(duplicate_candidate)


@pytest.mark.parametrize("mutation", ["rank", "nan", "inf", "count"])
def test_strict_evidence_rejects_rank_score_and_count_mismatch(mutation: str) -> None:
    payload = _evidence_payload(_dataset())
    case = payload["cases"][0]
    if mutation == "rank":
        case["ranked_candidates"][1]["rank"] = 3
    elif mutation == "nan":
        case["ranked_candidates"][0]["rrf_score"] = float("nan")
    elif mutation == "inf":
        case["ranked_candidates"][0]["rrf_score"] = float("inf")
    else:
        case["ranked_candidate_count"] = 1
    with pytest.raises(ValidationError):
        RrfEvidenceEnvelope.model_validate(payload)


def _recontext(context: EvaluationContextIdentity, **changes: object) -> EvaluationContextIdentity:
    payload = context.model_dump(mode="json", exclude={"context_digest"})
    payload.update(changes)
    return EvaluationContextIdentity(**payload, context_digest=canonical_digest(payload))


def test_lock_and_evaluation_context_exact_binding() -> None:
    dataset, _, validated, calibration, context, result = _validated_chain()
    assert result.metrics.false_answer_count == 0
    evaluation_cases = [
        case for case in dataset.cases if case.ground_truth.answerability.split == AnswerabilitySplit.EVALUATION
    ]
    signals = signals_for_split(validated, AnswerabilitySplit.EVALUATION)
    for changes in (
        {"corpus_ref": "other-corpus"},
        {"dataset_digest": "0" * 64},
        {"evaluation_split_digest": "1" * 64},
        {"evaluation_evidence_digest": "2" * 64},
    ):
        with pytest.raises(NoAnswerProtocolError, match="CONTEXT_LOCK_MISMATCH"):
            evaluate(
                locked_policy=calibration.locked_policy,
                evaluation_context=_recontext(context, **changes),
                evaluation_cases=evaluation_cases,
                evaluation_signals=signals,
            )

    dataset_b_payload = dataset.model_dump(mode="json")
    for case in dataset_b_payload["cases"]:
        if case["ground_truth"]["answerability"]["split"] == "EVALUATION":
            case["name"] = f"{case['name']} changed"
            break
    dataset_b = EvaluationDataset.model_validate(dataset_b_payload)
    evaluation_b = [
        case for case in dataset_b.cases if case.ground_truth.answerability.split == AnswerabilitySplit.EVALUATION
    ]
    with pytest.raises(NoAnswerProtocolError, match="SPLIT_DIGEST_MISMATCH"):
        evaluate(
            locked_policy=calibration.locked_policy,
            evaluation_context=context,
            evaluation_cases=evaluation_b,
            evaluation_signals=signals,
        )

    for field, value in (
        ("rrf_k", 61),
        ("algorithm_ref", "other"),
        ("dense_cache_identity", "other-dense"),
        ("bm25_cache_identity", "other-bm25"),
        ("final_fused_candidate_limit", 7),
    ):
        wrong_rrf = context.model_dump(mode="json")
        wrong_rrf["rrf_config"][field] = value
        with pytest.raises(ValidationError):
            EvaluationContextIdentity.model_validate(wrong_rrf)


def _gate_v2(result, dataset, evidence, calibration, context):
    return acceptance_gate_v2(
        evaluation=result,
        dataset=dataset,
        evidence=evidence,
        locked_policy=calibration.locked_policy,
        evaluation_context=context,
    )


def test_gate_v2_accept_reject_and_gate_v1_coverage_compatibility() -> None:
    dataset, evidence, _, calibration, context, result = _validated_chain()
    gate = _gate_v2(result, dataset, evidence, calibration, context)
    assert gate.outcome == GateOutcome.ACCEPT and gate.gate_ref.endswith(".v2")

    rejected_dataset, rejected_evidence, _, rejected_calibration, rejected_context, rejected = _validated_chain(
        evaluation_negative_answer=1
    )
    assert (
        _gate_v2(rejected, rejected_dataset, rejected_evidence, rejected_calibration, rejected_context).outcome
        == GateOutcome.REJECT
    )

    v1_invariants = GateInvariants(
        dataset_identity_correct=True,
        coverage_correct=True,
        split_leakage_correct=True,
        rrf_config_correct=True,
        policy_lock_correct=True,
        calibration_only_selection_proof=True,
        forbidden_model_used=False,
        privacy_safe=True,
        runtime_read_only=True,
    )
    assert acceptance_gate_v1(
        evaluation=result, invariants=v1_invariants, expected_evaluation_count=14
    ).outcome == GateOutcome.NOT_EVALUATED_BLOCKED
    v1_dump = result.model_dump(mode="json")
    v1_dump["subtype_results"]["MISLEADING"]["count"] = 4
    v1_dump["subtype_results"]["MISLEADING"]["correct_count"] = 4
    assert acceptance_gate_v1(
        evaluation=EvaluationResult.model_validate(v1_dump),
        invariants=v1_invariants,
        expected_evaluation_count=14,
    ).outcome == GateOutcome.ACCEPT


def test_gate_v2_revalidates_raw_evidence_and_does_not_accept_caller_forged_proof() -> None:
    dataset, evidence, validated, calibration, context, result = _validated_chain()
    forged_payload = validated.model_dump(mode="json", exclude={"proof_digest"})
    forged_payload["dataset_digest"] = "f" * 64
    forged = ValidatedExperimentInvariants(
        **forged_payload,
        proof_digest=canonical_digest(forged_payload),
    )
    assert forged.verify()

    with pytest.raises(TypeError, match="validated_experiment"):
        acceptance_gate_v2(
            evaluation=result,
            validated_experiment=forged,
            locked_policy=calibration.locked_policy,
            evaluation_context=context,
        )

    bypassed_evidence = evidence.model_copy(update={"ce_used": True})
    gate = acceptance_gate_v2(
        evaluation=result,
        dataset=dataset,
        evidence=bypassed_evidence,
        locked_policy=calibration.locked_policy,
        evaluation_context=context,
    )
    assert gate.outcome == GateOutcome.NOT_EVALUATED_BLOCKED
    assert gate.reason_codes == ("strict_evidence_validation_failure",)


def test_gate_v2_blocks_policy_calibrated_from_forged_signals() -> None:
    dataset, evidence, validated, calibration, context, _ = _validated_chain()
    calibration_cases = {
        case.case_id: case
        for case in dataset.cases
        if case.ground_truth.answerability.split == AnswerabilitySplit.CALIBRATION
    }
    forged_signals = []
    for signal in validated.signals:
        case = calibration_cases.get(signal.case_id)
        if case is not None and case.ground_truth.answerability.case_type == AnswerabilityCaseType.ANSWERABLE:
            signal = signal.model_copy(update={"rrf_scores": (0.039, 0.019)})
        forged_signals.append(signal)
    forged_by_id = {signal.case_id: signal for signal in forged_signals}
    forged_calibration_digest = canonical_digest(
        [
            {
                "case": calibration_cases[case_id].model_dump(mode="json"),
                "signal": forged_by_id[case_id].model_dump(mode="json"),
            }
            for case_id in sorted(calibration_cases)
        ]
    )
    forged_payload = validated.model_dump(mode="json", exclude={"proof_digest"})
    forged_payload["signals"] = tuple(signal.model_dump(mode="json") for signal in forged_signals)
    forged_payload["calibration_evidence_digest"] = forged_calibration_digest
    forged = ValidatedExperimentInvariants(
        **forged_payload,
        proof_digest=canonical_digest(forged_payload),
    )
    assert forged.verify()

    forged_calibration = calibrate(
        calibration_cases=tuple(calibration_cases.values()),
        calibration_signals=signals_for_split(forged, AnswerabilitySplit.CALIBRATION),
        validated_experiment=forged,
    )
    assert calibration.locked_policy.policy_config.min_top1_score == 0.04
    assert forged_calibration.locked_policy.policy_config.min_top1_score == 0.039

    evaluation_cases = [
        case
        for case in dataset.cases
        if case.ground_truth.answerability.split == AnswerabilitySplit.EVALUATION
    ]
    forged_result = evaluate(
        locked_policy=forged_calibration.locked_policy,
        evaluation_context=context,
        evaluation_cases=evaluation_cases,
        evaluation_signals=signals_for_split(validated, AnswerabilitySplit.EVALUATION),
    )
    gate = _gate_v2(forged_result, dataset, evidence, forged_calibration, context)
    assert gate.outcome == GateOutcome.NOT_EVALUATED_BLOCKED
    assert "calibration_policy_lock_mismatch" in gate.reason_codes


def test_gate_v2_blocks_summary_and_per_case_contradictions() -> None:
    dataset, evidence, _, calibration, context, result = _validated_chain()

    subtype_mismatch = result.model_dump(mode="json")
    subtype_mismatch["subtype_results"]["WEAK"]["count"] = 3
    assert _gate_v2(
        EvaluationResult.model_validate(subtype_mismatch), dataset, evidence, calibration, context
    ).outcome == GateOutcome.NOT_EVALUATED_BLOCKED

    fact_mismatch = result.model_dump(mode="json")
    negative = next(fact for fact in fact_mismatch["case_facts"] if not fact["answerable"])
    negative["decision"] = "ANSWER"
    blocked = _gate_v2(EvaluationResult.model_validate(fact_mismatch), dataset, evidence, calibration, context)
    assert blocked.outcome == GateOutcome.NOT_EVALUATED_BLOCKED
    assert "evaluation_summary_contradiction" in blocked.reason_codes

    truth_mismatch = result.model_dump(mode="json")
    answerable = next(fact for fact in truth_mismatch["case_facts"] if fact["answerable"])
    answerable.update(
        {
            "case_type": "WEAK",
            "answerable": False,
            "expected_decision": "ABSTAIN",
            "decision": "ABSTAIN",
        }
    )
    assert _gate_v2(
        EvaluationResult.model_validate(truth_mismatch), dataset, evidence, calibration, context
    ).outcome == GateOutcome.NOT_EVALUATED_BLOCKED

    sidecar_mismatch = result.model_dump(mode="json")
    sidecar_mismatch["decisions"][0]["policy_ref"] = "other-policy"
    assert _gate_v2(
        EvaluationResult.model_validate(sidecar_mismatch), dataset, evidence, calibration, context
    ).outcome == GateOutcome.NOT_EVALUATED_BLOCKED


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate"])
def test_gate_v2_blocks_decision_identity_mismatch(mutation: str) -> None:
    dataset, evidence, _, calibration, context, result = _validated_chain()
    payload = result.model_dump(mode="json")
    if mutation == "missing":
        payload["decisions"] = payload["decisions"][:-1]
    elif mutation == "duplicate":
        payload["decisions"].append(deepcopy(payload["decisions"][0]))
    else:
        extra = deepcopy(payload["decisions"][0])
        extra["case_id"] = "extra-evaluation-case"
        extra["decision_id"] = "extra-decision"
        payload["decisions"].append(extra)
    assert _gate_v2(
        EvaluationResult.model_validate(payload), dataset, evidence, calibration, context
    ).outcome == GateOutcome.NOT_EVALUATED_BLOCKED


def test_sidecar_evidence_and_report_privacy() -> None:
    _, evidence, validated, _, _, result = _validated_chain()
    canaries = ("SECRET_QUERY_WP4", "SECRET_CHUNK_WP4", r"C:\secret\model", "raw stacktrace")
    assert privacy_safe_serialization(evidence, canaries)
    assert privacy_safe_serialization(validated, canaries)
    assert privacy_safe_serialization(result, canaries)
    with pytest.raises(ValidationError):
        type(result.decisions[0]).model_validate(
            {**result.decisions[0].model_dump(mode="json"), "query_plaintext": "SECRET_QUERY_WP4"}
        )


def test_runner_help_does_not_attempt_network(monkeypatch, capsys) -> None:
    attempts: list[object] = []

    def fail_connect(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("network attempt during --help")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    spec = importlib.util.spec_from_file_location("wp4_no_answer_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit) as exc:
        module.main(["--help"])
    assert exc.value.code == 0
    assert attempts == []
    assert "--rrf-evidence" in capsys.readouterr().out
