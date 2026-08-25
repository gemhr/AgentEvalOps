"""WP2 具体业务 workflow：DocumentAnalysis -> (RiskRetrieval || TestReview) -> join。

不是通用 workflow framework。冻结的 failure semantics：

- DocumentAnalysis 失败 -> workflow FAILED，risk/test = NOT_STARTED。
- RiskRetrieval 失败、TestReview 成功 -> PARTIAL，保留 document + test result。
- TestReview 失败、RiskRetrieval 成功 -> PARTIAL，保留 document + risk result。
- 两个 branch 都失败 -> FAILED，保留 document + 两个 branch failure。

并行原语冻结为 ``asyncio.gather(..., return_exceptions=True)``：一支失败不会取消
sibling，成功 sibling 的结果被保留。每个 exception 被转换为轻量 ``BranchFailure``，
不携带 traceback / raw prompt / raw model response / secret。
"""

# ruff: noqa: D415

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import Field, StrictStr, model_validator

from app.core.feature_risk_review.agents import (
    DocumentAnalysisAgent,
    DocumentAnalysisResult,
    RiskRetrievalAgent,
    RiskRetrievalResult,
    TestReviewAgent,
    TestReviewResult,
)
from app.core.feature_risk_review.contracts import FeatureRiskReviewCase, _Contract
from app.core.feature_risk_review.ports import (
    FeatureRiskReviewDataProvider,
    FeatureRiskReviewModelPort,
    HistoricalKnowledgeRetriever,
)

T = TypeVar("T")

_MESSAGE_MAX_LENGTH = 300


class BranchStatus(StrEnum):
    """分支状态：SUCCESS / FAILED / NOT_STARTED。"""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    NOT_STARTED = "NOT_STARTED"


class WorkflowStatus(StrEnum):
    """Workflow 状态：SUCCESS / PARTIAL / FAILED。"""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class BranchFailure(_Contract):
    """分支失败的安全摘要；message 不携带 stack / raw prompt / raw response。"""

    branch: StrictStr = Field(min_length=1)
    error_type: StrictStr = Field(min_length=1)
    message: StrictStr = Field(min_length=1)
    recoverable: bool = False


class BranchResult(_Contract, Generic[T]):
    """唯一通用 branch wrapper：SUCCESS 带 value，FAILED 带 failure，NOT_STARTED 两者皆无。"""

    branch: StrictStr = Field(min_length=1)
    status: BranchStatus
    value: T | None = None
    failure: BranchFailure | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "BranchResult[T]":
        if self.status == BranchStatus.SUCCESS and (self.value is None or self.failure is not None):
            raise ValueError("SUCCESS branch must carry a value and no failure")
        if self.status == BranchStatus.FAILED and (self.failure is None or self.value is not None):
            raise ValueError("FAILED branch must carry a failure and no value")
        if self.status == BranchStatus.NOT_STARTED and (self.value is not None or self.failure is not None):
            raise ValueError("NOT_STARTED branch must carry no value and no failure")
        return self


class FeatureRiskReviewWorkflowResult(_Contract):
    """join 后的结构化 workflow 结果；保留每个 branch 的 value 或 failure。"""

    case_id: StrictStr = Field(min_length=1)
    workflow_status: WorkflowStatus
    document_analysis: BranchResult[DocumentAnalysisResult]
    risk_retrieval: BranchResult[RiskRetrievalResult]
    test_review: BranchResult[TestReviewResult]


class FeatureRiskReviewWorkflow:
    """FeatureRiskReviewCase 到 FeatureRiskReviewWorkflowResult 的具体业务 workflow。"""

    def __init__(
        self,
        *,
        model_port: FeatureRiskReviewModelPort,
        data_provider: FeatureRiskReviewDataProvider,
        retriever: HistoricalKnowledgeRetriever,
        top_k: int = 5,
    ) -> None:
        self._document_agent = DocumentAnalysisAgent(model_port)
        self._risk_agent = RiskRetrievalAgent(
            model_port=model_port,
            data_provider=data_provider,
            retriever=retriever,
            top_k=top_k,
        )
        self._test_agent = TestReviewAgent(model_port=model_port, data_provider=data_provider)

    async def run(self, case: FeatureRiskReviewCase) -> FeatureRiskReviewWorkflowResult:
        """执行 DocumentAnalysis -> (RiskRetrieval || TestReview) -> join 的业务流程。"""
        case_id = case.feature_document.case_id
        try:
            document_result = await self._document_agent.analyze(case.feature_document)
        except Exception as exc:  # noqa: BLE001 - workflow 边界负责把任意分支异常转为 BranchFailure
            return FeatureRiskReviewWorkflowResult(
                case_id=case_id,
                workflow_status=WorkflowStatus.FAILED,
                document_analysis=BranchResult[DocumentAnalysisResult](
                    branch="document_analysis",
                    status=BranchStatus.FAILED,
                    failure=_to_branch_failure("document_analysis", exc),
                ),
                risk_retrieval=_not_started("risk_retrieval"),
                test_review=_not_started("test_review"),
            )

        risk_coro = self._risk_agent.review(document_result)
        test_coro = self._test_agent.review(document_result)
        risk_raw, test_raw = await asyncio.gather(risk_coro, test_coro, return_exceptions=True)

        risk_branch = _finalize("risk_retrieval", risk_raw, RiskRetrievalResult)
        test_branch = _finalize("test_review", test_raw, TestReviewResult)
        status = _workflow_status(risk_branch.status, test_branch.status)

        return FeatureRiskReviewWorkflowResult(
            case_id=case_id,
            workflow_status=status,
            document_analysis=BranchResult[DocumentAnalysisResult](
                branch="document_analysis",
                status=BranchStatus.SUCCESS,
                value=document_result,
            ),
            risk_retrieval=risk_branch,
            test_review=test_branch,
        )


def _not_started(branch: str) -> BranchResult[object]:
    return BranchResult[object](branch=branch, status=BranchStatus.NOT_STARTED)


def _finalize(branch: str, raw: object, value_type: type[T]) -> BranchResult[T]:
    if isinstance(raw, Exception):
        return BranchResult[T](
            branch=branch, status=BranchStatus.FAILED, failure=_to_branch_failure(branch, raw)
        )
    return BranchResult[T](branch=branch, status=BranchStatus.SUCCESS, value=raw)  # type: ignore[arg-type]


def _workflow_status(risk: BranchStatus, test: BranchStatus) -> WorkflowStatus:
    if risk == BranchStatus.SUCCESS and test == BranchStatus.SUCCESS:
        return WorkflowStatus.SUCCESS
    if risk == BranchStatus.FAILED and test == BranchStatus.FAILED:
        return WorkflowStatus.FAILED
    return WorkflowStatus.PARTIAL


def _to_branch_failure(branch: str, exc: Exception) -> BranchFailure:
    message = str(exc).strip() or exc.__class__.__name__
    message = " ".join(message.split())[:_MESSAGE_MAX_LENGTH]
    return BranchFailure(
        branch=branch,
        error_type=type(exc).__name__,
        message=message,
        recoverable=False,
    )


__all__ = [
    "BranchFailure",
    "BranchResult",
    "BranchStatus",
    "FeatureRiskReviewWorkflow",
    "FeatureRiskReviewWorkflowResult",
    "WorkflowStatus",
]