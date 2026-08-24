from __future__ import annotations

import importlib.util
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
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
