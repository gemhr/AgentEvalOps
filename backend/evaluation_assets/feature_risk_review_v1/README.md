# Kubernetes Feature Risk Review Dataset v1

Stage5 Phase4 的离线 Kubernetes Feature Risk Review 数据集。源冻结为 `kubernetes/enhancements@c4f439c2dd4acb928094660be0ea771bf63f2b76`。

- `raw/` 是公开 KEP 与 GitHub enhancement tracking issue 的冻结来源。
- `cases/` 分离 Agent 可见 `feature.md` 与 evaluation-only `evaluation_reference.md`。
- `normalized/cases.v1.json` 是 WP1 Typed Contract 的正常 demo path 投影，不包含 annotation。
- `annotations/annotations.v1.json` 是 evaluation-only annotation；所有 case 均为 `PENDING`。
- `issues/` 与 `tests/` 分别保存真实 tracking issue 和 KEP Test Plan；真实 test-function mapping 为 `PARTIAL`。

Ground Truth 仍为 `PENDING`。未构建 RAG index，未执行真实 Agent，未发生 production change。
