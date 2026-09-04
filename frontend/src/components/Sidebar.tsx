import { useState } from 'react';
import type { PermissionModeType, SessionMeta } from '../types';
import { relativeTime } from '../lib/format';
import { ROLE_OPTIONS } from '../lib/roles';

type Page = 'chat' | 'workspace';

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
];

interface SidebarProps {
  sessions: SessionMeta[];
  activeId: string | null;
  page: Page;
  connected: boolean;
  reconnecting: boolean;
  model: string;
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
  model,
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
      {/* 品牌 */}
      <div className="flex items-center gap-2.5 px-4 pt-4">
        <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-[10px] bg-gradient-to-br from-accent to-[#6FB6F2] text-white shadow-card">
          <WaveMark className="h-[18px] w-[18px]" />
        </div>
        <div className="min-w-0 leading-tight">
          <div className="truncate text-[13.5px] font-semibold text-ink">OceanBreeze</div>
          <div className="text-[11px] text-ink-3">电商运营 Agent</div>
        </div>
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
            className={`flex h-9 items-center gap-2.5 rounded-lg px-3 text-[13px] transition-colors ${
              page === item.id ? 'bg-elevated font-medium text-ink shadow-card' : 'text-ink-2 hover:bg-black/[0.04] hover:text-ink'
            }`}
          >
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
          新对话
        </button>

        {/* 身份角色选择（影响下一次创建会话的 user.role） */}
        <label className="mt-2 flex h-8 items-center gap-2 rounded-lg border border-line bg-sidebar px-2.5">
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
      </nav>

      {/* 弹性空间 */}
      <div className="min-h-4 flex-1" />

      {/* 历史会话（左下角，内部滚动） */}
      <div className="flex min-h-0 flex-col">
        <div className="flex items-baseline justify-between px-4 pb-1">
          <span className="text-[11px] font-medium text-ink-3">历史会话</span>
          <span className="text-[10.5px] text-ink-3">{sessions.length}</span>
        </div>
        <nav className="max-h-[38vh] overflow-y-auto px-2 pb-2">
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
          {sessions.length === 0 && <div className="px-3 py-2 text-[12px] text-ink-3">暂无历史会话</div>}
        </nav>
      </div>

      {/* 底部：设置 + 连接状态 */}
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
        <div className="px-3 pb-0.5 pt-0.5 text-[10.5px] text-ink-3">
          模型 <span className="font-mono">{model}</span>
        </div>
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

export function WaveMark({ className = 'h-[18px] w-[18px]' }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 12c1.5-2 4-4 6-2s4 4 6 2 4-4 6-2" />
      <path d="M2 17c1.5-2 4-4 6-2s4 4 6 2 4-4 6-2" />
    </svg>
  );
}