import { expect, test } from "@playwright/test";

import { loginAndOpenWorkspace, seedRuntime, submitTask } from "./fixtures/runtime";
import { RuntimeFixture } from "./fixtures/runtime";

/**
 * 工作台关键 E2E(计划 04 Task 11;retries=0,无重试后侥幸通过)。
 *
 * - 测试 1 走真实后端(compose api/worker,RUNTIME_E2E=1 的 ScriptedBackend
 *   与 Mock 平台),覆盖登录 → 任务 → 缺店铺 Input → 确认 → 改价 → 重连回读;
 * - 测试 2 用路由拦截注入快照与事件,覆盖事件缺口触发快照重取、
 *   终态不被迟到事件逆转。
 */

test("resumes a price update after input, confirmation, and reconnect", async ({ page }) => {
  const seed = await seedRuntime();
  await loginAndOpenWorkspace(page, seed.workspace_name);
  await submitTask(page, `把 ${seed.sku} 改成 99 元`);

  // 缺店铺:PreToolUse 产生 Input Request,候选限定授权店铺。
  await page.getByRole("button", { name: seed.store_name }).click();

  // 写前确认:确认后 Runtime 推进执行,Mock 平台真实落库。
  await page.getByRole("button", { name: "确认执行" }).click();

  // 断线重连等价路径:整页刷新后由快照恢复会话状态与最终权威价。
  await page.reload();
  await expect(page.getByText(`${seed.sku} 当前价格：99.00 元`)).toBeVisible();
});

test("reloads a snapshot when an event sequence has a gap", async ({ page }) => {
  const runtime = await RuntimeFixture.attach(page);
  runtime.seedSnapshot({ runId: "run-1", lastSequenceNo: 4, status: "RUNNING" });
  await page.goto("/?session=session-1");

  // 等待首条事件连接建立后再推进事件序。
  await page.waitForTimeout(500);
  runtime.pushEvent({ id: "e-5", runId: "run-1", sequenceNo: 5, type: "RUN_COMPLETED" });
  runtime.pushEvent({ id: "e-7", runId: "run-1", sequenceNo: 7, type: "RUN_RUNNING" });

  // 初始快照 1 次 + 终态/缺口触发的快照重取 1 次;终态不被迟到事件逆转。
  await expect.poll(() => runtime.snapshotRequestCount("run-1")).toBe(2);
  await expect(page.getByText("已完成")).toBeVisible();
});
