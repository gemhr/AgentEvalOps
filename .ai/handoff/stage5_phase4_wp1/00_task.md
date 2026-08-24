# Stage5-Phase4-WP1 — Feature Risk Review Dataset + Contracts

基于 WP0 冻结的 5 个 Kubernetes case，创建最小 typed contracts、归一化业务投影、显式 evaluation annotation contract 与 loader。正常 Feature Review 数据路径不得读取评价标注。

不实现 Agent、工作流、风险聚合、RAG index、LLM 调用或生产 Runtime 修改；不修改 LocalAgent 与 Phase3 冻结资产。
