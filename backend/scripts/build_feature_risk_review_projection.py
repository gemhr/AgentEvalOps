"""从 WP0 冻结快照构建 WP1 normalized business projection。"""

# ruff: noqa: D103,D415

from __future__ import annotations

import json
from pathlib import Path


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def evidence(
    *, evidence_id: str, source_type: str, source_id: str, source_path: str, source_url: str, section: str | None = None
) -> dict[str, str]:
    result = {
        "evidence_id": evidence_id,
        "source_type": source_type,
        "source_id": source_id,
        "source_path": source_path,
        "source_url": source_url,
    }
    if section:
        result["section"] = section
    return result


def build(root: Path) -> None:
    """构建，不修改 raw 文件或 case 内的 evaluation reference/template。"""
    manifest = read_json(root / "manifest.json")
    assert isinstance(manifest, dict)
    issues = read_json(root / "issues" / "historical_issues.json")
    plans = read_json(root / "tests" / "test_plans.json")
    assert isinstance(issues, list) and isinstance(plans, list)
    issues_by_id = {str(item["issue_id"]): item for item in issues if isinstance(item, dict)}
    plans_by_case = {str(item["case_id"]): item for item in plans if isinstance(item, dict)}
    cases: list[dict[str, object]] = []
    annotations: list[dict[str, object]] = []

    for entry in manifest["cases"]:
        assert isinstance(entry, dict)
        case_id, kep_id = str(entry["case_id"]), str(entry["kep_id"])
        case_root = root / "cases" / case_id
        metadata = read_json(case_root / "metadata.json")
        assert isinstance(metadata, dict)
        issue, plan = issues_by_id[kep_id], plans_by_case[case_id]
        feature_source = evidence(
            evidence_id=f"{case_id}:feature", source_type=str(metadata["source_type"]), source_id=kep_id,
            source_path=f"raw/kubernetes_enhancements/{case_id}/README.md", source_url=str(metadata["source_url"]),
            section="agent_visible_feature_document",
        )
        issue_source = evidence(
            evidence_id=f"{case_id}:issue:{kep_id}", source_type=str(issue["source_type"]), source_id=kep_id,
            source_path=f"raw/github_issues/enhancements_{kep_id}.json", source_url=str(issue["source_url"]),
        )
        plan_source = evidence(
            evidence_id=f"{case_id}:test-plan", source_type=str(plan["source_type"]), source_id=kep_id,
            source_path="tests/test_plans.json", source_url=str(plan["source_url"]), section=f"case_id={case_id}",
        )
        cases.append({
            "feature_document": {
                "case_id": case_id, "feature_id": kep_id, "title": metadata["title"],
                "agent_visible_content": (case_root / "feature.md").read_text(encoding="utf-8"), "source": feature_source,
            },
            "historical_issues": [{
                "issue_id": kep_id, "title": issue["title"], "description": issue["body"], "component": issue["component"],
                "labels": issue["labels"], "state": issue["state"], "severity": issue["severity"], "evidence_ref": issue_source,
            }],
            "test_plans": [{"plan_id": f"{case_id}:kep-test-plan", "case_id": case_id, "content": plan["content"], "evidence_ref": plan_source}],
            "test_cases": [],
        })
        annotations.append({
            "case_id": case_id, "annotation_status": "PENDING", "expected_change_points": [],
            "expected_components": [], "expected_risk_areas": [], "expected_historical_issue_ids": [],
            "expected_coverage_gaps": [], "expected_risk_level": None, "annotation_source": "human_curated",
        })

    normalized = root / "normalized" / "cases.v1.json"
    annotations_path = root / "annotations" / "annotations.v1.json"
    normalized.parent.mkdir(parents=True, exist_ok=True)
    annotations_path.parent.mkdir(parents=True, exist_ok=True)
    normalized.write_text(json.dumps({"schema_version": "feature-risk-review-cases.v1", "cases": cases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not annotations_path.exists():
        annotations_path.write_text(
            json.dumps({"schema_version": "feature-risk-review-annotations.v1", "annotations": annotations}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    build(Path(__file__).resolve().parents[1] / "evaluation_assets" / "feature_risk_review_v1")
