import { useState } from 'react';
import type { ToolCallInfo } from '../types';
import { CHANNEL_META, TOOL_META, durationText, inferChannel, paramSummary, prettyJSON } from '../lib/format';

/**
 * 工具调用组（craft 式 activity rows）：一次回复的全部工具调用折叠进一个
 * ring 容器，行间 hairline 分隔。每行 = 状态图标 + 中文工具名 + 内联参数摘要
 * + 执行徽章（渠道 / 重试 / 幂等回放 / 耗时），点击展开输入输出与 Trace 元数据。
 * 对话是叙事，工具活动是折叠的行——不再一张一张浮卡打断阅读。
 */
export function ToolCallGroup({ calls }: { calls: ToolCallInfo[] }) {
  return (
    <div className="animate-fade-up overflow-hidden rounded-xl bg-elevated shadow-card">
      {calls.map((tc, i) => (
        <ToolCallRow key={tc.toolUseId} call={tc} separated={i > 0} />
      ))}
    </div>
  );
}

function ToolCallRow({ call, separated }: { call: ToolCallInfo; separated: boolean }) {
  const [open, setOpen] = useState(false);
  const meta = TOOL_META[call.toolName];
  const channel = inferChannel(call.input);
  const summary = paramSummary(call.toolName, call.input);
  const dur = durationText(call.durationMs);
  const retried = (call.attempt ?? 1) > 1;

  return (
    <div className={separated ? 'border-t border-line' : ''}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 py-[7px] pl-3.5 pr-3 text-left transition-colors hover:bg-black/[0.02]"
      >
        <StatusIcon status={call.status} />
        <span className="whitespace-nowrap text-[12.5px] font-medium text-ink">{meta?.label ?? call.toolName}</span>
        {summary ? (
          <span className="min-w-0 flex-1 truncate text-[11.5px] text-ink-3">{summary}</span>
        ) : (
          <span className="min-w-0 flex-1" />
        )}
        {channel && (
          <span
            className="badge"
            style={{
              backgroundColor: `${CHANNEL_META[channel].color}14`,
              color: CHANNEL_META[channel].color,
            }}
          >
            {CHANNEL_META[channel].label}
          </span>
        )}
        {retried && <span className="badge badge-warn">重试 {call.attempt} 次</span>}
        {call.idempotentReplay && <span className="badge badge-ok">幂等回放</span>}
        {dur && <span className="badge badge-neutral tnum">{dur}</span>}
        <span className="ml-1 flex flex-shrink-0 items-center gap-1 text-ink-3">
          {call.status === 'running' && <span className="text-[10.5px]">执行中…</span>}
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
        <div className="border-t border-line bg-black/[0.015] px-3.5 py-2.5">
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
          {(call.traceId || call.source || retried) && (
            <div className="mt-2 flex flex-wrap items-center gap-x-2 font-mono text-[10.5px] text-ink-3">
              {call.traceId && <span>trace {call.traceId}</span>}
              {call.source && <span>source {call.source}</span>}
              {retried && <span>attempt {call.attempt}</span>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function StatusIcon({ status }: { status: ToolCallInfo['status'] }) {
  if (status === 'running') {
    return <span className="h-3.5 w-3.5 flex-shrink-0 animate-spin rounded-full border-2 border-line-strong border-t-accent" />;
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
