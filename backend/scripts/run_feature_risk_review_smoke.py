"""WP2 controlled real-model smoke runner (REAL EXPERIMENT, not benchmark/tuning).

只执行一次真实 smoke：
1. 通过 `load_feature_risk_review_cases()` 加载 1 个冻结 WP1 case（不读 annotation/expected_*）。
2. 组装 NormalizedFeatureRiskReviewDataProvider + SourcePreservingLexicalRetriever
   + 真实 LiteLLMFeatureRiskReviewModelPort。
3. 运行 FeatureRiskReviewWorkflow 一次。
4. 序列化 typed result 并保存 experiment artifact。

credential 只从本地 `.env.development` 经 python-dotenv 读入 os.environ（项目配置的 EVAL LLM
provider），不在此脚本硬编码、不打印、不写入 artifact。artifact 只含 validated structured
result，不含 raw prompt / raw response / credential。

Retry #2：输出到独立的 retry2 artifact，不覆盖第一次失败 artifact。
"""

# ruff: noqa: D103,D415,E402

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from app.adapters.feature_risk_review.data_provider import NormalizedFeatureRiskReviewDataProvider
from app.adapters.feature_risk_review.model import LiteLLMFeatureRiskReviewModelPort
from app.adapters.feature_risk_review.retrieval import SourcePreservingLexicalRetriever
from app.core.feature_risk_review import (
    BranchStatus,
    FeatureRiskReviewWorkflow,
    WorkflowStatus,
    load_feature_risk_review_cases,
)
from app.registry.settings import settings

ASSET_ROOT = PROJECT_ROOT / "evaluation_assets" / "feature_risk_review_v1"
SMOKE_CASE_ID = "k8s_541"
ATTEMPT = 2
EXPERIMENT_ID = f"wp2_real_model_smoke_retry{ATTEMPT}"
ARTIFACT_NAME = f"wp2_real_model_smoke_retry{ATTEMPT}_{SMOKE_CASE_ID}.json"
CORPUS_PATH = ASSET_ROOT / "retrieval" / "phase4_retrieval_corpus.v1.json"


def _env_setup() -> None:
    """把本地 `.env.development` 的 provider credential 读入 os.environ。

    仅填充未设置的环境变量（override=False）；不修改任何源码或冻结实现。
    """
    env_file = PROJECT_ROOT / ".env.development"
    if env_file.is_file():
        load_dotenv(env_file, override=False)


async def run_smoke() -> dict[str, object]:
    _env_setup()
    case = next(
        c for c in load_feature_risk_review_cases(ASSET_ROOT) if c.feature_document.case_id == SMOKE_CASE_ID
    )
    provider = NormalizedFeatureRiskReviewDataProvider(root=ASSET_ROOT)
    retriever = SourcePreservingLexicalRetriever(path=CORPUS_PATH)
    model_port = LiteLLMFeatureRiskReviewModelPort()
    workflow = FeatureRiskReviewWorkflow(
        model_port=model_port, data_provider=provider, retriever=retriever
    )

    started_at = datetime.now(timezone.utc)
    try:
        result = await workflow.run(case)
        exc = None
    except Exception as run_error:  # noqa: BLE001 - 顶层异常也如实记录
        result = None
        exc = run_error
    finished_at = datetime.now(timezone.utc)

    if result is not None:
        payload = result.model_dump(mode="json")
    else:
        payload = {
            "workflow_status": WorkflowStatus.FAILED.value,
            "document_analysis": {"status": BranchStatus.FAILED.value},
            "risk_retrieval": {"status": BranchStatus.NOT_STARTED.value},
            "test_review": {"status": BranchStatus.NOT_STARTED.value},
            "top_level_error": {
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
        }

    return {
        "experiment_id": EXPERIMENT_ID,
        "attempt": ATTEMPT,
        "case_id": SMOKE_CASE_ID,
        "model_ref": settings.EVAL_LLM_MODEL,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "result": payload,
    }


def main() -> None:
    report = asyncio.run(run_smoke())
    artifact_dir = ASSET_ROOT / "experiments"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / ARTIFACT_NAME
    artifact.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    r = report["result"]
    summary = {
        "case_id": report["case_id"],
        "model_ref": report["model_ref"],
        "workflow_status": r.get("workflow_status"),
        "document_analysis": r.get("document_analysis", {}).get("status"),
        "risk_retrieval": r.get("risk_retrieval", {}).get("status"),
        "test_review": r.get("test_review", {}).get("status"),
        "artifact": str(artifact),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()