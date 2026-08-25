"""Feature Risk Review 的 ports-and-adapters 协议。

这里定义 WP2 冻结的最小注入面：

- ``FeatureRiskReviewModelPort``：薄模型端口，只负责把 prompt + schema 变成
  typed/validated model result；Agent 内部不创建 HTTP client。
- ``FeatureRiskReviewDataProvider``：按 ``case_id`` 提供 source-backed 的
  HistoricalIssue / TestPlan / TestCase。
- ``HistoricalKnowledgeRetriever``：可选的窄 retrieval port，返回 provider/retriever
  创建的 ``EvidenceRef`` 与 source fragment。Agent 只能消费、选择、原样传播证据身份。

证据身份（EvidenceRef）只能由 provider/retriever 创建，LLM 不能制造。
"""

# ruff: noqa: D415

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel, Field, StrictStr

from app.core.feature_risk_review.contracts import EvidenceRef, HistoricalIssue, TestCase, TestPlan

T = TypeVar("T", bound=BaseModel)


class RetrievedKnowledgeFragment(BaseModel):
    """retriever 返回的一条 source fragment 与其 source-backed EvidenceRef。"""

    source_fragment: StrictStr = Field(min_length=1)
    evidence_ref: EvidenceRef
    relevance_score: float | None = None

    model_config = {"extra": "forbid", "frozen": True}


class TestEvidence(BaseModel):
    """data provider 对某个 case 的 source-backed test evidence。"""

    test_plans: list[TestPlan]
    test_cases: list[TestCase]

    model_config = {"extra": "forbid", "frozen": True}


class RiskRetrievalQuery(BaseModel):
    """RiskRetrievalAgent 交给 data provider 的 typed query inputs。

    由 DocumentAnalysisResult 派生；data provider 只把它当查询输入，不修改其真实性。
    """

    change_point_descriptions: list[StrictStr]
    affected_components: list[StrictStr]
    potential_risk_areas: list[StrictStr]

    model_config = {"extra": "forbid", "frozen": True}


class FeatureRiskReviewModelPort(Protocol):
    """Phase4 最小结构化模型端口。

    只接收 prompt 与 response schema，返回 typed/validated model result。
    模型调用、解析、验证全部由 adapter 负责。
    """

    async def generate(self, *, prompt: str, response_schema: type[T]) -> T:
        """根据 prompt 调用模型并返回验证后的 ``response_schema`` 实例。"""
        ...


class FeatureRiskReviewDataProvider(Protocol):
    """source-backed 业务数据提供者。

    只返回 WP1 normalized 数据集中的结构化记录，不推断、不补造 severity。
    """

    async def historical_issues(self, *, case_id: str, query_inputs: RiskRetrievalQuery) -> list[HistoricalIssue]:
        """按 case_id 返回 source-backed historical issues。"""
        ...

    async def test_evidence(self, *, case_id: str) -> TestEvidence:
        """按 case_id 返回 source-backed test plans / test cases。"""
        ...


class HistoricalKnowledgeRetriever(Protocol):
    """非结构化 historical knowledge 的窄 retrieval port。

    返回 source fragment + provider/retriever 创建的 EvidenceRef；
    不返回模型自行生成的 citation。
    """

    async def retrieve(self, *, query: str, top_k: int = 5) -> list[RetrievedKnowledgeFragment]:
        """检索与 query 相关的 source fragments。"""
        ...


__all__ = [
    "FeatureRiskReviewDataProvider",
    "FeatureRiskReviewModelPort",
    "HistoricalKnowledgeRetriever",
    "RetrievedKnowledgeFragment",
    "RiskRetrievalQuery",
    "TestEvidence",
]