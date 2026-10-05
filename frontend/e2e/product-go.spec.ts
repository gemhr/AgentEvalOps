import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

const path = process.env.G8_E2E_FIXTURE_PATH;
test.skip(!path, "显式真实 Go/PG fixture 才运行 G8 E2E");
const fixture = path ? JSON.parse(readFileSync(path, "utf8")) : null;
test.describe.configure({ mode: "serial" });
async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("开发登录密码").fill(fixture.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("link", { name: fixture.project, exact: true }).click();
  await expect(page.getByRole("heading", { name: "项目概览" })).toBeVisible();
}
async function area(page: Page, name: string) {
  await page.getByRole("link", { name, exact: true }).click();
}
test("F01 Login/dev auth → Project", async ({ page }) => {
  await login(page);
  await expect(
    page.getByText("CONTROLLED_DEV_AUTH", { exact: true }),
  ).toBeVisible();
});
test("F02 Dataset browse", async ({ page }) => {
  await login(page);
  await area(page, "数据集");
  await page.getByRole("button", { name: "G6 dataset", exact: true }).click();
  await expect(page.getByText("不可变版本", { exact: true })).toBeVisible();
});
test("F03 Experiment/Run read", async ({ page }) => {
  await login(page);
  await area(page, "运行");
  await page.getByLabel("资源 ID").fill(fixture.run);
  await expect(
    page.getByRole("heading", { name: "Pipeline State（流水线状态）" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Task Success / Evaluation Verdict（独立展示）",
    }),
  ).toBeVisible();
});
test("F04 Gate PASS/FAIL/BLOCKED display", async ({ page }) => {
  await login(page);
  await area(page, "发布门禁");
  for (const decision of ["PASS", "FAIL", "BLOCKED"]) {
    await page.getByLabel("资源 ID").fill(fixture.gates[decision]);
    await expect(page.getByTestId("gate-decision")).toContainText(decision);
  }
  await expect(page.getByTestId("gate-decision")).toContainText(
    "证据或可比性不足",
  );
});
test("F05 Review claim / submit", async ({ page }) => {
  await login(page);
  await area(page, "人工评审");
  await page.getByLabel("资源 ID").fill(fixture.review);
  await page.getByRole("button", { name: "领取评审", exact: true }).click();
  await page.getByLabel("人工结论").selectOption("SUCCESS");
  await page.getByLabel("判断理由").fill("受控用户独立核对证据");
  await page.getByRole("button", { name: "提交独立判断", exact: true }).click();
  await expect(
    page.getByText("已经提交，本次决定不会再次提交。", { exact: true }),
  ).toBeVisible();
});
test("F06 blind review JSON leakage", async ({ page }) => {
  await login(page);
  const responsePromise = page.waitForResponse(
    (r) =>
      r.url().endsWith(`/reviews/${fixture.review}`) &&
      r.request().method() === "GET",
  );
  await area(page, "人工评审");
  await page.getByLabel("资源 ID").fill(fixture.review);
  const response = await responsePromise;
  const body = await response.json();
  expect(body.automatic_decision).toBeUndefined();
  expect(body.annotations).toBeUndefined();
  expect(JSON.stringify(body)).not.toContain("ProviderCall");
});
test("F07 disagreement → authorized adjudication", async ({ page }) => {
  await login(page);
  await area(page, "人工评审");
  await page.getByLabel("资源 ID").fill(fixture.adjudication_review);
  await page.getByRole("button", { name: "请求裁决视图", exact: true }).click();
  await expect(page.getByTestId("adjudication-annotations")).toBeVisible();
  const details = page
    .locator("details")
    .filter({ has: page.locator("summary", { hasText: "提交裁决" }) });
  await details.locator("summary").click();
  await details.getByLabel("提交裁决内容").fill(
    JSON.stringify({
      decision: {
        kind: "TASK_SUCCESS",
        value: "SUCCESS",
        reason: "核对双方证据",
        evidence_refs: fixture.adjudication_evidence_refs,
      },
      reason: "第三人裁决",
    }),
  );
  await details.getByRole("button", { name: "提交裁决", exact: true }).click();
  await expect(details.getByText(/命令已提交/)).toBeVisible();
});
test("F08 Calibration coverage/confusion", async ({ page }) => {
  await login(page);
  await area(page, "校准");
  await page.getByLabel("资源 ID").fill(fixture.calibration);
  await expect(
    page.getByRole("heading", {
      name: "Calibration（N、paired decidable、coverage、agreement、confusion）",
    }),
  ).toBeVisible();
  await expect(page.getByText(/"MissingHuman": 9/)).toBeVisible();
  await expect(page.getByText(/"Confusion"/)).toBeVisible();
});
test("F09 explicit Dataset feedback publish", async ({ page }) => {
  await login(page);
  await area(page, "反馈草稿");
  await page.getByLabel("资源 ID").fill(fixture.draft);
  const publish = page.locator("details").filter({
    has: page.locator("summary", { hasText: "显式发布审核后的用例" }),
  });
  await publish.locator("summary").click();
  await publish
    .getByRole("button", { name: "显式发布审核后的用例", exact: true })
    .click();
  await expect(publish.getByText(/命令已提交/)).toBeVisible();
  const dataset = page.locator("details").filter({
    has: page.locator("summary", { hasText: "显式构建 Dataset Revision" }),
  });
  await dataset.locator("summary").click();
  await dataset.getByLabel("显式构建 Dataset Revision内容").fill(
    JSON.stringify({
      base: fixture.dataset,
      target: { entity_id: fixture.dataset.entity_id, version: "e2e-new" },
      draft_ids: [fixture.draft],
    }),
  );
  await dataset
    .getByRole("button", { name: "显式构建 Dataset Revision", exact: true })
    .click();
  await expect(dataset.getByText(/命令已提交/)).toBeVisible();
});
test("F10 revoked project session denied", async ({ page, request }) => {
  await login(page);
  await request.post(`${fixture.api_url}/_test/revoke-session`, {
    headers: { "X-Control": fixture.control },
  });
  await area(page, "运行");
  await expect(page.locator('p[role="alert"]').filter({hasText:"NOT_FOUND"})).toContainText("NOT_FOUND");
});
