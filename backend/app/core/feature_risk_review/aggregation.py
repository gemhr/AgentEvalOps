"""WP3 确定性 Risk Aggregation + Citation Report。

输入 ``FeatureRiskReviewWorkflowResult``，输出 typed ``FeatureRiskReviewReport`` 或
``FeatureRiskReviewAggregationFailure``。``FeatureRiskReviewAggregator`` 是
deterministic / near-pure 的 application service：不调用 LLM、不发网络请求、
不访问 filesystem、不读取 annotation loader。

三层边界（冻结）：
- SOURCE FACT：``HistoricalIssue`` / ``EvidenceRef`` / ``TestPlan`` / ``TestCase`` /
  retrieved evidence。
- AGENT INFERENCE：feature summary / change points / ``RiskFinding`` /
  coverage_assessment / potential_gaps / recommended_missing_cases / branch uncertainty。
- AGGREGATION POLICY OUTPUT：``RiskLevel`` / ``Priority`` / scenario ordering /
  report completeness / deterministic uncertainty summary。

Aggregator 只收集既有 ``EvidenceRef``，绝不创建新的 evidence_id / source_url /
source_id / issue_id。它不重新判定 citation correctness，也不读取 annotations /
expected_* / evaluation reference。
"""

# ruff: noqa: D415

from __future__ import annotations

import json

from pydantic import Field, StrictStr

from app.core.feature_risk_review.agents import (
    DocumentAnalysisResult,
    RiskRetrievalResult,
    TestReviewResult,
)
from app.core.feature_risk_review.contracts import (
    CoverageState,
    EvidenceRef,
    FeatureRiskReviewReport,
    Priority,
    ReportCompleteness,
    ReportUncertainty,
    RiskFinding,
    RiskLevel,
    _Contract,
)
from app.core.feature_risk_review.workflow import (
    BranchFailure,
    BranchStatus,
    FeatureRiskReviewWorkflowResult,
    WorkflowStatus,
)


class FeatureRiskReviewAggregationFailure(_Contract):
    """FAILED workflow 的最小 typed 失败；不是完成的业务 report。"""

    workflow_status: WorkflowStatus
    summary: StrictStr = Field(min_length=1)


class _CitationConflictError(Exception):
    """相同 evidence_id 对应不同 EvidenceRef 对象时的输入完整性错误。"""


_BRANCH_DISPLAY = {
    "risk_retrieval": "Risk retrieval",
    "test_review": "Test review",
}
_UNCERTAINTY_LABEL = {
    "document_analysis": "Document analysis",
    "risk_retrieval": "Risk retrieval",
    "test_review": "Test review",
    "workflow": "Workflow",
}


class FeatureRiskReviewAggregator:
    """确定性聚合器：把 workflow result 组装成 typed report。

    Risk policy 是 deterministic transparent heuristic，不是 calibrated production
    model / statistical score / ML model / GroundTruth-derived policy。
    """

    def aggregate(
        self,
        workflow_result: FeatureRiskReviewWorkflowResult,
    ) -> FeatureRiskReviewReport | FeatureRiskReviewAggregationFailure:
        """把 ``FeatureRiskReviewWorkflowResult`` 聚合为 report 或明确 failure。"""
        if workflow_result.document_analysis.status != BranchStatus.SUCCESS:
            return self._failure(
                workflow_result,
                "Document analysis unavailable; cannot produce a feature risk review report",
            )
        if workflow_result.workflow_status == WorkflowStatus.FAILED:
            return self._failure(
                workflow_result,
                "Both downstream branches failed; cannot produce a completed feature risk review report",
            )

        document = workflow_result.document_analysis.value
        assert document is not None

        risk: RiskRetrievalResult | None = None
        risk_failure: BranchFailure | None = None
        if workflow_result.risk_retrieval.status == BranchStatus.SUCCESS:
            risk = workflow_result.risk_retrieval.value
        else:
            risk_failure = workflow_result.risk_retrieval.failure

        test: TestReviewResult | None = None
        test_failure: BranchFailure | None = None
        if workflow_result.test_review.status == BranchStatus.SUCCESS:
            test = workflow_result.test_review.value
        else:
            test_failure = workflow_result.test_review.failure

        return self._build_report(workflow_result, document, risk, test, risk_failure, test_failure)

    def _build_report(
        self,
        workflow_result: FeatureRiskReviewWorkflowResult,
        document: DocumentAnalysisResult,
        risk: RiskRetrievalResult | None,
        test: TestReviewResult | None,
        risk_failure: BranchFailure | None,
        test_failure: BranchFailure | None,
    ) -> FeatureRiskReviewReport | FeatureRiskReviewAggregationFailure:
        risk_available = risk is not None
        test_available = test is not None
        completeness = _completeness(risk_available, test_available)

        findings = self._accepted_findings(risk) if risk is not None else []
        ordered_findings = self._order_findings(findings)
        historical_issues = list(risk.retrieved_historical_issues) if risk is not None else []
        retrieved_evidence = list(risk.retrieved_evidence) if risk is not None else []
        test_plans = list(test.test_plans) if test is not None else []
        test_cases = list(test.test_cases) if test is not None else []

        try:
            evidence_refs = self._collect_evidence(
                findings=ordered_findings,
                historical_issues=historical_issues,
                retrieved_evidence=retrieved_evidence,
                test_plans=test_plans,
                test_cases=test_cases,
            )
        except _CitationConflictError as exc:
            return self._failure(workflow_result, str(exc))

        potential_gaps = list(test.potential_gaps) if test is not None else []
        coverage_state = test.coverage_state if test is not None else None

        risk_level = self._risk_level(
            risk_available=risk_available,
            test_available=test_available,
            findings=findings,
            potential_gaps=potential_gaps,
            coverage_state=coverage_state,
            doc_uncertainty=document.uncertainty,
            risk_uncertainty=risk.uncertainty if risk is not None else None,
            test_uncertainty=test.uncertainty if test is not None else None,
        )
        priority = self._priority(
            completeness=completeness,
            risk_level=risk_level,
            coverage_state=coverage_state,
            potential_gaps=potential_gaps,
        )
        uncertainties = self._build_uncertainties(
            document=document,
            risk=risk,
            test=test,
            risk_failure=risk_failure,
            test_failure=test_failure,
            completeness=completeness,
        )

        return FeatureRiskReviewReport(
            case_id=workflow_result.case_id,
            feature_summary=document.feature_summary,
            change_points=list(document.change_points),
            high_risk_scenarios=ordered_findings,
            historical_issues=historical_issues,
            existing_coverage=test_plans,
            existing_test_cases=test_cases,
            coverage_state=coverage_state,
            coverage_assessment=test.coverage_assessment if test is not None else None,
            potential_gaps=potential_gaps,
            missing_cases=list(test.recommended_missing_cases) if test is not None else [],
            risk_level=risk_level,
            priority=priority,
            completeness=completeness,
            unavailable_sections=_unavailable_sections(completeness),
            uncertainties=uncertainties,
            evidence_refs=evidence_refs,
            uncertainty=_uncertainty_summary(uncertainties),
        )

    @staticmethod
    def _accepted_findings(risk: RiskRetrievalResult) -> list[RiskFinding]:
        """Accepted risk finding = WP2 已 identity-filtered 且至少一个 EvidenceRef。"""
        return [f for f in risk.agent_inferred_risk_findings if f.evidence_refs]

    @staticmethod
    def _order_findings(findings: list[RiskFinding]) -> list[RiskFinding]:
        """按完整 typed 值去重，并按冻结稳定排序。"""
        deduped = _dedup_by_value(findings)
        return sorted(deduped, key=_finding_sort_key)

    @staticmethod
    def _collect_evidence(
        *,
        findings: list[RiskFinding],
        historical_issues,
        retrieved_evidence,
        test_plans,
        test_cases,
    ) -> list[EvidenceRef]:
        seen: dict[str, EvidenceRef] = {}
        ordered: list[EvidenceRef] = []

        def _add(ref: EvidenceRef) -> None:
            existing = seen.get(ref.evidence_id)
            if existing is not None:
                if existing != ref:
                    raise _CitationConflictError(
                        f"citation identity conflict: evidence_id={ref.evidence_id!r} "
                        "maps to conflicting EvidenceRef objects"
                    )
                return
            seen[ref.evidence_id] = ref
            ordered.append(ref)

        for finding in findings:
            for ref in finding.evidence_refs:
                _add(ref)
        for issue in historical_issues:
            _add(issue.evidence_ref)
        for fragment in retrieved_evidence:
            _add(fragment.evidence_ref)
        for plan in test_plans:
            _add(plan.evidence_ref)
        for case in test_cases:
            _add(case.evidence_ref)
        return ordered

    @staticmethod
    def _has_source_backed_historical_issue_ref(findings: list[RiskFinding]) -> bool:
        return any(f.historical_issue_refs for f in findings)

    def _risk_level(
        self,
        *,
        risk_available: bool,
        test_available: bool,
        findings: list[RiskFinding],
        potential_gaps: list[str],
        coverage_state: CoverageState | None,
        doc_uncertainty: str | None,
        risk_uncertainty: str | None,
        test_uncertainty: str | None,
    ) -> RiskLevel | None:
        if not risk_available:
            return None
        if not test_available:
            if len(findings) >= 2 and self._has_source_backed_historical_issue_ref(findings):
                return RiskLevel.HIGH
            return RiskLevel.MEDIUM
        if len(findings) >= 2 and (
            self._has_source_backed_historical_issue_ref(findings)
            or coverage_state == CoverageState.NO_TEST_DATA
            or bool(potential_gaps)
        ):
            return RiskLevel.HIGH
        if (
            findings
            or potential_gaps
            or coverage_state in (CoverageState.NO_TEST_DATA, CoverageState.PLAN_ONLY)
            or doc_uncertainty
            or risk_uncertainty
            or test_uncertainty
        ):
            return RiskLevel.MEDIUM
        if (
            len(findings) == 0
            and not potential_gaps
            and coverage_state == CoverageState.COVERED
            and not doc_uncertainty
            and not risk_uncertainty
            and not test_uncertainty
        ):
            return RiskLevel.LOW
        return RiskLevel.MEDIUM

    @staticmethod
    def _priority(
        *,
        completeness: ReportCompleteness,
        risk_level: RiskLevel | None,
        coverage_state: CoverageState | None,
        potential_gaps: list[str],
    ) -> Priority:
        if completeness != ReportCompleteness.FULL:
            return Priority.COMPLETE_REVIEW
        if risk_level == RiskLevel.HIGH and (
            coverage_state == CoverageState.NO_TEST_DATA or bool(potential_gaps)
        ):
            return Priority.ACT_NOW
        if risk_level in (RiskLevel.HIGH, RiskLevel.MEDIUM):
            return Priority.SCHEDULE_REVIEW
        if risk_level == RiskLevel.LOW:
            return Priority.MONITOR
        return Priority.COMPLETE_REVIEW

    @staticmethod
    def _build_uncertainties(
        *,
        document: DocumentAnalysisResult,
        risk: RiskRetrievalResult | None,
        test: TestReviewResult | None,
        risk_failure: BranchFailure | None,
        test_failure: BranchFailure | None,
        completeness: ReportCompleteness,
    ) -> list[ReportUncertainty]:
        entries: list[ReportUncertainty] = []
        if document.uncertainty:
            entries.append(ReportUncertainty(branch="document_analysis", message=document.uncertainty))
        if risk is not None:
            if risk.uncertainty:
                entries.append(ReportUncertainty(branch="risk_retrieval", message=risk.uncertainty))
        else:
            entries.append(
                ReportUncertainty(
                    branch="risk_retrieval",
                    message=_unavailable_message("risk_retrieval", risk_failure),
                )
            )
        if test is not None:
            if test.uncertainty:
                entries.append(ReportUncertainty(branch="test_review", message=test.uncertainty))
        else:
            entries.append(
                ReportUncertainty(
                    branch="test_review",
                    message=_unavailable_message("test_review", test_failure),
                )
            )
        if completeness != ReportCompleteness.FULL:
            entries.append(
                ReportUncertainty(
                    branch="workflow",
                    message="Report is partial; some sections are unavailable.",
                )
            )
        return entries

    @staticmethod
    def _failure(
        workflow_result: FeatureRiskReviewWorkflowResult,
        summary: str,
    ) -> FeatureRiskReviewAggregationFailure:
        return FeatureRiskReviewAggregationFailure(
            workflow_status=workflow_result.workflow_status,
            summary=summary,
        )


def _completeness(risk_available: bool, test_available: bool) -> ReportCompleteness:
    if risk_available and test_available:
        return ReportCompleteness.FULL
    if not risk_available:
        return ReportCompleteness.PARTIAL_RISK_UNAVAILABLE
    return ReportCompleteness.PARTIAL_TEST_UNAVAILABLE


def _unavailable_sections(completeness: ReportCompleteness) -> list[str]:
    if completeness == ReportCompleteness.PARTIAL_RISK_UNAVAILABLE:
        return ["High-Risk Scenarios", "Historical Issues"]
    if completeness == ReportCompleteness.PARTIAL_TEST_UNAVAILABLE:
        return ["Existing Coverage", "Missing Cases"]
    return []


def _unavailable_message(branch: str, failure: BranchFailure | None) -> str:
    display = _BRANCH_DISPLAY.get(branch, branch)
    reason = failure.message if failure is not None else "branch unavailable"
    return f"{display} branch unavailable: {reason}"


def _uncertainty_summary(entries: list[ReportUncertainty]) -> str | None:
    if not entries:
        return None
    parts = [f"[{_UNCERTAINTY_LABEL.get(e.branch, e.branch)}] {e.message}" for e in entries]
    return " ".join(parts)


def _dedup_by_value(findings: list[RiskFinding]) -> list[RiskFinding]:
    seen: set[str] = set()
    result: list[RiskFinding] = []
    for finding in findings:
        key = json.dumps(finding.model_dump(mode="json"), sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def _finding_sort_key(finding: RiskFinding) -> tuple[object, ...]:
    distinct_refs = len(set(finding.historical_issue_refs)) + len(
        {ref.evidence_id for ref in finding.evidence_refs}
    )
    return (
        -int(bool(finding.historical_issue_refs)),
        -distinct_refs,
        finding.description.casefold(),
        json.dumps(finding.model_dump(mode="json"), sort_keys=True),
    )


def render_feature_risk_review_markdown(report: FeatureRiskReviewReport) -> str:
    """确定性 Markdown 渲染；不调用 LLM，不改写 report 语义。"""
    labels = {ref.evidence_id: f"[C{i + 1}]" for i, ref in enumerate(report.evidence_refs)}
    lines: list[str] = ["# Feature Risk Review", ""]
    lines.append(f"- case_id: {report.case_id}")
    lines.append(f"- Report completeness: {report.completeness.value}")
    lines.append(f"- Risk Level: {report.risk_level.value if report.risk_level else 'Unavailable'}")
    lines.append(f"- Priority: {report.priority.value if report.priority else 'Unavailable'}")
    lines.append("")

    lines.append("## Feature Summary")
    lines.append("")
    lines.append(report.feature_summary)
    lines.append("")

    lines.append("## Change Points")
    lines.append("")
    for cp in report.change_points:
        lines.append(f"- {cp.description}")
    if not report.change_points:
        lines.append("- None")
    lines.append("")

    lines.append("## High-Risk Scenarios")
    lines.append("")
    if report.completeness == ReportCompleteness.PARTIAL_RISK_UNAVAILABLE:
        lines.append("Unavailable — risk retrieval branch failed.")
    else:
        for finding in report.high_risk_scenarios:
            lines.append(f"- {finding.description} {_resolve_citations(finding.evidence_refs, labels)}")
            lines.append(f"  - Risk area: {finding.risk_area}")
            if finding.affected_components:
                lines.append(f"  - Affected components: {', '.join(finding.affected_components)}")
    lines.append("")

    lines.append("## Historical Issues")
    lines.append("")
    if report.completeness == ReportCompleteness.PARTIAL_RISK_UNAVAILABLE:
        lines.append("Unavailable — risk retrieval branch failed.")
    else:
        for issue in report.historical_issues:
            severity = issue.severity if issue.severity else "not provided"
            lines.append(
                f"- {issue.title} (issue_id={issue.issue_id}) "
                f"{_resolve_citations([issue.evidence_ref], labels)}"
            )
            lines.append(f"  - component: {issue.component}")
            lines.append(f"  - severity: {severity}")
            lines.append(f"  - description: {issue.description}")
    lines.append("")

    lines.append("## Existing Coverage")
    lines.append("")
    if report.completeness == ReportCompleteness.PARTIAL_TEST_UNAVAILABLE:
        lines.append("Unavailable — test review branch failed.")
    else:
        state = report.coverage_state.value if report.coverage_state else "unknown"
        lines.append(f"- coverage_state: {state}")
        for plan in report.existing_coverage:
            lines.append(f"- Test Plan {plan.plan_id} {_resolve_citations([plan.evidence_ref], labels)}")
        for case in report.existing_test_cases:
            lines.append(
                f"- Test Case {case.test_case_id}: {case.description} "
                f"{_resolve_citations([case.evidence_ref], labels)}"
            )
    lines.append("")

    lines.append("## Coverage Assessment")
    lines.append("")
    if report.completeness == ReportCompleteness.PARTIAL_TEST_UNAVAILABLE:
        lines.append("Unavailable — test review branch failed.")
    else:
        lines.append(report.coverage_assessment or "Not provided")
    lines.append("")

    lines.append("## Missing / Recommended Cases")
    lines.append("")
    if report.completeness == ReportCompleteness.PARTIAL_TEST_UNAVAILABLE:
        lines.append("Unavailable — test review branch failed.")
    else:
        lines.append("Potential gaps:")
        if report.potential_gaps:
            for gap in report.potential_gaps:
                lines.append(f"- {gap}")
        else:
            lines.append("- None")
        lines.append("")
        lines.append("Recommended missing cases (RECOMMENDATION, not existing tests):")
        if report.missing_cases:
            for case in report.missing_cases:
                lines.append(f"- {case}")
        else:
            lines.append("- None")
    lines.append("")

    lines.append("## Risk Level and Priority")
    lines.append("")
    lines.append(f"- Risk Level: {report.risk_level.value if report.risk_level else 'Unavailable'}")
    lines.append(f"- Priority: {report.priority.value if report.priority else 'Unavailable'}")
    lines.append("")

    lines.append("## Uncertainty")
    lines.append("")
    if report.uncertainties:
        for entry in report.uncertainties:
            lines.append(f"- [{entry.branch}] {entry.message}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Evidence")
    lines.append("")
    if report.evidence_refs:
        for i, ref in enumerate(report.evidence_refs, start=1):
            lines.append(f"- [{ref.evidence_id}] [C{i}]")
            lines.append(f"  - source_type: {ref.source_type}")
            lines.append(f"  - source_id: {ref.source_id}")
            lines.append(f"  - section: {ref.section or 'n/a'}")
            lines.append(f"  - source_url: {ref.source_url}")
    else:
        lines.append("- None")

    return "\n".join(lines) + "\n"


def _resolve_citations(
    evidence_refs: list[EvidenceRef],
    labels: dict[str, str],
) -> str:
    resolved: list[str] = []
    for ref in evidence_refs:
        label = labels.get(ref.evidence_id)
        if label is None:
            raise ValueError(
                f"citation integrity error: evidence_id={ref.evidence_id!r} "
                "not present in report evidence_refs"
            )
        resolved.append(label)
    return "".join(resolved)


__all__ = [
    "FeatureRiskReviewAggregationFailure",
    "FeatureRiskReviewAggregator",
    "render_feature_risk_review_markdown",
]
