import type { ChatMessage as Msg } from '../types';
import { clockTime } from '../lib/format';
import { Markdown } from '../lib/markdown';
import { ToolCallCard } from './ToolCallCard';
import { PermissionCard } from './PermissionCard';

/** 单条消息渲染：用户右侧气泡 / 助手全宽带头像 / 系统居中弱化 */
export function ChatMessageView({
  msg,
  onRespondPermission,
  externalDecision,
}: {
  msg: Msg;
  onRespondPermission: (requestId: string, approved: boolean) => void;
  externalDecision?: 'approved' | 'denied';
}) {
  if (msg.role === 'system') {
    if (msg.permission) {
      return (
        <PermissionCard
          permission={msg.permission}
          onRespond={onRespondPermission}
          externalDecision={externalDecision}
        />
      );
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
    <div className="animate-fade-up">
      <div className="min-w-0">
        <div className="flex items-baseline gap-2">
          <span className="text-[12.5px] font-semibold text-ink">电商运营 Agent</span>
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
