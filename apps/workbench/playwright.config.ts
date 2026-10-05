import { defineConfig } from "@playwright/test";

/**
 * Workbench E2E(阶段 4 Gate;计划 04 Task 11)。
 *
 * - retries 固定为 0:计划要求“无重试后侥幸通过”,失败即失败;
 * - webServer 启动 vite dev(同源 /api 代理到本地 API,由 RUNTIME_API_URL
 *   指定,默认 http://localhost:8000,即 docker compose 暴露的 api 服务);
 * - chromium 单浏览器;workers=1 保证种子数据不被并发测试干扰。
 */
const PORT = 5173;

export default defineConfig({
  testDir: "./e2e",
  retries: 0,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "off",
  },
  webServer: {
    command: `npm run dev -- --port ${PORT} --strictPort --host 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      RUNTIME_API_URL: process.env.RUNTIME_API_URL ?? "http://localhost:8000",
    },
  },
});
