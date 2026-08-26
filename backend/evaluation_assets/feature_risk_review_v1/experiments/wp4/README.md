# WP4 experiments layout (evaluation-only)

WP4 真实执行与 quality evaluation 的 artifact 目录。本目录由 implementation
创建为模板/schema；**不包含伪造 evaluation result**。

```text
experiments/wp4/
  run_manifest.json        runtime freeze manifest（当前为 NOT_FROZEN draft）
  predictions/             每 Case 一个 <case_id>.json（未来真实 execution 落盘）
  reports/                 每 Case 一个 <case_id>.md（未来 renderer 输出）
  adjudications/           每 Case 一个 <case_id>.json（人工 verdict，Agent 不代填）
  evaluations/             每 Case 一个 <case_id>.json（evaluator 输出）
  wp4_evaluation_summary.json   aggregate typed authority
  wp4_evaluation_summary.md     deterministic presentation
```

## Guardrails

- Ground Truth 未 `GROUND_TRUTH_READY` 前：不生成 final frozen manifest，
  不产生真实 prediction，不运行 quality evaluation。
- `predictions/` 中的 prediction 不复制 annotation / expected_*。
- `adjudications/` 的 verdict 只能由用户本人填写；Agent 不预填。
- annotation 继续只位于 `annotations/` evaluation-only 区域。

## Runner

```text
cd backend
uv run python scripts/run_feature_risk_review_evaluation.py validate-annotations
uv run python scripts/run_feature_risk_review_evaluation.py prepare-manifest
uv run python scripts/run_feature_risk_review_evaluation.py evaluate
```