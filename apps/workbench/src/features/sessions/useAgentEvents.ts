import { useCallback, useEffect, useRef, useState } from "react";

import type { AgentEventProjection } from "./eventReducer";
import { projectionFromSnapshot, reduceAgentEvent } from "./eventReducer";
import { TERMINAL_RUN_STATUSES, type AgentEvent, type SessionSnapshot } from "./types";

/**
 * useAgentEvents(计划 04 Task 10 Step 3):
 *
 * - 挂载/重连时先 GET 快照,projectionFromSnapshot 重建投影;
 * - 有活动 Run 时建立 WS /api/runs/{run_id}/events?cursor={events_cursor};
 * - 事件经 reduceAgentEvent 维护投影;needsSnapshot(缺口/等待详情)时
 *   关闭连接、重新取快照并以新 cursor 重连;
 * - 断线后按简单退避重连,重连同样先取快照再从 cursor 重放;
 * - WebSocket 构造器可注入,便于测试(fake WebSocket)。
 */

export interface AgentEventSocket {
  onopen: (() => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
  close(): void;
}

export type AgentSocketFactory = (url: string) => AgentEventSocket;

export interface UseAgentEventsOptions {
  sessionId: string;
  /** 加载会话快照(Session + Run 快照合并)。 */
  loadSnapshot: () => Promise<SessionSnapshot>;
  /** 测试注入;缺省使用全局 WebSocket。 */
  socketFactory?: AgentSocketFactory;
  /** 重连退避(毫秒);参数为第几次重试,从 1 开始。 */
  reconnectDelayMs?: (attempt: number) => number;
}

export interface UseAgentEventsResult {
  projection: AgentEventProjection | null;
  connectionError: string | null;
  /** 命令成功后手动刷新快照(发送消息/回答/裁决)。 */
  reload: () => Promise<void>;
  /** Workspace 执行条件(来自 Session 快照);``ready`` 为 null 表示不适用。 */
  workspaceReadiness: WorkspaceReadiness;
}

/** 快照里的执行条件;真实模型入口才有意义,演示入口恒为 null。 */
export interface WorkspaceReadiness {
  ready: boolean | null;
  reason: string | null;
}

interface WireEvent {
  id?: unknown;
  sequence_no?: unknown;
  event_kind?: unknown;
  payload?: unknown;
  created_at?: unknown;
}

const defaultReconnectDelayMs = (attempt: number): number =>
  Math.min(500 * 2 ** (attempt - 1), 4000);

function buildEventsUrl(runId: string, cursor: number | null): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const query = cursor === null ? "" : `?cursor=${cursor}`;
  return `${protocol}//${window.location.host}/api/runs/${runId}/events${query}`;
}

function toAgentEvent(wire: WireEvent, runId: string): AgentEvent | null {
  if (
    typeof wire.id !== "string" ||
    typeof wire.sequence_no !== "number" ||
    typeof wire.event_kind !== "string"
  ) {
    return null;
  }
  return {
    id: wire.id,
    runId,
    sequenceNo: wire.sequence_no,
    kind: wire.event_kind,
    payload: (wire.payload ?? {}) as Record<string, unknown>,
    createdAt: typeof wire.created_at === "string" ? wire.created_at : undefined,
  };
}

export function useAgentEvents({
  sessionId,
  loadSnapshot,
  socketFactory,
  reconnectDelayMs,
}: UseAgentEventsOptions): UseAgentEventsResult {
  const [projection, setProjection] = useState<AgentEventProjection | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [workspaceReadiness, setWorkspaceReadiness] = useState<WorkspaceReadiness>({
    ready: null,
    reason: null,
  });

  const projectionRef = useRef<AgentEventProjection | null>(null);
  const socketRef = useRef<AgentEventSocket | null>(null);
  const disposedRef = useRef(false);
  const attemptRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const refreshRef = useRef<() => Promise<void>>(async () => {});

  // 每次渲染刷新最新回调,避免 WS 处理器闭包过期。
  // (render 期间不可写 ref:经 effect 同步,事件回调读取时已是最新值。)
  const optionsRef = useRef({ loadSnapshot, socketFactory, reconnectDelayMs });
  useEffect(() => {
    optionsRef.current = { loadSnapshot, socketFactory, reconnectDelayMs };
  });

  const closeSocket = useCallback(() => {
    const socket = socketRef.current;
    socketRef.current = null;
    if (!socket) {
      return;
    }
    // 先摘掉处理器再 close:主动关闭不触发重连。
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    socket.close();
  }, []);

  const scheduleRetry = useCallback(() => {
    if (disposedRef.current || timerRef.current !== null) {
      return;
    }
    attemptRef.current += 1;
    const delay = (optionsRef.current.reconnectDelayMs ?? defaultReconnectDelayMs)(
      attemptRef.current,
    );
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      void refreshRef.current();
    }, delay);
  }, []);

  const connect = useCallback(
    (current: AgentEventProjection) => {
      if (disposedRef.current || !current.activeRunId) {
        return;
      }
      const runId = current.activeRunId;
      const factory =
        optionsRef.current.socketFactory ??
        ((url: string) => new WebSocket(url) as unknown as AgentEventSocket);
      let socket: AgentEventSocket;
      try {
        socket = factory(buildEventsUrl(runId, current.cursor));
      } catch (cause) {
        setConnectionError(cause instanceof Error ? cause.message : String(cause));
        scheduleRetry();
        return;
      }
      socketRef.current = socket;

      /* eslint-disable react-hooks/immutability -- WebSocket 事件回调挂载在
       -- 既有 socket 实例上是 WS 客户端的标准用法,不是对共享可变状态的修改。 */
      socket.onopen = () => {
        attemptRef.current = 0;
        setConnectionError(null);
      };
      socket.onmessage = (message) => {
        let wire: WireEvent;
        try {
          wire = JSON.parse(String(message.data)) as WireEvent;
        } catch {
          return;
        }
        const agentEvent = toAgentEvent(wire, runId);
        const previous = projectionRef.current;
        if (!agentEvent || !previous) {
          return;
        }
        const next = reduceAgentEvent(previous, agentEvent);
        projectionRef.current = next;
        setProjection(next);
        if (next.needsSnapshot) {
          // 缺口/等待详情:重新取快照并以新 cursor 重连。
          void refreshRef.current();
        } else if (
          TERMINAL_RUN_STATUSES.includes(next.runs[agentEvent.runId]?.status ?? "")
        ) {
          // 终态:重新取快照以加载最终消息(如写操作回执),不再重连事件流。
          void refreshRef.current();
        }
      };
      socket.onclose = () => {
        if (disposedRef.current || socketRef.current !== socket) {
          return;
        }
        socketRef.current = null;
        setConnectionError("事件连接已断开,正在重新同步…");
        scheduleRetry();
      };
      socket.onerror = () => {
        // onerror 后通常跟随 onclose,由 onclose 统一处理。
      };
      /* eslint-enable react-hooks/immutability */
    },
    [scheduleRetry],
  );

  const refreshSnapshot = useCallback(async () => {
    closeSocket();
    try {
      const snapshot = await optionsRef.current.loadSnapshot();
      if (disposedRef.current) {
        return;
      }
      // 重建时带入旧投影(保留事件已知的工具名等知识);快照事实覆盖之。
      const rebuilt = projectionFromSnapshot(snapshot, projectionRef.current);
      projectionRef.current = rebuilt;
      setProjection(rebuilt);
      setWorkspaceReadiness({
        ready: snapshot.model_ready ?? null,
        reason: snapshot.model_blocked_reason ?? null,
      });
      setConnectionError(null);
      attemptRef.current = 0;
      connect(rebuilt);
    } catch (cause) {
      if (disposedRef.current) {
        return;
      }
      setConnectionError(cause instanceof Error ? cause.message : String(cause));
      scheduleRetry();
    }
  }, [closeSocket, connect, scheduleRetry]);

  useEffect(() => {
    disposedRef.current = false;
    refreshRef.current = refreshSnapshot;
    void refreshSnapshot();
    return () => {
      disposedRef.current = true;
      refreshRef.current = async () => {};
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      closeSocket();
    };
  }, [sessionId, refreshSnapshot, closeSocket]);

  const reload = useCallback(() => refreshRef.current(), []);

  return { projection, connectionError, reload, workspaceReadiness };
}
