import { useCallback, useEffect, useRef, useState } from 'react';
import type { AgentEvent, PermissionModeType } from '../types';
import { MockEventStream } from '../mocks/mockEventStream';

interface UseMockWebSocketOptions {
  sessionId: string | null;
  onEvent: (event: AgentEvent) => void;
}

/**
 * mock WebSocket hook — 与 useWebSocket 完全同接口，但 connected 恒为 true。
 *
 * 内部用 MockEventStream 按会话生成模拟 AgentEvent 序列（无需真实后端），
 * 供展示版独立跑通完整交互：发消息 → 流式事件 → 工具卡 → 权限卡 → 完成。
 */
export function useMockWebSocket({ sessionId, onEvent }: UseMockWebSocketOptions) {
  const [connected] = useState(true);
  const [reconnecting] = useState(false);
  const onEventRef = useRef(onEvent);
  const streamRef = useRef<MockEventStream | null>(null);

  useEffect(() => {
    onEventRef.current = onEvent;
  }, [onEvent]);

  // sessionId 变化时重建事件流（模拟断旧连新）
  useEffect(() => {
    streamRef.current?.close();
    streamRef.current = sessionId ? new MockEventStream(sessionId, (ev) => onEventRef.current(ev), 'ASK') : null;
    return () => {
      streamRef.current?.close();
      streamRef.current = null;
    };
  }, [sessionId]);

  const sendMessage = useCallback((content: string) => {
    void streamRef.current?.sendMessage(content);
  }, []);

  const sendAbort = useCallback(() => {
    streamRef.current?.abort();
  }, []);

  const respondPermission = useCallback((requestId: string, approved: boolean) => {
    streamRef.current?.respondPermission(requestId, approved);
  }, []);

  const sendModeChange = useCallback((mode: PermissionModeType) => {
    streamRef.current?.sendModeChange(mode);
  }, []);

  const reconnect = useCallback(() => {
    /* mock 模式无需重连 */
  }, []);

  return { connected, reconnecting, sendMessage, sendAbort, respondPermission, sendModeChange, reconnect };
}