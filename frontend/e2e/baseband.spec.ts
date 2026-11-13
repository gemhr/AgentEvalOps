import { test, expect } from "@playwright/test";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import path from "node:path";

test.use({ channel: process.env.BASEBAND_BROWSER_CHANNEL });

let child: ChildProcessWithoutNullStreams;
let humanToken = "";
let sources: { case_key?: string; incident_id: string; investigation_id: string; input: { receipt_available: boolean; ddt: { sample_rate: number }; kdt: { sample_rate: number } } }[] = [];

test.beforeAll(async () => {
  const root = process.env.BASEBAND_AGENTCORE, python = process.env.BASEBAND_PYTHON;
  if (!root || !python || !process.env.BASEBAND_REPLAY_EVIDENCE && !process.env.BASEBAND_SOURCE_REPLAY) throw new Error("需要显式提供真实封存报告和本机后端");
  child = spawn(python, ["-u", path.join(root, "scripts/baseband_rehearsal_server.py")], { cwd: root, stdio: "pipe", env: { ...process.env, BASEBAND_PORT: "18091" } });
  child.stderr.resume();
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("真实后端未就绪")), 60000);
    const lines = createInterface({ input: child.stdout });
    lines.on("line", line => {
      if (!line.startsWith("BASEBAND_PRIVATE:")) return;
      try { const data = JSON.parse(line.slice("BASEBAND_PRIVATE:".length)); humanToken = data.human_token; sources = data.sources; clearTimeout(timer); resolve(); }
      catch { reject(new Error("私有测试握手无效")); }
    });
    child.once("exit", () => { clearTimeout(timer); reject(new Error("真实后端提前退出")); });
  });
});

test.afterAll(async () => {
  if (!child) return;
  child.stdin.end("stop\n");
  await new Promise<void>(resolve => { if (child.exitCode !== null) return resolve(); const timer = setTimeout(() => { child.kill(); resolve(); }, 30000); child.once("exit", () => { clearTimeout(timer); resolve(); }); });
});

test("真实封存基带报告、输入差异和人工追加回归在现有页面可用", async ({ page }) => {
  test.setTimeout(120000);
  for (const source of sources) {
    await page.goto(`/ci-incidents/${source.incident_id}`);
    await page.getByLabel("HUMAN JWT").fill(humanToken);
    await page.getByRole("button", { name: "验证身份", exact: true }).click();
    await page.getByLabel("选择调查报告").selectOption(source.investigation_id);
    await expect(page.getByRole("heading", { name: `基带调查 · ${(process.env.BASEBAND_INPUT_VERSION === "v2" ? source.case_key !== "oracle_partial_capture" : source.input.receipt_available) ? "SUPPORTED" : "INSUFFICIENT_EVIDENCE"}` })).toBeVisible();
    await expect(page.getByText("输入摘要：", { exact: false })).toBeVisible();
    if (process.env.BASEBAND_INPUT_VERSION === "v2") {
      await expect(page.getByText("主张状态：", { exact: false }).first()).toBeVisible();
      await expect(page.getByText("原始 CI FAIL", { exact: false })).toBeVisible();
      await page.getByRole("button", { name: "DAAS_TIMELINE · AVAILABLE", exact: true }).click();
      await expect(page.getByText("ACK 不等于生效", { exact: false })).toBeVisible();
      await expect(page.locator("pre").filter({ hasText: '"sample_tick": 10' }).last()).toBeVisible();
      const missing = source.case_key === "oracle_partial_capture";
      await page.getByRole("button", { name: `APPLIED_STATE · ${missing ? "MISSING" : "AVAILABLE"}`, exact: true }).click();
      await expect(page.getByRole("heading", { name: `APPLIED_STATE · ${missing ? "MISSING" : "AVAILABLE"}`, exact: true })).toBeVisible();
      await expect(page.locator("main").getByRole("alert")).toHaveCount(0);
      if (missing && process.env.BASEBAND_SCREENSHOT) await page.screenshot({ path: process.env.BASEBAND_SCREENSHOT, fullPage: true });
      continue;
    }
    if (source.input.receipt_available && source.input.ddt.sample_rate !== source.input.kdt.sample_rate) {
      await page.getByRole("button", { name: "预览修复", exact: true }).click();
      const row = page.getByRole("row").filter({ hasText: "baseband.kdt.sample_rate" });
      await expect(row).toContainText("15"); await expect(row).toContainText("30");
      await page.getByRole("button", { name: "确认执行修复", exact: true }).click();
      await expect(page.getByText("已应用到版本 2，仍需单例和组合验证。")).toBeVisible();
      await page.getByRole("button", { name: "单独重跑 SINGLE", exact: true }).click();
      await expect(page.getByRole("heading", { name: "SINGLE · PASS · 版本 2" })).toBeVisible();
      await page.getByRole("button", { name: "组合回归 GROUP", exact: true }).click();
      await expect(page.getByRole("heading", { name: "GROUP · PASS · 版本 2" })).toBeVisible();
      await page.getByLabel("人工根因", { exact: true }).fill("DDT/KDT 采样率不一致");
      await page.getByLabel("修复确认", { exact: true }).fill("HUMAN 核对真实输入差异与回归");
      await page.getByLabel("确认组合回归已检查").check();
      await page.getByRole("button", { name: "人工关闭事件", exact: true }).click();
      await expect(page.getByText("已关闭 ·", { exact: false })).toBeVisible();
      if (process.env.BASEBAND_SCREENSHOT) await page.screenshot({ path: process.env.BASEBAND_SCREENSHOT, fullPage: true });
    }
    await expect(page.locator("main").getByRole("alert")).toHaveCount(0);
  }
});
