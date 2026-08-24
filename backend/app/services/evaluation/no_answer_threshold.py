"""WP4 No-Answer threshold 的 deterministic calibration、lock、evaluation 与 Gate。"""

# ruff: noqa: D101, D102, D415

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.evaluation.dataset import (
    AnswerabilityExpectedDecision,
    AnswerabilitySplit,
    EvaluationCase,
    EvaluationDataset,
)
from app.core.evaluation.no_answer import (
    BM25_CHANNEL_REF,
    CORPUS_REF,
    CURRENT_CHANNEL_REF,
    FINAL_CANDIDATE_LIMIT,
    PER_CHANNEL_CANDIDATE_LIMIT,
    PRE_FUSION_UNION_LIMIT,
    RRF_BASELINE_REF,
    RRF_K,
    WP2_BM25_CACHE_IDENTITY,
    WP2_DENSE_CACHE_IDENTITY,
    WP4_BM25_CACHE_IDENTITY,
    WP4_CHUNK_MANIFEST_DIGEST,
    WP4_DENSE_CACHE_IDENTITY,
    WP4_RRF_SUBSTRATE_REF,
    WP4_SOURCE_MANIFEST_DIGEST,
    ConfusionMetrics,
    NoAnswerDecisionSidecar,
    NoAnswerDecisionValue,
    NoAnswerPolicy,
    NoAnswerPolicyConfig,
    NoAnswerProtocolError,
    NoAnswerSignal,
    RrfEvidenceEnvelope,
    RrfEvidenceEnvelopeV2,
    calculate_confusion,
)

DATASET_ASSET_REF = "no-answer-threshold-dataset.v2"
DATASET_SCHEMA_VERSION = "evaluation-dataset.v4"
GATE_V1_REF = "WP4_NO_ANSWER_ACCEPTANCE_GATE.v1"
GATE_V2_REF = "WP4_NO_ANSWER_ACCEPTANCE_GATE.v2"
GATE_V3_REF = "WP4_NO_ANSWER_ACCEPTANCE_GATE.v3"
GATE_REF = GATE_V2_REF
REPORT_SCHEMA_V2 = "no-answer-threshold-report.v2"
REPORT_SCHEMA_V3 = "no-answer-threshold-report.v3"
CALIBRATION_FAILURE = "CALIBRATION_FAILED_NO_FEASIBLE_POLICY"
VALIDATED_PROOF_V1 = "validated-no-answer-experiment.v1"
VALIDATED_PROOF_V2 = "validated-no-answer-experiment.v2"


def canonical_digest(value: object) -> str:
    """以 canonical JSON 生成稳定 SHA-256。"""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FrozenRrfConfig(BaseModel):
    """WP4 唯一允许的固定 retrieval substrate identity。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm_ref: Literal["rrf.v1"] = RRF_BASELINE_REF
    rrf_k: Literal[60] = RRF_K
    dense_channel_ref: Literal["current-dense-led-ranked.v1"] = CURRENT_CHANNEL_REF
    bm25_channel_ref: Literal["bm25-lucene-idf.v1"] = BM25_CHANNEL_REF
    per_channel_candidate_limit: Literal[8] = PER_CHANNEL_CANDIDATE_LIMIT
    pre_fusion_union_limit: Literal[16] = PRE_FUSION_UNION_LIMIT
    final_fused_candidate_limit: Literal[8] = FINAL_CANDIDATE_LIMIT
    corpus_ref: Literal["rag-evaluation-corpus.v1"] = CORPUS_REF
    dense_cache_identity: Literal[
        "b63c0bbd115150da766b84a80331b332010812bbb757a6782df9ae2224ca8f46"
    ] = WP2_DENSE_CACHE_IDENTITY
    bm25_cache_identity: Literal[
        "594c9c95a3b6f29bcd2fcd738e23ee345427d06b49a70ac3149544a1a4f8f84b"
    ] = WP2_BM25_CACHE_IDENTITY

    @classmethod
    def from_evidence(cls, evidence: RrfEvidenceEnvelope) -> "FrozenRrfConfig":
        return cls(
            algorithm_ref=evidence.algorithm_ref,
            rrf_k=evidence.rrf_k,
            dense_channel_ref=evidence.dense_channel_ref,
            bm25_channel_ref=evidence.bm25_channel_ref,
            per_channel_candidate_limit=evidence.per_channel_candidate_limit,
            pre_fusion_union_limit=evidence.pre_fusion_union_limit,
            final_fused_candidate_limit=evidence.final_candidate_limit,
            corpus_ref=evidence.corpus_ref,
            dense_cache_identity=evidence.dense_cache_identity,
            bm25_cache_identity=evidence.bm25_cache_identity,
        )


class FrozenRrfConfigV2(BaseModel):
    """WP4 synthetic substrate v2 唯一允许的固定 retrieval substrate identity。

    显式绑定 substrate_ref、两个 synthetic cache identities 与 source/chunk
    manifest digests；不再隐式依赖两个 cache hash 识别 substrate。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    substrate_ref: Literal["wp4-no-answer-rrf-substrate.v2"] = WP4_RRF_SUBSTRATE_REF
    algorithm_ref: Literal["rrf.v1"] = RRF_BASELINE_REF
    rrf_k: Literal[60] = RRF_K
    dense_channel_ref: Literal["current-dense-led-ranked.v1"] = CURRENT_CHANNEL_REF
    bm25_channel_ref: Literal["bm25-lucene-idf.v1"] = BM25_CHANNEL_REF
    per_channel_candidate_limit: Literal[8] = PER_CHANNEL_CANDIDATE_LIMIT
    pre_fusion_union_limit: Literal[16] = PRE_FUSION_UNION_LIMIT
    final_fused_candidate_limit: Literal[8] = FINAL_CANDIDATE_LIMIT
    corpus_ref: Literal["rag-evaluation-corpus.v1"] = CORPUS_REF
    source_manifest_digest: Literal[
        "4da8c504a8ad77ae6c8dd9ec004c7178f26fe5ee7be1a4cf94b822bce9b427f6"
    ] = WP4_SOURCE_MANIFEST_DIGEST
    chunk_manifest_digest: Literal[
        "149a39a7d6b45fb7484f934288037f787b6322dd13d135fd721b4a1d5117cc91"
    ] = WP4_CHUNK_MANIFEST_DIGEST
    dense_cache_identity: Literal[
        "92c4743c308e914e311345c22cb09f633a8bb89a6dd73e3820f45cb167046616"
    ] = WP4_DENSE_CACHE_IDENTITY
    bm25_cache_identity: Literal[
        "33040278c1995934df185be2c625fb2e45f0950436e0d748f03e80950e65c4f9"
    ] = WP4_BM25_CACHE_IDENTITY

    @classmethod
    def from_evidence(cls, evidence: RrfEvidenceEnvelopeV2) -> "FrozenRrfConfigV2":
        return cls(
            substrate_ref=evidence.substrate_ref,
            algorithm_ref=evidence.algorithm_ref,
            rrf_k=evidence.rrf_k,
            dense_channel_ref=evidence.dense_channel_ref,
            bm25_channel_ref=evidence.bm25_channel_ref,
            per_channel_candidate_limit=evidence.per_channel_candidate_limit,
            pre_fusion_union_limit=evidence.pre_fusion_union_limit,
            final_fused_candidate_limit=evidence.final_candidate_limit,
            corpus_ref=evidence.corpus_ref,
            source_manifest_digest=evidence.source_manifest_digest,
            chunk_manifest_digest=evidence.chunk_manifest_digest,
            dense_cache_identity=evidence.dense_cache_identity,
            bm25_cache_identity=evidence.bm25_cache_identity,
        )


class CandidateGrid(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    top1_thresholds: tuple[float, ...]
    margin_thresholds: tuple[float, ...]
    digest: str


def _axis(values: Sequence[float]) -> tuple[float, ...]:
    observed = sorted(set(values))
    if not observed or any(not math.isfinite(value) for value in observed):
        raise NoAnswerProtocolError("candidate grid requires finite observed values")
    candidates = {
        math.nextafter(observed[0], -math.inf),
        math.nextafter(observed[-1], math.inf),
        *observed,
    }
    candidates.update((left + right) / 2 for left, right in zip(observed, observed[1:], strict=False))
    return tuple(sorted(candidates))


def build_candidate_grid(signals: Sequence[NoAnswerSignal]) -> CandidateGrid:
    """从 calibration observed values + midpoint + nextafter sentinels 机械构造 grid。"""
    top1 = [signal.top1_score for signal in signals if signal.top1_score is not None]
    margins = [signal.top1_top2_margin for signal in signals if signal.top1_top2_margin is not None]
    top1_axis = _axis(top1)
    margin_axis = _axis(margins)
    digest = canonical_digest({"top1_thresholds": top1_axis, "margin_thresholds": margin_axis})
    return CandidateGrid(top1_thresholds=top1_axis, margin_thresholds=margin_axis, digest=digest)


def validate_no_answer_dataset(dataset: EvaluationDataset) -> dict[str, dict[str, int]]:
    """验证 v2 minimum coverage、identity、corpus 与 semantic leakage split isolation。"""
    if dataset.dataset_schema_version != DATASET_SCHEMA_VERSION:
        raise NoAnswerProtocolError("DATASET_SCHEMA_IDENTITY_MISMATCH")
    if dataset.dataset_id != "no-answer-threshold-dataset" or dataset.version != "v2":
        raise NoAnswerProtocolError("DATASET_ASSET_IDENTITY_MISMATCH")
    counts: Counter[tuple[str, str]] = Counter()
    leakage: dict[str, str] = {}
    for case in dataset.cases:
        truth = case.ground_truth.answerability
        if truth is None:
            raise NoAnswerProtocolError("WP4_DATASET_ANSWERABILITY_REQUIRED")
        if truth.corpus_ref != CORPUS_REF:
            raise NoAnswerProtocolError("DATASET_CORPUS_IDENTITY_MISMATCH")
        counts[(truth.split.value, truth.case_type.value)] += 1
        group = str(case.metadata["leakage_group"])
        previous = leakage.setdefault(group, truth.split.value)
        if previous != truth.split.value:
            raise NoAnswerProtocolError("DATASET_LEAKAGE_CROSS_SPLIT")
    required_minima = {
        ("CALIBRATION", "ANSWERABLE"): 4,
        ("CALIBRATION", "EMPTY"): 4,
        ("CALIBRATION", "WEAK"): 4,
        ("CALIBRATION", "MISLEADING"): 2,
        ("EVALUATION", "ANSWERABLE"): 4,
        ("EVALUATION", "EMPTY"): 4,
        ("EVALUATION", "WEAK"): 4,
        ("EVALUATION", "MISLEADING"): 2,
    }
    if any(counts[key] < minimum for key, minimum in required_minima.items()):
        raise NoAnswerProtocolError("DATASET_NOT_READY")
    allowed = {*required_minima, ("DIAGNOSTIC", "CONFLICT")}
    if any(key not in allowed for key in counts):
        raise NoAnswerProtocolError("DATASET_SPLIT_CASE_TYPE_INVALID")
    result: dict[str, dict[str, int]] = {}
    for (split, case_type), count in sorted(counts.items()):
        result.setdefault(split, {})[case_type] = count
    return result


def _case_map(cases: Sequence[EvaluationCase], split: AnswerabilitySplit) -> dict[str, EvaluationCase]:
    result: dict[str, EvaluationCase] = {}
    for case in cases:
        truth = case.ground_truth.answerability
        if truth is None or truth.split != split:
            raise NoAnswerProtocolError(f"cases must be explicit {split.value} split")
        if case.case_id in result:
            raise NoAnswerProtocolError("duplicate case identity")
        result[case.case_id] = case
    return result


def _signal_map(signals: Sequence[NoAnswerSignal]) -> dict[str, NoAnswerSignal]:
    result: dict[str, NoAnswerSignal] = {}
    for signal in signals:
        if signal.case_id in result:
            raise NoAnswerProtocolError("duplicate signal case identity")
        result[signal.case_id] = signal
    return result


def _align(cases: Mapping[str, EvaluationCase], signals: Sequence[NoAnswerSignal]) -> dict[str, NoAnswerSignal]:
    by_case = _signal_map(signals)
    if by_case.keys() != cases.keys():
        raise NoAnswerProtocolError("case/signal alignment failed")
    return by_case


def _dataset_split_digest(cases: Mapping[str, EvaluationCase]) -> str:
    return canonical_digest([cases[case_id].model_dump(mode="json") for case_id in sorted(cases)])


def _split_evidence_digest(
    cases: Mapping[str, EvaluationCase], signals: Mapping[str, NoAnswerSignal]
) -> str:
    return canonical_digest(
        [
            {
                "case": cases[case_id].model_dump(mode="json"),
                "signal": signals[case_id].model_dump(mode="json"),
            }
            for case_id in sorted(cases)
        ]
    )


class ExpectedEvaluationCaseFact(BaseModel):
    """Validated Dataset 中 Gate 必须匹配的 evaluation Ground Truth。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    case_type: Literal["ANSWERABLE", "EMPTY", "WEAK", "MISLEADING"]
    answerable: bool
    expected_decision: NoAnswerDecisionValue


class ValidatedExperimentInvariants(BaseModel):
    """只能由 strict Dataset/evidence validator 产生并可自校验的 proof。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["validated-no-answer-experiment.v1"] = "validated-no-answer-experiment.v1"
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    corpus_ref: str
    rrf_config: FrozenRrfConfig
    coverage: dict[str, dict[str, int]]
    calibration_case_ids: tuple[str, ...]
    evaluation_case_ids: tuple[str, ...]
    evaluation_ground_truth: tuple[ExpectedEvaluationCaseFact, ...]
    calibration_split_digest: str
    evaluation_split_digest: str
    calibration_evidence_digest: str
    evaluation_evidence_digest: str
    evidence_digest: str
    signals: tuple[NoAnswerSignal, ...]
    proof_digest: str

    @model_validator(mode="after")
    def _identities(self) -> "ValidatedExperimentInvariants":
        if tuple(sorted(self.calibration_case_ids)) != self.calibration_case_ids:
            raise ValueError("calibration case identities must be canonical")
        if tuple(sorted(self.evaluation_case_ids)) != self.evaluation_case_ids:
            raise ValueError("evaluation case identities must be canonical")
        if set(self.calibration_case_ids) & set(self.evaluation_case_ids):
            raise ValueError("calibration and evaluation cases must be disjoint")
        truth_ids = [fact.case_id for fact in self.evaluation_ground_truth]
        if tuple(truth_ids) != self.evaluation_case_ids:
            raise ValueError("evaluation Ground Truth must exactly match canonical evaluation identities")
        signal_ids = [signal.case_id for signal in self.signals]
        if len(signal_ids) != len(set(signal_ids)):
            raise ValueError("validated signals must have unique case identities")
        if set(signal_ids) != set(self.calibration_case_ids) | set(self.evaluation_case_ids):
            raise ValueError("validated signals must exactly cover eligible cases")
        return self

    def verify(self) -> bool:
        payload = self.model_dump(mode="json", exclude={"proof_digest"})
        return canonical_digest(payload) == self.proof_digest


class ValidatedExperimentInvariantsV2(BaseModel):
    """synthetic substrate v2 的 strict proof；显式携带 substrate_ref。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["validated-no-answer-experiment.v2"] = VALIDATED_PROOF_V2
    substrate_ref: Literal["wp4-no-answer-rrf-substrate.v2"] = WP4_RRF_SUBSTRATE_REF
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    corpus_ref: str
    rrf_config: FrozenRrfConfigV2
    coverage: dict[str, dict[str, int]]
    calibration_case_ids: tuple[str, ...]
    evaluation_case_ids: tuple[str, ...]
    evaluation_ground_truth: tuple[ExpectedEvaluationCaseFact, ...]
    calibration_split_digest: str
    evaluation_split_digest: str
    calibration_evidence_digest: str
    evaluation_evidence_digest: str
    evidence_digest: str
    signals: tuple[NoAnswerSignal, ...]
    proof_digest: str

    @model_validator(mode="after")
    def _identities(self) -> "ValidatedExperimentInvariantsV2":
        if tuple(sorted(self.calibration_case_ids)) != self.calibration_case_ids:
            raise ValueError("calibration case identities must be canonical")
        if tuple(sorted(self.evaluation_case_ids)) != self.evaluation_case_ids:
            raise ValueError("evaluation case identities must be canonical")
        if set(self.calibration_case_ids) & set(self.evaluation_case_ids):
            raise ValueError("calibration and evaluation cases must be disjoint")
        truth_ids = [fact.case_id for fact in self.evaluation_ground_truth]
        if tuple(truth_ids) != self.evaluation_case_ids:
            raise ValueError("evaluation Ground Truth must exactly match canonical evaluation identities")
        signal_ids = [signal.case_id for signal in self.signals]
        if len(signal_ids) != len(set(signal_ids)):
            raise ValueError("validated signals must have unique case identities")
        if set(signal_ids) != set(self.calibration_case_ids) | set(self.evaluation_case_ids):
            raise ValueError("validated signals must exactly cover eligible cases")
        return self

    def verify(self) -> bool:
        payload = self.model_dump(mode="json", exclude={"proof_digest"})
        return canonical_digest(payload) == self.proof_digest


def _validate_evidence_impl(
    dataset: EvaluationDataset,
    evidence: RrfEvidenceEnvelope | RrfEvidenceEnvelopeV2,
    *,
    evidence_type: type[RrfEvidenceEnvelope] | type[RrfEvidenceEnvelopeV2],
    rrf_config_type: type[FrozenRrfConfig] | type[FrozenRrfConfigV2],
    invariants_type: type[ValidatedExperimentInvariants] | type[ValidatedExperimentInvariantsV2],
    schema_version: str,
    include_substrate: bool,
) -> ValidatedExperimentInvariants | ValidatedExperimentInvariantsV2:
    """从 strict Dataset + evidence 机械生成 hard-invariant proof（v1/v2 共享实现）。"""
    # 不信任调用方可能通过 model_construct() 绕过的 Pydantic 实例；Authority
    # boundary 必须从 wire payload 重新执行完整 strict validation。
    dataset = EvaluationDataset.model_validate(dataset.model_dump(mode="json"))
    evidence = evidence_type.model_validate(evidence.model_dump(mode="json"))
    coverage = validate_no_answer_dataset(dataset)
    dataset_digest = canonical_digest(dataset.model_dump(mode="json"))
    if (
        evidence.dataset_id != dataset.dataset_id
        or evidence.dataset_version != dataset.version
        or evidence.dataset_digest != dataset_digest
        or evidence.corpus_ref != CORPUS_REF
    ):
        raise NoAnswerProtocolError("EVIDENCE_DATASET_IDENTITY_MISMATCH")

    split_cases = {
        split: _case_map(
            [
                case
                for case in dataset.cases
                if case.ground_truth.answerability is not None
                and case.ground_truth.answerability.split == split
            ],
            split,
        )
        for split in (AnswerabilitySplit.CALIBRATION, AnswerabilitySplit.EVALUATION)
    }
    evidence_by_case = {case.case_id: case for case in evidence.cases}
    expected_ids = set(split_cases[AnswerabilitySplit.CALIBRATION]) | set(
        split_cases[AnswerabilitySplit.EVALUATION]
    )
    if set(evidence_by_case) != expected_ids:
        raise NoAnswerProtocolError("EVIDENCE_CASE_COVERAGE_MISMATCH")

    signals: list[NoAnswerSignal] = []
    for case_id in sorted(expected_ids):
        case = (
            split_cases[AnswerabilitySplit.CALIBRATION].get(case_id)
            or split_cases[AnswerabilitySplit.EVALUATION][case_id]
        )
        raw = evidence_by_case[case_id]
        query = case.input.get("query")
        if not isinstance(query, str) or not query:
            raise NoAnswerProtocolError("DATASET_QUERY_REQUIRED")
        expected_query_digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
        if raw.query_sha256 != expected_query_digest:
            raise NoAnswerProtocolError("EVIDENCE_QUERY_DIGEST_MISMATCH")
        signals.append(
            NoAnswerSignal(
                case_id=raw.case_id,
                query_sha256=raw.query_sha256,
                retrieval_artifact_id=raw.retrieval_artifact_id,
                retrieval_status=raw.retrieval_status,
                retrieved_candidate_count=raw.retrieved_candidate_count,
                ranked_candidate_count=raw.ranked_candidate_count,
                candidate_count=len(raw.ranked_candidates),
                rrf_scores=tuple(item.rrf_score for item in raw.ranked_candidates),
            )
        )
    signal_map = _signal_map(signals)
    calibration_cases = split_cases[AnswerabilitySplit.CALIBRATION]
    evaluation_cases = split_cases[AnswerabilitySplit.EVALUATION]
    calibration_signals = {case_id: signal_map[case_id] for case_id in calibration_cases}
    evaluation_signals = {case_id: signal_map[case_id] for case_id in evaluation_cases}
    payload = {
        "schema_version": schema_version,
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.version,
        "dataset_digest": dataset_digest,
        "corpus_ref": CORPUS_REF,
        "rrf_config": rrf_config_type.from_evidence(evidence).model_dump(mode="json"),
        "coverage": coverage,
        "calibration_case_ids": tuple(sorted(calibration_cases)),
        "evaluation_case_ids": tuple(sorted(evaluation_cases)),
        "evaluation_ground_truth": tuple(
            {
                "case_id": case_id,
                "case_type": evaluation_cases[case_id].ground_truth.answerability.case_type.value,
                "answerable": evaluation_cases[case_id].ground_truth.answerability.answerable,
                "expected_decision": evaluation_cases[
                    case_id
                ].ground_truth.answerability.expected_decision.value,
            }
            for case_id in sorted(evaluation_cases)
        ),
        "calibration_split_digest": _dataset_split_digest(calibration_cases),
        "evaluation_split_digest": _dataset_split_digest(evaluation_cases),
        "calibration_evidence_digest": _split_evidence_digest(calibration_cases, calibration_signals),
        "evaluation_evidence_digest": _split_evidence_digest(evaluation_cases, evaluation_signals),
        "evidence_digest": canonical_digest(evidence.model_dump(mode="json")),
        "signals": tuple(signal.model_dump(mode="json") for signal in signals),
    }
    if include_substrate:
        payload["substrate_ref"] = WP4_RRF_SUBSTRATE_REF
    proof = invariants_type(**payload, proof_digest=canonical_digest(payload))
    if not proof.verify():  # pragma: no cover
        raise NoAnswerProtocolError("EXPERIMENT_PROOF_INVALID")
    return proof


def validate_experiment_evidence(
    dataset: EvaluationDataset, evidence: RrfEvidenceEnvelope
) -> ValidatedExperimentInvariants:
    """从 v1 strict Dataset + evidence 机械生成 hard-invariant proof。"""
    return _validate_evidence_impl(
        dataset,
        evidence,
        evidence_type=RrfEvidenceEnvelope,
        rrf_config_type=FrozenRrfConfig,
        invariants_type=ValidatedExperimentInvariants,
        schema_version=VALIDATED_PROOF_V1,
        include_substrate=False,
    )


def validate_experiment_evidence_v2(
    dataset: EvaluationDataset, evidence: RrfEvidenceEnvelopeV2
) -> ValidatedExperimentInvariantsV2:
    """从 v2 strict Dataset + synthetic evidence 机械生成 hard-invariant proof。"""
    return _validate_evidence_impl(
        dataset,
        evidence,
        evidence_type=RrfEvidenceEnvelopeV2,
        rrf_config_type=FrozenRrfConfigV2,
        invariants_type=ValidatedExperimentInvariantsV2,
        schema_version=VALIDATED_PROOF_V2,
        include_substrate=True,
    )


def signals_for_split(
    validated: ValidatedExperimentInvariants | ValidatedExperimentInvariantsV2,
    split: AnswerabilitySplit,
) -> tuple[NoAnswerSignal, ...]:
    """从已验证 proof 返回指定 hard split 的 exact signals。"""
    if not validated.verify():
        raise NoAnswerProtocolError("EXPERIMENT_PROOF_INVALID")
    if split not in {AnswerabilitySplit.CALIBRATION, AnswerabilitySplit.EVALUATION}:
        raise NoAnswerProtocolError("signals are only defined for hard Dataset splits")
    expected = set(
        validated.calibration_case_ids
        if split == AnswerabilitySplit.CALIBRATION
        else validated.evaluation_case_ids
    )
    return tuple(signal for signal in validated.signals if signal.case_id in expected)


class EvaluationContextIdentity(BaseModel):
    """当前 evaluation Dataset/RRF/evidence 的 strict identity（historical v1）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    dataset_version: str
    dataset_digest: str
    corpus_ref: str
    rrf_config: FrozenRrfConfig
    evaluation_case_ids: tuple[str, ...]
    evaluation_split_digest: str
    evaluation_evidence_digest: str
    evidence_digest: str
    context_digest: str

    def verify(self) -> bool:
        payload = self.model_dump(mode="json", exclude={"context_digest"})
        return canonical_digest(payload) == self.context_digest


class EvaluationContextIdentityV2(BaseModel):
    """synthetic substrate v2 evaluation context；显式绑定 substrate_ref。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    substrate_ref: Literal["wp4-no-answer-rrf-substrate.v2"] = WP4_RRF_SUBSTRATE_REF
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    corpus_ref: str
    rrf_config: FrozenRrfConfigV2
    evaluation_case_ids: tuple[str, ...]
    evaluation_split_digest: str
    evaluation_evidence_digest: str
    evidence_digest: str
    context_digest: str

    def verify(self) -> bool:
        payload = self.model_dump(mode="json", exclude={"context_digest"})
        return canonical_digest(payload) == self.context_digest


def _build_context_impl(
    validated: ValidatedExperimentInvariants | ValidatedExperimentInvariantsV2,
    *,
    context_type: type[EvaluationContextIdentity] | type[EvaluationContextIdentityV2],
    include_substrate: bool,
) -> EvaluationContextIdentity | EvaluationContextIdentityV2:
    """从已验证 proof 机械派生 evaluation context identity（v1/v2 共享实现）。"""
    if not validated.verify():
        raise NoAnswerProtocolError("EXPERIMENT_PROOF_INVALID")
    payload = {
        "dataset_id": validated.dataset_id,
        "dataset_version": validated.dataset_version,
        "dataset_digest": validated.dataset_digest,
        "corpus_ref": validated.corpus_ref,
        "rrf_config": validated.rrf_config.model_dump(mode="json"),
        "evaluation_case_ids": validated.evaluation_case_ids,
        "evaluation_split_digest": validated.evaluation_split_digest,
        "evaluation_evidence_digest": validated.evaluation_evidence_digest,
        "evidence_digest": validated.evidence_digest,
    }
    if include_substrate:
        payload["substrate_ref"] = validated.substrate_ref
    return context_type(**payload, context_digest=canonical_digest(payload))


def build_evaluation_context(validated: ValidatedExperimentInvariants) -> EvaluationContextIdentity:
    """从 v1 proof 机械派生 evaluation context identity。"""
    return _build_context_impl(validated, context_type=EvaluationContextIdentity, include_substrate=False)


def build_evaluation_context_v2(
    validated: ValidatedExperimentInvariantsV2,
) -> EvaluationContextIdentityV2:
    """从 v2 proof 机械派生携带 substrate_ref 的 evaluation context identity。"""
    return _build_context_impl(validated, context_type=EvaluationContextIdentityV2, include_substrate=True)


class LockedPolicy(BaseModel):
    """Calibration 结束后不可变的 policy/config/identity lock（historical v1）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_ref: str
    policy_config: NoAnswerPolicyConfig
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    corpus_ref: str
    rrf_config: FrozenRrfConfig
    calibration_split_digest: str
    calibration_evidence_digest: str
    evaluation_split_digest: str
    evaluation_evidence_digest: str
    evidence_digest: str
    candidate_grid_digest: str
    lock_digest: str

    def verify(self) -> bool:
        payload = self.model_dump(mode="json", exclude={"lock_digest"})
        return canonical_digest(payload) == self.lock_digest


class LockedPolicyV2(BaseModel):
    """Synthetic substrate v2 的不可变 policy lock；显式绑定 substrate_ref。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_ref: str
    policy_config: NoAnswerPolicyConfig
    substrate_ref: Literal["wp4-no-answer-rrf-substrate.v2"] = WP4_RRF_SUBSTRATE_REF
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    corpus_ref: str
    rrf_config: FrozenRrfConfigV2
    calibration_split_digest: str
    calibration_evidence_digest: str
    evaluation_split_digest: str
    evaluation_evidence_digest: str
    evidence_digest: str
    candidate_grid_digest: str
    lock_digest: str

    def verify(self) -> bool:
        payload = self.model_dump(mode="json", exclude={"lock_digest"})
        return canonical_digest(payload) == self.lock_digest


class CalibrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    locked_policy: LockedPolicy
    candidate_grid: CandidateGrid
    feasible_policy_count: int = Field(gt=0)
    calibration_metrics: ConfusionMetrics


class CalibrationResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    locked_policy: LockedPolicyV2
    candidate_grid: CandidateGrid
    feasible_policy_count: int = Field(gt=0)
    calibration_metrics: ConfusionMetrics


def _decisions_for_config(
    cases: Mapping[str, EvaluationCase],
    signals: Mapping[str, NoAnswerSignal],
    config: NoAnswerPolicyConfig,
    *,
    split: Literal["CALIBRATION", "EVALUATION"],
    policy_ref: str,
    dataset_id: str,
    dataset_version: str,
) -> tuple[list[NoAnswerDecisionSidecar], ConfusionMetrics]:
    decisions: list[NoAnswerDecisionSidecar] = []
    labeled: list[tuple[bool, NoAnswerDecisionValue]] = []
    technical = 0
    for case_id in sorted(cases):
        decision = NoAnswerPolicy.decide(
            signal=signals[case_id],
            config=config,
            policy_ref=policy_ref,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            split=split,
        )
        if decision is None:
            technical += 1
            continue
        decisions.append(decision)
        truth = cases[case_id].ground_truth.answerability
        assert truth is not None
        labeled.append((truth.answerable, decision.decision))
    return decisions, calculate_confusion(labeled, technical_failure_count=technical)


def _calibrate_core(
    *,
    calibration_cases: Sequence[EvaluationCase],
    calibration_signals: Sequence[NoAnswerSignal],
    validated_experiment: ValidatedExperimentInvariants | ValidatedExperimentInvariantsV2,
    lock_type: type[LockedPolicy] | type[LockedPolicyV2],
) -> tuple[LockedPolicy | LockedPolicyV2, CandidateGrid, int, ConfusionMetrics]:
    """仅接受 validated CALIBRATION cases/signals；API 结构上隔离 evaluation labels。"""
    if not validated_experiment.verify():
        raise NoAnswerProtocolError("EXPERIMENT_PROOF_INVALID")
    cases = _case_map(calibration_cases, AnswerabilitySplit.CALIBRATION)
    signals = _align(cases, calibration_signals)
    if tuple(sorted(cases)) != validated_experiment.calibration_case_ids:
        raise NoAnswerProtocolError("CALIBRATION_CASE_IDENTITY_MISMATCH")
    if _dataset_split_digest(cases) != validated_experiment.calibration_split_digest:
        raise NoAnswerProtocolError("CALIBRATION_SPLIT_DIGEST_MISMATCH")
    if _split_evidence_digest(cases, signals) != validated_experiment.calibration_evidence_digest:
        raise NoAnswerProtocolError("CALIBRATION_EVIDENCE_DIGEST_MISMATCH")
    grid = build_candidate_grid(tuple(signals.values()))
    feasible: list[tuple[NoAnswerPolicyConfig, ConfusionMetrics]] = []
    for top1 in grid.top1_thresholds:
        for margin in grid.margin_thresholds:
            config = NoAnswerPolicyConfig(min_top1_score=top1, min_top1_top2_margin=margin)
            _, metrics = _decisions_for_config(
                cases,
                signals,
                config,
                split="CALIBRATION",
                policy_ref="calibration-candidate",
                dataset_id=validated_experiment.dataset_id,
                dataset_version=validated_experiment.dataset_version,
            )
            if metrics.technical_failure_count:
                raise NoAnswerProtocolError("CALIBRATION_TECHNICAL_FAILURE")
            if metrics.false_answer_count == 0 and metrics.true_answer_count > metrics.false_abstain_count:
                feasible.append((config, metrics))
    if not feasible:
        raise NoAnswerProtocolError(CALIBRATION_FAILURE)
    selected_config, selected_metrics = min(
        feasible,
        key=lambda item: (
            item[1].false_abstain_count,
            -item[0].min_top1_score,
            -item[0].min_top1_top2_margin,
        ),
    )
    policy_identity = {
        "policy_config": selected_config.model_dump(mode="json"),
        "dataset_id": validated_experiment.dataset_id,
        "dataset_version": validated_experiment.dataset_version,
        "dataset_digest": validated_experiment.dataset_digest,
        "corpus_ref": validated_experiment.corpus_ref,
        "rrf_config": validated_experiment.rrf_config.model_dump(mode="json"),
        "calibration_split_digest": validated_experiment.calibration_split_digest,
        "calibration_evidence_digest": validated_experiment.calibration_evidence_digest,
        "evaluation_split_digest": validated_experiment.evaluation_split_digest,
        "evaluation_evidence_digest": validated_experiment.evaluation_evidence_digest,
        "evidence_digest": validated_experiment.evidence_digest,
        "candidate_grid_digest": grid.digest,
    }
    substrate_ref = getattr(validated_experiment, "substrate_ref", None)
    if substrate_ref is not None:
        policy_identity["substrate_ref"] = substrate_ref
    policy_ref = f"no-answer-policy-{canonical_digest(policy_identity)}"
    lock_payload = {"policy_ref": policy_ref, **policy_identity}
    locked = lock_type(**lock_payload, lock_digest=canonical_digest(lock_payload))
    return locked, grid, len(feasible), selected_metrics


def calibrate(
    *,
    calibration_cases: Sequence[EvaluationCase],
    calibration_signals: Sequence[NoAnswerSignal],
    validated_experiment: ValidatedExperimentInvariants,
) -> CalibrationResult:
    """仅接受 validated CALIBRATION cases/signals；API 结构上隔离 evaluation labels。"""
    locked, grid, feasible_count, metrics = _calibrate_core(
        calibration_cases=calibration_cases,
        calibration_signals=calibration_signals,
        validated_experiment=validated_experiment,
        lock_type=LockedPolicy,
    )
    return CalibrationResult(
        locked_policy=locked,
        candidate_grid=grid,
        feasible_policy_count=feasible_count,
        calibration_metrics=metrics,
    )


def calibrate_v2(
    *,
    calibration_cases: Sequence[EvaluationCase],
    calibration_signals: Sequence[NoAnswerSignal],
    validated_experiment: ValidatedExperimentInvariantsV2,
) -> CalibrationResultV2:
    """Synthetic substrate v2 calibration；锁与 proof 显式绑定 substrate_ref。"""
    locked, grid, feasible_count, metrics = _calibrate_core(
        calibration_cases=calibration_cases,
        calibration_signals=calibration_signals,
        validated_experiment=validated_experiment,
        lock_type=LockedPolicyV2,
    )
    return CalibrationResultV2(
        locked_policy=locked,
        candidate_grid=grid,
        feasible_policy_count=feasible_count,
        calibration_metrics=metrics,
    )


class SubtypeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(ge=0)
    correct_count: int = Field(ge=0)


class EvaluationCaseFact(BaseModel):
    """Gate summary 的逐 case 唯一 Authority。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    case_type: Literal["ANSWERABLE", "EMPTY", "WEAK", "MISLEADING"]
    answerable: bool
    expected_decision: NoAnswerDecisionValue
    decision: NoAnswerDecisionValue | None

    @model_validator(mode="after")
    def _truth_semantics(self) -> "EvaluationCaseFact":
        if self.case_type == "ANSWERABLE":
            if not self.answerable or self.expected_decision != NoAnswerDecisionValue.ANSWER:
                raise ValueError("ANSWERABLE case fact is inconsistent")
        elif self.answerable or self.expected_decision != NoAnswerDecisionValue.ABSTAIN:
            raise ValueError("negative case fact is inconsistent")
        return self


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decisions: tuple[NoAnswerDecisionSidecar, ...]
    case_facts: tuple[EvaluationCaseFact, ...]
    metrics: ConfusionMetrics
    subtype_results: dict[str, SubtypeResult]

    @property
    def unique_case_decisions(self) -> bool:
        return len({decision.case_id for decision in self.decisions}) == len(self.decisions)


def derive_evaluation_summary(
    case_facts: Sequence[EvaluationCaseFact],
) -> tuple[ConfusionMetrics, dict[str, SubtypeResult]]:
    """从逐 case truth + decision 单一派生 confusion、metrics 与 subtype summary。"""
    case_ids = [fact.case_id for fact in case_facts]
    if len(case_ids) != len(set(case_ids)):
        raise NoAnswerProtocolError("duplicate evaluation case facts")
    labeled = [(fact.answerable, fact.decision) for fact in case_facts if fact.decision is not None]
    technical = sum(fact.decision is None for fact in case_facts)
    metrics = calculate_confusion(labeled, technical_failure_count=technical)
    subtype: dict[str, SubtypeResult] = {}
    for case_type in ("ANSWERABLE", "EMPTY", "WEAK", "MISLEADING"):
        selected = [fact for fact in case_facts if fact.case_type == case_type]
        subtype[case_type] = SubtypeResult(
            count=len(selected),
            correct_count=sum(fact.decision == fact.expected_decision for fact in selected),
        )
    return metrics, subtype


def _verify_evaluation_context_against_lock(
    locked_policy: LockedPolicy | LockedPolicyV2,
    evaluation_context: EvaluationContextIdentity | EvaluationContextIdentityV2,
) -> None:
    """v1/v2 共享：lock 与当前 evaluation context 的 exact identity binding。

    v2 lock/context 额外比较 substrate_ref；v1 无该字段时保持历史语义。
    """
    if not locked_policy.verify():
        raise NoAnswerProtocolError("POLICY_LOCK_INVALID")
    if not evaluation_context.verify():
        raise NoAnswerProtocolError("EVALUATION_CONTEXT_INVALID")
    shared_keys = (
        "dataset_id",
        "dataset_version",
        "dataset_digest",
        "corpus_ref",
        "rrf_config",
        "evaluation_split_digest",
        "evaluation_evidence_digest",
        "evidence_digest",
    )
    lock_identity = {key: getattr(locked_policy, key) for key in shared_keys}
    context_identity = {key: getattr(evaluation_context, key) for key in shared_keys}
    if getattr(locked_policy, "substrate_ref", None) is not None:
        lock_identity["substrate_ref"] = locked_policy.substrate_ref
        context_identity["substrate_ref"] = evaluation_context.substrate_ref
    if lock_identity != context_identity:
        raise NoAnswerProtocolError("EVALUATION_CONTEXT_LOCK_MISMATCH")


def evaluate(
    *,
    locked_policy: LockedPolicy | LockedPolicyV2,
    evaluation_context: EvaluationContextIdentity | EvaluationContextIdentityV2,
    evaluation_cases: Sequence[EvaluationCase],
    evaluation_signals: Sequence[NoAnswerSignal],
) -> EvaluationResult:
    """用 immutable lock 一次性评价 exact-bound EVALUATION context。"""
    _verify_evaluation_context_against_lock(locked_policy, evaluation_context)
    cases = _case_map(evaluation_cases, AnswerabilitySplit.EVALUATION)
    signals = _align(cases, evaluation_signals)
    if tuple(sorted(cases)) != evaluation_context.evaluation_case_ids:
        raise NoAnswerProtocolError("EVALUATION_CASE_IDENTITY_MISMATCH")
    if _dataset_split_digest(cases) != evaluation_context.evaluation_split_digest:
        raise NoAnswerProtocolError("EVALUATION_SPLIT_DIGEST_MISMATCH")
    if _split_evidence_digest(cases, signals) != evaluation_context.evaluation_evidence_digest:
        raise NoAnswerProtocolError("EVALUATION_EVIDENCE_DIGEST_MISMATCH")

    decisions: list[NoAnswerDecisionSidecar] = []
    facts: list[EvaluationCaseFact] = []
    for case_id in sorted(cases):
        decision = NoAnswerPolicy.decide(
            signal=signals[case_id],
            config=locked_policy.policy_config,
            policy_ref=locked_policy.policy_ref,
            dataset_id=locked_policy.dataset_id,
            dataset_version=locked_policy.dataset_version,
            split="EVALUATION",
        )
        if decision is not None:
            decisions.append(decision)
        truth = cases[case_id].ground_truth.answerability
        assert truth is not None
        if truth.expected_decision == AnswerabilityExpectedDecision.DIAGNOSTIC_ONLY:
            raise NoAnswerProtocolError("DIAGNOSTIC case entered evaluation")
        facts.append(
            EvaluationCaseFact(
                case_id=case_id,
                case_type=truth.case_type.value,
                answerable=truth.answerable,
                expected_decision=truth.expected_decision.value,
                decision=decision.decision if decision is not None else None,
            )
        )
    metrics, subtype = derive_evaluation_summary(facts)
    return EvaluationResult(
        decisions=tuple(decisions),
        case_facts=tuple(facts),
        metrics=metrics,
        subtype_results=subtype,
    )


class GateOutcome(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    NOT_EVALUATED_BLOCKED = "NOT_EVALUATED_BLOCKED"


class GateInvariants(BaseModel):
    """Gate v1 历史调用契约；v2 runner 不消费调用者自报 bool。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_identity_correct: bool
    coverage_correct: bool
    split_leakage_correct: bool
    rrf_config_correct: bool
    policy_lock_correct: bool
    calibration_only_selection_proof: bool
    forbidden_model_used: bool
    privacy_safe: bool
    runtime_read_only: bool


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_ref: str
    outcome: GateOutcome
    reason_codes: tuple[str, ...]


def _quality_gate(metrics: ConfusionMetrics, gate_ref: str) -> GateResult:
    quality_failures = []
    if metrics.false_answer_count != 0:
        quality_failures.append("false_answer")
    if metrics.true_answer_count <= metrics.false_abstain_count:
        quality_failures.append("false_abstain_guardrail")
    constant_baseline = max(metrics.answerable_count, metrics.unanswerable_count) / metrics.eligible_count
    if metrics.no_answer_accuracy <= constant_baseline:
        quality_failures.append("non_trivial_policy_guardrail")
    if quality_failures:
        return GateResult(gate_ref=gate_ref, outcome=GateOutcome.REJECT, reason_codes=tuple(quality_failures))
    return GateResult(gate_ref=gate_ref, outcome=GateOutcome.ACCEPT, reason_codes=())


def acceptance_gate_v1(
    *, evaluation: EvaluationResult, invariants: GateInvariants, expected_evaluation_count: int = 16
) -> GateResult:
    """保留 Gate v1 的 4/4/4/4 coverage 与历史 bool 输入语义。"""
    hard_failures = [
        name
        for name, value in invariants.model_dump().items()
        if (name == "forbidden_model_used" and value) or (name != "forbidden_model_used" and not value)
    ]
    if evaluation.metrics.technical_failure_count:
        hard_failures.append("technical_failure_count")
    if len(evaluation.decisions) != expected_evaluation_count:
        hard_failures.append("incomplete_evaluation_decisions")
    if not evaluation.unique_case_decisions:
        hard_failures.append("duplicate_evaluation_decisions")
    required_subtypes = {"ANSWERABLE", "EMPTY", "WEAK", "MISLEADING"}
    if set(evaluation.subtype_results) != required_subtypes or any(
        result.count < 4 or result.correct_count > result.count
        for result in evaluation.subtype_results.values()
    ):
        hard_failures.append("minimum_subtype_coverage")
    if hard_failures:
        return GateResult(
            gate_ref=GATE_V1_REF,
            outcome=GateOutcome.NOT_EVALUATED_BLOCKED,
            reason_codes=tuple(sorted(set(hard_failures))),
        )
    return _quality_gate(evaluation.metrics, GATE_V1_REF)


def acceptance_gate(
    *, evaluation: EvaluationResult, invariants: GateInvariants, expected_evaluation_count: int = 16
) -> GateResult:
    """Gate v1 compatibility alias。"""
    return acceptance_gate_v1(
        evaluation=evaluation,
        invariants=invariants,
        expected_evaluation_count=expected_evaluation_count,
    )


def _gate_per_case_checks(
    evaluation: EvaluationResult,
    validated_experiment: ValidatedExperimentInvariants | ValidatedExperimentInvariantsV2,
    locked_policy: LockedPolicy | LockedPolicyV2,
) -> list[str]:
    """v2/v3 共享：per-case/confusion/subtype/identity/privacy consistency checks。"""
    failures: list[str] = []
    expected_ids = set(validated_experiment.evaluation_case_ids)
    fact_ids = [fact.case_id for fact in evaluation.case_facts]
    decision_ids = [decision.case_id for decision in evaluation.decisions]
    if len(fact_ids) != len(set(fact_ids)):
        failures.append("duplicate_evaluation_case_facts")
    if set(fact_ids) != expected_ids:
        failures.append("evaluation_case_identity_mismatch")
    if len(decision_ids) != len(set(decision_ids)):
        failures.append("duplicate_evaluation_decisions")
    if set(decision_ids) != expected_ids:
        failures.append("evaluation_decision_identity_mismatch")

    try:
        derived_metrics, derived_subtypes = derive_evaluation_summary(evaluation.case_facts)
    except NoAnswerProtocolError:
        failures.append("evaluation_summary_not_derivable")
    else:
        if derived_metrics != evaluation.metrics or derived_subtypes != evaluation.subtype_results:
            failures.append("evaluation_summary_contradiction")
        if derived_metrics.technical_failure_count:
            failures.append("technical_failure_count")
        minima = {"ANSWERABLE": 4, "EMPTY": 4, "WEAK": 4, "MISLEADING": 2}
        if set(derived_subtypes) != set(minima) or any(
            derived_subtypes[key].count < minimum for key, minimum in minima.items()
        ):
            failures.append("minimum_subtype_coverage_v2")

    facts_by_id = {fact.case_id: fact for fact in evaluation.case_facts}
    decisions_by_id = {decision.case_id: decision for decision in evaluation.decisions}
    expected_truth = {fact.case_id: fact for fact in validated_experiment.evaluation_ground_truth}
    for case_id in expected_ids & facts_by_id.keys() & expected_truth.keys():
        fact = facts_by_id[case_id]
        truth = expected_truth[case_id]
        if (
            fact.case_type != truth.case_type
            or fact.answerable != truth.answerable
            or fact.expected_decision != truth.expected_decision
        ):
            failures.append("evaluation_ground_truth_contradiction")
            break
    for case_id in expected_ids & facts_by_id.keys() & decisions_by_id.keys():
        if facts_by_id[case_id].decision != decisions_by_id[case_id].decision:
            failures.append("decision_case_fact_contradiction")
            break
    signals_by_id = {signal.case_id: signal for signal in validated_experiment.signals}
    for case_id in expected_ids & decisions_by_id.keys() & signals_by_id.keys():
        decision = decisions_by_id[case_id]
        signal = signals_by_id[case_id]
        if (
            decision.policy_ref != locked_policy.policy_ref
            or decision.dataset_id != locked_policy.dataset_id
            or decision.dataset_version != locked_policy.dataset_version
            or decision.split != "EVALUATION"
            or decision.query_sha256 != signal.query_sha256
            or decision.retrieval_artifact_id != signal.retrieval_artifact_id
        ):
            failures.append("decision_sidecar_identity_mismatch")
            break
    if not privacy_safe_serialization(evaluation, ()):
        failures.append("privacy_safety_failure")
    return failures


def acceptance_gate_v2(
    *,
    evaluation: EvaluationResult,
    dataset: EvaluationDataset,
    evidence: RrfEvidenceEnvelope,
    locked_policy: LockedPolicy,
    evaluation_context: EvaluationContextIdentity,
) -> GateResult:
    """Gate v2 自行验证 v1 evidence/calibration，并从 per-case facts 重算 summary。"""
    hard_failures: list[str] = []
    try:
        validated_dataset = EvaluationDataset.model_validate(dataset.model_dump(mode="json"))
        validated_experiment = validate_experiment_evidence(validated_dataset, evidence)
    except (NoAnswerProtocolError, ValueError, TypeError):
        return GateResult(
            gate_ref=GATE_V2_REF,
            outcome=GateOutcome.NOT_EVALUATED_BLOCKED,
            reason_codes=("strict_evidence_validation_failure",),
        )
    calibration_cases = [
        case
        for case in validated_dataset.cases
        if case.ground_truth.answerability is not None
        and case.ground_truth.answerability.split == AnswerabilitySplit.CALIBRATION
    ]
    try:
        expected_calibration = calibrate(
            calibration_cases=calibration_cases,
            calibration_signals=signals_for_split(validated_experiment, AnswerabilitySplit.CALIBRATION),
            validated_experiment=validated_experiment,
        )
    except (NoAnswerProtocolError, ValueError, TypeError):
        hard_failures.append("calibration_derivation_failure")
    else:
        if locked_policy != expected_calibration.locked_policy:
            hard_failures.append("calibration_policy_lock_mismatch")
    try:
        _verify_evaluation_context_against_lock(locked_policy, evaluation_context)
    except NoAnswerProtocolError:
        hard_failures.append("evaluation_context_lock_mismatch")
    proof_context = build_evaluation_context(validated_experiment)
    if proof_context != evaluation_context:
        hard_failures.append("evaluation_context_proof_mismatch")

    hard_failures.extend(_gate_per_case_checks(evaluation, validated_experiment, locked_policy))
    if hard_failures:
        return GateResult(
            gate_ref=GATE_V2_REF,
            outcome=GateOutcome.NOT_EVALUATED_BLOCKED,
            reason_codes=tuple(sorted(set(hard_failures))),
        )
    return _quality_gate(evaluation.metrics, GATE_V2_REF)


def acceptance_gate_v3(
    *,
    evaluation: EvaluationResult,
    dataset: EvaluationDataset,
    evidence: RrfEvidenceEnvelopeV2,
    locked_policy: LockedPolicyV2,
    evaluation_context: EvaluationContextIdentityV2,
) -> GateResult:
    """Gate v3 消费 synthetic substrate v2；质量规则与 Gate v2 完全一致。

    Authority flow 与 Gate v2 相同：strict Dataset/evidence 重解析 → gate-owned
    deterministic calibration → supplied lock exact compare → context/per-case/
    summary/privacy checks → quality Gate。
    """
    hard_failures: list[str] = []
    try:
        validated_dataset = EvaluationDataset.model_validate(dataset.model_dump(mode="json"))
        validated_experiment = validate_experiment_evidence_v2(validated_dataset, evidence)
    except (NoAnswerProtocolError, ValueError, TypeError):
        return GateResult(
            gate_ref=GATE_V3_REF,
            outcome=GateOutcome.NOT_EVALUATED_BLOCKED,
            reason_codes=("strict_evidence_validation_failure",),
        )
    if validated_experiment.substrate_ref != WP4_RRF_SUBSTRATE_REF:
        hard_failures.append("substrate_ref_mismatch")
    calibration_cases = [
        case
        for case in validated_dataset.cases
        if case.ground_truth.answerability is not None
        and case.ground_truth.answerability.split == AnswerabilitySplit.CALIBRATION
    ]
    try:
        expected_calibration = calibrate_v2(
            calibration_cases=calibration_cases,
            calibration_signals=signals_for_split(validated_experiment, AnswerabilitySplit.CALIBRATION),
            validated_experiment=validated_experiment,
        )
    except (NoAnswerProtocolError, ValueError, TypeError):
        hard_failures.append("calibration_derivation_failure")
    else:
        if locked_policy != expected_calibration.locked_policy:
            hard_failures.append("calibration_policy_lock_mismatch")
    try:
        _verify_evaluation_context_against_lock(locked_policy, evaluation_context)
    except NoAnswerProtocolError:
        hard_failures.append("evaluation_context_lock_mismatch")
    proof_context = build_evaluation_context_v2(validated_experiment)
    if proof_context != evaluation_context:
        hard_failures.append("evaluation_context_proof_mismatch")

    hard_failures.extend(_gate_per_case_checks(evaluation, validated_experiment, locked_policy))
    if hard_failures:
        return GateResult(
            gate_ref=GATE_V3_REF,
            outcome=GateOutcome.NOT_EVALUATED_BLOCKED,
            reason_codes=tuple(sorted(set(hard_failures))),
        )
    return _quality_gate(evaluation.metrics, GATE_V3_REF)


def build_no_answer_report_v3(
    *,
    dataset: EvaluationDataset,
    evidence: RrfEvidenceEnvelopeV2,
    validated: ValidatedExperimentInvariantsV2,
    calibration: CalibrationResultV2,
    evaluation_context: EvaluationContextIdentityV2,
    evaluation: EvaluationResult,
    gate: GateResult,
) -> dict[str, object]:
    """构造 privacy-safe 的 no-answer-threshold-report.v3。

    显式记录 Dataset identity、substrate ref、corpus、两个 synthetic cache
    identities、RRF ref/k/budgets、evidence schema/digest、Gate ref、LockedPolicy
    identity、calibration/evaluation metrics、final outcome 与 technical failure facts。
    Phase B 不生成真实 28-case evidence，capability 保持 DETERMINISTIC_TEST_ONLY。
    """
    return {
        "report_schema_version": REPORT_SCHEMA_V3,
        "capability": "DETERMINISTIC_TEST_ONLY",
        "real_retrieval": False,
        "dataset": {
            "dataset_id": validated.dataset_id,
            "dataset_version": validated.dataset_version,
            "dataset_digest": validated.dataset_digest,
            "corpus_ref": validated.corpus_ref,
            "coverage": validated.coverage,
        },
        "substrate": {
            "substrate_ref": evidence.substrate_ref,
            "source_manifest_digest": evidence.source_manifest_digest,
            "chunk_manifest_digest": evidence.chunk_manifest_digest,
            "dense_cache_identity": evidence.dense_cache_identity,
            "bm25_cache_identity": evidence.bm25_cache_identity,
            "dense_channel_ref": evidence.dense_channel_ref,
            "bm25_channel_ref": evidence.bm25_channel_ref,
            "algorithm_ref": evidence.algorithm_ref,
            "rrf_k": evidence.rrf_k,
            "per_channel_candidate_limit": evidence.per_channel_candidate_limit,
            "pre_fusion_union_limit": evidence.pre_fusion_union_limit,
            "final_candidate_limit": evidence.final_candidate_limit,
            "ce_used": evidence.ce_used,
            "new_model_used": evidence.new_model_used,
            "runtime_read_only": evidence.runtime_read_only,
        },
        "rrf_evidence": {
            "schema_version": evidence.schema_version,
            "digest": validated.evidence_digest,
        },
        "gate_ref": gate.gate_ref,
        "locked_policy": calibration.locked_policy.model_dump(mode="json"),
        "evaluation_context": evaluation_context.model_dump(mode="json"),
        "calibration": {
            "policy_config": calibration.locked_policy.policy_config.model_dump(mode="json"),
            "policy_ref": calibration.locked_policy.policy_ref,
            "lock_digest": calibration.locked_policy.lock_digest,
            "candidate_grid_digest": calibration.candidate_grid.digest,
            "feasible_policy_count": calibration.feasible_policy_count,
            "calibration_metrics": calibration.calibration_metrics.model_dump(mode="json"),
        },
        "evaluation": evaluation.model_dump(mode="json"),
        "diagnostic": {"CONFLICT": validated.coverage.get("DIAGNOSTIC", {}).get("CONFLICT", 0)},
        "technical_failure_facts": {
            "technical_failure_count": evaluation.metrics.technical_failure_count,
        },
        "final_outcome": gate.outcome.value,
        "gate": gate.model_dump(mode="json"),
    }


def privacy_safe_serialization(value: object, forbidden_plaintexts: Sequence[str]) -> bool:
    """递归序列化后检查禁止字段与调用方提供的敏感明文。"""
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    forbidden_keys = (
        '"query"',
        '"query_plaintext"',
        '"chunk"',
        '"chunk_text"',
        '"document"',
        '"document_text"',
        '"local_path"',
        '"prompt"',
        '"model_output"',
        '"raw_exception"',
        '"credential"',
    )
    return not any(item and item in text for item in (*forbidden_keys, *forbidden_plaintexts))


__all__ = [
    "CalibrationResult",
    "CalibrationResultV2",
    "CandidateGrid",
    "EvaluationCaseFact",
    "EvaluationContextIdentity",
    "EvaluationContextIdentityV2",
    "EvaluationResult",
    "ExpectedEvaluationCaseFact",
    "FrozenRrfConfig",
    "FrozenRrfConfigV2",
    "GATE_V2_REF",
    "GATE_V3_REF",
    "GateInvariants",
    "GateOutcome",
    "GateResult",
    "LockedPolicy",
    "LockedPolicyV2",
    "REPORT_SCHEMA_V2",
    "REPORT_SCHEMA_V3",
    "SubtypeResult",
    "ValidatedExperimentInvariants",
    "ValidatedExperimentInvariantsV2",
    "acceptance_gate",
    "acceptance_gate_v1",
    "acceptance_gate_v2",
    "acceptance_gate_v3",
    "build_candidate_grid",
    "build_evaluation_context",
    "build_evaluation_context_v2",
    "build_no_answer_report_v3",
    "calibrate",
    "calibrate_v2",
    "canonical_digest",
    "derive_evaluation_summary",
    "evaluate",
    "privacy_safe_serialization",
    "signals_for_split",
    "validate_experiment_evidence",
    "validate_experiment_evidence_v2",
    "validate_no_answer_dataset",
]
