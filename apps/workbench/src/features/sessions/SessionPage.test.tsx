import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SessionPage } from "./SessionPage";

interface StubResponse {
  status?: number;
  body: unknown;
}

class StubWebSocket {
  static instances: StubWebSocket[] = [];

  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    StubWebSocket.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  serverOpen(): void {
    this.onopen?.();
  }

  serverClose(): void {
    if (!this.closed) {
      this.closed = true;
      this.onclose?.();
    }
  }

  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
}

type FetchHandler = (url: string, init?: RequestInit) => StubResponse;

function stubFetch(handler: FetchHandler) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const { status = 200, body } = handler(url, init);
    return {
      ok: status < 400,
      status,
      json: async () => body,
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const inputRequest = {
  id: "req-1",
  run_id: "run-1",
  field_paths: ["store_id"],
  question: "请选择操作店铺",
  options: ["淘宝旗舰店", "抖音小店"],
  status: "PENDING",
};

const confirmationRequest = {
  id: "req-2",
  run_id: "run-1",
  summary: "将 SKU-001 价格从 128.00 改为 99.00",
  normalized_arguments: { sku: "SKU-001", price: 99 },
  resource_versions: { "sku/SKU-001": "v3" },
  status: "PENDING",
};

function sessionSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    id: "session-1",
    workspace_id: "ws-brand-1",
    agent_id: "agent-1",
    title: "改价任务",
    status: "ACTIVE",
    created_by_actor_id: "user-1",
    active_run_id: "run-1",
    messages: [
      {
        id: "m-1",
        run_id: "run-1",
        role: "USER",
        status: "COMPLETED",
        content: "把 SKU-001 改成 99 元",
        sequence_no: 1,
      },
      {
        id: "m-2",
        run_id: "run-1",
        role: "ASSISTANT",
        status: "COMPLETED",
        content: "SKU-001 当前价格:99.00 元",
        sequence_no: 2,
      },
      {
        id: "m-3",
        run_id: "run-1",
        role: "TOOL",
        status: "COMPLETED",
        content: "改价成功",
        sequence_no: 3,
      },
    ],
    runs: [{ id: "run-1", status: "RUNNING", created_at: "2026-10-03T00:00:00Z" }],
    pending_inputs: [],
    pending_confirmations: [],
    ...overrides,
  };
}

function runSnapshot(status: string, eventsCursor: number, overrides: Record<string, unknown> = {}) {
  return {
    id: "run-1",
    session_id: "session-1",
    workspace_id: "ws-brand-1",
    status,
    state_version: 1,
    events_cursor: eventsCursor,
    messages: [],
    tool_calls: [],
    ...overrides,
  };
}

function decisionView(requestId: string) {
  return {
    request_id: requestId,
    kind: "INPUT",
    status: "ANSWERED",
    run_id: "run-1",
    run_status: "QUEUED",
  };
}

describe("SessionPage 计划原文语义", () => {
  it("renders input and confirmation as different interactions", async () => {
    stubFetch((url) =>
      url.endsWith("/api/sessions/session-1")
        ? {
            body: sessionSnapshot({
              runs: [{ id: "run-1", status: "WAITING_INPUT", created_at: "2026-10-03T00:00:00Z" }],
              pending_inputs: [inputRequest],
            }),
          }
        : url.includes("/api/runs/run-1")
          ? { body: runSnapshot("WAITING_INPUT", 2) }
          : { status: 404, body: {} },
    );
    vi.stubGlobal("WebSocket", StubWebSocket);

    render(<SessionPage sessionId="session-1" />);

    expect(await screen.findByText("请选择操作店铺")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认执行" })).not.toBeInTheDocument();
  });
});

describe("SessionPage 交互卡片", () => {
  beforeEach(() => {
    StubWebSocket.instances = [];
  });

  it("确认场景展示摘要与资源版本,并提供确认执行/拒绝按钮", async () => {
    stubFetch((url) =>
      url.endsWith("/api/sessions/session-1")
        ? {
            body: sessionSnapshot({
              runs: [
                {
                  id: "run-1",
                  status: "WAITING_CONFIRMATION",
                  created_at: "2026-10-03T00:00:00Z",
                },
              ],
              pending_confirmations: [confirmationRequest],
            }),
          }
        : url.includes("/api/runs/run-1")
          ? { body: runSnapshot("WAITING_CONFIRMATION", 3) }
          : { status: 404, body: {} },
    );
    vi.stubGlobal("WebSocket", StubWebSocket);

    render(<SessionPage sessionId="session-1" />);

    expect(await screen.findByRole("button", { name: "确认执行" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "拒绝" })).toBeInTheDocument();
    expect(screen.getByText("将 SKU-001 价格从 128.00 改为 99.00")).toBeInTheDocument();
    expect(screen.getByText(/sku\/SKU-001=v3/)).toBeInTheDocument();
    expect(screen.queryByText("请选择操作店铺")).not.toBeInTheDocument();
  });

  it("Input 候选按钮点击即携带声明字段调用 answer API", async () => {
    const fetchMock = stubFetch((url) =>
      url.endsWith("/api/sessions/session-1")
        ? {
            body: sessionSnapshot({
              runs: [{ id: "run-1", status: "WAITING_INPUT", created_at: "2026-10-03T00:00:00Z" }],
              pending_inputs: [inputRequest],
            }),
          }
        : url.includes("/api/runs/run-1")
          ? { body: runSnapshot("WAITING_INPUT", 2) }
          : url.endsWith("/api/requests/req-1/answer")
            ? { body: decisionView("req-1") }
            : { status: 404, body: {} },
    );
    vi.stubGlobal("WebSocket", StubWebSocket);

    render(<SessionPage sessionId="session-1" />);

    fireEvent.click(await screen.findByRole("button", { name: "淘宝旗舰店" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/requests/req-1/answer",
        expect.objectContaining({
          method: "POST",
          credentials: "include",
          body: JSON.stringify({ values: { store_id: "淘宝旗舰店" } }),
        }),
      );
    });
  });

  it("确认执行与拒绝分别调用 confirmation accept/reject API", async () => {
    const fetchMock = stubFetch((url) =>
      url.endsWith("/api/sessions/session-1")
        ? {
            body: sessionSnapshot({
              runs: [
                {
                  id: "run-1",
                  status: "WAITING_CONFIRMATION",
                  created_at: "2026-10-03T00:00:00Z",
                },
              ],
              pending_confirmations: [confirmationRequest],
            }),
          }
        : url.includes("/api/runs/run-1")
          ? { body: runSnapshot("WAITING_CONFIRMATION", 3) }
          : url.includes("/api/requests/req-2/confirmation")
            ? { body: decisionView("req-2") }
            : { status: 404, body: {} },
    );
    vi.stubGlobal("WebSocket", StubWebSocket);

    render(<SessionPage sessionId="session-1" />);

    fireEvent.click(await screen.findByRole("button", { name: "确认执行" }));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/requests/req-2/confirmation/accept",
        expect.objectContaining({ method: "POST" }),
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/requests/req-2/confirmation/reject",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });
});

describe("SessionPage 消息与运行状态", () => {
  beforeEach(() => {
    StubWebSocket.instances = [];
  });

  it("渲染用户/助手/工具结果消息与 Run 状态卡", async () => {
    stubFetch((url) =>
      url.endsWith("/api/sessions/session-1")
        ? { body: sessionSnapshot() }
        : url.includes("/api/runs/run-1")
          ? {
              body: runSnapshot("RUNNING", 4, {
                tool_calls: [
                  {
                    id: "tc-1",
                    ordinal: 1,
                    status: "SUCCEEDED",
                    raw_arguments: { sku: "SKU-001", price: 99 },
                    failure_code: null,
                  },
                ],
              }),
            }
          : { status: 404, body: {} },
    );
    vi.stubGlobal("WebSocket", StubWebSocket);

    render(<SessionPage sessionId="session-1" />);

    expect(await screen.findByText("把 SKU-001 改成 99 元")).toBeInTheDocument();
    expect(screen.getByText("SKU-001 当前价格:99.00 元")).toBeInTheDocument();
    expect(screen.getByText("改价成功")).toBeInTheDocument();
    expect(screen.getByText("运行中")).toBeInTheDocument();
    // 快照 ToolCallView 不含 tool_name,名称由 TOOL_CALL_PROPOSED 事件提供
    const toolSection = screen.getByRole("region", { name: "工具调用" });
    expect(within(toolSection).getByText("#1")).toBeInTheDocument();
    expect(within(toolSection).getByText("成功")).toBeInTheDocument();
    expect(within(toolSection).getByText(/SKU-001/)).toBeInTheDocument();
  });

  it("Run 失败时展示失败原因", async () => {
    stubFetch((url) =>
      url.endsWith("/api/sessions/session-1")
        ? {
            body: sessionSnapshot({
              active_run_id: null,
              runs: [{ id: "run-1", status: "FAILED", created_at: "2026-10-03T00:00:00Z" }],
            }),
          }
        : url.includes("/api/runs/run-1")
          ? { body: runSnapshot("FAILED", 7, { failure_code: "BACKEND_FAILED" }) }
          : { status: 404, body: {} },
    );
    vi.stubGlobal("WebSocket", StubWebSocket);

    render(<SessionPage sessionId="session-1" />);

    expect(await screen.findByText("失败")).toBeInTheDocument();
    expect(screen.getByText(/BACKEND_FAILED/)).toBeInTheDocument();
  });

  it("活动等待 Run 存在时输入编辑器禁用并提示", async () => {
    stubFetch((url) =>
      url.endsWith("/api/sessions/session-1")
        ? {
            body: sessionSnapshot({
              runs: [{ id: "run-1", status: "WAITING_INPUT", created_at: "2026-10-03T00:00:00Z" }],
              pending_inputs: [inputRequest],
            }),
          }
        : url.includes("/api/runs/run-1")
          ? { body: runSnapshot("WAITING_INPUT", 2) }
          : { status: 404, body: {} },
    );
    vi.stubGlobal("WebSocket", StubWebSocket);

    render(<SessionPage sessionId="session-1" />);

    await screen.findByText("请选择操作店铺");
    const editor = screen.getByLabelText("任务输入") as HTMLTextAreaElement;
    expect(editor).toBeDisabled();
    expect(
      screen.getByText("当前存在进行中的任务,请等待其完成后再提交新任务。"),
    ).toBeInTheDocument();
  });

  it("无活动 Run 时可提交新任务并 POST messages", async () => {
    const fetchMock = stubFetch((url) =>
      url.endsWith("/api/sessions/session-1")
        ? {
            body: sessionSnapshot({
              active_run_id: null,
              runs: [{ id: "run-1", status: "COMPLETED", created_at: "2026-10-03T00:00:00Z" }],
            }),
          }
        : url.endsWith("/api/sessions/session-1/messages")
          ? { status: 201, body: { message_id: "m-9", run_id: "run-2", status: "QUEUED" } }
          : { status: 404, body: {} },
    );
    vi.stubGlobal("WebSocket", StubWebSocket);

    render(<SessionPage sessionId="session-1" />);

    const editor = (await screen.findByLabelText("任务输入")) as HTMLTextAreaElement;
    expect(editor).not.toBeDisabled();
    fireEvent.change(editor, { target: { value: "再上架一个新品" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/sessions/session-1/messages",
        expect.objectContaining({
          method: "POST",
          credentials: "include",
          body: JSON.stringify({ content: "再上架一个新品" }),
        }),
      );
    });
  });
});

describe("SessionPage WebSocket 集成", () => {
  beforeEach(() => {
    StubWebSocket.instances = [];
  });

  it("快照加载后按 cursor 连接事件流,WS 消息驱动状态更新", async () => {
    // 终态事件会触发一次快照重取(加载最终消息):首次 RUNNING,其后 COMPLETED。
    let sessionFetchCount = 0;
    stubFetch((url) =>
      url.endsWith("/api/sessions/session-1")
        ? ((sessionFetchCount += 1),
          {
            body: sessionSnapshot(
              sessionFetchCount === 1
                ? {}
                : {
                    runs: [
                      { id: "run-1", status: "COMPLETED", created_at: "2026-10-03T00:00:00Z" },
                    ],
                  },
            ),
          })
        : url.includes("/api/runs/run-1")
          ? {
              body: runSnapshot(
                sessionFetchCount === 1 ? "RUNNING" : "COMPLETED",
                4,
              ),
            }
          : { status: 404, body: {} },
    );
    vi.stubGlobal("WebSocket", StubWebSocket);

    render(<SessionPage sessionId="session-1" />);

    const socket = await waitFor(() => {
      expect(StubWebSocket.instances).toHaveLength(1);
      return StubWebSocket.instances[0];
    });
    expect(socket.url).toContain("/api/runs/run-1/events?cursor=4");

    act(() => {
      socket.serverOpen();
      socket.emit({
        id: "e-5",
        sequence_no: 5,
        event_kind: "TOOL_CALL_PROPOSED",
        payload: { call_id: "call-1", tool_name: "update_price", ordinal: 2 },
        created_at: "2026-10-03T00:00:00Z",
      });
      socket.emit({
        id: "e-6",
        sequence_no: 6,
        event_kind: "RUN_COMPLETED",
        payload: {},
        created_at: "2026-10-03T00:00:01Z",
      });
    });

    expect(await screen.findByText("已完成")).toBeInTheDocument();
    expect(screen.getByText("update_price")).toBeInTheDocument();
    expect(screen.getByText("待执行")).toBeInTheDocument();
  });

  it("断线重连:先重新 GET 快照再按 cursor 重连 WebSocket", async () => {
    const fetchMock = stubFetch((url) =>
      url.endsWith("/api/sessions/session-1")
        ? { body: sessionSnapshot() }
        : url.includes("/api/runs/run-1")
          ? { body: runSnapshot("RUNNING", 4) }
          : { status: 404, body: {} },
    );
    vi.stubGlobal("WebSocket", StubWebSocket);

    render(<SessionPage sessionId="session-1" />);

    const first = await waitFor(() => {
      expect(StubWebSocket.instances).toHaveLength(1);
      return StubWebSocket.instances[0];
    });

    act(() => {
      first.serverClose();
    });

    await waitFor(
      () => {
        expect(StubWebSocket.instances).toHaveLength(2);
      },
      { timeout: 3000 },
    );
    await waitFor(() => {
      expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/sessions/session-1")))
        .toHaveLength(2);
    });
    expect(StubWebSocket.instances[1].url).toContain("/api/runs/run-1/events?cursor=4");
  });
});
