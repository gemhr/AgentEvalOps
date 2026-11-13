import { test, expect } from "@playwright/test";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { writeFileSync } from "node:fs";
import path from "node:path";

let child: ChildProcessWithoutNullStreams; let token = ""; let backend = "";
test.beforeAll(async () => {
  const root = process.env.BASEBAND_AGENTCORE, python = process.env.BASEBAND_PYTHON;
  if (!root || !python || !process.env.COVERAGE_REPLAY_EVIDENCE) throw new Error("需要显式隔离后端与封存来源");
  child = spawn(python, ["-u", path.join(root, "scripts/coverage_execution_server.py")], { cwd: root, stdio: "pipe", env: { ...process.env, COVERAGE_PORT: "18091" } }); child.stderr.resume();
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("后端未就绪")), 60000); const lines = createInterface({ input: child.stdout });
    lines.on("line", line => { if (line.startsWith("COVERAGE_PRIVATE:")) { const data = JSON.parse(line.slice("COVERAGE_PRIVATE:".length)); token = data.human_token; backend = data.base_url; clearTimeout(timer); resolve(); } });
    child.once("exit", () => { clearTimeout(timer); reject(new Error("后端提前退出")); });
  });
});
test.afterAll(async () => {
  if (!child) return; child.stdin.end("stop\n");
  await new Promise<void>(resolve => { if (child.exitCode !== null) return resolve(); const timer = setTimeout(() => { child.kill(); resolve(); }, 30000); child.once("exit", () => { clearTimeout(timer); resolve(); }); });
});

test("覆盖资产准备、刷新、人工执行与同一来源的独立评价", async ({ page, request }) => {
  test.setTimeout(120000);
  if (process.env.COVERAGE_GO_RESULT) {
    await page.goto("/"); await page.getByLabel("开发登录密码").fill(process.env.COVERAGE_GO_DEV_PASSWORD!);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await page.locator(`a[href*="/project/${process.env.COVERAGE_GO_PROJECT}"]`).first().click();
    await page.getByRole("link", { name: "结果", exact: true }).click();
    await page.getByLabel("资源 ID").fill(process.env.COVERAGE_GO_RESULT);
    await expect(page.getByRole("heading", { name: "Evaluation Verdict（评估结论）", exact: true }).locator("..")).toContainText("PASS");
    await expect(page.getByRole("heading", { name: "数值评分（工作流评价与原始测试结果独立）", exact: true }).locator("..")).toContainText("1");
    if (process.env.COVERAGE_RESULT_SCREENSHOT) await page.screenshot({ path: process.env.COVERAGE_RESULT_SCREENSHOT, fullPage: true });
    return;
  }
  const authenticate = async () => { await page.getByLabel("HUMAN 访问凭据").fill(token); await page.getByRole("button", { name: "验证身份并读取任务" }).click(); };
  await page.goto("/feature-impact"); await authenticate();
  const created = page.waitForResponse(r => r.url().endsWith("/public-experiment-tasks") && r.request().method() === "POST");
  await page.getByRole("button", { name: "创建接口覆盖合成任务" }).click(); const task = await (await created).json();
  const reporting = page.waitForResponse(r => r.url().endsWith("/rule-reports")); await page.getByRole("button", { name: "执行确定性规则分析" }).click(); const report = await (await reporting).json();
  await page.getByLabel("sampling-control 审核决定").selectOption("ACCEPT"); await page.getByLabel("sampling-control 审核理由").fill("HUMAN 核对新版值域与旧用例覆盖缺口");
  const reviewing = page.waitForResponse(r => r.url().endsWith("/reviews") && r.request().method() === "POST");
  await page.getByRole("button", { name: "提交审核并封存覆盖计划" }).click(); const plan = await (await reviewing).json();
  const preparing = page.waitForResponse(r => r.url().endsWith("/case-assets") && r.request().method() === "POST");
  await page.getByRole("button", { name: /构造并记录模拟分发/ }).click(); const preparedResponse = await (await preparing).json();
  await expect(page.getByRole("heading", { name: `资产 ${preparedResponse.instance_id}`, exact: true })).toBeVisible();
  await expect(page.getByText("未执行 · LOCAL_SIMULATED", { exact: false })).toBeVisible();
  const headers = { Authorization: `Bearer ${token}` }; const assetURL = `${backend}/api/feature-impact/coverage-plans/${plan.plan_id}/case-assets`;
  const prepared = (await (await request.get(assetURL, { headers })).json()).items[0]; expect(prepared.executions).toHaveLength(0);
  expect((await (await request.get(`${backend}/api/feature-impact/tasks/${task.task_id}`, { headers })).json()).exposed).toBe(false);
  const restore = async () => { await page.reload(); await authenticate(); await page.getByRole("button", { name: `${task.title} · ${task.task_id.slice(0, 8)}`, exact: true }).click(); await page.getByRole("button", { name: "RULE v1", exact: true }).click(); };
  await restore(); await expect(page.getByRole("button", { name: "执行此资产", exact: true })).toBeVisible();
  const executing = page.waitForResponse(r => r.url().includes(`/scenarios/${prepared.instance_id}/executions`) && r.request().method() === "POST");
  await page.getByRole("button", { name: "执行此资产", exact: true }).click(); const execution = await (await executing).json();
  await expect(page.getByText(`原测试 FAIL · CIExecution ${execution.execution_id}`, { exact: false })).toBeVisible();
  await restore(); await expect(page.getByText(execution.execution_id, { exact: false }).first()).toBeVisible();
  await page.getByRole("link", { name: "查看同一执行的 Incident" }).click(); await page.getByLabel("HUMAN JWT").fill(token); await page.getByRole("button", { name: "验证身份", exact: true }).click();
  await expect(page.getByRole("heading", { name: "覆盖计划与资产来源" })).toBeVisible(); await expect(page.getByText("原始 CI FAIL", { exact: false })).toBeVisible();
  await expect(page.getByText("CONFIG_APPLIED_AFTER_SAMPLE", { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: "返回 Feature 任务与封存计划" })).toHaveAttribute("href", new RegExp(task.task_id));
  if (process.env.COVERAGE_PAGE_SCREENSHOT) await page.screenshot({ path: process.env.COVERAGE_PAGE_SCREENSHOT, fullPage: true });
  const selector = { task_id: task.task_id, report_id: report.report_id, plan_id: plan.plan_id, test_id: plan.tests[0].test_id, instance_id: prepared.instance_id, execution_id: execution.execution_id, incident_id: execution.incident_id, diagnosis_kind: "RULE" };
  if (!process.env.COVERAGE_PAGE_WORKFLOW_PATH) throw new Error("需要保存页面工作流来源");
  writeFileSync(process.env.COVERAGE_PAGE_WORKFLOW_PATH, JSON.stringify({ selector, task, report, plan, prepared, execution, workflow: "PASS", model_calls: 0, incremental_agent_value: "NOT_VALIDATED", prepared_executions: 0, reload_verified: true }, null, 2));
});
