from __future__ import annotations

import json
import shutil
import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.feature_risk_review import (
    AnnotationStatus,
    EvaluationAnnotation,
    FeatureDocument,
    FeatureRiskDatasetLoadError,
    load_evaluation_annotations,
    load_feature_risk_review_cases,
)


ASSET_ROOT = Path(__file__).resolve().parents[2] / "evaluation_assets" / "feature_risk_review_v1"
EXPECTED_CASE_IDS = {"k8s_541", "k8s_753", "k8s_1287", "k8s_1472", "k8s_1602"}
BUILDER = Path(__file__).resolve().parents[2] / "scripts" / "build_feature_risk_review_projection.py"


def test_loads_all_frozen_cases_with_real_issue_and_plan_evidence() -> None:
    cases = load_feature_risk_review_cases(ASSET_ROOT)

    assert {case.feature_document.case_id for case in cases} == EXPECTED_CASE_IDS
    for case in cases:
        assert case.historical_issues[0].severity is None
        assert case.test_plans
        assert case.test_cases == []
        for ref in (case.feature_document.source, case.historical_issues[0].evidence_ref, case.test_plans[0].evidence_ref):
            assert (ASSET_ROOT / ref.source_path).is_file()
            assert str(ref.source_url).startswith("https://github.com/kubernetes/")


def test_feature_document_requires_typed_visible_content_and_evidence() -> None:
    with pytest.raises(ValidationError, match="agent_visible_content"):
        FeatureDocument.model_validate(
            {"case_id": "case", "feature_id": "541", "title": "title", "agent_visible_content": "", "source": {}}
        )


def test_annotations_are_explicit_and_pending_or_human_reviewed() -> None:
    annotations = load_evaluation_annotations(ASSET_ROOT)
    assert {annotation.case_id for annotation in annotations} == EXPECTED_CASE_IDS
    assert {annotation.annotation_status for annotation in annotations} == {AnnotationStatus.PENDING}
    reviewed = EvaluationAnnotation.model_validate(
        {"case_id": "case", "annotation_status": "HUMAN_REVIEWED", "annotation_source": "human_curated"}
    )
    assert reviewed.annotation_status == AnnotationStatus.HUMAN_REVIEWED


def test_runtime_case_loader_does_not_need_or_return_annotations(tmp_path: Path) -> None:
    copied = tmp_path / "dataset"
    shutil.copytree(ASSET_ROOT, copied)
    shutil.rmtree(copied / "annotations")

    case = load_feature_risk_review_cases(copied)[0]

    dumped = case.model_dump(mode="json")
    assert "evaluation_reference" not in json.dumps(dumped)
    assert "expected_change_points" not in dumped


def test_projection_rebuild_preserves_human_reviewed_annotations(tmp_path: Path) -> None:
    copied = tmp_path / "dataset"
    shutil.copytree(ASSET_ROOT, copied)
    annotation_path = copied / "annotations" / "annotations.v1.json"
    annotation_payload = json.loads(annotation_path.read_text(encoding="utf-8"))
    annotation_payload["annotations"][0]["annotation_status"] = "HUMAN_REVIEWED"
    annotation_payload["annotations"][0]["expected_risk_level"] = "HIGH"
    annotation_path.write_text(json.dumps(annotation_payload), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("feature_risk_projection_builder", BUILDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.build(copied)

    rebuilt = json.loads(annotation_path.read_text(encoding="utf-8"))
    assert rebuilt["annotations"][0]["annotation_status"] == "HUMAN_REVIEWED"


def test_malformed_normalized_fixture_fails_clearly(tmp_path: Path) -> None:
    copied = tmp_path / "dataset"
    shutil.copytree(ASSET_ROOT, copied)
    normalized = copied / "normalized" / "cases.v1.json"
    payload = json.loads(normalized.read_text(encoding="utf-8"))
    payload["cases"][0]["feature_document"]["source"]["source_url"] = "not-a-url"
    normalized.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FeatureRiskDatasetLoadError, match="invalid normalized case"):
        load_feature_risk_review_cases(copied)
