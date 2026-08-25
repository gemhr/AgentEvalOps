"""WP2 focused tests: workflow 依赖、并发 overlap、failure semantics 与 evidence 传播。"""

# ruff: noqa: D415

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from app.adapters.feature_risk_review.data_provider import NormalizedFeatureRiskReviewDataProvider
from app.core.feature_risk_review import (
    BranchFailure,
    BranchResult,
    BranchStatus,
    DocumentAnalysisModelOutput,
    DocumentAnalysisResult,
    EvidenceRef,
    FeatureChangePoint,
    FeatureDocument,
    FeatureRiskReviewCase,
    FeatureRiskReviewModelOutputError,
    FeatureRiskReviewWorkflow,
    FeatureRiskReviewWorkflowResult,
    HistoricalIssue,
    RiskFindingModelOutput,
    RiskRetrievalModelOutput,
    RiskRetrievalQuery,
    TestEvidence,
    TestPlan,
    TestReviewModelOutput,
    WorkflowStatus,
    load_feature_risk_review_cases,
)
from app.core.feature_risk_review.ports import RetrievedKnowledgeFragment

ASSET_ROOT = Path(__file__).resolve().parents[2] / "evaluation_assets" / "feature_risk_review_v1"


def evidence_ref(evidence_id: str = "ev-1") -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        source_type="kubernetes_issue_snapshot",
        source_id="1",
        source_path="retrieval/enrichment/issue.json",
        source_url="https://github.com/kubernetes/kubernetes/issues/1",
        section="issue_body",
    )


def document_model_output() -> DocumentAnalysisModelOutput:
    return DocumentAnalysisModelOutput(
        feature_summary="Adds exec-based credentials.",
        change_points=[
            FeatureChangePoint(
                description="Add external credential exec flow",
                affected_components=["client-go"],
                change_type="api_change",
                potential_risk_areas=["credential rotation"],
            )
        ],
        affected_components=["client-go"],
        potential_risk_areas=["credential exfiltration"],
        uncertainty=None,
    )


def document_result() -> DocumentAnalysisResult:
    out = document_model_output()
    return DocumentAnalysisResult(
        case_id="k8s_test",
        feature_summary=out.feature_summary,
        change_points=list(out.change_points),
        affected_components=list(out.affected_components),
        potential_risk_areas=list(out.potential_risk_areas),
        uncertainty=out.uncertainty,
    )


def risk_model_output(evidence_id: str = "hist-ev", issue_id: str = "1287") -> RiskRetrievalModelOutput:
    return RiskRetrievalModelOutput(
        risk_findings=[
            RiskFindingModelOutput(
                description="credential rotation risk",
                affected_components=["client-go"],
                risk_area="credential handling",
                evidence_ids=[evidence_id],
                historical_issue_ids=[issue_id],
            )
        ],
        uncertainty=None,
    )


def review_model_output() -> TestReviewModelOutput:
    return TestReviewModelOutput(coverage_assessment="plans exist", recommended_missing_cases=["add case"])


def make_case(case_id: str = "k8s_test") -> FeatureRiskReviewCase:
    ref = evidence_ref("case-ev")
    feature = FeatureDocument(
        case_id=case_id,
        feature_id="999",
        title="Test feature",
        agent_visible_content="A feature changes kubelet restart behavior.",
        source=ref,
    )
    issue = HistoricalIssue(
        issue_id="1287",
        title="In-place update",
        description="tracking issue",
        component="sig-node",
        state="closed",
        severity=None,
        evidence_ref=ref,
    )
    plan = TestPlan(plan_id="p1", case_id=case_id, content="test plan", evidence_ref=ref)
    return FeatureRiskReviewCase(feature_document=feature, historical_issues=[issue], test_plans=[plan], test_cases=[])


class FakeModelPort:
    """按 schema 返回预置模型输出，或按配置抛错。"""

    def __init__(self, outputs: dict[type, object] | None = None, raise_error: Exception | None = None) -> None:
        self.outputs = outputs or {}
        self.raise_error = raise_error
        self.calls: list[str] = []

    async def generate(self, *, prompt: str, response_schema: type):
        """按 schema 返回预置输出或抛出配置的错误。"""
        self.calls.append(response_schema.__name__)
        if self.raise_error is not None:
            raise self.raise_error
        value = self.outputs.get(response_schema)
        if value is None:
            raise AssertionError(f"no canned output for {response_schema.__name__}")
        return value


class RaisingSchemaModel(FakeModelPort):
    """对指定 schema 抛错的模型 fake。"""

    def __init__(self, *, raise_for: set[type], outputs: dict[type, object], error: Exception) -> None:
        super().__init__(outputs)
        self.raise_for = raise_for
        self.error = error

    async def generate(self, *, prompt: str, response_schema: type):
        """对指定 schema 抛错，其余返回预置输出。"""
        self.calls.append(response_schema.__name__)
        if response_schema in self.raise_for:
            raise self.error
        return self.outputs[response_schema]


class FakeDataProvider:
    """按 case_id 提供 canned historical issues / test evidence。"""

    def __init__(
        self,
        issues: dict[str, list[HistoricalIssue]] | None = None,
        test_evidence: dict[str, TestEvidence] | None = None,
    ) -> None:
        self.issues = issues or {}
        self.test_evidence_by_case = test_evidence or {}
        self.historical_calls: list[str] = []
        self.test_calls: list[str] = []

    async def historical_issues(self, *, case_id: str, query_inputs: RiskRetrievalQuery) -> list[HistoricalIssue]:
        """记录调用并返回 canned issues。"""
        self.historical_calls.append(case_id)
        return list(self.issues.get(case_id, []))

    async def test_evidence(self, *, case_id: str) -> TestEvidence:
        """记录调用并返回 canned test evidence。"""
        self.test_calls.append(case_id)
        return self.test_evidence_by_case[case_id]


class FakeRetriever:
    """返回 canned retrieved fragments 的 retriever fake。"""

    def __init__(self, fragments: list[RetrievedKnowledgeFragment] | None = None) -> None:
        self.fragments = fragments or []
        self.calls: list[tuple[str, int]] = []

    async def retrieve(self, *, query: str, top_k: int = 5) -> list[RetrievedKnowledgeFragment]:
        """记录调用并返回 canned fragments。"""
        self.calls.append((query, top_k))
        return list(self.fragments[:top_k])


def default_provider(case: FeatureRiskReviewCase) -> FakeDataProvider:
    return FakeDataProvider(
        issues={case.feature_document.case_id: list(case.historical_issues)},
        test_evidence={case.feature_document.case_id: TestEvidence(test_plans=list(case.test_plans), test_cases=list(case.test_cases))},
    )


def default_outputs() -> dict[type, object]:
    return {
        DocumentAnalysisModelOutput: document_model_output(),
        RiskRetrievalModelOutput: risk_model_output(),
        TestReviewModelOutput: review_model_output(),
    }


async def test_both_branches_success_is_workflow_success() -> None:
    case = make_case()
    model = FakeModelPort(default_outputs())
    wf = FeatureRiskReviewWorkflow(
        model_port=model, data_provider=default_provider(case), retriever=FakeRetriever()
    )
    result = await wf.run(case)
    assert result.workflow_status == WorkflowStatus.SUCCESS
    assert result.document_analysis.status == BranchStatus.SUCCESS
    assert result.risk_retrieval.status == BranchStatus.SUCCESS
    assert result.test_review.status == BranchStatus.SUCCESS
    assert result.test_review.value is not None
    assert result.test_review.value.coverage_state.value == "PLAN_ONLY"


async def test_document_analysis_failure_leaves_branches_not_started() -> None:
    case = make_case()
    provider = default_provider(case)
    retriever = FakeRetriever()
    model = FakeModelPort(
        outputs=default_outputs(), raise_error=FeatureRiskReviewModelOutputError("document failed")
    )
    wf = FeatureRiskReviewWorkflow(model_port=model, data_provider=provider, retriever=retriever)
    result = await wf.run(case)
    assert result.workflow_status == WorkflowStatus.FAILED
    assert result.document_analysis.status == BranchStatus.FAILED
    assert result.document_analysis.failure is not None
    assert result.risk_retrieval.status == BranchStatus.NOT_STARTED
    assert result.test_review.status == BranchStatus.NOT_STARTED
    assert result.risk_retrieval.value is None
    assert result.test_review.value is None
    assert provider.historical_calls == []
    assert provider.test_calls == []
    assert retriever.calls == []


async def test_risk_failure_is_partial_and_test_preserved() -> None:
    case = make_case()
    model = RaisingSchemaModel(
        raise_for={RiskRetrievalModelOutput},
        outputs=default_outputs(),
        error=FeatureRiskReviewModelOutputError("risk failed"),
    )
    wf = FeatureRiskReviewWorkflow(
        model_port=model, data_provider=default_provider(case), retriever=FakeRetriever()
    )
    result = await wf.run(case)
    assert result.workflow_status == WorkflowStatus.PARTIAL
    assert result.document_analysis.status == BranchStatus.SUCCESS
    assert result.risk_retrieval.status == BranchStatus.FAILED
    assert result.test_review.status == BranchStatus.SUCCESS
    assert result.test_review.value is not None
    assert result.risk_retrieval.failure is not None
    assert result.risk_retrieval.failure.branch == "risk_retrieval"
    assert "traceback" not in result.risk_retrieval.failure.message.lower()


async def test_test_failure_is_partial_and_risk_preserved() -> None:
    case = make_case()
    model = RaisingSchemaModel(
        raise_for={TestReviewModelOutput},
        outputs=default_outputs(),
        error=FeatureRiskReviewModelOutputError("test failed"),
    )
    wf = FeatureRiskReviewWorkflow(
        model_port=model, data_provider=default_provider(case), retriever=FakeRetriever()
    )
    result = await wf.run(case)
    assert result.workflow_status == WorkflowStatus.PARTIAL
    assert result.risk_retrieval.status == BranchStatus.SUCCESS
    assert result.risk_retrieval.value is not None
    assert result.test_review.status == BranchStatus.FAILED
    assert result.test_review.failure is not None


async def test_both_branches_fail_is_workflow_failed_with_document_preserved() -> None:
    case = make_case()
    model = RaisingSchemaModel(
        raise_for={RiskRetrievalModelOutput, TestReviewModelOutput},
        outputs=default_outputs(),
        error=FeatureRiskReviewModelOutputError("branch failed"),
    )
    wf = FeatureRiskReviewWorkflow(
        model_port=model, data_provider=default_provider(case), retriever=FakeRetriever()
    )
    result = await wf.run(case)
    assert result.workflow_status == WorkflowStatus.FAILED
    assert result.document_analysis.status == BranchStatus.SUCCESS
    assert result.document_analysis.value is not None
    assert result.risk_retrieval.status == BranchStatus.FAILED
    assert result.test_review.status == BranchStatus.FAILED
    assert result.risk_retrieval.failure is not None
    assert result.test_review.failure is not None


class BarrierModelPort(FakeModelPort):
    """用 asyncio.Event 证明 RiskRetrieval 与 TestReview 真正 overlap。"""

    def __init__(self, outputs: dict[type, object]) -> None:
        super().__init__(outputs)
        self.risk_entered = asyncio.Event()
        self.test_entered = asyncio.Event()

    async def generate(self, *, prompt: str, response_schema: type):
        """Risk 与 test 分支先互等对方进入，证明并发 overlap。"""
        self.calls.append(response_schema.__name__)
        if response_schema is RiskRetrievalModelOutput:
            self.risk_entered.set()
            await asyncio.wait_for(self.test_entered.wait(), timeout=2)
            return self.outputs[response_schema]
        if response_schema is TestReviewModelOutput:
            self.test_entered.set()
            await asyncio.wait_for(self.risk_entered.wait(), timeout=2)
            return self.outputs[response_schema]
        return self.outputs[response_schema]


async def test_risk_and_test_branches_really_overlap() -> None:
    """两个 branch 必须同时进入模型调用；序列化执行会让 barrier 超时 -> PARTIAL。"""
    case = make_case()
    model = BarrierModelPort(default_outputs())
    wf = FeatureRiskReviewWorkflow(
        model_port=model, data_provider=default_provider(case), retriever=FakeRetriever()
    )
    result = await asyncio.wait_for(wf.run(case), timeout=10)
    assert result.workflow_status == WorkflowStatus.SUCCESS
    assert model.risk_entered.is_set()
    assert model.test_entered.is_set()
    assert result.risk_retrieval.status == BranchStatus.SUCCESS
    assert result.test_review.status == BranchStatus.SUCCESS


async def test_evidence_refs_propagate_unchanged_from_provider_and_retriever() -> None:
    case = make_case()
    issue_ev = EvidenceRef(
        evidence_id="issue-ev",
        source_type="github_enhancement_tracking_issue",
        source_id="1287",
        source_path="raw/github_issues/enhancements_1287.json",
        source_url="https://github.com/kubernetes/enhancements/issues/1287",
    )
    hist_ev = EvidenceRef(
        evidence_id="hist-ev",
        source_type="kubernetes_issue_snapshot",
        source_id="116415",
        source_path="retrieval/enrichment/kubernetes_kubernetes_issue_1287_116415.json",
        source_url="https://github.com/kubernetes/kubernetes/issues/116415",
    )
    issue = HistoricalIssue(
        issue_id="1287",
        title="In-place update",
        description="tracking",
        component="sig-node",
        state="closed",
        severity=None,
        evidence_ref=issue_ev,
    )
    provider = FakeDataProvider(
        issues={"k8s_test": [issue]},
        test_evidence={"k8s_test": TestEvidence(test_plans=list(case.test_plans), test_cases=[])},
    )
    retriever = FakeRetriever(
        [RetrievedKnowledgeFragment(source_fragment="resize restarts pod", evidence_ref=hist_ev)]
    )
    model = FakeModelPort(
        {
            DocumentAnalysisModelOutput: document_model_output(),
            RiskRetrievalModelOutput: risk_model_output(evidence_id="hist-ev", issue_id="1287"),
            TestReviewModelOutput: review_model_output(),
        }
    )
    wf = FeatureRiskReviewWorkflow(model_port=model, data_provider=provider, retriever=retriever)
    result = await wf.run(case)
    assert result.workflow_status == WorkflowStatus.SUCCESS
    risk = result.risk_retrieval.value
    assert risk is not None
    assert risk.retrieved_historical_issues[0].evidence_ref == issue_ev
    assert risk.retrieved_evidence[0].evidence_ref == hist_ev
    assert risk.agent_inferred_risk_findings[0].evidence_refs == [hist_ev]
    assert risk.agent_inferred_risk_findings[0].historical_issue_refs == ["1287"]


def test_branch_result_validation() -> None:
    with pytest.raises(ValueError):
        BranchResult[DocumentAnalysisResult](
            branch="x", status=BranchStatus.SUCCESS, failure=BranchFailure(branch="x", error_type="E", message="m")
        )
    with pytest.raises(ValueError):
        BranchResult[DocumentAnalysisResult](branch="x", status=BranchStatus.NOT_STARTED, value=document_result())
    ok = BranchResult[DocumentAnalysisResult](branch="x", status=BranchStatus.SUCCESS, value=document_result())
    assert ok.status == BranchStatus.SUCCESS


async def test_five_real_cases_run_through_workflow_without_annotation_leakage(tmp_path: Path) -> None:
    copied = tmp_path / "dataset"
    shutil.copytree(ASSET_ROOT, copied)
    shutil.rmtree(copied / "annotations")
    cases = load_feature_risk_review_cases(copied)
    assert len(cases) == 5
    provider = NormalizedFeatureRiskReviewDataProvider(root=copied)
    model = FakeModelPort(
        {
            DocumentAnalysisModelOutput: document_model_output(),
            RiskRetrievalModelOutput: RiskRetrievalModelOutput(risk_findings=[], uncertainty=None),
            TestReviewModelOutput: review_model_output(),
        }
    )
    wf = FeatureRiskReviewWorkflow(model_port=model, data_provider=provider, retriever=FakeRetriever())
    for case in cases:
        result: FeatureRiskReviewWorkflowResult = await wf.run(case)
        assert result.workflow_status == WorkflowStatus.SUCCESS, case.feature_document.case_id
        assert result.test_review.value is not None
        assert result.test_review.value.coverage_state.value == "PLAN_ONLY"
        dumped = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        assert "expected_" not in dumped
        assert "evaluation_reference" not in dumped
        assert "annotations" not in dumped