import { useCallback, useEffect, useRef, useState } from 'react';
import type { AgentEvent, PermissionModeType } from '../types';
import { USE_MOCK } from '../lib/api';
import { useMockWebSocket } from './useMockWebSocket';

interface UseWebSocketOptions {
  sessionId: string | null;
  onEvent: (event: AgentEvent) => void;
}

/**
 * 会话 WebSocket 连接（展示版统一入口）。
 *
 * mock 模式（默认）：内部用 MockEventStream 模拟事件流，connected 恒 true，
 * 无需真实后端即可完整交互；VITE_OBS_MOCK=0 时走下方真实 WS 实现。
 * 两种实现的返回接口完全一致，App 层零改动。
 */
export function useWebSocket(options: UseWebSocketOptions) {
  if (USE_MOCK) {
    return useMockWebSocket(options);
  }
  return useLiveWebSocket(options);
}

/**
 * 真实 WebSocket 连接：
 * - sessionId 变化时自动断开旧连接并连接新会话；
 * - 异常断开后按 2s → 10s 退避自动重连（仅当前会话）；
 * - onEvent 通过 ref 转发，回调变化不会触发重连。
 */
function useLiveWebSocket({ sessionId, onEvent }: UseWebSocketOptions) {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const timerRef = useRef<number | null>(null);
  const retryRef = useRef(0);
  const onEventRef = useRef(onEvent);

  useEffect(() => {
    onEventRef.current = onEvent;
  }, [onEvent]);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const connect = useCallback(() => {
    clearTimer();
    if (!sessionId) {
      setConnected(false);
      setReconnecting(false);
      return;
    }

    /* 主动丢弃旧连接：旧 socket 的 onclose 会因 ref 已替换而被忽略 */
    wsRef.current?.close();
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws/${sessionId}`);
    wsRef.current = ws;

    ws.onopen = () => {
      retryRef.current = 0;
      setConnected(true);
      setReconnecting(false);
    };

    ws.onmessage = (evt) => {
      try {
        onEventRef.current(JSON.parse(evt.data) as AgentEvent);
      } catch {
        console.warn('无法解析 WebSocket 消息:', evt.data);
      }
    };

    ws.onclose = () => {
      if (wsRef.current !== ws) return; // 已被新连接替换
      setConnected(false);
      const delay = Math.min(2000 * 1.5 ** retryRef.current, 10_000);
      retryRef.current += 1;
      setReconnecting(true);
      timerRef.current = window.setTimeout(() => {
        if (wsRef.current === ws || wsRef.current === null) connect();
      }, delay);
    };
  }, [sessionId, clearTimer]);

  useEffect(() => {
    connect();
    return () => {
      clearTimer();
      wsRef.current?.close();
    };
  }, [connect, clearTimer]);

  const sendMessage = useCallback((content: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'message', content }));
    }
  }, []);

  const sendAbort = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'abort' }));
    }
  }, []);

  /** 回应权限确认：批准或拒绝某个挂起的工具调用 */
  const respondPermission = useCallback((requestId: string, approved: boolean) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'permission_response', request_id: requestId, approved }));
    }
  }, []);

  /** 切换会话权限模式：后端更新并广播 mode_change */
  const sendModeChange = useCallback((mode: PermissionModeType) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'mode_change', mode }));
    }
  }, []);

  return { connected, reconnecting, sendMessage, sendAbort, respondPermission, sendModeChange, reconnect: connect };
}