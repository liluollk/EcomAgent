import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";

// 先运行 npm run dev；仅截取真实渲染的演示页面，不修改页面状态或图片内容。
const baseURL = process.env.WORKBENCH_URL ?? "http://127.0.0.1:5173";
const output = fileURLToPath(new URL("../../../docs/images/", import.meta.url));
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const errors = [];
const apiRequests = [];
try {
  const page = await browser.newPage({
    viewport: { width: 1600, height: 1100 },
    deviceScaleFactor: 1,
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/"))
      apiRequests.push(request.url());
  });
  await page.goto(`${baseURL}/?demo=1`);
  await page
    .getByRole("heading", { name: "运营工作台", exact: true })
    .waitFor();
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({
    path: `${output}/workbench-overview.png`,
    fullPage: true,
  });
  await page.getByRole("button", { name: "查看待确认任务" }).click();
  await page.getByRole("button", { name: "确认执行", exact: true }).waitFor();
  await page.screenshot({
    path: `${output}/workbench-confirmation.png`,
    fullPage: true,
  });
  await page.getByRole("button", { name: "拒绝", exact: true }).click();
  await page
    .getByRole("status")
    .filter({ hasText: "示例价格保持 ¥99.00" })
    .waitFor();
  await page.getByRole("button", { name: "重新演示", exact: true }).click();
  await page.getByRole("button", { name: "确认执行", exact: true }).click();
  await page
    .getByRole("status")
    .filter({ hasText: "当前价格 ¥88.00" })
    .waitFor();
  await page
    .getByRole("button", { name: "执行记录", exact: true })
    .first()
    .click();
  await page
    .getByRole("heading", { name: "每一步，都有记录", exact: true })
    .waitFor();

  const layouts = [];
  for (const width of [390, 768, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    for (const label of ["工作台", "任务会话", "执行记录"]) {
      await page
        .getByRole("navigation", { name: "演示导航" })
        .getByRole("button", { name: label })
        .click();
      const layout = await page.evaluate(() => ({
        viewport: innerWidth,
        content: document.documentElement.scrollWidth,
      }));
      if (layout.content > layout.viewport)
        throw new Error(`${label} 在 ${width}px 发生页面横向溢出`);
      layouts.push(`${width}px ${label}`);
    }
  }
  if (errors.length || apiRequests.length)
    throw new Error(JSON.stringify({ errors, apiRequests }));
  await page.getByRole("link", { name: "进入真实工作台" }).click();
  await page.getByRole("textbox", { name: "登录名" }).waitFor();
  console.log(
    JSON.stringify(
      {
        screenshots: ["workbench-overview.png", "workbench-confirmation.png"],
        layouts,
        pageErrors: errors,
        demoApiRequests: apiRequests,
        loginEntry: "通过",
      },
      null,
      2,
    ),
  );
} finally {
  await browser.close();
}
