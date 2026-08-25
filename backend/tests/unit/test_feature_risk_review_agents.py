"""WP2 focused tests: 三个 Agent 的输入/输出、evidence 边界与 coverage state。"""

# ruff: noqa: D415

from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.feature_risk_review.model import parse_structured_model_output
from app.core.feature_risk_review import (
    CoverageState,
    DocumentAnalysisAgent,
    DocumentAnalysisModelOutput,
    DocumentAnalysisResult,
    EvidenceRef,
    FeatureChangePoint,
    FeatureDocument,
    FeatureRiskReviewModelOutputError,
    HistoricalIssue,
    RiskFindingModelOutput,
    RiskRetrievalAgent,
    RiskRetrievalModelOutput,
    RiskRetrievalQuery,
    TestCase,
    TestEvidence,
    TestPlan,
    TestReviewAgent,
    TestReviewModelOutput,
    compute_coverage_state,
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


def feature_document(content: str = "A feature changes kubelet restart behavior.") -> FeatureDocument:
    return FeatureDocument(
        case_id="k8s_test",
        feature_id="999",
        title="Test feature",
        agent_visible_content=content,
        source=evidence_ref("feature-ev"),
    )


def document_model_output() -> DocumentAnalysisModelOutput:
    return DocumentAnalysisModelOutput(
        feature_summary="Adds exec-based credentials.",
        change_points=[
            FeatureChangePoint(
                description="Add external credential exec flow",
                affected_components=["client-go", "kubeconfig"],
                change_type="api_change",
                potential_risk_areas=["credential rotation"],
            )
        ],
        affected_components=["client-go", "kubeconfig"],
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


class FakeModelPort:
    """按 response_schema 返回 canned output，或按配置抛错。"""

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


async def test_document_analysis_success() -> None:
    agent = DocumentAnalysisAgent(FakeModelPort({DocumentAnalysisModelOutput: document_model_output()}))
    result = await agent.analyze(feature_document())
    assert result.case_id == "k8s_test"
    assert result.change_points[0].description == "Add external credential exec flow"
    assert result.affected_components == ["client-go", "kubeconfig"]


async def test_document_analysis_failure_propagates() -> None:
    model = FakeModelPort(raise_error=FeatureRiskReviewModelOutputError("bad output"))
    agent = DocumentAnalysisAgent(model)
    with pytest.raises(FeatureRiskReviewModelOutputError):
        await agent.analyze(feature_document())


async def test_risk_retrieval_filters_unknown_evidence_refs() -> None:
    issue = HistoricalIssue(
        issue_id="1287",
        title="In-place update",
        description="tracking issue",
        component="sig-node",
        state="closed",
        severity=None,
        evidence_ref=evidence_ref("issue-ev"),
    )
    retrieved = RetrievedKnowledgeFragment(
        source_fragment="resize restarts pod", evidence_ref=evidence_ref("hist-ev")
    )
    model_output = RiskRetrievalModelOutput(
        risk_findings=[
            RiskFindingModelOutput(
                description="resize can restart pod",
                affected_components=["kubelet"],
                risk_area="state consistency",
                evidence_ids=["hist-ev"],
                historical_issue_ids=["1287"],
            ),
            RiskFindingModelOutput(
                description="fabricated evidence finding",
                affected_components=["x"],
                risk_area="y",
                evidence_ids=["unknown-evidence"],
                historical_issue_ids=["1287"],
            ),
        ]
    )
    agent = RiskRetrievalAgent(
        model_port=FakeModelPort({RiskRetrievalModelOutput: model_output}),
        data_provider=FakeDataProvider(issues={"k8s_test": [issue]}),
        retriever=FakeRetriever([retrieved]),
    )
    result = await agent.review(document_result())
    assert [f.description for f in result.agent_inferred_risk_findings] == ["resize can restart pod"]
    assert result.agent_inferred_risk_findings[0].evidence_refs == [evidence_ref("hist-ev")]
    assert result.agent_inferred_risk_findings[0].historical_issue_refs == ["1287"]
    assert result.filtered_invalid_findings == ["fabricated evidence finding"]


async def test_risk_retrieval_unknown_issue_id_filters_finding() -> None:
    issue = HistoricalIssue(
        issue_id="1287",
        title="In-place update",
        description="tracking issue",
        component="sig-node",
        state="closed",
        severity=None,
        evidence_ref=evidence_ref("issue-ev"),
    )
    retrieved = RetrievedKnowledgeFragment(
        source_fragment="resize restarts pod", evidence_ref=evidence_ref("hist-ev")
    )
    agent = RiskRetrievalAgent(
        model_port=FakeModelPort(
            {
                RiskRetrievalModelOutput: RiskRetrievalModelOutput(
                    risk_findings=[
                        RiskFindingModelOutput(
                            description="references bogus issue",
                            affected_components=["a"],
                            risk_area="b",
                            evidence_ids=["hist-ev"],
                            historical_issue_ids=["9999"],
                        )
                    ]
                )
            }
        ),
        data_provider=FakeDataProvider(issues={"k8s_test": [issue]}),
        retriever=FakeRetriever([retrieved]),
    )
    result = await agent.review(document_result())
    assert result.agent_inferred_risk_findings == []
    assert len(result.filtered_invalid_findings) == 1


async def test_risk_retrieval_missing_evidence_filters_whole_finding() -> None:
    agent = RiskRetrievalAgent(
        model_port=FakeModelPort(
            {
                RiskRetrievalModelOutput: RiskRetrievalModelOutput(
                    risk_findings=[
                        RiskFindingModelOutput(
                            description="no evidence at all",
                            affected_components=["a"],
                            risk_area="b",
                            evidence_ids=["missing"],
                        )
                    ]
                )
            }
        ),
        data_provider=FakeDataProvider(),
        retriever=FakeRetriever([]),
    )
    result = await agent.review(document_result())
    assert result.agent_inferred_risk_findings == []
    assert len(result.filtered_invalid_findings) == 1


def test_coverage_state_semantics() -> None:
    plan = TestPlan(plan_id="p1", case_id="k8s_test", content="test plan", evidence_ref=evidence_ref("plan-ev"))
    case = TestCase(test_case_id="tc1", description="case", evidence_ref=evidence_ref("tc-ev"))
    assert compute_coverage_state([], []) == CoverageState.NO_TEST_DATA
    assert compute_coverage_state([plan], []) == CoverageState.PLAN_ONLY
    assert compute_coverage_state([plan], [case]) == CoverageState.PARTIAL_COVERAGE
    assert CoverageState.PLAN_ONLY != CoverageState.NO_TEST_DATA


async def test_test_review_plan_only_result_preserves_source_facts() -> None:
    plan = TestPlan(plan_id="p1", case_id="k8s_test", content="test plan", evidence_ref=evidence_ref("plan-ev"))
    provider = FakeDataProvider(test_evidence={"k8s_test": TestEvidence(test_plans=[plan], test_cases=[])})
    agent = TestReviewAgent(
        model_port=FakeModelPort(
            {TestReviewModelOutput: TestReviewModelOutput(coverage_assessment="plans exist", recommended_missing_cases=["add exec case"])}
        ),
        data_provider=provider,
    )
    result = await agent.review(document_result())
    assert result.coverage_state == CoverageState.PLAN_ONLY
    assert result.test_plans == [plan]
    assert result.test_cases == []
    assert result.recommended_missing_cases == ["add exec case"]
    assert result.evidence_refs == [evidence_ref("plan-ev")]


async def test_test_review_no_test_data_state() -> None:
    provider = FakeDataProvider(test_evidence={"k8s_test": TestEvidence(test_plans=[], test_cases=[])})
    agent = TestReviewAgent(
        model_port=FakeModelPort(
            {TestReviewModelOutput: TestReviewModelOutput(coverage_assessment="no tests")}
        ),
        data_provider=provider,
    )
    result = await agent.review(document_result())
    assert result.coverage_state == CoverageState.NO_TEST_DATA


def test_malformed_structured_model_output_fails_clearly() -> None:
    with pytest.raises(FeatureRiskReviewModelOutputError):
        parse_structured_model_output("not json at all", DocumentAnalysisModelOutput)
    with pytest.raises(FeatureRiskReviewModelOutputError):
        parse_structured_model_output('{"feature_summary": "x"}', DocumentAnalysisModelOutput)
    valid = parse_structured_model_output(
        '```json\n{"feature_summary": "ok", "change_points": [], "affected_components": ["a"], "potential_risk_areas": []}\n```',
        DocumentAnalysisModelOutput,
    )
    assert valid.feature_summary == "ok"