"""WP4 evaluation-only runner（单一入口，不调用模型）。

Subcommands:

- ``validate-annotations``：加载 annotations + field-status sidecar，输出
  ``GROUND_TRUTH_STATE``（当前预期为 ``HUMAN_REVIEW_REQUIRED``）。
- ``prepare-manifest``：构建 runtime freeze manifest。Ground Truth 未
  ``GROUND_TRUTH_READY`` 时只生成 ``NOT_FROZEN`` draft，拒绝 final frozen manifest。
- ``evaluate``：加载 frozen predictions + human GT + adjudications，运行
  ``FeatureRiskReviewEvaluator`` 生成 per-case evaluation 与 aggregate summary。

本阶段绝不对真实模型发起调用；真实 execution 的 ``run`` 子命令属于未来阶段。
"""

# ruff: noqa: D103,D415,E402

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.feature_risk_review.evaluation import (  # noqa: E402
    EvaluationValidationError,
    FeatureRiskReviewEvaluator,
    FreezeStatus,
    GroundTruthState,
    build_runtime_manifest,
    detect_ground_truth_state,
    load_ground_truth_field_statuses,
    load_manual_adjudications,
    load_runtime_prediction_artifact,
    render_evaluation_summary_markdown,
)
from app.core.feature_risk_review.loader import (  # noqa: E402
    FeatureRiskDatasetLoadError,
    load_evaluation_annotations,
)

DEFAULT_ASSET_ROOT = PROJECT_ROOT / "evaluation_assets" / "feature_risk_review_v1"


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception:  # noqa: BLE001 - 缺少 git 时 manifest 记录 unknown
        return "unknown"


def cmd_validate_annotations(root: Path) -> int:
    annotations = load_evaluation_annotations(root)
    field_statuses = load_ground_truth_field_statuses(root)
    report = detect_ground_truth_state(annotations, field_statuses)
    payload = report.model_dump(mode="json")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_prepare_manifest(root: Path, backend_root: Path) -> int:
    annotations = load_evaluation_annotations(root)
    field_statuses = load_ground_truth_field_statuses(root)
    report = detect_ground_truth_state(annotations, field_statuses)
    manifest = build_runtime_manifest(
        root=root,
        backend_root=backend_root,
        git_commit=_git_commit(),
        gt_state=report.state,
    )
    out = root / "experiments" / "wp4" / "run_manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if manifest.freeze_status == FreezeStatus.FROZEN:
        print(json.dumps({"FREEZE_STATUS": "FROZEN", "manifest": str(out)}, ensure_ascii=False))
    else:
        print(
            json.dumps(
                {
                    "FREEZE_STATUS": "NOT_FROZEN",
                    "GROUND_TRUTH_STATE": report.state.value,
                    "message": "Ground Truth not READY; draft manifest only, final frozen manifest refused",
                    "manifest": str(out),
                },
                ensure_ascii=False,
            )
        )
    return 0


def cmd_evaluate(root: Path) -> int:
    annotations = load_evaluation_annotations(root)
    field_statuses = load_ground_truth_field_statuses(root)
    gt_report = detect_ground_truth_state(annotations, field_statuses)
    if gt_report.state != GroundTruthState.GROUND_TRUTH_READY:
        print(
            json.dumps(
                {
                    "EVALUATION_STATUS": "NOT_RUN",
                    "GROUND_TRUTH_STATE": gt_report.state.value,
                    "reason": "quality evaluation requires GROUND_TRUTH_READY",
                },
                ensure_ascii=False,
            )
        )
        return 2

    predictions_dir = root / "experiments" / "wp4" / "predictions"
    prediction_files = sorted(predictions_dir.glob("*.json")) if predictions_dir.is_dir() else []
    if not prediction_files:
        print(
            json.dumps(
                {
                    "EVALUATION_STATUS": "NOT_RUN",
                    "GROUND_TRUTH_STATE": gt_report.state.value,
                    "reason": "no prediction artifacts found under experiments/wp4/predictions",
                },
                ensure_ascii=False,
            )
        )
        return 2

    adjudications = load_manual_adjudications(root)

    predictions = []
    paths: dict[str, str] = {}
    for path in prediction_files:
        prediction = load_runtime_prediction_artifact(path)
        predictions.append(prediction)
        paths[prediction.case_id] = str(path)

    evaluator = FeatureRiskReviewEvaluator()
    summary = evaluator.evaluate(
        predictions=predictions,
        annotations=annotations,
        field_statuses=field_statuses,
        adjudications=adjudications,
        prediction_artifact_paths=paths,
    )

    evaluations_dir = root / "experiments" / "wp4" / "evaluations"
    evaluations_dir.mkdir(parents=True, exist_ok=True)
    for per_case in summary.per_case:
        (evaluations_dir / f"{per_case.case_id}.json").write_text(
            json.dumps(per_case.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    summary_json = root / "experiments" / "wp4" / "wp4_evaluation_summary.json"
    summary_json.write_text(
        json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown = render_evaluation_summary_markdown(summary)
    (root / "experiments" / "wp4" / "wp4_evaluation_summary.md").write_text(
        markdown, encoding="utf-8", newline=""
    )

    print(
        json.dumps(
            {
                "EVALUATION_STATUS": "RUN",
                "GROUND_TRUTH_STATE": summary.ground_truth_state.value,
                "E2E_WORKFLOW_SUCCESS": summary.e2e_workflow_success.value,
                "REPORT_GENERATION_SUCCESS": summary.report_generation_success.value,
                "RISK_LEVEL_ACCURACY": summary.risk_level_accuracy.value,
                "CITATION_CORRECTNESS": summary.citation_correctness.value,
                "per_case": [pc.case_id for pc in summary.per_case],
                "summary_json": str(summary_json),
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WP4 feature risk review evaluation runner")
    parser.add_argument(
        "subcommand",
        choices=("validate-annotations", "prepare-manifest", "evaluate"),
    )
    parser.add_argument("--asset-root", type=Path, default=DEFAULT_ASSET_ROOT)
    parser.add_argument("--backend-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()

    try:
        if args.subcommand == "validate-annotations":
            return cmd_validate_annotations(args.asset_root)
        if args.subcommand == "prepare-manifest":
            return cmd_prepare_manifest(args.asset_root, args.backend_root)
        return cmd_evaluate(args.asset_root)
    except (EvaluationValidationError, FeatureRiskDatasetLoadError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
