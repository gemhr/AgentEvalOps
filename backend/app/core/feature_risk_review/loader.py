"""加载 WP1 的归一化投影；评价标注只能通过显式入口读取。"""

# ruff: noqa: D415

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.core.feature_risk_review.contracts import EvaluationAnnotation, FeatureRiskReviewCase


class FeatureRiskDatasetLoadError(ValueError):
    """Feature Risk Review 离线数据无法读取或违反其 contract。"""


def _read_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeatureRiskDatasetLoadError(f"unable to load dataset file: {path}") from exc
    if not isinstance(payload, dict):
        raise FeatureRiskDatasetLoadError(f"dataset file must contain a JSON object: {path}")
    return payload


def load_feature_risk_review_cases(root: Path) -> list[FeatureRiskReviewCase]:
    """加载正常 demo path 所需数据，故意不读取 annotations 目录。"""
    payload = _read_object(root / "normalized" / "cases.v1.json")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise FeatureRiskDatasetLoadError("normalized dataset must contain non-empty cases")
    try:
        loaded = [FeatureRiskReviewCase.model_validate(case) for case in cases]
    except ValidationError as exc:
        raise FeatureRiskDatasetLoadError(f"invalid normalized case: {exc}") from exc
    case_ids = [case.feature_document.case_id for case in loaded]
    if len(case_ids) != len(set(case_ids)):
        raise FeatureRiskDatasetLoadError("duplicate case_id in normalized dataset")
    return loaded


def load_evaluation_annotations(root: Path) -> list[EvaluationAnnotation]:
    """显式加载 evaluation-only PENDING/HUMAN_REVIEWED annotations。"""
    payload = _read_object(root / "annotations" / "annotations.v1.json")
    annotations = payload.get("annotations")
    if not isinstance(annotations, list):
        raise FeatureRiskDatasetLoadError("annotation dataset must contain annotations list")
    try:
        return [EvaluationAnnotation.model_validate(annotation) for annotation in annotations]
    except ValidationError as exc:
        raise FeatureRiskDatasetLoadError(f"invalid evaluation annotation: {exc}") from exc
