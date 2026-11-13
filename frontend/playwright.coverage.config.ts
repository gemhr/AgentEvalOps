import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e", testMatch: "coverage-execution.spec.ts", workers: 1, retries: 0, reporter: "line",
  use: { ...devices["Desktop Chrome"], channel: "msedge", baseURL: "http://localhost:3000", trace: "off" },
  webServer: { command: "corepack yarn dev --hostname 127.0.0.1 --port 3000", url: "http://localhost:3000", reuseExistingServer: false, timeout: 60000, env: { LOCAL_AGENT_API_URL: "http://127.0.0.1:18091", NEXT_PUBLIC_GO_PRODUCT_API_URL: process.env.NEXT_PUBLIC_GO_PRODUCT_API_URL ?? "" } },
});
