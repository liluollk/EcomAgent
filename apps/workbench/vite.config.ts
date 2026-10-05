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
  },
});
