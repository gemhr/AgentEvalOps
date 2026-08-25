"""基于 WP1 normalized dataset 的 source-backed FeatureRiskReviewDataProvider。

只返回 `load_feature_risk_review_cases()` 提供的结构化记录，不推断 severity、
不补造 TestCase、不读取 annotations/expected_*。HistoricalIssue 保持其
enhancement tracking issue 语义，不等于 production incident。
"""

# ruff: noqa: D415

from __future__ import annotations

from pathlib import Path

from app.core.feature_risk_review import (
    FeatureRiskReviewCase,
    HistoricalIssue,
    load_feature_risk_review_cases,
)
from app.core.feature_risk_review.errors import FeatureRiskReviewDataError
from app.core.feature_risk_review.ports import (
    RiskRetrievalQuery,
    TestEvidence,
)

_DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "evaluation_assets" / "feature_risk_review_v1"


class NormalizedFeatureRiskReviewDataProvider:
    """按 case_id 提供 WP1 normalized source-backed 数据。"""

    def __init__(
        self,
        *,
        root: Path | None = None,
        cases: list[FeatureRiskReviewCase] | None = None,
    ) -> None:
        if cases is not None:
            loaded = cases
        else:
            loaded = load_feature_risk_review_cases(root or _DEFAULT_ROOT)
        self._by_case = {case.feature_document.case_id: case for case in loaded}

    def _require(self, case_id: str) -> FeatureRiskReviewCase:
        case = self._by_case.get(case_id)
        if case is None:
            raise FeatureRiskReviewDataError(f"unknown case_id: {case_id}")
        return case

    async def historical_issues(self, *, case_id: str, query_inputs: RiskRetrievalQuery) -> list[HistoricalIssue]:
        """返回该 case 的 source-backed historical issues。"""
        return list(self._require(case_id).historical_issues)

    async def test_evidence(self, *, case_id: str) -> TestEvidence:
        """返回该 case 的 source-backed test plans / test cases。"""
        case = self._require(case_id)
        return TestEvidence(test_plans=list(case.test_plans), test_cases=list(case.test_cases))


__all__ = ["NormalizedFeatureRiskReviewDataProvider"]