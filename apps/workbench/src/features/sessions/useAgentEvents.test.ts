import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SessionSnapshot } from "./types";
import { useAgentEvents } from "./useAgentEvents";

class FakeSocket {
  static instances: FakeSocket[] = [];

  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
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

function snapshotFor(eventsCursor: number, status = "RUNNING"): SessionSnapshot {
  return {
    id: "session-1",
    title: null,
    status: "ACTIVE",
    active_run_id: "run-1",
    messages: [],
    runs: [{ id: "run-1", status, events_cursor: eventsCursor }],
    pending_inputs: [],
    pending_confirmations: [],
  };
}

function wireEvent(id: string, sequenceNo: number, kind: string) {
  return { id, sequence_no: sequenceNo, event_kind: kind, payload: {}, created_at: "2026-10-03T00:00:00Z" };
}

describe("useAgentEvents", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
  });

  it("先取快照再按 cursor 建立 WebSocket 连接", async () => {
    const loadSnapshot = vi.fn(async () => snapshotFor(4));
    const { result } = renderHook(() =>
      useAgentEvents({
        sessionId: "session-1",
        loadSnapshot,
        socketFactory: (url) => new FakeSocket(url),
      }),
    );

    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    expect(loadSnapshot).toHaveBeenCalledTimes(1);
    expect(FakeSocket.instances[0].url).toContain("/api/runs/run-1/events?cursor=4");
    await waitFor(() => expect(result.current.projection).not.toBeNull());
    expect(result.current.projection?.cursor).toBe(4);
  });

  it("WS 事件驱动 reducer 投影并按 id 去重", async () => {
    // 终态事件会触发一次快照重取(加载最终消息):第二次快照 cursor 已是 5。
    const loadSnapshot = vi
      .fn<() => Promise<SessionSnapshot>>()
      .mockResolvedValueOnce(snapshotFor(4))
      .mockResolvedValueOnce(snapshotFor(5, "COMPLETED"));
    const { result } = renderHook(() =>
      useAgentEvents({
        sessionId: "session-1",
        loadSnapshot,
        socketFactory: (url) => new FakeSocket(url),
      }),
    );

    const socket = await waitFor(() => {
      expect(FakeSocket.instances).toHaveLength(1);
      return FakeSocket.instances[0];
    });
    act(() => {
      socket.serverOpen();
      socket.emit(wireEvent("e-5", 5, "RUN_COMPLETED"));
      // 同一事件重复推送(at-least-once):必须被去重
      socket.emit(wireEvent("e-5", 5, "RUN_COMPLETED"));
    });

    await waitFor(() =>
      expect(result.current.projection?.runs["run-1"].status).toBe("COMPLETED"),
    );
    expect(result.current.projection?.runs["run-1"].lastSequenceNo).toBe(5);
    expect(result.current.projection?.cursor).toBe(5);
    // 初始 1 次 + 终态回读 1 次;重复事件不得再触发。
    await waitFor(() => expect(loadSnapshot).toHaveBeenCalledTimes(2));
  });

  it("事件缺口触发快照重建并以新 cursor 重连", async () => {
    const loadSnapshot = vi
      .fn<() => Promise<SessionSnapshot>>()
      .mockResolvedValueOnce(snapshotFor(4))
      .mockResolvedValueOnce(snapshotFor(6));
    const { result } = renderHook(() =>
      useAgentEvents({
        sessionId: "session-1",
        loadSnapshot,
        socketFactory: (url) => new FakeSocket(url),
        reconnectDelayMs: () => 5,
      }),
    );

    const first = await waitFor(() => {
      expect(FakeSocket.instances).toHaveLength(1);
      return FakeSocket.instances[0];
    });
    act(() => {
      first.serverOpen();
      first.emit(wireEvent("e-5", 5, "RUN_COMPLETED"));
      // 序号跳变 5 → 7:缺口
      first.emit(wireEvent("e-7", 7, "RUN_RUNNING"));
    });

    await waitFor(() => expect(loadSnapshot).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(2));
    expect(FakeSocket.instances[1].url).toContain("/api/runs/run-1/events?cursor=6");
    await waitFor(() => expect(result.current.projection?.cursor).toBe(6));
    expect(result.current.projection?.needsSnapshot).toBe(false);
  });

  it("断线后先重新取快照再重连 WebSocket", async () => {
    const loadSnapshot = vi.fn(async () => snapshotFor(4));
    const { result } = renderHook(() =>
      useAgentEvents({
        sessionId: "session-1",
        loadSnapshot,
        socketFactory: (url) => new FakeSocket(url),
        reconnectDelayMs: () => 5,
      }),
    );

    const first = await waitFor(() => {
      expect(FakeSocket.instances).toHaveLength(1);
      return FakeSocket.instances[0];
    });
    act(() => {
      first.serverClose();
    });

    await waitFor(() => expect(loadSnapshot).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(2));
    expect(FakeSocket.instances[1].url).toContain("/api/runs/run-1/events?cursor=4");
    await waitFor(() => expect(result.current.connectionError).toBeNull());
  });

  it("无活动 Run 时不建立 WebSocket,仅渲染快照", async () => {
    const loadSnapshot = vi.fn(async () => ({
      ...snapshotFor(9, "COMPLETED"),
      active_run_id: null,
    }));
    const { result } = renderHook(() =>
      useAgentEvents({
        sessionId: "session-1",
        loadSnapshot,
        socketFactory: (url) => new FakeSocket(url),
      }),
    );

    await waitFor(() => expect(result.current.projection).not.toBeNull());
    expect(result.current.projection?.runs["run-1"].status).toBe("COMPLETED");
    expect(FakeSocket.instances).toHaveLength(0);
  });

  it("快照加载失败时报告错误并按退避重试", async () => {
    const loadSnapshot = vi
      .fn<() => Promise<SessionSnapshot>>()
      .mockRejectedValueOnce(new Error("API 请求失败:500"))
      .mockResolvedValue(snapshotFor(4));
    const { result } = renderHook(() =>
      useAgentEvents({
        sessionId: "session-1",
        loadSnapshot,
        socketFactory: (url) => new FakeSocket(url),
        reconnectDelayMs: () => 150,
      }),
    );

    await waitFor(() => expect(result.current.connectionError).toContain("500"));
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    await waitFor(() => expect(result.current.connectionError).toBeNull());
  });
});
