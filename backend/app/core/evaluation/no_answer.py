"""WP4 evaluation-only No-Answer policy、sidecar 与 confusion metrics。"""

# ruff: noqa: D101, D102, D415

from __future__ import annotations

import hashlib
import math
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

NO_ANSWER_DECISION_SCHEMA_VERSION = "no-answer-decision.v1"
NO_ANSWER_SIGNAL_KIND = "rrf-top1-margin.v1"
RRF_BASELINE_REF = "rrf.v1"
RRF_EVIDENCE_SCHEMA_VERSION = "no-answer-rrf-evidence.v1"
WP2_DENSE_CACHE_IDENTITY = "b63c0bbd115150da766b84a80331b332010812bbb757a6782df9ae2224ca8f46"
WP2_BM25_CACHE_IDENTITY = "594c9c95a3b6f29bcd2fcd738e23ee345427d06b49a70ac3149544a1a4f8f84b"
CURRENT_CHANNEL_REF = "current-dense-led-ranked.v1"
BM25_CHANNEL_REF = "bm25-lucene-idf.v1"
RRF_K = 60
PER_CHANNEL_CANDIDATE_LIMIT = 8
PRE_FUSION_UNION_LIMIT = 16
FINAL_CANDIDATE_LIMIT = 8
CORPUS_REF = "rag-evaluation-corpus.v1"


class RetrievalStatus(StrEnum):
    """WP4 消费的冻结 retrieval terminal facts。"""

    SUCCEEDED = "SUCCEEDED"
    EMPTY = "EMPTY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class EmptyOrigin(StrEnum):
    """AgentEvalOps 从原始 counts 派生的 EMPTY diagnostic。"""

    ZERO_CANDIDATE = "ZERO_CANDIDATE"
    FILTERED_EMPTY = "FILTERED_EMPTY"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class NoAnswerDecisionValue(StrEnum):
    ANSWER = "ANSWER"
    ABSTAIN = "ABSTAIN"


class NoAnswerReasonCode(StrEnum):
    NO_CANDIDATE = "NO_CANDIDATE"
    TOP1_BELOW_THRESHOLD = "TOP1_BELOW_THRESHOLD"
    MARGIN_BELOW_THRESHOLD = "MARGIN_BELOW_THRESHOLD"
    EVIDENCE_THRESHOLD_MET = "EVIDENCE_THRESHOLD_MET"


class NoAnswerProtocolError(ValueError):
    """必要 evidence 缺失或组合不一致。"""


def _safe_identifier(value: str, field_name: str) -> str:
    if not value or "\\" in value or value.startswith(("/", "file:")):
        raise ValueError(f"{field_name} must be a non-path identifier")
    return value


def _sha256(value: str, field_name: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field_name} must be lowercase SHA-256")
    return value


class RrfRankedCandidateEvidence(BaseModel):
    """一个 frozen RRF ranked candidate 的 privacy-safe provenance。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: StrictStr
    chunk_id: StrictStr
    rank: int = Field(ge=1, le=FINAL_CANDIDATE_LIMIT)
    rrf_score: float = Field(ge=0)
    source_channels: tuple[
        Literal["current-dense-led-ranked.v1", "bm25-lucene-idf.v1"], ...
    ] = Field(min_length=1, max_length=2)
    contributing_channel_count: int = Field(ge=1, le=2)

    @field_validator("document_id", "chunk_id")
    @classmethod
    def _identity(cls, value: str, info: object) -> str:
        return _safe_identifier(value, getattr(info, "field_name", "candidate_identity"))

    @field_validator("rrf_score")
    @classmethod
    def _score(cls, value: float) -> float:
        return _finite(value, "rrf_score")

    @model_validator(mode="after")
    def _channels(self) -> "RrfRankedCandidateEvidence":
        if len(self.source_channels) != len(set(self.source_channels)):
            raise ValueError("candidate source_channels must be unique")
        if self.contributing_channel_count != len(self.source_channels):
            raise ValueError("contributing_channel_count must equal source channel count")
        return self


class RrfCaseEvidence(BaseModel):
    """一个 WP4 case 的 strict RRF evidence。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: StrictStr
    query_sha256: StrictStr
    retrieval_artifact_id: StrictStr
    retrieval_status: RetrievalStatus
    retrieved_candidate_count: int = Field(ge=0, le=PRE_FUSION_UNION_LIMIT)
    ranked_candidate_count: int = Field(ge=0, le=FINAL_CANDIDATE_LIMIT)
    ranked_candidates: tuple[RrfRankedCandidateEvidence, ...] = Field(max_length=FINAL_CANDIDATE_LIMIT)

    @field_validator("case_id", "retrieval_artifact_id")
    @classmethod
    def _identifiers(cls, value: str, info: object) -> str:
        return _safe_identifier(value, getattr(info, "field_name", "evidence_identity"))

    @field_validator("query_sha256")
    @classmethod
    def _query_digest(cls, value: str) -> str:
        return _sha256(value, "query_sha256")

    @model_validator(mode="after")
    def _case_invariants(self) -> "RrfCaseEvidence":
        if self.ranked_candidate_count != len(self.ranked_candidates):
            raise ValueError("ranked_candidate_count must equal ranked candidate item count")
        if self.ranked_candidate_count > self.retrieved_candidate_count:
            raise ValueError("ranked candidate count must not exceed retrieved candidate count")
        expected_ranks = tuple(range(1, len(self.ranked_candidates) + 1))
        if tuple(item.rank for item in self.ranked_candidates) != expected_ranks:
            raise ValueError("candidate ranks must be exact 1..N")
        identities = [(item.document_id, item.chunk_id) for item in self.ranked_candidates]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate candidate identity is not allowed")
        scores = tuple(item.rrf_score for item in self.ranked_candidates)
        if any(left < right for left, right in zip(scores, scores[1:], strict=False)):
            raise ValueError("RRF candidates must be ordered by descending score")
        technical = {RetrievalStatus.FAILED, RetrievalStatus.TIMED_OUT, RetrievalStatus.CANCELLED}
        if self.retrieval_status in technical:
            if self.retrieved_candidate_count or self.ranked_candidate_count:
                raise ValueError("technical retrieval status must carry zero candidate counts")
        elif self.retrieval_status == RetrievalStatus.EMPTY:
            if self.ranked_candidate_count:
                raise ValueError("EMPTY retrieval status must not carry ranked candidates")
        elif self.ranked_candidate_count < 1:
            raise ValueError("SUCCEEDED/DEGRADED requires ranked candidates")
        return self


class RrfEvidenceEnvelope(BaseModel):
    """WP4 runner 唯一接受的 strict RRF evidence envelope。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["no-answer-rrf-evidence.v1"]
    dataset_id: StrictStr
    dataset_version: StrictStr
    dataset_digest: StrictStr
    corpus_ref: Literal["rag-evaluation-corpus.v1"]
    dense_cache_identity: Literal[
        "b63c0bbd115150da766b84a80331b332010812bbb757a6782df9ae2224ca8f46"
    ]
    bm25_cache_identity: Literal[
        "594c9c95a3b6f29bcd2fcd738e23ee345427d06b49a70ac3149544a1a4f8f84b"
    ]
    algorithm_ref: Literal["rrf.v1"]
    rrf_k: Literal[60]
    dense_channel_ref: Literal["current-dense-led-ranked.v1"]
    bm25_channel_ref: Literal["bm25-lucene-idf.v1"]
    per_channel_candidate_limit: Literal[8]
    pre_fusion_union_limit: Literal[16]
    final_candidate_limit: Literal[8]
    ce_used: Literal[False]
    new_model_used: Literal[False]
    runtime_read_only: Literal[True]
    cases: tuple[RrfCaseEvidence, ...] = Field(min_length=1)

    @field_validator("dataset_id", "dataset_version")
    @classmethod
    def _dataset_identity(cls, value: str, info: object) -> str:
        return _safe_identifier(value, getattr(info, "field_name", "dataset_identity"))

    @field_validator("dataset_digest")
    @classmethod
    def _dataset_digest(cls, value: str) -> str:
        return _sha256(value, "dataset_digest")

    @model_validator(mode="after")
    def _unique_evidence(self) -> "RrfEvidenceEnvelope":
        case_ids = [case.case_id for case in self.cases]
        artifact_ids = [case.retrieval_artifact_id for case in self.cases]
        query_digests = [case.query_sha256 for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("duplicate evidence case identity is not allowed")
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("duplicate retrieval artifact identity is not allowed")
        if len(query_digests) != len(set(query_digests)):
            raise ValueError("duplicate query digest is not allowed")
        return self


def _finite(value: float, field_name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


class NoAnswerPolicyConfig(BaseModel):
    """阈值绑定 frozen RRF config；RRF score 不是置信概率。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_top1_score: float
    min_top1_top2_margin: float

    @field_validator("min_top1_score", "min_top1_top2_margin")
    @classmethod
    def _values(cls, value: float, info: object) -> float:
        return _finite(value, getattr(info, "field_name", "threshold"))


class NoAnswerSignal(BaseModel):
    """只含 RRF evidence facts 的 privacy-safe policy 输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: StrictStr
    query_sha256: StrictStr
    retrieval_artifact_id: StrictStr
    retrieval_status: RetrievalStatus
    retrieved_candidate_count: int | None = Field(default=None, ge=0, le=PRE_FUSION_UNION_LIMIT)
    ranked_candidate_count: int | None = Field(default=None, ge=0, le=8)
    candidate_count: int = Field(ge=0, le=8)
    rrf_scores: tuple[float, ...]

    @field_validator("query_sha256")
    @classmethod
    def _query_digest(cls, value: str) -> str:
        return _sha256(value, "query_sha256")

    @field_validator("rrf_scores")
    @classmethod
    def _scores(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        checked = tuple(_finite(value, "rrf_score") for value in values)
        if any(value < 0 for value in checked):
            raise ValueError("rrf_scores must be non-negative rank-fusion scores")
        if any(left < right for left, right in zip(checked, checked[1:], strict=False)):
            raise ValueError("rrf_scores must be ordered descending")
        return checked

    @model_validator(mode="after")
    def _invariants(self) -> "NoAnswerSignal":
        if self.retrieved_candidate_count is None or self.ranked_candidate_count is None:
            raise ValueError("retrieved/ranked candidate counts are required")
        if self.ranked_candidate_count > self.retrieved_candidate_count:
            raise ValueError("ranked count must not exceed retrieved count")
        if self.candidate_count != len(self.rrf_scores):
            raise ValueError("candidate_count must equal RRF score count")
        technical = {
            RetrievalStatus.FAILED,
            RetrievalStatus.TIMED_OUT,
            RetrievalStatus.CANCELLED,
        }
        if self.retrieval_status in technical:
            if self.candidate_count != 0:
                raise ValueError("technical failure must not carry policy candidates")
            return self
        if self.retrieval_status == RetrievalStatus.EMPTY:
            if self.candidate_count != 0:
                raise ValueError("EMPTY must not carry RRF candidates")
        elif self.candidate_count < 1:
            raise ValueError("SUCCEEDED/DEGRADED requires at least one candidate")
        return self

    @property
    def top1_score(self) -> float | None:
        return self.rrf_scores[0] if self.rrf_scores else None

    @property
    def top1_top2_margin(self) -> float | None:
        return self.rrf_scores[0] - self.rrf_scores[1] if len(self.rrf_scores) >= 2 else None


def derive_empty_origin(signal: NoAnswerSignal) -> EmptyOrigin:
    """仅从 status 与原始 retrieved/ranked counts 派生 EMPTY origin。"""
    if signal.retrieval_status != RetrievalStatus.EMPTY:
        return EmptyOrigin.NOT_APPLICABLE
    if signal.retrieved_candidate_count == 0 and signal.ranked_candidate_count == 0:
        return EmptyOrigin.ZERO_CANDIDATE
    return EmptyOrigin.FILTERED_EMPTY


class NoAnswerSignalValues(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rrf_top1_score: float | None
    rrf_top1_top2_margin: float | None

    @field_validator("rrf_top1_score", "rrf_top1_top2_margin")
    @classmethod
    def _optional_finite(cls, value: float | None, info: object) -> float | None:
        return None if value is None else _finite(value, getattr(info, "field_name", "signal"))


class NoAnswerDecisionSidecar(BaseModel):
    """Strict、privacy-safe 的 evaluation-only derived decision。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["no-answer-decision.v1"] = NO_ANSWER_DECISION_SCHEMA_VERSION
    decision_id: StrictStr
    policy_ref: StrictStr
    dataset_id: StrictStr
    dataset_version: StrictStr
    case_id: StrictStr
    split: Literal["CALIBRATION", "EVALUATION"]
    query_sha256: StrictStr
    retrieval_artifact_id: StrictStr
    retrieval_baseline_ref: Literal["rrf.v1"] = RRF_BASELINE_REF
    retrieval_status: Literal["SUCCEEDED", "EMPTY", "DEGRADED"]
    empty_origin: EmptyOrigin
    candidate_count: int = Field(ge=0)
    signal_kind: Literal["rrf-top1-margin.v1"] = NO_ANSWER_SIGNAL_KIND
    signal_values: NoAnswerSignalValues
    policy_config: NoAnswerPolicyConfig
    decision: NoAnswerDecisionValue
    reason_code: NoAnswerReasonCode

    @field_validator("query_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, "query_sha256")

    @field_validator(
        "decision_id", "policy_ref", "dataset_id", "dataset_version", "case_id", "retrieval_artifact_id"
    )
    @classmethod
    def _safe_identifier(cls, value: str) -> str:
        return _safe_identifier(value, "sidecar_identifier")

    @model_validator(mode="after")
    def _decision_semantics(self) -> "NoAnswerDecisionSidecar":
        top1 = self.signal_values.rrf_top1_score
        margin = self.signal_values.rrf_top1_top2_margin
        if self.retrieval_status == RetrievalStatus.EMPTY:
            if (
                self.candidate_count != 0
                or top1 is not None
                or margin is not None
                or self.empty_origin == EmptyOrigin.NOT_APPLICABLE
                or self.decision != NoAnswerDecisionValue.ABSTAIN
                or self.reason_code != NoAnswerReasonCode.NO_CANDIDATE
            ):
                raise ValueError("EMPTY sidecar combination is invalid")
            return self
        if self.empty_origin != EmptyOrigin.NOT_APPLICABLE or self.candidate_count < 1 or top1 is None:
            raise ValueError("non-empty sidecar combination is invalid")
        if (self.candidate_count == 1) != (margin is None):
            raise ValueError("margin presence must match candidate_count")
        if top1 < self.policy_config.min_top1_score:
            expected = (NoAnswerDecisionValue.ABSTAIN, NoAnswerReasonCode.TOP1_BELOW_THRESHOLD)
        elif margin is not None and margin < self.policy_config.min_top1_top2_margin:
            expected = (NoAnswerDecisionValue.ABSTAIN, NoAnswerReasonCode.MARGIN_BELOW_THRESHOLD)
        else:
            expected = (NoAnswerDecisionValue.ANSWER, NoAnswerReasonCode.EVIDENCE_THRESHOLD_MET)
        if (self.decision, self.reason_code) != expected:
            raise ValueError("decision/reason does not match evidence thresholds")
        return self


class NoAnswerPolicy:
    """冻结 policy family；不读取 Ground Truth。"""

    @staticmethod
    def decide(
        *,
        signal: NoAnswerSignal,
        config: NoAnswerPolicyConfig,
        policy_ref: str,
        dataset_id: str,
        dataset_version: str,
        split: Literal["CALIBRATION", "EVALUATION"],
    ) -> NoAnswerDecisionSidecar | None:
        if signal.retrieval_status in {
            RetrievalStatus.FAILED,
            RetrievalStatus.TIMED_OUT,
            RetrievalStatus.CANCELLED,
        }:
            return None
        top1 = signal.top1_score
        margin = signal.top1_top2_margin
        if signal.retrieval_status == RetrievalStatus.EMPTY:
            decision = NoAnswerDecisionValue.ABSTAIN
            reason = NoAnswerReasonCode.NO_CANDIDATE
        elif top1 is None:
            raise NoAnswerProtocolError("eligible retrieval lacks top1 score")
        elif top1 < config.min_top1_score:
            decision = NoAnswerDecisionValue.ABSTAIN
            reason = NoAnswerReasonCode.TOP1_BELOW_THRESHOLD
        elif margin is not None and margin < config.min_top1_top2_margin:
            decision = NoAnswerDecisionValue.ABSTAIN
            reason = NoAnswerReasonCode.MARGIN_BELOW_THRESHOLD
        else:
            decision = NoAnswerDecisionValue.ANSWER
            reason = NoAnswerReasonCode.EVIDENCE_THRESHOLD_MET
        identity = "|".join((policy_ref, dataset_id, dataset_version, signal.case_id, signal.retrieval_artifact_id))
        return NoAnswerDecisionSidecar(
            decision_id=f"nad-{hashlib.sha256(identity.encode('utf-8')).hexdigest()}",
            policy_ref=policy_ref,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            case_id=signal.case_id,
            split=split,
            query_sha256=signal.query_sha256,
            retrieval_artifact_id=signal.retrieval_artifact_id,
            retrieval_status=signal.retrieval_status.value,
            empty_origin=derive_empty_origin(signal),
            candidate_count=signal.candidate_count,
            signal_values=NoAnswerSignalValues(
                rrf_top1_score=top1,
                rrf_top1_top2_margin=margin,
            ),
            policy_config=config,
            decision=decision,
            reason_code=reason,
        )


class ConfusionMetrics(BaseModel):
    """冻结 confusion cells、派生指标与独立 technical failure count。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    true_answer_count: int = Field(ge=0)
    false_abstain_count: int = Field(ge=0)
    false_answer_count: int = Field(ge=0)
    true_abstain_count: int = Field(ge=0)
    technical_failure_count: int = Field(ge=0)
    eligible_count: int = Field(gt=0)
    answerable_count: int = Field(gt=0)
    unanswerable_count: int = Field(gt=0)
    no_answer_accuracy: float
    false_answer_rate: float
    false_abstain_rate: float
    coverage: float

    @model_validator(mode="after")
    def _derived_values(self) -> "ConfusionMetrics":
        eligible = (
            self.true_answer_count
            + self.false_abstain_count
            + self.false_answer_count
            + self.true_abstain_count
        )
        answerable = self.true_answer_count + self.false_abstain_count
        unanswerable = self.false_answer_count + self.true_abstain_count
        if (eligible, answerable, unanswerable) != (
            self.eligible_count,
            self.answerable_count,
            self.unanswerable_count,
        ):
            raise ValueError("confusion counts are inconsistent")
        expected = (
            (self.true_answer_count + self.true_abstain_count) / eligible,
            self.false_answer_count / unanswerable,
            self.false_abstain_count / answerable,
            (self.true_answer_count + self.false_answer_count) / eligible,
        )
        actual = (self.no_answer_accuracy, self.false_answer_rate, self.false_abstain_rate, self.coverage)
        if any(
            not math.isclose(left, right, rel_tol=0.0, abs_tol=1e-15)
            for left, right in zip(actual, expected, strict=True)
        ):
            raise ValueError("confusion metrics are inconsistent")
        return self


def calculate_confusion(
    labeled_decisions: list[tuple[bool, NoAnswerDecisionValue]], *, technical_failure_count: int = 0
) -> ConfusionMetrics:
    """计算 confusion；必要 denominator 为零时 fail closed。"""
    ta = sum(answerable and decision == NoAnswerDecisionValue.ANSWER for answerable, decision in labeled_decisions)
    fab = sum(answerable and decision == NoAnswerDecisionValue.ABSTAIN for answerable, decision in labeled_decisions)
    fa = sum(not answerable and decision == NoAnswerDecisionValue.ANSWER for answerable, decision in labeled_decisions)
    tab = sum(not answerable and decision == NoAnswerDecisionValue.ABSTAIN for answerable, decision in labeled_decisions)
    eligible = ta + fab + fa + tab
    answerable_count = ta + fab
    unanswerable_count = fa + tab
    if eligible == 0 or answerable_count == 0 or unanswerable_count == 0:
        raise NoAnswerProtocolError("confusion denominator must be non-zero")
    return ConfusionMetrics(
        true_answer_count=ta,
        false_abstain_count=fab,
        false_answer_count=fa,
        true_abstain_count=tab,
        technical_failure_count=technical_failure_count,
        eligible_count=eligible,
        answerable_count=answerable_count,
        unanswerable_count=unanswerable_count,
        no_answer_accuracy=(ta + tab) / eligible,
        false_answer_rate=fa / unanswerable_count,
        false_abstain_rate=fab / answerable_count,
        coverage=(ta + fa) / eligible,
    )


__all__ = [
    "ConfusionMetrics",
    "EmptyOrigin",
    "NoAnswerDecisionSidecar",
    "NoAnswerDecisionValue",
    "NoAnswerPolicy",
    "NoAnswerPolicyConfig",
    "NoAnswerProtocolError",
    "NoAnswerReasonCode",
    "NoAnswerSignal",
    "RrfCaseEvidence",
    "RrfEvidenceEnvelope",
    "RrfRankedCandidateEvidence",
    "RetrievalStatus",
    "WP2_BM25_CACHE_IDENTITY",
    "WP2_DENSE_CACHE_IDENTITY",
    "calculate_confusion",
    "derive_empty_origin",
]
