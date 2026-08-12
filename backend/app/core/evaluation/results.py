"""Evaluator draft 与最终 EvaluationResult 实体。"""

# ruff: noqa: D105, D415

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.core.evaluation.immutable import FrozenDict, require_text
from app.core.evaluation.references import ArtifactRef, EvidenceRef, VersionRef, freeze_metadata


class EvaluationVerdict(StrEnum):
    """单个 evaluator 的稳定判定代数。"""

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"


class ProvenanceCompleteness(StrEnum):
    """结果能否完整归因到本阶段要求的 provenance。"""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


def _validate_score(score: float | None) -> None:
    if score is not None and not math.isfinite(score):
        raise ValueError("score must be finite")


@dataclass(frozen=True, slots=True)
class EvaluationResultDraft:
    """Evaluator 尚未绑定 run/case/suite provenance 的输出。"""

    evaluator_id: str
    evaluator_version: str
    config_ref: VersionRef
    verdict: EvaluationVerdict
    reason: str
    score: float | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    prompt_ref: VersionRef | None = None
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        require_text(self.evaluator_id, "evaluator_id")
        require_text(self.evaluator_version, "evaluator_version")
        require_text(self.reason, "reason")
        if not isinstance(self.verdict, EvaluationVerdict):
            raise ValueError("unknown verdict")
        _validate_score(self.score)
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """可脱离 Trace 独立存在、append-only 的评价事实。"""

    result_id: str
    run_id: str
    attempt_id: str
    dataset_id: str
    dataset_version: str
    case_id: str
    case_version: str
    suite_id: str
    suite_version: str
    evaluator_id: str
    evaluator_version: str
    config_ref: VersionRef
    execution_target_id: str
    execution_request_id: str
    verdict: EvaluationVerdict
    reason: str
    provenance_completeness: ProvenanceCompleteness
    created_at: datetime
    target_version_ref: VersionRef | None = None
    prompt_ref: VersionRef | None = None
    output_artifact_ref: ArtifactRef | None = None
    score: float | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        for field_name in (
            "result_id",
            "run_id",
            "attempt_id",
            "dataset_id",
            "dataset_version",
            "case_id",
            "case_version",
            "suite_id",
            "suite_version",
            "evaluator_id",
            "evaluator_version",
            "execution_target_id",
            "execution_request_id",
            "reason",
        ):
            require_text(getattr(self, field_name), field_name)
        if not isinstance(self.verdict, EvaluationVerdict):
            raise ValueError("unknown verdict")
        if not isinstance(self.provenance_completeness, ProvenanceCompleteness):
            raise ValueError("unknown provenance_completeness")
        _validate_score(self.score)
        if self.provenance_completeness is ProvenanceCompleteness.COMPLETE and self.target_version_ref is None:
            raise ValueError("COMPLETE provenance requires target_version_ref")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))
