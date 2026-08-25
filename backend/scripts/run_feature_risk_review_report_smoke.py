"""WP3 deterministic real-report smoke runner (NO MODEL, NO RETRIEVAL, NO ANNOTATIONS).

只消费冻结的 WP2 typed execution artifact，经正式 core code 生成最终 report artifact：

    JSON
    -> FeatureRiskReviewWorkflowResult.model_validate(...)
    -> FeatureRiskReviewAggregator.aggregate(...)
    -> FeatureRiskReviewReport
    -> render_feature_risk_review_markdown(...)

本脚本不调用 LLM / ModelPort / Agent / Retriever / DataProvider，不读取
annotations / EvaluationAnnotation / expected_* / evaluation_reference。
Risk / Priority / Citation 逻辑全部来自正式 core，不在此复制 Policy。
"""

# ruff: noqa: D103,D415,E402

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.feature_risk_review import (  # noqa: E402
    CoverageState,
    FeatureRiskReviewAggregationFailure,
    FeatureRiskReviewAggregator,
    FeatureRiskReviewReport,
    FeatureRiskReviewWorkflowResult,
    render_feature_risk_review_markdown,
)

ASSET_ROOT = PROJECT_ROOT / "evaluation_assets" / "feature_risk_review_v1"
CASE_ID = "k8s_541"
EXPERIMENT_ID = "wp3_real_report_smoke"
SOURCE_ARTIFACT = ASSET_ROOT / "experiments" / "wp2_real_model_smoke_retry2_k8s_541.json"
JSON_ARTIFACT = ASSET_ROOT / "experiments" / f"wp3_real_report_smoke_{CASE_ID}.json"
MD_ARTIFACT = ASSET_ROOT / "experiments" / f"wp3_real_report_smoke_{CASE_ID}.md"

_CITATION_LABEL_RE = re.compile(r"\[C(\d+)\]")


def _count_citation_labels(markdown: str) -> int:
    labels = {int(m) for m in _CITATION_LABEL_RE.findall(markdown)}
    return len(labels) if labels else 0


def _max_citation_label(markdown: str) -> int:
    labels = [int(m) for m in _CITATION_LABEL_RE.findall(markdown)]
    return max(labels) if labels else 0


def main() -> None:
    if not SOURCE_ARTIFACT.is_file():
        raise SystemExit(f"missing source artifact: {SOURCE_ARTIFACT}")

    source = json.loads(SOURCE_ARTIFACT.read_text(encoding="utf-8"))
    workflow_result = FeatureRiskReviewWorkflowResult.model_validate(source["result"])

    report = FeatureRiskReviewAggregator().aggregate(workflow_result)
    if isinstance(report, FeatureRiskReviewAggregationFailure):
        summary = {
            "REPORT_SMOKE_STATUS": "FAILED",
            "CASE_ID": CASE_ID,
            "failure": report.model_dump(mode="json"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    markdown = render_feature_risk_review_markdown(report)

    report_json = {
        "experiment_id": EXPERIMENT_ID,
        "source_artifact": str(SOURCE_ARTIFACT.relative_to(PROJECT_ROOT)),
        "case_id": CASE_ID,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "aggregation_type": "deterministic",
        "risk_policy_calibrated": False,
        "report": report.model_dump(mode="json"),
    }

    JSON_ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    JSON_ARTIFACT.write_text(
        json.dumps(report_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # newline="" 关闭 write 时的 newline translation：磁盘字节必须精确等于
    # renderer 输出字符串的 UTF-8 编码（source description 内可能含 \r\n）。
    MD_ARTIFACT.write_text(markdown, encoding="utf-8", newline="")

    saved_markdown = MD_ARTIFACT.read_bytes().decode("utf-8")
    if saved_markdown != markdown:
        print(json.dumps({"REPORT_SMOKE_STATUS": "FAILED", "reason": "saved markdown != renderer output"}, ensure_ascii=False))
        raise SystemExit(2)

    saved_report = json.loads(JSON_ARTIFACT.read_bytes().decode("utf-8"))["report"]
    if saved_report != report.model_dump(mode="json"):
        print(json.dumps({"REPORT_SMOKE_STATUS": "FAILED", "reason": "saved json report != typed report"}, ensure_ascii=False))
        raise SystemExit(2)

    evidence_count = len(report.evidence_refs)
    label_count = _count_citation_labels(markdown)
    max_label = _max_citation_label(markdown)
    resolution = "PASS" if (label_count > 0 and max_label <= evidence_count) else "FAIL"

    observed = {
        "REPORT_SMOKE_STATUS": "SUCCESS",
        "RENDERER_OUTPUT_EQUALS_SAVED_MARKDOWN": "YES",
        "MARKDOWN_RENDERER_BYTE_TEXT_EQUIVALENCE": "PASS",
        "CASE_ID": CASE_ID,
        "REPORT_COMPLETENESS": report.completeness.value,
        "RISK_LEVEL": report.risk_level.value if report.risk_level else None,
        "PRIORITY": report.priority.value if report.priority else None,
        "COVERAGE_STATE": report.coverage_state.value if report.coverage_state else None,
        "RISK_FINDING_COUNT": len(report.high_risk_scenarios),
        "HISTORICAL_ISSUE_COUNT": len(report.historical_issues),
        "TEST_PLAN_COUNT": len(report.existing_coverage),
        "TEST_CASE_COUNT": len(report.existing_test_cases),
        "RECOMMENDED_MISSING_CASE_COUNT": len(report.missing_cases),
        "EVIDENCE_COUNT": evidence_count,
        "UNCERTAINTY_COUNT": len(report.uncertainties),
        "MARKDOWN_CITATION_LABEL_COUNT": label_count,
        "REPORT_EVIDENCE_COUNT": evidence_count,
        "CITATION_LABEL_RESOLUTION": resolution,
        "JSON_ARTIFACT": str(JSON_ARTIFACT.relative_to(PROJECT_ROOT)),
        "MARKDOWN_ARTIFACT": str(MD_ARTIFACT.relative_to(PROJECT_ROOT)),
    }
    print(json.dumps(observed, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
