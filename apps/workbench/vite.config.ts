import process from "node:process";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 本地 API(compose 暴露的 8000 端口);可用 RUNTIME_API_URL 覆盖。
const apiTarget = process.env.RUNTIME_API_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // 工作台与事件 WebSocket 同源走 /api,vite dev 代理到本地 API。
      "/api": {
        target: apiTarget,
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // Playwright E2E 用例(e2e/*.spec.ts)由 `npm run test:e2e` 执行,不进入
    // Vitest 收集范围;其余保持 Vitest 默认排除不变。
    exclude: [
      "**/node_modules/**",
      "**/dist/**",
      "**/cypress/**",
      "**/.{idea,git,cache,output,temp}/**",
      "**/{karma,rollup,webpack,vite,vitest,jest,ava,babel,nyc,cypress,tsup,build,eslint,prettier}.config.*",
      "e2e/**",
    ],
  },
});
