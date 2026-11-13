# PandaProbe Documentation

This directory contains the PandaProbe documentation site, built with [Mintlify](https://mintlify.com/).

## Prerequisites

- [Node.js](https://nodejs.org/) v20.17.0 or higher (LTS recommended)

## Local Development

### Install the Mintlify CLI

```bash
npm i -g mint
```

Or with pnpm:

```bash
pnpm add -g mint
```

See the [Mintlify CLI installation docs](https://mintlify.com/docs/installation) for full details and troubleshooting.

### Preview locally

Run the following command from the `docs/` directory:

```bash
mint dev
```

The site will be available at [http://localhost:3000](http://localhost:3000). Changes to `.mdx` files are reflected automatically via hot reload.

To use a custom port:

```bash
mint dev --port 3001
```

### Push changes

Commit and push to trigger an automatic deployment:

```bash
git add .
git commit -m "Update docs"
git push
```

Mintlify automatically deploys changes when connected to your GitHub repository.

## Project Structure

```
docs/
├── docs.json                        # Mintlify configuration (theme, navigation, branding)
├── assets/
│   ├── logo/                        # Brand logos and favicons
│   ├── images/                      # Screenshots and diagrams
│   ├── PandProbe.png                # Banner image (dark bg)
│   └── PandProbe-1.png              # Banner image with mascot
├── get-started/                     # Getting started guides
├── tracing/
│   ├── wrappers/                    # LLM provider wrappers (OpenAI, Anthropic, Gemini)
│   ├── integrations/                # Agent framework integrations
│   ├── manual/                      # Manual instrumentation (decorators, context managers)
│   └── configuration/               # Env vars, project config, troubleshooting
├── evaluation/                      # Evaluation framework (placeholders)
├── api-reference/                   # REST API reference (placeholder)
└── changelog/                       # Release notes (placeholder)
```

## Configuration

All site configuration lives in `docs.json`. Key settings:

| Setting | Description |
|---|---|
| `theme` | Mintlify theme (`linden`) |
| `colors` | Brand colors (primary: `#10B981`) |
| `background.color.dark` | Dark mode background (`#18191b`) |
| `logo` | Logo images for light/dark mode |
| `navigation` | Full sidebar and tab structure |

See the [Mintlify docs.json reference](https://mintlify.com/docs/organize/settings-reference) for all options.

## Writing Content

- All pages are `.mdx` files with YAML frontmatter (`title`, `description`, `icon`)
- Use [Mintlify components](https://mintlify.com/docs/content/components) for rich content: `<CodeGroup>`, `<Tabs>`, `<Note>`, `<Warning>`, `<Tip>`, `<Steps>`, `<Card>`, `<CardGroup>`, `<Accordion>`
- Cross-reference pages with absolute paths (e.g., `/tracing/concepts`)
- Add new pages to the `navigation` section in `docs.json`

## Adding a New Page

1. Create a new `.mdx` file in the appropriate directory
2. Add YAML frontmatter:
   ```yaml
   ---
   title: "Page Title"
   description: "Brief description"
   icon: "icon-name"
   ---
   ```
3. Add the page path (without `.mdx`) to `docs.json` under the correct navigation group
4. Verify it renders correctly with `mint dev`

## Deployment

Mintlify handles deployment automatically when connected to a GitHub repository. Push changes to the configured branch and the site updates within minutes.

For manual deployment or CI/CD integration, see the [Mintlify deployment docs](https://mintlify.com/docs/quickstart#deployment).

## Useful Commands

| Command | Description |
|---|---|
| `mint dev` | Start local dev server on port 3000 |
| `mint dev --port 3001` | Start on a custom port |
| `mint broken-links` | Check for broken internal links |

### 基带已封存调查评价

`BASEBAND_ENABLED=1` 在 API 与 Worker 选择独占业务模式，拒绝与其他 Target 模式或 Fixture/Online 模式混用。`BASEBAND_TARGET_CONFIG` 使用已有 LocalAgentConfig JSON（base_url、token_env、max_response_bytes、call_milliseconds、http）；token_env 只引用本机 SERVICE 凭据变量。启动会检查 evaluator capability；未发布相应 evaluator 时不能假装具备能力。

`POST /api/v1/projects/{project_id}/baseband/runs` 接收 suite 引用与按 Case identity 索引的 selectors。业务 Input/GT/evaluator 由正常 Catalog 发布和冻结；selector 保存到 EvaluationRun.Subject，不改变业务 Input digest。Go 不创建 AgentCore Run，只重验既有封存来源。`scripts/prepare-baseband-evaluation.py` 可发布 `go-backend/testdata/baseband_development.json` 的三个 DEVELOPMENT_VISIBLE 样本，GT 不发送给 AgentCore。规则源为 RULE，模型源为 AGENT，不能相互冒充。

Target 为 `baseband-sealed-http.v1`，只调用 `/api/ci/quality/baseband/evaluate`；专项 evaluator 为 `baseband-deterministic.v1`，metric 为 `baseband_diagnosis.v1`。传输/来源缺失返回不可评测；可信但错误的机制、类别、责任、可决性或动作得到业务 FAIL。真实模型 Candidate 与比较尚受准入阻断时，不得用 RULE 的 HTTP/PG PASS 替代 Candidate 验收。

基带 Target 的 Subject 现在明确区分 baseband-rule.v1 与 baseband_test_investigator/1.0.0；selectors 不进入 Case Input。评价来源投影提供实际 model_calls、COMMITTED tools、OBSERVED batches；标准 applicability provenance 与业务 Verdict 分开。公开 v1 ENUM 指标和冻结策略保留原定义：Comparison 可以真实完成并返回 BLOCKED，不能将 HTTP/PG PASS 或双方逐案 PASS 自动标记为 Release Gate PASS。

`scripts/prepare-baseband-evaluation.py --version v2` 发布独立 v2 Metric/Evaluator/Policy/Suite，复用原公开 Case/GT 和来源校验。`baseband-deterministic.v2` 输出 `baseband_diagnosis_correctness.v2`（SCALAR [0,1]、HIGHER_IS_BETTER、mean）：可信可评分且全部原业务条件成立为 1，可信业务错误为 0，无法评分仍为 null/INCONCLUSIVE；正确拒判也可得 1。策略保留 Required/PerCase、完整覆盖、最小值 1 和零下降容忍，双方全正确允许 UNCHANGED/PASS，缺失仍 BLOCKED。v1 定义和历史结果保持不变；v2 可通过既有 SERVICE 只读投影重新评价封存 Agent Run，无需新模型调用。

联合诊断开发集为 `go-backend/testdata/oracle_chain_development.json`，以 `--version v3` 正常发布独立 Case/GT/Dataset/Metric/Evaluator/Policy/Suite。`baseband-deterministic.v3` / `baseband_joint_diagnosis_correctness.v3` 采用相同 SCALAR/mean/全覆盖数值策略，按必需/允许 finding 的语义集合、逐项状态/失败关系、角色/安全动作及支持/反证条件判分。合法但错误报告为 0；可信的部分诊断与合理整体拒判为 1；缺 Artifact/来源/GT 为 null。既有 Worker、PG 与 Decision 不变。

覆盖工作流使用独立 `coverage-execution-input/request/output.v1`、`coverage-execution-deterministic.v1`、`coverage_execution_correctness.v1` 与 `coverage-execution-score.v1`。`prepare-coverage-execution-evaluation.py` 正常发布公开 Case/独立 GT/SCALAR Metric/Required PerCase 绝对门槛 Policy/Suite；`POST /api/v1/projects/{project_id}/coverage-execution/runs` 只选择已封存 RULE/HUMAN→HUMAN 资产执行。复用 BASEBAND_ENABLED 的同一 Target/client/Worker/PG，版本明确为 `coverage-execution-http.v1`，不能与旧输入或 evaluator 混用。GT 留在 Go 评价侧；Target 只传公开输入和受约束 selector。

五项原始判断为 coverage、approval_source、asset、execution、diagnosis，Provenance 还记录 required/matched/missing/unjustified/duplicate。语义/值域判断不用 test_id 作答案；跨域输入、子 baseband、plan/asset/distribution/trigger 分别核对摘要。来源可信且全部正确=1/PASS，可信业务错误=0/FAIL，缺来源/Artifact/GT=null/INCONCLUSIVE。原 CI FAIL 与工作流评价 PASS 是不同事实，不据此声称 Agent 增量价值。公开向量 `coverage_execution_vector.json` 明示 PURE_CONTRACT_VECTOR_NOT_PERSISTED；真实 HTTP/PG 与页面证据另存本地 Handoff，不将向量当真实执行。

`baseband-sealed-http.v2` 明确发送 quality-request.v2，严格读取 output.v2/diagnosis.v2；不放宽 v1 decoder。六类证据中 INPUT_MANIFEST 按完整 recipe 的公开字段投影校验，不把它错误地当作完整输入。完整 recipe 只发给受信 SERVICE 精确绑定，GT 不发往 AgentCore。source.observed_reads 包含真实原文读取的 evidence_id/digest、invocation/action、iteration_no 和 availability。前端沿原 IncidentDetail 展示逐项状态、业务 tick、原始 verdict 与缺失采集；旧 v1/v2 资产和结果不覆盖。
