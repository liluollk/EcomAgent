import type { Page, Route, WebSocketRoute } from "@playwright/test";

/**
 * E2E 运行时夹具(计划 04 Task 11)。
 *
 * - 真实后端路径:POST /api/e2e/seed 幂等种子(RUNTIME_E2E=1 时注册),
 *   UI 登录 → 选工作区 → 提交任务,全链路走 compose 启动的 api/worker;
 * - 纯前端路径:page.route + routeWebSocket 拦截,按计划语义注入
 *   快照与事件(缺口/去重/终态测试不依赖后端)。
 */

const API_BASE = process.env.RUNTIME_API_URL ?? "http://localhost:8000";

export const E2E_LOGIN = "e2e-operator";
export const E2E_PASSWORD = "e2e-password";

export interface E2ESeed {
  workspace_id: string;
  workspace_name: string;
  agent_id: string;
  store_id: string;
  store_name: string;
  sku: string;
  listing_id: string;
  initial_price: string;
  login: string;
  password: string;
}

/** 幂等种子:每次运行都复位到确定初始状态(128.00)。 */
export async function seedRuntime(): Promise<E2ESeed> {
  const response = await fetch(`${API_BASE}/api/e2e/seed`, { method: "POST" });
  if (!response.ok) {
    throw new Error(`e2e seed failed: ${response.status} ${await response.text()}`);
  }
  return (await response.json()) as E2ESeed;
}

/** UI 登录并打开指定工作区。 */
export async function loginAndOpenWorkspace(page: Page, workspaceName: string): Promise<void> {
  await page.goto("/");
  await page.getByLabel("登录名").fill(E2E_LOGIN);
  await page.getByLabel("密码").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();
  await page.getByRole("button", { name: workspaceName }).click();
}

/** 在工作区页提交新任务,进入会话页。 */
export async function submitTask(page: Page, content: string): Promise<void> {
  await page.getByRole("textbox").fill(content);
  await page.getByRole("button", { name: "发送任务" }).click();
}

export interface SeedSnapshotOptions {
  runId: string;
  lastSequenceNo: number;
  status: string;
  sessionId?: string;
}

export interface PushEventOptions {
  id: string;
  runId: string;
  sequenceNo: number;
  type: string;
}

interface SnapshotState {
  lastSequenceNo: number;
  status: string;
  active: boolean;
}

/**
 * 纯前端夹具:拦截 Session/Run 快照与事件 WebSocket。
 * `snapshotRequestCount` 统计 Run 快照被拉取的次数(缺口/终态触发重取)。
 */
export class RuntimeFixture {
  private readonly page: Page;
  private readonly sessionId: string;
  private readonly runId: string;
  private readonly snapshots = new Map<string, SnapshotState>();
  private readonly runSnapshotRequests = new Map<string, number>();
  private readonly sockets: WebSocketRoute[] = [];

  private constructor(page: Page, sessionId: string, runId: string) {
    this.page = page;
    this.sessionId = sessionId;
    this.runId = runId;
  }

  static async attach(page: Page): Promise<RuntimeFixture> {
    const fixture = new RuntimeFixture(page, "session-1", "run-1");
    await fixture.installRoutes();
    return fixture;
  }

  private async installRoutes(): Promise<void> {
    const page = this.page;
    // 登录态:注入 sessionStorage 令牌后直接深链进入会话页。
    await page.addInitScript(() => {
      window.sessionStorage.setItem("workbench.token", "e2e-mock-token");
    });

    const sessionPattern = `**/api/sessions/${this.sessionId}`;
    const runPattern = `**/api/runs/${this.runId}`;

    await page.route(sessionPattern, (route: Route) =>
      route.fulfill({ json: this.sessionSnapshot() }),
    );
    await page.route(runPattern, (route: Route) => {
      const key = this.runId;
      this.runSnapshotRequests.set(key, (this.runSnapshotRequests.get(key) ?? 0) + 1);
      route.fulfill({ json: this.runSnapshot() });
    });
    await page.routeWebSocket(new RegExp(`/api/runs/${this.runId}/events.*`), (ws: WebSocketRoute) => {
      this.sockets.push(ws);
    });
    await page.route("**/api/agents", (route: Route) => route.fulfill({ json: { agents: [] } }));
  }

  seedSnapshot(options: SeedSnapshotOptions): void {
    this.snapshots.set(options.runId, {
      lastSequenceNo: options.lastSequenceNo,
      status: options.status,
      active: options.status !== "COMPLETED",
    });
  }

  /** 向(可能已重连的)事件流推送一条事件;客户端已断开时为无操作。 */
  pushEvent(event: PushEventOptions): void {
    const socket = this.sockets.at(-1);
    socket?.send(JSON.stringify({
      id: event.id,
      sequence_no: event.sequenceNo,
      event_kind: event.type,
      payload: {},
      created_at: "2026-10-05T00:00:00Z",
    }));
  }

  snapshotRequestCount(runId: string): number {
    return this.runSnapshotRequests.get(runId) ?? 0;
  }

  private currentState(): SnapshotState {
    return (
      this.snapshots.get(this.runId) ?? { lastSequenceNo: 0, status: "RUNNING", active: true }
    );
  }

  private sessionSnapshot(): Record<string, unknown> {
    const state = this.currentState();
    return {
      id: this.sessionId,
      title: null,
      status: "ACTIVE",
      created_by_actor_id: "00000000-0000-0000-0000-0000000000e2",
      active_run_id: state.active ? this.runId : null,
      runs: [
        {
          id: this.runId,
          status: state.status,
          events_cursor: state.lastSequenceNo,
        },
      ],
      messages: [
        {
          id: "m-1",
          run_id: this.runId,
          role: "USER",
          status: "COMPLETED",
          content: "把 SKU-001 改成 99 元",
          sequence_no: 1,
        },
      ],
    };
  }

  private runSnapshot(): Record<string, unknown> {
    const state = this.currentState();
    return {
      id: this.runId,
      session_id: this.sessionId,
      status: state.status,
      state_version: 1,
      failure_code: null,
      events_cursor: state.lastSequenceNo,
      messages: [],
      tool_calls: [],
      pending_inputs: [],
      pending_confirmations: [],
    };
  }
}
