"""Feature Risk Review 领域错误。

业务 DTO 与 sibling branch 只携带安全摘要：message 中不包含完整 traceback、
raw prompt 或 raw model response。
"""

# ruff: noqa: D415

from __future__ import annotations


class FeatureRiskReviewError(Exception):
    """Feature Risk Review 领域的基类错误。"""


class FeatureRiskReviewModelOutputError(FeatureRiskReviewError):
    """模型输出无法解析或验证为约定 schema，或模型调用失败。"""


class FeatureRiskReviewEvidenceError(FeatureRiskReviewError):
    """模型引用了 provider/retriever 未提供的证据或 issue 身份。"""


class FeatureRiskReviewDataError(FeatureRiskReviewError):
    """Data provider 无法提供 source-backed 数据。"""


__all__ = [
    "FeatureRiskReviewDataError",
    "FeatureRiskReviewError",
    "FeatureRiskReviewEvidenceError",
    "FeatureRiskReviewModelOutputError",
]