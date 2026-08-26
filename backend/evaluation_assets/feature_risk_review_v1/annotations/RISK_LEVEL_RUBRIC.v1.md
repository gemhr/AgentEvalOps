# WP4 Human Risk-Level Rubric v1

```text
RUBRIC_ID = feature-risk-review-human-risk-level.v1
RUBRIC_STATUS = FROZEN_FOR_WP4_ANNOTATION
GROUND_TRUTH_OWNER = HUMAN_REVIEW
APPLIES_TO = FROZEN_5_KUBERNETES_CASES
CALIBRATED_POLICY = NO
PREDICTION_INPUT_ALLOWED = NO
```

本量表用于人工填写 `EvaluationAnnotation.expected_risk_level`。它独立于 WP3 runtime Risk Policy、模型输出、
retrieval 排名和最终报告，不用于修改或校准 runtime policy。

## 1. 允许阅读的证据

只能依据当前 Case 的冻结 source：

- KEP 原文与 metadata/source manifest；
- enhancement tracking issue 与 enrichment 中的真实 historical issue snapshot；
- Test Plan 与 evaluation reference；
- source 中明确描述的风险、缓解、回滚、兼容性、测试和 production-readiness 信息。

不得查看或使用：

- WP2/WP3/WP4 prediction、RiskFinding、RiskLevel、Priority 或 summary；
- retriever top-K 排名或 Agent 是否引用某个 issue；
- 为了让模型 accuracy 更高而反向调整的标签；
- WP3 deterministic Risk Policy 的输出或规则匹配结果。

## 2. 评审维度

逐 Case 阅读 source 后，从以下四个维度形成书面判断。维度不单独计分，也不做机械多数投票。

### 2.1 潜在影响（Impact）

- 严重：可能导致安全或认证边界破坏、数据损坏/丢失、核心可用性中断、大范围工作负载或关键控制路径受影响。
- 中等：可能导致范围受限的功能失败、兼容性/版本偏差、可恢复的服务降级或显著运维负担。
- 较低：影响局部、非关键、容易检测和恢复，且不会实质影响安全、数据完整性或核心可用性。

### 2.2 暴露程度（Exposure）

- 较高：常见或默认路径可能触发，影响面广，或 source 中存在直接相关的真实历史问题。
- 中等：需要特定配置、版本组合或操作条件，但属于现实可发生场景。
- 较低：显式 opt-in、适用范围狭窄，且有可靠 guardrail 限制影响传播。

Opt-in 只能降低暴露程度，不能自动把严重安全、数据或可用性后果降为 LOW。

### 2.3 缓解与恢复（Mitigation and Recovery）

- 较弱：缺少明确缓解，失败难检测、难回滚，或恢复需要复杂人工介入。
- 部分：已有缓解/回滚方案，但仍存在重要限制、依赖或未验证路径。
- 较强：关键风险有明确 guardrail、监控、回滚和可验证恢复路径。

### 2.4 测试证据（Test Evidence）

- 明显不足：安全、数据、核心可用性、升级/回滚或关键兼容路径缺少测试证据。
- 部分覆盖：主路径已有计划或测试，但仍有重要失败路径、版本组合或恢复路径缺口。
- 充分：关键主路径与重要失败/恢复路径均有 source-backed 测试证据。

Test Plan 存在不等于测试已实现；缺少真实 TestCase mapping 时，不得仅凭计划文字宣称“充分覆盖”。

## 3. 最终等级判定

按以下顺序判定，命中后停止；不要先选等级再寻找理由。

### HIGH

满足任一条件：

1. source 支持“严重潜在影响”，并且暴露程度不是较低，或缓解/恢复较弱，或关键测试明显不足；
2. source 存在与该变更直接相关的真实历史问题，且同类失败可能影响安全、数据完整性或核心可用性；
3. 多个相互独立的中等风险会在同一现实场景叠加，形成大范围影响或困难恢复。

不得仅因“有历史 issue”“有 coverage gap”或“变更复杂”单独标为 HIGH；必须同时写明实际影响链和 source 依据。

### MEDIUM

未命中 HIGH，且满足任一条件：

1. 存在明确、现实可发生的兼容性、可靠性、升级/回滚、运维或局部可用性风险；
2. 风险影响可控制或可恢复，但缓解/测试仍有重要缺口；
3. 影响主要受 opt-in、范围或 guardrail 限制，但失败仍会给真实用户或操作方造成显著问题。

### LOW

只有同时满足以下全部条件才可标为 LOW：

1. source 未显示安全、数据完整性、核心可用性或大范围影响风险；
2. 变更影响局部，触发范围受限，失败容易检测和恢复；
3. 主要风险具有明确、source-backed 的 guardrail、回滚或缓解措施；
4. 关键主路径及重要失败/恢复路径具有充分测试证据；
5. 没有尚未解释的直接相关历史问题或重要 coverage gap。

“没有检索到风险”“没有 Agent finding”“文档没写风险”均不能作为 LOW 的充分依据。证据不足时应使用
`NOT_EVALUATED`，而不是 LOW。

## 4. 无法可靠判定

如果 source 不足以满足上述任一等级的最低证据要求：

```text
expected_risk_level = null
field_status(expected_risk_level) = NOT_EVALUATED
reason = <说明缺少的 source 或无法消解的歧义>
```

不得为了保持 denominator=5 勉强选择等级。

## 5. 每个 Case 必须保存的人工理由

`annotation_source` 至少写明：

1. 使用了哪些 source 文件/section；
2. 主要影响与触发条件；
3. 关键缓解/回滚和测试证据；
4. 为什么满足所选等级而不满足相邻等级。

`annotation_source = human_curated` 是占位值，不足以支持 `HUMAN_REVIEWED`。

## 6. 冲突处理

- 若影响严重但暴露很低：不能自动降级；结合缓解、恢复和测试证据判断 HIGH 或 MEDIUM。
- 若历史 issue 相关性不确定：不得作为升级依据；在 `annotation_source` 记录不确定性。
- 若 MEDIUM 与 HIGH 难以区分：只有 source 能明确支持 HIGH 的影响链时才选 HIGH，否则选 MEDIUM并记录边界。
- 若 LOW 与 MEDIUM 难以区分：LOW 需要第 3 节全部条件的正面证据；缺一项则不能标 LOW。
- 若关键事实相互冲突且无法依据冻结 source 消解：使用 `NOT_EVALUATED`。

## 7. Freeze Rule

本文件是 WP4 人工标注的冻结 v1 量表。开始填写第一个 Case 后：

- 不得修改本文件来适配已看到的 Case 或 prediction；
- 不得依据 5 Case 的 evaluation 结果调整等级定义；
- 如确需改变规则，必须创建新的 rubric version 和新的独立 evaluation 阶段/split；
- 已按 v1 产生的 Ground Truth 不得与新版本混算。

文件名、`RUBRIC_ID` 与 Git/source bytes 共同标识本版本；hash 只能作为 freeze evidence，不替代 Human Review authority。

