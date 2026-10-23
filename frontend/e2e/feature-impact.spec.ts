import { test, expect } from "@playwright/test";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import path from "node:path";

let child: ChildProcessWithoutNullStreams;
let humanToken = "";

test.beforeAll(async () => {
  const root = process.env.FEATURE_IMPACT_AGENTCORE;
  const python = process.env.FEATURE_IMPACT_PYTHON;
  if (!root || !python) throw new Error("需要显式配置真实 AgentCore 工作区与 Python 环境");
  child = spawn(python, ["-u", path.join(root, "scripts/feature_impact_rehearsal_server.py")], { cwd: root, stdio: "pipe", env: { ...process.env, FEATURE_IMPACT_PORT: "18091" } });
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("真实业务服务未就绪")), 15000);
    const lines = createInterface({ input: child.stdout });
    lines.once("line", (line) => {
      try { const data = JSON.parse(line); humanToken = data.human_token; if (!humanToken) throw new Error(); clearTimeout(timer); lines.close(); resolve(); }
      catch { reject(new Error("私有测试握手无效")); }
    });
    child.once("exit", () => { clearTimeout(timer); reject(new Error("真实业务服务提前退出")); });
  });
});

test.afterAll(async () => {
  if (!child) return;
  child.stdin.end("stop\n");
  await new Promise<void>((resolve) => { if (child.exitCode !== null) return resolve(); const timer = setTimeout(() => { child.kill(); resolve(); }, 10000); child.once("exit", () => { clearTimeout(timer); resolve(); }); });
});

test("冻结任务、规则、人工审核和独立执行经过真实页面与 PostgreSQL", async ({ page }) => {
  await page.goto("/feature-impact");
  await page.getByLabel("HUMAN 访问凭据").fill(humanToken);
  await page.getByRole("button", { name: "验证身份并读取任务" }).click();
  await page.getByRole("button", { name: "创建冻结演练任务" }).click();
  await expect(page.getByText("事前规划视图，尚未执行", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "执行确定性规则分析" }).click();
  await expect(page.getByRole("heading", { name: /RULE 报告 v1/ })).toBeVisible();
  for (const id of ["H1", "H2", "H3", "H4", "H5"]) {
    await page.getByLabel(`${id} 审核决定`).selectOption(["H1", "H2", "H3"].includes(id) ? "ACCEPT" : id === "H4" ? "REJECT" : "NEEDS_CLARIFICATION");
    await page.getByLabel(`${id} 审核理由`).fill(id === "H1" ? "Sol-A independent review" : id === "H5" ? "数据所有权与消费时点需开发澄清" : "已人工核对公开依据和测试操作");
  }
  await page.getByRole("button", { name: "提交审核并封存覆盖计划" }).click();
  await expect(page.getByText("审核不会自动执行；以下按钮单独触发已获准的操作序列。")).toBeVisible();
  await page.getByRole("button", { name: "执行 test-H1", exact: true }).click();
  await expect(page.getByText('"verdict": "FAIL"', { exact: false })).toBeVisible();
  await expect(page.locator("pre").filter({ hasText: '"execution_version": "resource-lifecycle.v1"' })).toContainText('"count": 91');
  await expect(page.locator("main").getByRole("alert")).toHaveCount(0);
  if (process.env.FEATURE_IMPACT_SCREENSHOT) await page.screenshot({ path: process.env.FEATURE_IMPACT_SCREENSHOT, fullPage: true });
  await page.reload();
  await page.getByLabel("HUMAN 访问凭据").fill(humanToken);
  await page.getByRole("button", { name: "验证身份并读取任务" }).click();
  await page.getByRole("button", { name: /公共资源回收变更的历史影响分析/ }).first().click();
  await page.getByRole("button", { name: "RULE v1", exact: true }).click();
  for (const id of ["H1", "H2", "H3", "H4", "H5"]) {
    await expect(page.getByLabel(`${id} 审核决定`)).toHaveValue(["H1", "H2", "H3"].includes(id) ? "ACCEPT" : id === "H4" ? "REJECT" : "NEEDS_CLARIFICATION");
    await expect(page.getByLabel(`${id} 审核理由`)).toHaveValue(id === "H1" ? "Sol-A independent review" : id === "H5" ? "数据所有权与消费时点需开发澄清" : "已人工核对公开依据和测试操作");
    await expect(page.getByLabel(`${id} 审核决定`)).toBeDisabled();
    await expect(page.getByLabel(`${id} 审核理由`)).toBeDisabled();
  }
  await page.getByRole("button", { name: /查看 test-H1/ }).click();
  await expect(page.getByText('"verdict": "FAIL"', { exact: false })).toBeVisible();
});
