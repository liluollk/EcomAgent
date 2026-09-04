import { CHANNEL_LIST } from '../lib/format';

interface TopBarProps {
  title: string;
  roleLabel?: string;
  userId?: string;
}

export function TopBar({ title, roleLabel, userId }: TopBarProps) {
  return (
    <header className="flex h-[52px] flex-shrink-0 items-center justify-between gap-4 border-b border-line px-5">
      <h1 className="min-w-0 truncate text-[14px] font-semibold text-ink">{title}</h1>

      {/* 当前身份（RBAC） + 已接入渠道 */}
      <div className="flex flex-shrink-0 items-center gap-1.5">
        {roleLabel && (
          <span
            className="flex items-center gap-1.5 rounded-full border border-line bg-elevated px-2.5 py-1 text-[11px] text-ink-2"
            title={userId ? `user_id: ${userId}` : undefined}
          >
            <svg className="h-3 w-3 text-ink-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="8" r="4" />
              <path d="M4 21c0-4 3.6-6 8-6s8 2 8 6" />
            </svg>
            {roleLabel}
          </span>
        )}
        {CHANNEL_LIST.map((c) => (
          <span
            key={c.id}
            className="flex items-center gap-1.5 rounded-full border border-line bg-elevated px-2.5 py-1 text-[11px] text-ink-2"
          >
            <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: c.color }} />
            {c.label}
          </span>
        ))}
      </div>
    </header>
  );
}
