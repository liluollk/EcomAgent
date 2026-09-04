/**
 * API 抽象层 — 统一分发到真实后端 fetch 或展示版 mock 数据。
 *
 * 开关：import.meta.env.VITE_OBS_MOCK（默认开，即展示版独立跑 mock）；
 * 设 VITE_OBS_MOCK=0 后，本层走真实 fetch（对接项目根后端，接口形状一致）。
 * 返回结构刻意模仿 fetch Response 的子集 { ok, json() }，组件改动最小。
 */

import { mockRequest } from '../mocks/apiMock';

/** mock 开关：VITE_OBS_MOCK !== '0' 时启用（默认展示版用 mock） */
export const USE_MOCK: boolean = (import.meta.env.VITE_OBS_MOCK ?? '1') !== '0';

interface ApiLike {
  ok: boolean;
  status?: number;
  json(): Promise<any>;
}

async function liveFetch(url: string, init?: RequestInit): Promise<ApiLike> {
  const resp = await fetch(url, init);
  return { ok: resp.ok, status: resp.status, json: () => resp.json() };
}

/**
 * 请求入口：mock 模式下走内存路由表，否则发真实 fetch。
 * 组件侧保持 `const resp = await api(...)` 的既有形态。
 */
export function api(url: string, init?: RequestInit): Promise<ApiLike> {
  if (USE_MOCK) {
    const method = (init?.method ?? 'GET').toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    return mockRequest(method, url, body);
  }
  return liveFetch(url, init);
}