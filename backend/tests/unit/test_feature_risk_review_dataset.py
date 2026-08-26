from __future__ import annotations

import importlib.util
import json
import re
import shutil
from pathlib import Path

from app.core.feature_risk_review import load_evaluation_annotations


ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = ROOT / "evaluation_assets" / "feature_risk_review_v1"
SCRIPT = ROOT / "scripts" / "validate_feature_risk_review_dataset.py"
SPEC = importlib.util.spec_from_file_location("feature_risk_dataset_validator", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_frozen_kubernetes_feature_risk_review_dataset_is_valid() -> None:
    assert MODULE.validate(ROOT / "evaluation_assets" / "feature_risk_review_v1") == []


def test_agent_visible_documents_exclude_evaluation_reference_headings() -> None:
    protected = re.compile(
        r"^#{2,6} .*?(Risks and Mitigations|Test Plan|Production Readiness|Upgrade / Downgrade|Version Skew|Graduation Criteria|Implementation History)",
        re.MULTILINE,
    )
    for feature in (ROOT / "evaluation_assets" / "feature_risk_review_v1" / "cases").glob("*/feature.md"):
        assert not protected.search(feature.read_text(encoding="utf-8")), feature


def test_projection_rebuild_preserves_human_reviewed_annotations_and_field_status(tmp_path: Path) -> None:
    """WP1/WP4 直接回归：dataset rebuild 不得覆盖 HUMAN_REVIEWED annotation.

    annotations.v1.json 与 field_status.v1.json 都不得被 rebuild 改写（当前 builder
    仅在 annotation 文件不存在时创建 PENDING template）。
    """
    copied = tmp_path / "dataset"
    shutil.copytree(ASSET_ROOT, copied)
    annotation_path = copied / "annotations" / "annotations.v1.json"
    field_status_path = copied / "annotations" / "field_status.v1.json"
    field_status_bytes = field_status_path.read_bytes()

    payload = json.loads(annotation_path.read_text(encoding="utf-8"))
    payload["annotations"][0]["annotation_status"] = "HUMAN_REVIEWED"
    payload["annotations"][0]["expected_risk_level"] = "HIGH"
    payload["annotations"][0]["annotation_source"] = "raw KEP README and evaluation reference"
    annotation_path.write_text(json.dumps(payload), encoding="utf-8")

    spec = importlib.util.spec_from_file_location("feature_risk_projection_builder", ROOT / "scripts" / "build_feature_risk_review_projection.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build(copied)

    rebuilt = json.loads(annotation_path.read_text(encoding="utf-8"))
    assert rebuilt["annotations"][0]["annotation_status"] == "HUMAN_REVIEWED"
    assert rebuilt["annotations"][0]["expected_risk_level"] == "HIGH"

    loaded = load_evaluation_annotations(copied)
    assert {a.annotation_status.value for a in loaded} == {"HUMAN_REVIEWED"}
    assert field_status_path.read_bytes() == field_status_bytes
