import { useState } from 'react';
import type { ToolCallInfo } from '../types';
import { CHANNEL_META, TOOL_META, inferChannel, prettyJSON } from '../lib/format';

/** 工具调用卡片：状态图标 + 中文工具名 + 渠道标签，可展开查看输入/输出 */
export function ToolCallCard({ call }: { call: ToolCallInfo }) {
  const [open, setOpen] = useState(false);
  const meta = TOOL_META[call.toolName];
  const channel = inferChannel(call.input);

  return (
    <div className="animate-fade-up relative overflow-hidden rounded-lg border border-line bg-elevated shadow-card">
      {/* 渠道品牌色左条：一眼识别这次操作发生在哪个平台 */}
      <span
        className="absolute left-0 top-0 h-full w-[2.5px]"
        style={{ backgroundColor: channel ? CHANNEL_META[channel].color : 'var(--accent)' }}
      />
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2.5 py-2.5 pl-4 pr-3.5 text-left transition-colors hover:bg-black/[0.02]"
      >
        <StatusIcon status={call.status} />
        <span className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-md bg-accent-soft text-accent">
          <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d={meta?.icon ?? 'M12 8v4l3 3m6-3a9 9 0 1 1-18 0 9 9 0 0 1 18 0z'} />
          </svg>
        </span>
        <span className="whitespace-nowrap text-[13px] font-medium text-ink">{meta?.label ?? call.toolName}</span>
        <span className="hidden font-mono text-[11px] text-ink-3 sm:inline">{call.toolName}</span>
        {channel && (
          <span
            className="rounded px-1.5 py-px text-[10.5px] font-medium"
            style={{ backgroundColor: `${CHANNEL_META[channel].color}14`, color: CHANNEL_META[channel].color }}
          >
            {CHANNEL_META[channel].label}
          </span>
        )}
        <span className="ml-auto flex flex-shrink-0 items-center gap-1 font-mono text-[10.5px] text-ink-3 tnum">
          {call.status === 'running' ? '执行中…' : call.status === 'error' ? '失败' : '完成'}
          <svg
            className={`h-3 w-3 transition-transform ${open ? 'rotate-90' : ''}`}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          >
            <polyline points="9 18 15 12 9 6" />
          </svg>
        </span>
      </button>

      {open && (
        <div className="border-t border-line px-3.5 py-2.5">
          <div className="mb-1 text-[11px] font-medium text-ink-3">输入参数</div>
          <pre className="overflow-x-auto rounded-lg bg-inset px-3 py-2 font-mono text-[11.5px] leading-relaxed text-ink-2">
            {prettyJSON(call.input)}
          </pre>
          {call.result !== null && (
            <>
              <div className="mb-1 mt-2.5 text-[11px] font-medium text-ink-3">返回结果</div>
              <pre
                className={`overflow-x-auto rounded-lg px-3 py-2 font-mono text-[11.5px] leading-relaxed ${
                  call.isError ? 'bg-[#FDF0EF] text-danger' : 'bg-inset text-ink-2'
                }`}
              >
                {prettyJSON(call.result)}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function StatusIcon({ status }: { status: ToolCallInfo['status'] }) {
  if (status === 'running') {
    return <span className="h-3.5 w-3.5 flex-shrink-0 animate-spin rounded-full border-2 border-accent/25 border-t-accent" />;
  }
  if (status === 'error') {
    return (
      <svg className="h-3.5 w-3.5 flex-shrink-0 text-danger" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
        <circle cx="12" cy="12" r="10" />
        <line x1="15" y1="9" x2="9" y2="15" />
        <line x1="9" y1="9" x2="15" y2="15" />
      </svg>
    );
  }
  return (
    <svg className="h-3.5 w-3.5 flex-shrink-0 text-ok" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}
