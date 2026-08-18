"""Regression Report 与 Release Decision 的最小 immutable Domain。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.core.evaluation.comparison import AlignedResultComparison, RunComparisonProvenance
from app.core.evaluation.references import CaseVersionRef


class ReleaseDecision(StrEnum):
    """最小 Release 决策状态：只有 PASS / FAIL。"""

    PASS = "PASS"
    FAIL = "FAIL"


class RegressionReportContractError(RuntimeError):
    """Criticality 输入违反 Report contract（重复/缺失/版本不匹配）。"""


@dataclass(frozen=True, slots=True)
class RegressionReport:
    """一次 Baseline vs Candidate 比较的 Report 与 Release Decision。

    只引用 WP1 comparison 的 immutable DTO 与 caller 提供的 criticality input，
    不复制 EvaluationResult payload / artifact / metadata。
    """

    project_id: UUID
    baseline_run_id: UUID
    candidate_run_id: UUID
    baseline_provenance: RunComparisonProvenance
    candidate_provenance: RunComparisonProvenance
    critical_case_refs: tuple[CaseVersionRef, ...]
    comparisons: tuple[AlignedResultComparison, ...]
    total_count: int
    regression_count: int
    improvement_count: int
    unchanged_count: int
    not_comparable_count: int
    regressions: tuple[AlignedResultComparison, ...]
    critical_regressions: tuple[AlignedResultComparison, ...]
    critical_not_comparable: tuple[AlignedResultComparison, ...]
    release_decision: ReleaseDecision

    def __post_init__(self) -> None:
        if not isinstance(self.release_decision, ReleaseDecision):
            raise ValueError("unknown release decision")
        counts = (
            self.regression_count,
            self.improvement_count,
            self.unchanged_count,
            self.not_comparable_count,
        )
        if any(count < 0 for count in counts):
            raise ValueError("classification counts must be non-negative")
        if sum(counts) != self.total_count:
            raise ValueError("classification counts must sum to total_count")
        if self.total_count != len(self.comparisons):
            raise ValueError("total_count must match comparisons length")
        if len(self.regressions) != self.regression_count:
            raise ValueError("regressions length must match regression_count")
