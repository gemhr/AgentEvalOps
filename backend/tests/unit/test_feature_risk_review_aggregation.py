"""WP3 focused tests: deterministic risk aggregation + citation report.

覆盖 SUCCESS / PARTIAL / FAILED 语义、Risk Level / Priority 策略、Evidence 身份
保留、citation conflict 拒绝、uncertainty 保留与 deterministic Markdown 渲染。
"""

# ruff: noqa: D415

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.feature_risk_review import (
    BranchResult,
    BranchStatus,
    CoverageState,
    DocumentAnalysisResult,
    EvidenceRef,
    FeatureChangePoint,
    FeatureRiskReviewAggregationFailure,
    FeatureRiskReviewAggregator,
    FeatureRiskReviewReport,
    FeatureRiskReviewWorkflowResult,
    HistoricalIssue,
    Priority,
    ReportCompleteness,
    RiskFinding,
    RiskRetrievalResult,
    TestCase,
    TestPlan,
    TestReviewResult,
    WorkflowStatus,
    compute_coverage_state,
    render_feature_risk_review_markdown,
)

ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "evaluation_assets"
    / "feature_risk_review_v1"
    / "experiments"
    / "wp2_real_model_smoke_retry2_k8s_541.json"
)


def ev(evidence_id: str = "e1") -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        source_type="kubernetes_issue_snapshot",
        source_id="541",
        source_path="retrieval/enrichment/issue.json",
        source_url=f"https://github.com/kubernetes/kubernetes/issues/{evidence_id}",
        section="body",
    )


def document_result(case_id: str = "k8s_test", uncertainty: str | None = None) -> DocumentAnalysisResult:
    return DocumentAnalysisResult(
        case_id=case_id,
        feature_summary="Adds exec-based auth.",
        change_points=[
            FeatureChangePoint(description="Add credential exec flow", affected_components=["client-go"])
        ],
        affected_components=["client-go"],
        potential_risk_areas=["credential rotation"],
        uncertainty=uncertainty,
    )


def risk_result(
    findings: list[RiskFinding] | None = None,
    historical_issues: list[HistoricalIssue] | None = None,
    retrieved_evidence: list[EvidenceRef] | None = None,
    uncertainty: str | None = None,
) -> RiskRetrievalResult:
    return RiskRetrievalResult(
        case_id="k8s_test",
        retrieved_historical_issues=list(historical_issues or []),
        retrieved_evidence=[
            _fragment(ref) for ref in (retrieved_evidence or [])
        ],
        agent_inferred_risk_findings=list(findings or []),
        filtered_invalid_findings=[],
        uncertainty=uncertainty,
    )


def _fragment(ref: EvidenceRef):
    from app.core.feature_risk_review.ports import RetrievedKnowledgeFragment

    return RetrievedKnowledgeFragment(source_fragment="fragment", evidence_ref=ref)


def make_test_result(
    test_plans: list[TestPlan] | None = None,
    test_cases: list[TestCase] | None = None,
    coverage_state: CoverageState | None = None,
    potential_gaps: list[str] | None = None,
    recommended: list[str] | None = None,
    coverage_assessment: str = "coverage assessment",
    uncertainty: str | None = None,
) -> TestReviewResult:
    plans = list(test_plans or [])
    cases = list(test_cases or [])
    state = coverage_state if coverage_state is not None else compute_coverage_state(plans, cases)
    return TestReviewResult(
        case_id="k8s_test",
        coverage_state=state,
        test_plans=plans,
        test_cases=cases,
        coverage_assessment=coverage_assessment,
        potential_gaps=list(potential_gaps or []),
        recommended_missing_cases=list(recommended or []),
        evidence_refs=[p.evidence_ref for p in plans] + [c.evidence_ref for c in cases],
        uncertainty=uncertainty,
    )


def finding(description: str, evidence_refs: list[EvidenceRef], issue_refs: list[str] | None = None) -> RiskFinding:
    return RiskFinding(
        description=description,
        affected_components=["client-go"],
        risk_area="credential handling",
        historical_issue_refs=list(issue_refs or []),
        evidence_refs=evidence_refs,
    )


def workflow_result(
    *,
    workflow_status: WorkflowStatus,
    document: DocumentAnalysisResult,
    risk: RiskRetrievalResult | None = None,
    risk_status: BranchStatus = BranchStatus.SUCCESS,
    risk_failure=None,
    test: TestReviewResult | None = None,
    test_status: BranchStatus = BranchStatus.SUCCESS,
    test_failure=None,
) -> FeatureRiskReviewWorkflowResult:
    def branch(name: str, status: BranchStatus, value, failure):
        return BranchResult(
            branch=name,
            status=status,
            value=value,
            failure=failure,
        )

    return FeatureRiskReviewWorkflowResult(
        case_id=document.case_id,
        workflow_status=workflow_status,
        document_analysis=branch("document_analysis", BranchStatus.SUCCESS, document, None),
        risk_retrieval=branch("risk_retrieval", risk_status, risk, risk_failure),
        test_review=branch("test_review", test_status, test, test_failure),
    )


AGG = FeatureRiskReviewAggregator()


def full_workflow(
    *,
    findings: list[RiskFinding] | None = None,
    test_plans: list[TestPlan] | None = None,
    test_cases: list[TestCase] | None = None,
    coverage_state: CoverageState | None = None,
    potential_gaps: list[str] | None = None,
    recommended: list[str] | None = None,
    historical_issues: list[HistoricalIssue] | None = None,
    retrieved_evidence: list[EvidenceRef] | None = None,
    doc_uncertainty: str | None = None,
    risk_uncertainty: str | None = None,
    test_uncertainty: str | None = None,
) -> FeatureRiskReviewWorkflowResult:
    return workflow_result(
        workflow_status=WorkflowStatus.SUCCESS,
        document=document_result(uncertainty=doc_uncertainty),
        risk=risk_result(
            findings=findings,
            historical_issues=historical_issues,
            retrieved_evidence=retrieved_evidence,
            uncertainty=risk_uncertainty,
        ),
        test=make_test_result(
            test_plans=test_plans,
            test_cases=test_cases,
            coverage_state=coverage_state,
            potential_gaps=potential_gaps,
            recommended=recommended,
            uncertainty=test_uncertainty,
        ),
    )


def test_success_workflow_builds_full_report() -> None:
    report = AGG.aggregate(full_workflow())
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.completeness == ReportCompleteness.FULL
    assert report.case_id == "k8s_test"
    assert report.feature_summary == "Adds exec-based auth."
    assert report.uncertainties == []
    assert report.priority is not None


def test_risk_branch_failed_gives_partial_risk_unavailable() -> None:
    result = workflow_result(
        workflow_status=WorkflowStatus.PARTIAL,
        document=document_result(),
        risk_status=BranchStatus.FAILED,
        risk_failure=_branch_failure("risk_retrieval"),
        test=make_test_result(test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))]),
    )
    report = AGG.aggregate(result)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.completeness == ReportCompleteness.PARTIAL_RISK_UNAVAILABLE
    assert report.risk_level is None
    assert report.priority == Priority.COMPLETE_REVIEW
    assert report.high_risk_scenarios == []
    assert report.historical_issues == []
    assert "High-Risk Scenarios" in report.unavailable_sections
    assert "Historical Issues" in report.unavailable_sections
    assert report.existing_coverage  # test branch preserved
    assert report.coverage_assessment == "coverage assessment"


def test_test_branch_failed_gives_partial_test_unavailable() -> None:
    result = workflow_result(
        workflow_status=WorkflowStatus.PARTIAL,
        document=document_result(),
        risk=risk_result(findings=[finding("risk", [ev("e1")])], retrieved_evidence=[ev("e1")]),
        test_status=BranchStatus.FAILED,
        test_failure=_branch_failure("test_review"),
    )
    report = AGG.aggregate(result)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.completeness == ReportCompleteness.PARTIAL_TEST_UNAVAILABLE
    assert report.priority == Priority.COMPLETE_REVIEW
    assert report.risk_level is not None
    assert report.risk_level in ("HIGH", "MEDIUM")
    assert report.existing_coverage == []
    assert report.missing_cases == []
    assert report.coverage_state is None
    assert "Existing Coverage" in report.unavailable_sections
    assert "Missing Cases" in report.unavailable_sections


def _branch_failure(branch: str):
    from app.core.feature_risk_review import BranchFailure

    return BranchFailure(branch=branch, error_type="RuntimeError", message="boom")


def test_document_failure_is_aggregation_failure_not_report() -> None:
    from app.core.feature_risk_review import BranchFailure

    result = FeatureRiskReviewWorkflowResult(
        case_id="k8s_test",
        workflow_status=WorkflowStatus.FAILED,
        document_analysis=BranchResult(
            branch="document_analysis",
            status=BranchStatus.FAILED,
            failure=BranchFailure(branch="document_analysis", error_type="RuntimeError", message="doc boom"),
        ),
        risk_retrieval=BranchResult(branch="risk_retrieval", status=BranchStatus.NOT_STARTED),
        test_review=BranchResult(branch="test_review", status=BranchStatus.NOT_STARTED),
    )
    out = AGG.aggregate(result)
    assert isinstance(out, FeatureRiskReviewAggregationFailure)
    assert out.workflow_status == WorkflowStatus.FAILED


def test_both_downstream_failed_is_aggregation_failure() -> None:
    result = workflow_result(
        workflow_status=WorkflowStatus.FAILED,
        document=document_result(),
        risk_status=BranchStatus.FAILED,
        risk_failure=_branch_failure("risk_retrieval"),
        test_status=BranchStatus.FAILED,
        test_failure=_branch_failure("test_review"),
    )
    out = AGG.aggregate(result)
    assert isinstance(out, FeatureRiskReviewAggregationFailure)


def test_risk_policy_is_deterministic() -> None:
    a = AGG.aggregate(full_workflow())
    b = AGG.aggregate(full_workflow())
    assert isinstance(a, FeatureRiskReviewReport)
    assert isinstance(b, FeatureRiskReviewReport)
    assert a.risk_level == b.risk_level
    assert a.priority == b.priority


def test_same_input_same_risk_level_and_priority() -> None:
    wf = full_workflow(
        findings=[finding("r1", [ev("e1")])],
        retrieved_evidence=[ev("e1")],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.COVERED,
    )
    r1 = AGG.aggregate(wf)
    r2 = AGG.aggregate(wf)
    assert isinstance(r1, FeatureRiskReviewReport)
    assert isinstance(r2, FeatureRiskReviewReport)
    assert r1.risk_level == r2.risk_level == "MEDIUM"
    assert r1.priority == r2.priority


def test_risk_missing_means_risk_level_none() -> None:
    result = workflow_result(
        workflow_status=WorkflowStatus.PARTIAL,
        document=document_result(),
        risk_status=BranchStatus.FAILED,
        risk_failure=_branch_failure("risk_retrieval"),
        test=make_test_result(),
    )
    report = AGG.aggregate(result)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.risk_level is None
    assert report.priority == Priority.COMPLETE_REVIEW


def test_test_missing_never_low() -> None:
    result = workflow_result(
        workflow_status=WorkflowStatus.PARTIAL,
        document=document_result(),
        risk=risk_result(findings=[finding("r1", [ev("e1")])], retrieved_evidence=[ev("e1")]),
        test_status=BranchStatus.FAILED,
        test_failure=_branch_failure("test_review"),
    )
    report = AGG.aggregate(result)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.risk_level in ("HIGH", "MEDIUM")
    assert report.risk_level != "LOW"


def test_two_findings_with_historical_ref_is_high() -> None:
    issue = HistoricalIssue(
        issue_id="541",
        title="tracking issue",
        description="tracking",
        component="sig-auth",
        state="open",
        severity=None,
        evidence_ref=ev("issue-ev"),
    )
    wf = full_workflow(
        findings=[
            finding("r1", [ev("e1")], issue_refs=["541"]),
            finding("r2", [ev("e2")], issue_refs=["541"]),
        ],
        retrieved_evidence=[ev("e1"), ev("e2")],
        historical_issues=[issue],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        test_cases=[TestCase(test_case_id="tc1", description="case", evidence_ref=ev("tc"))],
        coverage_state=CoverageState.PARTIAL_COVERAGE,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.risk_level == "HIGH"


def test_two_findings_with_potential_gaps_is_high() -> None:
    wf = full_workflow(
        findings=[
            finding("r1", [ev("e1")]),
            finding("r2", [ev("e2")]),
        ],
        retrieved_evidence=[ev("e1"), ev("e2")],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        test_cases=[TestCase(test_case_id="tc1", description="case", evidence_ref=ev("tc"))],
        coverage_state=CoverageState.COVERED,
        potential_gaps=["gap"],
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.risk_level == "HIGH"
    assert report.priority == Priority.ACT_NOW


def test_one_finding_is_medium() -> None:
    wf = full_workflow(
        findings=[finding("r1", [ev("e1")])],
        retrieved_evidence=[ev("e1")],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.COVERED,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.risk_level == "MEDIUM"


def test_plan_only_never_low() -> None:
    for gaps in ([], ["g1"]):
        wf = full_workflow(
            findings=[],
            test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
            coverage_state=CoverageState.PLAN_ONLY,
            potential_gaps=gaps,
        )
        report = AGG.aggregate(wf)
        assert isinstance(report, FeatureRiskReviewReport)
        assert report.coverage_state == CoverageState.PLAN_ONLY
        assert report.risk_level in ("HIGH", "MEDIUM")
        assert report.risk_level != "LOW"


def test_low_requires_full_clean_covered() -> None:
    wf = full_workflow(
        findings=[],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        test_cases=[TestCase(test_case_id="tc1", description="case", evidence_ref=ev("tc"))],
        coverage_state=CoverageState.COVERED,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.risk_level == "LOW"
    assert report.priority == Priority.MONITOR


def test_null_historical_severity_not_invented() -> None:
    issue = HistoricalIssue(
        issue_id="541",
        title="tracking",
        description="tracking",
        component="sig-auth",
        state="open",
        severity=None,
        evidence_ref=ev("issue-ev"),
    )
    wf = full_workflow(
        findings=[finding("r1", [ev("e1")])],
        retrieved_evidence=[ev("e1")],
        historical_issues=[issue],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.COVERED,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.historical_issues[0].severity is None


def test_same_evidence_id_same_ref_deduplicated() -> None:
    wf = full_workflow(
        findings=[
            finding("r1", [ev("e1")]),
            finding("r1", [ev("e1")]),
        ],
        retrieved_evidence=[ev("e1")],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("e1"))],
        coverage_state=CoverageState.COVERED,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    ids = [ref.evidence_id for ref in report.evidence_refs]
    assert ids.count("e1") == 1
    # identical finding deduped
    assert len(report.high_risk_scenarios) == 1


def test_same_evidence_id_conflicting_object_is_failure() -> None:
    ref_a = ev("e1")
    ref_b = ev("e1")
    ref_b = EvidenceRef(
        evidence_id="e1",
        source_type="kubernetes_issue_snapshot",
        source_id="541",
        source_path="retrieval/enrichment/issue.json",
        source_url="https://github.com/kubernetes/kubernetes/issues/DIFFERENT",
        section="body",
    )
    assert ref_a != ref_b
    wf = full_workflow(
        findings=[finding("r1", [ref_a])],
        retrieved_evidence=[ref_b],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.COVERED,
    )
    out = AGG.aggregate(wf)
    assert isinstance(out, FeatureRiskReviewAggregationFailure)
    assert "citation identity conflict" in out.summary


def test_aggregator_cannot_introduce_new_evidence_ref() -> None:
    wf = full_workflow(
        findings=[finding("r1", [ev("e1")])],
        retrieved_evidence=[ev("e1")],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.COVERED,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    input_ids = {
        "e1",
        "tp",
    }
    assert {ref.evidence_id for ref in report.evidence_refs} <= input_ids


def test_existing_testplan_separate_from_missing_recommendations() -> None:
    plan = TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))
    wf = full_workflow(
        findings=[],
        test_plans=[plan],
        coverage_state=CoverageState.PLAN_ONLY,
        recommended=["add exec case"],
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.existing_coverage == [plan]
    assert report.missing_cases == ["add exec case"]


def test_plan_only_preserved() -> None:
    wf = full_workflow(
        findings=[],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.PLAN_ONLY,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.coverage_state == CoverageState.PLAN_ONLY
    assert report.existing_test_cases == []


def test_uncertainties_preserve_branch_source() -> None:
    wf = full_workflow(
        findings=[],
        doc_uncertainty="doc note",
        risk_uncertainty="risk note",
        test_uncertainty="test note",
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.COVERED,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    branches = {u.branch for u in report.uncertainties}
    assert branches == {"document_analysis", "risk_retrieval", "test_review"}
    assert report.uncertainty is not None
    assert "[Document analysis]" in report.uncertainty
    assert "[Risk retrieval]" in report.uncertainty
    assert "[Test review]" in report.uncertainty


def test_branch_unavailable_message_in_partial_uncertainty() -> None:
    result = workflow_result(
        workflow_status=WorkflowStatus.PARTIAL,
        document=document_result(uncertainty="doc note"),
        risk_status=BranchStatus.FAILED,
        risk_failure=_branch_failure("risk_retrieval"),
        test=make_test_result(),
    )
    report = AGG.aggregate(result)
    assert isinstance(report, FeatureRiskReviewReport)
    messages = [u.message for u in report.uncertainties]
    assert any("Risk retrieval branch unavailable" in m for m in messages)
    assert any(u.branch == "workflow" for u in report.uncertainties)


def test_markdown_is_deterministic() -> None:
    wf = full_workflow(
        findings=[finding("r1", [ev("e1")])],
        retrieved_evidence=[ev("e1")],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.COVERED,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    assert render_feature_risk_review_markdown(report) == render_feature_risk_review_markdown(report)


def test_markdown_citations_resolve_to_report_evidence() -> None:
    wf = full_workflow(
        findings=[finding("r1", [ev("e1")])],
        retrieved_evidence=[ev("e1")],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.COVERED,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    md = render_feature_risk_review_markdown(report)
    assert "[C1]" in md
    for i, ref in enumerate(report.evidence_refs, start=1):
        assert f"[C{i}]" in md
        assert ref.evidence_id in md


def test_risk_finding_citation_resolves() -> None:
    wf = full_workflow(
        findings=[finding("credential rotation risk", [ev("e1")], issue_refs=["541"])],
        retrieved_evidence=[ev("e1")],
        historical_issues=[
            HistoricalIssue(
                issue_id="541", title="t", description="d", component="sig-auth",
                state="open", severity=None, evidence_ref=ev("issue-ev"),
            )
        ],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.COVERED,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    md = render_feature_risk_review_markdown(report)
    assert "credential rotation risk [C1]" in md


def test_historical_issue_citation_resolves() -> None:
    wf = full_workflow(
        findings=[finding("r1", [ev("e1")])],
        retrieved_evidence=[ev("e1")],
        historical_issues=[
            HistoricalIssue(
                issue_id="541", title="tracking", description="d", component="sig-auth",
                state="open", severity=None, evidence_ref=ev("issue-ev"),
            )
        ],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.COVERED,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    md = render_feature_risk_review_markdown(report)
    assert "issue_id=541" in md
    # historical issue citation resolves without error
    assert "tracking (issue_id=541)" in md


def test_testplan_testcase_citation_resolves() -> None:
    wf = full_workflow(
        findings=[],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        test_cases=[TestCase(test_case_id="tc1", description="case desc", evidence_ref=ev("tc"))],
        coverage_state=CoverageState.PARTIAL_COVERAGE,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    md = render_feature_risk_review_markdown(report)
    assert "Test Plan p1" in md
    assert "Test Case tc1: case desc" in md


def test_partial_report_sections_explicitly_unavailable() -> None:
    result = workflow_result(
        workflow_status=WorkflowStatus.PARTIAL,
        document=document_result(),
        risk_status=BranchStatus.FAILED,
        risk_failure=_branch_failure("risk_retrieval"),
        test=make_test_result(),
    )
    report = AGG.aggregate(result)
    assert isinstance(report, FeatureRiskReviewReport)
    md = render_feature_risk_review_markdown(report)
    assert "Unavailable — risk retrieval branch failed." in md
    assert "Report completeness: PARTIAL_RISK_UNAVAILABLE" in md


def test_aggregation_succeeds_without_annotations() -> None:
    wf = full_workflow(
        findings=[finding("r1", [ev("e1")])],
        retrieved_evidence=[ev("e1")],
        test_plans=[TestPlan(plan_id="p1", case_id="k8s_test", content="plan", evidence_ref=ev("tp"))],
        coverage_state=CoverageState.COVERED,
    )
    report = AGG.aggregate(wf)
    assert isinstance(report, FeatureRiskReviewReport)
    assert report.completeness == ReportCompleteness.FULL


def test_frozen_wp2_artifact_validates_and_aggregates() -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    result = FeatureRiskReviewWorkflowResult.model_validate(payload["result"])
    assert result.workflow_status == WorkflowStatus.SUCCESS
    out = AGG.aggregate(result)
    assert isinstance(out, FeatureRiskReviewReport)
    assert out.completeness == ReportCompleteness.FULL
    assert out.case_id == "k8s_541"
    assert out.high_risk_scenarios  # findings present
    assert out.coverage_state == CoverageState.PLAN_ONLY
    # no model invocation: render is deterministic and offline
    md = render_feature_risk_review_markdown(out)
    assert md.startswith("# Feature Risk Review")
