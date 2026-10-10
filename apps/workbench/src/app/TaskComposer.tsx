import { useState } from "react";

import { postJSON } from "../api/client";
import type { WorkspaceOverviewAgent } from "../features/workspaces/types";

interface SessionCreated {
  id: string;
}

interface RunDispatched {
  run_id: string;
}

export interface TaskComposerProps {
  workspaceId: string;
  agent: WorkspaceOverviewAgent | null;
  onSessionOpened: (sessionId: string) => void;
}

/**
 * 新任务编辑器:使用 Workspace overview 中已启用的 Agent 创建会话并发送首条消息。
 */
export function TaskComposer({ workspaceId, agent, onSessionOpened }: TaskComposerProps) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = async () => {
    const content = draft.trim();
    if (!content || sending) {
      return;
    }
    setSending(true);
    setError(null);
    try {
      if (!agent) {
        return;
      }
      const session = await postJSON<SessionCreated>("/sessions", {
        workspace_id: workspaceId,
      });
      await postJSON<RunDispatched>(`/sessions/${session.id}/messages`, { content });
      setDraft("");
      onSessionOpened(session.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSending(false);
    }
  };

  return (
    <section aria-label="新任务">
      <h2>提交新任务</h2>
      <textarea
        aria-label="新任务输入"
        value={draft}
        disabled={sending || !agent}
        onChange={(changeEvent) => setDraft(changeEvent.target.value)}
      />
      {!agent && <p>当前 Workspace 未启用商品运营 Agent</p>}
      <button
        type="button"
        onClick={send}
        disabled={sending || !agent || draft.trim().length === 0}
      >
        发送任务
      </button>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
