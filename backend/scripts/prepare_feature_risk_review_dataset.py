"""从冻结的 kubernetes/enhancements checkout 准备 Phase4 WP0 小型数据集。"""

# ruff: noqa: D103,D415

from __future__ import annotations

import argparse
import json
import re
import shutil
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


SOURCE_REPOSITORY = "https://github.com/kubernetes/enhancements"
SOURCE_PROJECT = "kubernetes/enhancements"
CASES = (
    ("k8s_541", "keps/sig-auth/541-external-credential-providers", "security / authorization"),
    ("k8s_753", "keps/sig-node/753-sidecar-containers", "lifecycle / state"),
    ("k8s_1287", "keps/sig-node/1287-in-place-update-pod-resources", "lifecycle / state"),
    ("k8s_1472", "keps/sig-storage/1472-storage-capacity-tracking", "storage / scheduling"),
    ("k8s_1602", "keps/sig-instrumentation/1602-structured-logging", "observability / compatibility"),
)
FEATURE_SECTIONS = {"summary", "motivation", "proposal", "design details"}
REFERENCE_MARKERS = (
    "risks and mitigations",
    "test plan",
    "test plans",
    "production readiness",
    "upgrade / downgrade",
    "version skew",
    "graduation criteria",
    "implementation history",
)
HEADING = re.compile(r"^(#{2,6})\s+(.+?)\s*$")


def heading_name(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[`*_]", "", value).strip().lower())


def sections(markdown: str, wanted: set[str] | tuple[str, ...], contains: bool = False) -> str:
    """按 Markdown heading 提取段落，保留原始文字，不制造 Kubernetes 事实。"""
    lines = markdown.splitlines(keepends=True)
    found: list[tuple[int, int, int]] = []
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if not match:
            continue
        level, name = len(match.group(1)), heading_name(match.group(2))
        selected = any(marker in name for marker in wanted) if contains else name in wanted
        if selected:
            found.append((index, level, index + 1))

    blocks: list[tuple[int, str]] = []
    for start, level, content_start in found:
        end = len(lines)
        for index in range(content_start, len(lines)):
            match = HEADING.match(lines[index])
            if match and len(match.group(1)) <= level:
                end = index
                break
        block = "".join(lines[start:end]).strip()
        if block:
            blocks.append((start, block))
    return "\n\n".join(block for _, block in sorted(blocks)).strip() + "\n"


def feature_sections(markdown: str) -> str:
    """提取 feature 输入，同时剔除嵌套在 Proposal 内的评价泄漏段。"""
    lines = markdown.splitlines(keepends=True)
    output: list[str] = []
    active_level: int | None = None
    excluded_level: int | None = None
    for line in lines:
        match = HEADING.match(line)
        if match:
            level, name = len(match.group(1)), heading_name(match.group(2))
            if level == 2:
                active_level = level if name in FEATURE_SECTIONS else None
                excluded_level = None
            elif active_level is not None and excluded_level is not None and level <= excluded_level:
                excluded_level = None
            if active_level is not None and any(marker in name for marker in REFERENCE_MARKERS):
                excluded_level = level
        if active_level is not None and excluded_level is None:
            output.append(line)
    return "".join(output).strip() + "\n"


def yaml_scalar(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*[\"']?(.+?)[\"']?\s*$", text, re.MULTILINE)
    if not match:
        raise ValueError(f"kep.yaml missing {key}")
    return match.group(1).strip().strip('"').strip("'")


def github_issue(issue_number: str) -> dict[str, object]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/kubernetes/enhancements/issues/{issue_number}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "AgentEvalOps-WP0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310: fixed public GitHub API URL
        return json.load(response)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare(source_root: Path, output_root: Path, source_commit: str, retrieved_at: str) -> None:
    """将 5 个冻结 KEP 与其 enhancement tracking issue 投影为离线可用数据。"""
    if output_root.exists():
        shutil.rmtree(output_root)
    raw_root, cases_root = output_root / "raw" / "kubernetes_enhancements", output_root / "cases"
    issue_records: list[dict[str, object]] = []
    knowledge_sources: list[dict[str, object]] = []
    test_plans: list[dict[str, object]] = []
    manifest_cases: list[dict[str, object]] = []

    for case_id, relative_dir, risk_domain in CASES:
        source_dir = source_root / relative_dir
        readme = source_dir / "README.md"
        yaml_file = source_dir / "kep.yaml"
        markdown, yaml_text = readme.read_text(encoding="utf-8"), yaml_file.read_text(encoding="utf-8")
        kep_id, title, owning_sig = (
            yaml_scalar(yaml_text, "kep-number"),
            yaml_scalar(yaml_text, "title"),
            yaml_scalar(yaml_text, "owning-sig"),
        )
        raw_case_dir, case_dir = raw_root / case_id, cases_root / case_id
        raw_case_dir.mkdir(parents=True, exist_ok=True)
        case_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(readme, raw_case_dir / "README.md")
        shutil.copy2(yaml_file, raw_case_dir / "kep.yaml")

        feature = feature_sections(markdown)
        reference = sections(markdown, REFERENCE_MARKERS, contains=True)
        test_plan = sections(markdown, ("test plan", "test plans"), contains=True)
        if not feature or not reference or not test_plan:
            raise ValueError(f"{case_id} lacks required feature/reference/test-plan sections")
        source_path = f"{relative_dir}/README.md"
        source_url = f"{SOURCE_REPOSITORY}/blob/{source_commit}/{source_path}"
        metadata = {
            "case_id": case_id,
            "kep_id": kep_id,
            "title": title,
            "owning_sig": owning_sig,
            "risk_domain": risk_domain,
            "source_project": SOURCE_PROJECT,
            "source_repository": SOURCE_REPOSITORY,
            "source_commit": source_commit,
            "source_path": source_path,
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "source_type": "kubernetes_enhancement_proposal",
            "agent_visible_boundary": "Summary, Motivation, Proposal, Design Details only",
            "evaluation_reference_boundary": "Risk, test, readiness, upgrade/version-skew, graduation and implementation-history sections",
            "human_review_required": True,
        }
        (case_dir / "feature.md").write_text(feature, encoding="utf-8")
        (case_dir / "evaluation_reference.md").write_text(reference, encoding="utf-8")
        write_json(case_dir / "metadata.json", metadata)
        write_json(case_dir / "source_manifest.json", {"raw_files": [
            {"source_path": source_path, "saved_as": f"raw/kubernetes_enhancements/{case_id}/README.md", "source_url": source_url},
            {"source_path": f"{relative_dir}/kep.yaml", "saved_as": f"raw/kubernetes_enhancements/{case_id}/kep.yaml", "source_url": f"{SOURCE_REPOSITORY}/blob/{source_commit}/{relative_dir}/kep.yaml"},
        ], "source_commit": source_commit, "retrieved_at": retrieved_at})
        write_json(case_dir / "annotation_template.json", {
            "case_id": case_id, "expected_change_points": [], "expected_components": [],
            "expected_risk_areas": [], "expected_historical_issue_ids": [], "expected_coverage_gaps": [],
            "expected_risk_level": None, "annotation_source": "human_curated", "human_review_required": True,
        })
        test_plans.append({"case_id": case_id, "kep_id": kep_id, "source_type": "kep_test_plan", "source_url": source_url, "content": test_plan})

        issue = github_issue(kep_id)
        raw_issue_path = output_root / "raw" / "github_issues" / f"enhancements_{kep_id}.json"
        write_json(raw_issue_path, issue)
        issue_records.append({
            "issue_id": str(issue["number"]), "repository": "kubernetes/enhancements", "title": issue["title"],
            "body": issue.get("body") or "", "labels": [label["name"] for label in issue.get("labels", [])],
            "state": issue["state"], "component": owning_sig, "severity": None,
            "source_url": issue["html_url"], "source_type": "github_enhancement_tracking_issue",
            "source_repository": "https://github.com/kubernetes/enhancements", "retrieved_at": retrieved_at,
            "evidence_note": "真实 enhancement tracking issue；不是经过人工归类的生产缺陷或官方 severity。",
        })
        knowledge_sources.extend((
            {"source_id": f"kep-{kep_id}", "case_id": case_id, "source_type": "kubernetes_enhancement_proposal", "path": f"raw/kubernetes_enhancements/{case_id}/README.md", "source_url": source_url},
            {"source_id": f"issue-{kep_id}", "case_id": case_id, "source_type": "github_enhancement_tracking_issue", "path": f"raw/github_issues/enhancements_{kep_id}.json", "source_url": issue["html_url"]},
        ))
        manifest_cases.append({"case_id": case_id, "kep_id": kep_id, "title": title, "source_path": source_path})

    write_json(output_root / "issues" / "historical_issues.json", issue_records)
    write_json(output_root / "knowledge" / "sources.json", knowledge_sources)
    write_json(output_root / "tests" / "test_plans.json", test_plans)
    write_json(output_root / "tests" / "test_cases.json", {"mapping_status": "PARTIAL", "records": [], "note": "仅冻结真实 KEP Test Plan；未将测试计划伪装为真实测试函数映射。"})
    write_json(output_root / "manifest.json", {
        "dataset_id": "kubernetes-feature-risk-review.v1", "dataset_type": "real_source_offline_snapshot",
        "source_project": SOURCE_PROJECT, "source_repository": SOURCE_REPOSITORY, "source_commit": source_commit,
        "retrieved_at": retrieved_at, "case_count": len(manifest_cases), "cases": manifest_cases,
        "truth_boundary": {"kubernetes_feature_documents": "REAL", "historical_issue_source": "REAL", "test_plan_source": "REAL", "ground_truth": "PENDING", "rag_index_built": "NO", "real_agent_execution": "NO"},
    })
    (output_root / "README.md").write_text(
        "# Kubernetes Feature Risk Review Dataset v1\n\n"
        "离线真实来源数据集；详情见 manifest、raw、cases、normalized 和 annotations。\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--retrieved-at", default=datetime.now(UTC).replace(microsecond=0).isoformat())
    args = parser.parse_args()
    prepare(args.source_root, args.output_root, args.source_commit, args.retrieved_at)


if __name__ == "__main__":
    main()
