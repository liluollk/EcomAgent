import { useCallback, useEffect, useRef, useState } from 'react';
import type { AgentEvent, ChatMessage, ModelProvider, PermissionModeType, SessionMeta, ToolCallInfo } from './types';
import { useWebSocket } from './hooks/useWebSocket';
import { Sidebar, type Page } from './components/Sidebar';
import { TopBar } from './components/TopBar';
import { ROLE_LABELS } from './lib/roles';
import { ChatPage } from './components/ChatPage';
import { WorkspacePage } from './components/workspace/WorkspacePage';
import { ApprovalsPage } from './components/pages/ApprovalsPage';
import { DashboardPage } from './components/pages/DashboardPage';
import { ComingSoon } from './components/pages/ComingSoon';
import { SettingsModal } from './components/SettingsModal';
import { ToastHost } from './components/Toast';
import { uid } from './lib/format';

const PAGE_TITLES: Record<Page, string> = {
  dashboard: '运营看板',
  chat: '对话',
  approvals: '审批中心',
  workspace: '工作台',
  skills: '技能',
  mcp: 'MCP',
};

const MODE_LABEL: Record<PermissionModeType, string> = {
  READONLY: '只读模式',
  ASK: '询问模式',
  EXECUTE: '执行模式',
};

/** localStorage 迁移：旧版 SAFE 模式名统一为 READONLY */
function loadStoredMode(): PermissionModeType {
  const stored = localStorage.getItem('ob.mode');
  if (stored === 'SAFE') {
    localStorage.setItem('ob.mode', 'READONLY');
    return 'READONLY';
  }
  return stored === 'ASK' || stored === 'EXECUTE' ? stored : 'ASK';
}

export default function App() {
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messagesBySession, setMessagesBySession] = useState<Record<string, ChatMessage[]>>({});
  const [isStreaming, setIsStreaming] = useState(false);
  const [statusText, setStatusText] = useState('');
  const [connError, setConnError] = useState<string | null>(null);
  const [page, setPage] = useState<Page>('chat');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [mode, setMode] = useState<PermissionModeType>(loadStoredMode);
  const [activeProvider, setActiveProvider] = useState(() => localStorage.getItem('ob.provider') || 'openai');
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [role, setRole] = useState<string>(() => localStorage.getItem('ob.role') || 'operator');
  /** 审批中心等外部入口的决定（requestId → 结果）：同步对话内仍挂"等待确认"的权限卡 */
  const [externalDecisions, setExternalDecisions] = useState<Record<string, 'approved' | 'denied'>>({});

  const activeIdRef = useRef<string | null>(null);
  activeIdRef.current = activeId;
  const sessionsRef = useRef<SessionMeta[]>([]);
  sessionsRef.current = sessions;

  const pushMessage = useCallback((sid: string, msg: ChatMessage) => {
    setMessagesBySession((prev) => ({ ...prev, [sid]: [...(prev[sid] ?? []), msg] }));
  }, []);

  /** 从后端拉取会话历史消息（含工具调用结构），用于会话恢复展示 */
  const loadHistory = useCallback(async (sid: string) => {
    try {
      const resp = await fetch(`/sessions/${sid}/messages`);
      if (!resp.ok) return;
      const raw = (await resp.json()) as Array<{
        role: string;
        content: string;
        timestamp: string;
        tool_calls?: Array<{ id: string; function?: { name?: string; arguments?: string } }>;
        tool_call_id?: string;
      }>;
      if (!Array.isArray(raw)) return;

      const toolResults = new Map<string, { result: string; isError: boolean }>();
      const msgs: ChatMessage[] = [];
      for (const m of raw) {
        if (m.role === 'tool') {
          // 工具结果并入对应 assistant 消息的工具卡片中展示
          toolResults.set(m.tool_call_id ?? '', {
            result: m.content,
            isError: m.content.startsWith('['),
          });
          continue;
        }
        if (m.role !== 'user' && m.role !== 'assistant') continue;
        const ts = Date.parse(m.timestamp) || Date.now();
        if (m.role === 'assistant' && m.tool_calls?.length) {
          msgs.push({
            id: `h-${ts}-${msgs.length}`,
            role: 'assistant',
            content: m.content ?? '',
            toolCalls: m.tool_calls.map((tc) => {
              let input: Record<string, unknown> = {};
              try {
                input = tc.function?.arguments ? JSON.parse(tc.function.arguments) : {};
              } catch {
                input = {};
              }
              const tr = toolResults.get(tc.id);
              return {
                toolUseId: tc.id,
                toolName: tc.function?.name ?? '',
                input,
                result: tr?.result ?? null,
                isError: tr?.isError ?? false,
                status: tr ? (tr.isError ? ('error' as const) : ('done' as const)) : ('done' as const),
              };
            }),
            isStreaming: false,
            timestamp: ts,
          });
        } else {
          msgs.push({
            id: `h-${ts}-${msgs.length}`,
            role: m.role,
            content: m.content,
            toolCalls: [],
            isStreaming: false,
            timestamp: ts,
          });
        }
      }
      setMessagesBySession((prev) => ({ ...prev, [sid]: msgs }));
    } catch {
      /* 历史加载失败不阻塞会话使用 */
    }
  }, []);

  /** 从后端拉取供应商列表与当前激活项（输入框模型切换与设置页共用此状态） */
  const fetchProviders = useCallback(async () => {
    try {
      const resp = await fetch('/providers');
      if (!resp.ok) return;
      const data = (await resp.json()) as { active?: string; providers?: ModelProvider[] };
      if (Array.isArray(data.providers)) setProviders(data.providers);
      if (data.active) {
        setActiveProvider(data.active);
        localStorage.setItem('ob.provider', data.active);
      }
    } catch {
      /* 加载失败不阻塞 */
    }
  }, []);

  /** 切换激活供应商（输入框模型选择器调用，成功后刷新列表） */
  const activateProvider = useCallback(
    async (name: string) => {
      try {
        const resp = await fetch(`/providers/${name}/activate`, { method: 'POST' });
        if (resp.ok) await fetchProviders();
      } catch {
        /* 失败静默：列表刷新时自然回正 */
      }
    },
    [fetchProviders],
  );

  /** 处理后端 AgentEvent 流 */
  const handleEvent = useCallback(
    (event: AgentEvent) => {
      const sid = activeIdRef.current;
      if (!sid) return;

      switch (event.type) {
        case 'text_delta':
          setMessagesBySession((prev) => {
            const list = prev[sid] ?? [];
            const last = list[list.length - 1];
            if (last && last.role === 'assistant') {
              return {
                ...prev,
                [sid]: [...list.slice(0, -1), { ...last, content: last.content + event.text, isStreaming: true }],
              };
            }
            return {
              ...prev,
              [sid]: [
                ...list,
                { id: uid(), role: 'assistant', content: event.text, toolCalls: [], isStreaming: true, timestamp: Date.now() },
              ],
            };
          });
          break;

        case 'tool_start': {
          const tc: ToolCallInfo = {
            toolUseId: event.tool_use_id,
            toolName: event.tool_name,
            input: event.input,
            result: null,
            isError: false,
            status: 'running',
          };
          setMessagesBySession((prev) => {
            const list = prev[sid] ?? [];
            const last = list[list.length - 1];
            if (last && last.role === 'assistant') {
              return { ...prev, [sid]: [...list.slice(0, -1), { ...last, toolCalls: [...last.toolCalls, tc] }] };
            }
            return {
              ...prev,
              [sid]: [...list, { id: uid(), role: 'assistant', content: '', toolCalls: [tc], isStreaming: true, timestamp: Date.now() }],
            };
          });
          setStatusText('');
          break;
        }

        case 'tool_result': {
          setMessagesBySession((prev) => {
            const list = prev[sid] ?? [];
            const next = [...list];
            for (let i = next.length - 1; i >= 0; i--) {
              const m = next[i];
              if (m.role !== 'assistant') continue;
              if (!m.toolCalls.some((tc) => tc.toolUseId === event.tool_use_id)) continue;
              next[i] = {
                ...m,
                toolCalls: m.toolCalls.map((tc) =>
                  tc.toolUseId === event.tool_use_id
                    ? {
                        ...tc,
                        result: event.result,
                        isError: event.is_error,
                        status: event.is_error ? ('error' as const) : ('done' as const),
                      }
                    : tc,
                ),
              };
              return { ...prev, [sid]: next };
            }
            return prev;
          });
          // 工具已出结果但权限卡仍在等待 → 决定来自审批中心等外部入口，同步卡片状态
          const permId = `perm_${event.tool_use_id}`;
          setExternalDecisions((prev) => {
            if (prev[permId]) return prev;
            if (!event.is_error) return { ...prev, [permId]: 'approved' };
            if (event.result.startsWith('[已拒绝]')) return { ...prev, [permId]: 'denied' };
            return prev;
          });
          break;
        }

        case 'permission_request':
          pushMessage(sid, {
            id: uid(),
            role: 'system',
            content: '',
            toolCalls: [],
            isStreaming: false,
            timestamp: Date.now(),
            permission: {
              requestId: event.request_id,
              toolName: event.tool_name,
              toolInput: event.tool_input,
              reason: event.reason,
            },
          });
          break;

        case 'status':
          setStatusText(event.message);
          break;

        case 'mode_change':
          setMode(event.mode);
          localStorage.setItem('ob.mode', event.mode);
          break;

        case 'complete':
        case 'turn_complete':
          setIsStreaming(false);
          setStatusText('');
          setMessagesBySession((prev) => {
            const list = prev[sid] ?? [];
            const last = list[list.length - 1];
            if (last && last.role === 'assistant' && last.isStreaming) {
              return { ...prev, [sid]: [...list.slice(0, -1), { ...last, isStreaming: false }] };
            }
            return prev;
          });
          break;

        case 'abort':
          // 后端 AbortEvent：中断提示统一由事件驱动（含中断原因），本地不再重复 push
          setIsStreaming(false);
          setStatusText('');
          pushMessage(sid, {
            id: uid(),
            role: 'system',
            content: event.reason ? `已中断（${event.reason}）` : '已中断',
            toolCalls: [],
            isStreaming: false,
            timestamp: Date.now(),
          });
          break;

        case 'error':
          setConnError(event.message);
          pushMessage(sid, {
            id: uid(),
            role: 'system',
            content: `出错：${event.message}`,
            toolCalls: [],
            isStreaming: false,
            timestamp: Date.now(),
          });
          setIsStreaming(false);
          setStatusText('');
          break;
      }
    },
    [pushMessage],
  );

  const { connected, reconnecting, sendMessage, sendAbort, respondPermission, sendModeChange } = useWebSocket({
    sessionId: activeId,
    onEvent: handleEvent,
  });

  /** 创建新会话（用当前选择的身份角色） */
  const createSession = useCallback(async () => {
    try {
      const resp = await fetch('/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role }),
      });
      const data = await resp.json();
      const meta: SessionMeta = { ...data, createdAt: Date.now() };
      setSessions((prev) => [meta, ...prev.filter((s) => s.session_id !== data.session_id)]);
      setMessagesBySession((prev) => ({ ...prev, [data.session_id]: prev[data.session_id] ?? [] }));
      setActiveId(data.session_id);
      setConnError(null);
    } catch (e) {
      setConnError(`创建会话失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }, [role]);

  /** 初始化：加载会话列表与当前激活供应商；为空则自动创建一个 */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch('/sessions');
        const list = (await resp.json()) as SessionMeta[];
        if (cancelled) return;
        if (list.length > 0) {
          setSessions([...list].reverse());
          setActiveId(list[list.length - 1].session_id);
          void loadHistory(list[list.length - 1].session_id);
        } else {
          await createSession();
        }
      } catch {
        if (!cancelled) {
          setConnError('无法连接后端服务，请先启动：uvicorn transport.server:app --port 8000');
        }
      }
    })();
    void fetchProviders();
    return () => {
      cancelled = true;
    };
  }, [createSession, loadHistory, fetchProviders]);

  /** 删除会话；若删除的是当前会话则切换到最近一个 */
  const deleteSession = useCallback(
    async (id: string) => {
      try {
        await fetch(`/sessions/${id}`, { method: 'DELETE' });
      } catch {
        /* 忽略删除错误 */
      }
      setSessions((prev) => prev.filter((s) => s.session_id !== id));
      setMessagesBySession((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      if (activeIdRef.current === id) {
        const rest = sessionsRef.current.filter((s) => s.session_id !== id);
        if (rest.length > 0) {
          setActiveId(rest[0].session_id);
        } else {
          await createSession();
        }
      }
    },
    [createSession],
  );

  /** 切换会话；若当前正在生成则先中断，并拉取目标会话的历史消息与真实权限模式 */
  const switchSession = useCallback(
    (id: string) => {
      if (id === activeIdRef.current) return;
      if (isStreaming) sendAbort();
      setIsStreaming(false);
      setStatusText('');
      setActiveId(id);
      void loadHistory(id);
      void (async () => {
        try {
          const resp = await fetch(`/sessions/${id}/mode`);
          if (!resp.ok) return;
          const data = (await resp.json()) as { mode?: PermissionModeType };
          if (data.mode) {
            setMode(data.mode);
            localStorage.setItem('ob.mode', data.mode);
          }
        } catch {
          /* 拉取失败不阻塞切换 */
        }
      })();
    },
    [isStreaming, sendAbort, loadHistory],
  );

  /** 发送用户消息 */
  const handleSend = useCallback(
    (content: string) => {
      const sid = activeIdRef.current;
      if (!sid || isStreaming) return;
      pushMessage(sid, {
        id: uid(),
        role: 'user',
        content,
        toolCalls: [],
        isStreaming: false,
        timestamp: Date.now(),
      });
      setSessions((prev) =>
        prev.map((s) =>
          s.session_id === sid && !s.title
            ? { ...s, title: content.length > 24 ? `${content.slice(0, 24)}…` : content }
            : s,
        ),
      );
      setIsStreaming(true);
      setStatusText('正在思考…');
      setConnError(null);
      sendMessage(content);
    },
    [isStreaming, pushMessage, sendMessage],
  );

  /** 中断生成：发送 WS abort，中断提示由后端 AbortEvent 统一回推展示 */
  const handleAbort = useCallback(() => {
    sendAbort();
    setIsStreaming(false);
    setStatusText('');
  }, [sendAbort]);

  /** 切换权限模式：写入后端（持久化 + 广播 mode_change），本地乐观更新 */
  const handleModeChange = useCallback(
    (m: PermissionModeType) => {
      setMode(m);
      localStorage.setItem('ob.mode', m);
      const sid = activeIdRef.current;
      if (sid) {
        pushMessage(sid, {
          id: uid(),
          role: 'system',
          content: `权限模式已切换为「${MODE_LABEL[m]}」`,
          toolCalls: [],
          isStreaming: false,
          timestamp: Date.now(),
        });
        // 后端持久化并广播 mode_change；WS 连接上会收到回写事件保持同步。
        void fetch(`/sessions/${sid}/mode`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: m }),
        }).catch(() => {});
      }
    },
    [pushMessage],
  );

  /** 切换思考强度：PATCH 当前供应商 thinking_level，成功后刷新供应商列表 */
  const handleThinkingLevelChange = useCallback(
    async (level: string) => {
      const name = activeProvider;
      if (!name) return;
      try {
        const resp = await fetch(`/providers/${name}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ thinking_level: level || null }),
        });
        if (resp.ok) {
          await fetchProviders();
        }
      } catch {
        /* 失败静默 */
      }
    },
    [activeProvider, fetchProviders],
  );

  const activeMessages = activeId ? messagesBySession[activeId] ?? [] : [];
  const activeTitle = sessions.find((s) => s.session_id === activeId)?.title ?? '';
  const activeRole = sessions.find((s) => s.session_id === activeId)?.user?.role ?? '';
  const activeRoleLabel = ROLE_LABELS[activeRole] ?? '';
  const activeUserId = sessions.find((s) => s.session_id === activeId)?.user?.user_id ?? '';
  const thinkingLevel = providers.find((p) => p.name === activeProvider)?.thinking_level ?? 'high';

  const handleRoleChange = (next: string) => {
    setRole(next);
    localStorage.setItem('ob.role', next);
  };

  return (
    <div className="flex h-screen w-screen text-ink">
      <ToastHost />
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        page={page}
        connected={connected}
        reconnecting={reconnecting}
        mode={mode}
        role={role}
        onRoleChange={handleRoleChange}
        onNav={setPage}
        onSelect={(id) => {
          setPage('chat');
          switchSession(id);
        }}
        onCreate={async () => {
          setPage('chat');
          await createSession();
        }}
        onDelete={deleteSession}
        onOpenSettings={() => setSettingsOpen(true)}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        {connError && (
          <div className="flex flex-shrink-0 items-center gap-2 border-b border-[#F0D48A] bg-[#FFFBEB] px-5 py-2 text-[12.5px] text-[#92610A]">
            <svg className="h-3.5 w-3.5 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
              <line x1="12" y1="9" x2="12" y2="13" />
              <line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
            <span className="flex-1 truncate">{connError}</span>
            <button onClick={() => setConnError(null)} className="flex-shrink-0 text-[11.5px] underline hover:no-underline">
              关闭
            </button>
          </div>
        )}

        <TopBar
          title={page === 'chat' ? activeTitle || PAGE_TITLES.chat : PAGE_TITLES[page]}
          roleLabel={activeRoleLabel}
          userId={activeUserId}
        />

        {page === 'chat' ? (
          <ChatPage
            messages={activeMessages}
            isStreaming={isStreaming}
            statusText={statusText}
            connected={connected}
            mode={mode}
            providers={providers}
            activeProvider={activeProvider}
            onActivateProvider={(name) => void activateProvider(name)}
            onModeChange={handleModeChange}
            onSend={handleSend}
            onAbort={handleAbort}
            onRespondPermission={respondPermission}
            externalDecisions={externalDecisions}
            thinkingLevel={thinkingLevel}
            onThinkingLevelChange={handleThinkingLevelChange}
          />
        ) : page === 'dashboard' ? (
          <DashboardPage />
        ) : page === 'approvals' ? (
          <ApprovalsPage />
        ) : page === 'workspace' ? (
          <WorkspacePage onOpenChat={() => setPage('chat')} />
        ) : page === 'skills' ? (
          <ComingSoon
            title="技能管理"
            desc="技能机制已在引擎层实现（渐进式加载：load_skill 元工具 + SOP 注入 + 工具门控），管理界面开发中。"
          />
        ) : (
          <ComingSoon
            title="MCP 管理"
            desc="MCP 工具服务运行中（stdio 子进程，运营工具经 JSON-RPC 真实发现并注入执行链路），管理界面开发中。"
          />
        )}
      </div>

      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        mode={mode}
        onModeChange={handleModeChange}
        activeProvider={activeProvider}
        onProvidersChange={fetchProviders}
      />
    </div>
  );
}