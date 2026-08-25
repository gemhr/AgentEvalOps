"""构建 WP2 专用、source-preserving 的 Phase4 retrieval corpus artifact。

只使用 WP0/WP1 已冻结的 Kubernetes historical knowledge（``knowledge/sources.json``
指向的 10 个 source）。KEP README 只纳入与 `agent_visible_boundary` 一致的 section
（Summary / Motivation / Goals / Non-Goals / Proposal / Design Details），排除
evaluation-reference section（Risks and Mitigations / Test Plan / Production
Readiness / Upgrade / Downgrade / Version Skew / Graduation Criteria / Implementation
History / Drawbacks / Alternatives 等），从而不泄漏 evaluation reference。

产出：``retrieval/phase4_retrieval_corpus.v1.json``。不访问外网，不修改 raw/ 与
cases/，不创建 embedding / vector DB。
"""

# ruff: noqa: D103,D415

from __future__ import annotations

import json
import re
from pathlib import Path

_AGENT_VISIBLE_TOP_LEVEL = frozenset({"Summary", "Motivation", "Goals", "Non-Goals", "Proposal", "Design Details"})
_PROTECTED_PHRASES = [
    "Risks and Mitigations",
    "Test Plan",
    "Production Readiness",
    "Upgrade / Downgrade",
    "Version Skew",
    "Graduation Criteria",
    "Implementation History",
    "Drawbacks",
    "Alternatives",
    "Table of Contents",
    "Release Signoff Checklist",
]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

_MAX_CHUNK_CHARS = 3500


def _slug(value: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return lowered or "section"


def _is_protected_heading(heading: str) -> bool:
    """Heading 的任意 protected 段名（按子串）命中即视为 evaluation-reference。"""
    normalized = re.sub(r"\s+", " ", heading)
    return any(phrase in normalized for phrase in _PROTECTED_PHRASES)


def _split_agent_visible_sections(readme: str) -> list[tuple[str, str]]:
    """把 KEP README 切成 agent-visible 的 (heading, body) 段。

    只在 `##` / `###` 级别起新 chunk；更深的标题折叠进当前 chunk 的正文。
    `###` 级别同时受 protected-heading 过滤（例如 Design Details 下的
    "Risks and Mitigations" / "Test Plan" 属于 evaluation reference，须排除）。
    """
    lines = readme.splitlines()
    current_top: str | None = None
    current_allowed = False
    current_heading = "Summary"
    current_body: list[str] = []
    sections: list[tuple[str, str]] = []

    def flush() -> None:
        if current_allowed:
            body = "\n".join(current_body).strip()
            if body:
                sections.append((current_heading, body))

    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            heading = match.group(2).strip()
            if level <= 2:
                flush()
                current_top = heading
                current_allowed = heading in _AGENT_VISIBLE_TOP_LEVEL
                current_heading = heading
                current_body = []
            elif level == 3:
                flush()
                current_heading = f"{current_top} / {heading}" if current_top else heading
                current_allowed = (
                    current_top in _AGENT_VISIBLE_TOP_LEVEL and not _is_protected_heading(heading)
                )
                current_body = []
            else:
                if current_allowed:
                    current_body.append(line)
            continue
        if current_allowed:
            current_body.append(line)
    flush()
    return sections


def _split_paragraphs(text: str, max_chars: int = _MAX_CHUNK_CHARS) -> list[str]:
    """把过长 section 按段落切成多个 chunk（保留 source 原文）。"""
    if len(text) <= max_chars:
        return [text]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + 1 + len(paragraph) > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def _evidence(
    *, evidence_id: str, source_type: str, source_id: str, source_path: str, source_url: str, section: str
) -> dict[str, str]:
    return {
        "evidence_id": evidence_id,
        "source_type": source_type,
        "source_id": source_id,
        "source_path": source_path,
        "source_url": source_url,
        "section": section,
    }


def _kep_id(source_id: str) -> str:
    return source_id.split("-", 1)[1]


def _enrichment_chunks(root: Path) -> list[dict[str, object]]:
    """读取 WP2 scoped enrichment manifest 并生成 source-preserving chunks。"""
    manifest_path = root / "retrieval" / "enrichment" / "manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    chunks: list[dict[str, object]] = []
    for case_id, entries in manifest["cases"].items():
        for entry in entries:
            snapshot_path = root / str(entry["path"])
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            body = str(payload.get("body", "")).strip()
            title = str(payload.get("title", "")).strip()
            text = f"TITLE: {title}\n\nBODY: {body}".strip()
            if not text:
                continue
            issue_number = str(entry["issue_number"])
            chunks.append(
                {
                    "chunk_id": f"{case_id}:historical:k8s-k8s:{issue_number}",
                    "text": text,
                    "evidence_ref": _evidence(
                        evidence_id=f"{case_id}:historical:k8s-k8s:{issue_number}",
                        source_type="kubernetes_issue_snapshot",
                        source_id=issue_number,
                        source_path=str(entry["path"]),
                        source_url=str(entry["url"]),
                        section="issue_body",
                    ),
                }
            )
    return chunks


def build(root: Path) -> Path:
    """构建 retrieval corpus artifact，返回 artifact 路径。"""
    sources = json.loads((root / "knowledge" / "sources.json").read_text(encoding="utf-8"))
    assert isinstance(sources, list)
    chunks: list[dict[str, object]] = []
    for source in sources:
        case_id = str(source["case_id"])
        source_id = str(source["source_id"])
        kep_id = _kep_id(source_id)
        source_url = str(source["source_url"])
        source_path = str(source["path"])
        if source["source_type"] == "github_enhancement_tracking_issue":
            payload = json.loads((root / source_path).read_text(encoding="utf-8"))
            body = str(payload.get("body", "")).strip()
            title = str(payload.get("title", "")).strip()
            text = f"TITLE: {title}\n\nBODY: {body}".strip()
            chunks.append(
                {
                    "chunk_id": f"{case_id}:issue:{kep_id}",
                    "text": text,
                    "evidence_ref": _evidence(
                        evidence_id=f"{case_id}:issue:{kep_id}",
                        source_type="github_enhancement_tracking_issue",
                        source_id=kep_id,
                        source_path=source_path,
                        source_url=source_url,
                        section="issue_body",
                    ),
                }
            )
            continue
        if source["source_type"] != "kubernetes_enhancement_proposal":
            raise AssertionError(f"unsupported source_type: {source['source_type']}")
        # KEP 内容从 WP0 已冻结的 agent-visible projection（cases/<case>/feature.md）读取，
        # 保证 corpus 边界与 evaluation reference 完全一致；evidence 仍指向 raw README。
        feature_md = root / "cases" / case_id / "feature.md"
        if not feature_md.is_file():
            raise AssertionError(f"missing agent-visible feature projection: {feature_md}")
        readme = feature_md.read_text(encoding="utf-8")
        sections = _split_agent_visible_sections(readme)
        for heading, body in sections:
            if _is_protected_heading(heading):
                raise AssertionError(f"protected heading leaked into corpus: {case_id} {heading}")
            slug = _slug(heading)
            parts = _split_paragraphs(body)
            for part_index, part in enumerate(parts):
                suffix = f"-p{part_index + 1}" if len(parts) > 1 else ""
                chunk_id = f"{case_id}:kep-{slug}{suffix}"
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "text": part,
                        "evidence_ref": _evidence(
                            evidence_id=chunk_id,
                            source_type="kubernetes_enhancement_proposal",
                            source_id=kep_id,
                            source_path=source_path,
                            source_url=source_url,
                            section=heading,
                        ),
                    }
                )
    chunks.sort(key=lambda item: str(item["chunk_id"]))
    chunks.extend(_enrichment_chunks(root))
    chunks.sort(key=lambda item: str(item["chunk_id"]))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    corpus = {
        "schema_version": "feature-risk-review-retrieval-corpus.v1",
        "corpus_id": "kubernetes-feature-risk-review-phase4.v1",
        "source_project": str(manifest["source_project"]),
        "source_commit": str(manifest["source_commit"]),
        "agent_visible_boundary": "Summary, Motivation, Goals, Non-Goals, Proposal, Design Details only; "
        "plus WP2 scoped kubernetes/kubernetes issue snapshots (real historical bug reports)",
        "chunks": chunks,
    }
    artifact = root / "retrieval" / "phase4_retrieval_corpus.v1.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return artifact


if __name__ == "__main__":
    build(Path(__file__).resolve().parents[1] / "evaluation_assets" / "feature_risk_review_v1")
    print("PASS: WP2 phase4 retrieval corpus artifact written")