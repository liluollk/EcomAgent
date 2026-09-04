import type { ChatMessage as Msg } from '../types';
import { clockTime } from '../lib/format';
import { Markdown } from '../lib/markdown';
import { ToolCallCard } from './ToolCallCard';
import { PermissionCard } from './PermissionCard';
import { WaveMark } from './Sidebar';

/** 单条消息渲染：用户右侧气泡 / 助手全宽带头像 / 系统居中弱化 */
export function ChatMessageView({
  msg,
  onRespondPermission,
}: {
  msg: Msg;
  onRespondPermission: (requestId: string, approved: boolean) => void;
}) {
  if (msg.role === 'system') {
    if (msg.permission) {
      return <PermissionCard permission={msg.permission} onRespond={onRespondPermission} />;
    }
    return (
      <div className="flex justify-center">
        <span className="rounded-full bg-black/[0.04] px-3 py-1 text-[11.5px] text-ink-3">{msg.content}</span>
      </div>
    );
  }

  if (msg.role === 'user') {
    return (
      <div className="flex flex-col items-end animate-fade-up">
        <div className="max-w-[78%] whitespace-pre-wrap break-words rounded-2xl rounded-br-[6px] bg-[#E3EEFD] px-4 py-2.5 text-[14px] leading-relaxed text-ink">
          {msg.content}
        </div>
        <span className="mt-1 text-[10.5px] text-ink-3">{clockTime(msg.timestamp)}</span>
      </div>
    );
  }

  return (
    <div className="flex gap-3 animate-fade-up">
      <div className="mt-0.5 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-accent to-[#6FB6F2] text-white shadow-card">
        <WaveMark className="h-4 w-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="text-[12.5px] font-semibold text-ink">OceanBreeze</span>
          <span className="text-[10.5px] text-ink-3">{clockTime(msg.timestamp)}</span>
        </div>

        {(msg.content || msg.toolCalls.length === 0) && (
          <div className="mt-1">
            <Markdown text={msg.content} />
            {msg.isStreaming && <span className="caret" />}
          </div>
        )}

        {msg.toolCalls.length > 0 && (
          <div className="mt-2 space-y-2">
            {msg.toolCalls.map((tc) => (
              <ToolCallCard key={tc.toolUseId} call={tc} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
