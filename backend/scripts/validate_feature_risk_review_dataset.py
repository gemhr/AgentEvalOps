"""Phase4 WP0 数据集的最小完整性校验。"""

# ruff: noqa: D103,D415

from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate(root: Path) -> list[str]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    case_ids = [case["case_id"] for case in manifest["cases"]]
    if len(case_ids) != len(set(case_ids)):
        errors.append("duplicate case_id in manifest")
    issues = json.loads((root / "issues" / "historical_issues.json").read_text(encoding="utf-8"))
    issue_ids = {record["issue_id"] for record in issues}
    if not issue_ids:
        errors.append("no historical issue evidence")
    test_plans = {record["case_id"] for record in json.loads((root / "tests" / "test_plans.json").read_text(encoding="utf-8"))}
    for case_id in case_ids:
        case_dir = root / "cases" / case_id
        for name in ("metadata.json", "feature.md", "evaluation_reference.md", "source_manifest.json", "annotation_template.json"):
            path = case_dir / name
            if not path.is_file() or not path.read_text(encoding="utf-8").strip():
                errors.append(f"{case_id}: missing or empty {name}")
        metadata = json.loads((case_dir / "metadata.json").read_text(encoding="utf-8"))
        if not all(metadata.get(key) for key in ("source_commit", "source_path", "source_url", "retrieved_at", "source_type")):
            errors.append(f"{case_id}: incomplete source metadata")
        if case_id not in test_plans:
            errors.append(f"{case_id}: no test plan")
        if metadata.get("kep_id") not in issue_ids:
            errors.append(f"{case_id}: no matching tracking issue")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    errors = validate(args.root)
    if errors:
        raise SystemExit("\n".join(errors))
    print("PASS: Kubernetes feature risk review dataset is structurally valid")


if __name__ == "__main__":
    main()
