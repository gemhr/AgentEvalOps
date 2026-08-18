"""Baseline vs Candidate EvaluationRun 的最小 Regression Comparison Domain。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.core.evaluation.immutable import require_text
from app.core.evaluation.references import VersionRef


class RegressionClassification(StrEnum):
    """跨 Run 对齐后单个 (case, evaluator) 槽位的最小比较分类。"""

    REGRESSION = "REGRESSION"
    IMPROVEMENT = "IMPROVEMENT"
    UNCHANGED = "UNCHANGED"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class ComparisonReason(StrEnum):
    """稳定、可测试的短 reason 标签，不承载动态文本。"""

    VERDICT_REGRESSED = "verdict_regressed"
    VERDICT_IMPROVED = "verdict_improved"
    VERDICT_UNCHANGED = "verdict_unchanged"
    BASELINE_MISSING = "baseline_missing"
    CANDIDATE_MISSING = "candidate_missing"
    INCONCLUSIVE_RESULT = "inconclusive_result"
    EVALUATOR_CONFIG_MISMATCH = "evaluator_config_mismatch"


class RegressionComparisonError(RuntimeError):
    """Comparison 边界的 typed base error。"""


class RunsNotComparable(RegressionComparisonError):
    """Run 不满足最小 eligibility / comparability 条件。"""


class ResultAlignmentAmbiguous(RegressionComparisonError):
    """同一 Run 内一个跨 Run 对齐键对应多个 Result，fail closed。"""


@dataclass(frozen=True, slots=True)
class AlignedResultComparison:
    """一个 (case, evaluator) 槽位对齐后的比较结果。

    只引用两侧 Result identity 与最小 score evidence，不复制完整 Result payload。
    """

    case_id: str
    case_version: str
    evaluator_id: str
    evaluator_version: str
    baseline_result_id: str | None = None
    candidate_result_id: str | None = None
    classification: RegressionClassification = RegressionClassification.NOT_COMPARABLE
    reason: ComparisonReason = ComparisonReason.BASELINE_MISSING
    baseline_score: float | None = None
    candidate_score: float | None = None
    score_delta: float | None = None
    score_regressed: bool | None = None

    def __post_init__(self) -> None:
        for field_name in ("case_id", "case_version", "evaluator_id", "evaluator_version"):
            require_text(getattr(self, field_name), field_name)
        if not isinstance(self.classification, RegressionClassification):
            raise ValueError("unknown classification")
        if not isinstance(self.reason, ComparisonReason):
            raise ValueError("unknown reason")


@dataclass(frozen=True, slots=True)
class RunComparisonProvenance:
    """一侧 Run 用于 comparison provenance 的最小身份快照。

    dataset/suite/target version 差异是 Regression 比较的合法输入，必须保留为 provenance。
    """

    dataset_id: str
    dataset_version: str
    suite_id: str
    suite_version: str
    execution_target_id: str
    execution_target_kind: str
    target_version_ref: VersionRef | None = None

    def __post_init__(self) -> None:
        for field_name in ("dataset_id", "dataset_version", "suite_id", "suite_version", "execution_target_id", "execution_target_kind"):
            require_text(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class EvaluationRunComparison:
    """两个 COMPLETED EvaluationRun 的 immutable comparison 结果。"""

    project_id: UUID
    baseline_run_id: UUID
    candidate_run_id: UUID
    baseline_provenance: RunComparisonProvenance
    candidate_provenance: RunComparisonProvenance
    comparisons: tuple[AlignedResultComparison, ...] = ()
