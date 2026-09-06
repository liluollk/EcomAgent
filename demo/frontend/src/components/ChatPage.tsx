import { useCallback, useEffect, useRef } from 'react';
import type { ChatMessage, ModelProvider, PermissionModeType, Skill } from '../types';
import { ChatMessageView } from './ChatMessage';
import { ChatInput } from './ChatInput';
import { EmptyState } from './EmptyState';

interface ChatPageProps {
  messages: ChatMessage[];
  isStreaming: boolean;
  statusText: string;
  connected: boolean;
  mode: PermissionModeType;
  providers: ModelProvider[];
  activeProvider: string;
  onActivateProvider: (name: string) => void;
  onModeChange: (mode: PermissionModeType) => void;
  onSend: (text: string) => void;
  onAbort: () => void;
  onRespondPermission: (requestId: string, approved: boolean) => void;
  /** 技能菜单（输入框 / 命令补全） */
  skills: Skill[];
}

/** 对话页：消息流 + 底部输入（含模型/权限切换）；空状态展示快捷指令 */
export function ChatPage({
  messages,
  isStreaming,
  statusText,
  connected,
  mode,
  providers,
  activeProvider,
  onActivateProvider,
  onModeChange,
  onSend,
  onAbort,
  onRespondPermission,
  skills,
}: ChatPageProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [messages, isStreaming, statusText]);

  const empty = messages.length === 0 && !isStreaming;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {empty ? (
        <EmptyState onPrompt={onSend} />
      ) : (
        <div ref={scrollRef} onScroll={onScroll} className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-[1060px] space-y-4 px-5 py-5">
            {messages.map((m) => (
              <ChatMessageView key={m.id} msg={m} onRespondPermission={onRespondPermission} />
            ))}
            {isStreaming && (
              <div className="flex animate-fade-up items-center gap-2 text-[12px] text-ink-3">
                <span className="h-3 w-3 animate-spin rounded-full border-2 border-accent/25 border-t-accent" />
                {statusText || '正在思考…'}
              </div>
            )}
          </div>
        </div>
      )}
      <ChatInput
        onSend={onSend}
        onAbort={onAbort}
        isStreaming={isStreaming}
        disabled={!connected}
        mode={mode}
        providers={providers}
        activeProvider={activeProvider}
        onActivateProvider={onActivateProvider}
        onModeChange={onModeChange}
        skills={skills}
      />
    </div>
  );
}