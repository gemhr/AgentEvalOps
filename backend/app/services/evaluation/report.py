"""WP1 EvaluationRunComparison + caller-supplied criticality → RegressionReport / ReleaseDecision。"""

# ruff: noqa: D105, D415

from __future__ import annotations

from app.core.evaluation.comparison import EvaluationRunComparison, RegressionClassification
from app.core.evaluation.references import CaseVersionRef
from app.core.evaluation.report import RegressionReport, RegressionReportContractError, ReleaseDecision


class RegressionReportService:
    """纯同步派生：只消费已完成的 EvaluationRunComparison，不读取 persistence / DB。

    - 只使用 WP1 的 per-slot classification 作为 truth，不重算 verdict / score。
    - criticality 完全由 caller 显式传入（CALLER_SUPPLIED），不从 tags/metadata/name 推断。
    - 子集与计数保持 WP1 comparison 的 deterministic 顺序。
    """

    def build_report(
        self,
        comparison: EvaluationRunComparison,
        critical_case_refs: tuple[CaseVersionRef, ...],
    ) -> RegressionReport:
        """聚合 comparison 并应用冻结的 Release policy，返回 immutable Report。"""
        canonical = self._validate_critical_refs(comparison, critical_case_refs)
        critical = frozenset((ref.case_id, ref.version) for ref in canonical)
        comparisons = comparison.comparisons
        regressions = tuple(
            item for item in comparisons if item.classification is RegressionClassification.REGRESSION
        )
        critical_regressions = tuple(
            item
            for item in comparisons
            if item.classification is RegressionClassification.REGRESSION
            and (item.case_id, item.case_version) in critical
        )
        critical_not_comparable = tuple(
            item
            for item in comparisons
            if item.classification is RegressionClassification.NOT_COMPARABLE
            and (item.case_id, item.case_version) in critical
        )
        total_count = len(comparisons)
        regression_count = len(regressions)
        improvement_count = sum(
            item.classification is RegressionClassification.IMPROVEMENT for item in comparisons
        )
        unchanged_count = sum(
            item.classification is RegressionClassification.UNCHANGED for item in comparisons
        )
        not_comparable_count = sum(
            item.classification is RegressionClassification.NOT_COMPARABLE for item in comparisons
        )
        decision = (
            ReleaseDecision.FAIL
            if critical_regressions or critical_not_comparable
            else ReleaseDecision.PASS
        )
        return RegressionReport(
            project_id=comparison.project_id,
            baseline_run_id=comparison.baseline_run_id,
            candidate_run_id=comparison.candidate_run_id,
            baseline_provenance=comparison.baseline_provenance,
            candidate_provenance=comparison.candidate_provenance,
            critical_case_refs=canonical,
            comparisons=comparisons,
            total_count=total_count,
            regression_count=regression_count,
            improvement_count=improvement_count,
            unchanged_count=unchanged_count,
            not_comparable_count=not_comparable_count,
            regressions=regressions,
            critical_regressions=critical_regressions,
            critical_not_comparable=critical_not_comparable,
            release_decision=decision,
        )

    @staticmethod
    def _validate_critical_refs(
        comparison: EvaluationRunComparison,
        critical_case_refs: tuple[CaseVersionRef, ...],
    ) -> tuple[CaseVersionRef, ...]:
        """验证 critical refs 合法后返回 canonical（按 case_id/version 排序）tuple。"""
        refs = tuple(critical_case_refs)
        identities = [(ref.case_id, ref.version) for ref in refs]
        if len(identities) != len(set(identities)):
            raise RegressionReportContractError("duplicate critical case ref is not allowed")
        canonical = tuple(sorted(refs))
        if not comparison.comparisons:
            if canonical:
                raise RegressionReportContractError(
                    "critical case refs are outside the empty comparison universe"
                )
            return canonical
        universe = {(item.case_id, item.case_version) for item in comparison.comparisons}
        for ref in canonical:
            if (ref.case_id, ref.version) not in universe:
                raise RegressionReportContractError(
                    f"critical case ref ({ref.case_id}, {ref.version}) is outside the comparison universe"
                )
        return canonical
