"""WP2 retrieval usefulness spike。

评估当前 WP0/WP1 corpus 对 5 个 case 的 retrieval usefulness：
每个 case 以 title + feature 关键内容构造查询，检查 top-k 是否返回
有风险价值的 historical evidence（不只是该 case 自身的 tracking issue）。
本脚本不访问外网、不改数据，只输出评估结果供 Handoff 记录。
"""

# ruff: noqa: D103,D415,E402

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.feature_risk_review.retrieval import SourcePreservingLexicalRetriever
from app.core.feature_risk_review import load_feature_risk_review_cases

STOP_BOUNDARY = re.compile(
    r"(^#{2,6} .*?(Risks and Mitigations|Test Plan|Production Readiness|Upgrade / Downgrade|"
    r"Version Skew|Graduation Criteria|Implementation History))",
    re.MULTILINE,
)


def build_query(case, max_chars: int = 600) -> str:
    content = case.feature_document.agent_visible_content
    cut = STOP_BOUNDARY.search(content)
    if cut:
        content = content[: cut.start()]
    content = re.sub(r"^#{1,6}\s+", "", content, flags=re.MULTILINE)
    content = re.sub(r"\s+", " ", content).strip()
    return f"{case.feature_document.title}. {content[:max_chars]}"


async def spike(root: Path, top_k: int = 5) -> dict[str, object]:
    retriever = SourcePreservingLexicalRetriever(path=root / "retrieval" / "phase4_retrieval_corpus.v1.json")
    cases = load_feature_risk_review_cases(root)
    case_results: dict[str, object] = {}
    meaningful = 0
    for case in sorted(cases, key=lambda item: item.feature_document.case_id):
        query = build_query(case)
        hits = await retriever.retrieve(query=query, top_k=top_k)
        case_id = case.feature_document.case_id
        issue_id = case.historical_issues[0].issue_id
        entries = []
        cross_case = 0
        own_issue_only = True
        for hit in hits:
            entries.append(
                {
                    "chunk_id": hit.evidence_ref.evidence_id,
                    "source_id": hit.evidence_ref.source_id,
                    "section": hit.evidence_ref.section,
                    "source_type": hit.evidence_ref.source_type,
                    "score": hit.relevance_score,
                    "fragment_preview": hit.source_fragment[:160].replace("\n", " "),
                }
            )
            if hit.evidence_ref.source_id != issue_id:
                cross_case += 1
            if hit.evidence_ref.source_id != issue_id or hit.evidence_ref.source_type != "github_enhancement_tracking_issue":
                own_issue_only = False
        has_issue = any(entry["source_type"] == "github_enhancement_tracking_issue" for entry in entries)
        has_risk_value = cross_case > 0 or (has_issue and any(e["source_type"] == "kubernetes_enhancement_proposal" for e in entries))
        if has_risk_value:
            meaningful += 1
        case_results[case_id] = {
            "top_hits": entries,
            "cross_case_hits": cross_case,
            "own_issue_only": own_issue_only,
            "retrieval_has_risk_value": has_risk_value,
        }
    return {
        "top_k": top_k,
        "cases_evaluated": len(cases),
        "meaningful_evidence_cases": meaningful,
        "sufficient_3_of_5": meaningful >= 3,
        "case_results": case_results,
    }


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "evaluation_assets" / "feature_risk_review_v1"
    report = asyncio.run(spike(root))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["sufficient_3_of_5"]:
        sys.exit(2)


if __name__ == "__main__":
    main()