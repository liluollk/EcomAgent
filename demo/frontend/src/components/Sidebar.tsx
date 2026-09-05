import { useState } from 'react';
import type { PermissionModeType, SessionMeta } from '../types';
import { relativeTime } from '../lib/format';
import { ROLE_OPTIONS } from '../lib/roles';

export type Page = 'chat' | 'workspace' | 'skills' | 'mcp';

/** 功能导航（左上角） */
const NAV_ITEMS: { id: Page; label: string; icon: string }[] = [
  {
    id: 'chat',
    label: '对话',
    icon: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z',
  },
  {
    id: 'workspace',
    label: '工作台',
    icon: 'M3 3h7v7H3z M14 3h7v7h-7z M14 14h7v7h-7z M3 14h7v7H3z',
  },
  {
    id: 'skills',
    label: '技能',
    icon: 'M13 2 3 14h9l-1 8 10-12h-9l1-8z',
  },
  {
    id: 'mcp',
    label: 'MCP',
    icon: 'M2 5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z M2 15a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z M6 7.5h.01 M6 17.5h.01',
  },
];

interface SidebarProps {
  sessions: SessionMeta[];
  activeId: string | null;
  page: Page;
  connected: boolean;
  reconnecting: boolean;
  mode: PermissionModeType;
  role: string;
  onRoleChange: (role: string) => void;
  onNav: (page: Page) => void;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
  onOpenSettings: () => void;
}

export function Sidebar({
  sessions,
  activeId,
  page,
  connected,
  reconnecting,
  onNav,
  onSelect,
  onCreate,
  onDelete,
  onOpenSettings,
  role,
  onRoleChange,
}: SidebarProps) {
  const [confirmId, setConfirmId] = useState<string | null>(null);

  return (
    <aside className="flex w-[264px] flex-shrink-0 flex-col bg-sidebar">
      {/* 品牌（纯文字，无 logo） */}
      <div className="px-4 pb-1 pt-5">
        <div className="text-[14px] font-semibold tracking-tight text-ink">电商运营 Agent 平台</div>
        <div className="mt-0.5 text-[11px] text-ink-3">多渠道智能运营</div>
      </div>

      {/* 功能导航（左上角） */}
      <nav className="mt-4 flex flex-col gap-0.5 px-2">
        {NAV_ITEMS.map((item) => (
          <button
            key={item.id}
            onClick={() => {
              setConfirmId(null);
              onNav(item.id);
            }}
            className={`relative flex h-9 items-center gap-2.5 rounded-lg px-3 text-[13px] transition-colors ${
              page === item.id ? 'bg-elevated font-medium text-ink' : 'text-ink-2 hover:bg-black/[0.04] hover:text-ink'
            }`}
          >
            {page === item.id && (
              <span className="absolute left-0 top-1/2 h-4 w-[2.5px] -translate-y-1/2 rounded-r-full bg-accent" />
            )}
            <svg
              className={`h-4 w-4 ${page === item.id ? 'text-accent' : 'text-ink-3'}`}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d={item.icon} />
            </svg>
            {item.label}
          </button>
        ))}
        <button
          onClick={() => {
            setConfirmId(null);
            onCreate();
          }}
          className="mt-2 flex h-9 items-center justify-center gap-1.5 rounded-lg bg-accent text-[13px] font-medium text-white shadow-card transition-colors hover:bg-accent-hover"
        >
          <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
          </svg>
          新建任务
        </button>
      </nav>

      {/* 历史会话（中部，内部可滑动） */}
      <div className="mt-3 flex min-h-0 flex-1 flex-col border-t border-line/70 pt-2">
        <div className="flex items-baseline justify-between px-4 pb-1">
          <span className="text-[11px] font-medium text-ink-3">任务记录</span>
          <span className="text-[10.5px] text-ink-3">{sessions.length}</span>
        </div>
        <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
          {sessions.map((s) => (
            <SessionItem
              key={s.session_id}
              session={s}
              active={s.session_id === activeId && page === 'chat'}
              confirming={confirmId === s.session_id}
              onSelect={() => {
                setConfirmId(null);
                onSelect(s.session_id);
              }}
              onRequestDelete={() => setConfirmId(s.session_id)}
              onCancelDelete={() => setConfirmId(null)}
              onConfirmDelete={() => {
                setConfirmId(null);
                onDelete(s.session_id);
              }}
            />
          ))}
          {sessions.length === 0 && <div className="px-3 py-2 text-[12px] text-ink-3">暂无任务记录</div>}
        </nav>
      </div>

      {/* 左下角：设置 + 身份切换 + 连接状态 */}
      <div className="flex-shrink-0 border-t border-line px-2 py-2">
        <div className="flex items-center justify-between">
          <button
            onClick={onOpenSettings}
            className="flex h-8 flex-1 items-center gap-2.5 rounded-lg px-3 text-[13px] text-ink-2 transition-colors hover:bg-black/[0.04] hover:text-ink"
          >
            <svg className="h-4 w-4 text-ink-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
            设置
          </button>
          <span
            title={connected ? '已连接' : reconnecting ? '重连中…' : '未连接'}
            className={`mr-2 h-2 w-2 flex-shrink-0 rounded-full ${
              connected ? 'bg-ok' : reconnecting ? 'animate-pulse bg-warn' : 'bg-danger'
            }`}
          />
        </div>
        {/* 身份角色选择（影响下一次创建会话的 user.role） */}
        <label className="mt-1 flex h-8 items-center gap-2 rounded-lg border border-line bg-sidebar px-3">
          <span className="flex-shrink-0 text-[11px] text-ink-3">身份</span>
          <select
            value={role}
            onChange={(e) => onRoleChange(e.target.value)}
            className="h-full min-w-0 flex-1 bg-transparent text-[12.5px] text-ink outline-none"
            title="新建会话时使用的身份角色（RBAC）"
          >
            {ROLE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
      </div>
    </aside>
  );
}

function SessionItem({
  session,
  active,
  confirming,
  onSelect,
  onRequestDelete,
  onCancelDelete,
  onConfirmDelete,
}: {
  session: SessionMeta;
  active: boolean;
  confirming: boolean;
  onSelect: () => void;
  onRequestDelete: () => void;
  onCancelDelete: () => void;
  onConfirmDelete: () => void;
}) {
  const title = session.title || `会话 ${session.session_id}`;
  const sub = session.createdAt
    ? relativeTime(session.createdAt)
    : session.message_count != null
      ? `${session.message_count} 条消息`
      : session.status.toLowerCase();

  return (
    <div
      onClick={onSelect}
      className={`group flex cursor-pointer items-center justify-between gap-2 rounded-lg px-3 py-2 transition-colors ${
        active ? 'bg-elevated shadow-card' : 'hover:bg-black/[0.04]'
      }`}
    >
      {confirming ? (
        <div className="flex flex-1 items-center justify-between gap-1">
          <span className="whitespace-nowrap text-[12px] text-ink-2">删除该会话？</span>
          <div className="flex flex-shrink-0 gap-1">
            <button
              onClick={(e) => {
                e.stopPropagation();
                onConfirmDelete();
              }}
              className="rounded-md bg-danger px-2 py-0.5 text-[11px] font-medium text-white hover:opacity-90"
            >
              删除
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onCancelDelete();
              }}
              className="rounded-md px-2 py-0.5 text-[11px] text-ink-2 hover:bg-black/5"
            >
              取消
            </button>
          </div>
        </div>
      ) : (
        <>
          <div className="min-w-0">
            <div className={`truncate text-[13px] ${active ? 'font-medium text-ink' : 'text-ink-2'}`}>{title}</div>
            <div className="text-[11px] text-ink-3">{sub}</div>
          </div>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onRequestDelete();
            }}
            className="flex-shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
          >
            <svg className="h-3.5 w-3.5 text-ink-3 hover:text-danger" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="3 6 5 6 21 6" />
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
            </svg>
          </button>
        </>
      )}
    </div>
  );
}
