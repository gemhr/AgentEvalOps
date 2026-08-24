# Stage5-Phase4-WP0 — Kubernetes Data Source Bootstrap

## 目标与范围

为后续 Feature Risk Review 准备 5 个可追溯、可离线使用的 Kubernetes KEP case；只包含数据、轻量准备/校验脚本、测试和交接材料。

不实现 Three-Agent Workflow、风险聚合、RAG index、Runtime、生产 GitHub 集成或 LocalAgent 修改；不修改任何 Phase3 artifact。

## 验收条件

- Kubernetes 源项目固定到一个 commit。
- 候选 KEP 完成审计，5 个正式 case 均有 feature input、隔离的 evaluation reference、真实 issue evidence 和真实 Test Plan。
- 原始资料、归一化投影和人工 annotation template 分层保存。
- 基本数据校验与其 focused test 通过。
