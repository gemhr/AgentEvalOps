"""Evaluator 输入与依赖上下文。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.evaluation.catalog import AssertionSpec, EvaluatorSpec
from app.core.evaluation.immutable import FrozenDict, FrozenJsonValue, freeze_json
from app.core.evaluation.references import ArtifactRef, CaseVersionRef, EvidenceRef, freeze_metadata

if TYPE_CHECKING:
    from app.core.evaluation.ports import JudgeModelPort


@dataclass(frozen=True, slots=True)
class EvaluationInput:
    """不依赖 Trace 或具体 ExecutionOutcome schema 的统一 evaluator 输入。"""

    case_ref: CaseVersionRef
    expected_output: FrozenJsonValue | None
    assertion_specs: tuple[AssertionSpec, ...]
    actual_artifact: ArtifactRef | None = None
    execution_outcome_ref: EvidenceRef | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        if self.expected_output is not None:
            object.__setattr__(self, "expected_output", freeze_json(self.expected_output))
        object.__setattr__(self, "assertion_specs", tuple(self.assertion_specs))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class EvaluatorContext:
    """EvaluatorSpec snapshot 及可选外部 Judge port。"""

    evaluator_spec: EvaluatorSpec
    judge_model: JudgeModelPort | None = field(default=None, compare=False, repr=False)
    metadata: FrozenDict = field(default_factory=FrozenDict, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))
